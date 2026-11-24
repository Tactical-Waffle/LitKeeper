from __future__ import annotations
import pytest
from flask.testing import FlaskClient


@pytest.fixture(autouse=True)
def no_saved_limit(app, monkeypatch):
    """LitKeeper's test app shares one database between tests, so start and finish with no saved limit."""
    monkeypatch.delenv('MAX_DAILY_DOWNLOADS', raising=False)
    from app.models import AppConfig, db
    from app.services.download_queue_worker import DAILY_CAP_KEY

    def clear():
        with app.app_context():
            AppConfig.query.filter_by(key=DAILY_CAP_KEY).delete()
            db.session.commit()

    clear()
    yield
    clear()


@pytest.mark.integration
class TestDailyDownloadLimit:

    def test_default_when_nothing_set(self, app) -> None:
        from app.services import download_queue_worker as dq
        with app.app_context():
            assert dq.get_daily_cap() == dq.DEFAULT_MAX_DAILY_DOWNLOADS

    def test_environment_variable_used_until_saved(self, app, client: FlaskClient, monkeypatch) -> None:
        from app.services import download_queue_worker as dq
        monkeypatch.setenv('MAX_DAILY_DOWNLOADS', '100')
        with app.app_context():
            assert dq.get_daily_cap() == 100
        assert client.post('/settings/daily-downloads', json={'limit': 40}).status_code == 200
        with app.app_context():
            assert dq.get_daily_cap() == 40

    def test_bad_environment_variable_falls_back(self, app, monkeypatch) -> None:
        from app.services import download_queue_worker as dq
        for bad in ('lots', '-5'):
            monkeypatch.setenv('MAX_DAILY_DOWNLOADS', bad)
            with app.app_context():
                assert dq.get_daily_cap() == dq.DEFAULT_MAX_DAILY_DOWNLOADS
        monkeypatch.setenv('MAX_DAILY_DOWNLOADS', '0')  # No limit, as in Settings.
        with app.app_context():
            assert dq.get_daily_cap() == 0

    def test_save_and_show_on_settings_page(self, client: FlaskClient) -> None:
        response = client.post('/settings/daily-downloads', json={'limit': '60'})
        assert response.status_code == 200
        assert response.get_json() == {'success': True, 'limit': 60}
        page = client.get('/settings/')
        assert b'downloadsSection' in page.data
        assert b'value="60"' in page.data

    @pytest.mark.parametrize('bad', [None, '', 'abc', '2.5', -1, 10001])
    def test_rejects_bad_values(self, client: FlaskClient, bad) -> None:
        response = client.post('/settings/daily-downloads', json={'limit': bad})
        assert response.status_code == 400
        assert response.get_json()['success'] is False

    def test_worker_uses_saved_limit(self, app, client: FlaskClient) -> None:
        from app.services.download_queue_worker import DownloadQueueWorker
        client.post('/settings/daily-downloads', json={'limit': 7})
        with app.app_context():
            assert DownloadQueueWorker(app)._get_daily_cap() == 7
