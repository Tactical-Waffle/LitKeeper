"""From RedKeeper: shared text models (up to five, tried in order) and story text, up to five image models, new
excerpt defaults, remade covers starting with the second image model; Title (numbers by value) and Length sorts;
word counts as the sum of all parts; AI Rewrite on import and for the next part while reading; cup sizes are not
initials; GF and BF in capitals."""
from __future__ import annotations
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.services import ai_cover, ai_rewrite, ai_summary

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]

PARTS = [{'number': 1, 'title': 'Part 1', 'paragraphs': ['B met her at the bar.', 'They talked.']},
         {'number': 2, 'title': 'Part 2', 'paragraphs': ['Later B called her.', 'They talked all night long.']},
         {'number': 3, 'title': 'Part 3', 'paragraphs': ['The end came.']}]


def reply(text: str, cost: float = 0.001, finish: str = 'stop', model: str = '') -> dict:
    return {'model': model, 'usage': {'cost': cost}, 'choices': [{'message': {'content': text}, 'finish_reason': finish}]}


def save_config(app: Flask, key: str, value: dict) -> None:
    from app.models import AppConfig, db
    with app.app_context():
        config = AppConfig.query.filter_by(key=key).first() or AppConfig(key=key, value_type='json')
        config.value_type = 'json'
        config.set_value(value)
        db.session.add(config)
        db.session.commit()


@pytest.fixture(autouse=True)
def clean_settings(app: Flask):
    from app.models import AppConfig, db

    def clear():
        with app.app_context():
            AppConfig.query.filter(AppConfig.key.in_((ai_cover.KEY, ai_summary.KEY, ai_rewrite.KEY))).delete(synchronize_session=False)
            db.session.commit()
    clear()
    with ai_rewrite._lock:
        ai_rewrite._queue.clear()
        ai_rewrite._only.clear()
        ai_rewrite._redo.clear()
    yield
    clear()
    with ai_rewrite._lock:
        ai_rewrite._queue.clear()
        ai_rewrite._only.clear()
        ai_rewrite._redo.clear()


@pytest.mark.integration
class TestModelSettings:

    def test_old_settings_move_to_the_lists_and_new_excerpt_defaults(self, app: Flask) -> None:
        save_config(app, ai_cover.KEY, {'text_model': 'a/text', 'image_model': 'a/image', 'fallback_models': ['b/image'],
                                        'excerpt_percent': 20, 'excerpt_parts': 5, 'excerpt_words': 40000})
        save_config(app, ai_summary.KEY, {'model': 'b/text', 'auto': False})
        with app.app_context():
            config = ai_cover.settings()
        assert config['text_models'] == ['a/text', 'b/text'] and config['image_models'] == ['a/image', 'b/image']
        assert (config['excerpt_percent'], config['excerpt_parts'], config['excerpt_words']) == (20, 3, 20000)

    def test_changed_excerpt_settings_are_kept(self, app: Flask) -> None:
        save_config(app, ai_cover.KEY, {'excerpt_percent': 30, 'excerpt_parts': 5, 'excerpt_words': 40000})
        with app.app_context():
            assert ai_cover.settings()['excerpt_percent'] == 30 and ai_cover.settings()['excerpt_parts'] == 5

    def test_ai_models_section_saves_text_models_and_story_text(self, client: FlaskClient, app: Flask) -> None:
        data = client.post('/settings/ai-models', json={'text_model': 'x/one, x/two', 'story_text': 'opening',
                                                         'excerpt_parts': '2'}).get_json()
        assert data['success'] and data['settings']['text_models'] == ['x/one', 'x/two']
        with app.app_context():
            assert ai_cover.settings()['story_text'] == 'opening' and ai_cover.settings()['excerpt_parts'] == 2
        too_many = client.post('/settings/ai-models', json={'text_model': 'a/1, a/2, a/3, a/4, a/5, a/6'})
        assert too_many.status_code == 400 and 'at most 5' in too_many.get_json()['message']
        assert client.post('/settings/ai-covers', json={'image_model': 'i/one, i/two'}).get_json()['settings']['image_models'] == ['i/one', 'i/two']

    def test_settings_page(self, client: FlaskClient) -> None:
        page = client.get('/settings/').get_data(as_text=True)
        assert page.index('id="aiModelsSection"') < page.index('id="aiCoversSection"')
        assert 'Story text sent to the AI' in page and 'id="aiCoverImageModels"' in page
        assert 'id="aiSummaryModel"' not in page and 'Fallback image models' not in page
        assert 'Rewrite single-part stories when they are imported' in page and 'Rewrite the next part while reading' in page


