"""AI cover artwork through OpenRouter, in two steps.

1. A text model reads the story's title, category, tags, description and some of its text, and writes
   a short visual description (the "scene") for the artwork.
2. An image model paints that scene as 3:4 artwork, either with the title and author lettered into the
   picture, or without lettering so LitKeeper adds them (Settings → AI Covers → Cover lettering).

The artwork is kept in `covers/ai/<id>_<base>.art.jpg`, with the scene, models and cost in
`covers/ai/<id>_<base>.art.json`. The story's normal cover (`covers/<id>_<base>.jpg`) is then drawn from
it and put into the EPUB. Covers are made one at a time, in a background thread, only when asked:
from the story window, or after each download when that is turned on. Nothing is sent to OpenRouter
unless an API key is saved.
"""
from __future__ import annotations
import base64
import http.client
import io
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from . import ai_prompts
from .logger import log_action, log_error

KEY = 'ai_covers'
DEFAULT_TEXT_MODEL = 'deepseek/deepseek-v4-flash'
DEFAULT_IMAGE_MODEL = 'bytedance-seed/seedream-5-0-flash'
API = 'https://openrouter.ai/api/v1'
CHAT = f'{API}/chat/completions'
IMAGES = f'{API}/images'
MODEL_NAME = re.compile(r'[@\w./:-]+')

# How much of the story the text model reads: (label, words in all).
STORY_TEXT = {
    'opening': ('Opening only', 1200),
    'excerpts': ('All of part 1 plus the first 20% of the next 5 parts (up to 40,000 words)', 40000),
    'whole': ('The whole story (up to 40,000 words)', 40000),
}
LATER_PART_SHARE = 0.2   # The excerpt defaults: 20% of each of the next 5 parts, 40,000 words in all.
EXCERPT_DEFAULTS = {'excerpt_percent': 20, 'excerpt_parts': 5, 'excerpt_words': 40000}
EXCERPT_WORDS_RANGE = (1000, 200000)
LETTERING = {
    'image': 'The image model letters the title and author',
    'litkeeper': 'LitKeeper adds the title and author',
}
DEFAULTS = {
    'api_key': '', 'text_model': DEFAULT_TEXT_MODEL, 'image_model': DEFAULT_IMAGE_MODEL, 'fallback_models': [],
    'auto': True, 'story_text': 'excerpts', 'lettering': 'image', 'retry_unlettered': True, 'retry_softer': True,
    'parallel': 3, **EXCERPT_DEFAULTS,
}
PARALLEL_MAX = 5   # covers made at the same time; OpenRouter's rate limits make more pointless

_queue: deque = deque()
_queued_at: dict = {}   # story id -> when it joined the queue (ISO time)
_active: dict = {}      # story id -> {'progress': text, 'started': ISO time} for covers being made now
_workers = 0            # worker threads running; each makes one cover at a time
_current = threading.local()  # which story this worker thread is making, for progress updates
_lock = threading.Lock()


# --- Settings -------------------------------------------------------------------------------

def settings() -> dict:
    from app.models import AppConfig
    config = AppConfig.query.filter_by(key=KEY).first()
    value = config.get_value() if config else None
    saved = dict(value) if isinstance(value, dict) else {}
    # "Make an AI cover for each newly downloaded story" became on by default. Settings saved before
    # then hold the old default (off) without anyone having chosen it, so that value is not used.
    if not saved.get('auto_chosen'):
        saved.pop('auto', None)
    return {**DEFAULTS, **saved}


def excerpt_label(config: Optional[dict] = None) -> str:
    """The excerpt option as Settings shows it, e.g. "All of part 1 plus the first 20% of the next 5 parts
    (up to 40,000 words)"; with no number of parts, "… of every later part …"."""
    config = {**EXCERPT_DEFAULTS, **(config or {})}
    parts = config.get('excerpt_parts')
    which = 'every later part' if not parts else ('the next part' if parts == 1 else f'the next {parts} parts')
    return f"All of part 1 plus the first {config['excerpt_percent']}% of {which} (up to {config['excerpt_words']:,} words)"


def story_text_options(config: Optional[dict] = None) -> dict:
    """STORY_TEXT with the excerpt option's label and word limit from the settings."""
    config = {**EXCERPT_DEFAULTS, **(config or {})}
    return {**STORY_TEXT, 'excerpts': (excerpt_label(config), config['excerpt_words'])}


def masked_key(key: str) -> str:
    return f'••••{key[-4:]}' if len(key or '') > 8 else ('••••' if key else '')


def public_settings() -> dict:
    """Settings for the browser: the API key is replaced by a masked hint."""
    current = settings()
    return {**{k: v for k, v in current.items() if k != 'api_key'},
            'has_key': bool(current['api_key']), 'key_hint': masked_key(current['api_key'])}


def _model_names(value, label: str, limit: int) -> list[str]:
    names = value if isinstance(value, list) else re.split(r'[\s,]+', value or '')
    names = [n for n in dict.fromkeys(str(n).strip() for n in names) if n]
    for name in names:
        if len(name) > 120 or not MODEL_NAME.fullmatch(name):
            raise ValueError(f'"{name}" is not an OpenRouter model name.')
    if len(names) > limit:
        raise ValueError(f'List at most {limit} {label}.')
    return names


