from __future__ import annotations
import base64
import io
import json
import os
import uuid
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from flask import Flask
from flask.testing import FlaskClient
from PIL import Image

from app.services import ai_cover, ai_prompts
from app.services.cover_generator import draw_story_cover, ai_art_paths

API_KEY = 'sk-or-v1-' + 'k' * 40

# LitKeeper's models use datetime.utcnow() and its routes Query.get_or_404(), which newer Python and
# SQLAlchemy warn about; pytest.ini turns warnings into errors.
pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]


def _jpeg(color=(120, 60, 60), size=(768, 1024)) -> bytes:
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, 'JPEG')
    return out.getvalue()


def _png(size=(768, 1024)) -> bytes:
    out = io.BytesIO()
    Image.new('RGB', size, (10, 20, 30)).save(out, 'PNG')
    return out.getvalue()


@pytest.fixture(autouse=True)
def clean_settings(app: Flask):
    """LitKeeper's test app shares one database between tests, so start and finish with default settings."""
    from app.models import AppConfig, db

    def clear():
        with app.app_context():
            AppConfig.query.filter(AppConfig.key.in_([ai_cover.KEY, ai_prompts.KEY])).delete(synchronize_session=False)
            db.session.commit()
    clear()
    yield
    clear()


@pytest.fixture
def covers(monkeypatch: pytest.MonkeyPatch, temp_dir: Path) -> Path:
    folder = temp_dir / 'covers'
    folder.mkdir()
    import app.utils
    monkeypatch.setattr(app.utils, 'get_cover_directory', lambda: str(folder))
    return folder


@pytest.fixture
def story(app: Flask, temp_dir: Path):
    """A story with a reader (JSON) file of three parts, removed again afterwards."""
    from app.models import Author, Story, StoryFormat, Tag, db
    unique = uuid.uuid4().hex[:8]
    chapters = [{'number': n, 'title': f'Part {n}', 'paragraphs': [f'Part {n} paragraph {i} ' + 'word ' * 50 for i in range(10)]}
                for n in (1, 2, 3)]
    json_path = temp_dir / f'{unique}.json'
    json_path.write_text(json.dumps({'title': 'Harbour Lights', 'chapters': chapters}))
    with app.app_context():
        author = Author(name=f'Test Author {unique}')
        db.session.add(author)
        db.session.flush()
        record = Story(title='Harbour Lights', author_id=author.id, filename_base=f'harbour-lights-{unique}',
                       description='Two old friends meet again at a seaside inn.', source_type='custom')
        record.tags = [Tag(name=f'romance-{unique}')]
        db.session.add(record)
        db.session.flush()
        db.session.add(StoryFormat(story_id=record.id, format_type='json', file_path=str(json_path)))
        db.session.commit()
        story_id, author_id, tag_names = record.id, author.id, [t.name for t in record.tags]
    yield story_id
    with app.app_context():
        record = db.session.get(Story, story_id)
        if record:
            db.session.delete(record)
        Tag.query.filter(Tag.name.in_(tag_names)).delete(synchronize_session=False)
        db.session.commit()
        author = db.session.get(Author, author_id)
        if author:
            db.session.delete(author)
        db.session.commit()


def _get_story(story_id):
    from app.models import Story, db
    return db.session.get(Story, story_id)


