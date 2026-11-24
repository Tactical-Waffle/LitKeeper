from __future__ import annotations
import os
import random
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional
from flask import Flask
from app.services.http_client import global_rate_limiter

DEFAULT_MAX_DAILY_DOWNLOADS = 25
MAX_DAILY_DOWNLOADS_LIMIT = 10000
DAILY_CAP_KEY = 'max_daily_downloads'

# Pacing: a wait of about this many minutes after each story fetched from Literotica, varied by up to
# DELAY_VARIATION either way so downloads don't arrive like clockwork.
DOWNLOAD_DELAY_KEY = 'download_delay_minutes'
DEFAULT_DOWNLOAD_DELAY_MINUTES = 10
MAX_DOWNLOAD_DELAY_MINUTES = 720
DELAY_VARIATION = 0.25
NEXT_DOWNLOAD_KEY = 'next_download_at'

# Failed downloads (often a Cloudflare check) are tried again later, each wait longer than the last.
RETRY_DELAYS_MINUTES = (5, 15, 45, 120, 360)
MAX_ATTEMPTS = len(RETRY_DELAYS_MINUTES) + 1
# Failures in a row also stretch the wait before the next download of any story, up to this.
FAILURE_GAP_CAP_MINUTES = 240
# After this many downloads in a row fail (usually Literotica or Cloudflare refusing requests), downloads stop
# until midnight UTC, when the daily limit starts again, instead of asking again and again.
PAUSE_AFTER_FAILURES = 3
FAILURES_KEY = 'download_failures_in_a_row'
PAUSED_UNTIL_KEY = 'downloads_paused_until'


def env_daily_cap() -> Optional[int]:
    """The MAX_DAILY_DOWNLOADS environment variable, if it holds a usable number (0 is unlimited)."""
    try:
        value = int(os.environ['MAX_DAILY_DOWNLOADS'])
    except (KeyError, ValueError):
        return None
    return value if value >= 0 else None


def saved_daily_cap() -> Optional[int]:
    """The limit saved in Settings, if any (0 is unlimited). Needs an app context."""
    from app.models import AppConfig
    cfg = AppConfig.query.filter_by(key=DAILY_CAP_KEY).first()
    if not cfg:
        return None
    try:
        value = int(cfg.get_value())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def get_daily_cap() -> int:
    """Settings wins, then the MAX_DAILY_DOWNLOADS environment variable, then the default. 0 means no limit."""
    for value in (saved_daily_cap(), env_daily_cap()):
        if value is not None:
            return value
    return DEFAULT_MAX_DAILY_DOWNLOADS


def over_daily_cap(count: int, cap: int) -> bool:
    return bool(cap) and count >= cap


def _config(key: str):
    from app.models import AppConfig
    return AppConfig.query.filter_by(key=key).first()


def _store(key: str, value: Optional[str], description: str) -> None:
    from app.models import AppConfig, db
    cfg = _config(key)
    if not cfg:
        cfg = AppConfig(key=key, value_type='string', description=description)
        db.session.add(cfg)
    cfg.value = value
    db.session.commit()


def failures_in_a_row() -> int:
    """Downloads that failed in a row, kept in the database so a restart doesn't forget them."""
    cfg = _config(FAILURES_KEY)
    try:
        return max(0, int(cfg.value)) if cfg and cfg.value else 0
    except ValueError:
        return 0


def paused_until() -> Optional[datetime]:
    """When downloads start again after repeated failures (UTC), or None when they aren't paused."""
    cfg = _config(PAUSED_UNTIL_KEY)
    try:
        when = datetime.fromisoformat(cfg.value) if cfg and cfg.value else None
    except ValueError:
        return None
    return when if when and when > datetime.utcnow() else None


def pause_until_tomorrow() -> datetime:
    """Stop downloading until midnight UTC."""
    tomorrow = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    _store(PAUSED_UNTIL_KEY, tomorrow.isoformat(), 'Downloads paused after failures in a row, until (UTC)')
    return tomorrow


def resume_downloads() -> None:
    """Start downloading again now (the Queue page's Resume Now)."""
    _store(PAUSED_UNTIL_KEY, None, 'Downloads paused after failures in a row, until (UTC)')
    _store(FAILURES_KEY, '0', 'Downloads that failed in a row')
    _store_next_download_at(None)


