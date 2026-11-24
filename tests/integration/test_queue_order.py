"""The Queue page: AI Rewrite and summaries listed with AI covers, the download list in the order it runs,
drag-and-drop ordering of waiting downloads and AI work, and the AI cover queue saved across restarts."""
from __future__ import annotations
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.services import ai_cover, ai_queues, ai_rewrite, ai_summary

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]


@pytest.fixture
def stories(add_story):
    return [add_story(f'Order Story {n}') for n in range(4)]


@pytest.fixture(autouse=True)
def empty_ai_queues():
    def clear():
        for service in (ai_rewrite, ai_summary):
            with service._lock:
                service._queue.clear()
                service._running.clear()
        with ai_cover._lock:
            ai_cover._queue.clear()
            ai_cover._queued_at.clear()
            ai_cover._active.clear()
            ai_cover._workers = 0
    clear()
    yield
    clear()


def _downloads(app: Flask, statuses):
    from app.models import DownloadQueueItem, db
    start = datetime(2026, 11, 24, 10, 0, 0)
    ids = []
    with app.app_context():
        for n, status in enumerate(statuses):
            item = DownloadQueueItem(url=f'https://www.literotica.com/s/order-{n}', status=status, formats='["epub"]',
                                     title=f'Download {n}', created_at=start + timedelta(minutes=n))
            db.session.add(item)
            db.session.flush()
            ids.append(item.id)
        db.session.commit()
    return ids


@pytest.mark.integration
class TestAiWorkInTheQueue:

    def test_rewrites_and_summaries_are_listed_with_covers(self, client: FlaskClient, stories) -> None:
        ai_rewrite._running.update(story_id=stories[0], index=2, total=5, number='2', part_started=0)
        ai_rewrite._queue.extend([stories[1], stories[2]])
        ai_summary._running['story_id'] = stories[3]
        ai_cover._queue.append(stories[1])
        html = client.get('/queue/partials/ai-covers').get_data(as_text=True)
        assert 'Order Story 0' in html and 'Part 2 (2 of 5)' in html
        assert 'data-sortable="rewrite"' in html and 'data-sortable="cover"' in html
        assert "cancelAiJob(&#39;rewrite&#39;, %d)" % stories[2] in html  # Quotes escaped in the attribute.
        assert '>Summary<' in html and 'Order Story 3' in html
        assert '2 running · 3 waiting · runs beside downloads' in html
        page = client.get('/queue/').get_data(as_text=True)
        assert page.index('id="aiCoverQueue"') < page.index('id="queueList"')  # AI work is listed above downloads.

    def test_take_a_waiting_rewrite_out(self, client: FlaskClient, stories) -> None:
        ai_rewrite._queue.append(stories[1])
        assert client.delete(f'/queue/api/ai/rewrite/{stories[1]}').get_json()['success']
        assert stories[1] not in ai_rewrite._queue
        assert client.delete(f'/queue/api/ai/rewrite/{stories[1]}').status_code == 409

    def test_reorder_ai_work(self, client: FlaskClient, stories) -> None:
        ai_summary._queue.extend(stories[:3])
        ai_cover._queue.extend(stories[:3])
        assert client.post('/queue/api/order', json={'kind': 'summary', 'ids': [stories[2], stories[0]]}).get_json()['success']
        assert list(ai_summary._queue) == [stories[2], stories[0], stories[1]]
        client.post('/queue/api/order', json={'kind': 'cover', 'ids': [stories[1], 999]})
        assert list(ai_cover._queue) == [stories[1], stories[0], stories[2]]
        assert [i[0] for i in ai_queues.load('cover')] == [stories[1], stories[0], stories[2]]  # Saved at once.
        assert client.post('/queue/api/order', json={'kind': 'nope', 'ids': []}).status_code == 400


@pytest.mark.integration
class TestDownloadOrder:

    def test_list_runs_in_order_with_handles_on_waiting_rows(self, app: Flask, client: FlaskClient) -> None:
        done, first, second, active = _downloads(app, ['completed', 'pending', 'pending', 'processing'])
        html = client.get('/queue/partials/list').get_data(as_text=True)
        assert html.index('Download 3') < html.index('Download 1') < html.index('Download 2') < html.index('Download 0')
        assert html.count('data-drag-handle') == 2 and f'data-sort-id="{first}"' in html

    def test_reorder_changes_what_downloads_next(self, app: Flask, client: FlaskClient) -> None:
        a, b, c = _downloads(app, ['pending', 'pending', 'pending'])
        assert client.post('/queue/api/order', json={'kind': 'download', 'ids': [c, a, b]}).get_json()['success']
        from app.models import DownloadQueueItem
        with app.app_context():
            order = [i.id for i in DownloadQueueItem.query.order_by(DownloadQueueItem.created_at.asc()).all()]
        assert order == [c, a, b]
        html = client.get('/queue/partials/list').get_data(as_text=True)
        assert html.index('Download 2') < html.index('Download 0') < html.index('Download 1')


@pytest.mark.integration
class TestCoverQueueSurvivesRestart:

    def test_saved_and_restored(self, app: Flask, stories) -> None:
        with patch('app.services.ai_cover.make_cover'), patch.object(ai_cover, '_work'):
            with app.app_context():
                ai_cover.start(app, stories[0], limit=1)
                ai_cover.start(app, stories[1], limit=1)
        assert [i[0] for i in ai_queues.load('cover')] == [stories[0], stories[1]]
        ai_queues.save('cover', [[stories[1], 'cover'], [999999, 'cover']])
        with patch('app.services.ai_cover.start') as start:
            ai_cover.restore(app)
        assert [c.args[1] for c in start.call_args_list] == [stories[1]]  # Deleted stories are dropped.
