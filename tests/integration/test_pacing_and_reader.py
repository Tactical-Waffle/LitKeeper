"""Download pacing (a varied wait between downloads), retries that wait longer each time, and the
reader's Mark as Read with the Settings → Reading option."""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.services import download_queue_worker as dq

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]

KEYS = (dq.DOWNLOAD_DELAY_KEY, dq.NEXT_DOWNLOAD_KEY, 'auto_mark_read')


@pytest.fixture(autouse=True)
def clean(app: Flask):
    """The test database is shared: remove what this file adds."""
    from app.models import AppConfig, DownloadQueueItem, db
    marker = f'https://www.literotica.com/s/pacing-{uuid.uuid4().hex[:8]}'

    def clear():
        with app.app_context():
            AppConfig.query.filter(AppConfig.key.in_(KEYS)).delete(synchronize_session=False)
            DownloadQueueItem.query.filter(DownloadQueueItem.url.like(marker + '%')).delete(synchronize_session=False)
            db.session.commit()

    clear()
    yield marker
    clear()


@pytest.mark.unit
class TestVariedWait:

    def test_stays_within_a_quarter(self) -> None:
        values = [dq.vary(10) for _ in range(2000)]
        assert min(values) >= 7.5 and max(values) <= 12.5
        assert max(values) - min(values) > 3  # It really does vary.

    def test_retry_waits_grow(self) -> None:
        with patch.object(dq.random, 'uniform', return_value=1.0):
            waits = [dq.retry_wait(n).total_seconds() / 60 for n in range(1, 7)]
        assert waits == [5, 15, 45, 120, 360, 360]
        assert dq.MAX_ATTEMPTS == 6


@pytest.mark.integration
class TestDelaySetting:

    def test_default_save_and_show(self, app: Flask, client: FlaskClient) -> None:
        with app.app_context():
            assert dq.get_download_delay() == 10
        response = client.post('/settings/download-delay', json={'minutes': '20'})
        assert response.get_json() == {'success': True, 'minutes': 20}
        page = client.get('/settings/').get_data(as_text=True)
        assert 'id="downloadDelay"' in page and 'value="20"' in page

    @pytest.mark.parametrize('bad', [None, '', 'soon', '2.5', -1, 721])
    def test_rejects_bad_values(self, client: FlaskClient, bad) -> None:
        assert client.post('/settings/download-delay', json={'minutes': bad}).status_code == 400

    def test_shorter_wait_replaces_planned_one(self, app: Flask, client: FlaskClient) -> None:
        with app.app_context():
            dq._store_next_download_at(datetime.utcnow() + timedelta(hours=3))
        client.post('/settings/download-delay', json={'minutes': 0})
        with app.app_context():
            assert dq.next_download_at() is None


def _worker(app: Flask):
    worker = dq.DownloadQueueWorker(app)
    worker._get_daily_cap = lambda: 10 ** 6
    worker._reset_rate_limited_items = lambda: None
    return worker


def _add_item(app: Flask, url: str):
    from app.models import DownloadQueueItem, db
    with app.app_context():
        # Oldest of all, so the worker takes it before anything else in the shared database.
        item = DownloadQueueItem(url=url, status='pending', job_type='single', formats='["epub"]', created_at=datetime(2000, 1, 1))
        db.session.add(item)
        db.session.commit()
        return item.id


