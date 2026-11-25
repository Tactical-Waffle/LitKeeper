"""The AI Rewrite and AI summary queues, saved in data/ai-queues.json so they survive a restart or a
container update (as in RedKeeper). Each queue is a list of [story_id, option] pairs, the running story first.
All AI work also pauses together when OpenRouter cannot be used at all (see pause)."""
from __future__ import annotations
import json
import os
import threading

_lock = threading.Lock()


def _file() -> str:
    from app.utils import paths
    return os.path.join(paths.get_data_directory(), 'ai-queues.json')


def _read() -> dict:
    try:
        with open(_file(), encoding='utf-8') as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(name: str, items: list) -> None:
    """Never raises: a queue that could not be saved only means it is not restored after a restart."""
    try:
        with _lock:
            data = _read()
            data[name] = items
            target = _file()
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target + '.tmp', 'w', encoding='utf-8') as handle:
                json.dump(data, handle)
            os.replace(target + '.tmp', target)
    except OSError:
        pass


def load(name: str) -> list:
    items = _read().get(name)
    return [i for i in items if isinstance(i, list) and len(i) == 2 and isinstance(i[0], int)] \
        if isinstance(items, list) else []


# --- Pausing all AI work ------------------------------------------------------------------------
# A failure no other story could get past (OpenRouter refused the key, the credit ran out, OpenRouter cannot
# be reached) pauses every AI queue instead of failing each waiting story in turn. The story that hit it goes
# back to the front of its queue; nothing waiting is lost. The queues start again when someone starts AI work
# (a button in the story window or the Queue page's Resume), when the AI settings are saved, or, when
# OpenRouter could not be reached, by themselves a few minutes later.

_pause: dict = {}
_pause_lock = threading.Lock()


def paused() -> str:
    """Why AI work is paused ('' when it is not)."""
    with _pause_lock:
        return _pause.get('reason', '')


def pause_info() -> dict:
    with _pause_lock:
        return dict(_pause)


def pause(app, error: Exception) -> None:
    """Pause every AI queue because of `error` (an ai_cover.StopError). Its `wait`, when set, is how many
    seconds until the queues try again by themselves."""
    from datetime import datetime, timedelta, timezone
    from .logger import log_error
    wait = getattr(error, 'wait', None)
    now = datetime.now(timezone.utc)
    with _pause_lock:
        first = not _pause
        generation = _pause.get('generation', 0) + 1
        _pause.clear()
        _pause.update(reason=str(error), since=now.isoformat(), generation=generation,
                      until=(now + timedelta(seconds=wait)).isoformat() if wait else '')
    if first:
        log_error(f'AI work paused: {error}')
        try:
            from .notifier import send_notification
            send_notification(f'AI work is paused: {error}', is_error=True)
        except Exception:
            pass
    if wait:
        def later():
            with _pause_lock:
                if _pause.get('generation') != generation:
                    return  # Resumed (or paused again for another reason) in the meantime.
            resume(app, 'trying OpenRouter again')
        timer = threading.Timer(wait, later)
        timer.daemon = True
        timer.start()


def resume(app, why: str = '') -> bool:
    """Lift the pause and start the waiting AI work again. Returns whether it was paused."""
    with _pause_lock:
        was = bool(_pause)
        _pause.clear()
    if was:
        from .logger import log_action
        log_action('AI work resumed' + (f' ({why})' if why else ''))
    from . import ai_cover, ai_rewrite, ai_summary
    for service in (ai_cover, ai_rewrite, ai_summary):
        try:
            service.kick(app)
        except Exception as error:
            from .logger import log_error
            log_error(f'Could not restart the {service.__name__.rsplit(".", 1)[-1]} queue: {error}')
    return was
