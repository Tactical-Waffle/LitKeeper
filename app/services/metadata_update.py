"""Update a saved story's details from Literotica: community rating, views, favorites, comments, the
description, tags and page count. Only the story's first page is read (for a series, the series page and its
first part), never the story text.

Updates have their own queue, separate from downloads, and run one at a time in the background. They share
the download pacing (http_client.global_rate_limiter), so asking for the whole library never sends Literotica
more requests a minute than downloads do. When Literotica refuses several requests in a row, the queue waits
longer between them. The queue is saved in data/ai-queues.json (as "metadata") and comes back after a restart.
"""
from __future__ import annotations
import threading
import time
from collections import deque
from datetime import datetime

from .logger import log_action, log_error

STATS = (('score', 'literotica_score'), ('views', 'literotica_views'),
         ('favorites', 'literotica_favorites'), ('comments', 'literotica_comments'))
FAILURE_PAUSE_SECONDS = 60     # After a refused request; doubles each time in a row, up to MAX_PAUSE_SECONDS.
MAX_PAUSE_SECONDS = 600

_queue: deque = deque()
_running: dict = {}
_errors: dict = {}
_finished: dict = {'done': 0, 'failed': 0}
_lock = threading.Lock()
_stop = threading.Event()


def source_url(story):
    """The page to read: the series page for a series (its numbers are the whole series' totals)."""
    if story.literotica_series_url and (story.chapter_count or 1) > 1:
        return story.literotica_series_url
    return story.literotica_url


def can_update(story) -> bool:
    return bool(source_url(story)) and (story.source_type or 'literotica') == 'literotica'


def apply(story, metadata: dict) -> list:
    """Copy what Literotica sent onto the story. Returns the names of what changed."""
    from app.models import Tag
    changed = []
    for key, column in STATS:
        value = metadata.get(key)
        if value is not None and getattr(story, column) != value:
            setattr(story, column, value)
            changed.append(key)
    description = (metadata.get('description') or '').strip()
    if description and description != (story.description or ''):
        story.description = description
        changed.append('description')
    if metadata.get('page_count') and not ((story.chapter_count or 1) > 1) \
            and metadata['page_count'] != story.literotica_page_count:
        story.literotica_page_count = metadata['page_count']
        changed.append('pages')
    tags = [t for t in dict.fromkeys(t.strip() for t in metadata.get('tags') or []) if t]
    if tags and {t.lower() for t in tags} != {t.name.lower() for t in story.tags}:
        found = []
        for name in tags:
            tag = Tag.query.filter_by(name=name).first() or Tag.query.filter_by(slug=Tag.create_slug(name)).first()
            if not tag:
                from app.models import db
                tag = Tag(name=name)
                db.session.add(tag)
                db.session.flush()
            if tag not in found:
                found.append(tag)
        story.tags = found
        changed.append('tags')
    if metadata.get('author_url') and story.author and not story.author.literotica_url:
        story.author.literotica_url = metadata['author_url']
    story.last_metadata_refresh = datetime.utcnow()
    story.metadata_refresh_status = 'success'
    return changed


def update(story_id: int) -> list:
    """Update one story now. Raises ValueError with a message for the person when it can't."""
    from app.models import Story, db
    from .http_client import global_rate_limiter
    from .story_downloader import fetch_story_metadata
    story = db.session.get(Story, story_id)
    if not story:
        raise ValueError('The story was deleted.')
    if not can_update(story):
        raise ValueError('This story has no Literotica address.')
    global_rate_limiter.wait_if_needed()
    metadata = fetch_story_metadata(source_url(story))
    if not metadata or (metadata.get('views') is None and metadata.get('score') is None and not metadata.get('tags')):
        raise ValueError('Literotica did not send the story’s details (it may be limiting requests).')
    changed = apply(story, metadata)
    db.session.commit()
    log_action(f"Updated story {story_id} from Literotica: " + (', '.join(changed) if changed else 'nothing changed'))
    return changed


# --- The queue ------------------------------------------------------------------------------------

