"""One- or two-sentence AI summaries of saved stories, through OpenRouter (from RedKeeper).

A text model reads the title, category, tags, description and the same story text as the AI cover's scene
description (Settings → AI Covers sets how much), and writes a summary with the AI summary prompt (Settings →
AI Covers → Prompts). At most two sentences are kept (asking once more if the reply is too long).

Literotica stories already have the author's own short description, so the summary is kept beside it rather
than replacing it: in `stories/html/<id>_<name>.summary.json`, shown in the story window and the HTML download.
Summaries are made one at a time in the background, separately from downloads and the other AI work.
"""
from __future__ import annotations
import json
import os
import re
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from .logger import log_action, log_error

KEY = 'ai_summaries'
DEFAULT_MODEL = 'deepseek/deepseek-v4-flash'
MODEL_NAME = re.compile(r'[@\w./:-]+')
MAX_WORDS = 60

_queue: deque = deque()
_running: dict = {}
_errors: dict = {}
_lock = threading.Lock()


# --- Settings -------------------------------------------------------------------------------

def settings() -> dict:
    from app.models import AppConfig
    config = AppConfig.query.filter_by(key=KEY).first()
    saved = config.get_value() if config else None
    return {'model': DEFAULT_MODEL, 'auto': False, **(saved if isinstance(saved, dict) else {})}


def save_settings(model: str, auto: bool) -> dict:
    from app.models import AppConfig, db
    model = (model or '').strip() or DEFAULT_MODEL
    if len(model) > 120 or not MODEL_NAME.fullmatch(model):
        raise ValueError('Enter an OpenRouter model name, for example deepseek/deepseek-v4-flash.')
    config = AppConfig.query.filter_by(key=KEY).first()
    if not config:
        config = AppConfig(key=KEY, value_type='json', description='OpenRouter model and options for AI summaries')
        db.session.add(config)
    config.value_type = 'json'
    config.set_value({'model': model, 'auto': bool(auto)})
    db.session.commit()
    return settings()


def api_key() -> str:
    from .ai_cover import settings as cover_settings
    return cover_settings()['api_key']


# --- The summary ----------------------------------------------------------------------------

def _path(story) -> str:
    from app.utils import paths
    return os.path.join(paths.get_html_directory(), f'{story.id}_{story.filename_base}.summary.json')


