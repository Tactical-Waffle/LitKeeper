"""A story's word count is the sum of all its parts. Older stories are recounted once, from their reader (JSON)
files, by a background thread at startup (the Length sort and the library's word counts rely on it)."""
from __future__ import annotations
import re
import threading

from .logger import log_action, log_error

DONE_KEY = 'word_counts_recounted'
TAGS = re.compile(r'<[^>]+>')


def count_words(chapters: list) -> int:
    """Words in every part's paragraphs (HTML tags left out)."""
    return sum(len(TAGS.sub(' ', str(paragraph)).split())
               for chapter in chapters if isinstance(chapter, dict) for paragraph in chapter.get('paragraphs') or [])


def recount_all() -> int:
    """Recount every story with a reader file once. Returns how many counts changed. Needs an app context."""
    from app.models import AppConfig, Story, db
    from .ai_cover import _chapters
    if AppConfig.query.filter_by(key=DONE_KEY).first():
        return 0
    changed = 0
    for done, (story_id,) in enumerate(db.session.query(Story.id).all(), 1):
        story = db.session.get(Story, story_id)
        chapters = _chapters(story) if story else []
        words = count_words(chapters)
        if words and story.word_count != words:
            story.word_count = words
            changed += 1
        if done % 100 == 0:
            db.session.commit()
            db.session.expunge_all()  # Keep memory flat across a large library.
    db.session.add(AppConfig(key=DONE_KEY, value_type='bool', value='true', description='Word counts recounted from all parts'))
    db.session.commit()
    return changed


def recount_in_background(app) -> None:
    def run():
        try:
            with app.app_context():
                changed = recount_all()
            if changed:
                log_action(f'Word counts recounted from all parts: {changed} {"story" if changed == 1 else "stories"} corrected')
        except Exception as error:
            log_error(f'Could not recount word counts: {error}')
    threading.Thread(target=run, daemon=True, name='WordCounts').start()
