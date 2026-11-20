"""The author window: Literotica's works feed, series with their parts, picking some parts,
the story filter terms and the numbers to sort by."""
from __future__ import annotations
import json
import uuid
import pytest
from unittest.mock import patch, MagicMock
from flask import Flask
from flask.testing import FlaskClient

from app.services import author_scraper

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]


def _work(slug, title, rating=4.5, views=1000, favs=10, saves=5, comments=2, words=3000, date='01/02/2024',
          page='fetish-stories', tags=('voyeur',), name=None):
    info = {'type': 'story', 'pageUrl': page}
    if name:
        info['name'] = name
    return {'url': slug, 'title': title, 'type': 'story', 'rate_all': rating, 'view_count': views,
            'favorite_count': favs, 'reading_lists_count': saves, 'comment_count': comments,
            'words_count': words, 'date_approve': date, 'category_info': info,
            'description': f'About {title}', 'tags': [{'id': 1, 'tag': t} for t in tags]}


PAGE_1 = {'current_page': 1, 'last_page': 2, 'total': 3, 'per_page': 50, 'data': [
    _work('a-birthday-surprise-34', 'A Birthday Surprise', rating=4.43, views=6509, date='11/18/2026'),
    {'id': 493789737, 'title': 'The Best of Two Marriages', 'work_count': 2, 'description': None, 'parts': [
        _work('the-best-of-two-marriages', 'The Best of Two Marriages', rating=4.6, views=133118, favs=236,
              saves=475, date='04/04/2023', page='loving-wives', name='Loving Wives', tags=('wife',)),
        _work('the-best-of-two-marriages-ch-02', 'The Best of Two Marriages Ch. 02', rating=4.8, views=37130,
              favs=90, saves=110, date='11/11/2023', page='taboo-sex-stories', name='Taboo / Incest', tags=('sisters',)),
    ]},
]}
PAGE_2 = {'current_page': 2, 'last_page': 2, 'total': 3, 'per_page': 50, 'data': [
    _work('new-medicine-for-ed', 'New Medicine For ED', rating=4.08, date='07/18/2026', page='erotic-couplings'),
]}


def _session(pages):
    session = MagicMock()
    replies = []
    for page in pages:
        reply = MagicMock()
        reply.json.return_value = page
        reply.raise_for_status.return_value = None
        replies.append(reply)
    session.get.side_effect = replies
    return session


@pytest.mark.unit
class TestWorksFeed:

    def test_reads_every_page_with_series_and_numbers(self) -> None:
        session = _session([PAGE_1, PAGE_2])
        with patch.object(author_scraper.time, 'sleep'):
            entries = author_scraper.fetch_author_works('Someone', session=session)
        assert [e['title'] for e in entries] == ['A Birthday Surprise', 'The Best of Two Marriages', 'New Medicine For ED']
        first_url = session.get.call_args_list[0].args[0]
        assert '/api/3/users/Someone/series_and_works?params=' in first_url
        assert '%22page%22%3A1' in first_url and '%22page%22%3A2' in session.get.call_args_list[1].args[0]

        story = entries[0]
        assert story['url'] == 'https://www.literotica.com/s/a-birthday-surprise-34'
        assert (story['rating'], story['views'], story['favorites'], story['saves'], story['comments'], story['words']) == \
            (4.43, 6509, 10, 5, 2, 3000)
        assert story['category'] == 'Fetish' and story['tags'] == ['voyeur']
        # The date belongs to this story (the old page reading sometimes took a neighbour's).
        assert entries[2]['date_approve'] == '07/18/2026'

        series = entries[1]
        assert series['is_series'] and series['url'] == 'https://www.literotica.com/series/se/493789737'
        assert [p['title'] for p in series['parts']] == ['The Best of Two Marriages', 'The Best of Two Marriages Ch. 02']
        assert series['views'] == 133118 + 37130 and series['saves'] == 585 and series['favorites'] == 326
        assert series['rating'] == pytest.approx(4.7)
        assert series['date_approve'] == '11/11/2023'
        assert series['category'] == 'Multiple categories'
        assert series['description'] == 'About The Best of Two Marriages'

    def test_first_page_failure_means_fall_back(self) -> None:
        session = MagicMock()
        session.get.side_effect = RuntimeError('blocked')
        assert author_scraper.fetch_author_works('Someone', session=session) is None

    def test_username(self) -> None:
        assert author_scraper.author_username('https://www.literotica.com/authors/Cuckvoyeurfan/works/stories') == 'Cuckvoyeurfan'
        assert author_scraper.author_username('https://www.literotica.com/stories/memberpage.php?uid=1&page=submissions') is None


