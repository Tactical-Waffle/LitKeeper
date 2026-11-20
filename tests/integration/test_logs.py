"""Logs: a new file each day or at the size limit, old files kept uncompressed for the retention period,
both set in Settings → Logs; API data on its own hideable line; the viewer's entries."""
from __future__ import annotations
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path
import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.services import logger as logs
import re

re_line = re.compile(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d INFO Hello$')

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]


@pytest.fixture(autouse=True)
def clean(app: Flask, monkeypatch: pytest.MonkeyPatch):
    from app.models import AppConfig, db
    monkeypatch.delenv('LOG_RETENTION_DAYS', raising=False)

    def clear():
        with app.app_context():
            AppConfig.query.filter_by(key=logs.LOG_SETTINGS_KEY).delete()
            db.session.commit()
    clear()
    yield
    clear()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path


def _handler(folder: Path, name='litkeeper.log', max_bytes=0, days=30):
    handler = logs.DailyFileHandler(str(folder / name), max_bytes, days)
    handler.setFormatter(logs._FORMAT)
    return handler


def _record(message: str, level=logging.INFO, name='litkeeper.action'):
    return logging.LogRecord(name, level, __file__, 1, message, None, None)


@pytest.mark.unit
class TestDailyFiles:

    def test_new_file_each_day_kept_uncompressed(self, folder: Path) -> None:
        handler = _handler(folder)
        handler.emit(_record('yesterday'))
        handler.day = date(2026, 11, 19)  # As if the file was started yesterday.
        handler.emit(_record('today'))
        handler.close()
        assert (folder / 'litkeeper.2026-11-19.log').read_text().strip().endswith('yesterday')
        assert 'today' in (folder / 'litkeeper.log').read_text()
        assert not list(folder.glob('*.gz'))

    def test_new_file_at_size_limit(self, folder: Path) -> None:
        handler = _handler(folder, max_bytes=300)
        for n in range(12):
            handler.emit(_record(f'message {n} ' + 'x' * 40))
        handler.close()
        today = date.today().isoformat()
        kept = sorted(p.name for p in folder.iterdir())
        assert f'litkeeper.{today}.log' in kept and f'litkeeper.{today}.1.log' in kept
        assert all(p.stat().st_size < 400 for p in folder.iterdir())

    def test_old_files_removed_after_retention(self, folder: Path) -> None:
        old = (date.today() - timedelta(days=31)).isoformat()
        recent = (date.today() - timedelta(days=29)).isoformat()
        for name in (f'litkeeper.{old}.log', f'litkeeper.{old}.1.log', f'litkeeper.{recent}.log', f'api.{old}.log'):
            (folder / name).write_text('x\n')
        _handler(folder, days=30).close()
        names = {p.name for p in folder.iterdir()}
        assert f'litkeeper.{recent}.log' in names
        assert f'litkeeper.{old}.log' not in names and f'litkeeper.{old}.1.log' not in names
        assert f'api.{old}.log' in names  # Each log looks after its own files.

    def test_earlier_builds_files_renamed(self, app: Flask) -> None:
        base = Path(logs.log_file_path('litkeeper.log'))
        stamp = (date.today() - timedelta(days=2)).isoformat()
        old = Path(f'{base}.{stamp}')
        old.write_text('from before\n')
        logs.setup_file_logging()
        renamed = base.parent / f'litkeeper.{stamp}.log'
        try:
            assert not old.exists() and renamed.read_text() == 'from before\n'
        finally:
            renamed.unlink(missing_ok=True)


