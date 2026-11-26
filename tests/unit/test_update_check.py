"""The scheduled new-part check: it reads its on/off setting and checks each enabled story."""
from __future__ import annotations
from unittest.mock import patch
import pytest
from flask import Flask


@pytest.mark.unit
class TestScheduledUpdateCheck:

    def _story(self, **values):
        from app.models import db, Story, Author
        author = Author.query.filter_by(name='A').first() or Author(name='A')
        story = Story(author=author, **values)
        db.session.add(story)
        db.session.commit()
        return story

    def test_checks_enabled_stories_when_turned_on(self, app: Flask) -> None:
        from app.models import AppConfig, db
        from app.services import story_update_checker as checker
        with app.app_context():
            db.session.add(AppConfig(key='auto_update_enabled', value='true', value_type='bool'))
            db.session.commit()
            story = self._story(title='Checked', filename_base='checked', literotica_url='https://www.literotica.com/s/c')
            found = {'has_update': True, 'old_chapter_count': 1, 'new_chapter_count': 2}
            with patch.object(checker.StoryUpdateChecker, 'check_for_updates_via_series', return_value=found) as check, \
                 patch.object(checker.StoryUpdateChecker, 'update_story', return_value=True), \
                 patch.object(checker, 'send_notification') as notify, \
                 patch.object(checker, '_check_watched_authors_for_new_stories'):
                checker.check_all_stories_for_updates(app)
            assert check.call_args.args[0].id == story.id
            assert 'Checked: 1 -> 2' in notify.call_args.args[0]

    def test_does_nothing_when_turned_off(self, app: Flask) -> None:
        from app.services import story_update_checker as checker
        with app.app_context():
            self._story(title='Checked', filename_base='checked', literotica_url='https://www.literotica.com/s/c')
            with patch.object(checker.StoryUpdateChecker, 'check_for_updates_via_series') as check, \
                 patch.object(checker, 'send_notification') as notify:
                checker.check_all_stories_for_updates(app)
            check.assert_not_called()
            notify.assert_not_called()
