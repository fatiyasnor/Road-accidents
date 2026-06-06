#!/bin/bash
set -e
echo "==> DATABASE_URL set: $([ -n "$DATABASE_URL" ] && echo YES || echo NO — using SQLite)"
python -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('==> Database tables ready')
"
exec gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1