def save_daily_cap(value) -> int:
    """Save the limit from Settings. Raises ValueError for anything but a whole number in range."""
    from app.models import AppConfig, db
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError('Enter a whole number.')
    if number < 0 or number > MAX_DAILY_DOWNLOADS_LIMIT:
        raise ValueError(f'Enter a number from 0 (no limit) to {MAX_DAILY_DOWNLOADS_LIMIT}.')
    cfg = AppConfig.query.filter_by(key=DAILY_CAP_KEY).first()
    if not cfg:
        cfg = AppConfig(key=DAILY_CAP_KEY, value_type='int', description='Maximum stories downloaded per day')
        db.session.add(cfg)
    cfg.value_type = 'int'
    cfg.set_value(number)
    db.session.commit()
    return number


def vary(minutes: float) -> float:
    """The given number of minutes, made up to 25% shorter or longer at random."""
    return minutes * random.uniform(1 - DELAY_VARIATION, 1 + DELAY_VARIATION)


def get_download_delay() -> int:
    """Minutes between downloads from Settings (0 means no wait). Needs an app context."""
    from app.models import AppConfig
    cfg = AppConfig.query.filter_by(key=DOWNLOAD_DELAY_KEY).first()
    try:
        value = int(cfg.get_value()) if cfg else DEFAULT_DOWNLOAD_DELAY_MINUTES
    except (TypeError, ValueError):
        return DEFAULT_DOWNLOAD_DELAY_MINUTES
    return min(max(value, 0), MAX_DOWNLOAD_DELAY_MINUTES)


def save_download_delay(value) -> int:
    """Save minutes between downloads. Raises ValueError for anything but a whole number in range."""
    from app.models import AppConfig, db
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError('Enter a whole number of minutes.')
    if number < 0 or number > MAX_DOWNLOAD_DELAY_MINUTES:
        raise ValueError(f'Enter a number from 0 to {MAX_DOWNLOAD_DELAY_MINUTES}.')
    cfg = AppConfig.query.filter_by(key=DOWNLOAD_DELAY_KEY).first()
    if not cfg:
        cfg = AppConfig(key=DOWNLOAD_DELAY_KEY, value_type='int', description='Minutes between downloads (varied by up to 25%)')
        db.session.add(cfg)
    cfg.value_type = 'int'
    cfg.set_value(number)
    # A shorter wait takes effect now rather than after the wait already planned.
    planned = next_download_at()
    if planned and planned > datetime.utcnow() + timedelta(minutes=number * (1 + DELAY_VARIATION)):
        _store_next_download_at(datetime.utcnow() + timedelta(minutes=vary(number)) if number else None, commit=False)
    db.session.commit()
    return number


def next_download_at() -> Optional[datetime]:
    """When the next story may be downloaded (UTC), or None for straight away. Kept in the
    database so the queue page (served by any web worker) can show it and restarts respect it."""
    from app.models import AppConfig
    cfg = AppConfig.query.filter_by(key=NEXT_DOWNLOAD_KEY).first()
    if not cfg or not cfg.value:
        return None
    try:
        return datetime.fromisoformat(cfg.value)
    except ValueError:
        return None


def _store_next_download_at(when: Optional[datetime], commit: bool = True) -> None:
    from app.models import AppConfig, db
    cfg = AppConfig.query.filter_by(key=NEXT_DOWNLOAD_KEY).first()
    if not cfg:
        cfg = AppConfig(key=NEXT_DOWNLOAD_KEY, value_type='string', description='Earliest time of the next download (UTC)')
        db.session.add(cfg)
    cfg.value = when.isoformat() if when else None
    if commit:
        db.session.commit()


def retry_wait(failures: int) -> timedelta:
    """How long to wait before trying a story again after this many failed attempts."""
    minutes = RETRY_DELAYS_MINUTES[min(max(failures, 1), len(RETRY_DELAYS_MINUTES)) - 1]
    return timedelta(minutes=vary(minutes))


