from __future__ import annotations
import logging
import pytest
from unittest.mock import patch, MagicMock
from flask import Flask
from app.services import notifier

USER_KEY = 'u' * 30
APP_TOKEN = 'a' * 30


@pytest.fixture(autouse=True)
def no_saved_connections(app):
    """LitKeeper's test app shares one database between tests, so start and finish with no connections."""
    from app.services import notifier
    with app.app_context():
        notifier.save_connections([])
    yield
    with app.app_context():
        notifier.save_connections([])


@pytest.mark.unit
class TestBuildUrl:
    """Form fields are turned into Apprise URLs that Apprise accepts."""

    def test_pushover(self) -> None:
        url = notifier.build_url('pushover', {'user_key': USER_KEY, 'app_token': APP_TOKEN, 'priority': 'high'})
        assert url == f'pover://{USER_KEY}@{APP_TOKEN}?priority=high'
        notifier.check_url(url)

    def test_pushover_unknown_priority_falls_back_to_normal(self) -> None:
        url = notifier.build_url('pushover', {'user_key': USER_KEY, 'app_token': APP_TOKEN, 'priority': 'loud'})
        assert url.endswith('?priority=normal')

    def test_discord_webhook_is_used_as_is(self) -> None:
        hook = 'https://discord.com/api/webhooks/123456/abcdefTOKEN'
        assert notifier.build_url('discord', {'webhook_url': hook}) == hook
        notifier.check_url(hook)

    def test_telegram(self) -> None:
        url = notifier.build_url('telegram', {'bot_token': '123456:ABCdef', 'chat_id': '987654'})
        assert url == 'tgram://123456%3AABCdef/987654'
        notifier.check_url(url)

    def test_ntfy_default_server(self) -> None:
        assert notifier.build_url('ntfy', {'topic': 'my-topic'}) == 'ntfy://my-topic'

    def test_ntfy_own_server(self) -> None:
        assert notifier.build_url('ntfy', {'topic': 't', 'server': 'https://ntfy.example.com/'}) == 'ntfys://ntfy.example.com/t'
        assert notifier.build_url('ntfy', {'topic': 't', 'server': 'http://10.0.0.2:8080'}) == 'ntfy://10.0.0.2:8080/t'

    def test_gotify(self) -> None:
        url = notifier.build_url('gotify', {'server': 'gotify.example.com', 'app_token': 'AbCdEf12345'})
        assert url == 'gotifys://gotify.example.com/AbCdEf12345'
        notifier.check_url(url)

    def test_missing_field_raises(self) -> None:
        with pytest.raises(ValueError, match='API Token'):
            notifier.build_url('pushover', {'user_key': USER_KEY})

    def test_unknown_service_raises(self) -> None:
        with pytest.raises(ValueError):
            notifier.build_url('carrier-pigeon', {})

    def test_check_url_rejects_unknown_address(self) -> None:
        with pytest.raises(ValueError):
            notifier.check_url('nonsense://x')


@pytest.mark.unit
class TestPublicView:

    def test_secrets_are_masked(self) -> None:
        connection = notifier.new_connection('Phone', 'pushover',
                                             {'user_key': USER_KEY, 'app_token': APP_TOKEN}, ['download'])
        view = notifier.public_view(connection)
        text = repr(view)
        assert USER_KEY not in text and APP_TOKEN not in text
        assert {'label': 'Priority', 'value': 'Normal'} in view['details']
        assert view['service_label'] == 'Pushover'

    def test_unknown_events_are_dropped(self) -> None:
        connection = notifier.new_connection('', 'ntfy', {'topic': 't'}, ['download', 'bogus', 'error'])
        assert connection['events'] == ['download', 'error']
        assert connection['name'] == 'ntfy'


@pytest.mark.unit
class TestSendNotification:
    """Saved connections only receive the events they are subscribed to."""

    def _save(self, app: Flask, events: list[str]) -> None:
        with app.app_context():
            notifier.save_connections([notifier.new_connection('Phone', 'ntfy', {'topic': 'litkeeper-test'}, events)])

    def test_subscribed_event_is_sent(self, app: Flask) -> None:
        self._save(app, ['download'])
        with app.app_context(), patch.object(notifier, 'deliver') as deliver:
            notifier.send_notification('Story downloaded')
        deliver.assert_called_once_with(['ntfy://litkeeper-test'], '✅ Story downloaded')

    def test_unsubscribed_event_is_not_sent(self, app: Flask) -> None:
        self._save(app, ['download'])
        with app.app_context(), patch.object(notifier, 'deliver') as deliver:
            notifier.send_notification('EPUB created', event='file')
            notifier.send_notification('Update failed', is_error=True)
        deliver.assert_not_called()

    def test_errors_use_error_event(self, app: Flask) -> None:
        self._save(app, ['error'])
        with app.app_context(), patch.object(notifier, 'deliver') as deliver:
            notifier.send_notification('Update failed', is_error=True)
        deliver.assert_called_once_with(['ntfy://litkeeper-test'], '❌ Update failed')

    def test_env_urls_receive_every_event(self, app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(notifier, 'NOTIFICATION_URLS', ['ntfy://from-env'])
        with app.app_context(), patch.object(notifier, 'deliver') as deliver:
            notifier.send_notification('EPUB created', event='file')
        deliver.assert_called_once_with(['ntfy://from-env'], '✅ EPUB created')

    def test_works_outside_app_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(notifier, 'NOTIFICATION_URLS', ['ntfy://from-env'])
        with patch.object(notifier, 'deliver') as deliver:
            notifier.send_notification('Story downloaded')
        deliver.assert_called_once()

    def test_nothing_configured_sends_nothing(self, app: Flask) -> None:
        with app.app_context(), patch.object(notifier, 'deliver') as deliver:
            notifier.send_notification('Story downloaded')
        deliver.assert_not_called()

    def test_delivery_failure_does_not_raise(self, app: Flask) -> None:
        self._save(app, ['download'])
        with app.app_context(), patch.object(notifier, 'deliver', side_effect=RuntimeError('offline')):
            notifier.send_notification('Story downloaded')


@pytest.mark.unit
class TestNoSecretsInLogs:

    def test_startup_log_shows_only_service_type(self, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        from app.services import logger
        secret_url = f'pover://{USER_KEY}@{APP_TOKEN}'
        monkeypatch.setattr(notifier, 'NOTIFICATION_URLS', [secret_url])
        with caplog.at_level(logging.INFO, logger='litkeeper.action'):
            logger._log_startup_info()
        assert 'pover://…' in caplog.text
        assert USER_KEY not in caplog.text and APP_TOKEN not in caplog.text

    def test_rejected_url_is_not_logged(self, caplog) -> None:
        with caplog.at_level(logging.ERROR, logger='litkeeper.error'):
            assert notifier.deliver([f'nonsense://{USER_KEY}'], 'hello') is False
        assert USER_KEY not in caplog.text
