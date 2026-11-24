"""AI Rewrite: proofreading and copy-editing of saved stories through OpenRouter, one part at a time (from RedKeeper).

The original text is never changed. Rewritten parts are saved next to the reader file as
`stories/html/<id>_<name>.edited.json`, keyed by part number together with a fingerprint of the original
part, so a part whose original changes (or a new part, after an update) counts as not yet rewritten.
The reader and the HTML download show the rewrite where there is one, with a link back to the original;
the EPUB keeps the original text.

The model is an OpenRouter preset (its own prompt and model, the default) or a model name, which is sent
the AI Rewrite prompt from Settings → AI Covers → Prompts. The OpenRouter key is the one in Settings → AI Covers.

Character names stay consistent across parts: after each part, initials in the original (such as "B") are
matched to the names the model used for them (such as "Brian"), and later parts are sent with
"Character names already used…" ahead of their text.
"""
from __future__ import annotations
import hashlib
import html
import json
import os
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from .logger import log_action, log_error

KEY = 'ai_rewrite'
DEFAULT_MODEL = '@preset/redkeeper'
MODEL_NAME = re.compile(r'[@\w./:-]+')
# A capital letter standing alone as a word, possibly possessive (B, B's, B’s, Bs); not B-cup, 33M, (F) or [M].
INITIAL = re.compile(r"(?<![\w'’(\[/-])([B-HJ-Z])(?!\s+cups?\b)(?=(?:['’]?s)?(?![\w)\]/-]))")
_TAG = re.compile(r'<[^>]+>')

_queue: deque = deque()
_running: dict = {}
_redo: dict = {}
_lock = threading.Lock()


# --- Settings -------------------------------------------------------------------------------

def settings() -> dict:
    from app.models import AppConfig
    config = AppConfig.query.filter_by(key=KEY).first()
    saved = config.get_value() if config else None
    return {'model': DEFAULT_MODEL, **(saved if isinstance(saved, dict) else {})}


def save_settings(model: str) -> dict:
    from app.models import AppConfig, db
    model = (model or '').strip() or DEFAULT_MODEL
    if len(model) > 120 or not MODEL_NAME.fullmatch(model):
        raise ValueError('Enter an OpenRouter preset or model, for example @preset/redkeeper.')
    config = AppConfig.query.filter_by(key=KEY).first()
    if not config:
        config = AppConfig(key=KEY, value_type='json', description='OpenRouter model for AI Rewrite')
        db.session.add(config)
    config.value_type = 'json'
    config.set_value({'model': model})
    db.session.commit()
    return settings()


def api_key() -> str:
    from .ai_cover import settings as cover_settings
    return cover_settings()['api_key']


# --- Rewritten versions ---------------------------------------------------------------------

def _path(story) -> str:
    from app.utils import paths
    return os.path.join(paths.get_html_directory(), f'{story.id}_{story.filename_base}.edited.json')


def _fingerprint(paragraphs: list) -> str:
    return hashlib.sha256('\n\n'.join(str(p) for p in paragraphs).encode()).hexdigest()[:16]