@pytest.fixture
def clean_queue(app: Flask):
    """Queue items and authors this file adds are removed again (the test database is shared)."""
    from app.models import DownloadQueueItem, Author, AppConfig, db
    marker = f'https://www.literotica.com/s/test-{uuid.uuid4().hex[:8]}'
    yield marker
    with app.app_context():
        DownloadQueueItem.query.filter(DownloadQueueItem.url.like(marker + '%')).delete(synchronize_session=False)
        Author.query.filter_by(name='WindowTestAuthor').delete()
        AppConfig.query.filter_by(key='story_filter_terms').delete()
        db.session.commit()


@pytest.mark.integration
class TestAuthorWindowRoutes:

    def test_preview_uses_feed_and_marks_parts(self, app: Flask, client: FlaskClient, clean_queue) -> None:
        with patch.object(author_scraper.time, 'sleep'):
            feed = author_scraper.fetch_author_works('WindowTestAuthor', session=_session([PAGE_1, PAGE_2]))
        from app.models import SeenLiteroticaUrl, AppConfig, db
        part_url = 'https://www.literotica.com/s/the-best-of-two-marriages'
        with app.app_context():
            if not SeenLiteroticaUrl.query.filter_by(url=part_url).first():
                db.session.add(SeenLiteroticaUrl(url=part_url))
            cfg = AppConfig(key='story_filter_terms', value_type='json', description='')
            cfg.set_value(['sisters'])
            db.session.add(cfg)
            db.session.commit()
        try:
            with patch('app.services.author_scraper.fetch_author_works', return_value=feed) as fetch, \
                 patch('app.services.author_scraper.AuthorScraper.scrape_story_list_with_metadata') as old_way:
                data = client.post('/api/authors/preview',
                                   json={'author_url': 'https://www.literotica.com/authors/WindowTestAuthor/works/stories'}).get_json()
            fetch.assert_called_once_with('WindowTestAuthor')
            old_way.assert_not_called()
            assert data['success'] and data['author_name'] == 'WindowTestAuthor'
            assert data['filter_terms'] == ['sisters']
            series = data['stories'][1]
            assert [p['in_library'] for p in series['parts']] == [True, False]
            assert series['in_library'] is False
        finally:
            with app.app_context():
                SeenLiteroticaUrl.query.filter_by(url=part_url).delete()
                db.session.commit()

    def test_preview_falls_back_to_page_reading(self, client: FlaskClient) -> None:
        old = [{'url': 'https://www.literotica.com/s/x', 'title': 'X', 'is_series': False}]
        with patch('app.services.author_scraper.fetch_author_works', return_value=None), \
             patch('app.services.author_scraper.AuthorScraper.scrape_story_list_with_metadata', return_value=old):
            data = client.post('/api/authors/preview',
                               json={'author_url': 'https://www.literotica.com/authors/Someone'}).get_json()
        assert data['stories'][0]['title'] == 'X'

    def test_queue_whole_items_and_picked_parts(self, app: Flask, client: FlaskClient, clean_queue) -> None:
        base = clean_queue
        app.download_worker = MagicMock()
        body = {
            'author_url': 'https://www.literotica.com/authors/WindowTestAuthor',
            'story_urls': [f'{base}-story'],
            'part_groups': [[f'{base}-one-part'], [f'{base}-p1', f'{base}-p3']],
            'known_urls': [f'{base}-story', f'{base}-skipped-on-purpose'],
            'watch': True,
        }
        data = client.post('/api/authors/queue-stories', json=body).get_json()
        assert data['success'], data
        from app.models import DownloadQueueItem, Author
        with app.app_context():
            items = {i.url: i for i in DownloadQueueItem.query.filter(DownloadQueueItem.url.like(base + '%')).all()}
            assert items[f'{base}-story'].job_type == 'single'
            assert items[f'{base}-one-part'].job_type == 'part'
            assert items[f'{base}-p1'].job_type == 'parts'
            assert items[f'{base}-p1'].get_extra_urls() == [f'{base}-p3']
            known = Author.query.filter_by(name='WindowTestAuthor').first().get_known_story_urls()
            # Stories left out on purpose count as known, so watching won't fetch them later.
            assert f'{base}-skipped-on-purpose' in known and f'{base}-p3' in known

    def test_queue_needs_something(self, client: FlaskClient) -> None:
        response = client.post('/api/authors/queue-stories', json={'author_url': 'https://www.literotica.com/authors/A'})
        assert response.status_code == 400


