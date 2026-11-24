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
    def set_state(running=(), progress='', waiting=()):
        if isinstance(running, int):
            running = [running]
        with ai_cover._lock:
            ai_cover._queue.clear()
            ai_cover._queued_at.clear()
            ai_cover._active.clear()
            ai_cover._workers = 0
            for story_id in running:
                ai_cover._active[story_id] = {'progress': progress, 'started': ai_cover._now()}
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
        assert [r['story_id'] for r in overview['running']] == [stories[0]]
        assert overview['running'][0]['progress'] == 'Painting the cover'
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
        assert f'data-sort-id="{stories[1]}"' in html and f'cancelAiCover({stories[1]})' in html
        assert 'Queue Story 2' not in html  # A finished cover leaves the list.
        assert 'The image model refused.' in html  # A failed one stays for a day.
        assert '1 running · 1 waiting' in html
        page = client.get('/queue/').get_data(as_text=True)
        assert 'id="aiCoverQueue"' in page and 'Queue Story 0' in page

    def test_section_hidden_until_used(self, client: FlaskClient, covers, queue_state) -> None:
        queue_state()
        with patch('app.services.ai_cover.settings', return_value={'api_key': ''}):
            html = client.get('/queue/partials/ai-covers').get_data(as_text=True)
        assert 'AI Covers' not in html

    def test_nothing_shown_when_nothing_to_do(self, app: Flask, client: FlaskClient, stories, covers, queue_state) -> None:
        queue_state()
        _notes(app, stories[2], {'made': ai_cover._now(), 'cost': 0.042})
        _notes(app, stories[3], {'error': 'Refused long ago.', 'failed': '2026-01-01T00:00:00+00:00'})
        with patch('app.services.ai_cover.settings', return_value={'api_key': 'sk-test'}):
            html = client.get('/queue/partials/ai-covers').get_data(as_text=True)
        assert not html.strip()  # Finished covers and failures older than a day are not shown.

    def test_worker_records_times_and_failures(self, app: Flask, stories, covers, queue_state) -> None:
        queue_state()
        with patch('app.services.ai_cover.make_cover', side_effect=ValueError('No credit left.')), \
             patch('app.services.notifier.send_notification'):
            with ai_cover._lock:
                ai_cover._queue.append(stories[0])
                ai_cover._queued_at[stories[0]] = ai_cover._now()
                ai_cover._workers += 1
            ai_cover._work(app)
        assert stories[0] not in ai_cover._queued_at and not ai_cover._active and ai_cover._workers == 0
        with app.app_context():
            recent = ai_cover.recent_results()
        assert recent[0]['story_id'] == stories[0] and recent[0]['error'] == 'No credit left.'
        assert recent[0]['when']

    def test_status_updates_go_to_the_api_log(self) -> None:
        from app.services.logger import is_api_path
        assert is_api_path('/queue/partials/ai-covers') and is_api_path('/queue/partials/list')
        assert not is_api_path('/queue/')


@pytest.mark.integration
class TestSeveralAtOnce:

    def _run_batch(self, app: Flask, story_ids, limit):
        """Queue covers with make_cover replaced by one that waits until released, and record
        the most covers that were being made at the same moment."""
        import threading, time
        release = threading.Event()
        lock = threading.Lock()
        seen = {'now': 0, 'most': 0, 'done': 0}

        def fake_make_cover(story_id):
            ai_cover._set_progress('Painting the cover')
            with lock:
                seen['now'] += 1
                seen['most'] = max(seen['most'], seen['now'])
            release.wait(5)
            with lock:
                seen['now'] -= 1
                seen['done'] += 1

        with patch.object(ai_cover, 'make_cover', side_effect=fake_make_cover):
            with app.app_context():
                states = [ai_cover.start(app, i, limit=limit) for i in story_ids]
            deadline = time.time() + 5
            while time.time() < deadline and seen['now'] < min(limit, len(story_ids)):
                time.sleep(0.02)
            snapshot = {i: dict(a) for i, a in ai_cover._active.items()}
            waiting = list(ai_cover._queue)
            release.set()
            while time.time() < deadline and (ai_cover._workers or ai_cover._active):
                time.sleep(0.02)
        return states, seen, snapshot, waiting

    def test_makes_several_at_once(self, app: Flask, stories, covers, queue_state) -> None:
        queue_state()
        states, seen, snapshot, waiting = self._run_batch(app, stories, limit=3)
        assert states == ['started', 'started', 'started', 'queued']
        assert seen['most'] == 3 and seen['done'] == 4
        assert len(snapshot) == 3 and waiting == [stories[3]]
        # Each worker reports progress for its own story.
        assert all(a['progress'] == 'Painting the cover' for a in snapshot.values())
        assert ai_cover._workers == 0 and not ai_cover._active

    def test_one_at_a_time_when_set_to_1(self, app: Flask, stories, covers, queue_state) -> None:
        queue_state()
        states, seen, _, waiting = self._run_batch(app, stories[:3], limit=1)
        assert states == ['started', 'queued', 'queued']
        assert seen['most'] == 1 and seen['done'] == 3

    def test_same_story_twice_is_refused_while_running(self, app: Flask, stories, queue_state) -> None:
        queue_state(running=stories[0])
        with pytest.raises(ValueError):
            ai_cover.start(app, stories[0], limit=3)

    @pytest.mark.parametrize('value, ok', [(1, True), (5, True), ('3', True), (0, False), (6, False), ('lots', False)])
    def test_setting_is_checked(self, app: Flask, value, ok) -> None:
        from app.models import AppConfig, db
        with app.app_context():
            try:
                if ok:
                    assert ai_cover.save_settings({'parallel': value})['parallel'] == int(value)
                    assert ai_cover.parallel_limit() == int(value)
                else:
                    with pytest.raises(ValueError):
                        ai_cover.save_settings({'parallel': value})
            finally:
                AppConfig.query.filter_by(key=ai_cover.KEY).delete()
                db.session.commit()

    def test_default_is_three(self, app: Flask) -> None:
        with app.app_context():
            assert ai_cover.DEFAULTS['parallel'] == 3
            with patch.object(ai_cover, 'settings', return_value=dict(ai_cover.DEFAULTS)):
                assert ai_cover.parallel_limit() == 3

    def test_settings_page_has_the_choice(self, client: FlaskClient) -> None:
        page = client.get('/settings/').get_data(as_text=True)
        assert 'id="aiCoverParallel"' in page and '<option value="5"' in page