@pytest.mark.integration
class TestWorkerPacing:

    def test_success_plans_a_varied_wait_and_holds_the_next(self, app: Flask, clean) -> None:
        item_id = _add_item(app, clean + '-ok')
        worker = _worker(app)
        with app.app_context(), patch.object(worker, '_download_and_save', return_value=True), \
             patch('app.services.notifier.send_notification'), patch('app.services.ai_cover.make_after_download'), \
             patch.object(dq.global_rate_limiter, 'wait_if_needed'):
            worker._process_next_item()
            planned = dq.next_download_at()
            from app.models import DownloadQueueItem
            assert DownloadQueueItem.query.get(item_id).status == 'completed'
        minutes = (planned - datetime.utcnow()).total_seconds() / 60
        assert 7.4 <= minutes <= 12.5

        second = _add_item(app, clean + '-next')
        with app.app_context(), patch.object(worker, '_download_and_save') as download:
            worker._process_next_item()
            download.assert_not_called()  # Still waiting.
            from app.models import DownloadQueueItem
            assert DownloadQueueItem.query.get(second).status == 'pending'
        assert 1 <= worker._seconds_until_next_check() <= worker.poll_interval

    def test_skipped_story_does_not_wait(self, app: Flask, clean) -> None:
        _add_item(app, clean + '-seen')
        worker = _worker(app)
        with app.app_context(), patch.object(worker, '_download_and_save', return_value=False), \
             patch.object(dq.global_rate_limiter, 'wait_if_needed'):
            worker._process_next_item()
            assert dq.next_download_at() is None

    def test_failures_back_off(self, app: Flask, clean) -> None:
        from app.models import DownloadQueueItem, db
        item_id = _add_item(app, clean + '-cf')
        worker = _worker(app)
        with app.app_context(), patch.object(worker, '_download_and_save', side_effect=Exception('Cloudflare check')), \
             patch.object(dq.global_rate_limiter, 'wait_if_needed'), patch.object(dq.random, 'uniform', return_value=1.0):
            worker._process_next_item()
            item = db.session.get(DownloadQueueItem, item_id)
            assert item.status == 'pending' and item.retry_count == 1
            assert 'Attempt 1 of 6' in item.error_message
            assert 4.9 <= (item.scheduled_after - datetime.utcnow()).total_seconds() / 60 <= 5.1
            # One failure doubles the usual wait before the next story.
            assert 19.9 <= (dq.next_download_at() - datetime.utcnow()).total_seconds() / 60 <= 20.1

            for expected in (15, 45, 120, 360):
                item.scheduled_after = None
                dq._store_next_download_at(None)
                worker._process_next_item()
                item = db.session.get(DownloadQueueItem, item_id)
                assert round((item.scheduled_after - datetime.utcnow()).total_seconds() / 60) == expected
            # The gap before other stories stops growing at the cap.
            assert round((dq.next_download_at() - datetime.utcnow()).total_seconds() / 60) == dq.FAILURE_GAP_CAP_MINUTES

            item.scheduled_after = None
            dq._store_next_download_at(None)
            worker._process_next_item()
            item = db.session.get(DownloadQueueItem, item_id)
            assert item.status == 'failed' and item.retry_count == 6

    def test_queue_page_shows_waits(self, app: Flask, client: FlaskClient, clean) -> None:
        from app.models import DownloadQueueItem, db
        with app.app_context():
            db.session.add(DownloadQueueItem(url=clean + '-retry', status='pending', job_type='single', formats='["epub"]', retry_count=2,
                                             error_message='Attempt 2 of 6 failed: blocked',
                                             scheduled_after=datetime.utcnow() + timedelta(minutes=44, seconds=50)))
            db.session.commit()
            dq._store_next_download_at(datetime.utcnow() + timedelta(minutes=8, seconds=50))
        page = client.get('/queue/partials/list').get_data(as_text=True)
        assert 'Next download in about 9 minutes' in page
        assert 'Trying again in about 45 minutes' in page and 'Attempt 2 of 6 failed: blocked' in page


@pytest.fixture
def story(app: Flask):
    from app.models import Story, ReadingProgress, db
    with app.app_context():
        story = Story.query.first()
        if not story:
            pytest.skip('No story in the test database')
        progress = ReadingProgress.query.filter_by(story_id=story.id).first()
        before = (progress.is_completed, progress.last_read_at) if progress else None
        story_id, in_queue = story.id, story.in_queue
    yield story_id
    with app.app_context():
        progress = ReadingProgress.query.filter_by(story_id=story_id).first()
        if before is None and progress:
            db.session.delete(progress)
        elif progress:
            progress.is_completed, progress.last_read_at = before
        db.session.get(Story, story_id).in_queue = in_queue
        db.session.commit()


@pytest.mark.integration
class TestMarkAsRead:

    def test_mark_read_and_unread(self, app: Flask, client: FlaskClient, story) -> None:
        from app.models import ReadingProgress, Story, db
        with app.app_context():
            db.session.get(Story, story).in_queue = True
            db.session.commit()
        data = client.post(f'/api/story/{story}/read', json={'read': True}).get_json()
        assert data['success'] and data['read'] is True and data['in_queue'] is False
        with app.app_context():
            assert ReadingProgress.query.filter_by(story_id=story).first().is_completed
        assert client.post(f'/api/story/{story}/read', json={'read': False}).get_json()['read'] is False
        with app.app_context():
            assert not ReadingProgress.query.filter_by(story_id=story).first().is_completed
        assert client.post(f'/api/story/{story}/read', json={'read': 'yes'}).status_code == 400
        assert client.post('/api/story/99999999/read', json={'read': True}).status_code == 404

    def test_reader_has_button_and_compact_top(self, client: FlaskClient, story) -> None:
        response = client.get(f'/read/{story}')
        if response.status_code == 404:
            pytest.skip('Story file missing in the test library')
        page = response.get_data(as_text=True)
        assert 'id="mark-read-btn"' in page and 'class="story-byline"' in page
        assert 'class="story-header"' not in page

    def test_reading_setting(self, app: Flask, client: FlaskClient) -> None:
        from app.services.reading_settings import auto_mark_read
        with app.app_context():
            assert auto_mark_read() is False
        assert client.post('/settings/reading', json={'auto_mark_read': True}).get_json() == {'success': True, 'auto_mark_read': True}
        with app.app_context():
            assert auto_mark_read() is True
        assert 'id="autoMarkRead"' in client.get('/settings/').get_data(as_text=True)
        assert client.post('/settings/reading', json={'auto_mark_read': 'on'}).status_code == 400
