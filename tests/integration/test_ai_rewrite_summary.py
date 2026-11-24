"""AI Rewrite and AI summaries (from RedKeeper): rewriting part by part with the original kept, consistent
names, flags, stop and redo; the reader and HTML download; summaries kept beside the description; the
story window, bulk actions, Settings, statistics and saved queues."""
from __future__ import annotations
import json
import os
import time
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.services import ai_rewrite, ai_summary

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]

PARTS = [
    {'number': 1, 'title': 'Part 1', 'paragraphs': ['B met her at the <em>bar</em>.', 'B smiled &amp; waved at her friend.']},
    {'number': 2, 'title': 'Part 2', 'paragraphs': ['Later B called her.', 'They talked all night.']},
]


def reply(text: str, cost: float = 0.001, finish: str = 'stop') -> dict:
    return {'model': 'test/model', 'usage': {'cost': cost, 'total_tokens': 100},
            'choices': [{'message': {'content': text}, 'finish_reason': finish}]}


@pytest.fixture
def story(add_story, app: Flask):
    story_id = add_story('Bar Night', category='Romance', chapters=PARTS, description="The author's own blurb.")
    with app.app_context():
        from app.models import Story, db
        yield db.session.get(Story, story_id)


@pytest.fixture
def keyed():
    with patch('app.services.ai_rewrite.api_key', return_value='sk-or-test'), \
         patch('app.services.ai_summary.api_key', return_value='sk-or-test'):
        yield


def rewrite_answers(*texts):
    calls = []

    def fake_post(url, payload, key, timeout=300, kind=None, label=''):
        calls.append({'payload': payload, 'kind': kind, 'label': label})
        return reply(texts[min(len(calls), len(texts)) - 1])
    return calls, fake_post


@pytest.mark.integration
class TestRewrite:

    def test_rewrites_each_part_keeps_the_original_and_names(self, app: Flask, story, keyed) -> None:
        calls, fake = rewrite_answers('Brian met her at the bar.\n\nBrian smiled and waved at her friend.',
                                      'Later Brian called her.\n\nThey talked all night.')
        with patch('app.services.ai_cover._post', side_effect=fake):
            assert ai_rewrite.rewrite_story(story.id) == 'done'
        first = calls[0]['payload']['messages']
        assert first[-1]['content'] == 'B met her at the bar.\n\nB smiled & waved at her friend.'  # Plain text, no HTML.
        assert first[0]['role'] == 'user'  # The default preset brings its own prompt.
        assert calls[0]['kind'] == 'rewrite' and 'part 1' in calls[0]['label']
        assert calls[1]['payload']['messages'][-1]['content'].startswith('Character names already used')
        assert 'B = Brian' in calls[1]['payload']['messages'][-1]['content']
        status = ai_rewrite.status(story)
        assert (status['done'], status['total'], status['flags']) == (2, 2, {})
        assert status['cost'] == pytest.approx(0.002)
        with open(story.formats[1].file_path if story.formats[1].format_type == 'json' else story.formats[0].file_path) as f:
            assert 'B met her' in f.read()  # The reader file is untouched.

    def test_a_model_name_gets_the_rewrite_prompt(self, app: Flask, story, keyed) -> None:
        ai_rewrite.save_settings('anthropic/claude-sonnet-4.5')
        calls, fake = rewrite_answers('Brian met her at the bar. Brian smiled and waved at her friend.')
        with patch('app.services.ai_cover._post', side_effect=fake):
            ai_rewrite.rewrite_story(story.id)
        messages = calls[0]['payload']['messages']
        assert messages[0]['role'] == 'system' and 'copy editor' in messages[0]['content']

    def test_flags_and_only_missing_parts_are_sent_again(self, app: Flask, story, keyed) -> None:
        calls, fake = rewrite_answers('B — met her.', 'Later Brian called her.\n\nThey talked all night.')
        with patch('app.services.ai_cover._post', side_effect=fake):
            ai_rewrite.rewrite_story(story.id)
        flags = ai_rewrite.status(story)['flags']['1']
        assert any('length changed' in f for f in flags) and 'contains dashes' in flags and 'still has initials B' in flags
        data = ai_rewrite.load_edits(story)
        del data['parts']['2']
        ai_rewrite._save_edits(story, data)
        calls2, fake2 = rewrite_answers('Later Brian called her.\n\nThey talked all night.')
        with patch('app.services.ai_cover._post', side_effect=fake2):
            ai_rewrite.rewrite_story(story.id)
        assert len(calls2) == 1 and 'Later B called her' in calls2[0]['payload']['messages'][-1]['content']

    def test_failure_is_saved_and_stops_the_run(self, app: Flask, story, keyed) -> None:
        with patch('app.services.ai_cover._post', side_effect=ValueError('OpenRouter reports insufficient credits.')):
            assert ai_rewrite.rewrite_story(story.id) == 'failed'
        assert ai_rewrite.status(story)['error'] == 'Part 1: OpenRouter reports insufficient credits.'

    def test_a_changed_original_part_counts_as_not_rewritten(self, app: Flask, story, keyed) -> None:
        calls, fake = rewrite_answers('Brian met her at the bar. Brian smiled and waved at her friend.', 'Later Brian called her. They talked.')
        with patch('app.services.ai_cover._post', side_effect=fake):
            ai_rewrite.rewrite_story(story.id)
        path = next(f.file_path for f in story.formats if f.format_type == 'json')
        data = json.load(open(path))
        data['chapters'][1]['paragraphs'].append('A new paragraph from an update.')
        json.dump(data, open(path, 'w'))
        assert ai_rewrite.status(story)['done'] == 1
        assert ai_rewrite.words_to_rewrite(story) > 0

    def test_reader_and_download_show_the_rewrite_with_the_original_a_click_away(self, client: FlaskClient, story, keyed) -> None:
        assert 'Read the AI rewrite' not in client.get(f'/read/{story.id}').get_data(as_text=True)
        calls, fake = rewrite_answers('Brian met <her> at the bar.', 'Later Brian called her.')
        with patch('app.services.ai_cover._post', side_effect=fake):
            ai_rewrite.rewrite_story(story.id)
        page = client.get(f'/read/{story.id}').get_data(as_text=True)
        assert 'Brian met &lt;her&gt; at the bar.' in page and 'Read the original' in page
        original = client.get(f'/read/{story.id}?version=original').get_data(as_text=True)
        assert '<em>bar</em>' in original and 'Read the AI rewrite' in original
        download = client.get(f'/download/html/{story.filename_base}').get_data(as_text=True)
        assert 'Brian met' in download
        assert 'B met her' in client.get(f'/download/html/{story.filename_base}?version=original').get_data(as_text=True)

    def test_stop_after_the_current_part(self, app: Flask, story, keyed) -> None:
        ai_rewrite._running['stop'] = True
        try:
            calls, fake = rewrite_answers('Brian met her at the bar. Brian smiled and waved at her friend.')
            with patch('app.services.ai_cover._post', side_effect=fake):
                assert ai_rewrite.rewrite_story(story.id) == 'stopped'
        finally:
            ai_rewrite._running.clear()
        assert len(calls) == 1 and ai_rewrite.status(story)['stopped'] == 'Stopped after part 1.'