@pytest.mark.integration
class TestStoryFilterSetting:

    def test_save_and_show(self, client: FlaskClient, clean_queue) -> None:
        response = client.post('/settings/story-filter', json={'terms': 'incest\n non-consent, incest ,\n\nmind control'})
        assert response.get_json() == {'success': True, 'terms': ['incest', 'non-consent', 'mind control']}
        page = client.get('/settings/').get_data(as_text=True)
        assert 'id="storyFilterSection"' in page and 'incest\nnon-consent\nmind control' in page

    def test_limits(self, client: FlaskClient, clean_queue) -> None:
        assert client.post('/settings/story-filter', json={'terms': 'x' * 101}).status_code == 400
        assert client.post('/settings/story-filter', json={'terms': ','.join(f't{i}' for i in range(201))}).status_code == 400
        assert client.post('/settings/story-filter', json={'terms': ''}).get_json()['terms'] == []


@pytest.mark.unit
class TestDownloadingSomeParts:

    def test_part_job_downloads_only_that_part(self, app: Flask) -> None:
        from app.services.download_queue_worker import DownloadQueueWorker
        worker = DownloadQueueWorker(app)
        item = MagicMock(job_type='part', url='https://www.literotica.com/s/x-ch-02')
        with app.app_context(), \
             patch('app.models.SeenLiteroticaUrl') as seen, \
             patch('app.services.story_downloader.download_story', return_value=(None,) * 10) as download:
            seen.query.filter_by.return_value.first.return_value = None
            with pytest.raises(Exception):
                worker._download_and_save(item)
        download.assert_called_once_with('https://www.literotica.com/s/x-ch-02', only_this_part=True)

    def test_parts_job_combines_only_those_parts(self, app: Flask) -> None:
        from app.services.download_queue_worker import DownloadQueueWorker
        worker = DownloadQueueWorker(app)
        item = MagicMock(job_type='parts', url='https://www.literotica.com/s/a')
        item.get_extra_urls.return_value = ['https://www.literotica.com/s/c']
        with app.app_context(), \
             patch('app.services.story_downloader.download_and_combine_stories', return_value=(None,) * 12) as combine:
            with pytest.raises(Exception):
                worker._download_and_save_multi(item)
        combine.assert_called_once_with(['https://www.literotica.com/s/a', 'https://www.literotica.com/s/c'],
                                        only_these_parts=True)

    def test_only_this_part_skips_the_series(self) -> None:
        from app.services import story_downloader
        with patch.object(story_downloader, 'extract_series_url_from_chapter') as find_series, \
             patch.object(story_downloader, 'get_session') as session:
            session.return_value.get.side_effect = RuntimeError('stop here')
            story_downloader.download_story('https://www.literotica.com/s/x-ch-02', only_this_part=True)
            find_series.assert_not_called()
            story_downloader.download_story('https://www.literotica.com/s/x-ch-02')
            find_series.assert_called_once()
