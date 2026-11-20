from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Callable, Generator

import pytest
from flask import Flask
from flask.testing import FlaskClient


# Existing app code trips two warnings that pytest.ini escalates to errors on
# Python 3.12+ / SQLAlchemy 2: TimestampMixin's datetime.utcnow() and a few
# legacy Query.get() calls in routes. Neither is what these tests exercise.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore::sqlalchemy.exc.LegacyAPIWarning"),
]

# Far in the past so these sort ahead of anything queued for real.
EPOCH = datetime(2000, 1, 1, 12, 0, 0)

MakeStory = Callable[..., int]


@pytest.fixture
def make_story(app: Flask) -> Generator[MakeStory, None, None]:
    """Create stories and delete them afterwards.

    create_app() points at app/data/litkeeper.db rather than a temp DB, so
    tests share state; clean up and never assume the DB starts empty.
    """
    from app.models import Author, ReadingProgress, Story, db

    created: list[tuple[int, int]] = []

    def _make(title: str, *, queued_at: datetime | None = None) -> int:
        with app.app_context():
            author = Author(name=f"Queue Test Author {uuid.uuid4().hex[:12]}")
            db.session.add(author)
            db.session.flush()
            story = Story(
                title=title,
                author_id=author.id,
                filename_base=f"queue-test-{uuid.uuid4().hex[:12]}",
                in_queue=queued_at is not None,
                queued_at=queued_at,
            )
            db.session.add(story)
            db.session.commit()
            created.append((story.id, author.id))
            return story.id

    yield _make

    with app.app_context():
        for story_id, author_id in created:
            ReadingProgress.query.filter_by(story_id=story_id).delete()
            Story.query.filter_by(id=story_id).delete()
            Author.query.filter_by(id=author_id).delete()
        db.session.commit()


def _queue_count(app: Flask) -> int:
    with app.app_context():
        from app.services.library import get_queue_count
        return get_queue_count()


def _in_queue(app: Flask, story_id: int) -> bool:
    with app.app_context():
        from app.models import Story, db
        return bool(db.session.get(Story, story_id).in_queue)


def _card_ids(html: bytes) -> list[int]:
    return [int(m) for m in re.findall(rb'<div class="story-card cursor-pointer"\s+data-story-id="(\d+)"', html)]


class TestQueueToggleApi:
    def test_add_then_remove(self, client: FlaskClient, app: Flask, make_story: MakeStory) -> None:
        story_id = make_story("Toggle Me")
        before = _queue_count(app)

        resp = client.post(f"/api/story/{story_id}/queue", json={"in_queue": True})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["in_queue"] is True
        assert data["queued_at"] is not None
        assert data["queue_count"] == before + 1

        data = client.post(f"/api/story/{story_id}/queue", json={"in_queue": False}).get_json()
        assert data["in_queue"] is False
        assert data["queued_at"] is None
        assert data["queue_count"] == before

    def test_rejects_non_boolean(self, client: FlaskClient, make_story: MakeStory) -> None:
        story_id = make_story("Bad Payload")
        resp = client.post(f"/api/story/{story_id}/queue", json={"in_queue": "yes"})
        assert resp.status_code == 400

    def test_missing_story(self, client: FlaskClient) -> None:
        resp = client.post("/api/story/987654321/queue", json={"in_queue": True})
        assert resp.status_code == 404