def save_settings(form: dict) -> dict:
    """Validate and save the Settings → AI Covers form. A blank API key keeps the saved one;
    `clear_key` removes it."""
    from app.models import AppConfig, db
    current = settings()
    new = dict(current)
    key = str(form.get('api_key') or '').strip()
    if form.get('clear_key'):
        new['api_key'] = ''
    elif key:
        if len(key) > 200 or not re.fullmatch(r'[\w-]+', key):
            raise ValueError('That does not look like an OpenRouter API key (it usually starts with sk-or-).')
        new['api_key'] = key
    for field, default in (('text_model', DEFAULT_TEXT_MODEL), ('image_model', DEFAULT_IMAGE_MODEL)):
        if field in form:
            name = str(form.get(field) or '').strip() or default
            if len(name) > 120 or not MODEL_NAME.fullmatch(name):
                raise ValueError(f'"{name}" is not an OpenRouter model name (for example {default}).')
            new[field] = name
    if 'parallel' in form:
        try:
            parallel = int(form.get('parallel'))
        except (TypeError, ValueError):
            raise ValueError('Covers at a time must be a whole number.')
        if not 1 <= parallel <= PARALLEL_MAX:
            raise ValueError(f'Covers at a time must be from 1 to {PARALLEL_MAX}.')
        new['parallel'] = parallel
    if 'fallback_models' in form:
        new['fallback_models'] = _model_names(form.get('fallback_models'), 'fallback image models', 5)
    if 'excerpt_percent' in form:
        try:
            percent = int(str(form.get('excerpt_percent')).strip().rstrip('%'))
        except (TypeError, ValueError):
            raise ValueError('The share of each later part must be a whole number of percent.')
        if not 1 <= percent <= 100:
            raise ValueError('The share of each later part must be from 1% to 100%.')
        new['excerpt_percent'] = percent
    if 'excerpt_parts' in form:
        raw = str(form.get('excerpt_parts') if form.get('excerpt_parts') is not None else '').strip()
        if raw == '':
            new['excerpt_parts'] = None   # Blank: every later part.
        else:
            try:
                parts = int(raw)
            except ValueError:
                raise ValueError('The number of later parts must be a whole number, or blank for all of them.')
            if not 1 <= parts <= 999:
                raise ValueError('The number of later parts must be from 1 to 999, or blank for all of them.')
            new['excerpt_parts'] = parts
    if 'excerpt_words' in form:
        try:
            words = int(str(form.get('excerpt_words')).replace(',', '').strip())
        except (TypeError, ValueError):
            raise ValueError('The word limit must be a whole number.')
        low, high = EXCERPT_WORDS_RANGE
        if not low <= words <= high:
            raise ValueError(f'The word limit must be from {low:,} to {high:,} words.')
        new['excerpt_words'] = words
    if form.get('story_text') in STORY_TEXT:
        new['story_text'] = form['story_text']
    if form.get('lettering') in LETTERING:
        new['lettering'] = form['lettering']
    for flag in ('auto', 'retry_unlettered', 'retry_softer'):
        if flag in form:
            new[flag] = bool(form.get(flag))
    if 'auto' in form:
        new['auto_chosen'] = True
    config = AppConfig.query.filter_by(key=KEY).first()
    if not config:
        config = AppConfig(key=KEY, value_type='json', description='OpenRouter key and models for AI covers')
        db.session.add(config)
    config.set_value(new)
    db.session.commit()
    return new


# --- Files ----------------------------------------------------------------------------------

def _base(story) -> str:
    return f'{story.id}_{story.filename_base}'


def cover_path(story) -> str:
    from app.utils import get_cover_directory
    return os.path.join(get_cover_directory(), f'{_base(story)}.jpg')


def art_paths(story) -> tuple[str, str]:
    from app.utils import get_cover_directory
    from .cover_generator import ai_art_paths
    return ai_art_paths(get_cover_directory(), _base(story))


def _read_notes(story) -> dict:
    try:
        with open(art_paths(story)[1], encoding='utf-8') as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_notes(story, data: dict) -> None:
    notes = art_paths(story)[1]
    os.makedirs(os.path.dirname(notes), exist_ok=True)
    temporary = notes + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=1, ensure_ascii=False)
    os.replace(temporary, notes)


def info(story) -> dict:
    """What the story window shows about the story's AI cover."""
    art, _ = art_paths(story)
    notes = _read_notes(story)
    with _lock:
        running = story.id in _active
        progress = _active[story.id].get('progress', '') if running else ''
        queued = story.id in _queue
    return {
        'has_art': os.path.exists(art),
        'running': running, 'queued': queued, 'progress': progress,
        'configured': bool(settings()['api_key']),
        'paused': _paused() if queued else '',
        'error': notes.get('error', ''),
        'cost': notes.get('cost'),
        'made': notes.get('made', ''),
        'text_model': notes.get('text_model', ''),
        'image_model': notes.get('image_model', ''),
        'lettered': bool(notes.get('lettered')),
        'softened': bool(notes.get('softened')),
    }


def _paused() -> str:
    from . import ai_queues
    return ai_queues.paused()


