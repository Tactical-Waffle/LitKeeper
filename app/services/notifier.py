from __future__ import annotations
import os
import traceback
import uuid
from typing import Optional
from urllib.parse import quote
from .logger import log_error

NOTIFICATION_URLS_RAW = os.getenv('NOTIFICATION_URLS', '')
NOTIFICATION_URLS = NOTIFICATION_URLS_RAW.split(',')

ENABLE_NOTIFICATIONS = bool(NOTIFICATION_URLS and NOTIFICATION_URLS[0])

def _initialize_logging() -> None:
    """Initialize startup logging for notification configuration."""
    from .logger import _log_startup_info
    _log_startup_info()

_initialize_logging()

CONFIG_KEY = 'notification_connections'

# Events a connection can subscribe to. Addresses from the NOTIFICATION_URLS
# environment variable keep receiving every event, as before.
EVENTS = {
    'download': 'Story downloaded',
    'update': 'Story updates found',
    'file': 'EPUB or reader file created',
    'error': 'Errors and failures',
}
DEFAULT_EVENTS = ['download', 'update', 'error']

PUSHOVER_PRIORITIES = {'low': 'Quiet', 'normal': 'Normal', 'high': 'High'}

# Services offered in Settings → Notifications. Each one turns a few form
# fields into an Apprise URL; "Other" accepts any Apprise URL directly.
SERVICES: dict[str, dict] = {
    'pushover': {
        'label': 'Pushover',
        'fields': [
            {'name': 'user_key', 'label': 'User Key', 'secret': True},
            {'name': 'app_token', 'label': 'API Token', 'secret': True},
            {'name': 'priority', 'label': 'Priority', 'choices': list(PUSHOVER_PRIORITIES.items()), 'default': 'normal'},
        ],
    },
    'discord': {
        'label': 'Discord',
        'fields': [
            {'name': 'webhook_url', 'label': 'Webhook URL', 'secret': True,
             'placeholder': 'https://discord.com/api/webhooks/...'},
        ],
    },
    'telegram': {
        'label': 'Telegram',
        'fields': [
            {'name': 'bot_token', 'label': 'Bot Token', 'secret': True},
            {'name': 'chat_id', 'label': 'Chat ID'},
        ],
    },
    'ntfy': {
        'label': 'ntfy',
        'fields': [
            {'name': 'topic', 'label': 'Topic'},
            {'name': 'server', 'label': 'Server', 'optional': True, 'placeholder': 'Leave blank for ntfy.sh'},
        ],
    },
    'gotify': {
        'label': 'Gotify',
        'fields': [
            {'name': 'server', 'label': 'Server', 'placeholder': 'https://gotify.example.com'},
            {'name': 'app_token', 'label': 'App Token', 'secret': True},
        ],
    },
    'apprise': {
        'label': 'Other (Apprise URL)',
        'fields': [
            {'name': 'url', 'label': 'Apprise URL', 'secret': True,
             'placeholder': 'e.g. mailto://user:pass@example.com'},
        ],
    },
}


def _q(value: str) -> str:
    return quote(value.strip(), safe='')


def _split_server(server: str) -> tuple[str, str]:
    """Return (scheme suffix, host[/path]) for a server typed with or without http(s)://."""
    server = server.strip().rstrip('/')
    if server.startswith('http://'):
        return '', server[len('http://'):]
    if server.startswith('https://'):
        return 's', server[len('https://'):]
    return 's', server


def build_url(service: str, fields: dict) -> str:
    """Turn a service and its form fields into an Apprise URL. Raises ValueError for missing fields."""
    spec = SERVICES.get(service)
    if not spec:
        raise ValueError('Choose a notification service.')
    values = {f['name']: str(fields.get(f['name']) or '').strip() for f in spec['fields']}
    for f in spec['fields']:
        if not values[f['name']] and not f.get('optional') and 'choices' not in f:
            raise ValueError(f"Enter the {f['label']}.")

    if service == 'pushover':
        priority = values['priority'] if values['priority'] in PUSHOVER_PRIORITIES else 'normal'
        return f"pover://{_q(values['user_key'])}@{_q(values['app_token'])}?priority={priority}"
    if service == 'discord':
        return values['webhook_url']
    if service == 'telegram':
        return f"tgram://{_q(values['bot_token'])}/{_q(values['chat_id'])}"
    if service == 'ntfy':
        if not values['server']:
            return f"ntfy://{_q(values['topic'])}"
        secure, host = _split_server(values['server'])
        return f"ntfy{secure}://{host}/{_q(values['topic'])}"
    if service == 'gotify':
        secure, host = _split_server(values['server'])
        return f"gotify{secure}://{host}/{_q(values['app_token'])}"
    return values['url']