def load_edits(story) -> dict:
    try:
        with open(_path(story), encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    return {'parts': {}, 'names': {}, 'model': '', 'tokens': 0, 'cost': 0.0, **(data if isinstance(data, dict) else {})}


def _save_edits(story, data: dict) -> None:
    path = _path(story)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.tmp', 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(path + '.tmp', path)


def discard(story) -> None:
    try:
        os.remove(_path(story))
    except FileNotFoundError:
        pass
    log_action(f'AI Rewrite discarded for story {story.id}')


def _chapters(story) -> list[dict]:
    from .ai_cover import _chapters as chapters
    return chapters(story)


def _number(chapter: dict, index: int) -> str:
    return str(chapter.get('number') or index + 1)


def plain(paragraph) -> str:
    """A reader paragraph (which may hold <em>, <br> and entities) as plain text for the model."""
    text = re.sub(r'<br\s*/?>', '\n', str(paragraph), flags=re.I)
    return html.unescape(_TAG.sub('', text)).strip()


def edited_parts(story, chapters: list | None = None) -> dict:
    """Rewritten paragraphs by part number, only where the rewrite matches the current original part."""
    if not os.path.exists(_path(story)):
        return {}
    edits = load_edits(story)['parts']
    result = {}
    for index, chapter in enumerate(chapters if chapters is not None else _chapters(story)):
        number = _number(chapter, index)
        edit = edits.get(number)
        if edit and edit.get('paragraphs') and edit.get('source') == _fingerprint(chapter.get('paragraphs', [])):
            result[number] = edit
    return result


def apply_edits(data: dict, story) -> dict:
    """The reader/export data with rewritten paragraphs (as safe HTML) in place of the originals where they exist."""
    edits = edited_parts(story)
    chapters = []
    for index, chapter in enumerate(data.get('chapters') or []):
        edit = edits.get(_number(chapter, index)) if isinstance(chapter, dict) else None
        if edit:
            chapter = {**chapter, 'paragraphs': [html.escape(p).replace('\n', '<br>') for p in edit['paragraphs']]}
        chapters.append(chapter)
    return {**data, 'chapters': chapters}


def words_to_rewrite(story) -> int:
    """Words in the parts AI Rewrite would send now (those without a current rewrite)."""
    current = edited_parts(story)
    return sum(len(' '.join(plain(p) for p in c.get('paragraphs', [])).split())
               for i, c in enumerate(_chapters(story)) if _number(c, i) not in current)


def status(story) -> dict:
    """What the story window shows: progress, flags, cost, and whether a run is queued or running.
    Without a rewrite file the story text is not read at all (the window opens on every click)."""
    has_file = os.path.exists(_path(story))
    chapters = _chapters(story) if has_file else []
    total = len(chapters) if has_file else max(1, story.chapter_count or 1)
    edits = edited_parts(story, chapters) if has_file else {}
    data = load_edits(story) if has_file else {}
    with _lock:
        running = _running.get('story_id') == story.id
        queued = story.id in _queue
    flags = {n: e['flags'] for n, e in sorted(edits.items(), key=lambda item: float(item[0])) if e.get('flags')}
    return {'total': total, 'done': len(edits), 'flags': flags, 'flagged': list(flags),
            'running': running, 'queued': queued, 'live': progress() if running else {},
            'error': data.get('error', ''), 'cost': data.get('cost', 0.0), 'tokens': data.get('tokens', 0),
            'model': data.get('model', ''), 'finished': data.get('finished'), 'stopped': data.get('stopped', ''),
            'configured': bool(api_key())}


# --- Names and checks -----------------------------------------------------------------------

def initials(text: str) -> set:
    return set(INITIAL.findall(text))


def learn_names(original: str, edited: str, known: dict) -> dict:
    """Match each initial in the original to the new name the model used most for it (same first letter)."""
    names = dict(known)
    original_words = set(re.findall(r"\b[A-Z][a-z]+\b", original))
    candidates = Counter(word for word in re.findall(r"\b([A-Z][a-z]{2,})\b", edited) if word not in original_words)
    for letter in sorted(initials(original)):
        if letter in names:
            continue
        options = [(count, word) for word, count in candidates.items() if word[0] == letter and count >= 2]
        if options:
            names[letter] = max(options)[1]
    return names


def check(original: list, edited: list) -> list:
    """Reasons to look at a rewritten part. An empty list means it looks fine."""
    flags = []
    before, after = len(' '.join(original).split()), len(' '.join(edited).split())
    if before and not 0.85 <= after / before <= 1.15:
        flags.append(f'length changed from {before:,} to {after:,} words')
    text = '\n'.join(edited)
    if re.search('[–—]', text) and not re.search('[–—]', '\n'.join(original)):
        flags.append('contains dashes')
    left = sorted(initials(text) & initials('\n'.join(original)))
    if left:
        flags.append('still has initials ' + ', '.join(left))
    return flags


def paragraphs_of(text: str) -> list:
    return [p.strip() for p in re.split(r'\n\s*\n', (text or '').strip()) if p.strip()]


# --- OpenRouter -----------------------------------------------------------------------------

def request(text: str, key: str, model: str, label: str = 'rewrite') -> dict:
    """One part. Returns {'text', 'tokens', 'cost', 'model', 'truncated'}."""
    from . import ai_prompts
    from .ai_cover import _post, CHAT
    messages = [{'role': 'user', 'content': text}]
    if not model.startswith('@preset/'):
        messages.insert(0, {'role': 'system', 'content': ai_prompts.get('rewrite')})
    result = _post(CHAT, {'model': model, 'messages': messages, 'usage': {'include': True}}, key,
                   timeout=600, kind='rewrite', label=label)
    choice = (result.get('choices') or [{}])[0]
    usage = result.get('usage') or {}
    return {'text': (choice.get('message') or {}).get('content') or '', 'model': result.get('model') or model,
            'tokens': usage.get('total_tokens', 0) or 0, 'cost': float(usage.get('cost') or 0),
            'truncated': choice.get('finish_reason') == 'length'}


def rewrite_story(story_id: int, redo: bool = False) -> str:
    """Rewrite every part that has no current rewrite (or all parts, with redo). Saves after each part.
    Returns 'done', 'stopped' or 'failed'."""
    from app.models import Story, db
    story = db.session.get(Story, story_id)
    if not story:
        return 'failed'
    key, model = api_key(), settings()['model']
    if not key:
        raise ValueError('Add an OpenRouter API key in Settings → AI Covers first.')
    data = load_edits(story)
    if redo:
        data = {**data, 'parts': {}, 'names': {}, 'tokens': 0, 'cost': 0.0}
    data['error'] = ''
    data.pop('stopped', None)
    data.pop('finished', None)
    current = {} if redo else edited_parts(story)
    chapters = _chapters(story)
    if not chapters:
        raise ValueError('This story has no reader text to rewrite.')
    todo = [(i, c) for i, c in enumerate(chapters) if _number(c, i) not in current]
    log_action(f'AI Rewrite of "{story.title}": {len(todo)} of {len(chapters)} parts with {model}')
    previous = None
    for position, (index, chapter) in enumerate(todo, 1):
        number = _number(chapter, index)
        if _running.get('stop') and previous:
            data['stopped'] = f'Stopped after part {previous}.'
            _save_edits(story, data)
            log_action(f'AI Rewrite of "{story.title}" stopped after part {previous}, as asked')
            return 'stopped'
        with _lock:
            _running.update(index=position, total=len(todo), number=number, part_started=time.monotonic())
        text = [plain(p) for p in chapter.get('paragraphs', []) if plain(p)]
        original = '\n\n'.join(text)
        hint = ''
        if data['names']:
            hint = ('Character names already used in earlier parts of this story (keep using them): ' +
                    ', '.join(f'{letter} = {name}' for letter, name in sorted(data['names'].items())) + '\n\n')
        try:
            answer = request(hint + original, key, model, f'rewrite of story {story.id}, part {number}')
        except ValueError as error:
            data['error'] = f'Part {number}: {error}'
            _save_edits(story, data)
            log_error(f'AI Rewrite of "{story.title}" stopped at part {number}: {error}')
            return 'failed'
        edited = paragraphs_of(answer['text'])
        if not edited:
            data['error'] = f'Part {number}: the model returned no text.'
            _save_edits(story, data)
            log_error(f'AI Rewrite of "{story.title}": {data["error"]}')
            return 'failed'
        flags = check(text, edited)
        if answer['truncated']:
            flags.insert(0, 'the answer was cut off (the model reached its length limit)')
        data['names'] = learn_names(original, '\n\n'.join(edited), data['names'])
        data['parts'][number] = {'paragraphs': edited, 'source': _fingerprint(chapter.get('paragraphs', [])),
                                 'flags': flags, 'tokens': answer['tokens'], 'cost': answer['cost'],
                                 'model': answer['model'], 'edited_at': datetime.now(timezone.utc).isoformat()}
        data['tokens'] = data.get('tokens', 0) + answer['tokens']
        data['cost'] = round(data.get('cost', 0.0) + answer['cost'], 6)
        data['model'] = answer['model']
        _save_edits(story, data)
        previous = number
        with _lock:
            _running.setdefault('durations', []).append(time.monotonic() - _running.get('part_started', time.monotonic()))
    data['finished'] = datetime.now(timezone.utc).isoformat()
    _save_edits(story, data)
    log_action(f'AI Rewrite of "{story.title}" finished: {data["tokens"]} tokens, ${data["cost"]:.4f}')
    return 'done'


# --- Queue ----------------------------------------------------------------------------------

def start(app, story_id: int, redo: bool = False) -> str:
    """Queue a story; one story is rewritten at a time, separately from downloads and AI covers."""
    with _lock:
        if _running.get('story_id') == story_id:
            raise ValueError('This story is already being rewritten.')
        if story_id in _queue:
            return 'queued'
        _queue.append(story_id)
        _redo[story_id] = redo
        _persist()
        if _running.get('working'):
            return 'queued'
        _running['working'] = True
    threading.Thread(target=_work, args=(app,), daemon=True, name='AIRewrite').start()
    return 'started'


def _work(app) -> None:
    from app.models import Story, db
    while True:
        with _lock:
            if not _queue:
                _running.clear()
                return
            story_id = _queue.popleft()
            redo = _redo.pop(story_id, False)
            for name in ('stop', 'index', 'total', 'number', 'part_started', 'durations'):
                _running.pop(name, None)
            _running['story_id'] = story_id
            _persist()
        state = 'failed'
        try:
            with app.app_context():
                state = rewrite_story(story_id, redo)
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else 'AI Rewrite stopped unexpectedly.'
            log_error(f'AI Rewrite of story {story_id} failed: {error}')
            try:
                with app.app_context():
                    story = db.session.get(Story, story_id)
                    if story:
                        data = load_edits(story)
                        data['error'] = message
                        _save_edits(story, data)
            except Exception:
                pass
        if state == 'failed':
            try:
                from .notifier import send_notification
                send_notification(f'AI Rewrite failed for story {story_id}. See the story window for details.', is_error=True)
            except Exception:
                pass
        with _lock:
            _running.pop('story_id', None)
            _persist()


def _persist() -> None:
    """Save the running story and the waiting ones (call with _lock held), for after a restart. The running
    one goes back without redo, so it continues after the parts already rewritten."""
    from . import ai_queues
    running = [[_running['story_id'], False]] if _running.get('story_id') else []
    ai_queues.save('rewrite', running + [[i, bool(_redo.get(i))] for i in _queue])


def restore(app) -> None:
    """Queue the rewrites saved before the last restart again, the running one first."""
    from . import ai_queues
    from app.models import Story, db
    with app.app_context():
        items = [i for i in ai_queues.load('rewrite') if db.session.get(Story, i[0])]
    for story_id, redo in items:
        try:
            start(app, story_id, bool(redo))
        except ValueError:
            pass
    if items:
        log_action(f'Restored {len(items)} AI Rewrite {"job" if len(items) == 1 else "jobs"} after a restart')


def reorder(story_ids: list) -> None:
    """The Queue page's drag and drop: put the waiting stories in this order."""
    with _lock:
        wanted = [i for i in story_ids if i in _queue]
        rest = [i for i in _queue if i not in wanted]
        _queue.clear()
        _queue.extend(wanted + rest)
        _persist()


def overview() -> dict:
    """The Queue page: the story being worked on and the waiting ones, in order."""
    with _lock:
        return {'running': _running.get('story_id'), 'waiting': list(_queue)}


def remove(story_id: int) -> bool:
    with _lock:
        if story_id not in _queue:
            return False
        _queue.remove(story_id)
        _redo.pop(story_id, None)
        _persist()
    return True


def request_stop(story_id: int) -> bool:
    """Stop the running story after the part it is on (parts already rewritten are kept)."""
    with _lock:
        if _running.get('story_id') != story_id:
            return False
        _running['stop'] = True
    log_action(f'AI Rewrite of story {story_id} will stop after its current part')
    return True


def progress() -> dict:
    """The running story's part, position and estimated time left (from the parts finished so far)."""
    with _lock:
        if not _running.get('story_id') or not _running.get('total'):
            return {'story_id': _running.get('story_id')} if _running.get('story_id') else {}
        index, total, durations = _running['index'], _running['total'], list(_running.get('durations', []))
        info = {'story_id': _running['story_id'], 'index': index, 'total': total, 'number': _running.get('number'),
                'stopping': bool(_running.get('stop'))}
        elapsed = time.monotonic() - _running.get('part_started', time.monotonic())
    if durations:
        seconds = max(0, round(sum(durations) / len(durations) * (total - index + 1) - elapsed))
        info['eta_text'] = 'under a minute left' if seconds < 60 else f'about {round(seconds / 60)} min left'
    return info

