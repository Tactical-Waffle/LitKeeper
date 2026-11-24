from __future__ import annotations
from flask import Blueprint, render_template, jsonify, request
from flask.typing import ResponseReturnValue
from app.models import DownloadQueueItem, db
from app.services import log_error
from app.services.logger import log_action
from sqlalchemy import desc, and_, not_

queue = Blueprint('queue', __name__, url_prefix='/queue')


@queue.app_context_processor
def _download_queue_active() -> dict:
    """The number on the header's Queue button: stories waiting or downloading now."""
    try:
        count = DownloadQueueItem.query.filter(DownloadQueueItem.status.in_(('pending', 'processing'))).count()
    except Exception:
        count = 0
    return {'download_queue_active': count}

def _visible_items_query():
    """Items shown in the queue list — excludes skipped duplicates and completed author scans."""
    return DownloadQueueItem.query.filter(
        DownloadQueueItem.status != 'skipped',
        not_(and_(
            DownloadQueueItem.job_type == 'author',
            DownloadQueueItem.status == 'completed'
        ))
    )


def _minutes_until(when) -> int | None:
    from datetime import datetime
    if not when:
        return None
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when)
        except ValueError:
            return None
    seconds = (when - datetime.utcnow()).total_seconds()
    return max(1, round(seconds / 60)) if seconds > 0 else None


def _list_values(items) -> dict:
    """The queue list's items, each waiting retry with how long until it is tried again, and the
    wait before the next download when stories are waiting for it."""
    from app.services.download_queue_worker import next_download_at
    shown = []
    for item in items:
        entry = item.to_dict()
        entry['retry_in'] = _minutes_until(item.scheduled_after) if item.status == 'pending' and item.retry_count else None
        shown.append(entry)
    waiting = DownloadQueueItem.query.filter_by(status='pending').count()
    return {'items': shown, 'next_download_in': _minutes_until(next_download_at()) if waiting else None}


def _shown_items() -> list:
    """The queue list in the order it runs: downloading now, then the waiting ones in the order they will be
    downloaded (drag to change it), then the history, newest first."""
    active = _visible_items_query().filter(DownloadQueueItem.status.in_(('processing', 'pending', 'rate_limited'))) \
        .order_by(DownloadQueueItem.created_at.asc()).all()
    rank = {'processing': 0, 'pending': 1, 'rate_limited': 2}
    active.sort(key=lambda item: rank[item.status])  # A stable sort keeps the waiting order.
    history = _visible_items_query().filter(DownloadQueueItem.status.notin_(('processing', 'pending', 'rate_limited'))) \
        .order_by(desc(DownloadQueueItem.created_at)).limit(max(20, 70 - len(active))).all()
    return active + history


@queue.route('/')
def index() -> ResponseReturnValue:
    all_items = _shown_items()

    rate_limited_count = DownloadQueueItem.query.filter_by(status='rate_limited').count()
    return render_template('queue/index.html',
                           pending=DownloadQueueItem.query.filter_by(status='pending').count(),
                           processing=DownloadQueueItem.query.filter_by(status='processing').count(),
                           completed=DownloadQueueItem.query.filter_by(status='completed').count(),
                           failed=DownloadQueueItem.query.filter_by(status='failed').count(),
                           rate_limited=rate_limited_count,
                           queue_list=_list_values(all_items),
                           ai=_ai_queue(), metadata=_metadata_queue(), **_paused())

@queue.route('/api/items')
def get_queue_items() -> ResponseReturnValue:
    """Get all queue items grouped by status"""
    try:
        pending = DownloadQueueItem.query.filter_by(status='pending').order_by(DownloadQueueItem.created_at.asc()).all()
        processing = DownloadQueueItem.query.filter_by(status='processing').order_by(DownloadQueueItem.started_at.desc()).all()
        completed = DownloadQueueItem.query.filter_by(status='completed').order_by(desc(DownloadQueueItem.completed_at)).limit(50).all()
        failed = DownloadQueueItem.query.filter_by(status='failed').order_by(desc(DownloadQueueItem.completed_at)).limit(20).all()

        return jsonify({
            'pending': [item.to_dict() for item in pending],
            'processing': [item.to_dict() for item in processing],
            'completed': [item.to_dict() for item in completed],
            'failed': [item.to_dict() for item in failed]
        })
    except Exception as e:
        log_error(f"Error fetching queue items: {str(e)}")
        return jsonify({'error': 'Failed to fetch queue items'}), 500

