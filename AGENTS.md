# LitKeeper (litkeeparr fork)

Instructions for coding agents (Codex, Claude Code and others) working in this repository. `README.md` describes
every feature; read the parts that touch what you change. `DEPLOY-LITKEEPARR.md` covers deployment.

## Branches and pushing

- Work on and push to **`litkeeparr`** (`origin` = `Tactical-Waffle/LitKeeper`). It holds all current work.
- `main` is an old state of the fork. Do not commit to it, merge into it, or base work on it.
- Never push to `upstream` (`redwoodstory/LitKeeper`), and never open pull requests or issues there.
- Never commit `CONTRIBUTING-DRAFT-ISSUE.md` (a local draft). Never commit credentials, `.env` files, personal data,
  or anything under `app/data/` or `app/stories/`.
- Every push to `litkeeparr` runs the tests on GitHub, then builds and publishes `ghcr.io/tactical-waffle/litkeeparr:latest`,
  which the user's server pulls. A push that fails the tests publishes nothing; one that passes goes live on the next
  `docker compose pull`. So run the full test suite before every push.
- Commit messages: a short summary line, then what changed and why in plain words.

## Checks

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q -p no:cacheprovider
```

All tests must pass (pytest treats warnings as errors; see `pytest.ini`). Tests run in temporary folders
(`tests/conftest.py`), never in `app/data/` or `app/stories/`. Add or update tests with every behaviour change.
For changes to pages, also load the page (`.venv/bin/python run.py`, port 5017) and check for browser console errors,
at phone width (375 px) too.

## Data to preserve

- `app/data/` holds the live SQLite library (`litkeeper.db`), settings, logs, AI statistics (`ai-calls.jsonl`) and
  saved queues (`ai-queues.json`). `app/stories/` holds EPUBs, reader files (`stories/html/<id>_<name>.json`), covers,
  AI rewrites (`.edited.json`) and AI summaries (`.summary.json`). Never delete or rewrite them as incidental cleanup.
- Schema changes need a migration that keeps existing rows (see `app/__init__.py` startup checks and `migrations/`).

## How the app works (things easy to break)

- Flask app factory in `app/__init__.py`; blueprints in `app/blueprints/`; services in `app/services/`; SQLite in
  WAL mode. Gunicorn runs **one worker** with threads (`gunicorn.docker.conf.py`): background workers and in-memory
  queues live in that process. Never add workers.
- Settings are `AppConfig` rows (JSON values) read through each service's `settings()`; keep defaults in code and
  keep older saved values working (several settings migrate old shapes on read, e.g. `ai_cover.settings()`).
- Downloads go through the database queue (`download_queue_worker.py`), paced and with a daily limit (0 = none);
  after 3 failures in a row downloads pause until midnight UTC. Literotica updates (`metadata_update.py`), AI covers,
  AI Rewrite and AI summaries each have their own in-memory queue, saved in `data/ai-queues.json` and restored at
  startup. All OpenRouter calls go through `ai_cover._post` / `ai_cover.chat` (retries, logging, statistics; the
  API key is never logged). Text models and image models are ordered lists tried in turn.
- `StoryFormat.json_data` is deferred: do not load it for lists of stories. The library pages and cover requests
  must stay light; covers are requested with `?v=<mtime>` and cached by the browser.
- The story window (`components/story_modal.html`) is rendered twice (phone and larger screens), so ids repeat:
  scripts must update every copy (select by class or data attribute). Its inline script runs on every opening, so
  never declare top-level `const`/`let` there.
- The AI summary is shown in place of the Literotica description; the original description is kept.
- Logging: one log, `app/data/logs/litkeeper.log`, through `app/services/logger.py`; log changes and problems, not routine polls.

## Working agreements

- Make the smallest change that does the job; keep existing conventions; no unrelated refactors.
- Update `README.md` when a feature or setting changes.
- Page text and messages use plain words a non-programmer understands.
