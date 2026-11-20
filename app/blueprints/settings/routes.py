from __future__ import annotations
from flask import render_template, jsonify, request
from flask.typing import ResponseReturnValue
from . import settings
from app.services.logger import log_error, log_action
from app.models import AppConfig, Story, db
from app.models.webauthn import WebAuthnCredential
import traceback
import os


@settings.route('/')
def index() -> ResponseReturnValue:
    from flask import url_for
    enable_library = os.getenv('ENABLE_LIBRARY', 'true').lower() == 'true'
    passkeys = WebAuthnCredential.query.order_by(WebAuthnCredential.created_at).all()
    auto_lock_config = AppConfig.query.filter_by(key='auto_lock_timeout').first()
    auto_lock_timeout = int(auto_lock_config.get_value()) if auto_lock_config else 0
    auto_watch_config = AppConfig.query.filter_by(key='auto_watch_authors_enabled').first()
    auto_watch_enabled = auto_watch_config.get_value() if auto_watch_config else False
    auto_update_config = AppConfig.query.filter_by(key='auto_update_enabled').first()
    auto_update_enabled = auto_update_config.get_value() if auto_update_config else False

    opds_keys = ['opds_enabled', 'opds_auth_enabled', 'opds_username']
    opds_cfgs = {c.key: c for c in AppConfig.query.filter(AppConfig.key.in_(opds_keys)).all()}
    opds_enabled = opds_cfgs['opds_enabled'].get_value() if 'opds_enabled' in opds_cfgs else False
    opds_auth_enabled = opds_cfgs['opds_auth_enabled'].get_value() if 'opds_auth_enabled' in opds_cfgs else False
    opds_username = opds_cfgs['opds_username'].value if 'opds_username' in opds_cfgs else ''
    opds_url = url_for('opds.root', _external=True)
    covers_category_config = AppConfig.query.filter_by(key='covers_show_category').first()
    covers_show_category = covers_category_config.get_value() if covers_category_config else False

    from app.services import notifier
    notification_connections = [notifier.public_view(c) for c in notifier.load_connections()]
    from app.services import ai_cover, ai_prompts
    from app.services import download_queue_worker as dq

    return render_template(
        'settings.html',
        enable_library=enable_library,
        passkeys=passkeys,
        auto_lock_timeout=auto_lock_timeout,
        auto_watch_enabled=auto_watch_enabled,
        auto_update_enabled=auto_update_enabled,
        opds_enabled=opds_enabled,
        opds_auth_enabled=opds_auth_enabled,
        opds_username=opds_username,
        opds_url=opds_url,
        covers_show_category=covers_show_category,
        notification_connections=notification_connections,
        notification_services=notifier.SERVICES,
        notification_events=notifier.EVENTS,
        notification_default_events=notifier.DEFAULT_EVENTS,
        notification_env_count=notifier.env_url_count(),
        ai_cover_settings=ai_cover.public_settings(),
        ai_cover_story_text=ai_cover.STORY_TEXT,
        ai_cover_lettering=ai_cover.LETTERING,
        ai_cover_parallel_max=ai_cover.PARALLEL_MAX,
        ai_cover_prompts=ai_prompts.overview(),
        log_files=_log_file_list(),
        story_filter_terms=_load_story_filter_terms(),
        log_retention_days=_log_retention_days(),
        daily_download_limit=dq.get_daily_cap(),
        daily_download_default=dq.DEFAULT_MAX_DAILY_DOWNLOADS,
        daily_download_max=dq.MAX_DAILY_DOWNLOADS_LIMIT,
    )


_LOG_LABELS = {'litkeeper.log': 'Main Log', 'api.log': 'API Log'}


def _log_retention_days() -> int:
    from app.services.logger import log_retention_days
    return log_retention_days()


def _log_file_list() -> list[dict]:
    from app.services.logger import LOG_FILES, log_file_path
    files = []
    for name in LOG_FILES:
        path = log_file_path(name)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if size >= 1024 * 1024:
            shown = f'{size / (1024 * 1024):.1f} MB'
        elif size >= 1024:
            shown = f'{size / 1024:.0f} KB'
        else:
            shown = 'empty' if size == 0 else f'{size} B'
        files.append({'name': name, 'label': _LOG_LABELS.get(name, name), 'size': shown})
    return files