@queue.route('/api/items/<int:item_id>')
def get_queue_item(item_id: int) -> ResponseReturnValue:
    """Get a single queue item by ID"""
    try:
        item = db.session.get(DownloadQueueItem, item_id)
        if not item:
            return jsonify({'error': 'Item not found'}), 404
        return jsonify(item.to_dict())
    except Exception as e:
        log_error(f"Error fetching queue item {item_id}: {str(e)}")
        return jsonify({'error': 'Failed to fetch queue item'}), 500

def _paused() -> dict:
    """Downloads paused until tomorrow after failures in a row, for the Queue page."""
    from app.services import download_queue_worker as dq
    until = dq.paused_until()
    return {'paused_until': f"{until:%H:%M} UTC" if until else None, 'pause_after': dq.PAUSE_AFTER_FAILURES}


@queue.route('/api/downloads/resume', methods=['POST'])
def resume_downloads() -> ResponseReturnValue:
    """Resume Now: start downloading again before tomorrow."""
    from flask import current_app
    from app.services import download_queue_worker as dq
    dq.resume_downloads()
    log_action("[Queue] Downloads resumed by hand")
    worker = getattr(current_app, 'download_worker', None)
    if worker:
        worker.wake()
    return jsonify({'success': True})


@queue.route('/api/stats')
def get_queue_stats() -> ResponseReturnValue:
    """Get queue statistics"""
    try:
        stats = {
            'pending': DownloadQueueItem.query.filter_by(status='pending').count(),
            'processing': DownloadQueueItem.query.filter_by(status='processing').count(),
            'completed': DownloadQueueItem.query.filter_by(status='completed').count(),
            'failed': DownloadQueueItem.query.filter_by(status='failed').count(),
            'rate_limited': DownloadQueueItem.query.filter_by(status='rate_limited').count(),
        }
        return jsonify(stats)
    except Exception as e:
        log_error(f"Error fetching queue stats: {str(e)}")
        return jsonify({'error': 'Failed to fetch stats'}), 500

@queue.route('/partials/stats')
def queue_stats_partial() -> ResponseReturnValue:
    """HTMX partial for queue stats"""
    try:
        stats = {
            'pending': DownloadQueueItem.query.filter_by(status='pending').count(),
            'processing': DownloadQueueItem.query.filter_by(status='processing').count(),
            'completed': DownloadQueueItem.query.filter_by(status='completed').count(),
            'failed': DownloadQueueItem.query.filter_by(status='failed').count(),
            'rate_limited': DownloadQueueItem.query.filter_by(status='rate_limited').count(),
        }
        return render_template('queue/partials/stats.html', **stats, **_paused())
    except Exception as e:
        log_error(f"Error rendering queue stats: {str(e)}")
        return '<div class="text-red-600 dark:text-red-400">Error loading stats</div>', 500

@queue.route('/partials/list')
def queue_list_partial() -> ResponseReturnValue:
    """HTMX partial for queue list"""
    try:
        return render_template('queue/partials/queue_list.html', **_list_values(_shown_items()))
    except Exception as e:
        log_error(f"Error rendering queue list: {str(e)}")
        return '<div class="text-red-600 dark:text-red-400">Error loading queue</div>', 500


