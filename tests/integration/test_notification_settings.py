from __future__ import annotations
import pytest
from unittest.mock import patch
from flask.testing import FlaskClient

USER_KEY = 'u' * 30
APP_TOKEN = 'a' * 30
PUSHOVER = {'service': 'pushover', 'name': 'My phone',
            'fields': {'user_key': USER_KEY, 'app_token': APP_TOKEN, 'priority': 'normal'},
            'events': ['download', 'error']}


@pytest.fixture(autouse=True)
def no_saved_connections(app):
    """LitKeeper's test app shares one database between tests, so start and finish with no connections."""
    from app.services import notifier
    with app.app_context():
        notifier.save_connections([])
    yield
    with app.app_context():
        notifier.save_connections([])


def _add(client: FlaskClient, payload: dict = PUSHOVER) -> dict:
    response = client.post('/settings/notifications', json=payload)
    assert response.status_code == 200, response.get_json()
    return response.get_json()['connection']


@pytest.mark.integration
class TestNotificationSettings:

    def test_settings_page_shows_section(self, client: FlaskClient) -> None:
        response = client.get('/settings/')
        assert response.status_code == 200
        assert b'notificationsSection' in response.data
        assert b'Pushover' in response.data

    def test_add_and_list(self, client: FlaskClient) -> None:
        added = _add(client)
        assert added['name'] == 'My phone'
        assert added['events'] == ['download', 'error']

        listed = client.get('/settings/notifications').get_json()
        assert [c['id'] for c in listed['connections']] == [added['id']]

    def test_keys_never_sent_back_to_browser(self, client: FlaskClient) -> None:
        _add(client)
        for body in (client.get('/settings/notifications').data, client.get('/settings/').data):
            assert USER_KEY.encode() not in body
            assert APP_TOKEN.encode() not in body

    def test_add_rejects_missing_fields(self, client: FlaskClient) -> None:
        response = client.post('/settings/notifications', json={'service': 'pushover', 'fields': {'user_key': USER_KEY}})
        assert response.status_code == 400
        assert 'API Token' in response.get_json()['message']

    def test_add_rejects_unrecognised_url(self, client: FlaskClient) -> None:
        response = client.post('/settings/notifications', json={'service': 'apprise', 'fields': {'url': 'nonsense://x'}})
        assert response.status_code == 400

    def test_update_events(self, client: FlaskClient) -> None:
        added = _add(client)
        response = client.post(f"/settings/notifications/{added['id']}", json={'events': ['update']})
        assert response.get_json()['connection']['events'] == ['update']
        listed = client.get('/settings/notifications').get_json()
        assert listed['connections'][0]['events'] == ['update']

    def test_delete(self, client: FlaskClient) -> None:
        added = _add(client)
        assert client.delete(f"/settings/notifications/{added['id']}").status_code == 200
        assert client.get('/settings/notifications').get_json()['connections'] == []
        assert client.delete(f"/settings/notifications/{added['id']}").status_code == 404

    def test_test_unsaved_details(self, client: FlaskClient) -> None:
        with patch('app.services.notifier.send_test', return_value=True) as send_test:
            response = client.post('/settings/notifications/test', json=PUSHOVER)
        assert response.status_code == 200
        send_test.assert_called_once_with(f'pover://{USER_KEY}@{APP_TOKEN}?priority=normal')

    def test_test_saved_connection_failure(self, client: FlaskClient) -> None:
        added = _add(client)
        with patch('app.services.notifier.send_test', return_value=False):
            response = client.post(f"/settings/notifications/{added['id']}/test")
        assert response.status_code == 502
        assert response.get_json()['success'] is False

    def test_test_unknown_connection(self, client: FlaskClient) -> None:
        assert client.post('/settings/notifications/missing/test').status_code == 404