@settings.route('/logs/<name>', methods=['GET'])
def download_log(name: str) -> ResponseReturnValue:
    """Download one of LitKeeper's own log files (only the names in LOG_FILES are allowed)."""
    from flask import abort, send_file
    from app.services.logger import LOG_FILES, log_file_path
    if name not in LOG_FILES:
        abort(404)
    path = log_file_path(name)
    if not os.path.exists(path):
        open(path, 'a').close()
    return send_file(path, mimetype='text/plain', as_attachment=True, download_name=name, max_age=0)


LOG_TAIL_DEFAULT = 500
LOG_TAIL_MAX = 5000


@settings.route('/logs/<name>/tail', methods=['GET'])
def view_log(name: str) -> ResponseReturnValue:
    """The newest lines of a log, for reading in Settings without downloading it."""
    from flask import abort
    from app.services.logger import LOG_FILES, tail_log
    if name not in LOG_FILES:
        abort(404)
    try:
        count = int(request.args.get('lines', LOG_TAIL_DEFAULT))
    except ValueError:
        count = LOG_TAIL_DEFAULT
    count = max(1, min(count, LOG_TAIL_MAX))
    lines = tail_log(name, count)
    return jsonify({'success': True, 'name': name, 'lines': lines, 'count': len(lines)})


@settings.route('/theme-preference', methods=['GET'])
def get_theme_preference() -> ResponseReturnValue:
    try:
        theme_config = AppConfig.query.filter_by(key='theme_preference').first()
        theme = theme_config.get_value() if theme_config else 'system'
        return jsonify({"success": True, "theme": theme})
    except Exception as e:
        error_msg = f"Error getting theme preference: {str(e)}\n{traceback.format_exc()}"
        log_error(error_msg)
        return jsonify({"success": False, "message": "Error loading theme preference"}), 500


@settings.route('/theme-preference', methods=['POST'])
def save_theme_preference() -> ResponseReturnValue:
    try:
        data = request.get_json()
        theme = data.get('theme', 'system')

        if theme not in ['light', 'dark', 'system']:
            return jsonify({"success": False, "message": "Invalid theme value"}), 400

        theme_config = AppConfig.query.filter_by(key='theme_preference').first()
        if theme_config:
            theme_config.set_value(theme)
        else:
            theme_config = AppConfig(
                key='theme_preference',
                value=theme,
                value_type='string',
                description='User theme preference (light, dark, or system)'
            )
            db.session.add(theme_config)

        db.session.commit()
        log_action(f"Theme preference updated to: {theme}")

        return jsonify({"success": True, "theme": theme})
    except Exception as e:
        db.session.rollback()
        error_msg = f"Error saving theme preference: {str(e)}\n{traceback.format_exc()}"
        log_error(error_msg)
        return jsonify({"success": False, "message": "Error saving theme preference"}), 500


@settings.route('/auto-update-enabled', methods=['GET'])
def get_auto_update_enabled() -> ResponseReturnValue:
    try:
        auto_update_config = AppConfig.query.filter_by(key='auto_update_enabled').first()
        enabled = auto_update_config.get_value() if auto_update_config else False
        return jsonify({"success": True, "enabled": enabled})
    except Exception as e:
        error_msg = f"Error getting auto-update setting: {str(e)}\n{traceback.format_exc()}"
        log_error(error_msg)
        return jsonify({"success": False, "message": "Error loading auto-update setting"}), 500


def _cron_to_readable(cron_schedule: str) -> str:
    """Convert cron schedule to human-readable format (UTC time)."""
    try:
        parts = cron_schedule.strip().split()
        if len(parts) != 5:
            return cron_schedule
        
        minute = parts[0]
        hour = parts[1]
        day_of_week = parts[4]
        
        day_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        day_name = day_names[int(day_of_week)] if day_of_week.isdigit() and 0 <= int(day_of_week) <= 6 else f"day {day_of_week}"
        
        hour_int = int(hour) if hour.isdigit() else 0
        minute_int = int(minute) if minute.isdigit() else 0
        
        time_str = f"{hour_int:02d}:{minute_int:02d} UTC"
        
        return f"Weekly on {day_name} at {time_str}"
    except Exception:
        return cron_schedule