class TestQueueLibraryView:
    def test_queue_only_filters_and_orders_oldest_first(
        self, client: FlaskClient, make_story: MakeStory
    ) -> None:
        newer = make_story("Queued Second", queued_at=EPOCH)
        older = make_story("Queued First", queued_at=EPOCH - timedelta(days=2))
        not_queued = make_story("Not Queued")

        resp = client.get("/library/filter", query_string={
            "queue_only": "true", "sort_by": "queued", "sort_order": "asc",
        })
        assert resp.status_code == 200
        ids = _card_ids(resp.data)
        assert not_queued not in ids
        assert ids[:2] == [older, newer]

    def test_queue_only_paginates_with_flag(self, client: FlaskClient, make_story: MakeStory) -> None:
        # The infinite-scroll URL must keep the queue filter, or page 2 would
        # silently switch back to the whole library.
        for i in range(41):
            make_story(f"Paged {i}", queued_at=EPOCH + timedelta(minutes=i))
        resp = client.get("/library/filter", query_string={"queue_only": "true", "sort_by": "queued"})
        next_url = re.search(rb'hx-get="(/library/filter\?[^"]+)"', resp.data)
        assert next_url is not None
        assert b"queue_only=true" in next_url.group(1)

    def test_empty_queue_state(self, client: FlaskClient) -> None:
        resp = client.get("/library/filter", query_string={
            "queue_only": "true", "search": f"no-such-story-{uuid.uuid4().hex}",
        })
        assert resp.status_code == 200
        assert b"Your reading queue is empty" in resp.data

    def test_empty_library_state_unchanged_outside_queue(self, client: FlaskClient) -> None:
        resp = client.get("/library/filter", query_string={"search": f"no-such-story-{uuid.uuid4().hex}"})
        assert b"No stories found" in resp.data
        assert b"Your reading queue is empty" not in resp.data

    def test_card_badge_only_on_queued_stories(self, client: FlaskClient, make_story: MakeStory) -> None:
        queued = make_story("Badge Yes", queued_at=EPOCH)
        plain = make_story("Badge No")
        html = client.get("/library/filter", query_string={"sort_by": "date", "sort_order": "desc"}).get_data(as_text=True)

        def card(story_id: int) -> str:
            start = html.index(f'data-story-id="{story_id}"')
            end = html.find('<div class="cursor-pointer"', start)
            return html[start:end if end != -1 else len(html)]

        assert re.search(r'class="card-queue-badge\s+absolute', card(queued))
        assert re.search(r'class="card-queue-badge\s+hidden', card(plain))

    def test_index_shows_queue_count(self, client: FlaskClient, app: Flask, make_story: MakeStory) -> None:
        make_story("Count A", queued_at=EPOCH)
        make_story("Count B", queued_at=EPOCH)
        make_story("Count C")
        expected = _queue_count(app)

        html = client.get("/").get_data(as_text=True)
        assert 'id="libraryViewQueueTab"' in html
        badge = re.search(r'id="queueCountBadge"\s+class="([^"]*)">(\d+)<', html)
        assert badge is not None
        assert int(badge.group(2)) == expected >= 2
        assert "hidden" not in badge.group(1).split()

    def test_modal_reflects_queue_state(self, client: FlaskClient, make_story: MakeStory) -> None:
        queued = make_story("Modal Queued", queued_at=EPOCH)
        plain = make_story("Modal Plain")

        html = client.get(f"/api/story/{queued}/modal").get_data(as_text=True)
        assert 'queue-toggle-btn' in html
        assert 'data-in-queue="true"' in html
        assert "In Queue" in html

        html = client.get(f"/api/story/{plain}/modal").get_data(as_text=True)
        assert 'data-in-queue="false"' in html
        assert "Add to Queue" in html


class TestQueueCompletion:
    def test_completing_a_story_removes_it_from_queue(
        self, client: FlaskClient, app: Flask, make_story: MakeStory
    ) -> None:
        story_id = make_story("Finish Me", queued_at=EPOCH)

        assert client.post(f"/epub/api/progress/{story_id}", json={"percentage": 0.5}).status_code == 200
        assert _in_queue(app, story_id) is True

        resp = client.post(f"/epub/api/progress/{story_id}", json={"is_completed": True, "percentage": 1.0})
        assert resp.status_code == 200
        with app.app_context():
            from app.models import Story, db
            story = db.session.get(Story, story_id)
            assert story.in_queue is False
            assert story.queued_at is None

    def test_requeue_survives_repeated_completed_saves(
        self, client: FlaskClient, app: Flask, make_story: MakeStory
    ) -> None:
        story_id = make_story("Reread Me", queued_at=EPOCH)

        client.post(f"/epub/api/progress/{story_id}", json={"is_completed": True})
        assert _in_queue(app, story_id) is False
        client.post(f"/api/story/{story_id}/queue", json={"in_queue": True})
        # A client that re-sends is_completed=true on every save mustn't undo the re-queue.
        client.post(f"/epub/api/progress/{story_id}", json={"is_completed": True})
        assert _in_queue(app, story_id) is True
