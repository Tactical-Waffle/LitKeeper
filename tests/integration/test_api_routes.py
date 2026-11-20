from __future__ import annotations
import pytest
import json
import os
from pathlib import Path
from flask.testing import FlaskClient
from flask import Flask
from unittest.mock import patch, MagicMock
from app.services.story_processor import StoryProcessingResult


@pytest.mark.integration
class TestDownloadEndpoint:
    """Test /api/download endpoint with GET and POST requests."""

    def test_download_get_with_url(self, client: FlaskClient) -> None:
        """GET /api/download with URL parameter."""
        with patch('app.blueprints.api.routes.download_story_and_create_files') as mock_download:
            mock_download.return_value = StoryProcessingResult(
                success=True,
                message="Story downloaded successfully",
                title="Test Story",
                author="Test Author",
                formats=["epub"],
                files=["test-story.epub"]
            )

            response = client.get('/api/download?url=https://www.literotica.com/s/test-story')

            assert response.status_code == 200
            data = response.get_json()
            assert data['success'] == 'true'
            assert data['title'] == 'Test Story'
            mock_download.assert_called_once()

    def test_download_get_with_format_param(self, client: FlaskClient) -> None:
        """GET /api/download with format parameter (comma-separated)."""
        with patch('app.blueprints.api.routes.download_story_and_create_files') as mock_download:
            mock_download.return_value = StoryProcessingResult(
                success=True,
                message="Story downloaded",
                formats=["epub", "html"],
                files=["test.epub", "test.html"]
            )

            response = client.get('/api/download?url=https://www.literotica.com/s/test&format=epub,html')

            assert response.status_code == 200
            args, kwargs = mock_download.call_args
            assert 'epub' in args[1]
            assert 'html' in args[1]

    def test_download_post_json(self, client: FlaskClient) -> None:
        """POST /api/download with JSON body."""
        with patch('app.blueprints.api.routes.download_story_and_create_files') as mock_download:
            mock_download.return_value = StoryProcessingResult(
                success=True,
                message="Downloaded",
                formats=["epub"],
                files=["story.epub"]
            )

            response = client.post(
                '/api/download',
                json={
                    'url': 'https://www.literotica.com/s/test-story',
                    'format': ['epub']
                },
                content_type='application/json'
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['success'] == 'true'

    def test_download_post_form_data(self, client: FlaskClient) -> None:
        """POST /api/download with form data."""
        with patch('app.blueprints.api.routes.download_story_and_create_files') as mock_download:
            mock_download.return_value = StoryProcessingResult(
                success=True,
                message="Downloaded"
            )

            response = client.post(
                '/api/download',
                data={
                    'url': 'https://www.literotica.com/s/test-story',
                    'format': 'epub'
                }
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['success'] == 'true'

    def test_download_post_multiple_formats(self, client: FlaskClient) -> None:
        """POST /api/download with multiple formats."""
        with patch('app.blueprints.api.routes.download_story_and_create_files') as mock_download:
            mock_download.return_value = StoryProcessingResult(
                success=True,
                message="Both formats created",
                formats=["epub", "html"],
                files=["test.epub", "test.html"]
            )

            response = client.post(
                '/api/download',
                json={
                    'url': 'https://www.literotica.com/s/test',
                    'format': ['epub', 'html']
                }
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['success'] == 'true'
            assert 'formats' in data
            assert 'files' in data

    def test_download_validation_error_empty_url(self, client: FlaskClient) -> None:
        """Empty URL returns 400 validation error."""
        response = client.post('/api/download', json={'url': ''})

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False  # Errors return a real boolean; successful downloads keep the documented "true".
        assert 'url' in data['message'].lower()

    def test_download_validation_error_invalid_domain(self, client: FlaskClient) -> None:
        """Non-Literotica URL returns 400 validation error."""
        response = client.post(
            '/api/download',
            json={'url': 'https://www.example.com/story'}
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False  # Errors return a real boolean; successful downloads keep the documented "true".

    def test_download_validation_error_invalid_format(self, client: FlaskClient) -> None:
        """Invalid format returns 400 validation error."""
        response = client.post(
            '/api/download',
            json={
                'url': 'https://www.literotica.com/s/test',
                'format': ['pdf']
            }
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data['success'] is False  # Errors return a real boolean; successful downloads keep the documented "true".

    def test_download_wait_false_async(self, app: Flask, client: FlaskClient) -> None:
        """wait=false adds the story to the download queue and wakes the queue worker."""
        app.download_worker = MagicMock()
        response = client.post('/api/download', json={'url': 'https://www.literotica.com/s/test', 'wait': False})

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'queue' in data['message'].lower()
        assert data['queue_item']['url'] == 'https://www.literotica.com/s/test'
        app.download_worker.wake.assert_called_once()

    def test_download_wait_string_parsing(self, app: Flask, client: FlaskClient) -> None:
        """wait parameter as string 'false' is parsed correctly."""
        app.download_worker = MagicMock()
        with patch('app.blueprints.api.routes.download_story_and_create_files') as download_now:
            response = client.post('/api/download', json={'url': 'https://www.literotica.com/s/test', 'wait': 'false'})

        assert response.status_code == 200
        assert 'queue' in response.get_json()['message'].lower()
        download_now.assert_not_called()

    def test_download_get_method_with_params(self, client: FlaskClient) -> None:
        """GET method parses query parameters correctly."""
        with patch('app.blueprints.api.routes.download_story_and_create_files') as mock_download:
            mock_download.return_value = StoryProcessingResult(success=True, message="OK")

            response = client.get(
                '/api/download?url=https://www.literotica.com/s/test&wait=true&format=epub'
            )

            assert response.status_code == 200
            args = mock_download.call_args[0]
            assert args[0] == 'https://www.literotica.com/s/test'
            assert 'epub' in args[1]


@pytest.mark.integration
class TestLibraryEndpoint:
    """Test /api/library endpoint."""

    def test_library_get_empty(self, client: FlaskClient) -> None:
        """Empty library returns empty array."""
        response = client.get('/api/library')

        assert response.status_code == 200
        data = response.get_json()
        assert 'stories' in data
        assert isinstance(data['stories'], list)
        assert len(data['stories']) == 0

    def test_library_get_with_epub_only(self, client: FlaskClient, add_story) -> None:
        """A story saved as EPUB only."""
        add_story('Test Story', formats=('epub',))
        data = client.get('/api/library').get_json()
        assert len(data['stories']) == 1
        assert data['stories'][0]['filename_base'] == 'test-story'
        assert data['stories'][0]['formats'] == ['epub']

    def test_library_get_with_json_metadata(self, client: FlaskClient, add_story) -> None:
        """Title, author, category and tags come from the story's record."""
        add_story('Test Story Title', author='Test Author', category='Romance', tags=('love', 'drama'))
        story = client.get('/api/library').get_json()['stories'][0]
        assert story['title'] == 'Test Story Title'
        assert story['author'] == 'Test Author'
        assert story['category'] == 'Romance'
        assert sorted(story['tags']) == ['drama', 'love']
        assert story['cover'] == f"{story['id']}_test-story-title.jpg"

    def test_library_json_structure(self, client: FlaskClient, add_story) -> None:
        """Library JSON has correct structure."""
        add_story('Story', author='Author')
        story = client.get('/api/library').get_json()['stories'][0]
        for key in ('filename_base', 'title', 'formats', 'created_at'):
            assert key in story
        assert isinstance(story['formats'], list)

    def test_library_sorts_by_date_descending(self, client: FlaskClient, add_story) -> None:
        """Newest first."""
        from datetime import datetime
        add_story('Old Story', created_at=datetime(2024, 1, 1))
        add_story('New Story', created_at=datetime(2025, 1, 1))
        data = client.get('/api/library').get_json()
        assert [s['filename_base'] for s in data['stories']] == ['new-story', 'old-story']

    def test_library_handles_both_formats(self, client: FlaskClient, add_story) -> None:
        """A story with both EPUB and the reader's HTML/JSON file."""
        add_story('Dual Format Story', formats=('epub', 'json'))
        story = client.get('/api/library').get_json()['stories'][0]
        assert sorted(story['formats']) == ['epub', 'json']
        assert story['epub_file'].endswith('_dual-format-story.epub')
        assert story['html_file'] == f"{story['id']}_dual-format-story.html"


@pytest.mark.integration
class TestCoverEndpoint:
    """Test /api/cover/<filename> endpoint."""

    def test_cover_path_traversal_blocked(self, client: FlaskClient) -> None:
        """Path traversal attempts are blocked."""
        response = client.get('/api/cover/../../../etc/passwd')
        assert response.status_code == 404

    def test_cover_double_dot_blocked(self, client: FlaskClient) -> None:
        """Double-dot path traversal blocked."""
        response = client.get('/api/cover/..%2F..%2Fetc%2Fpasswd')
        assert response.status_code == 404

    def test_cover_absolute_path_blocked(self, client: FlaskClient) -> None:
        """Absolute path blocked."""
        response = client.get('/api/cover//etc/passwd')
        assert response.status_code == 404

    def test_cover_non_jpg_extension_rejected(self, client: FlaskClient) -> None:
        """Non-.jpg files are rejected."""
        response = client.get('/api/cover/test-story.png')
        assert response.status_code == 404

    def test_cover_returns_existing_file(self, client: FlaskClient, app: Flask) -> None:
        """Existing cover image is returned."""
        with app.app_context():
            from app.utils import get_cover_directory
            cover_dir = Path(get_cover_directory())

            cover_path = cover_dir / "test-story.jpg"
            fake_jpg = b'\xFF\xD8\xFF\xE0\x00\x10\x4A\x46\x49\x46'
            cover_path.write_bytes(fake_jpg)

            response = client.get('/api/cover/test-story.jpg')

            assert response.status_code == 200
            assert 'image/jpeg' in response.content_type

    def test_cover_generates_if_missing(self, client: FlaskClient, app: Flask) -> None:
        """Cover is generated if not found."""
        with app.app_context():
            from app.utils import get_html_directory, get_cover_directory

            json_path = Path(get_html_directory()) / "new-story.json"
            json_path.write_text(json.dumps({
                'title': 'New Story',
                'author': 'Author Name'
            }))

            with patch('app.services.generate_cover_image') as mock_generate:
                def create_fake_cover(title, author, output_path):
                    Path(output_path).write_bytes(b'\xFF\xD8\xFF\xE0')

                mock_generate.side_effect = create_fake_cover

                response = client.get('/api/cover/new-story.jpg')

                assert response.status_code == 200
                mock_generate.assert_called_once()
                args = mock_generate.call_args[0]
                assert args[0] == 'New Story'
                assert args[1] == 'Author Name'

    def test_cover_extracts_from_epub_if_available(self, client: FlaskClient, app: Flask) -> None:
        """Cover is extracted from EPUB if available."""
        with app.app_context():
            from app.utils import get_epub_directory, get_cover_directory

            epub_path = Path(get_epub_directory()) / "with-cover.epub"
            epub_path.write_text("fake epub with embedded cover")

            with patch('app.services.extract_cover_from_epub') as mock_extract:
                def extract_and_create(epub_path, cover_path):
                    Path(cover_path).write_bytes(b'\xFF\xD8\xFF\xE0')
                    return True

                mock_extract.side_effect = extract_and_create

                response = client.get('/api/cover/with-cover.jpg')

                assert response.status_code == 200
                mock_extract.assert_called_once()

    def test_cover_uses_metadata_from_json(self, client: FlaskClient, app: Flask) -> None:
        """Cover generation uses title/author from JSON metadata."""
        with app.app_context():
            from app.utils import get_html_directory

            json_path = Path(get_html_directory()) / "metadata-story.json"
            json_path.write_text(json.dumps({
                'title': 'Metadata Story Title',
                'author': 'Metadata Author'
            }))

            with patch('app.services.generate_cover_image') as mock_gen:
                def create_fake_cover(title, author, output_path):
                    Path(output_path).write_bytes(b'\xFF\xD8\xFF\xE0')

                mock_gen.side_effect = create_fake_cover

                response = client.get('/api/cover/metadata-story.jpg')

                assert response.status_code == 200
                mock_gen.assert_called_once()
                assert mock_gen.call_args[0][0] == 'Metadata Story Title'
                assert mock_gen.call_args[0][1] == 'Metadata Author'

    def test_cover_fallback_to_filename_if_no_metadata(self, client: FlaskClient, app: Flask) -> None:
        """Cover uses filename as title if no metadata available."""
        with patch('app.services.generate_cover_image') as mock_gen:
            def create_fake_cover(title, author, output_path):
                Path(output_path).write_bytes(b'\xFF\xD8\xFF\xE0')

            mock_gen.side_effect = create_fake_cover

            response = client.get('/api/cover/no-metadata.jpg')

            assert response.status_code == 200
            mock_gen.assert_called_once()
            assert mock_gen.call_args[0][0] == 'no-metadata'
            assert mock_gen.call_args[0][1] == 'Unknown Author'

    def test_cover_error_handling(self, client: FlaskClient) -> None:
        """Cover generation error returns 500."""
        with patch('app.services.generate_cover_image') as mock_gen:
            mock_gen.side_effect = Exception("Cover generation failed")

            response = client.get('/api/cover/error-story.jpg')

            assert response.status_code == 500