def _ago(iso: str) -> str:
    """'just now', '5 min ago', '3 h ago', '2 days ago' for an ISO time; '' if unknown."""
    from datetime import datetime, timezone
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ''
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return f'{int(seconds // 60)} min ago'
    if seconds < 86400:
        return f'{int(seconds // 3600)} h ago'
    days = int(seconds // 86400)
    return f'{days} day{"s" if days != 1 else ""} ago'


def _ai_cover_queue() -> dict:
    """The AI cover queue with story titles filled in, for the Queue page."""
    from app.models import Story
    from app.services import ai_cover
    overview = ai_cover.queue_overview()
    ids = {r['story_id'] for r in overview['running'] + overview['waiting'] + overview['recent']}
    stories = {s.id: s for s in Story.query.filter(Story.id.in_(ids)).all()} if ids else {}

    def described(entry: dict) -> dict:
        story = stories.get(entry['story_id'])
        when = entry.get('started') or entry.get('queued_at') or entry.get('when') or ''
        return {**entry, 'title': story.title if story else f"Story {entry['story_id']} (deleted)",
                'author': story.author.name if story and story.author else '', 'ago': _ago(when)}

    running = [described(e) for e in overview['running']]
    waiting = [described(e) for e in overview['waiting']]
    recent = [described(e) for e in overview['recent'] if e['story_id'] in stories]
    return {'running': running, 'waiting': waiting, 'recent': recent,
            'configured': bool(ai_cover.settings()['api_key']), 'parallel': ai_cover.parallel_limit()}


FAILURES_SHOWN_FOR_HOURS = 24


def _ai_queue() -> dict:
    """All AI work for the Queue page: AI covers, AI Rewrite and AI summaries, each running on its own beside
    the downloads. Finished covers leave the list; a cover that failed stays for a day so it can be seen."""
    from datetime import datetime, timedelta, timezone
    from app.models import Story
    from app.services import ai_cover, ai_rewrite, ai_summary
    covers = _ai_cover_queue()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FAILURES_SHOWN_FOR_HOURS)

    def recent(entry):
        try:
            when = datetime.fromisoformat(entry.get('when') or '')
            return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)) >= cutoff
        except ValueError:
            return False
    failed = [e for e in covers['recent'] if not e['ok'] and recent(e)]
    rewrite, summaries = ai_rewrite.overview(), ai_summary.overview()
    ids = {i for i in [rewrite['running'], summaries['running']] if i} | set(rewrite['waiting']) | set(summaries['waiting'])
    stories = {s.id: s for s in Story.query.filter(Story.id.in_(ids)).all()} if ids else {}

    def named(story_id):
        story = stories.get(story_id)
        return {'story_id': story_id, 'title': story.title if story else f'Story {story_id} (deleted)',
                'author': story.author.name if story and story.author else ''}
    live = ai_rewrite.progress() if rewrite['running'] else {}
    running_rewrite = None
    if rewrite['running']:
        running_rewrite = {**named(rewrite['running']),
                           'progress': (f"Part {live['number']} ({live['index']} of {live['total']})"
                                        + (f" · {live['eta_text']}" if live.get('eta_text') else '')
                                        + (' · stopping after this part' if live.get('stopping') else ''))
                           if live.get('total') else 'Starting'}
    result = {'covers': {**covers, 'failed': failed},
              'rewrite': {'running': running_rewrite, 'waiting': [named(i) for i in rewrite['waiting']]},
              'summary': {'running': named(summaries['running']) if summaries['running'] else None,
                          'waiting': [named(i) for i in summaries['waiting']]}}
    result['busy'] = bool(covers['running'] or covers['waiting'] or running_rewrite or rewrite['waiting']
                          or summaries['running'] or summaries['waiting'])
    result['shown'] = result['busy'] or bool(failed)
    return result


METADATA_SHOWN = 30  # Waiting Literotica updates listed by name; the rest are counted.


def _metadata_queue() -> dict:
    """Updates from Literotica (ratings, views, favorites): their own queue, separate from downloads."""
    from app.models import Story
    from app.services import metadata_update
    state = metadata_update.overview()
    shown_ids = state['waiting'][:METADATA_SHOWN]
    ids = set(shown_ids) | ({state['running']} if state['running'] else set())
    titles = dict(Story.query.with_entities(Story.id, Story.title).filter(Story.id.in_(ids)).all()) if ids else {}

    def named(story_id):
        return {'story_id': story_id, 'title': titles.get(story_id) or f'Story {story_id} (deleted)'}
    return {'running': named(state['running']) if state['running'] else None,
            'waiting': [named(i) for i in shown_ids],
            'more': max(0, len(state['waiting']) - METADATA_SHOWN),
            'waiting_count': len(state['waiting']),
            'paused_for': state['paused_for'],
            'failed': len(state['errors']),
            'shown': bool(state['running'] or state['waiting'])}


@queue.route('/partials/ai-covers')
def ai_covers_partial() -> ResponseReturnValue:
    """HTMX partial: the AI work (covers, rewrites, summaries)."""
    try:
        return render_template('queue/partials/ai_covers.html', ai=_ai_queue(), metadata=_metadata_queue())
    except Exception as e:
        log_error(f"Error rendering the AI queue: {str(e)}")
        return '<div class="text-red-600 dark:text-red-400 text-sm">Error loading the AI queue</div>', 500