@pytest.mark.integration
class TestRewriteApi:

    def test_needs_a_key(self, client: FlaskClient, story) -> None:
        with patch('app.services.ai_rewrite.api_key', return_value=''):
            response = client.post(f'/api/story/{story.id}/ai-rewrite', json={'action': 'continue'})
        assert response.status_code == 400 and 'OpenRouter API key' in response.get_json()['message']

    def test_runs_in_the_background_then_discard(self, client: FlaskClient, story, keyed, app: Flask) -> None:
        calls, fake = rewrite_answers('Brian met her at the bar. Brian smiled and waved at her friend.', 'Later Brian called her. They talked.')
        with patch('app.services.ai_cover._post', side_effect=fake):
            assert client.post(f'/api/story/{story.id}/ai-rewrite', json={'action': 'continue'}).get_json()['success']
            for _ in range(100):
                info = client.get(f'/api/story/{story.id}/ai-rewrite').get_json()
                if not info['running'] and not info['queued']:
                    break
                time.sleep(0.05)
        assert info['done'] == 2
        assert client.post(f'/api/story/{story.id}/ai-rewrite', json={'action': 'discard'}).get_json()['done'] == 0

    def test_story_window_has_the_controls(self, client: FlaskClient, story) -> None:
        html = client.get(f'/api/story/{story.id}/modal').get_data(as_text=True)
        assert 'data-kind="rewrite"' in html and 'data-kind="summary"' in html and 'initStoryAiText' in html