@pytest.mark.integration
class TestTextModelsInOrder:

    def test_chat_moves_on_after_a_failure(self, app: Flask) -> None:
        tried = []

        def post(url, payload, key, timeout=300, kind=None, label=''):
            tried.append(payload['model'])
            if payload['model'] == 'a/first':
                raise ValueError('OpenRouter error 400')
            return reply('fine')
        with app.app_context(), patch('app.services.ai_cover._post', side_effect=post):
            ai_cover.chat({'messages': []}, 'key', models=['a/first', 'b/second'])
            assert tried == ['a/first', 'b/second'] and ai_cover.used_model() == 'b/second'

    def test_key_problems_stop_at_once(self, app: Flask) -> None:
        with app.app_context(), patch('app.services.ai_cover._post', side_effect=ai_cover.StopError('key refused')) as post:
            with pytest.raises(ai_cover.StopError):
                ai_cover.chat({'messages': []}, 'key', models=['a/first', 'b/second'])
        assert post.call_count == 1

    def test_unusable_description_asks_the_next_model(self, app: Flask, add_story) -> None:
        story_id = add_story('Bar Night', chapters=PARTS)
        scene = 'A couple stands close together in a dim bar, her hand on his arm, warm amber light behind them, ' \
                'rain on the window, both smiling softly.'
        replies = iter([reply("I can't help with that."), reply(scene)])
        with app.app_context(), patch('app.services.ai_cover._post', side_effect=lambda *a, **k: next(replies)) as post:
            from app.models import Story, db
            brief, cost = ai_cover.write_brief(db.session.get(Story, story_id), 'key', ['a/first', 'b/second'])
        assert brief == scene and [c.args[1]['model'] for c in post.call_args_list] == ['a/first', 'b/second']

    def test_summaries_use_the_text_models(self, app: Flask, add_story) -> None:
        story_id = add_story('Bar Night', chapters=PARTS)
        save_config(app, ai_cover.KEY, {'text_models': ['a/first', 'b/second']})
        # The first model is asked twice (as before), then the next one.
        replies = iter([reply(''), reply(''), reply('Brian meets a woman at a bar and they talk all night.')])
        with app.app_context(), patch('app.services.ai_summary.api_key', return_value='key'), \
                patch('app.services.ai_cover._post', side_effect=lambda *a, **k: next(replies)) as post:
            from app.models import Story, db
            ai_summary.make(story_id)
            story = db.session.get(Story, story_id)
            assert ai_summary.saved(story)['model'] == 'b/second'
        assert [c.args[1]['model'] for c in post.call_args_list] == ['a/first', 'a/first', 'b/second']


@pytest.mark.integration
class TestRemadeCovers:

    def test_remake_starts_with_the_second_image_model(self, app: Flask, add_story) -> None:
        story_id = add_story('Bar Night', chapters=PARTS)
        save_config(app, ai_cover.KEY, {'api_key': 'sk-or-test', 'image_models': ['i/one', 'i/two', 'i/three']})
        seen = {}

        def paint(story, brief, key, config):
            seen['models'] = config['image_models']
            raise ValueError('stop here')
        with app.app_context(), patch('app.services.ai_cover.write_brief', return_value=('scene', 0.0)), \
                patch('app.services.ai_cover.paint_with_fallbacks', side_effect=paint):
            with pytest.raises(ValueError):
                ai_cover.make_cover(story_id, remake=True)
            assert seen['models'] == ['i/two', 'i/three', 'i/one']
            with pytest.raises(ValueError):
                ai_cover.make_cover(story_id)
            assert seen['models'] == ['i/one', 'i/two', 'i/three']

    def test_new_ai_cover_for_a_story_with_artwork_is_a_remake(self, app: Flask, client: FlaskClient, add_story) -> None:
        story_id = add_story('Bar Night', chapters=PARTS)
        save_config(app, ai_cover.KEY, {'api_key': 'sk-or-test'})
        with patch('app.services.ai_cover.start', return_value='queued') as start, \
                patch('app.services.ai_cover.info', return_value={'has_art': True}):
            client.post(f'/api/story/{story_id}/ai-cover')
        assert start.call_args.kwargs['remake'] is True


@pytest.mark.integration
class TestSorts:

    def test_title_sorts_numbers_by_value_and_length_by_words(self, client: FlaskClient, add_story) -> None:
        add_story('Chapter 10', word_count=50)
        add_story('Chapter 2', word_count=900)
        add_story('chapter 1', word_count=300)
        html = client.get('/library/filter?sort_by=name&sort_order=asc').get_data(as_text=True)
        assert html.index('chapter 1') < html.index('Chapter 2') < html.index('Chapter 10')
        html = client.get('/library/filter?sort_by=length&sort_order=desc').get_data(as_text=True)
        assert html.index('Chapter 2') < html.index('chapter 1') < html.index('Chapter 10')
        assert client.get('/library/filter?sort_by=pages&sort_order=desc').status_code == 200  # Falls back to Date Added.

    def test_sort_menu(self, client: FlaskClient) -> None:
        page = client.get('/').get_data(as_text=True)
        assert '<option value="length">Length</option>' in page and 'value="pages"' not in page
        assert '<option value="name">Title</option>' in page

    def test_word_counts_are_the_sum_of_all_parts(self, app: Flask, add_story) -> None:
        from app.services import word_counts
        story_id = add_story('Counted', chapters=PARTS, word_count=3)
        with app.app_context():
            from app.models import AppConfig, Story, db
            AppConfig.query.filter_by(key=word_counts.DONE_KEY).delete()
            db.session.commit()
            word_counts.recount_all()
            assert db.session.get(Story, story_id).word_count == 20
            assert word_counts.recount_all() == 0  # Once.