class DownloadQueueWorker:
    """Background worker for processing download queue"""

    def __init__(self, app: Flask, poll_interval: int = 60):
        self.app = app
        self.poll_interval = poll_interval
        self.thread: Optional[threading.Thread] = None
        self.running = False
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._last_rate_limit_reset_date: Optional[str] = None

    def start(self):
        """Start the background worker thread"""
        if self.thread and self.thread.is_alive():
            return

        self._recover_stale_jobs()

        self.running = True
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._worker_loop, daemon=True, name="DownloadQueueWorker")
        self.thread.start()

    def _recover_stale_jobs(self):
        """Reset jobs stuck in 'processing' state (from crashes/restarts)"""
        from app.models import DownloadQueueItem, db
        from datetime import datetime, timedelta

        with self.app.app_context():
            from .logger import log_action, log_error
            try:
                stale_cutoff = datetime.utcnow() - timedelta(minutes=10)
                stale_items = DownloadQueueItem.query.filter(
                    DownloadQueueItem.status == 'processing',
                    DownloadQueueItem.started_at < stale_cutoff
                ).all()

                if stale_items:
                    log_action(f"[DOWNLOAD WORKER] Recovering {len(stale_items)} stale jobs")
                    for item in stale_items:
                        item.status = 'pending'
                        item.started_at = None
                        item.progress_message = 'Reset from stale processing state'
                    db.session.commit()
            except Exception as e:
                db.session.rollback()
                log_error(f"[DOWNLOAD WORKER] Could not recover stale jobs (migrations pending?): {e}")

    def stop(self):
        """Stop the background worker thread"""
        self.running = False
        self._stop_event.set()
        self._wake_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def wake(self):
        """Interrupt the current sleep cycle and process the queue immediately."""
        self._wake_event.set()

    def _worker_loop(self):
        """Main worker loop"""
        from .logger import log_action, log_error

        log_action("Download queue worker started")

        while self.running and not self._stop_event.is_set():
            try:
                with self.app.app_context():
                    self._process_next_item()
            except Exception as e:
                log_error(f"Error in download queue worker: {str(e)}\n{traceback.format_exc()}")

            self._wake_event.wait(self._seconds_until_next_check())
            self._wake_event.clear()

        log_action("Download queue worker stopped")

    def _seconds_until_next_check(self) -> float:
        """Sleep until the planned next download when that comes before the usual poll."""
        try:
            with self.app.app_context():
                planned = next_download_at()
        except Exception:
            return self.poll_interval
        if not planned:
            return self.poll_interval
        remaining = (planned - datetime.utcnow()).total_seconds()
        return min(self.poll_interval, max(remaining + 1, 1))

    def _plan_next_download(self, failed: bool) -> None:
        """After a visit to Literotica: wait about the set number of minutes before the next one,
        doubling with each failure in a row (a Cloudflare check tends to last a while)."""
        from .logger import log_action
        failures = failures_in_a_row() + 1 if failed else 0
        _store(FAILURES_KEY, str(failures), 'Downloads that failed in a row')
        if failures >= PAUSE_AFTER_FAILURES:
            until = pause_until_tomorrow()
            _store(FAILURES_KEY, '0', 'Downloads that failed in a row')
            _store_next_download_at(None)
            log_action(f"[DOWNLOAD WORKER] {failures} downloads failed in a row (Literotica or Cloudflare is probably "
                       f"refusing requests): downloads are paused until {until:%Y-%m-%d %H:%M} UTC")
            try:
                from .notifier import send_notification
                send_notification(f"Downloads paused until tomorrow: {failures} downloads failed in a row. "
                                  "Literotica or Cloudflare is probably refusing requests.", is_error=True)
            except Exception:
                pass
            return
        minutes = get_download_delay()
        if failures:
            minutes = min(max(minutes, RETRY_DELAYS_MINUTES[0]) * 2 ** failures, FAILURE_GAP_CAP_MINUTES)
        if not minutes:
            _store_next_download_at(None)
            return
        minutes = vary(minutes)
        when = datetime.utcnow() + timedelta(minutes=minutes)
        _store_next_download_at(when)
        log_action(f"[DOWNLOAD WORKER] Next download in {minutes:.1f} minutes"
                   + (f" ({failures} failed in a row)" if failures else ''))

    def _get_daily_cap(self) -> int:
        return get_daily_cap()

    def _reset_rate_limited_items(self):
        """Requeue rate-limited items that should now be allowed to proceed."""
        from app.models import DownloadQueueItem, db
        from .logger import log_action
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Requeue items rate-limited on previous days (at most once per UTC day)
        today = datetime.utcnow().strftime('%Y-%m-%d')
        if self._last_rate_limit_reset_date != today:
            self._last_rate_limit_reset_date = today
            prev_limited = DownloadQueueItem.query.filter(
                DownloadQueueItem.status == 'rate_limited',
                DownloadQueueItem.completed_at < today_start
            ).all()
            if prev_limited:
                log_action(f"[DOWNLOAD WORKER] Resetting {len(prev_limited)} rate-limited items from previous days")
                for it in prev_limited:
                    it.status = 'pending'
                    it.progress_message = None
                    it.completed_at = None
                db.session.commit()

        # Requeue today's rate-limited items if we're now under the cap (e.g. cap was raised)
        daily_cap = self._get_daily_cap()
        if not over_daily_cap(self._daily_downloads_today(), daily_cap):
            today_limited = DownloadQueueItem.query.filter(
                DownloadQueueItem.status == 'rate_limited',
                DownloadQueueItem.completed_at >= today_start
            ).all()
            if today_limited:
                log_action(f"[DOWNLOAD WORKER] Resetting {len(today_limited)} rate-limited items — now under daily cap of {daily_cap}")
                for it in today_limited:
                    it.status = 'pending'
                    it.progress_message = None
                    it.completed_at = None
                db.session.commit()

    def _daily_downloads_today(self) -> int:
        """Count completed downloads since midnight UTC."""
        from app.models import DownloadQueueItem
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return DownloadQueueItem.query.filter(
            DownloadQueueItem.status == 'completed',
            DownloadQueueItem.completed_at >= today_start
        ).count()

    def _process_next_item(self):
        """Process the next pending item in the queue"""
        from app.models import DownloadQueueItem, db
        from .logger import log_action, log_error
        from sqlalchemy import select

        self._reset_rate_limited_items()

        now = datetime.utcnow()
        if paused_until():  # Too many failures in a row: nothing until tomorrow.
            return
        planned = next_download_at()
        if planned and planned > now:
            return
        item = db.session.execute(
            select(DownloadQueueItem)
            .filter(
                DownloadQueueItem.status == 'pending',
                (DownloadQueueItem.scheduled_after == None) | (DownloadQueueItem.scheduled_after <= now)
            )
            .order_by(DownloadQueueItem.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()

        if not item:
            return

        item_id = item.id

        daily_cap = self._get_daily_cap()
        if over_daily_cap(self._daily_downloads_today(), daily_cap):
            log_action(f"[DOWNLOAD WORKER] Daily cap of {daily_cap} reached — marking pending items as rate_limited")
            pending = DownloadQueueItem.query.filter(
                DownloadQueueItem.status == 'pending',
                (DownloadQueueItem.scheduled_after == None) | (DownloadQueueItem.scheduled_after <= now)
            ).all()
            for it in pending:
                it.status = 'rate_limited'
                it.progress_message = f'Daily download limit of {daily_cap} reached. Resumes tomorrow.'
                it.completed_at = datetime.utcnow()
            db.session.commit()
            return

        log_action(f"Processing download queue item {item_id}: {item.url} (job_type={item.job_type})")

        global_rate_limiter.wait_if_needed()

        item.status = 'processing'
        item.started_at = datetime.utcnow()
        item.progress_message = 'Starting download...'
        db.session.commit()

        try:
            downloaded = True
            if item.job_type == 'author':
                self._process_author_scan(item)
            elif item.job_type in ('multi', 'parts'):
                downloaded = self._download_and_save_multi(item)
            else:
                downloaded = self._download_and_save(item)

            if item.job_type != 'author' and not downloaded:
                item.status = 'skipped'
            else:
                item.status = 'completed'
                item.progress_message = 'Download completed successfully'
            item.completed_at = datetime.utcnow()
            db.session.commit()

            log_action(f"Successfully completed download queue item {item.id}")
            if downloaded:  # A story skipped as already saved never reached Literotica: no need to wait.
                self._plan_next_download(failed=False)

            if item.job_type != 'author' and downloaded:
                from .notifier import send_notification
                send_notification(f"Story Downloaded: '{item.title or 'Story'}' has been added to your library")
                from .ai_cover import make_after_download
                make_after_download(self.app, item.story_id)
                from .ai_summary import make_after_download as summarize_after_download
                summarize_after_download(self.app, item.story_id)

        except Exception as e:
            error_msg = str(e)

            db.session.rollback()

            item = db.session.get(DownloadQueueItem, item_id)
            if not item:
                log_error(f"Failed to process download queue item {item_id}: Item no longer exists")
                return

            log_error(f"Download queue item {item_id} failed: {error_msg}", item.url)

            item.retry_count += 1
            attempts = max(item.max_retries or 0, MAX_ATTEMPTS)

            if item.retry_count >= attempts:
                item.status = 'failed'
                item.scheduled_after = None
                item.error_message = f"Failed after {item.retry_count} attempts: {error_msg}"
                item.completed_at = datetime.utcnow()
                log_action(f"Download queue item {item_id} failed permanently after {item.retry_count} attempts")
            else:
                wait = retry_wait(item.retry_count)
                item.status = 'pending'
                item.started_at = None
                item.scheduled_after = datetime.utcnow() + wait
                item.error_message = f"Attempt {item.retry_count} of {attempts} failed: {error_msg}"
                item.progress_message = f"Trying again in about {round(wait.total_seconds() / 60)} minutes"
                log_action(f"Download queue item {item_id} will be tried again in {wait.total_seconds() / 60:.0f} minutes "
                           f"(attempt {item.retry_count + 1}/{attempts})")

            db.session.commit()
            self._plan_next_download(failed=True)

    def _download_and_save(self, item):
        """Download a single story URL and save to database.

        Returns True if the story was downloaded, False if skipped as already seen.
        """
        from app.models import db, SeenLiteroticaUrl
        from .story_downloader import download_story
        from .logger import log_action, log_error

        if item.job_type != 'redownload' and SeenLiteroticaUrl.query.filter_by(url=item.url).first():
            log_action(f"Skipping already-seen URL: {item.url}")
            item.progress_message = 'Skipped: already downloaded as part of a series'
            db.session.commit()
            return False

        item.progress_message = 'Downloading story content...'
        db.session.commit()

        # 'part': one part of a series picked on its own in the author window — download just it.
        story_data = download_story(item.url, only_this_part=item.job_type == 'part')
        story_content, title, author, category, tags, author_url, page_count, series_url, story_description, story_stats = story_data

        if not story_content or not title:
            raise Exception("Failed to download story content or extract metadata")

        item.title = title
        item.author = author
        item.category = category
        item.set_tags(tags)
        item.total_pages = page_count
        item.downloaded_pages = page_count
        db.session.commit()

        item.progress_message = 'Creating files...'
        db.session.commit()

        # Redownloads should always regenerate both formats to keep the library complete.
        formats = item.get_formats()
        if item.job_type == 'redownload':
            formats = ['epub', 'html']

        from .story_processor import _create_story_files
        result = _create_story_files(
            story_content=story_content,
            story_title=title,
            story_author=author,
            story_category=category,
            story_tags=tags,
            source_url=item.url,
            author_url=author_url,
            page_count=page_count,
            formats=formats,
            series_url=series_url,
            story_description=story_description,
            story_stats=story_stats
        )

        if not result.get('success'):
            raise Exception(result.get('message', 'Failed to create story files'))

        from app.models import Story
        story = Story.query.filter_by(literotica_url=item.url).first()
        if story:
            item.story_id = story.id
            db.session.commit()
            # Ensure any missing format is queued for generation after a download.
            self._ensure_complete_formats(story)

        log_action(f"Successfully saved story '{title}' from queue item {item.id}")
        return True

    def _download_and_save_multi(self, item):
        """Download and combine multiple URLs into a single story."""
        from app.models import db
        from .story_downloader import download_and_combine_stories
        from .logger import log_action, log_error

        extra = item.get_extra_urls()
        all_urls = [item.url] + extra

        item.progress_message = f'Downloading and combining {len(all_urls)} stories...'
        db.session.commit()

        # 'parts': some parts of one series picked in the author window — only those, as one book.
        story_data = download_and_combine_stories(all_urls, only_these_parts=item.job_type == 'parts')
        story_content, title, author, category, tags, author_url, page_count, series_url, story_description, all_authors, all_tags, story_stats = story_data

        if not story_content or not title:
            raise Exception("Failed to combine stories")

        item.title = title
        item.author = author
        item.category = category
        item.set_tags(tags)
        item.total_pages = page_count
        item.downloaded_pages = page_count
        db.session.commit()

        item.progress_message = 'Creating files...'
        db.session.commit()

        from .story_processor import _create_story_files
        result = _create_story_files(
            story_content=story_content,
            story_title=title,
            story_author=author,
            story_category=category,
            story_tags=tags,
            source_url=item.url,
            author_url=author_url,
            page_count=page_count,
            formats=item.get_formats(),
            series_url=series_url,
            story_description=story_description,
            all_authors=all_authors,
            all_tags=all_tags,
            story_stats=story_stats
        )

        if not result.get('success'):
            raise Exception(result.get('message', 'Failed to create combined story files'))

        from app.models import Story, StorySource
        story = Story.query.filter_by(literotica_url=item.url).first()
        if story:
            story.is_combined = True
            story.chapter_count = len(all_urls)
            story.auto_refresh_excluded = True
            story.auto_refresh_exclusion_reason = "User-created combined story — cannot be auto-refreshed"
            story.auto_refresh_exclusion_type = 'combined'
            story.auto_update_enabled = False
            item.story_id = story.id
            story.sources = [StorySource(url=url, position=pos) for pos, url in enumerate(all_urls)]
            db.session.commit()
            self._ensure_complete_formats(story)

        log_action(f"Successfully saved combined story '{title}' from queue item {item.id}")
        return True

    def _ensure_complete_formats(self, story):
        """After a download completes, queue any missing format generation jobs."""
        import os as _os
        from app.models import StoryFormat, FormatQueueItem
        from app.models.format_queue import FormatQueueItem as _FQI
        from app.models.base import db
        from app.services.story_processor import link_story_formats
        from app.services.logger import log_action
        from app.utils import story_epub_path, story_json_path

        try:
            link_story_formats(story)

            epub_fmt = StoryFormat.query.filter_by(story_id=story.id, format_type='epub').first()
            json_fmt = StoryFormat.query.filter_by(story_id=story.id, format_type='json').first()
            epub_ok = epub_fmt and _os.path.exists(epub_fmt.file_path)
            json_ok = json_fmt and _os.path.exists(json_fmt.file_path)

            queued = 0
            if not epub_ok:
                canonical = story_epub_path(story.id, story.filename_base)
                if _os.path.exists(canonical):
                    # File exists but no DB record — link_story_formats already handled it
                    pass
                elif not _FQI.query.filter_by(story_id=story.id, job_type='generate_epub', status='pending').first():
                    db.session.add(_FQI(story_id=story.id, job_type='generate_epub', method='auto'))
                    queued += 1

            if not json_ok:
                canonical = story_json_path(story.id, story.filename_base)
                if _os.path.exists(canonical):
                    pass
                elif not _FQI.query.filter_by(story_id=story.id, job_type='generate_json', status='pending').first():
                    db.session.add(_FQI(story_id=story.id, job_type='generate_json', method='auto'))
                    queued += 1

            if queued:
                db.session.commit()
                log_action(f"[DOWNLOAD WORKER] Queued {queued} missing format generation job(s) for story {story.id}")
                from flask import current_app
                if hasattr(current_app._get_current_object(), 'format_worker'):
                    current_app.format_worker.wake()
        except Exception:
            db.session.rollback()

    def _process_author_scan(self, item):
        """Scan an author URL and enqueue each discovered story/series as an individual 'single' queue item."""
        from app.models import DownloadQueueItem, Author, db
        from .author_scraper import AuthorScraper
        from .logger import log_action, log_error
        import json

        author_url = item.url
        item.progress_message = f'Scanning author page...'
        db.session.commit()

        scraper = AuthorScraper()
        stories = scraper.scrape_story_urls(author_url)

        if not stories:
            log_action(f"[AuthorScan] Author page loaded but no stories found: {author_url}")
            item.status = 'completed'
            item.progress_message = 'Author page scanned — no stories found'
            item.completed_at = datetime.utcnow()
            db.session.commit()
            return

        item.progress_message = f'Found {len(stories)} stories — queuing downloads...'
        db.session.commit()

        # Update known_story_urls so background watch can detect truly new stories later.
        all_urls = [s['url'] for s in stories]
        author_obj = Author.query.filter_by(literotica_url=author_url).first()
        if author_obj:
            author_obj.set_known_story_urls(all_urls)
            author_obj.last_watch_check_at = datetime.utcnow()
            db.session.commit()

        formats = item.get_formats()
        enqueued = 0

        for story in stories:
            story_url = story['url']

            # Skip if already active in the queue
            active = DownloadQueueItem.query.filter(
                DownloadQueueItem.url == story_url,
                DownloadQueueItem.status.in_(['pending', 'processing', 'rate_limited'])
            ).first()
            if active:
                continue

            # Skip if this URL has already been consumed (as a standalone story
            # or as a chapter inside a previously downloaded series).
            from app.models import SeenLiteroticaUrl
            if SeenLiteroticaUrl.query.filter_by(url=story_url).first():
                continue

            child = DownloadQueueItem(
                url=story_url,
                formats=json.dumps(formats),
                status='pending',
                job_type='single',
                title=story.get('title'),
                author=item.author,
                progress_message='Queued from author scan'
            )
            db.session.add(child)
            enqueued += 1

        db.session.commit()
        log_action(f"[AuthorScan] Queued {enqueued} new stories from {author_url}")
        item.title = item.title or f'Author scan: {author_url}'
        item.progress_message = f'Queued {enqueued} new stories for download'
        db.session.commit()
        if enqueued:
            self.wake()
