"""AI failures: OpenRouter requests retry what can succeed on a second try and explain the rest; a failure no
story could get past (key, credit, OpenRouter unreachable) pauses every AI queue without losing what waits."""
from __future__ import annotations
import io
import json
import time
import urllib.error
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.services import ai_cover, ai_queues, ai_rewrite, ai_summary

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]

PARTS = [{'number': 1, 'title': 'Part 1', 'paragraphs': ['B met her at the bar.', 'B smiled.']},
         {'number': 2, 'title': 'Part 2', 'paragraphs': ['Later B called her.']}]


class Reply:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def http_error(code: int, message: str = '') -> urllib.error.HTTPError:
    body = json.dumps({'error': {'message': message}}).encode()
    return urllib.error.HTTPError('https://openrouter.ai', code, 'error', {}, io.BytesIO(body))


GOOD = json.dumps({'model': 'm', 'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]}).encode()


@pytest.fixture(autouse=True)
def no_waiting():
    ai_queues._pause.clear()
    with patch('app.services.ai_cover.time.sleep'), patch('app.services.ai_stats.record'):
        yield
    ai_queues._pause.clear()


@pytest.mark.integration
class TestRequests:

    def post(self, *answers):
        with patch('urllib.request.urlopen', side_effect=list(answers)) as urlopen:
            try:
                return ai_cover._post(ai_cover.CHAT, {'model': 'test/model'}, 'sk-or-x'), urlopen.call_count
            except ValueError as error:
                return error, urlopen.call_count

    def test_a_reply_that_is_not_json_is_tried_again(self, app: Flask) -> None:
        result, calls = self.post(Reply(b'<html>Bad gateway</html>'), Reply(GOOD))
        assert calls == 2 and result['choices'][0]['message']['content'] == 'ok'

    def test_a_dropped_connection_is_tried_again(self, app: Flask) -> None:
        result, calls = self.post(ConnectionResetError('reset'), Reply(GOOD))
        assert calls == 2 and isinstance(result, dict)

    def test_a_provider_error_inside_a_200_reply_is_tried_again(self, app: Flask) -> None:
        busy = json.dumps({'error': {'code': 502, 'message': 'Provider returned error'}}).encode()
        result, calls = self.post(Reply(busy), Reply(GOOD))
        assert calls == 2 and isinstance(result, dict)

    def test_an_error_as_plain_text_is_reported(self, app: Flask) -> None:
        result, _ = self.post(Reply(json.dumps({'error': 'something odd'}).encode()))
        assert isinstance(result, ValueError) and 'something odd' in str(result)

    def test_unknown_model_says_so(self, app: Flask) -> None:
        result, calls = self.post(http_error(400, 'test/model is not a valid model ID'))
        assert calls == 1 and 'no model called "test/model"' in str(result)
        assert not isinstance(result, ai_cover.StopError)

    def test_no_credit_and_a_refused_key_stop_everything(self, app: Flask) -> None:
        credit, _ = self.post(http_error(402, 'Insufficient credits'))
        key, _ = self.post(http_error(401, 'No auth'))
        assert isinstance(credit, ai_cover.StopError) and 'credits' in str(credit) and credit.wait is None
        assert isinstance(key, ai_cover.StopError) and 'API key' in str(key)

    def test_unreachable_stops_and_tries_again_later(self, app: Flask) -> None:
        error = urllib.error.URLError('Name or service not known')
        result, calls = self.post(error, error, error)
        assert calls == 3 and isinstance(result, ai_cover.StopError) and result.wait == ai_cover.UNREACHABLE_WAIT

    def test_a_timeout_is_the_models_fault_not_a_pause(self, app: Flask) -> None:
        result, calls = self.post(TimeoutError(), TimeoutError(), TimeoutError())
        assert calls == 3 and not isinstance(result, ai_cover.StopError) and 'no answer within' in str(result)

    def test_a_statistics_failure_never_fails_the_call(self, app: Flask) -> None:
        with patch('app.services.ai_stats.record', side_effect=RuntimeError('database locked')):
            result, _ = self.post(Reply(GOOD))
        assert isinstance(result, dict)


@pytest.fixture
def stories(add_story, app: Flask):
    ids = [add_story(f'Story {n}', category='Romance', chapters=PARTS) for n in range(3)]
    with patch('app.services.ai_rewrite.api_key', return_value='sk-or-test'), \
         patch('app.services.ai_summary.api_key', return_value='sk-or-test'):
        yield ids


def wait_until(check, seconds: float = 5.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError('timed out')


@pytest.mark.integration
class TestPausing:

    def test_no_credit_pauses_the_queue_and_keeps_every_story(self, app: Flask, stories) -> None:
        calls = []

        def broke(*args, **kwargs):
            calls.append(1)
            raise ai_cover.StopError('OpenRouter reports insufficient credits.')
        with patch('app.services.ai_summary.make', side_effect=broke), \
             patch('app.services.notifier.send_notification') as notify:
            for story_id in stories:
                ai_summary.start(app, story_id, resume=False)
            wait_until(lambda: not ai_summary._running.get('working'))
        assert len(calls) == 1  # The other stories were not tried (and failed) one after another.
        assert ai_summary.overview()['waiting'] == stories  # The failed one went back to the front.
        assert 'insufficient credits' in ai_queues.paused()
        notify.assert_called_once()
        with app.app_context():
            from app.models import Story, db
            info = ai_summary.info(db.session.get(Story, stories[1]))
        assert info['queued'] and 'credits' in info['paused'] and not info['error']

    def test_resume_carries_on_where_it_stopped(self, app: Flask, stories) -> None:
        ai_queues._pause.update(reason='OpenRouter reports insufficient credits.', generation=1)
        for story_id in stories:
            ai_summary.start(app, story_id, resume=False)  # After a download: waits, does not resume.
        assert ai_queues.paused() and not ai_summary._running.get('working')
        made = []
        with patch('app.services.ai_summary.make', side_effect=lambda i: made.append(i)):
            assert ai_queues.resume(app, 'test')
            wait_until(lambda: len(made) == 3 and not ai_summary._running.get('working'))
        assert made == stories and not ai_queues.paused()

    def test_asking_for_ai_work_resumes(self, app: Flask, stories) -> None:
        ai_queues._pause.update(reason='OpenRouter refused the API key.', generation=1)
        with patch('app.services.ai_summary.make'):
            ai_summary.start(app, stories[0])
            wait_until(lambda: not ai_summary._running.get('working') and not ai_summary._queue)
        assert not ai_queues.paused()

    def test_unreachable_resumes_by_itself(self, app: Flask, stories) -> None:
        with patch('app.services.ai_queues.resume') as resume, patch('app.services.notifier.send_notification'):
            ai_queues.pause(app, ai_cover.StopError('Could not reach OpenRouter.', wait=0.05))
            wait_until(lambda: resume.called)
        assert ai_queues.pause_info()['until']

    def test_a_rewrite_paused_part_way_carries_on_after_the_saved_parts(self, app: Flask, stories) -> None:
        answers = iter([{'text': 'Brian met her at the bar.\n\nBrian smiled.', 'tokens': 1, 'cost': 0.0,
                         'model': 'm', 'truncated': False}])

        def fake(text, key, model, label=''):
            try:
                return next(answers)
            except StopIteration:
                raise ai_cover.StopError('OpenRouter reports insufficient credits.')
        with patch('app.services.ai_rewrite.request', side_effect=fake), \
             patch('app.services.notifier.send_notification'):
            ai_rewrite.start(app, stories[0], redo=True)
            wait_until(lambda: not ai_rewrite._running.get('working'))
        assert ai_rewrite.overview()['waiting'] == [stories[0]]
        assert ai_rewrite._redo[stories[0]] is False  # Part 1 is kept, not rewritten again.
        with app.app_context():
            from app.models import Story, db
            status = ai_rewrite.status(db.session.get(Story, stories[0]))
        assert status['done'] == 1 and status['queued'] and status['paused'] and not status['error']
        ai_rewrite.remove(stories[0])

    def test_queue_page_shows_the_pause_and_resumes(self, client: FlaskClient, app: Flask, stories) -> None:
        ai_queues._pause.update(reason='OpenRouter reports insufficient credits.', generation=1, until='')
        page = client.get('/queue/partials/ai-covers').get_data(as_text=True)
        assert 'AI work is paused.' in page and 'insufficient credits' in page and 'resumeAiWork()' in page
        assert client.post('/queue/api/ai/resume').get_json() == {'success': True, 'was_paused': True}
        assert not ai_queues.paused()