@pytest.mark.integration
class TestRewriteOptions:

    def test_switches_saved(self, client: FlaskClient, app: Flask) -> None:
        data = client.post('/settings/ai-text', json={'rewrite_model': '', 'rewrite_single': True, 'rewrite_next': True,
                                                       'summary_auto': False}).get_json()
        assert data['rewrite']['auto_single'] and data['rewrite']['auto_next']

    def test_single_part_story_rewritten_when_imported(self, app: Flask, add_story) -> None:
        one = add_story('One Part', chapter_count=1)
        many = add_story('Many Parts', chapters=PARTS, chapter_count=3)
        save_config(app, ai_rewrite.KEY, {'auto_single': True})
        with app.app_context(), patch('app.services.ai_rewrite.api_key', return_value='key'), \
                patch('app.services.ai_rewrite.start') as start:
            assert ai_rewrite.after_import(app, one) and not ai_rewrite.after_import(app, many)
        assert start.call_count == 1

    def test_off_by_default(self, app: Flask, add_story) -> None:
        one = add_story('One Part', chapter_count=1)
        with app.app_context(), patch('app.services.ai_rewrite.api_key', return_value='key'):
            assert not ai_rewrite.after_import(app, one)

    def test_next_part_while_reading(self, app: Flask, client: FlaskClient, add_story) -> None:
        story_id = add_story('Reading Along', chapters=PARTS, chapter_count=3)
        other = add_story('Waiting Story')
        assert client.post(f'/api/story/{story_id}/rewrite-next', json={'part': 1}).get_json()['state'] == 'off'
        save_config(app, ai_rewrite.KEY, {'auto_next': True})
        ai_rewrite._queue.append(other)
        with patch('app.services.ai_rewrite.api_key', return_value='key'), patch.object(ai_rewrite, 'kick', return_value=False):
            assert client.post(f'/api/story/{story_id}/rewrite-next', json={'part': 1}).get_json()['state'] == 'queued'
            assert list(ai_rewrite._queue) == [story_id, other] and ai_rewrite._only[story_id] == {'2'}
            assert client.post(f'/api/story/{story_id}/rewrite-next', json={'part': 3}).get_json()['state'] == 'last'
        from app.services import ai_queues
        saved = ai_queues.load('rewrite')
        assert saved[0] == [story_id, {'redo': False, 'only': ['2']}]
        assert client.post(f'/api/story/{story_id}/rewrite-next', json={'part': 'x'}).status_code == 400

    def test_only_rewrites_the_parts_asked_for(self, app: Flask, add_story) -> None:
        story_id = add_story('Just One', chapters=PARTS, chapter_count=3)
        with app.app_context(), patch('app.services.ai_rewrite.api_key', return_value='key'), \
                patch('app.services.ai_cover._post', return_value=reply('Later Brian called her.\n\nThey talked all night long.')) as post:
            ai_rewrite.rewrite_story(story_id, only={'2'})
            from app.models import Story, db
            assert set(ai_rewrite.edited_parts(db.session.get(Story, story_id))) == {'2'}
        assert post.call_count == 1

    def test_reader_asks_for_the_next_part_only_when_on(self, app: Flask, client: FlaskClient, add_story) -> None:
        story_id = add_story('Reading Along', chapters=PARTS, chapter_count=3)
        assert 'rewrite-next' not in client.get(f'/read/{story_id}').get_data(as_text=True)
        save_config(app, ai_rewrite.KEY, {'auto_next': True})
        with patch('app.services.ai_rewrite.api_key', return_value='key'):
            assert 'rewrite-next' in client.get(f'/read/{story_id}').get_data(as_text=True)

    @pytest.mark.parametrize('text', ['She wore a double D bra.', 'Her triple-D chest.', 'A DD cup.', 'Her DDs were huge.',
                                      'She had D cups.', "Her DD's tits"])
    def test_cup_sizes_are_not_initials(self, text: str) -> None:
        assert ai_rewrite.initials(text) == set()

    def test_real_initials_still_found(self) -> None:
        assert ai_rewrite.initials('Then B kissed her.') == {'B'}


@pytest.mark.unit
def test_gf_and_bf_in_capitals() -> None:
    from app.utils.titles import title_from_slug
    assert title_from_slug('my-gfs-sister-and-her-bf') == 'My GFs Sister And Her BF'
    assert title_from_slug("the-gf's-secret") == "The GF's Secret"
