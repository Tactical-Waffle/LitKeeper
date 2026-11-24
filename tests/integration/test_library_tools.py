"""Picking several stories in the library, the one combined log, the header theme button and square covers."""
from __future__ import annotations
import re
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
        assert response.get_json() == {'success': True, 'done': 2, 'failed': 0, 'skipped': 0, 'action': 'auto_update_on'}
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
        for handler in logging.getLogger('litkeeper.api').handlers + logging.getLogger('litkeeper.request').handlers + logging.getLogger('litkeeper.error').handlers:
            handler.flush()
        path = Path(log_file_path(name))
        return path.read_text() if path.exists() else ''

    def test_one_log_with_changes_failures_and_slow_pages_only(self, client: FlaskClient) -> None:
        """As in RedKeeper: page loads and the browser's checks are left out; changes are logged."""
        client.get('/api/does-not-exist-for-log-test')
        client.get('/settings/')
        client.get('/queue/partials/stats')
        client.post('/api/story/99999998/queue', json={'in_queue': True})
        log = self._read('litkeeper.log')
        assert '/api/does-not-exist-for-log-test' not in log and 'GET /settings/' not in log
        assert '/queue/partials/' not in log
        assert re.search(r'INFO #\d+ POST /api/story/99999998/queue → 404 in', log)
        assert not Path(self._log_path('api.log')).exists() or 'POST' not in self._read('api.log')

    def test_failed_and_slow_requests_are_logged(self, app: Flask) -> None:
        from app.services.logger import log_request
        log_request('GET', '/library/', 500, 12, None)
        log_request('GET', '/library/', 200, 2500, None)
        log_request('POST', '/epub/api/progress/3', 200, 5, None)
        log = self._read('litkeeper.log')
        assert 'ERROR GET /library/ → 500 in 12 ms' in log
        assert 'WARNING Slow request: GET /library/ → 200 in 2500 ms' in log
        assert '/epub/api/progress/' not in log  # Reading-position saves would drown everything else.

    def _log_path(self, name: str) -> str:
        from app.services.logger import log_file_path
        return log_file_path(name)

    def test_api_log_stays_off_the_console(self, client: FlaskClient, capsys) -> None:
        client.post('/api/story/99999997/queue', json={'in_queue': True, 'note': 'data-for-console-test'})
        assert 'data-for-console-test' not in capsys.readouterr().out

    def test_outgoing_openrouter_calls_are_logged_without_the_key(self, app: Flask) -> None:
        from app.services import ai_cover

        class Reply:
            status = 200
            def read(self): return b'{"data": {}}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch('urllib.request.urlopen', return_value=Reply()):
            ai_cover._get('key', 'sk-or-v1-secret-for-log-test')
        log = self._read('litkeeper.log')
        assert 'OpenRouter GET key → 200 in' in log
        assert 'secret-for-log-test' not in log

    def test_errors_are_one_line_with_the_page(self, app: Flask) -> None:
        from app.services.logger import log_error
        log_error('Network error while downloading chapter 1: HTTP 403', 'https://www.literotica.com/s/x')
        log = self._read('litkeeper.log')
        assert log.rstrip().endswith('ERROR Network error while downloading chapter 1: HTTP 403 (https://www.literotica.com/s/x)')
        assert '-----' not in log and 'URL:' not in log

    def test_download_log_files(self, client: FlaskClient) -> None:
        client.post('/api/story/99999996/queue', json={'in_queue': True})
        response = client.get('/settings/logs/litkeeper.log')
        assert response.status_code == 200
        assert b'/api/story/99999996/queue' in response.data

    def test_view_last_lines(self, client: FlaskClient) -> None:
        Path(self._log_path('litkeeper.log')).write_text(''.join(f'line {n}\n' for n in range(1, 1201)))
        data = client.get('/settings/logs/litkeeper.log/tail').get_json()
        assert data['count'] == 500
        assert data['lines'][0] == 'line 701' and data['lines'][-1] == 'line 1200'
        assert client.get('/settings/logs/litkeeper.log/tail?lines=3').get_json()['lines'] == ['line 1198', 'line 1199', 'line 1200']

    def test_view_fills_from_previous_days(self, client: FlaskClient) -> None:
        path = Path(self._log_path('litkeeper.log'))
        (path.parent / 'litkeeper.2026-11-17.log').write_text(''.join(f'day17 {n}\n' for n in range(1, 4)))
        (path.parent / 'litkeeper.2026-11-18.log').write_text('day18 1\nday18 2\n')
        path.write_text('today 1\n')
        try:
            lines = client.get('/settings/logs/litkeeper.log/tail?lines=5').get_json()['lines']
            assert lines == ['day17 2', 'day17 3', 'day18 1', 'day18 2', 'today 1']
        finally:
            for name in ('litkeeper.2026-11-17.log', 'litkeeper.2026-11-18.log'):
                (path.parent / name).unlink(missing_ok=True)

    def test_old_size_based_files_are_cleared(self, app: Flask) -> None:
        from app.services.logger import setup_file_logging, log_file_path
        old = Path(log_file_path('api.log') + '.1')
        old.write_text('from the 5 MB days\n')
        setup_file_logging()
        assert not old.exists()

    def test_view_empty_and_unknown_logs(self, client: FlaskClient) -> None:
        from app.services.logger import log_file_path
        Path(log_file_path('litkeeper.log')).unlink(missing_ok=True)
        assert client.get('/settings/logs/litkeeper.log/tail?lines=abc').get_json()['count'] == 0
        assert client.get('/settings/logs/api.log/tail').status_code == 404  # No longer written.
        assert client.get('/settings/logs/secret.key/tail').status_code == 404

    @pytest.mark.parametrize('name', ['secret.key', '..%2Flitkeeper.db', 'bulk_generation.log'])
    def test_only_log_files_can_be_downloaded(self, client: FlaskClient, name) -> None:
        assert client.get(f'/settings/logs/{name}').status_code == 404

    def test_settings_lists_logs_and_has_no_theme_section(self, client: FlaskClient) -> None:
        page = client.get('/settings/').data
        assert b'id="logsSection"' in page
        assert b'id="logLines"' in page
        assert b'id="logRetentionDays"' in page
        assert b'Today (current)' in page and b'API log (today)' not in page
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
