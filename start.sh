#!/bin/bash
python -c "from app import app, db;
with app.app_context(): db.create_all(); print('DB ready')"
exec gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 1