@pytest.mark.unit
class TestSettings:

    def test_defaults(self, app: Flask) -> None:
        with app.app_context():
            current = ai_cover.public_settings()
        assert current['text_model'] == ai_cover.DEFAULT_TEXT_MODEL
        assert current['has_key'] is False
        assert 'api_key' not in current

    def test_key_is_kept_when_left_blank_and_never_shown(self, app: Flask) -> None:
        with app.app_context():
            ai_cover.save_settings({'api_key': API_KEY})
            ai_cover.save_settings({'api_key': '', 'text_model': 'openai/gpt-5-mini'})
            assert ai_cover.settings()['api_key'] == API_KEY
            public = ai_cover.public_settings()
        assert API_KEY not in json.dumps(public)
        assert public['key_hint'] == '••••kkkk'
        assert public['text_model'] == 'openai/gpt-5-mini'

    def test_clear_key(self, app: Flask) -> None:
        with app.app_context():
            ai_cover.save_settings({'api_key': API_KEY})
            ai_cover.save_settings({'clear_key': True})
            assert ai_cover.settings()['api_key'] == ''

    def test_blank_model_goes_back_to_default(self, app: Flask) -> None:
        with app.app_context():
            ai_cover.save_settings({'image_model': ''})
            assert ai_cover.settings()['image_model'] == ai_cover.DEFAULT_IMAGE_MODEL

    @pytest.mark.parametrize('form', [
        {'text_model': 'not a model!'},
        {'fallback_models': 'a/b c/d e/f g/h i/j k/l'},
        {'api_key': 'has spaces in it'},
    ])
    def test_invalid_values_rejected(self, app: Flask, form: dict) -> None:
        with app.app_context(), pytest.raises(ValueError):
            ai_cover.save_settings(form)

    def test_fallbacks_from_lines(self, app: Flask) -> None:
        with app.app_context():
            ai_cover.save_settings({'fallback_models': 'google/gemini-image\n\nopenai/gpt-image, google/gemini-image'})
            assert ai_cover.settings()['fallback_models'] == ['google/gemini-image', 'openai/gpt-image']


@pytest.mark.unit
class TestPrompts:

    def test_saved_prompt_replaces_default_and_blank_restores_it(self, app: Flask) -> None:
        with app.app_context():
            ai_prompts.save({'cover_safer': 'Be gentle.'})
            assert ai_prompts.get('cover_safer') == 'Be gentle.'
            ai_prompts.save({'cover_safer': ''})
            assert ai_prompts.get('cover_safer') == ai_prompts.COVER_SAFER

    def test_picture_prompt_needs_description(self, app: Flask) -> None:
        with app.app_context(), pytest.raises(ValueError, match='description'):
            ai_prompts.save({'cover_image': 'A nice picture.'})


@pytest.mark.unit
class TestStoryDetails:

    def test_excerpts_include_metadata_and_part_starts(self, app: Flask, story) -> None:
        with app.app_context():
            text = ai_cover.story_details(_get_story(story), 'excerpts')
        assert text.startswith('Title: Harbour Lights\n')
        assert 'Description: Two old friends meet again at a seaside inn.' in text
        assert 'Tags: romance-' in text
        assert 'Part 1 (complete):' in text and 'Part 3 (first 20%):' in text
        assert 'Part 1 paragraph 9' in text      # all of part 1
        assert 'Part 3 paragraph 9' not in text  # only the start of later parts

    def test_opening_only(self, app: Flask, story) -> None:
        with app.app_context():
            text = ai_cover.story_details(_get_story(story), 'opening')
        assert 'Opening of the story:' in text
        assert len(text.split('Opening of the story:\n')[1].split()) == 1200

    def test_without_story_text(self) -> None:
        stub = SimpleNamespace(title='T', category=None, tags=[], description='', formats=[])
        assert 'not available' in ai_cover.story_details(stub, 'excerpts')


