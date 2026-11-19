#!/bin/bash
set -e

DATA_DIR="/litkeeparr/app/data"
STORIES_DIR="/litkeeparr/app/stories"

mkdir -p "$STORIES_DIR/epubs" "$STORIES_DIR/html" "$STORIES_DIR/covers"

echo "Stories directory ready at $STORIES_DIR"

echo "Running database migrations..."
SKIP_BACKGROUND_WORKERS=true flask db upgrade

SCRIPT="/litkeeparr/update_epub_descriptions.py"
MIGRATION_FLAG="$DATA_DIR/.epub_descriptions_migrated"
if [ -f "$SCRIPT" ] && [ ! -f "$MIGRATION_FLAG" ]; then
    echo "Injecting missing DC:description into existing EPUBs (one-time)..."
    SKIP_BACKGROUND_WORKERS=true python "$SCRIPT"
    touch "$MIGRATION_FLAG"
fi

echo "Starting application..."
exec "$@"