@settings.route('/auto-update-schedule', methods=['GET'])
def get_auto_update_schedule() -> ResponseReturnValue:
    try:
        from flask import current_app
        
        schedule_config = AppConfig.query.filter_by(key='auto_update_cron_schedule').first()
        if schedule_config:
            schedule = schedule_config.get_value()
            return jsonify({
                "success": True,
                "schedule": schedule,
                "schedule_readable": _cron_to_readable(schedule),
                "source": "database"
            })
        
        active_cron = current_app.config.get('ACTIVE_CRON')
        if active_cron:
            return jsonify({
                "success": True,
                "schedule": active_cron,
                "schedule_readable": _cron_to_readable(active_cron),
                "source": "app_config"
            })
        
        return jsonify({
            "success": True,
            "schedule": None,
            "schedule_readable": None,
            "source": None,
            "message": "No schedule configured yet. Will be generated on first startup."
        })
    except Exception as e:
        error_msg = f"Error getting auto-update schedule: {str(e)}\n{traceback.format_exc()}"
        log_error(error_msg)
        return jsonify({"success": False, "message": "Error loading schedule"}), 500


@settings.route('/regenerate-covers-new', methods=['POST'])
def regenerate_covers() -> ResponseReturnValue:
    try:
        from app.services.bulk_format_generator import BulkFormatGeneratorService
        service = BulkFormatGeneratorService()
        result = service.regenerate_all_covers()
        if result['failed'] > 0:
            return f'<p class="text-sm text-yellow-600 mt-2">Done: {result["successful"]} succeeded, {result["failed"]} failed.</p>'
        return f'<p class="text-sm text-green-600 mt-2">Done: regenerated {result["successful"]} covers.</p>'
    except Exception as e:
        log_error(f"Error in regenerate_covers: {str(e)}\n{traceback.format_exc()}")
        return '<p class="text-sm text-red-600 mt-2">An error occurred. Check the logs.</p>', 500


@settings.route('/regenerate-koreader-covers', methods=['POST'])
def regenerate_koreader_covers() -> ResponseReturnValue:
    try:
        from app.services.bulk_format_generator import BulkFormatGeneratorService
        service = BulkFormatGeneratorService()
        result = service.regenerate_all_koreader_covers()
        if result['failed'] > 0:
            return f'<p class="text-sm text-yellow-600 mt-2">Done: {result["successful"]} succeeded, {result["failed"]} failed.</p>'
        return f'<p class="text-sm text-green-600 mt-2">Done: regenerated {result["successful"]} e-reader covers.</p>'
    except Exception as e:
        log_error(f"Error in regenerate_koreader_covers: {str(e)}\n{traceback.format_exc()}")
        return '<p class="text-sm text-red-600 mt-2">An error occurred. Check the logs.</p>', 500