@pytest.mark.unit
class TestPainting:

    CONFIG = {**ai_cover.DEFAULTS, 'api_key': API_KEY, 'image_model': 'm/one', 'fallback_models': ['m/two']}

    def _story(self):
        return SimpleNamespace(id=1, title='Harbour Lights', author=SimpleNamespace(name='Ann'))

    def test_refused_lettered_cover_retried_without_lettering(self) -> None:
        calls = []

        def fake_paint(brief, key, model, title, author, lettered):
            calls.append((model, lettered))
            if lettered:
                raise ValueError('Blocked by the safety filter')
            return b'picture', 0.02

        with patch.object(ai_cover, 'paint', side_effect=fake_paint):
            picture, cost, used = ai_cover.paint_with_fallbacks(self._story(), 'scene', API_KEY, self.CONFIG)
        assert calls == [('m/one', True), ('m/two', True), ('m/one', False)]
        assert used['model'] == 'm/one' and used['lettered'] is False and len(used['attempts']) == 2

    def test_all_refused_tones_scene_down(self) -> None:
        def fake_paint(brief, key, model, title, author, lettered):
            if brief != 'softer scene':
                raise ValueError('content policy violation')
            return b'picture', 0.03

        with patch.object(ai_cover, 'paint', side_effect=fake_paint), \
             patch.object(ai_cover, 'soften', return_value=('softer scene', 0.001)) as soften:
            _, cost, used = ai_cover.paint_with_fallbacks(self._story(), 'scene', API_KEY, self.CONFIG)
        soften.assert_called_once()
        assert used['softened'] is True and cost == pytest.approx(0.031)

    def test_key_problem_stops_at_once(self) -> None:
        with patch.object(ai_cover, 'paint', side_effect=ai_cover.StopError('OpenRouter refused the API key.')) as paint:
            with pytest.raises(ai_cover.StopError):
                ai_cover.paint_with_fallbacks(self._story(), 'scene', API_KEY, self.CONFIG)
        assert paint.call_count == 1

    def test_paint_sends_3_by_4_and_converts_png(self) -> None:
        sent = {}

        def fake_post(url, payload, key, timeout=300):
            sent.update(url=url, payload=payload)
            return {'data': [{'b64_json': base64.b64encode(_png()).decode()}], 'usage': {'cost': 0.04}}

        with patch.object(ai_cover, '_post', side_effect=fake_post), patch.object(ai_cover, '_image_parameters', return_value={}):
            picture, cost = ai_cover.paint('A scene.', API_KEY, 'm/one', 'Harbour Lights', 'Ann', lettered=True)
        assert sent['url'] == ai_cover.IMAGES
        assert sent['payload']['aspect_ratio'] == '3:4'
        assert 'Harbour Lights' in sent['payload']['prompt'] and 'A scene.' in sent['payload']['prompt']
        assert picture[:3] == b'\xff\xd8\xff' and cost == 0.04

    def test_short_brief_is_an_error(self, app: Flask, story) -> None:
        reply = {'choices': [{'message': {'content': 'No.'}, 'finish_reason': 'stop'}]}
        with app.app_context(), patch.object(ai_cover, '_post', return_value=reply):
            with pytest.raises(ValueError, match='declined'):
                ai_cover.write_brief(_get_story(story), API_KEY, 'm/text')


@pytest.mark.unit
class TestCoverDrawing:

    def test_plain_cover_without_art(self, covers: Path) -> None:
        path = covers / '7_story.jpg'
        draw_story_cover('A Title', 'An Author', str(path))
        assert Image.open(path).size == (600, 800)

    @pytest.mark.parametrize('lettered', [False, True])
    def test_cover_drawn_from_art(self, covers: Path, lettered: bool) -> None:
        art, notes = ai_art_paths(str(covers), '7_story')
        os.makedirs(os.path.dirname(art))
        Path(art).write_bytes(_jpeg((200, 30, 30)))
        Path(notes).write_text(json.dumps({'lettered': lettered}))
        path = covers / '7_story.jpg'
        draw_story_cover('A Title', 'An Author', str(path))
        image = Image.open(path).convert('RGB')
        assert image.size == (600, 800)
        assert image.getpixel((300, 400))[0] > 150  # the red artwork shows through in the middle