@pytest.mark.integration
class TestSummary:

    def test_two_sentences_kept_beside_the_description(self, app: Flask, story, keyed) -> None:
        answer = 'Summary: "Brian meets a woman at a bar. They talk all night. Then something else happens."'
        with patch('app.services.ai_cover._post', return_value=reply(answer)) as post:
            ai_summary.make(story.id)
        assert post.call_args.kwargs['kind'] == 'summary'
        assert ai_summary.summary(story) == 'Brian meets a woman at a bar. They talk all night.'
        assert story.description == "The author's own blurb."
        assert ai_summary.info(story)['model'] == 'deepseek/deepseek-v4-flash'

    def test_asks_again_when_too_long_and_explains_a_filter_block(self, app: Flask, story, keyed) -> None:
        long = 'Word ' * 80 + '.'
        with patch('app.services.ai_cover._post', side_effect=[reply(long), reply('Brian meets a woman at a bar and they talk.')]) as post:
            ai_summary.make(story.id)
        assert post.call_count == 2 and 'Too long' in post.call_args.args[1]['messages'][-1]['content']
        with patch('app.services.ai_cover._post', return_value=reply('', finish='content_filter')):
            with pytest.raises(ValueError, match='content filter'):
                ai_summary.make(story.id)

    def test_api_window_export_and_clear(self, client: FlaskClient, story, keyed) -> None:
        with patch('app.services.ai_cover._post', return_value=reply('Brian meets a woman at a bar and they talk all night.')):
            assert client.post(f'/api/story/{story.id}/ai-summary', json={'action': 'make'}).get_json()['success']
            for _ in range(100):
                info = client.get(f'/api/story/{story.id}/ai-summary').get_json()
                if info['summary']:
                    break
                time.sleep(0.05)
        assert info['summary'].startswith('Brian meets')
        assert 'Brian meets a woman' in client.get(f'/api/story/{story.id}/modal').get_data(as_text=True)
        assert 'Brian meets a woman' in client.get(f'/download/html/{story.filename_base}').get_data(as_text=True)
        assert client.post(f'/api/story/{story.id}/ai-summary', json={'action': 'clear'}).get_json()['summary'] == ''

    def test_made_after_download_only_when_turned_on(self, app: Flask, story, keyed) -> None:
        with patch('app.services.ai_summary.start') as start:
            ai_summary.make_after_download(app, story.id)
            start.assert_not_called()
            ai_summary.save_settings('', True)
            ai_summary.make_after_download(app, story.id)
            start.assert_called_once_with(app, story.id)


@pytest.mark.integration
class TestBulkSettingsAndQueues:

    def test_bulk_skips_stories_already_done(self, client: FlaskClient, story, keyed) -> None:
        ai_summary.set_summary(story, 'Already summarized here.')
        with patch('app.services.ai_cover.settings', return_value={'api_key': 'sk-or-test'}), \
             patch('app.services.ai_summary.start') as start:
            data = client.post('/api/stories/bulk', json={'action': 'ai_summaries', 'ids': [story.id]}).get_json()
        assert data['skipped'] == 1 and data['done'] == 0
        start.assert_not_called()
        with patch('app.services.ai_cover.settings', return_value={'api_key': 'sk-or-test'}), \
             patch('app.services.ai_rewrite.start') as start:
            data = client.post('/api/stories/bulk', json={'action': 'ai_rewrite', 'ids': [story.id]}).get_json()
        assert data['done'] == 1
        start.assert_called_once()
        assert "bulkAction('ai_rewrite', this)" not in client.get('/').get_data(as_text=True)  # No key saved yet.
        with patch('app.services.ai_cover.settings', return_value={'api_key': 'sk-or-test'}):
            page = client.get('/').get_data(as_text=True)
        assert "bulkAction('ai_rewrite', this)" in page and "bulkAction('ai_summaries', this)" in page

    def test_settings_save_and_validate(self, client: FlaskClient) -> None:
        page = client.get('/settings/').get_data(as_text=True)
        assert 'id="aiTextSection"' in page and 'value="@preset/redkeeper"' in page and 'AI summary (text model)' in page
        data = client.post('/settings/ai-text', json={'rewrite_model': ' ', 'summary_model': 'google/gemini-2.5-flash',
                                                      'summary_auto': True}).get_json()
        assert data['rewrite']['model'] == '@preset/redkeeper'
        assert data['summary'] == {'model': 'google/gemini-2.5-flash', 'auto': True}
        assert client.post('/settings/ai-text', json={'rewrite_model': 'bad model!'}).status_code == 400

    def test_statistics_list_rewrites_and_summaries(self, app: Flask) -> None:
        from app.services import ai_stats
        ai_stats.record('rewrite', 'test/model', 0.01, True)
        ai_stats.record('summary', 'test/model', 0.0002, True)
        labels = [k['label'] for k in ai_stats.tables()['kinds']]
        assert 'AI Rewrite' in labels and 'AI summaries' in labels

    def test_queues_survive_a_restart(self, app: Flask, story) -> None:
        from app.services import ai_queues
        ai_queues.save('rewrite', [[story.id, True], [999999, False]])
        with patch('app.services.ai_rewrite.start') as start:
            ai_rewrite.restore(app)
        start.assert_called_once_with(app, story.id, True)  # Deleted stories are dropped.

    def test_deleting_a_story_removes_its_rewrite_and_summary(self, app: Flask, story) -> None:
        ai_summary.set_summary(story, 'A summary to delete.')
        ai_rewrite._save_edits(story, {'parts': {}})
        paths = [ai_summary._path(story), ai_rewrite._path(story)]
        from app.services.story_deletion import StoryDeletionService
        assert StoryDeletionService().delete_story(story.id)['success']
        assert not any(os.path.exists(p) for p in paths)
