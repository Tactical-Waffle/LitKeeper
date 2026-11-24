"""Views and favorites in the library (under covers, the Detail layout, sorting), the story window's numbers and
rating stars, updates from Literotica in their own queue, the layout order, and covers the browser can keep."""
from __future__ import annotations
import os
import re
import time
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.services import metadata_update

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]

LITEROTICA = {'score': 4.71, 'views': 123456, 'favorites': 789, 'comments': 12,
              'description': 'Fresh blurb.', 'tags': ['romance', 'slow burn'], 'page_count': 3}


@pytest.fixture(autouse=True)
def empty_queue():
    def clear():
        with metadata_update._lock:
            metadata_update._queue.clear()
            metadata_update._running.clear()
            metadata_update._errors.clear()
    clear()
    yield
    clear()


@pytest.fixture
def stories(add_story):
    return {
        'popular': add_story('Popular', literotica_url='https://www.literotica.com/s/popular',
                             literotica_views=50000, literotica_favorites=900, rating=4),
        'quiet': add_story('Quiet', literotica_url='https://www.literotica.com/s/quiet',
                           literotica_views=1200, literotica_favorites=5000),
        'mine': add_story('Mine', source_type='custom'),
    }


def _titles(html: str) -> list:
    return re.findall(r'card-info-title[^>]*>([^<]+)<', html)


@pytest.mark.integration
class TestViewsAndFavoritesInTheLibrary:

    def test_sort_by_views_and_favorites(self, client: FlaskClient, stories) -> None:
        by_views = client.get('/library/filter?sort_by=views&sort_order=desc').get_data(as_text=True)
        assert _titles(by_views)[:2] == ['Popular', 'Quiet']
        by_favorites = client.get('/library/filter?sort_by=favorites&sort_order=desc').get_data(as_text=True)
        assert _titles(by_favorites)[:2] == ['Quiet', 'Popular']
        page = client.get('/').get_data(as_text=True)
        assert '<option value="views">Views</option>' in page and '<option value="favorites">Favorites</option>' in page

    def test_shown_under_covers_and_in_detail(self, client: FlaskClient, stories) -> None:
        html = client.get('/library/filter?sort_by=views&sort_order=desc').get_data(as_text=True)
        assert 'card-views' in html and '>50k' in html.replace('\n', '').replace(' ', '')
        assert '50k views' in html and '5k favorites' in html  # Detail layout.
        page = client.get('/').get_data(as_text=True)
        assert 'id="viewViews"' in page and 'id="viewFavorites"' in page

    def test_your_rating_only_once_rated(self, client: FlaskClient, stories) -> None:
        html = client.get('/library/filter?sort_by=views&sort_order=desc').get_data(as_text=True)
        rating_classes = dict(re.findall(r'data-story-id="(\d+)".*?class="card-rating-container card-user-rating ([^"]*)"', html, re.S))
        assert 'card-unrated' not in rating_classes[str(stories['popular'])]
        assert 'card-unrated' in rating_classes[str(stories['quiet'])]

    def test_layouts_in_order(self, client: FlaskClient, stories) -> None:
        page = client.get('/').get_data(as_text=True)
        assert page.index('data-layout="list"') < page.index('data-layout="detail"') < page.index('data-layout="covers"')


@pytest.mark.integration
class TestStoryWindow:

    def test_views_and_favorites_without_a_community_rating(self, client: FlaskClient, stories) -> None:
        html = client.get(f"/api/story/{stories['quiet']}/modal").get_data(as_text=True)
        assert '1,200 views' in html and '5,000 favorites' in html
        assert 'Update from Literotica' in html

    def test_rating_stars_only_once_rated(self, client: FlaskClient, stories) -> None:
        unrated = client.get(f"/api/story/{stories['quiet']}/modal").get_data(as_text=True)
        assert 'class="rate-story-btn  mb-3' in unrated and 'class="hidden flex items-center gap-0.5 mb-3" id="storyRatingWidget"' in unrated
        rated = client.get(f"/api/story/{stories['popular']}/modal").get_data(as_text=True)
        assert 'class="rate-story-btn hidden' in rated and 'class=" flex items-center gap-0.5 mb-3" id="storyRatingWidget"' in rated

    def test_no_update_button_for_your_own_stories(self, client: FlaskClient, stories) -> None:
        assert 'Update from Literotica' not in client.get(f"/api/story/{stories['mine']}/modal").get_data(as_text=True)
        assert client.post(f"/api/story/{stories['mine']}/literotica-update").status_code == 400