def check_url(url: str) -> None:
    """Raise ValueError unless Apprise recognises the URL."""
    import apprise
    if not apprise.Apprise().add(url):
        raise ValueError('That address was not recognised. Check the details and try again.')


def clean_events(events) -> list[str]:
    return [e for e in EVENTS if e in (events or [])]


def mask(value: str) -> str:
    value = value or ''
    if len(value) <= 4:
        return '••••'
    return '••••' + value[-4:]


# --- Saved connections ------------------------------------------------------------------------

def load_connections() -> list[dict]:
    from app.models import AppConfig
    config = AppConfig.query.filter_by(key=CONFIG_KEY).first()
    value = config.get_value() if config else None
    return value if isinstance(value, list) else []


def save_connections(connections: list[dict]) -> None:
    from app.models import AppConfig, db
    config = AppConfig.query.filter_by(key=CONFIG_KEY).first()
    if not config:
        config = AppConfig(key=CONFIG_KEY, value_type='json',
                           description='Notification connections from Settings → Notifications')
        db.session.add(config)
    config.set_value(connections)
    db.session.commit()


def new_connection(name: str, service: str, fields: dict, events) -> dict:
    """Validate a new connection and return it (not yet saved)."""
    url = build_url(service, fields)
    check_url(url)
    spec = SERVICES[service]
    kept = {f['name']: str(fields.get(f['name']) or '').strip() or f.get('default', '') for f in spec['fields']}
    return {
        'id': uuid.uuid4().hex[:12],
        'name': (name or '').strip()[:60] or spec['label'],
        'service': service,
        'fields': kept,
        'events': clean_events(events),
    }


def public_view(connection: dict) -> dict:
    """A connection as shown in the browser: secret fields are masked."""
    spec = SERVICES.get(connection.get('service'), {'label': connection.get('service'), 'fields': []})
    details = []
    for f in spec['fields']:
        value = (connection.get('fields') or {}).get(f['name']) or ''
        if not value:
            continue
        if f.get('secret'):
            value = mask(value)
        elif 'choices' in f:
            value = dict(f['choices']).get(value, value)
        details.append({'label': f['label'], 'value': value})
    return {
        'id': connection.get('id'),
        'name': connection.get('name'),
        'service': connection.get('service'),
        'service_label': spec['label'],
        'details': details,
        'events': connection.get('events') or [],
    }


def env_url_count() -> int:
    return len([u for u in NOTIFICATION_URLS if u.strip()])


# --- Sending ----------------------------------------------------------------------------------

def _urls_for(event: str) -> list[str]:
    urls = [u.strip() for u in NOTIFICATION_URLS if u.strip()]
    from flask import has_app_context
    if not has_app_context():
        return urls
    try:
        for connection in load_connections():
            if event in (connection.get('events') or []):
                try:
                    urls.append(build_url(connection['service'], connection.get('fields') or {}))
                except (KeyError, ValueError):
                    log_error(f"Skipping notification connection '{connection.get('name')}': incomplete settings")
    except Exception as e:
        log_error(f"Could not load notification connections: {str(e)}")
    return urls


def deliver(urls: list[str], body: str) -> bool:
    """Send one message to the given Apprise URLs. Returns True if every service accepted it."""
    import apprise

    apobj = apprise.Apprise()
    added_count = 0
    for i, url in enumerate(urls):
        if apobj.add(url):
            added_count += 1
        else:
            # Never log the URL itself: it usually contains account keys.
            scheme = url.split('://', 1)[0] if '://' in url else 'unknown'
            log_error(f"Failed to add notification URL {i+1} ({scheme}://…)")

    if added_count == 0:
        log_error("No notification URLs were successfully added to Apprise")
        return False

    if apobj.notify(body=body):
        return True
    log_error("Failed to send notification - apobj.notify() returned False")
    return False


def send_notification(message: str, is_error: bool = False, event: Optional[str] = None) -> None:
    """Send a notification to the env-configured URLs and to saved connections subscribed to the event."""
    event = 'error' if is_error else (event or 'download')
    try:
        urls = _urls_for(event)
        if not urls:
            return
        icon = "❌" if is_error else "✅"
        deliver(urls, f"{icon} {message}")
    except Exception as e:
        log_error(f"Error sending notification: {str(e)}\n{traceback.format_exc()}")


def send_test(url: str) -> bool:
    return deliver([url], "✅ LitKeeper test notification. If you can read this, notifications are working.")