def apply_cover(story) -> None:
    """Redraw the story's cover (over its AI artwork if it has some), put it into the EPUB, and drop the
    cached e-reader cover so it is rebuilt from the new one."""
    from app.models import db
    from .cover_generator import draw_story_cover
    from .epub_service import EpubService
    path = cover_path(story)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    author = story.author.name if story.author else 'Unknown Author'
    category = story.category.name if story.category else None
    draw_story_cover(story.title, author, path, category=category)
    epub = next((f for f in story.formats if f.format_type == 'epub'), None)
    if epub and epub.file_path and os.path.exists(epub.file_path) and os.path.exists(path):
        if not EpubService.update_epub_cover(epub.file_path, path):
            log_error(f'Could not put the new cover into the EPUB for story {story.id}')
    koreader = os.path.join(os.path.dirname(path), f'{_base(story)}_koreader.jpg')
    if os.path.exists(koreader):
        os.remove(koreader)
    story.cover_filename = os.path.basename(path)
    db.session.commit()


def delete_art(story) -> None:
    for path in art_paths(story):
        if os.path.exists(path):
            os.remove(path)


def use_plain_cover(story) -> None:
    """Delete the AI artwork and go back to the generated cover."""
    delete_art(story)
    apply_cover(story)
    log_action(f'AI cover removed for story {story.id}; using the generated cover')


# --- OpenRouter -----------------------------------------------------------------------------

class StopError(ValueError):
    """A failure another model or prompt cannot fix (key refused, no credit, OpenRouter unreachable).
    It pauses every AI queue (ai_queues.pause); `wait` is how many seconds until they try again by themselves
    (None: only when someone starts AI work again or saves the AI settings)."""

    def __init__(self, message: str, wait: Optional[int] = None):
        super().__init__(message)
        self.wait = wait


UNREACHABLE_WAIT = 300   # seconds before paused AI work tries OpenRouter again after it could not be reached
RETRY_STATUS = {408, 429, 500, 502, 503, 504, 520, 522, 524, 529}
ATTEMPTS = 3


REFUSAL = re.compile(r'moderat|content (?:policy|filter)|safety|blocked|flagged|sensitive|nsfw|inappropriate|'
                     r'not allowed|violat|declin|refus|no picture', re.I)


def refused(error: Exception) -> bool:
    """The image service turned the request down because of what it shows (or returned nothing)."""
    return bool(REFUSAL.search(str(error)))