@pytest.mark.unit
class TestMakeCover:

    def test_make_cover_saves_art_notes_and_cover(self, app: Flask, story, covers: Path) -> None:
        with app.app_context():
            ai_cover.save_settings({'api_key': API_KEY, 'lettering': 'litkeeper'})
            with patch.object(ai_cover, 'write_brief', return_value=('a scene ' * 20, 0.001)), \
                 patch.object(ai_cover, 'paint', return_value=(_jpeg(), 0.04)):
                ai_cover.make_cover(story)
            record = _get_story(story)
            info = ai_cover.info(record)
            art, notes = ai_cover.art_paths(record)
            assert info['has_art'] and info['cost'] == pytest.approx(0.041) and not info['lettered']
            assert os.path.exists(art) and os.path.exists(ai_cover.cover_path(record))
            assert API_KEY not in Path(notes).read_text()

            ai_cover.use_plain_cover(record)
            assert not os.path.exists(art) and os.path.exists(ai_cover.cover_path(record))
            assert ai_cover.info(record)['has_art'] is False

    def test_failure_is_recorded_and_notified(self, app: Flask, story, covers: Path) -> None:
        with app.app_context():
            ai_cover.save_settings({'api_key': API_KEY})
        with patch.object(ai_cover, 'make_cover', side_effect=ValueError('The image model returned no picture.')), \
             patch('app.services.notifier.send_notification') as notify:
            ai_cover._queue.append(story)
            ai_cover._running['working'] = True
            ai_cover._work(app)
        with app.app_context():
            assert ai_cover.info(_get_story(story))['error'] == 'The image model returned no picture.'
        notify.assert_called_once()
        assert notify.call_args.kwargs.get('is_error') is True

    def test_after_download_only_when_turned_on(self, app: Flask, story) -> None:
        with app.app_context(), patch.object(ai_cover, 'start') as start:
            ai_cover.save_settings({'api_key': API_KEY, 'auto': False})
            ai_cover.make_after_download(app, story)
            start.assert_not_called()
            ai_cover.save_settings({'auto': True})
            ai_cover.make_after_download(app, story)
            start.assert_called_once_with(app, story)


@pytest.mark.integration
class TestRoutes:

    def test_start_without_key(self, client: FlaskClient, story) -> None:
        response = client.post(f'/api/story/{story}/ai-cover')
        assert response.status_code == 400
        assert 'API key' in response.get_json()['message']

    def test_start_and_status(self, client: FlaskClient, app: Flask, story) -> None:
        with app.app_context():
            ai_cover.save_settings({'api_key': API_KEY})
        with patch.object(ai_cover, 'start', return_value='started') as start:
            response = client.post(f'/api/story/{story}/ai-cover')
        assert response.status_code == 200 and response.get_json()['state'] == 'started'
        start.assert_called_once()
        status = client.get(f'/api/story/{story}/ai-cover').get_json()
        assert status['configured'] is True and status['has_art'] is False

    def test_story_window_shows_ai_cover_button(self, client: FlaskClient, story) -> None:
        response = client.get(f'/api/story/{story}/modal')
        assert response.status_code == 200
        assert b'aiCoverBtn' in response.data

    def test_settings_save_never_returns_key(self, client: FlaskClient) -> None:
        response = client.post('/settings/ai-covers', json={'api_key': API_KEY, 'auto': True})
        body = response.get_json()
        assert response.status_code == 200 and body['settings']['has_key'] and body['settings']['auto']
        assert API_KEY not in response.get_data(as_text=True)
        assert API_KEY.encode() not in client.get('/settings/').data

    def test_settings_rejects_bad_model(self, client: FlaskClient) -> None:
        response = client.post('/settings/ai-covers', json={'image_model': 'bad model'})
        assert response.status_code == 400

    def test_check_models(self, client: FlaskClient) -> None:
        with patch.object(ai_cover, 'check_models', return_value=[(True, 'API key accepted.')]):
            body = client.post('/settings/ai-covers/check').get_json()
        assert body['results'] == [{'ok': True, 'message': 'API key accepted.'}]

    def test_settings_page_has_section(self, client: FlaskClient) -> None:
        response = client.get('/settings/')
        assert b'aiCoversSection' in response.data