def start(app, story_id: int) -> str:
    with _lock:
        if _running.get('story_id') == story_id or story_id in _queue:
            return 'queued'
        _queue.append(story_id)
        _errors.pop(story_id, None)
        _persist()
        if _running.get('working'):
            return 'queued'
        _running['working'] = True
        _stop.clear()
    threading.Thread(target=_work, args=(app,), daemon=True, name='MetadataUpdates').start()
    return 'started'


def start_many(app, story_ids: list) -> int:
    """Queue several stories at once (one save of the queue). Returns how many were added."""
    with _lock:
        known = set(_queue) | {_running.get('story_id')}
        new = [i for i in dict.fromkeys(story_ids) if i not in known]
        _queue.extend(new)
        for i in new:
            _errors.pop(i, None)
        _persist()
        if not _queue or _running.get('working'):
            return len(new)
        _running['working'] = True
        _stop.clear()
    threading.Thread(target=_work, args=(app,), daemon=True, name='MetadataUpdates').start()
    return len(new)


def _work(app) -> None:
    from app.models import db
    refused = 0
    while True:
        with _lock:
            if not _queue:
                _running.clear()
                _persist()
                return
            story_id = _queue.popleft()
            _running['story_id'] = story_id
            _persist()
        try:
            with app.app_context():
                update(story_id)
            refused = 0
            _finished['done'] += 1
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else 'The update stopped unexpectedly.'
            log_error(f'Literotica update for story {story_id} failed: {error}')
            _errors[story_id] = message
            _finished['failed'] += 1
            try:
                with app.app_context():
                    db.session.rollback()
            except Exception:
                pass
            if 'Literotica' in message:
                refused += 1
        finally:
            with _lock:
                _running.pop('story_id', None)
                _persist()
        if refused:
            pause = min(MAX_PAUSE_SECONDS, FAILURE_PAUSE_SECONDS * 2 ** (refused - 1))
            with _lock:
                _running['paused_until'] = time.time() + pause
            _stop.wait(pause)
            _stop.clear()
            with _lock:
                _running.pop('paused_until', None)


def _persist() -> None:
    from . import ai_queues
    running = [[_running['story_id'], 'metadata']] if _running.get('story_id') else []
    ai_queues.save('metadata', running + [[i, 'metadata'] for i in _queue])


def restore(app) -> None:
    """Queue the updates saved before the last restart again, the running one first."""
    from . import ai_queues
    from app.models import Story, db
    with app.app_context():
        items = [i[0] for i in ai_queues.load('metadata') if db.session.get(Story, i[0])]
    if items:
        start_many(app, items)
        log_action(f'Restored {len(items)} Literotica {"update" if len(items) == 1 else "updates"} after a restart')


def reorder(story_ids: list) -> None:
    with _lock:
        wanted = [i for i in story_ids if i in _queue]
        rest = [i for i in _queue if i not in wanted]
        _queue.clear()
        _queue.extend(wanted + rest)
        _persist()


def remove(story_id: int) -> bool:
    with _lock:
        if story_id not in _queue:
            return False
        _queue.remove(story_id)
        _persist()
    return True


def clear() -> int:
    """Take every waiting update out (the one running finishes). Returns how many were taken out."""
    with _lock:
        count = len(_queue)
        _queue.clear()
        _persist()
    _stop.set()  # Ends a pause after refused requests at once.
    return count


def overview() -> dict:
    with _lock:
        paused = _running.get('paused_until')
        return {'running': _running.get('story_id'), 'waiting': list(_queue),
                'paused_for': max(0, round(paused - time.time())) if paused else 0,
                'errors': dict(_errors)}


def info(story) -> dict:
    with _lock:
        running = _running.get('story_id') == story.id
        queued = story.id in _queue
    return {'running': running, 'queued': queued, 'error': _errors.get(story.id, ''),
            'possible': can_update(story),
            'updated': story.last_metadata_refresh.isoformat() if story.last_metadata_refresh else None}
