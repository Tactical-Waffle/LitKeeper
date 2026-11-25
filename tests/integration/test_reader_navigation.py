"""The reader's way around a long story (Library button, previous and next part, contents, one part at a time,
tapping to turn pages) and the story window's Mark as Read and close button on phones."""
from __future__ import annotations
import pytest
from flask import Flask
from flask.testing import FlaskClient

pytestmark = [pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore::sqlalchemy.exc.LegacyAPIWarning')]

PARTS = [{'number': n, 'title': f'Part {n}', 'paragraphs': [f'Paragraph of part {n}.']} for n in (1, 2, 3)]


@pytest.mark.integration
class TestReader:

    def test_parts_link_to_each_other_and_to_the_library(self, add_story, client: FlaskClient) -> None:
        story_id = add_story('Long One', chapters=PARTS)
        page = client.get(f'/read/{story_id}').get_data(as_text=True)
        assert 'data-library-link' in page and 'Back to Library' in page
        assert 'id="contentsPanel"' in page and 'id="contentsToggle"' in page
        assert page.count('Previous part') == 4 and page.count('Next part') == 4  # Under each heading, at each end.
        assert 'data-part-link="2"' in page and 'id="partModeToggle"' in page and 'id="tapPagingToggle"' in page
        assert 'maximum-scale=1.0' in page

    def test_a_single_part_story_has_no_part_navigation(self, add_story, client: FlaskClient) -> None:
        story_id = add_story('Short One')
        page = client.get(f'/read/{story_id}').get_data(as_text=True)
        assert 'Previous part' not in page and 'contentsPanel' not in page and 'partModeToggle' not in page
        assert 'id="tapPagingToggle"' in page and 'Back to Library' in page


@pytest.mark.integration
class TestStoryWindow:

    def test_mark_as_read_follows_the_story(self, add_story, client: FlaskClient) -> None:
        story_id = add_story('Unread One')
        page = client.get(f'/api/story/{story_id}/modal').get_data(as_text=True)
        assert 'toggleStoryRead' in page and 'data-read="false"' in page and 'Mark as Read' in page
        assert client.post(f'/api/story/{story_id}/read', json={'read': True}).get_json()['read'] is True
        page = client.get(f'/api/story/{story_id}/modal').get_data(as_text=True)
        assert 'data-read="true"' in page and 'Mark as Unread' in page and '✓ Read' in page

    def test_phones_get_a_close_button(self, add_story, client: FlaskClient) -> None:
        story_id = add_story('Any One')
        page = client.get(f'/api/story/{story_id}/modal').get_data(as_text=True)
        handle = page.split('id="sheetDragHandle"', 1)[1].split('</div>\n', 2)
        assert 'closeStoryModal()' in ''.join(handle[:2]) and 'aria-label="Close"' in ''.join(handle[:2])