def _headers(api_key: str = '') -> dict:
    headers = {'Content-Type': 'application/json', 'X-Title': 'LitKeeper',
               'HTTP-Referer': 'https://github.com/redwoodstory/LitKeeper'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    return headers


def _endpoint(url: str) -> str:
    return url.split(API, 1)[-1].lstrip('/') if url.startswith(API) else url


def _log_call(method: str, url: str, status: object, started: float, number: int | None = None) -> None:
    """Record an OpenRouter call's result in api.log: the endpoint, status and time, never the key."""
    from .logger import log_api_call
    log_api_call('OpenRouter', method, _endpoint(url), status, (time.perf_counter() - started) * 1000, number)


def _record_call(url: str, payload: dict, ok: bool, status, result: Optional[dict] = None,
                 kind: Optional[str] = None) -> None:
    """Settings → AI Statistics: one finished OpenRouter call (what for, the model, its cost, whether it worked).
    Never raises: a statistic that could not be saved must not fail the call it describes."""
    try:
        from . import ai_stats
        kind = kind or ('cover_image' if url == IMAGES else 'cover_text')
        cost = float(((result or {}).get('usage') or {}).get('cost') or 0)
        ai_stats.record(kind, (result or {}).get('model') or payload.get('model') or '', cost, ok, str(status))
    except Exception as error:
        log_error(f'Could not record an OpenRouter call in the AI statistics: {error}')


def _retry_delay(attempt: int, error=None) -> float:
    """Seconds to wait before the next try: OpenRouter's Retry-After when it sends one (at most a minute),
    otherwise 10 s, then 20 s."""
    try:
        wanted = float(error.headers.get('Retry-After')) if error is not None and error.headers else 0
    except (TypeError, ValueError, AttributeError):
        wanted = 0
    return min(60.0, wanted) if wanted > 0 else 10.0 * (attempt + 1)


def _answer_error(result: dict) -> Optional[dict]:
    """The error in a reply that came back with status 200: OpenRouter puts provider failures in the body
    (`error`), or in the first choice. Returns {'code', 'message'} or None."""
    error = result.get('error')
    if not error:
        choice = (result.get('choices') or [{}])[0] if isinstance(result.get('choices'), list) else {}
        error = choice.get('error') if isinstance(choice, dict) else None
        if not error and isinstance(choice, dict) and choice.get('finish_reason') == 'error':
            error = {'message': 'the provider stopped with an error'}
    if not error:
        return None
    if not isinstance(error, dict):
        return {'code': None, 'message': str(error)}
    code = error.get('code')
    try:
        code = int(code)
    except (TypeError, ValueError):
        pass
    message = str(error.get('message') or 'request failed')
    raw = (error.get('metadata') or {}).get('raw') if isinstance(error.get('metadata'), dict) else None
    if raw and isinstance(raw, str) and raw not in message:
        message += f' ({raw[:200]})'
    return {'code': code, 'message': message}


def _failure(code, detail: str, model: str) -> ValueError:
    """The error to raise for a request OpenRouter turned down, with what to do about it."""
    if code == 401 or (code == 403 and not REFUSAL.search(detail)):
        return StopError('OpenRouter refused the API key. Check it in Settings → AI Covers '
                         '(the key is shared by every AI feature).')
    if code == 402:
        return StopError('OpenRouter reports insufficient credits. Top up at openrouter.ai/settings/credits, '
                         'then press Resume on the Queue page.')
    if re.search(r'not a valid model|model.{0,40}(?:not found|does not exist)|unknown model', detail, re.I):
        return ValueError(f'OpenRouter has no model called "{model}". Check the name in Settings.')
    if re.search(r'no endpoints found', detail, re.I):
        return ValueError(f'No provider on OpenRouter can run "{model}" for this request ({detail}). '
                          'Choose another model in Settings.')
    if code in (408, 504, 524) or re.search(r'timed? ?out', detail, re.I):
        return ValueError(f'The model ({model}) took too long to answer. Try again, or choose a faster model.')
    if code in (429,):
        return ValueError(f'OpenRouter is rate limiting {model} right now. Try again in a few minutes.')
    if isinstance(code, int) and code >= 500:
        return ValueError(f'OpenRouter or its provider for {model} had a problem (error {code}'
                          f'{": " + detail if detail else ""}). Try again later.')
    label = f'error {code}' if code else 'error'
    return ValueError(f'OpenRouter {label}{": " + detail if detail else ""}')


def _post(url: str, payload: dict, api_key: str, timeout: int = 300, kind: Optional[str] = None,
          label: str = '') -> dict:
    """One OpenRouter request, with bounded retries for rate limits, server errors, dropped connections and
    replies that could not be read. `kind` is what the call counts as in Settings → AI Statistics (default: a
    cover's description or picture); `label` says in the log what it is for. The API key travels in a header
    and is never logged.

    Raises StopError when no request could succeed (key refused, no credit, OpenRouter unreachable), and
    ValueError, with a message for the story window, for any other failure."""
    from .logger import log_api_call_started
    body = json.dumps(payload).encode()
    model = payload.get('model') or ''
    for attempt in range(ATTEMPTS):
        last = attempt == ATTEMPTS - 1
        request = urllib.request.Request(url, data=body, method='POST', headers=_headers(api_key))
        # The JSON sent goes on an "API data" line (Settings → Logs can hide it); the key is in a header, never logged.
        number = log_api_call_started('OpenRouter', 'POST', _endpoint(url) + (f' ({label})' if label else ''), payload)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            try:
                result = json.loads(raw.decode('utf-8', errors='replace'))
                if not isinstance(result, dict):
                    raise ValueError('not an object')
            except ValueError:
                _log_call('POST', url, 'unreadable reply', started, number)
                _record_call(url, payload, False, 'unreadable reply', kind=kind)
                log_error(f'OpenRouter sent a reply that is not JSON ({label or _endpoint(url)}): {raw[:200]!r}')
                if not last:
                    time.sleep(_retry_delay(attempt))
                    continue
                raise ValueError(f'OpenRouter sent a reply LitKeeper could not read (model {model}). Try again.')
            _log_call('POST', url, 200, started, number)
            problem = _answer_error(result)
            # A picture call that returns no picture is a failure too (usually a quiet refusal).
            usable = not problem and (url != IMAGES or bool((result.get('data') or [{}])[0].get('b64_json')))
            _record_call(url, payload, usable, 200 if usable else 'no usable answer', result, kind)
            if problem:
                if problem['code'] in RETRY_STATUS and not last:
                    log_action(f'OpenRouter {label or _endpoint(url)}: {problem["message"]}; trying again')
                    time.sleep(_retry_delay(attempt))
                    continue
                raise _failure(problem['code'], problem['message'], model)
            return result
        except urllib.error.HTTPError as error:
            _log_call('POST', url, error.code, started, number)
            _record_call(url, payload, False, error.code, kind=kind)
            detail = ''
            try:
                detail = _answer_error(json.loads(error.read().decode()))['message']
            except Exception:
                pass
            if error.code in RETRY_STATUS and not last:
                time.sleep(_retry_delay(attempt, error))
                continue
            raise _failure(error.code, detail, model)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            # Includes timeouts, refused or dropped connections and replies cut off part way.
            reason = getattr(error, 'reason', None) or error.__class__.__name__
            if isinstance(error, TimeoutError) or 'timed out' in str(reason):
                reason = f'no answer within {timeout} seconds'
            _log_call('POST', url, 'unreachable', started, number)
            _record_call(url, payload, False, 'unreachable', kind=kind)
            if not last:
                time.sleep(_retry_delay(attempt))
                continue
            if 'no answer within' in str(reason):
                raise ValueError(f'The model ({model}) gave no answer within {timeout} seconds, {ATTEMPTS} times. '
                                 'Try again, or choose a faster model.')
            raise StopError(f'Could not reach OpenRouter ({reason}). AI work will try again in '
                            f'{UNREACHABLE_WAIT // 60} minutes.', wait=UNREACHABLE_WAIT)
    raise StopError('OpenRouter did not answer.', wait=UNREACHABLE_WAIT)


def _get(path: str, api_key: str = '') -> tuple[int, dict]:
    """GET from OpenRouter; returns (status, JSON body). Does not raise for HTTP errors."""
    request = urllib.request.Request(f'{API}/{path}', headers=_headers(api_key))
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            _log_call('GET', path, response.status, started)
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        _log_call('GET', path, error.code, started)
        try:
            return error.code, json.loads(error.read().decode())
        except Exception:
            return error.code, {}
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise ValueError(f'Could not reach OpenRouter: {getattr(error, "reason", error)}')


def check_models() -> list[tuple[bool, str]]:
    """Ask OpenRouter whether the saved key works and the chosen models exist and can paint 3:4 pictures.
    Costs nothing: it only reads catalogue and account information."""
    config = settings()
    key = config['api_key']
    if not key:
        return [(False, 'No OpenRouter API key is saved yet.')]
    results = []
    status, body = _get('key', key)
    if status in (401, 403):
        return [(False, 'OpenRouter refused the API key.')]
    if status == 200:
        remaining = (body.get('data') or {}).get('limit_remaining')
        results.append((remaining is None or remaining > 0.05,
                        'API key accepted' + ('' if remaining is None else f'; ${remaining:.2f} of its spending limit remains') + '.'))

    text = config['text_model']
    status, body = _get(f'models/{text}/endpoints')
    found = body.get('data') or {}
    if status != 200:
        results.append((False, f'Text model {text}: OpenRouter does not list it. Check the spelling.'))
    elif not any(e.get('status', 0) == 0 for e in found.get('endpoints') or []):
        results.append((False, f'Text model {text}: no provider is answering right now. Try again later or choose another.'))
    else:
        results.append((True, f'Text model {text}: available.'))

    for index, image in enumerate([config['image_model']] + list(config.get('fallback_models') or [])):
        role = 'Image model' if index == 0 else f'Fallback image model {index}'
        status, body = _get(f'images/models/{image}/endpoints')
        endpoints = body.get('endpoints') or []
        if status != 200 or not endpoints:
            results.append((False, f'{role} {image}: OpenRouter does not list it as an image model. Check the spelling.'))
        elif not any('3:4' in ((e.get('supported_parameters') or {}).get('aspect_ratio') or {}).get('values', []) for e in endpoints):
            results.append((False, f'{role} {image} cannot paint a 3:4 (portrait) picture. Choose another image model.'))
        else:
            price = next((p.get('cost_usd') for e in endpoints for p in e.get('pricing') or [] if p.get('billable') == 'output_image'), None)
            results.append((True, f'{role} {image}: available' + (f', about ${price:.3f} per picture.' if price else '.')))
    return results


# --- What the text model reads ---------------------------------------------------------------

def _chapters(story) -> list[dict]:
    """The story's parts from its reader (JSON) file: [{'number', 'title', 'paragraphs'}]."""
    fmt = next((f for f in story.formats if f.format_type in ('json', 'html')), None)
    if not fmt:
        return []
    data = None
    try:
        if fmt.json_data:
            data = json.loads(fmt.json_data)
        elif fmt.file_path and fmt.file_path.endswith('.json') and os.path.exists(fmt.file_path):
            with open(fmt.file_path, encoding='utf-8') as handle:
                data = json.load(handle)
    except (OSError, ValueError):
        data = None
    chapters = (data or {}).get('chapters') or []
    return [c for c in chapters if isinstance(c, dict)]


def _first_words(paragraphs, limit: int) -> str:
    words: list[str] = []
    for paragraph in paragraphs:
        words += str(paragraph).split()
        if len(words) >= limit:
            break
    return ' '.join(words[:max(0, limit)])


def story_details(story, amount: Optional[str] = None) -> str:
    """Title, category, tags, description and story text for the text model."""
    config = settings()
    amount = amount if amount in STORY_TEXT else config.get('story_text', 'excerpts')
    budget = story_text_options(config).get(amount, STORY_TEXT['excerpts'])[1]
    share = (config.get('excerpt_percent') or EXCERPT_DEFAULTS['excerpt_percent']) / 100
    later_parts = config.get('excerpt_parts')  # None: every later part.
    chapters = _chapters(story)
    tags = ', '.join(tag.name for tag in story.tags[:15])
    head = f'Title: {story.title}\n'
    if story.category:
        head += f'Category: {story.category.name}\n'
    if tags:
        head += f'Tags: {tags}\n'
    if story.description:
        head += f'Description: {story.description}\n'
    head += f'Parts: {len(chapters) or 1}\n'
    if not chapters:
        return head + '(The story text is not available; use the details above.)'
    if amount == 'opening' or len(chapters) < 2:
        return head + f'Opening of the story:\n{_first_words([p for c in chapters for p in c.get("paragraphs", [])], budget)}'
    text, used = [], 0
    for index, chapter in enumerate(chapters):
        if amount == 'excerpts' and later_parts and index > later_parts:
            break
        paragraphs = chapter.get('paragraphs', [])
        words = sum(len(str(p).split()) for p in paragraphs)
        wanted = words if amount == 'whole' or index == 0 else max(1, round(words * share))
        part = _first_words(paragraphs, min(wanted, budget - used))
        if not part:
            break
        label = '' if amount == 'whole' else (' (start)' if index == 0 and len(part.split()) < words
                                              else ' (complete)' if index == 0 else f' (first {round(share * 100)}%)')
        text.append(f'Part {chapter.get("number") or index + 1}{label}:\n{part}')
        used += len(part.split())
    return head + 'Passages from the story:\n' + '\n\n'.join(text)


# --- Making a cover -------------------------------------------------------------------------

def _set_progress(text: str) -> None:
    story_id = getattr(_current, 'story_id', None)
    with _lock:
        if story_id in _active:
            _active[story_id]['progress'] = text


def write_brief(story, api_key: str, model: str) -> tuple[str, float]:
    """The scene description for the image model, and what it cost."""
    payload = {'model': model, 'usage': {'include': True}, 'max_tokens': 4000,
               'reasoning': {'effort': 'low', 'exclude': True},
               'messages': [{'role': 'system', 'content': ai_prompts.get('cover_brief')},
                            {'role': 'user', 'content': story_details(story)}]}
    result = _post(CHAT, payload, api_key, timeout=180)
    choice = (result.get('choices') or [{}])[0]
    brief = ai_prompts.tidy_reply((choice.get('message') or {}).get('content') or '')
    if len(brief.split()) < 15:
        finish = choice.get('finish_reason') or choice.get('native_finish_reason') or ''
        if finish == 'length':
            raise ValueError(f'The text model ({model}) used its whole allowance thinking and wrote no description. Try again, or choose a non-reasoning text model.')
        if finish == 'content_filter':
            raise ValueError(f"The text model ({model}) was blocked by its provider's content filter. Choose another text model.")
        if brief:
            raise ValueError(f'The text model ({model}) declined or answered too briefly: "{brief[:160]}". Try again, or choose another text model.')
        raise ValueError(f'The text model ({model}) returned an empty reply. Try again, or choose another text model.')
    return brief, float((result.get('usage') or {}).get('cost') or 0)


_parameters: dict[str, tuple[float, dict]] = {}


def _image_parameters(model: str) -> dict:
    """The parameters an image model accepts (OpenRouter catalogue, remembered for an hour); {} if unknown."""
    cached = _parameters.get(model)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    try:
        status, body = _get(f'images/models/{model}/endpoints')
    except ValueError:
        return {}
    merged: dict[str, set] = {}
    for endpoint in (body.get('endpoints') or []) if status == 200 else []:
        for name, spec in (endpoint.get('supported_parameters') or {}).items():
            merged.setdefault(name, set()).update((spec or {}).get('values') or [])
    _parameters[model] = (time.time(), merged)
    return merged


def _as_jpeg(picture: bytes) -> bytes:
    """Artwork is kept as JPEG; models that answer in PNG or WebP are converted."""
    from PIL import Image
    if picture[:3] == b'\xff\xd8\xff':
        return picture
    with Image.open(io.BytesIO(picture)) as image:
        out = io.BytesIO()
        image.convert('RGB').save(out, 'JPEG', quality=93)
        return out.getvalue()


def paint(brief: str, api_key: str, model: str, title: str, author: str, lettered: bool) -> tuple[bytes, float]:
    from .cover_generator import title_space
    template = ai_prompts.get('cover_image_lettered' if lettered else 'cover_image')
    prompt = (template.replace('{description}', brief).replace('{title_space}', f'{title_space(title):.0%}')
              .replace('{title}', title).replace('{author}', author))
    payload = {'model': model, 'prompt': prompt, 'n': 1, 'aspect_ratio': '3:4'}
    supported = _image_parameters(model)
    for name, wanted in (('resolution', '1K'), ('output_format', 'jpeg')):
        if wanted in supported.get(name, ()):
            payload[name] = wanted
    if supported and 'n' not in supported:
        payload.pop('n')
    result = _post(IMAGES, payload, api_key)
    images = result.get('data') or []
    if not images or not images[0].get('b64_json'):
        raise ValueError('The image model returned no picture. It may have declined the description; try again.')
    return _as_jpeg(base64.b64decode(images[0]['b64_json'])), float((result.get('usage') or {}).get('cost') or 0)


def soften(brief: str, api_key: str, model: str) -> tuple[str, float]:
    """Ask the text model to tone a refused description down."""
    payload = {'model': model, 'usage': {'include': True}, 'max_tokens': 3000, 'reasoning': {'effort': 'low', 'exclude': True},
               'messages': [{'role': 'system', 'content': ai_prompts.get('cover_safer')}, {'role': 'user', 'content': brief}]}
    result = _post(CHAT, payload, api_key, timeout=180)
    text = ai_prompts.tidy_reply(((result.get('choices') or [{}])[0].get('message') or {}).get('content') or '')
    if len(text.split()) < 15:
        raise ValueError('The text model did not write a toned-down description.')
    return text, float((result.get('usage') or {}).get('cost') or 0)


def paint_with_fallbacks(story, brief: str, api_key: str, config: dict) -> tuple[bytes, float, dict]:
    """Paint the cover, working through fallbacks until one succeeds:
    1. each image model (the chosen one, then the fallbacks), with the lettering as set;
    2. if refused and the image model was to letter the cover: each model again without lettering
       (LitKeeper letters it), since title words often trip safety filters;
    3. if everything was refused: a toned-down description from the text model, then each model again.
    A failure no retry can fix (key, credit, connection) stops at once."""
    title, author = story.title, story.author.name if story.author else ''
    chosen = [config['image_model']] + [m for m in config.get('fallback_models') or [] if m != config['image_model']]
    wants_lettering = config.get('lettering', 'image') == 'image'
    attempts: list[str] = []
    broken: set[str] = set()
    cost = 0.0

    def run(plan, description, softened):
        for model, lettered in plan:
            if model in broken:
                continue
            _set_progress(f'Painting with {model}' + (' (toned down)' if softened else ''))
            try:
                picture, spent = paint(description, api_key, model, title, author, lettered)
                return picture, spent, {'model': model, 'lettered': lettered, 'softened': softened, 'brief': description}
            except StopError:
                raise
            except ValueError as error:
                attempts.append(f'{model}{" with lettering" if lettered else ""}{" (toned down)" if softened else ""}: {error}')
                log_action(f'AI cover for story {story.id}: {attempts[-1]}')
                if not refused(error):
                    broken.add(model)
        return None

    plan = [(model, wants_lettering) for model in chosen]
    if wants_lettering and config.get('retry_unlettered', True):
        plan += [(model, False) for model in chosen]
    done = run(plan, brief, False)
    if not done and config.get('retry_softer', True) and any(refused(Exception(a)) for a in attempts) and len(broken) < len(chosen):
        _set_progress('Toning down the scene')
        softer, spent = soften(brief, api_key, config['text_model'])
        cost += spent
        lettering = wants_lettering and not config.get('retry_unlettered', True)
        done = run([(model, lettering) for model in chosen], softer, True)
    if not done:
        raise ValueError('No image model would paint this cover. Tried: ' + '; '.join(attempts) +
                         '. Add fallback image models in Settings → AI Covers, or edit the prompts.')
    picture, spent, used = done
    return picture, cost + spent, {**used, 'attempts': attempts}


def make_cover(story_id: int) -> None:
    """Write the scene, paint the artwork, and draw the story's cover from it."""
    from app.models import Story, db
    story = db.session.get(Story, story_id)
    if not story:
        return
    config = settings()
    if not config['api_key']:
        raise ValueError('Add an OpenRouter API key in Settings → AI Covers first.')
    _set_progress('Describing the cover')
    brief, brief_cost = write_brief(story, config['api_key'], config['text_model'])
    _set_progress('Painting the artwork')
    picture, image_cost, used = paint_with_fallbacks(story, brief, config['api_key'], config)
    art, _ = art_paths(story)
    os.makedirs(os.path.dirname(art), exist_ok=True)
    temporary = art + '.tmp'
    with open(temporary, 'wb') as handle:
        handle.write(picture)
    os.replace(temporary, art)
    total = round(brief_cost + image_cost, 4)
    _write_notes(story, {'brief': used['brief'], 'text_model': config['text_model'], 'image_model': used['model'],
                         'lettered': used['lettered'], 'softened': used['softened'], 'attempts': used['attempts'],
                         'cost': total, 'made': datetime.now(timezone.utc).isoformat()})
    apply_cover(story)
    log_action(f'AI cover made for story {story_id} ({config["text_model"]}, {used["model"]}), cost ${total:.4f}')


# --- Background worker ----------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cancel(story_id: int) -> bool:
    """Take a waiting cover out of the queue. A cover already being made can't be stopped."""
    with _lock:
        if story_id not in _queue:
            return False
        _queue.remove(story_id)
        _queued_at.pop(story_id, None)
        _persist()
    log_action(f'AI cover for story {story_id} cancelled before it started')
    return True


def recent_results(limit: int = 15) -> list[dict]:
    """The latest finished and failed covers, newest first, read from each story's notes file
    (so the list survives restarts). Each: story_id, ok, when, cost, error, image_model."""
    import glob
    from app.utils import get_cover_directory
    from .cover_generator import ai_art_paths
    folder = os.path.dirname(ai_art_paths(get_cover_directory(), 'x')[1])
    results = []
    for path in glob.glob(os.path.join(glob.escape(folder), '*.art.json')):
        try:
            story_id = int(os.path.basename(path).split('_', 1)[0])
            with open(path, encoding='utf-8') as handle:
                notes = json.load(handle)
        except (ValueError, OSError):
            continue
        if not isinstance(notes, dict):
            continue
        failed = bool(notes.get('error'))
        when = (notes.get('failed') if failed else notes.get('made')) or \
            datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat()
        results.append({'story_id': story_id, 'ok': not failed, 'when': when, 'cost': notes.get('cost'),
                        'error': notes.get('error', ''), 'image_model': notes.get('image_model', '')})
    results.sort(key=lambda r: r['when'], reverse=True)
    return results[:limit]


def _persist() -> None:
    """Save the covers being made and the waiting ones, in order (call with _lock held), so a restart of the
    server (or of its worker process, which happens after many requests) does not lose them."""
    from . import ai_queues
    ai_queues.save('cover', [[i, 'cover'] for i in list(_active) + list(_queue)])


def restore(app) -> None:
    """Queue the covers saved before the last restart again: the ones being made first, then the waiting ones."""
    from . import ai_queues
    from app.models import Story, db
    with app.app_context():
        items = [i for i in ai_queues.load('cover') if db.session.get(Story, i[0])]
        limit = parallel_limit()
    for story_id, _ in items:
        try:
            start(app, story_id, limit, resume=False)
        except ValueError:
            pass
    if items:
        log_action(f'Restored {len(items)} AI {"cover" if len(items) == 1 else "covers"} after a restart')


def reorder(story_ids: list) -> None:
    """The Queue page's drag and drop: put the waiting covers in this order (ones not named keep their place
    after them)."""
    with _lock:
        wanted = [i for i in story_ids if i in _queue]
        rest = [i for i in _queue if i not in wanted]
        _queue.clear()
        _queue.extend(wanted + rest)
        _persist()


def queue_overview() -> dict:
    """What the Queue page shows: the covers being made now, the ones waiting in order, and recent results."""
    with _lock:
        running = sorted(({'story_id': i, 'progress': a.get('progress', ''), 'started': a.get('started', '')}
                          for i, a in _active.items()), key=lambda r: r['started'])
        waiting = [{'story_id': i, 'queued_at': _queued_at.get(i, '')} for i in _queue]
    return {'running': running, 'waiting': waiting, 'recent': recent_results()}


def stories_without_cover() -> list[int]:
    """Ids of stories with no AI cover yet that aren't being made or waiting. Needs an app context."""
    from app.models import Story
    with _lock:
        busy = set(_active) | set(_queue)
    return [story.id for story in Story.query.order_by(Story.created_at.desc()).all()
            if story.id not in busy and not os.path.exists(art_paths(story)[0])]


def make_missing(app) -> int:
    """Queue an AI cover for every story without one. Returns how many were queued."""
    if not settings()['api_key']:
        raise ValueError('Add an OpenRouter API key in Settings → AI Covers first.')
    queued = 0
    for story_id in stories_without_cover():
        try:
            start(app, story_id)
            queued += 1
        except ValueError:
            pass  # Started meanwhile.
    log_action(f'AI covers queued for {queued} stories without one')
    return queued


def parallel_limit() -> int:
    """How many covers to make at the same time (Settings → AI Covers). Needs an app context."""
    try:
        value = int(settings().get('parallel', DEFAULTS['parallel']))
    except Exception:
        value = DEFAULTS['parallel']
    return max(1, min(PARALLEL_MAX, value))


def start(app, story_id: int, limit: Optional[int] = None, resume: bool = True) -> str:
    """Queue a cover for the story; returns 'started' or 'queued'. Up to `limit` covers
    (default: the Settings value) are made at the same time, each in its own worker thread.
    While AI work is paused, `resume` (someone asked for this cover) starts every AI queue again;
    without it (a cover made after a download) the story only waits in the queue."""
    from . import ai_queues
    global _workers
    if limit is None:
        limit = parallel_limit()
    with _lock:
        if story_id in _active:
            raise ValueError('A cover for this story is already being made.')
        if story_id in _queue:
            queued = True
        else:
            queued = False
            _queue.append(story_id)
            _queued_at[story_id] = _now()
            _persist()
    if ai_queues.paused():
        if resume:
            ai_queues.resume(app, 'AI work was asked for')
        return 'queued'
    if queued:
        return 'queued'
    with _lock:
        if _workers >= limit:
            return 'queued'
        _workers += 1
    threading.Thread(target=_work, args=(app,), daemon=True, name='AICovers').start()
    return 'started'


def kick(app) -> None:
    """Start workers for covers waiting in the queue (after a pause, or a restart)."""
    from . import ai_queues
    global _workers
    if ai_queues.paused():
        return
    limit = parallel_limit()
    with _lock:
        wanted = max(0, min(limit, len(_queue)) - _workers)
        _workers += wanted
    for _ in range(wanted):
        threading.Thread(target=_work, args=(app,), daemon=True, name='AICovers').start()


def make_after_download(app, story_id: Optional[int]) -> None:
    """Settings → AI Covers: queue a cover for a newly downloaded story, when that is turned on."""
    if not story_id:
        return
    try:
        config = settings()
        if not config.get('auto') or not config['api_key']:
            return
        from app.models import Story, db
        story = db.session.get(Story, story_id)
        if not story or os.path.exists(art_paths(story)[0]):
            return
        start(app, story_id, resume=False)
        log_action(f'AI cover queued for new story {story_id}')
    except ValueError:
        pass  # Already being made.
    except Exception as e:
        log_error(f'Could not queue an AI cover for story {story_id}: {e}')


def _work(app) -> None:
    """One worker: makes covers from the queue one after another until it's empty.
    Several of these run side by side (see start)."""
    from . import ai_queues
    global _workers
    while True:
        try:
            with app.app_context():
                limit = parallel_limit()
        except Exception:
            limit = DEFAULTS['parallel']
        with _lock:
            # Stop when the queue is empty, when AI work is paused, or when there are more workers than
            # the Settings value now allows (it was lowered while covers were being made).
            if not _queue or _workers > limit or ai_queues.paused():
                _workers -= 1
                return
            story_id = _queue.popleft()
            _queued_at.pop(story_id, None)
            _active[story_id] = {'progress': 'Starting', 'started': _now()}
            _persist()
        _current.story_id = story_id
        try:
            with app.app_context():
                make_cover(story_id)
        except StopError as error:
            # Nothing else could get through either: this story goes back to the front, and AI work pauses.
            log_error(f'AI cover for story {story_id} paused: {error}')
            with _lock:
                if story_id not in _queue:
                    _queue.appendleft(story_id)
                    _queued_at[story_id] = _now()
            ai_queues.pause(app, error)
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else 'Making the cover stopped unexpectedly.'
            log_error(f'AI cover for story {story_id} failed: {message}'
                      + ('' if isinstance(error, ValueError) else f'\n{traceback.format_exc()}'))
            try:
                with app.app_context():
                    from app.models import Story, db
                    from .notifier import send_notification
                    story = db.session.get(Story, story_id)
                    if story:
                        _write_notes(story, {**_read_notes(story), 'error': message, 'failed': _now()})
                        send_notification(f'AI cover failed for "{story.title}": {message}', is_error=True)
            except Exception as e:
                log_error(f'Could not record the AI cover failure for story {story_id}: {e}')
        finally:
            _current.story_id = None
            with _lock:
                _active.pop(story_id, None)
                _persist()