@queue.route('/api/order', methods=['POST'])
def reorder() -> ResponseReturnValue:
    """Drag and drop on the Queue page. Body {"kind": "download" | "cover" | "rewrite" | "summary", "ids": [...]}:
    the waiting downloads (queue item ids) or waiting AI work (story ids) in their new order."""
    data = request.get_json(silent=True) or {}
    kind = data.get('kind')
    try:
        ids = [int(i) for i in data.get('ids') or []]
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'The order must be a list of numbers.'}), 400
    if kind == 'download':
        _reorder_downloads(ids)
    elif kind in ('cover', 'rewrite', 'summary', 'metadata'):
        from importlib import import_module
        import_module(f"app.services.{ {'cover': 'ai_cover', 'rewrite': 'ai_rewrite', 'summary': 'ai_summary', 'metadata': 'metadata_update'}[kind] }").reorder(ids)
    else:
        return jsonify({'success': False, 'message': 'Unknown queue.'}), 400
    log_action(f"[Queue] New order for waiting {kind} jobs: {', '.join(map(str, ids))}")
    return jsonify({'success': True})


def _reorder_downloads(ids: list) -> None:
    """Waiting downloads run oldest first, so the new order is saved by handing the same set of queue times
    out again in the new order: no other item moves, and nothing else about the items changes."""
    waiting = DownloadQueueItem.query.filter(DownloadQueueItem.status == 'pending').all()
    by_id = {item.id: item for item in waiting}
    ordered = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
    ordered += sorted((item for item in waiting if item not in ordered), key=lambda item: item.created_at)
    times = sorted(item.created_at for item in ordered)
    for item, when in zip(ordered, times):
        item.created_at = when
    db.session.commit()


@queue.route('/api/ai/<kind>/<int:story_id>', methods=['DELETE'])
def cancel_ai_job(kind: str, story_id: int) -> ResponseReturnValue:
    """Take a waiting AI Rewrite or AI summary out of the queue."""
    from app.services import ai_rewrite, ai_summary, metadata_update
    service = {'rewrite': ai_rewrite, 'summary': ai_summary, 'metadata': metadata_update}.get(kind)
    if not service:
        return jsonify({'success': False, 'message': 'Unknown queue.'}), 404
    if service.remove(story_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'That job is not waiting any more.'}), 409


@queue.route('/api/metadata', methods=['DELETE'])
def clear_metadata_updates() -> ResponseReturnValue:
    """Take every waiting update from Literotica out of the queue (the one running finishes)."""
    from app.services import metadata_update
    count = metadata_update.clear()
    log_action(f"[Queue] Took {count} waiting Literotica {'update' if count == 1 else 'updates'} out")
    return jsonify({'success': True, 'removed': count})


@queue.route('/api/ai-covers/<int:story_id>', methods=['DELETE'])
def cancel_ai_cover(story_id: int) -> ResponseReturnValue:
    """Take a waiting AI cover out of the queue."""
    from app.services import ai_cover
    if ai_cover.cancel(story_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'That cover is not waiting any more.'}), 409


@queue.route('/api/items/<int:item_id>', methods=['DELETE'])
def cancel_queue_item(item_id: int) -> ResponseReturnValue:
    """Cancel or remove a single queue item."""
    try:
        item = db.session.get(DownloadQueueItem, item_id)
        if not item:
            return jsonify({'success': False, 'message': 'Item not found'}), 404

        label = item.title or item.url
        if item.status == 'processing':
            item.status = 'failed'
            item.error_message = 'Cancelled by user'
            db.session.commit()
            log_action(f"[Queue] Cancelled processing item {item_id}: {label}")
        else:
            db.session.delete(item)
            db.session.commit()
            log_action(f"[Queue] Removed queue item {item_id}: {label}")

        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        log_error(f"Error cancelling queue item {item_id}: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to cancel item'}), 500


@queue.route('/api/clear', methods=['DELETE'])
def clear_history() -> ResponseReturnValue:
    """Delete all completed and failed queue items."""
    try:
        deleted = DownloadQueueItem.query.filter(
            DownloadQueueItem.status.in_(['completed', 'failed', 'skipped'])
        ).delete(synchronize_session=False)
        db.session.commit()
        log_action(f"[Queue] Cleared {deleted} completed/failed history items")
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        db.session.rollback()
        log_error(f"Error clearing queue history: {str(e)}")
        return jsonify({'success': False, 'message': 'Failed to clear history'}), 500
