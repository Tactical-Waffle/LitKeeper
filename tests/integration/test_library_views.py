"""Library: several categories at once, "fewer than" ratings, read/unread, word count and the
list/detail information on cards, the header's Queue button and the story window's full-size cover."""
from __future__ import annotations
import re
import pytest
from flask import Flask
from flask.testing import FlaskClient


def _titles(html: bytes) -> list[str]:
    return [t.decode() for t in re.findall(rb'<p class="card-info-title[^"]*">([^<]+)</p>', html)]


@pytest.fixture
def library(add_story, app: Flask):
    ids = {
        'romance': add_story('Love Letters', category='Romance', rating=5, literotica_score=4.8, word_count=12000),
        'scifi': add_story('Space Ships', category='Sci-Fi & Fantasy', rating=3, literotica_score=4.2, word_count=800),
        'fetish': add_story('Odd Tastes', category='Fetish', rating=2, literotica_score=3.9),
        'none': add_story('No Category'),
    }
    from app.models import ReadingProgress, db
    with app.app_context():
        db.session.add(ReadingProgress(story_id=ids['romance'], is_completed=True, percentage=1.0))
        db.session.add(ReadingProgress(story_id=ids['scifi'], is_completed=False, percentage=0.4))
        db.session.commit()
    return ids


@pytest.mark.integration
class TestLibraryFilters:

    def test_several_categories(self, client: FlaskClient, library) -> None:
        html = client.get('/library/filter?category=Romance&category=Fetish').data
        assert sorted(_titles(html)) == ['Love Letters', 'Odd Tastes']
        html = client.get('/library/filter?category=uncategorized&category=Sci-Fi+%26+Fantasy').data
        assert sorted(_titles(html)) == ['No Category', 'Space Ships']
        assert len(_titles(client.get('/library/filter?category=all').data)) == 4

    def test_fewer_stars(self, client: FlaskClient, library) -> None:
        assert sorted(_titles(client.get('/library/filter?user_rating=below-4').data)) == ['Odd Tastes', 'Space Ships']
        assert _titles(client.get('/library/filter?user_rating=4').data) == ['Love Letters']
        assert _titles(client.get('/library/filter?user_rating=unrated').data) == ['No Category']

    def test_community_score_below(self, client: FlaskClient, library) -> None:
        assert _titles(client.get('/library/filter?min_community_score=below-4.0').data) == ['Odd Tastes']
        assert _titles(client.get('/library/filter?min_community_score=4.5').data) == ['Love Letters']

    def test_read_status(self, client: FlaskClient, library) -> None:
        assert _titles(client.get('/library/filter?read_status=read').data) == ['Love Letters']
        assert _titles(client.get('/library/filter?read_status=reading').data) == ['Space Ships']
        assert sorted(_titles(client.get('/library/filter?read_status=unread').data)) == ['No Category', 'Odd Tastes', 'Space Ships']

    def test_filters_combine_with_search_and_carry_to_next_page(self, client: FlaskClient, library, add_story) -> None:
        assert _titles(client.get('/library/filter?search=letters&category=Romance&read_status=read').data) == ['Love Letters']
        for n in range(45):
            add_story(f'Romance {n}', category='Romance')
        html = client.get('/library/filter?category=Romance&category=Fetish&read_status=unread').data
        next_url = re.search(rb'hx-get="(/library/filter\?[^"]+)"', html).group(1).decode().replace('&amp;', '&')
        assert 'category=Romance' in next_url and 'category=Fetish' in next_url and 'read_status=unread' in next_url


@pytest.mark.integration
class TestCardsAndPage:

    def test_card_shows_word_count_and_list_details(self, client: FlaskClient, library) -> None:
        html = client.get('/library/filter?category=Romance').get_data(as_text=True)
        assert 'story-word-count-label' in html and '12.0k words' in html
        assert 'story-page-length-label' not in html
        assert 'card-info' in html and 'Read</span>' in html
        assert 'card-community-score-paren' not in html and '(4.8)' not in html
        html = client.get('/library/filter?category=Sci-Fi+%26+Fantasy').get_data(as_text=True)
        assert '40% read' in html

    def test_library_page_controls(self, client: FlaskClient, library) -> None:
        page = client.get('/').get_data(as_text=True)
        assert 'Download History' not in page
        for marker in ('id="clearFiltersBtn"', 'id="readStatusFilter"', 'class="category-filter-box',
                       'value="below-4"', 'data-layout="list"', 'data-layout="detail"', 'id="viewWordCount"'):
            assert marker in page

    def test_header_queue_button_counts_active_downloads(self, client: FlaskClient, app: Flask) -> None:
        from app.models import DownloadQueueItem, db
        with app.app_context():
            for status in ('pending', 'processing', 'completed'):
                db.session.add(DownloadQueueItem(url=f'https://www.literotica.com/s/q-{status}', status=status, formats='["epub"]'))
            db.session.commit()
        page = client.get('/').get_data(as_text=True)
        button = re.search(r'aria-label="Queue" title="Download queue">.*?</a>', page, re.S).group(0)
        assert '>2</span>' in button

    def test_queue_page_is_compact(self, client: FlaskClient) -> None:
        page = client.get('/queue/').get_data(as_text=True)
        assert '<title>Queue - LitKeeper</title>' in page
        assert 'queue-list' in page and 'lg:grid-cols-4' not in page

    def test_story_window_cover_opens_full_size(self, client: FlaskClient, library) -> None:
        html = client.get(f"/api/story/{library['romance']}/modal").get_data(as_text=True)
        assert 'openCoverViewer(this.src, this.alt)' in html


@pytest.mark.integration
class TestSelectAll:

    def test_ids_of_every_matching_story_beyond_the_first_page(self, client: FlaskClient, library, add_story) -> None:
        for n in range(45):
            add_story(f'Romance Extra {n}', category='Romance')
        data = client.get('/library/filter?category=Romance&ids_only=1&page=2').get_json()
        assert data['total'] == 46 and len(data['ids']) == 46  # All of them, whatever page was asked for.
        assert library['romance'] in data['ids'] and library['scifi'] not in data['ids']
        data = client.get('/library/filter?search=letters&ids_only=1').get_json()
        assert data['ids'] == [library['romance']]

    def test_select_all_button_beside_select_all_shown(self, client: FlaskClient, library) -> None:
        page = client.get('/').get_data(as_text=True)
        assert page.index('selectAllShown()') < page.index('id="selectAllBtn"')
