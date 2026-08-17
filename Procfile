web: PYTHONPATH=src gunicorn "lottery_tracker.web.app:create_app()" --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 60