def saved(story) -> dict:
    try:
        with open(_path(story), encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and data.get('text') else {}
    except (OSError, ValueError):
        return {}


def summary(story) -> str:
    return saved(story).get('text', '')


def set_summary(story, text: str, model: str = '', cost: float = 0.0) -> None:
    """Store a summary; an empty text removes it."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    if len(text) > 1000:
        raise ValueError('Keep the summary under 1,000 characters.')
    path = _path(story)
    if not text:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {'text': text, 'model': model, 'cost': round(float(cost or 0), 6),
            'made': datetime.now(timezone.utc).isoformat()}
    with open(path + '.tmp', 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
    os.replace(path + '.tmp', path)


def tidy(reply: str) -> str:
    """Plain text, without a 'Summary:' label or quotation marks, cut to the first two sentences."""
    text = re.sub(r'\s+', ' ', reply or '').strip()
    text = re.sub(r'^[*_\s]*summary[*_\s]*:[*_\s]*', '', text, flags=re.I).strip(' "“”*_')
    sentences = re.split(r'(?<=[.!?…])["”’]?\s+(?=[A-Z"“‘])', text)
    return ' '.join(sentences[:2]).strip()


def write(story, key: str, model: str) -> tuple[str, float]:
    """The summary and what it cost."""
    from . import ai_prompts
    from .ai_cover import _post, CHAT, story_details
    messages = [{'role': 'system', 'content': ai_prompts.get('summary')},
                {'role': 'user', 'content': story_details(story)}]
    cost, text = 0.0, ''
    for attempt in range(2):
        result = _post(CHAT, {'model': model, 'usage': {'include': True}, 'max_tokens': 3000,
                              'reasoning': {'effort': 'low', 'exclude': True}, 'messages': messages}, key,
                       timeout=180, kind='summary', label=f'summary for story {story.id}')
        cost += float((result.get('usage') or {}).get('cost') or 0)
        choice = (result.get('choices') or [{}])[0]
        reply = (choice.get('message') or {}).get('content') or ''
        text = tidy(reply)
        if 5 <= len(text.split()) <= MAX_WORDS:
            return text, cost
        finish = choice.get('finish_reason') or choice.get('native_finish_reason') or ''
        log_action(f'AI summary from {model} unusable (finish {finish or "none"}): {reply[:200]!r}')
        if finish == 'content_filter':
            raise ValueError(f"The summary model ({model}) was blocked by its provider's content filter. "
                             'Choose another model in Settings → AI Summaries.')
        if attempt == 0 and text:
            messages += [{'role': 'assistant', 'content': reply},
                         {'role': 'user', 'content': f'Too long. Rewrite it as at most two sentences and {MAX_WORDS - 15} words.'}]
    if len(text.split()) > MAX_WORDS:
        return ' '.join(text.split()[:MAX_WORDS]).rstrip(',;:') + '…', cost
    if len(text.split()) >= 5:
        return text, cost
    raise ValueError(f'The summary model ({model}) returned no usable summary. Try again, or choose another '
                     'model in Settings → AI Summaries.')


def make(story_id: int) -> None:
    from app.models import Story, db
    story = db.session.get(Story, story_id)
    if not story:
        return
    key = api_key()
    if not key:
        raise ValueError('Add an OpenRouter API key in Settings → AI Covers first.')
    model = settings()['model']
    text, cost = write(story, key, model)
    set_summary(story, text, model, cost)
    _errors.pop(story.id, None)
    log_action(f'AI summary made for story {story_id} with {model}, cost ${cost:.4f}')


def make_after_download(app, story_id: Optional[int]) -> None:
    """Settings → AI Summaries: queue a summary for a newly downloaded story, when that is turned on."""
    if not story_id:
        return
    try:
        if settings().get('auto') and api_key():
            start(app, story_id)
    except ValueError:
        pass
    except Exception as e:
        log_error(f'Could not queue an AI summary for story {story_id}: {e}')


# --- Background worker ----------------------------------------------------------------------

def info(story) -> dict:
    with _lock:
        running = _running.get('story_id') == story.id
        queued = story.id in _queue
    data = saved(story)
    return {'summary': data.get('text', ''), 'model': data.get('model', ''), 'cost': data.get('cost'),
            'running': running, 'queued': queued, 'error': _errors.get(story.id, ''), 'configured': bool(api_key())}


def start(app, story_id: int) -> str:
    with _lock:
        if _running.get('story_id') == story_id:
            raise ValueError('A summary for this story is already being written.')
        if story_id in _queue:
            return 'queued'
        _queue.append(story_id)
        _errors.pop(story_id, None)
        _persist()
        if _running.get('working'):
            return 'queued'
        _running['working'] = True
    threading.Thread(target=_work, args=(app,), daemon=True, name='AISummaries').start()
    return 'started'


def _work(app) -> None:
    from app.models import db
    while True:
        with _lock:
            if not _queue:
                _running.clear()
                return
            story_id = _queue.popleft()
            _running['story_id'] = story_id
            _persist()
        try:
            with app.app_context():
                make(story_id)
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else 'Writing the summary stopped unexpectedly.'
            log_error(f'AI summary for story {story_id} failed: {error}')
            _errors[story_id] = message
            try:
                with app.app_context():
                    db.session.rollback()
            except Exception:
                pass
        finally:
            with _lock:
                _running.pop('story_id', None)
                _persist()


def _persist() -> None:
    from . import ai_queues
    running = [[_running['story_id'], 'summary']] if _running.get('story_id') else []
    ai_queues.save('summary', running + [[i, 'summary'] for i in _queue])


def restore(app) -> None:
    """Queue the summaries saved before the last restart again, the running one first."""
    from . import ai_queues
    from app.models import Story, db
    with app.app_context():
        items = [i for i in ai_queues.load('summary') if db.session.get(Story, i[0])]
    for story_id, _ in items:
        try:
            start(app, story_id)
        except ValueError:
            pass
    if items:
        log_action(f'Restored {len(items)} AI summary {"job" if len(items) == 1 else "jobs"} after a restart')


def remove(story_id: int) -> bool:
    with _lock:
        if story_id not in _queue:
            return False
        _queue.remove(story_id)
        _persist()
    return True
