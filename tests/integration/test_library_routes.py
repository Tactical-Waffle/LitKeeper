from __future__ import annotations
import pytest
import json
from pathlib import Path
from flask.testing import FlaskClient
from flask import Flask
from unittest.mock import patch, MagicMock
from app.services.story_processor import StoryProcessingResult


@pytest.mark.integration
class TestIndexRoute:
    """Test / (index) route with GET and POST requests."""

    def test_index_get_renders(self, client: FlaskClient) -> None:
        """GET / renders index page successfully."""
        response = client.get('/')

        assert response.status_code == 200
        assert b'<!DOCTYPE html>' in response.data

    def test_index_shows_stories(self, client: FlaskClient, add_story) -> None:
        """Stories in the library appear on the library page."""
        add_story('Test Story', author='Test Author')
        response = client.get('/')
        assert response.status_code == 200
        assert b'Test Story' in response.data

    def test_index_post_download(self, client: FlaskClient) -> None:
        """POST / triggers story download."""
        with patch('app.blueprints.library.routes.download_story_and_create_files') as mock_download:
            mock_download.return_value = StoryProcessingResult(
                success=True,
                message="Downloaded successfully",
                title="New Story",
                formats=["epub"]
            )

            response = client.post(
                '/',
                data={
                    'url': 'https://www.literotica.com/s/test-story',
                    'format': 'epub'
                }
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['success'] == 'true'
            mock_download.assert_called_once()

    def test_index_validation_error_shown(self, client: FlaskClient) -> None:
        """Validation errors are displayed on index page."""
        response = client.post(
            '/',
            data={'url': ''}
        )

        assert response.status_code == 200
        assert b'url' in response.data.lower()

    def test_index_invalid_url_error(self, client: FlaskClient) -> None:
        """Invalid URL shows error message."""
        response = client.post(
            '/',
            data={'url': 'https://www.example.com/story'}
        )

        assert response.status_code == 200
        assert b'error' in response.data.lower() or b'literotica' in response.data.lower()

    def test_index_multiple_formats(self, client: FlaskClient) -> None:
        """POST / with multiple formats."""
        with patch('app.blueprints.library.routes.download_story_and_create_files') as mock_download:
            mock_download.return_value = StoryProcessingResult(
                success=True,
                message="Downloaded",
                formats=["epub", "html"]
            )

            response = client.post(
                '/',
                data={
                    'url': 'https://www.literotica.com/s/test',
                    'format': ['epub', 'html']
                }
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['success'] == 'true'


@pytest.mark.integration
class TestLibraryFilter:
    """Test /library/filter (search and category)."""

    def test_filter_search_by_title(self, client: FlaskClient, add_story) -> None:
        add_story('Romance Story', author='Author A')
        add_story('Sci-Fi Adventure', author='Author B')
        response = client.get('/library/filter?search=romance')
        assert response.status_code == 200
        assert b'Romance Story' in response.data
        assert b'Sci-Fi Adventure' not in response.data

    def test_filter_search_by_author(self, client: FlaskClient, add_story) -> None:
        add_story('Story One', author='John Smith')
        add_story('Story Two', author='Jane Doe')
        response = client.get('/library/filter?search=john')
        assert b'Story One' in response.data
        assert b'Story Two' not in response.data

    def test_filter_by_category(self, client: FlaskClient, add_story) -> None:
        add_story('Love Letters', category='Romance')
        add_story('Space Ships', category='Sci-Fi & Fantasy')
        response = client.get('/library/filter?category=Romance')
        assert b'Love Letters' in response.data
        assert b'Space Ships' not in response.data

    def test_filter_uncategorized(self, client: FlaskClient, add_story) -> None:
        add_story('Categorized Story', category='Romance')
        add_story('Uncategorized Story')
        response = client.get('/library/filter?category=uncategorized')
        assert b'Uncategorized Story' in response.data
        assert b'Categorized Story' not in response.data

    def test_filter_combined(self, client: FlaskClient, add_story) -> None:
        add_story('Romance Story', category='Romance')
        add_story('Romance in Space', category='Sci-Fi & Fantasy')
        add_story('SciFi Story', category='Sci-Fi & Fantasy')
        response = client.get('/library/filter?search=romance&category=Romance')
        assert b'Romance Story' in response.data
        assert b'Romance in Space' not in response.data and b'SciFi Story' not in response.data

    def test_filter_case_insensitive_search(self, client: FlaskClient, add_story) -> None:
        add_story('Test Story')
        response = client.get('/library/filter?search=TEST')
        assert b'Test Story' in response.data


@pytest.mark.integration
class TestReadStory:
    """Test /read/<story id> (the web reader)."""

    def test_read_story(self, client: FlaskClient, add_story) -> None:
        story_id = add_story('Test Story', author='Test Author')
        response = client.get(f'/read/{story_id}')
        assert response.status_code == 200
        assert b'Test Story' in response.data and b'Test Author' in response.data
        assert b'First paragraph.' in response.data

    def test_read_story_by_name_is_not_a_route(self, client: FlaskClient) -> None:
        """Stories are opened by id; file names and paths are never looked up."""
        for path in ('/read/test-story.json', '/read/story.html', '/read/../../../etc/passwd', '/read/story.txt'):
            assert client.get(path, follow_redirects=True).status_code == 404

    def test_read_story_absolute_path_blocked(self, client: FlaskClient) -> None:
        response = client.get('/read//etc/passwd', follow_redirects=True)
        assert response.status_code == 404

    def test_read_story_missing(self, client: FlaskClient, add_story) -> None:
        assert client.get('/read/999999').status_code == 404
        no_reader_file = add_story('EPUB Only', formats=('epub',))
        assert client.get(f'/read/{no_reader_file}').status_code == 404

    def test_read_story_malformed_json_error(self, client: FlaskClient, app: Flask, add_story) -> None:
        story_id = add_story('Bad Story')
        with app.app_context():
            from app.models import StoryFormat
            path = StoryFormat.query.filter_by(story_id=story_id, format_type='json').first().file_path
        Path(path).write_text('{ this is not valid json }')
        assert client.get(f'/read/{story_id}').status_code == 500
