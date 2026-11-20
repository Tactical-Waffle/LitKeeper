"""Settings → Story Filter: terms that mark stories in the author window, in two lists.

- Any list: a story (or a series) is filtered when it, or any one of a series' parts, contains one of these.
- All list: a single story is filtered when it contains one of these; a series only when every part does
  (so one part in a category you avoid doesn't hide a long series, but a series that is all of it does).

Filtered stories are hidden in the author window by default (an option), and never ticked automatically.
"""
from __future__ import annotations
import re

ANY_KEY = 'story_filter_terms'      # The original single list, now the Any list.
ALL_KEY = 'story_filter_all'
HIDE_KEY = 'story_filter_hide'
MAX_TERMS = 200
MAX_LENGTH = 100


def _load(key: str) -> list[str]:
    from app.models import AppConfig
    cfg = AppConfig.query.filter_by(key=key).first()
    value = cfg.get_value() if cfg else None
    return [str(t) for t in value if str(t).strip()] if isinstance(value, list) else []


def settings() -> dict:
    """{'any': [...], 'all': [...], 'hide': bool}. Hiding is on unless turned off."""
    from app.models import AppConfig
    hide = AppConfig.query.filter_by(key=HIDE_KEY).first()
    return {'any': _load(ANY_KEY), 'all': _load(ALL_KEY), 'hide': bool(hide.get_value()) if hide else True}


def parse_terms(raw) -> list[str]:
    """Terms from a textarea (one per line or comma-separated) or a list; duplicates dropped.
    Raises ValueError when there are too many or one is too long."""
    pieces = raw if isinstance(raw, list) else re.split(r'[\n,]+', str(raw or ''))
    terms = list(dict.fromkeys(t.strip() for t in map(str, pieces) if t.strip()))
    if len(terms) > MAX_TERMS:
        raise ValueError(f'Use at most {MAX_TERMS} terms in each list.')
    if any(len(t) > MAX_LENGTH for t in terms):
        raise ValueError(f'Keep each term under {MAX_LENGTH} characters.')
    return terms


def save(any_terms=None, all_terms=None, hide=None) -> dict:
    """Save whichever parts are given. Raises ValueError for bad terms."""
    from app.models import AppConfig, db
    values = {}
    if any_terms is not None:
        values[ANY_KEY] = ('json', parse_terms(any_terms))
    if all_terms is not None:
        values[ALL_KEY] = ('json', parse_terms(all_terms))
    if hide is not None:
        values[HIDE_KEY] = ('bool', bool(hide))
    for key, (kind, value) in values.items():
        cfg = AppConfig.query.filter_by(key=key).first()
        if not cfg:
            cfg = AppConfig(key=key, value_type=kind, description='Story filter for the author window')
            db.session.add(cfg)
        cfg.value_type = kind
        cfg.set_value(value)
    db.session.commit()
    return settings()
