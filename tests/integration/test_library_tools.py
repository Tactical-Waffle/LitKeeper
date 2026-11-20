"""Picking several stories in the library, the separate API log, the header theme button and square covers."""
from __future__ import annotations
import io
import logging
import uuid
import zipfile
import pytest
from pathlib import Path
from unittest.mock import patch
from flask import Flask
from flask.testing import FlaskClient
from PIL import Image

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]


@pytest.fixture
def covers(monkeypatch: pytest.MonkeyPatch, temp_dir: Path) -> Path:
    folder = temp_dir / 'bulk-covers'
    folder.mkdir()
    import importlib
    api_routes = importlib.import_module('app.blueprints.api.routes')
    monkeypatch.setattr(api_routes, 'get_cover_directory', lambda: str(folder))
    return folder


@pytest.fixture
def stories(app: Flask, temp_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Three stories, each with an EPUB file, removed again afterwards."""
    from app.models import Author, Story, StoryFormat, db
    import importlib
    download_routes = importlib.import_module('app.blueprints.downloads.routes')
    epub_dir = temp_dir / 'bulk-epubs'
    epub_dir.mkdir()
    monkeypatch.setattr(download_routes, 'get_epub_directory', lambda: str(epub_dir))
    unique = uuid.uuid4().hex[:8]
    with app.app_context():
        author = Author(name=f'Bulk Author {unique}')
        db.session.add(author)
        db.session.flush()
        ids = []
        for n in range(3):
            record = Story(title=f'Bulk Story {n}', author_id=author.id, filename_base=f'bulk-{unique}-{n}',
                           source_type='custom', auto_update_enabled=False)
            db.session.add(record)
            db.session.flush()
            epub = epub_dir / f'{record.id}_bulk-{unique}-{n}.epub'
            epub.write_bytes(b'epub ' + str(n).encode())
            db.session.add(StoryFormat(story_id=record.id, format_type='epub', file_path=str(epub)))
            ids.append(record.id)
        db.session.commit()
        author_id = author.id
    yield ids
    with app.app_context():
        for story_id in ids:
            record = db.session.get(Story, story_id)
            if record:
                db.session.delete(record)
        db.session.commit()
        author = db.session.get(Author, author_id)
        if author:
            db.session.delete(author)
        db.session.commit()


@pytest.mark.integration
class TestBulkActions:

    def test_auto_update_on_and_off(self, app: Flask, client: FlaskClient, stories) -> None:
        from app.models import Story, db
        response = client.post('/api/stories/bulk', json={'action': 'auto_update_on', 'ids': stories[:2]})
        assert response.get_json() == {'success': True, 'done': 2, 'failed': 0, 'action': 'auto_update_on'}
        with app.app_context():
            assert [db.session.get(Story, i).auto_update_enabled for i in stories] == [True, True, False]
        client.post('/api/stories/bulk', json={'action': 'auto_update_off', 'ids': stories})
        with app.app_context():
            assert not any(db.session.get(Story, i).auto_update_enabled for i in stories)

    def test_delete(self, app: Flask, client: FlaskClient, stories) -> None:
        from app.models import Story, db
        response = client.post('/api/stories/bulk', json={'action': 'delete', 'ids': stories[:2]})
        assert response.get_json()['done'] == 2
        with app.app_context():
            assert db.session.get(Story, stories[0]) is None
            assert db.session.get(Story, stories[2]) is not None

    def test_regenerate_covers(self, app: Flask, client: FlaskClient, stories, covers: Path) -> None:
        with patch('app.services.epub_service.EpubService.update_epub_cover', return_value=True):
            response = client.post('/api/stories/bulk', json={'action': 'regenerate_covers', 'ids': stories})
        assert response.get_json()['done'] == 3
        assert len(list(covers.glob('*.jpg'))) == 3

    def test_missing_story_counts_as_failed(self, client: FlaskClient, stories) -> None:
        response = client.post('/api/stories/bulk', json={'action': 'auto_update_on', 'ids': [stories[0], 999999999]})
        data = response.get_json()
        assert (data['done'], data['failed'], data['success']) == (1, 1, False)

    def test_ai_covers_need_a_key(self, client: FlaskClient, stories) -> None:
        with patch('app.services.ai_cover.settings', return_value={'api_key': ''}):
            response = client.post('/api/stories/bulk', json={'action': 'ai_covers', 'ids': stories})
        assert response.status_code == 400

    def test_ai_covers_are_queued(self, client: FlaskClient, stories) -> None:
        with patch('app.services.ai_cover.settings', return_value={'api_key': 'sk-test'}), \
             patch('app.services.ai_cover.start', return_value='queued') as start:
            response = client.post('/api/stories/bulk', json={'action': 'ai_covers', 'ids': stories})
        assert response.get_json()['done'] == 3
        assert [c.args[1] for c in start.call_args_list] == stories

    @pytest.mark.parametrize('payload', [
        {'action': 'explode', 'ids': [1]},
        {'action': 'delete', 'ids': []},
        {'action': 'delete', 'ids': ['x']},
        {'action': 'delete'},
    ])
    def test_rejects_bad_requests(self, client: FlaskClient, payload) -> None:
        assert client.post('/api/stories/bulk', json=payload).status_code == 400

    def test_download_selected(self, client: FlaskClient, stories) -> None:
        response = client.get(f'/download/export/selected?ids={stories[0]},{stories[2]}')
        assert response.status_code == 200
        assert 'litkeeper-selected.zip' in response.headers['Content-Disposition']
        names = zipfile.ZipFile(io.BytesIO(response.data)).namelist()
        assert len(names) == 2
        assert any('Bulk Story 0' in n for n in names) and any('Bulk Story 2' in n for n in names)

    @pytest.mark.parametrize('query', ['', '?ids=', '?ids=a,b'])
    def test_download_selected_needs_ids(self, client: FlaskClient, query) -> None:
        assert client.get(f'/download/export/selected{query}').status_code == 400

    def test_library_page_has_select_and_size_controls(self, client: FlaskClient, stories) -> None:
        page = client.get('/').data
        assert b'id="selectModeBtn"' in page
        assert b'id="selectionBar"' in page
        assert b'data-cover-size="xlarge"' in page
        assert b'class="story-select-mark"' in page


@pytest.mark.integration
class TestLogging:

    def _read(self, name: str) -> str:
        from app.services.logger import log_file_path
        for handler in logging.getLogger('litkeeper.api').handlers + logging.getLogger('litkeeper.request').handlers:
            handler.flush()
        path = Path(log_file_path(name))
        return path.read_text() if path.exists() else ''

    def test_api_calls_go_to_their_own_file(self, client: FlaskClient) -> None:
        client.get('/api/does-not-exist-for-log-test')
        client.get('/settings/')
        api_log, main_log = self._read('api.log'), self._read('litkeeper.log')
        assert 'GET /api/does-not-exist-for-log-test 404' in api_log
        assert '/api/does-not-exist-for-log-test' not in main_log
        assert 'GET /settings/ 200' in main_log
        assert 'GET /settings/' not in api_log

    def test_api_log_stays_off_the_console(self, client: FlaskClient, capsys) -> None:
        client.get('/api/does-not-exist-for-console-test')
        assert 'does-not-exist-for-console-test' not in capsys.readouterr().out

    def test_outgoing_openrouter_calls_are_logged_without_the_key(self, app: Flask) -> None:
        from app.services import ai_cover

        class Reply:
            status = 200
            def read(self): return b'{"data": {}}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch('urllib.request.urlopen', return_value=Reply()):
            ai_cover._get('key', 'sk-or-v1-secret-for-log-test')
        api_log = self._read('api.log')
        assert '-> OpenRouter GET key 200' in api_log
        assert 'secret-for-log-test' not in api_log

    def test_download_log_files(self, client: FlaskClient) -> None:
        client.get('/api/does-not-exist-for-download-test')
        response = client.get('/settings/logs/api.log')
        assert response.status_code == 200
        assert b'does-not-exist-for-download-test' in response.data
        assert client.get('/settings/logs/litkeeper.log').status_code == 200

    def test_view_last_lines(self, client: FlaskClient) -> None:
        from app.services.logger import log_file_path
        Path(log_file_path('api.log')).write_text(''.join(f'line {n}\n' for n in range(1, 1201)))
        data = client.get('/settings/logs/api.log/tail').get_json()
        assert data['count'] == 500
        assert data['lines'][0] == 'line 701' and data['lines'][-1] == 'line 1200'
        assert client.get('/settings/logs/api.log/tail?lines=3').get_json()['lines'] == ['line 1198', 'line 1199', 'line 1200']

    def test_view_fills_from_previous_days(self, client: FlaskClient) -> None:
        from app.services.logger import log_file_path
        path = Path(log_file_path('api.log'))
        Path(str(path) + '.2026-11-17').write_text(''.join(f'day17 {n}\n' for n in range(1, 4)))
        Path(str(path) + '.2026-11-18').write_text('day18 1\nday18 2\n')
        path.write_text('today 1\n')
        lines = client.get('/settings/logs/api.log/tail?lines=5').get_json()['lines']
        assert lines == ['day17 2', 'day17 3', 'day18 1', 'day18 2', 'today 1']

    def test_logs_rotate_daily_and_keep_retention_days(self, app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
        import logging.handlers
        from app.services.logger import setup_file_logging, log_file_path
        monkeypatch.setenv('LOG_RETENTION_DAYS', '3')
        setup_file_logging()
        handler = next(h for h in logging.getLogger('litkeeper.api').handlers if getattr(h, '_litkeeper_file', False))
        assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
        assert handler.when == 'MIDNIGHT' and handler.backupCount == 3
        # Old days beyond the limit are removed when a new day's file starts.
        base = log_file_path('api.log')
        for day in range(10, 16):
            Path(f'{base}.2026-11-{day}').write_text('old\n')
        logging.getLogger('litkeeper.api').info('before midnight')
        handler.doRollover()
        kept = sorted(p.name for p in Path(base).parent.glob('api.log.*'))
        assert len(kept) == 3
        assert 'api.log.2026-11-10' not in kept

    @pytest.mark.parametrize('value, expected', [(None, 14), ('30', 30), ('0', 14), ('-2', 14), ('lots', 14)])
    def test_retention_days_setting(self, monkeypatch: pytest.MonkeyPatch, value, expected) -> None:
        from app.services.logger import log_retention_days
        if value is None:
            monkeypatch.delenv('LOG_RETENTION_DAYS', raising=False)
        else:
            monkeypatch.setenv('LOG_RETENTION_DAYS', value)
        assert log_retention_days() == expected

    def test_old_size_based_files_are_cleared(self, app: Flask) -> None:
        from app.services.logger import setup_file_logging, log_file_path
        old = Path(log_file_path('api.log') + '.1')
        old.write_text('from the 5 MB days\n')
        setup_file_logging()
        assert not old.exists()

    def test_view_empty_and_unknown_logs(self, client: FlaskClient) -> None:
        from app.services.logger import log_file_path
        Path(log_file_path('api.log')).unlink(missing_ok=True)
        assert client.get('/settings/logs/api.log/tail?lines=abc').get_json()['count'] == 0
        assert client.get('/settings/logs/secret.key/tail').status_code == 404

    @pytest.mark.parametrize('name', ['secret.key', '..%2Flitkeeper.db', 'bulk_generation.log'])
    def test_only_log_files_can_be_downloaded(self, client: FlaskClient, name) -> None:
        assert client.get(f'/settings/logs/{name}').status_code == 404

    def test_settings_lists_logs_and_has_no_theme_section(self, client: FlaskClient) -> None:
        page = client.get('/settings/').data
        assert b'id="logsSection"' in page
        assert b'id="logLines"' in page
        assert b'keeps the last 14 days' in page
        assert b'API Log' in page
        assert b'data-theme-option' not in page


@pytest.mark.integration
class TestThemeButtonAndCovers:

    def test_header_has_theme_button(self, client: FlaskClient) -> None:
        page = client.get('/settings/').data
        assert page.count(b'onclick="toggleTheme()"') == 2  # desktop top bar and phone corner button

    def test_theme_choice_is_still_saved(self, client: FlaskClient) -> None:
        assert client.post('/settings/theme-preference', json={'theme': 'dark'}).get_json()['success']
        assert client.get('/settings/theme-preference').get_json()['theme'] == 'dark'
        client.post('/settings/theme-preference', json={'theme': 'system'})

    def test_covers_have_square_corners(self, tmp_path: Path) -> None:
        from app.services.cover_generator import generate_cover_image
        path = tmp_path / 'cover.jpg'
        generate_cover_image('Square Corners', 'Someone', str(path))
        image = Image.open(path).convert('RGB')
        w, h = image.size
        for corner in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
            assert min(image.getpixel(corner)) < 230, f'corner {corner} is white'
