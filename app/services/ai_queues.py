"""The AI Rewrite and AI summary queues, saved in data/ai-queues.json so they survive a restart or a
container update (as in RedKeeper). Each queue is a list of [story_id, option] pairs, the running story first."""
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
