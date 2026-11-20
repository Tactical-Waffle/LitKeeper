from __future__ import annotations
import itertools
import json
import logging
import os
import re
import sys
from datetime import date, timedelta
from typing import Optional

# Log files live in the data folder (data/logs), so they survive container updates.
MAIN_LOG = 'litkeeper.log'
API_LOG = 'api.log'
LOG_FILES = (MAIN_LOG, API_LOG)
DEFAULT_LOG_RETENTION_DAYS = 30
MAX_LOG_RETENTION_DAYS = 3650
DEFAULT_LOG_MAX_MB = 10
MAX_LOG_MAX_MB = 1000
LOG_SETTINGS_KEY = 'log_settings'
LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
_FORMAT = logging.Formatter('%(asctime)s - %(levelname)s - %(name)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
# A finished day's file: litkeeper.2026-11-19.log, or litkeeper.2026-11-19.1.log after the size limit.
_DATED = re.compile(r'^(litkeeper|api)\.(\d{4}-\d\d-\d\d)(?:\.(\d+))?\.log$')
# Files from earlier builds: litkeeper.log.2026-11-19 (renamed to the pattern above at startup).
_OLD_DATED = re.compile(r'^(litkeeper|api)\.log\.(\d{4}-\d\d-\d\d)$')


def _setup_console_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Configure a logger with console/stdout handler for Docker logs."""
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_FORMAT)
        logger.addHandler(handler)

    return logger

action_logger = _setup_console_logger('litkeeper.action')
error_logger = _setup_console_logger('litkeeper.error', logging.ERROR)
url_logger = _setup_console_logger('litkeeper.url')
request_logger = _setup_console_logger('litkeeper.request')

# API calls go only to api.log, never to the console, so `docker logs` isn't flooded
# by the browser's constant polling.
api_logger = logging.getLogger('litkeeper.api')
api_logger.setLevel(logging.INFO)
api_logger.propagate = False


def _env_retention_days() -> Optional[int]:
    try:
        days = int(os.environ['LOG_RETENTION_DAYS'])
    except (KeyError, ValueError):
        return None
    return days if 1 <= days <= MAX_LOG_RETENTION_DAYS else None


def log_retention_days() -> int:
    """Days of old log files to keep: Settings → Logs, else the LOG_RETENTION_DAYS environment variable, else 30."""
    return log_settings()['retention_days']


def log_settings() -> dict:
    """{'max_mb', 'retention_days'}: what Settings → Logs saved, with defaults for anything missing.
    Works without an app context (then only the environment variable and defaults apply)."""
    values = {'max_mb': DEFAULT_LOG_MAX_MB, 'retention_days': _env_retention_days() or DEFAULT_LOG_RETENTION_DAYS}
    try:
        from app.models import AppConfig
        config = AppConfig.query.filter_by(key=LOG_SETTINGS_KEY).first()
        saved = config.get_value() if config else None
    except Exception:  # No app context or no table yet.
        saved = None
    if isinstance(saved, dict):
        if isinstance(saved.get('max_mb'), int) and 1 <= saved['max_mb'] <= MAX_LOG_MAX_MB:
            values['max_mb'] = saved['max_mb']
        if isinstance(saved.get('retention_days'), int) and 1 <= saved['retention_days'] <= MAX_LOG_RETENTION_DAYS:
            values['retention_days'] = saved['retention_days']
    return values


def save_log_settings(max_mb, retention_days) -> dict:
    """Save Settings → Logs and apply it to the running logs. Raises ValueError for bad values."""
    from app.models import AppConfig, db
    try:
        size, days = int(str(max_mb).strip()), int(str(retention_days).strip())
    except (TypeError, ValueError):
        raise ValueError('Enter whole numbers.')
    if not 1 <= size <= MAX_LOG_MAX_MB:
        raise ValueError(f'Enter a maximum file size from 1 to {MAX_LOG_MAX_MB} MB.')
    if not 1 <= days <= MAX_LOG_RETENTION_DAYS:
        raise ValueError(f'Enter how many days to keep logs, from 1 to {MAX_LOG_RETENTION_DAYS}.')
    values = {'max_mb': size, 'retention_days': days}
    config = AppConfig.query.filter_by(key=LOG_SETTINGS_KEY).first()
    if not config:
        config = AppConfig(key=LOG_SETTINGS_KEY, value_type='json', description='Log file size limit and retention')
        db.session.add(config)
    config.value_type = 'json'
    config.set_value(values)
    db.session.commit()
    apply_log_settings(**values)
    return values


class DailyFileHandler(logging.FileHandler):
    """A log file started afresh each day and whenever it reaches max_bytes. The finished file is kept,
    uncompressed, as <name>.YYYY-MM-DD.log (<name>.YYYY-MM-DD.1.log … when the size limit was reached
    that day), and kept files older than retention_days are deleted."""

    def __init__(self, path: str, max_bytes: int, retention_days: int):
        super().__init__(path, encoding='utf-8', delay=True)
        self.max_bytes, self.retention_days = max_bytes, retention_days
        self.stem = os.path.basename(path).rsplit('.', 1)[0]
        self.day = self._file_day()
        self.remove_old()

    def _file_day(self) -> date:
        try:
            if os.path.getsize(self.baseFilename):
                return date.fromtimestamp(os.path.getmtime(self.baseFilename))
        except OSError:
            pass
        return date.today()

    def _size(self) -> int:
        if self.stream:
            return self.stream.tell()
        try:
            return os.path.getsize(self.baseFilename)
        except OSError:
            return 0

    def emit(self, record):
        # Logging must never break the app: a missing folder or full disk is reported, not raised.
        try:
            if date.today() != self.day or (self.max_bytes and self._size() >= self.max_bytes):
                self.rotate()
            super().emit(record)
        except Exception:
            self.handleError(record)

    def rotate(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        if os.path.exists(self.baseFilename) and os.path.getsize(self.baseFilename):
            folder, stamp = os.path.dirname(self.baseFilename), self.day.isoformat()
            target, number = os.path.join(folder, f'{self.stem}.{stamp}.log'), 1
            while os.path.exists(target):
                target = os.path.join(folder, f'{self.stem}.{stamp}.{number}.log')
                number += 1
            os.replace(self.baseFilename, target)
        self.day = date.today()
        self.remove_old()

    def remove_old(self):
        cutoff = date.today() - timedelta(days=max(1, self.retention_days))
        folder = os.path.dirname(self.baseFilename)
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for name in names:
            match = _DATED.match(name)
            if match and match.group(1) == self.stem and date.fromisoformat(match.group(2)) < cutoff:
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass


def log_file_path(name: str) -> str:
    from app.utils import paths
    return os.path.join(paths.get_logs_directory(), name)


def _rename_old_style_files(folder: str) -> None:
    """Earlier builds kept litkeeper.log.2026-11-19 (daily) and api.log.1 to .5 (5 MB); keep the first in
    the new naming and remove the second, which nothing would ever clear."""
    for name in os.listdir(folder):
        old = _OLD_DATED.match(name)
        try:
            if old:
                os.replace(os.path.join(folder, name), os.path.join(folder, f'{old.group(1)}.{old.group(2)}.log'))
            elif re.match(r'^(litkeeper|api)\.log\.[1-5]$', name):
                os.remove(os.path.join(folder, name))
        except OSError:
            pass


def setup_file_logging(max_mb: Optional[int] = None, retention_days: Optional[int] = None) -> None:
    """Write the main log to data/logs/litkeeper.log and API calls to data/logs/api.log. Each starts a
    new file every day or at the size limit, and old files are kept for the retention period. Safe to
    call more than once; without values it uses Settings → Logs (when the database is reachable)."""
    folder = os.path.dirname(log_file_path(MAIN_LOG))
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        print(f"[LOGGING] Could not create the log folder, logging to the console only: {e}", file=sys.stderr)
        return
    _rename_old_style_files(folder)
    settings = log_settings()
    max_mb = max_mb or settings['max_mb']
    retention_days = retention_days or settings['retention_days']
    targets = {MAIN_LOG: (action_logger, error_logger, url_logger, request_logger), API_LOG: (api_logger,)}
    for name, loggers in targets.items():
        for logger in loggers:
            for old in [h for h in logger.handlers if getattr(h, '_litkeeper_file', False)]:
                logger.removeHandler(old)
                old.close()
        handler = DailyFileHandler(log_file_path(name), max_mb * 1024 * 1024, retention_days)
        handler.setFormatter(_FORMAT)
        handler._litkeeper_file = True  # marks handlers this module added, so setup can be repeated
        for logger in loggers:
            logger.addHandler(handler)


def apply_log_settings(max_mb: int, retention_days: int) -> None:
    """Change the size limit and retention of the running logs (Settings → Logs)."""
    for logger in (action_logger, api_logger):
        for handler in logger.handlers:
            if isinstance(handler, DailyFileHandler):
                handler.max_bytes, handler.retention_days = max_mb * 1024 * 1024, retention_days
                handler.remove_old()


def _last_lines_of_file(path: str, count: int) -> list[str]:
    """The last `count` lines of a file, read backwards from the end so big files stay cheap."""
    if count <= 0 or not os.path.exists(path):
        return []
    block = 64 * 1024
    data = b''
    with open(path, 'rb') as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        while position > 0 and data.count(b'\n') <= count:
            step = min(block, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
    lines = data.decode('utf-8', errors='replace').splitlines()
    return lines[-count:]


def _dated_key(name: str):
    match = _DATED.match(name)
    return match.group(2), int(match.group(3) or 0)


def older_log_files(name: str) -> list[str]:
    """Kept files of one log (litkeeper.log or api.log), newest first, as full paths."""
    stem = name.rsplit('.', 1)[0]
    folder = os.path.dirname(log_file_path(name))
    try:
        names = [n for n in os.listdir(folder) if _DATED.match(n) and _DATED.match(n).group(1) == stem]
    except OSError:
        return []
    return [os.path.join(folder, n) for n in sorted(names, key=_dated_key, reverse=True)]


def is_log_file(name: str) -> bool:
    """Only LitKeeper's own log files can be read or downloaded from Settings."""
    return name in LOG_FILES or bool(_DATED.match(name or ''))


def log_files() -> list[dict]:
    """Every log file, current ones first, then kept ones newest first, for the viewer's file picker."""
    def size(path):
        bytes_ = os.path.getsize(path) if os.path.exists(path) else 0
        if bytes_ >= 1024 * 1024:
            return f'{bytes_ / (1024 * 1024):.1f} MB'
        return f'{bytes_ / 1024:.0f} KB' if bytes_ >= 1024 else ('empty' if not bytes_ else f'{bytes_} B')
    files = [{'name': MAIN_LOG, 'label': 'Main log (today)', 'size': size(log_file_path(MAIN_LOG))},
             {'name': API_LOG, 'label': 'API log (today)', 'size': size(log_file_path(API_LOG))}]
    kept = older_log_files(MAIN_LOG) + older_log_files(API_LOG)
    for path in sorted(kept, key=lambda p: _dated_key(os.path.basename(p)), reverse=True):
        name = os.path.basename(path)
        match = _DATED.match(name)
        label = f"{'Main' if match.group(1) == 'litkeeper' else 'API'} log, {match.group(2)}" + \
                (f' (part {int(match.group(3)) + 1})' if match.group(3) else '')
        files.append({'name': name, 'label': label, 'size': size(path)})
    return files


def tail_log(name: str, count: int = 500) -> list[str]:
    """The newest `count` lines of one of LitKeeper's current logs, oldest first. Early in the day, when
    today's file is still short, the end of the kept files fills the gap. A kept file is read on its own."""
    lines = _last_lines_of_file(log_file_path(name), count)
    if name not in LOG_FILES:
        return lines
    for older in older_log_files(name):
        if len(lines) >= count:
            break
        lines = _last_lines_of_file(older, count - len(lines)) + lines
    return lines


_ENTRY_START = re.compile(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d - (?:(%s) - )?(\S+) - ' % '|'.join(LEVELS))


def log_entries(name: str, count: int = 3000) -> list[dict]:
    """The newest entries of a log for the viewer: one per logged message, with continuation lines
    (tracebacks, multi-line errors) attached. Each has its level and whether it is an API data line."""
    result: list[dict] = []
    for line in tail_log(name, count * 2):
        start = _ENTRY_START.match(line)
        if start or not result:
            level = (start.group(1) if start else None) or \
                    ('ERROR' if start and start.group(2) == 'litkeeper.error' else 'INFO')
            message = line[start.end():] if start else line
            result.append({'text': line, 'level': level, 'api_data': message.startswith('API data #')})
        else:
            result[-1]['text'] += '\n' + line
    return result[-count:]


_api_numbers = itertools.count(1)


def next_api_number() -> int:
    """A number tying an API call's lines together: started, its data, and its result."""
    return next(_api_numbers)


_SECRET_NAME = re.compile(r'key|token|secret|password|passwd|auth|cookie|credential', re.I)
_LONGEST_TEXT = 4000


def _readable(value):
    """Data as written to the API log: credentials hidden, very long text (e.g. pictures) shortened."""
    if isinstance(value, dict):
        return {k: '(hidden)' if _SECRET_NAME.search(str(k)) and v else _readable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_readable(v) for v in value]
    if isinstance(value, str) and len(value) > _LONGEST_TEXT:
        return value[:_LONGEST_TEXT] + f'… ({len(value) - _LONGEST_TEXT} more characters)'
    return value


def log_api_data(number: int, data) -> None:
    """The JSON sent with API call #number, on one "API data #N sent:" line in api.log, which
    Settings → Logs can show or hide. Credentials are hidden."""
    try:
        text = json.dumps(_readable(data), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(data)[:_LONGEST_TEXT]
    api_logger.info(f"API data #{number} sent: {text}")


def is_api_path(path: str) -> bool:
    """Requests counted as API calls: the JSON API, the reader's API, the queue's API and its
    every-few-seconds status updates, and the OPDS feed."""
    return path.startswith(('/api/', '/epub/api/', '/queue/api/', '/queue/partials/', '/opds'))


def log_request(method: str, path: str, status: int, duration_ms: float, client: Optional[str], data=None) -> None:
    """One line per request: API calls to api.log, page loads to the main log. Static files are skipped.
    JSON sent to an API path goes on its own "API data" line after the request line."""
    if path.startswith('/static/') or path == '/favicon.ico':
        return
    if path.startswith('/settings/logs/') and path.endswith('/entries') and status == 200:
        return  # The log viewer checks every 3 seconds; logging that would fill the log it is showing.
    line = f"{method} {path} {status} {duration_ms:.0f} ms {client or '-'}"
    if is_api_path(path):
        if data is not None:
            number = next_api_number()
            api_logger.info(f"#{number} {line}")
            log_api_data(number, data)
        else:
            api_logger.info(line)
    else:
        request_logger.info(line)


def log_api_call(service: str, method: str, endpoint: str, status: object, duration_ms: float,
                 number: Optional[int] = None) -> None:
    """The result of an outgoing call LitKeeper makes to another service's API (e.g. OpenRouter). Never include keys."""
    api_logger.info(f"-> {f'#{number} ' if number else ''}{service} {method} {endpoint} {status} {duration_ms:.0f} ms")


def log_api_call_started(service: str, method: str, endpoint: str, data=None) -> int:
    """An outgoing call is starting: one line, plus its data on an "API data" line. Returns the call's number."""
    number = next_api_number()
    api_logger.info(f"-> #{number} {service} {method} {endpoint} started")
    if data is not None:
        log_api_data(number, data)
    return number


def _log_startup_info() -> None:
    """Log startup notification configuration information."""
    if action_logger:
        from .notifier import NOTIFICATION_URLS, ENABLE_NOTIFICATIONS
        # Notification URLs usually contain account keys, so only the service
        # type (the part before "://") is logged, never the URL itself.
        urls = [url.strip() for url in NOTIFICATION_URLS if url.strip()]
        action_logger.info("[STARTUP] Notification configuration loaded")
        action_logger.info(f"[STARTUP] NOTIFICATION_URLS: {len(urls)} URLs")
        for i, url in enumerate(urls):
            scheme = url.split('://', 1)[0] if '://' in url else 'unknown'
            action_logger.info(f"[STARTUP] URL {i+1}: {scheme}://…")
        action_logger.info(f"[STARTUP] ENABLE_NOTIFICATIONS: {ENABLE_NOTIFICATIONS}")

def log_action(message: str) -> None:
    """Log an action using rotating file handler."""
    if action_logger:
        action_logger.info(message)

def log_error(error_message: str, url: Optional[str] = None) -> None:
    """Log an error message using rotating file handler."""
    if error_logger:
        message = error_message
        if url and url not in error_message:
            message += f"\nURL: {url}"
        message += "\n" + "-"*50
        error_logger.error(message)

def log_url(url: str) -> None:
    """Log URL using rotating file handler."""
    if url_logger:
        url_logger.info(url)
