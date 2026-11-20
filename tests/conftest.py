from __future__ import annotations
import pytest
import os
import tempfile
import shutil
import time
from pathlib import Path
from typing import Generator, Callable
from flask import Flask
from flask.testing import FlaskClient
from app import create_app


@pytest.fixture(scope="session")
def test_data_dir() -> Path:
    """Path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _keep_tests_out_of_real_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test, with or without the app, writes stories, covers and logs to a temporary folder,
    never to the real app/stories or app/data. (The app fixture below points them elsewhere again.)"""
    import app.utils.paths as paths_module
    root = tmp_path / "litkeeper-files"
    for name in ("epubs", "html", "covers", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(paths_module, "get_stories_directory", lambda: str(root))
    monkeypatch.setattr(paths_module, "get_data_directory", lambda: str(root))
    monkeypatch.setattr(paths_module, "get_logs_directory", lambda: str(root / "logs"))
    monkeypatch.setenv("LITKEEPER_DATABASE_PATH", str(root / "litkeeper.db"))


@pytest.fixture(scope="function")
def temp_dir() -> Generator[Path, None, None]:
    """Create temporary directory for test file operations."""
    tmp = tempfile.mkdtemp()
    try:
        yield Path(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="function")
def app(_keep_tests_out_of_real_folders, temp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    """Create Flask app with test configuration."""
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-for-testing-only-do-not-use-in-production")
    monkeypatch.setenv("SKIP_BACKGROUND_WORKERS", "true")
    monkeypatch.setenv("ENABLE_NOTIFICATIONS", "false")
    monkeypatch.setenv("ENABLE_ACTION_LOG", "false")
    monkeypatch.setenv("ENABLE_ERROR_LOG", "false")
    monkeypatch.setenv("ENABLE_URL_LOG", "false")

    data_dir = temp_dir / "data"
    data_dir.mkdir()
    # Each test gets an empty database of its own, never the real one in app/data.
    monkeypatch.setenv("LITKEEPER_DATABASE_PATH", str(data_dir / "litkeeper.db"))
    (data_dir / "epubs").mkdir()
    (data_dir / "html").mkdir()
    (data_dir / "covers").mkdir()
    (data_dir / "logs").mkdir()

    import app.utils.paths as paths_module
    monkeypatch.setattr(paths_module, "get_data_directory", lambda: str(data_dir))
    # The epub, html and cover folders are worked out from this one when they are asked for, so
    # code that imported those helpers early also writes here, never into the real app/stories.
    monkeypatch.setattr(paths_module, "get_stories_directory", lambda: str(data_dir))
    monkeypatch.setattr(paths_module, "get_epub_directory", lambda: str(data_dir / "epubs"))
    monkeypatch.setattr(paths_module, "get_html_directory", lambda: str(data_dir / "html"))
    monkeypatch.setattr(paths_module, "get_cover_directory", lambda: str(data_dir / "covers"))
    monkeypatch.setattr(paths_module, "get_logs_directory", lambda: str(data_dir / "logs"))

    flask_app = create_app()
    flask_app.config["TESTING"] = True

    return flask_app


@pytest.fixture(scope="function")
def client(app: Flask) -> FlaskClient:
    """Flask test client for making requests."""
    return app.test_client()


@pytest.fixture(scope="function")
def sample_story_content(test_data_dir: Path) -> str:
    """Load sample story content from fixtures."""
    content_file = test_data_dir / "sample_story_content.txt"
    if content_file.exists():
        return content_file.read_text(encoding='utf-8')

    return """Chapter 1: The Beginning

This is the first paragraph of chapter one. It contains some basic content for testing purposes.

This is the second paragraph with some additional details and story development.


Chapter 2: The Middle

This is the beginning of chapter two. The story continues here with more content.

More content for testing the chapter parsing and content formatting logic.


Chapter 3: The End

The final chapter begins here with its own distinct content.

This is the conclusion of the story for testing purposes."""


@pytest.fixture(scope="function")
def sample_literotica_html(test_data_dir: Path) -> str:
    """Load mocked Literotica HTML response."""
    html_file = test_data_dir / "sample_html_response.html"
    if html_file.exists():
        return html_file.read_text(encoding='utf-8')

    return """<!DOCTYPE html>
<html>
<head>
    <title>Test Story - Literotica.com</title>
    <meta charset="utf-8">
</head>
<body>
    <h1 class="_title_abc123">Test Story Title</h1>
    <a class="_author__title_xyz789" href="/members/testauthor">TestAuthor</a>
    <nav class="_breadcrumbs_nav">
        <span itemprop="name">Home</span>
        <span itemprop="name">Romance</span>
    </nav>
    <div class="_article__content_content">
        <p>This is the first paragraph of the story.</p>
        <p>This is the second paragraph with more content.</p>
        <p>This is the third paragraph continuing the narrative.</p>
    </div>
    <div class="_tags_list">
        <a class="_tags__link_tag" href="/tags/romance">Romance</a>
        <a class="_tags__link_tag" href="/tags/love">Love</a>
    </div>
</body>
</html>"""


@pytest.fixture(scope="module")
def rate_limiter() -> Callable[[], None]:
    """Ensure minimum 3-second delay between real HTTP requests for E2E tests."""
    last_request_time = None

    def wait() -> None:
        nonlocal last_request_time
        if last_request_time:
            elapsed = time.time() - last_request_time
            if elapsed < 3:
                time.sleep(3 - elapsed)
        last_request_time = time.time()

    return wait


@pytest.fixture(scope="session")
def test_story_urls() -> dict[str, str]:
    """Stable test story URLs for E2E testing.

    These should be public domain or very old stories less likely to be removed.
    Update these URLs if they become unavailable.
    """
    return {
        "single_page": "https://www.literotica.com/s/a-test-story-1",
        "multi_chapter": "https://www.literotica.com/s/a-test-series-ch-01",
        "with_series": "https://www.literotica.com/s/a-series-story-pt-01"
    }


@pytest.fixture(scope="function")
def add_story(app: Flask):
    """Create a story the way LitKeeper keeps one: a database record (author, category, tags) plus
    its files in the test folders. Returns the story's id. Each test has its own database."""
    import json as _json
    from datetime import datetime as _dt
    from app.models import Story, Author, Category, Tag, StoryFormat, db
    from app.utils import paths as _paths

    def _add(title: str = 'Test Story', author: str = 'Test Author', category: str | None = None,
             tags: tuple = (), formats: tuple = ('epub', 'json'), created_at=None, chapters=None,
             filename_base: str | None = None, **fields) -> int:
        with app.app_context():
            author_row = Author.query.filter_by(name=author).first() or Author(name=author)
            db.session.add(author_row)
            category_row = None
            if category:
                slug = category.lower().replace(' ', '-')
                category_row = Category.query.filter_by(name=category).first() or Category(name=category, slug=slug)
                db.session.add(category_row)
            tag_rows = []
            for name in tags:
                tag = Tag.query.filter_by(name=name).first() or Tag(name=name, slug=name.lower().replace(' ', '-'))
                db.session.add(tag)
                tag_rows.append(tag)
            base = filename_base or title.lower().replace(' ', '-')
            story = Story(title=title, author=author_row, category=category_row, filename_base=base, **fields)
            story.tags = tag_rows
            if created_at:
                story.created_at = created_at
            db.session.add(story)
            db.session.flush()
            for kind in formats:
                if kind == 'epub':
                    path = os.path.join(_paths.get_epub_directory(), f'{story.id}_{base}.epub')
                    Path(path).write_bytes(b'fake epub')
                else:
                    path = os.path.join(_paths.get_html_directory(), f'{story.id}_{base}.json')
                    data = {'title': title, 'author': author, 'category': category, 'tags': list(tags),
                            'chapters': chapters if chapters is not None else
                            [{'number': 1, 'title': title, 'paragraphs': ['First paragraph.', 'Second paragraph.']}]}
                    Path(path).write_text(_json.dumps(data), encoding='utf-8')
                db.session.add(StoryFormat(story_id=story.id, format_type=kind, file_path=path,
                                           file_size=os.path.getsize(path)))
            db.session.commit()
            return story.id

    return _add
