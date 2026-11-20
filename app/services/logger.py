from __future__ import annotations
import logging
import logging.handlers
import os
import sys
from typing import Optional

# Log files live in the data folder (data/logs), so they survive container updates.
MAIN_LOG = 'litkeeper.log'
API_LOG = 'api.log'
LOG_FILES = (MAIN_LOG, API_LOG)
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 5
_FORMAT = logging.Formatter('%(asctime)s - %(name)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')


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


def _file_handler(path: str) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS,
                                                   encoding='utf-8', delay=True)
    handler.setFormatter(_FORMAT)
    handler._litkeeper_file = True  # marks handlers this module added, so setup can be repeated
    return handler


def log_file_path(name: str) -> str:
    from app.utils import paths
    return os.path.join(paths.get_logs_directory(), name)


def setup_file_logging() -> None:
    """Write the main log to data/logs/litkeeper.log and API calls to data/logs/api.log.
    Both rotate at 5 MB, keeping 5 old files. Safe to call more than once."""
    try:
        os.makedirs(os.path.dirname(log_file_path(MAIN_LOG)), exist_ok=True)
    except OSError as e:
        print(f"[LOGGING] Could not create the log folder, logging to the console only: {e}", file=sys.stderr)
        return
    targets = {MAIN_LOG: (action_logger, error_logger, url_logger, request_logger), API_LOG: (api_logger,)}
    for name, loggers in targets.items():
        for logger in loggers:
            for old in [h for h in logger.handlers if getattr(h, '_litkeeper_file', False)]:
                logger.removeHandler(old)
                old.close()
        handler = _file_handler(log_file_path(name))
        for logger in loggers:
            logger.addHandler(handler)


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


def tail_log(name: str, count: int = 500) -> list[str]:
    """The newest `count` lines of one of LitKeeper's logs, oldest first. If the current file
    has just been started afresh, the end of the previous one (name.1) fills the gap."""
    path = log_file_path(name)
    lines = _last_lines_of_file(path, count)
    if len(lines) < count:
        lines = _last_lines_of_file(path + '.1', count - len(lines)) + lines
    return lines


def is_api_path(path: str) -> bool:
    """Requests counted as API calls: the JSON API, the reader's API, the queue's API and the OPDS feed."""
    return path.startswith(('/api/', '/epub/api/', '/queue/api/', '/opds'))


def log_request(method: str, path: str, status: int, duration_ms: float, client: Optional[str]) -> None:
    """One line per request: API calls to api.log, page loads to the main log. Static files are skipped."""
    if path.startswith('/static/') or path == '/favicon.ico':
        return
    line = f"{method} {path} {status} {duration_ms:.0f} ms {client or '-'}"
    if is_api_path(path):
        api_logger.info(line)
    else:
        request_logger.info(line)


def log_api_call(service: str, method: str, endpoint: str, status: object, duration_ms: float) -> None:
    """An outgoing call LitKeeper makes to another service's API (e.g. OpenRouter). Never include keys."""
    api_logger.info(f"-> {service} {method} {endpoint} {status} {duration_ms:.0f} ms")


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
