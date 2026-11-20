"""Settings → Reading: whether the reader marks a story read on reaching its end."""
from __future__ import annotations

AUTO_MARK_READ_KEY = 'auto_mark_read'


def auto_mark_read() -> bool:
    """Off unless turned on in Settings. Needs an app context."""
    from app.models import AppConfig
    cfg = AppConfig.query.filter_by(key=AUTO_MARK_READ_KEY).first()
    return bool(cfg.get_value()) if cfg else False


def set_auto_mark_read(on: bool) -> bool:
    from app.models import AppConfig, db
    cfg = AppConfig.query.filter_by(key=AUTO_MARK_READ_KEY).first()
    if not cfg:
        cfg = AppConfig(key=AUTO_MARK_READ_KEY, value_type='bool', description='Mark stories read at the end of the reader')
        db.session.add(cfg)
    cfg.value_type = 'bool'
    cfg.set_value(bool(on))
    db.session.commit()
    return bool(on)