@settings.route('/toggle-covers-show-category', methods=['POST'])
def toggle_covers_show_category() -> ResponseReturnValue:
    try:
        config = AppConfig.query.filter_by(key='covers_show_category').first()
        if config:
            config.set_value(not config.get_value())
        else:
            config = AppConfig(key='covers_show_category', value_type='bool', description='Show abbreviated category label on generated cover images')
            config.set_value(True)
            db.session.add(config)
        db.session.commit()
        enabled = config.get_value()
        checked = 'checked' if enabled else ''
        return f'''<label id="coversCategoryToggle" class="flex items-center gap-3 cursor-pointer">
  <div class="relative">
    <input type="checkbox" class="sr-only peer" {checked}
      hx-post="/settings/toggle-covers-show-category" hx-target="#coversCategoryToggle" hx-swap="outerHTML">
    <div class="w-11 h-6 bg-gray-200 dark:bg-gray-700 peer-checked:bg-primary rounded-full transition-colors duration-200 peer-focus:ring-2 peer-focus:ring-primary/30"></div>
    <div class="absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform duration-200 peer-checked:translate-x-5"></div>
  </div>
  <div>
    <span class="text-sm font-medium text-gray-700 dark:text-gray-300">Show category on covers</span>
    <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">Displays an abbreviated category label on generated cover images.</p>
  </div>
</label>'''
    except Exception as e:
        log_error(f"Error toggling covers_show_category: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"success": False, "message": "Error updating setting"}), 500


@settings.route('/repair-epub-metadata', methods=['POST'])
def repair_epub_metadata() -> ResponseReturnValue:
    try:
        from app.services.bulk_format_generator import BulkFormatGeneratorService
        service = BulkFormatGeneratorService()
        result = service.repair_all_epub_metadata()
        if result['errors']:
            return f'<p class="text-sm text-yellow-600 mt-2">Done: {result["repaired"]} repaired, {result["skipped"]} already clean, {len(result["errors"])} errors.</p>'
        return f'<p class="text-sm text-green-600 mt-2">Done: {result["repaired"]} repaired, {result["skipped"]} already clean.</p>'
    except Exception as e:
        log_error(f"Error in repair_epub_metadata: {str(e)}\n{traceback.format_exc()}")
        return '<p class="text-sm text-red-600 mt-2">An error occurred. Check the logs.</p>', 500


@settings.route('/auto-watch-enabled', methods=['GET'])
def get_auto_watch_enabled() -> ResponseReturnValue:
    try:
        cfg = AppConfig.query.filter_by(key='auto_watch_authors_enabled').first()
        enabled = cfg.get_value() if cfg else False
        return jsonify({"success": True, "enabled": enabled})
    except Exception as e:
        log_error(f"Error getting auto-watch setting: {str(e)}")
        return jsonify({"success": False, "message": "Error loading setting"}), 500


@settings.route('/toggle-auto-watch', methods=['POST'])
def toggle_auto_watch() -> ResponseReturnValue:
    try:
        data = request.get_json()
        enabled = data.get('enabled', False)
        cfg = AppConfig.query.filter_by(key='auto_watch_authors_enabled').first()
        if cfg:
            cfg.set_value(enabled)
        else:
            cfg = AppConfig(
                key='auto_watch_authors_enabled',
                value='true' if enabled else 'false',
                value_type='bool',
                description='Auto-download new stories from watched authors on schedule'
            )
            db.session.add(cfg)
        db.session.commit()
        log_action(f"Auto-watch-authors setting changed to: {enabled}")
        return jsonify({"success": True, "enabled": enabled})
    except Exception as e:
        db.session.rollback()
        log_error(f"Error toggling auto-watch: {str(e)}\n{traceback.format_exc()}")
        return jsonify({"success": False, "message": "Error updating setting"}), 500


@settings.route('/opds', methods=['GET'])
def get_opds_settings() -> ResponseReturnValue:
    try:
        from flask import url_for
        keys = ['opds_enabled', 'opds_auth_enabled', 'opds_username']
        cfgs = {c.key: c for c in AppConfig.query.filter(AppConfig.key.in_(keys)).all()}
        return jsonify({
            'success': True,
            'opds_enabled': cfgs['opds_enabled'].get_value() if 'opds_enabled' in cfgs else False,
            'opds_auth_enabled': cfgs['opds_auth_enabled'].get_value() if 'opds_auth_enabled' in cfgs else False,
            'opds_username': cfgs['opds_username'].value if 'opds_username' in cfgs else '',
            'opds_url': url_for('opds.root', _external=True),
        })
    except Exception as e:
        log_error(f"Error getting OPDS settings: {str(e)}")
        return jsonify({'success': False, 'message': 'Error loading OPDS settings'}), 500


@settings.route('/opds', methods=['POST'])
def save_opds_settings() -> ResponseReturnValue:
    from werkzeug.security import generate_password_hash
    try:
        data = request.get_json()
        _missing = object()
        all_updates = {
            'opds_enabled': ('bool', data.get('opds_enabled', _missing)),
            'opds_auth_enabled': ('bool', data.get('opds_auth_enabled', _missing)),
            'opds_username': ('string', data.get('opds_username', _missing)),
        }
        updates = {k: (vt, v) for k, (vt, v) in all_updates.items() if v is not _missing}
        if 'opds_username' in updates:
            updates['opds_username'] = ('string', (updates['opds_username'][1] or '').strip())
        new_password = (data.get('opds_password') or '').strip()

        for key, (value_type, value) in updates.items():
            cfg = AppConfig.query.filter_by(key=key).first()
            if cfg:
                cfg.value_type = value_type
                cfg.set_value(value)
            else:
                cfg = AppConfig(key=key, value_type=value_type, description='')
                cfg.set_value(value)
                db.session.add(cfg)

        if new_password:
            pw_cfg = AppConfig.query.filter_by(key='opds_password_hash').first()
            if pw_cfg:
                pw_cfg.value = generate_password_hash(new_password)
            else:
                pw_cfg = AppConfig(key='opds_password_hash', value_type='string',
                                   description='OPDS Basic Auth password (bcrypt hash)')
                pw_cfg.value = generate_password_hash(new_password)
                db.session.add(pw_cfg)

        db.session.commit()
        log_action("OPDS settings updated")
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        log_error(f"Error saving OPDS settings: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'message': 'Error saving OPDS settings'}), 500


STORY_FILTER_KEY = 'story_filter_terms'
STORY_FILTER_MAX_TERMS = 200
STORY_FILTER_MAX_LENGTH = 100


def _load_story_filter_terms() -> list[str]:
    cfg = AppConfig.query.filter_by(key=STORY_FILTER_KEY).first()
    value = cfg.get_value() if cfg else None
    return [str(t) for t in value] if isinstance(value, list) else []


@settings.route('/story-filter', methods=['POST'])
def save_story_filter() -> ResponseReturnValue:
    """Settings → Story Filter: words or phrases, one per line or separated by commas."""
    import re
    data = request.get_json(silent=True) or {}
    raw = data.get('terms', '')
    pieces = raw if isinstance(raw, list) else re.split(r'[\n,]+', str(raw or ''))
    terms = list(dict.fromkeys(t.strip() for t in map(str, pieces) if t.strip()))
    if len(terms) > STORY_FILTER_MAX_TERMS:
        return jsonify({'success': False, 'message': f'Use at most {STORY_FILTER_MAX_TERMS} terms.'}), 400
    if any(len(t) > STORY_FILTER_MAX_LENGTH for t in terms):
        return jsonify({'success': False, 'message': f'Keep each term under {STORY_FILTER_MAX_LENGTH} characters.'}), 400
    try:
        cfg = AppConfig.query.filter_by(key=STORY_FILTER_KEY).first()
        if not cfg:
            cfg = AppConfig(key=STORY_FILTER_KEY, value_type='json', description='Story filter terms for the author window')
            db.session.add(cfg)
        cfg.value_type = 'json'
        cfg.set_value(terms)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log_error(f"Error saving story filter terms: {str(e)}")
        return jsonify({'success': False, 'message': 'Error saving the filter terms'}), 500
    log_action(f"Story filter terms saved ({len(terms)})")
    return jsonify({'success': True, 'terms': terms})


@settings.route('/daily-downloads', methods=['POST'])
def save_daily_downloads() -> ResponseReturnValue:
    from flask import current_app
    from app.services import download_queue_worker as dq
    data = request.get_json(silent=True) or {}
    try:
        limit = dq.save_daily_cap(data.get('limit'))
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        log_error(f"Error saving daily download limit: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'message': 'Error saving the daily download limit'}), 500
    log_action(f"Daily download limit set to {limit}")
    # Let the worker pick up a raised limit straight away instead of on its next poll.
    worker = getattr(current_app, 'download_worker', None)
    if worker:
        worker.wake()
    return jsonify({'success': True, 'limit': limit})


@settings.route('/toggle-auto-update', methods=['POST'])
def toggle_auto_update() -> ResponseReturnValue:
    try:
        data = request.get_json()
        enabled = data.get('enabled', True)

        auto_update_config = AppConfig.query.filter_by(key='auto_update_enabled').first()
        if auto_update_config:
            auto_update_config.set_value(enabled)
        else:
            auto_update_config = AppConfig(
                key='auto_update_enabled',
                value='true' if enabled else 'false',
                value_type='bool',
                description='Global setting to enable/disable automatic story updates'
            )
            db.session.add(auto_update_config)

        db.session.commit()
        log_action(f"Auto-update setting changed to: {enabled}")

        return jsonify({"success": True, "enabled": enabled})
    except Exception as e:
        db.session.rollback()
        error_msg = f"Error toggling auto-update: {str(e)}\n{traceback.format_exc()}"
        log_error(error_msg)
        return jsonify({"success": False, "message": "Error updating auto-update setting"}), 500




# --- Notifications -------------------------------------------------------------------------

def _notification_payload() -> dict:
    data = request.get_json(silent=True) or {}
    return data if isinstance(data, dict) else {}


@settings.route('/notifications', methods=['GET'])
def list_notifications() -> ResponseReturnValue:
    from app.services import notifier
    try:
        return jsonify({
            'success': True,
            'connections': [notifier.public_view(c) for c in notifier.load_connections()],
            'env_url_count': notifier.env_url_count(),
        })
    except Exception as e:
        log_error(f"Error loading notification connections: {str(e)}")
        return jsonify({'success': False, 'message': 'Error loading notifications'}), 500


@settings.route('/notifications', methods=['POST'])
def add_notification() -> ResponseReturnValue:
    from app.services import notifier
    data = _notification_payload()
    try:
        connection = notifier.new_connection(data.get('name', ''), data.get('service', ''),
                                             data.get('fields') or {}, data.get('events') or [])
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    try:
        connections = notifier.load_connections()
        connections.append(connection)
        notifier.save_connections(connections)
        log_action(f"Notification connection added: {connection['name']} ({connection['service']})")
        return jsonify({'success': True, 'connection': notifier.public_view(connection)})
    except Exception as e:
        db.session.rollback()
        log_error(f"Error saving notification connection: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'message': 'Error saving notification'}), 500


@settings.route('/notifications/<connection_id>', methods=['POST'])
def update_notification(connection_id: str) -> ResponseReturnValue:
    """Change a saved connection's name or events. Keys are changed by removing and re-adding it."""
    from app.services import notifier
    data = _notification_payload()
    try:
        connections = notifier.load_connections()
        connection = next((c for c in connections if c.get('id') == connection_id), None)
        if not connection:
            return jsonify({'success': False, 'message': 'Notification not found'}), 404
        if 'events' in data:
            connection['events'] = notifier.clean_events(data.get('events'))
        if 'name' in data:
            connection['name'] = (data.get('name') or '').strip()[:60] or connection['name']
        notifier.save_connections(connections)
        return jsonify({'success': True, 'connection': notifier.public_view(connection)})
    except Exception as e:
        db.session.rollback()
        log_error(f"Error updating notification connection: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'message': 'Error saving notification'}), 500


@settings.route('/notifications/<connection_id>', methods=['DELETE'])
def delete_notification(connection_id: str) -> ResponseReturnValue:
    from app.services import notifier
    try:
        connections = notifier.load_connections()
        remaining = [c for c in connections if c.get('id') != connection_id]
        if len(remaining) == len(connections):
            return jsonify({'success': False, 'message': 'Notification not found'}), 404
        notifier.save_connections(remaining)
        log_action("Notification connection removed")
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        log_error(f"Error removing notification connection: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'message': 'Error removing notification'}), 500


def _test_result(url: str) -> ResponseReturnValue:
    from app.services import notifier
    if notifier.send_test(url):
        return jsonify({'success': True, 'message': 'Test notification sent'})
    return jsonify({'success': False,
                    'message': 'The service did not accept the test notification. Check the details and try again.'}), 502


@settings.route('/notifications/test', methods=['POST'])
def test_new_notification() -> ResponseReturnValue:
    """Send a test with details that have not been saved yet."""
    from app.services import notifier
    data = _notification_payload()
    try:
        url = notifier.build_url(data.get('service', ''), data.get('fields') or {})
        notifier.check_url(url)
    except ValueError as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    return _test_result(url)


@settings.route('/notifications/<connection_id>/test', methods=['POST'])
def test_saved_notification(connection_id: str) -> ResponseReturnValue:
    from app.services import notifier
    connection = next((c for c in notifier.load_connections() if c.get('id') == connection_id), None)
    if not connection:
        return jsonify({'success': False, 'message': 'Notification not found'}), 404
    try:
        url = notifier.build_url(connection['service'], connection.get('fields') or {})
    except (KeyError, ValueError) as e:
        return jsonify({'success': False, 'message': str(e)}), 400
    return _test_result(url)


# --- AI covers -----------------------------------------------------------------------------

@settings.route('/ai-covers', methods=['POST'])
def save_ai_cover_settings() -> ResponseReturnValue:
    from app.services import ai_cover
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    try:
        ai_cover.save_settings(data)
    except ValueError as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        log_error(f"Error saving AI cover settings: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'message': 'Error saving AI cover settings'}), 500
    log_action("AI cover settings updated")
    return jsonify({'success': True, 'settings': ai_cover.public_settings()})


@settings.route('/ai-covers/check', methods=['POST'])
def check_ai_cover_models() -> ResponseReturnValue:
    from app.services import ai_cover
    try:
        results = ai_cover.check_models()
    except ValueError as e:
        results = [(False, str(e))]
    return jsonify({'success': True, 'results': [{'ok': ok, 'message': message} for ok, message in results]})


@settings.route('/ai-covers/prompts', methods=['POST'])
def save_ai_cover_prompts() -> ResponseReturnValue:
    from app.services import ai_prompts
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    try:
        ai_prompts.save(data)
    except ValueError as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 400
    return jsonify({'success': True, 'prompts': ai_prompts.overview()})
