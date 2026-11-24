"""AI call statistics for Settings: what each OpenRouter call cost and whether it worked, by purpose and model.

Every OpenRouter call LitKeeper makes (for an AI cover: the scene description from the text model, a toned-down
description, and each picture attempt from an image model; each part of an AI Rewrite; each AI summary) is recorded as one line of JSON in
data/ai-calls.jsonl when it finishes: when, what for, the model, the cost in US dollars as OpenRouter reports it,
and whether it gave a usable answer. Retries and refused attempts are separate calls.

The first time the statistics are shown without that file, it is built once from the AI covers already made
(covers/ai/*.art.json): finished covers only, so no failures, with each cover's whole cost under its image model.
"""
from __future__ import annotations
import glob
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

_lock = threading.Lock()
KINDS = {'cover_text': 'AI cover descriptions (text model)', 'cover_image': 'AI cover pictures (image model)',
         'rewrite': 'AI Rewrite', 'summary': 'AI summaries'}
PERIODS = [('today', 'Today'), ('yesterday', 'Yesterday'), ('week', 'This week'), ('month', 'This month'),
           ('days30', 'Past 30 days'), ('all', 'All time')]


def _file(data_directory: Optional[str] = None) -> str:
    from app.utils import paths
    return os.path.join(data_directory or paths.get_data_directory(), 'ai-calls.jsonl')


def record(kind: str, model: str, cost: float, ok: bool, status: str = '', at: Optional[datetime] = None) -> None:
    """Add one finished call. Never raises: statistics must not break making a cover."""
    entry = {'at': (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(), 'kind': kind,
             'model': model or 'unknown', 'cost': round(float(cost or 0), 6), 'ok': bool(ok), 'status': str(status or '')}
    try:
        backfill()  # So the first recorded call doesn't stop the existing covers being counted.
        with _lock, open(_file(), 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry) + '\n')
    except OSError:
        pass


def entries(data_directory: Optional[str] = None) -> list[dict]:
    try:
        with open(_file(data_directory), encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return []
    result = []
    for line in lines:
        try:
            result.append(json.loads(line))
        except ValueError:
            continue
    return result


def from_cover_notes(notes: list[dict]) -> list[dict]:
    """Calls for covers made before statistics were kept: one picture call (the cover's whole cost) and
    one description call (cost included in the picture's) per finished cover."""
    calls = []
    for note in notes:
        if not note.get('made') or note.get('cost') is None:
            continue
        calls.append({'at': note['made'], 'kind': 'cover_image', 'model': note.get('image_model') or 'unknown',
                      'cost': float(note.get('cost') or 0), 'ok': True, 'status': 'from finished covers'})
        if note.get('text_model'):
            calls.append({'at': note['made'], 'kind': 'cover_text', 'model': note['text_model'], 'cost': 0.0,
                          'ok': True, 'status': 'from finished covers'})
    return calls


def backfill() -> int:
    """Build ai-calls.jsonl once from the AI covers already made. Returns how many calls were added (0 if built)."""
    target = _file()
    if os.path.exists(target):
        return 0
    from app.utils import paths
    notes = []
    for path in glob.glob(os.path.join(paths.get_cover_directory(), 'ai', '*.art.json')):
        try:
            with open(path, encoding='utf-8') as handle:
                notes.append(json.load(handle))
        except (OSError, ValueError):
            continue
    calls = sorted(from_cover_notes(notes), key=lambda c: c['at'])
    with _lock:
        if os.path.exists(target):
            return 0
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as handle:
            handle.write(''.join(json.dumps(c) + '\n' for c in calls))
    return len(calls)


def _period_starts(now: datetime) -> dict:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {'today': (today, None), 'yesterday': (today - timedelta(days=1), today),
            'week': (today - timedelta(days=today.weekday()), None), 'month': (today.replace(day=1), None),
            'days30': (now - timedelta(days=30), None), 'all': (None, None)}


def tables(now: Optional[datetime] = None) -> dict:
    """Costs by purpose, and costs, successes and failures by model, for each period (server's local time)."""
    try:
        backfill()
    except OSError:
        pass
    now = (now or datetime.now(timezone.utc)).astimezone()
    windows = _period_starts(now)

    def periods(entry):
        at = datetime.fromisoformat(entry['at']).astimezone()
        return [name for name, (start, end) in windows.items() if (start is None or at >= start) and (end is None or at < end)]

    def blank():
        return {name: 0 for name, _ in PERIODS}

    kinds = {kind: {'label': label, 'cost': blank()} for kind, label in KINDS.items()}
    models: dict = {}
    for entry in entries():
        try:
            in_periods = periods(entry)
        except (KeyError, ValueError):
            continue
        row = models.setdefault(entry.get('model') or 'unknown',
                                {'model': entry.get('model') or 'unknown', 'cost': blank(), 'ok': blank(), 'failed': blank()})
        kind = kinds.get(entry.get('kind'))
        for name in in_periods:
            row['cost'][name] += entry.get('cost') or 0
            row['ok' if entry.get('ok') else 'failed'][name] += 1
            if kind:
                kind['cost'][name] += entry.get('cost') or 0
    for row in models.values():
        calls = row['ok']['all'] + row['failed']['all']
        row['average_cost'] = row['cost']['all'] / calls if calls else 0
        row['success_rate'] = row['ok']['all'] / calls if calls else 0
    ordered = sorted(models.values(), key=lambda r: (-r['cost']['all'], r['model']))
    return {'periods': PERIODS, 'kinds': list(kinds.values()), 'models': ordered,
            'total': {name: sum(k['cost'][name] for k in kinds.values()) for name, _ in PERIODS}}