@pytest.mark.integration
class TestLogSettings:

    def test_defaults(self, app: Flask) -> None:
        with app.app_context():
            assert logs.log_settings() == {'max_mb': 10, 'retention_days': 30}

    def test_environment_retention_until_saved(self, app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('LOG_RETENTION_DAYS', '7')
        with app.app_context():
            assert logs.log_retention_days() == 7

    def test_save_applies_to_running_logs(self, app: Flask, client: FlaskClient) -> None:
        logs.setup_file_logging()
        response = client.post('/settings/logs', json={'max_mb': '25', 'retention_days': 60})
        assert response.get_json() == {'success': True, 'max_mb': 25, 'retention_days': 60}
        handler = next(h for h in logs.api_logger.handlers if isinstance(h, logs.DailyFileHandler))
        assert handler.max_bytes == 25 * 1024 * 1024 and handler.retention_days == 60
        page = client.get('/settings/').get_data(as_text=True)
        assert 'id="logMaxMb"' in page and 'value="25"' in page and 'value="60"' in page

    @pytest.mark.parametrize('body', [{'max_mb': 0, 'retention_days': 30}, {'max_mb': 1001, 'retention_days': 30},
                                      {'max_mb': 10, 'retention_days': 0}, {'max_mb': 'big', 'retention_days': 30}, {}])
    def test_rejects_bad_values(self, client: FlaskClient, body) -> None:
        assert client.post('/settings/logs', json=body).status_code == 400


@pytest.mark.integration
class TestApiDataAndViewer:

    def test_incoming_api_json_on_its_own_line_with_secrets_hidden(self, app: Flask, client: FlaskClient) -> None:
        logs.setup_file_logging()
        path = Path(logs.log_file_path('litkeeper.log'))
        path.write_text('')
        client.post('/api/story/99999999/queue', json={'in_queue': True, 'api_key': 'sk-should-not-appear'})
        text = path.read_text()
        request_line = next(l for l in text.splitlines() if 'POST /api/story/99999999/queue → 404' in l)
        number = request_line.split(' INFO ')[1].split()[0]
        assert number.startswith('#')
        data_line = next(l for l in text.splitlines() if f'API data {number} sent:' in l)
        assert '"in_queue": true' in data_line and 'sk-should-not-appear' not in data_line and '(hidden)' in data_line
        entries = client.get('/settings/logs/litkeeper.log/entries').get_json()['entries']
        assert [e['api_data'] for e in entries if number in e['text']] == [False, True]

    def test_outgoing_call_lines(self, app: Flask) -> None:
        logs.setup_file_logging()
        path = Path(logs.log_file_path('litkeeper.log'))
        path.write_text('')
        number = logs.log_api_call_started('OpenRouter', 'POST', 'chat/completions', {'model': 'm', 'messages': ['x' * 5000]})
        logs.log_api_call('OpenRouter', 'POST', 'chat/completions', 200, 1234, number)
        logs.log_api_call('OpenRouter', 'POST', 'chat/completions', 429, 50, number)
        lines = path.read_text().splitlines()
        assert lines[0].endswith(f'INFO API call #{number} started: OpenRouter POST chat/completions')
        assert f'API data #{number} sent:' in lines[1] and 'more characters' in lines[1]
        assert lines[2].endswith(f'INFO API call #{number}: OpenRouter POST chat/completions → 200 in 1.2 s')
        assert ' WARNING API call #' in lines[3] and '→ 429' in lines[3]

    def test_new_line_format(self, app: Flask) -> None:
        logs.setup_file_logging()
        path = Path(logs.log_file_path('litkeeper.log'))
        path.write_text('')
        logs.log_action('Hello')
        assert re_line.match(path.read_text().splitlines()[0])

    def test_entries_group_lines_and_mark_levels(self, app: Flask, client: FlaskClient) -> None:
        path = Path(logs.log_file_path('litkeeper.log'))
        path.write_text('\n'.join([
            '2026-11-20 10:00:00 - INFO - litkeeper.action - Started',
            '2026-11-20 10:00:01 - ERROR - litkeeper.error - Something broke',
            'Traceback (most recent call last):',
            '  File "x.py", line 1',
            '2026-11-20 10:00:02 - litkeeper.error - An old line without a level',
            '2026-11-20 10:00:03 - WARNING - litkeeper.action - Careful',
            '2026-11-20 10:00:04 - INFO - litkeeper.api - API data #3 sent: {"a": 1}',
            '2026-11-20 10:00:05 WARNING New style line',
            'continued',
            '2026-11-20 10:00:06 INFO API data #4 sent: {"b": 2}',
        ]) + '\n')
        data = client.get('/settings/logs/litkeeper.log/entries').get_json()
        entries = data['entries']
        assert [e['level'] for e in entries] == ['INFO', 'ERROR', 'ERROR', 'WARNING', 'INFO', 'WARNING', 'INFO']
        assert entries[5]['text'].endswith('continued')
        assert entries[1]['text'].endswith('line 1') and 'Traceback' in entries[1]['text']
        assert [e['api_data'] for e in entries] == [False, False, False, False, True, False, True]
        assert data['files'][0]['name'] == 'litkeeper.log'

    def test_kept_files_listed_viewable_downloadable(self, app: Flask, client: FlaskClient) -> None:
        folder = Path(logs.log_file_path('litkeeper.log')).parent
        kept = folder / 'api.2026-11-18.1.log'
        kept.write_text('2026-11-18 09:00:00 - INFO - litkeeper.api - GET /api/x 200 1 ms -\n')
        try:
            names = [f['name'] for f in client.get('/settings/logs/litkeeper.log/entries').get_json()['files']]
            assert 'api.2026-11-18.1.log' in names
            assert client.get('/settings/logs/api.2026-11-18.1.log/entries').get_json()['entries'][0]['text'].endswith('1 ms -')
            assert client.get('/settings/logs/api.2026-11-18.1.log').status_code == 200
            assert client.get('/settings/logs/api.2026-11-18.2.log').status_code == 404
            assert client.get('/settings/logs/other.2026-11-18.log/entries').status_code == 404
        finally:
            kept.unlink(missing_ok=True)

    def test_viewer_controls_on_page(self, client: FlaskClient) -> None:
        page = client.get('/settings/').get_data(as_text=True)
        for marker in ('id="logLevel"', 'Warnings and errors', 'id="logSearch"', 'id="logShowApi"',
                       'Scroll to bottom on new message', 'resize: vertical', 'id="logFile"'):
            assert marker in page
