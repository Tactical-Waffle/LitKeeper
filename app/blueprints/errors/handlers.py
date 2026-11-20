from __future__ import annotations
from flask import Blueprint, render_template
from app.services import log_error

errors = Blueprint('errors', __name__)

@errors.app_errorhandler(404)
def not_found_error(error):
    # Not logged: a missing page is not an error in LitKeeper (a changing request that gets 404 is logged with it).
    return render_template('errors/404.html'), 404

@errors.app_errorhandler(500)
def internal_error(error):
    log_error(f"500 Error: {error}")
    return render_template('errors/500.html'), 500

@errors.app_errorhandler(403)
def forbidden_error(error):
    log_error(f"403 Error: {error}")
    return render_template('errors/403.html'), 403