@pytest.mark.integration
class TestAutoCoverDefault:

    def test_on_by_default_and_old_saved_off_is_not_a_choice(self, app) -> None:
        from app.services import ai_cover
        from app.models import AppConfig, db
        with app.app_context():
            assert ai_cover.settings()['auto'] is True
            # Saved by an earlier build with the old default, never chosen: still on.
            config = AppConfig(key=ai_cover.KEY, value_type='json', description='')
            config.set_value({'api_key': 'sk-or-v1-test', 'auto': False})
            db.session.add(config)
            db.session.commit()
            assert ai_cover.settings()['auto'] is True
            # Turned off in Settings: stays off.
            ai_cover.save_settings({'auto': False})
            assert ai_cover.settings()['auto'] is False
            ai_cover.save_settings({'text_model': ai_cover.DEFAULT_TEXT_MODEL})
            assert ai_cover.settings()['auto'] is False

    def test_settings_page_label(self, client) -> None:
        assert b'Make an AI cover for each newly downloaded story' in client.get('/settings/').data


@pytest.mark.integration
class TestAiStatistics:

    def test_calls_are_recorded_and_shown(self, app, client) -> None:
        from app.services import ai_stats
        import urllib.error

        class Reply:
            def __init__(self, body): self.body = body
            def read(self): return json.dumps(self.body).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        replies = [Reply({'model': 'text/model', 'usage': {'cost': 0.0012}, 'choices': [{'message': {'content': 'x'}}]}),
                   Reply({'usage': {'cost': 0.04}, 'data': [{'b64_json': 'AAAA'}]}),
                   Reply({'usage': {'cost': 0}, 'data': []})]
        with app.app_context(), patch('urllib.request.urlopen', side_effect=replies):
            ai_cover._post(ai_cover.CHAT, {'model': 'text/model'}, 'sk-or-v1-test')
            ai_cover._post(ai_cover.IMAGES, {'model': 'image/model'}, 'sk-or-v1-test')
            ai_cover._post(ai_cover.IMAGES, {'model': 'image/model'}, 'sk-or-v1-test')
        with app.app_context(), patch('urllib.request.urlopen',
                                      side_effect=urllib.error.HTTPError('u', 402, 'no credit', {}, None)):
            with pytest.raises(Exception):
                ai_cover._post(ai_cover.IMAGES, {'model': 'image/model'}, 'sk-or-v1-test')
        with app.app_context():
            tables = ai_stats.tables()
        kinds = {k['label']: k['cost']['today'] for k in tables['kinds']}
        assert kinds['AI cover descriptions (text model)'] == pytest.approx(0.0012)
        assert kinds['AI cover pictures (image model)'] == pytest.approx(0.04)
        image = next(m for m in tables['models'] if m['model'] == 'image/model')
        assert image['ok']['all'] == 1 and image['failed']['all'] == 2
        assert image['success_rate'] == pytest.approx(1 / 3)
        page = client.get('/settings/').get_data(as_text=True)
        assert 'id="aiStats"' in page and 'AI model failures' in page and '$0.04' in page

    def test_built_once_from_finished_covers(self, app) -> None:
        from app.services import ai_stats
        from app.utils import paths
        folder = Path(paths.get_cover_directory()) / 'ai'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / '1_x.art.json').write_text(json.dumps({'made': '2026-11-01T10:00:00+00:00', 'cost': 0.05,
                                                         'text_model': 't/m', 'image_model': 'i/m'}))
        (folder / '2_y.art.json').write_text(json.dumps({'failed': '2026-11-02T10:00:00+00:00', 'error': 'refused'}))
        with app.app_context():
            assert ai_stats.backfill() == 2
            assert ai_stats.backfill() == 0
            models = {m['model']: m for m in ai_stats.tables()['models']}
        assert models['i/m']['cost']['all'] == pytest.approx(0.05) and models['t/m']['cost']['all'] == 0


@pytest.mark.integration
class TestCoversForBooksWithout:

    def test_count_and_queue_all_without_one(self, app, client, add_story) -> None:
        from app.utils import paths
        first, second = add_story('Has Cover'), add_story('Needs Cover')
        with app.app_context():
            from app.models import Story, db
            art = ai_cover.art_paths(db.session.get(Story, first))[0]
        Path(art).parent.mkdir(parents=True, exist_ok=True)
        Path(art).write_bytes(b'\xff\xd8\xff')
        assert client.get('/api/ai-covers/missing').get_json()['count'] == 1
        assert client.post('/api/ai-covers/missing').status_code == 400  # no API key yet
        with app.app_context():
            ai_cover.save_settings({'api_key': 'sk-or-v1-' + 'a' * 40})
        with patch.object(ai_cover, 'start') as start:
            data = client.post('/api/ai-covers/missing').get_json()
        assert data == {'success': True, 'queued': 1}
        assert start.call_args.args[1] == second
        assert 'id="aiCoverMissingBtn"' in client.get('/settings/').get_data(as_text=True)
