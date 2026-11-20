"""The AI cover queue on the Queue page: what's being made, what's waiting, recent results, cancelling."""
from __future__ import annotations
import json
import uuid
import pytest
from pathlib import Path
from unittest.mock import patch
from flask import Flask
from flask.testing import FlaskClient

from app.services import ai_cover

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]


@pytest.fixture
def covers(monkeypatch: pytest.MonkeyPatch, temp_dir: Path) -> Path:
    folder = temp_dir / 'queue-covers'
    folder.mkdir()
    import app.utils
    monkeypatch.setattr(app.utils, 'get_cover_directory', lambda: str(folder))
    return folder


@pytest.fixture
def stories(app: Flask):
    from app.models import Author, Story, db
    unique = uuid.uuid4().hex[:8]
    with app.app_context():
        author = Author(name=f'Queue Author {unique}')
        db.session.add(author)
        db.session.flush()
        records = [Story(title=f'Queue Story {n}', author_id=author.id, filename_base=f'queue-{unique}-{n}',
                         source_type='custom') for n in range(4)]
        db.session.add_all(records)
        db.session.commit()
        ids, author_id = [r.id for r in records], author.id
    yield ids
    with app.app_context():
        for story_id in ids:
            record = db.session.get(Story, story_id)
            if record:
                db.session.delete(record)
        db.session.commit()
        db.session.delete(db.session.get(Author, author_id))
        db.session.commit()


@pytest.fixture
def queue_state():
    """Set the in-memory queue by hand (no worker thread), and empty it again afterwards."""
    def set_state(running=None, progress='', waiting=()):
        with ai_cover._lock:
            ai_cover._queue.clear()
            ai_cover._queued_at.clear()
            ai_cover._running.clear()
            if running is not None:
                ai_cover._running.update({'story_id': running, 'progress': progress, 'started': ai_cover._now()})
            for story_id in waiting:
                ai_cover._queue.append(story_id)
                ai_cover._queued_at[story_id] = ai_cover._now()
    yield set_state
    set_state()


def _notes(app: Flask, story_id: int, data: dict) -> None:
    from app.models import Story, db
    with app.app_context():
        story = db.session.get(Story, story_id)
        ai_cover._write_notes(story, data)


@pytest.mark.integration
class TestAiCoverQueue:

    def test_overview_shows_running_and_waiting_in_order(self, app: Flask, stories, covers, queue_state) -> None:
        queue_state(running=stories[0], progress='Painting the cover', waiting=[stories[2], stories[1]])
        with app.app_context():
            overview = ai_cover.queue_overview()
        assert overview['running']['story_id'] == stories[0]
        assert overview['running']['progress'] == 'Painting the cover'
        assert [w['story_id'] for w in overview['waiting']] == [stories[2], stories[1]]

    def test_cancel_waiting_cover(self, client: FlaskClient, stories, covers, queue_state) -> None:
        queue_state(running=stories[0], waiting=[stories[1], stories[2]])
        assert client.delete(f'/queue/api/ai-covers/{stories[1]}').get_json() == {'success': True}
        assert list(ai_cover._queue) == [stories[2]]
        assert stories[1] not in ai_cover._queued_at
        # The one being made can't be cancelled, and nor can one that isn't queued.
        assert client.delete(f'/queue/api/ai-covers/{stories[0]}').status_code == 409
        assert client.delete(f'/queue/api/ai-covers/{stories[3]}').status_code == 409

    def test_recent_results_from_notes_newest_first(self, app: Flask, stories, covers, queue_state) -> None:
        _notes(app, stories[0], {'made': '2026-11-18T10:00:00+00:00', 'cost': 0.042, 'image_model': 'a/b'})
        _notes(app, stories[1], {'error': 'OpenRouter refused the API key.', 'failed': '2026-11-19T09:00:00+00:00'})
        _notes(app, stories[2], {'made': '2026-11-17T10:00:00+00:00', 'cost': 0.03})
        with app.app_context():
            recent = ai_cover.recent_results()
        assert [r['story_id'] for r in recent] == [stories[1], stories[0], stories[2]]
        assert recent[0]['ok'] is False and 'refused' in recent[0]['error']
        assert recent[1]['ok'] is True and recent[1]['cost'] == 0.042

    def test_queue_page_section(self, app: Flask, client: FlaskClient, stories, covers, queue_state) -> None:
        queue_state(running=stories[0], progress='Describing the cover', waiting=[stories[1]])
        _notes(app, stories[2], {'made': ai_cover._now(), 'cost': 0.042})
        _notes(app, stories[3], {'error': 'The image model refused.', 'failed': ai_cover._now()})
        html = client.get('/queue/partials/ai-covers').get_data(as_text=True)
        assert 'Queue Story 0' in html and 'Describing the cover' in html
        assert 'data-ai-cover-state="waiting"' in html and f'cancelAiCover({stories[1]})' in html
        assert 'Done · $0.042 · just now' in html
        assert 'The image model refused.' in html
        assert '1 in progress · 1 waiting' in html
        page = client.get('/queue/').get_data(as_text=True)
        assert 'id="aiCoverQueue"' in page and 'Queue Story 0' in page

    def test_section_hidden_until_used(self, client: FlaskClient, covers, queue_state) -> None:
        queue_state()
        with patch('app.services.ai_cover.settings', return_value={'api_key': ''}):
            html = client.get('/queue/partials/ai-covers').get_data(as_text=True)
        assert 'AI Covers' not in html

    def test_empty_but_set_up(self, client: FlaskClient, covers, queue_state) -> None:
        queue_state()
        with patch('app.services.ai_cover.settings', return_value={'api_key': 'sk-test'}):
            html = client.get('/queue/partials/ai-covers').get_data(as_text=True)
        assert 'No AI covers yet' in html

    def test_worker_records_times_and_failures(self, app: Flask, stories, covers, queue_state) -> None:
        queue_state()
        with patch('app.services.ai_cover.make_cover', side_effect=ValueError('No credit left.')), \
             patch('app.services.notifier.send_notification'):
            with ai_cover._lock:
                ai_cover._queue.append(stories[0])
                ai_cover._queued_at[stories[0]] = ai_cover._now()
                ai_cover._running['working'] = True
            ai_cover._work(app)
        assert stories[0] not in ai_cover._queued_at and 'started' not in ai_cover._running
        with app.app_context():
            recent = ai_cover.recent_results()
        assert recent[0]['story_id'] == stories[0] and recent[0]['error'] == 'No credit left.'
        assert recent[0]['when']

    def test_status_updates_go_to_the_api_log(self) -> None:
        from app.services.logger import is_api_path
        assert is_api_path('/queue/partials/ai-covers') and is_api_path('/queue/partials/list')
        assert not is_api_path('/queue/')