@pytest.mark.integration
class TestUpdatesFromLiterotica:

    def test_update_copies_the_numbers(self, app: Flask, stories) -> None:
        with app.app_context(), patch('app.services.story_downloader.fetch_story_metadata', return_value=dict(LITEROTICA)) as fetch, \
                patch('app.services.http_client.global_rate_limiter.wait_if_needed') as paced:
            changed = metadata_update.update(stories['quiet'])
            from app.models import Story, db
            story = db.session.get(Story, stories['quiet'])
            assert (story.literotica_score, story.literotica_views, story.literotica_favorites) == (4.71, 123456, 789)
            assert story.description == 'Fresh blurb.' and sorted(t.name for t in story.tags) == ['romance', 'slow burn']
            assert story.last_metadata_refresh is not None
        assert {'score', 'views', 'favorites', 'description', 'tags'} <= set(changed)
        assert paced.called and fetch.call_args.args[0] == 'https://www.literotica.com/s/quiet'

    def test_series_read_from_the_series_page(self, app: Flask, add_story) -> None:
        story_id = add_story('Saga', literotica_url='https://www.literotica.com/s/saga-ch-01',
                             literotica_series_url='https://www.literotica.com/series/se/123', chapter_count=3)
        with app.app_context():
            from app.models import Story, db
            assert metadata_update.source_url(db.session.get(Story, story_id)) == 'https://www.literotica.com/series/se/123'

    def test_nothing_from_literotica_is_an_error(self, app: Flask, stories) -> None:
        with app.app_context(), patch('app.services.story_downloader.fetch_story_metadata', return_value={}), \
                patch('app.services.http_client.global_rate_limiter.wait_if_needed'):
            with pytest.raises(ValueError, match='Literotica did not send'):
                metadata_update.update(stories['quiet'])

    def test_bulk_queues_them_and_skips_your_own(self, app: Flask, client: FlaskClient, stories) -> None:
        with patch.object(metadata_update, '_work'):
            data = client.post('/api/stories/bulk', json={'action': 'update_metadata', 'ids': list(stories.values())}).get_json()
        assert data['done'] == 2 and data['skipped'] == 1
        assert list(metadata_update._queue) == [stories['popular'], stories['quiet']]
        from app.services import ai_queues
        assert [i[0] for i in ai_queues.load('metadata')] == [stories['popular'], stories['quiet']]  # Saved.

    def test_queue_runs_separately_from_downloads(self, app: Flask, stories) -> None:
        done = []
        with patch.object(metadata_update, 'update', side_effect=done.append):
            metadata_update.start_many(app, [stories['popular'], stories['quiet']])
            for _ in range(100):
                if len(done) == 2 and not metadata_update._running:
                    break
                time.sleep(0.02)
        assert done == [stories['popular'], stories['quiet']]

    def test_queue_page_lists_and_clears_them(self, client: FlaskClient, stories) -> None:
        metadata_update._queue.extend([stories['popular'], stories['quiet']])
        html = client.get('/queue/partials/ai-covers').get_data(as_text=True)
        assert 'Updates from Literotica' in html and 'data-sortable="metadata"' in html and 'Quiet' in html
        assert client.post('/queue/api/order', json={'kind': 'metadata', 'ids': [stories['quiet']]}).get_json()['success']
        assert list(metadata_update._queue) == [stories['quiet'], stories['popular']]
        assert client.delete(f"/queue/api/ai/metadata/{stories['quiet']}").get_json()['success']
        assert client.delete('/queue/api/metadata').get_json()['removed'] == 1
        assert 'Updates from Literotica' not in client.get('/queue/partials/ai-covers').get_data(as_text=True)

    def test_story_window_button_queues_one(self, client: FlaskClient, stories) -> None:
        with patch.object(metadata_update, '_work'):
            data = client.post(f"/api/story/{stories['quiet']}/literotica-update").get_json()
        assert data['success'] and data['queued'] and data['views'] == 1200


@pytest.mark.integration
class TestCoversTheBrowserKeeps:

    def test_cards_ask_for_a_versioned_cover(self, app: Flask, client: FlaskClient, stories) -> None:
        from app.utils import get_cover_directory
        with app.app_context():
            from app.models import Story, db
            story = db.session.get(Story, stories['quiet'])
            path = os.path.join(get_cover_directory(), f'{story.id}_{story.filename_base}.jpg')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as handle:
            handle.write(b'\xff\xd8\xff\xd9')
        html = client.get('/library/filter?sort_by=views&sort_order=desc').get_data(as_text=True)
        version = int(os.path.getmtime(path))
        assert f"/api/story/{stories['quiet']}/cover?v={version}" in html
        kept = client.get(f"/api/story/{stories['quiet']}/cover?v={version}")
        assert kept.status_code == 200 and 'immutable' in kept.headers['Cache-Control']
        assert client.get(f"/api/story/{stories['quiet']}/cover").headers['Cache-Control'] == 'no-cache'
