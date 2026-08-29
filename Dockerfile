# Valley Lotto web app (dashboard + scan capture).
#
# Build:  docker build -t valley-lotto .
# Run:    docker run -p 8000:8000 -e SECRET_KEY=... -e DATABASE_URL=... valley-lotto
#
# Notes for hosted platforms (Railway/Render/Fly):
#   * No VOLUME directive — Railway rejects it; persistence comes from Postgres
#     (DATABASE_URL, e.g. Supabase), not from the container filesystem.
#   * Binds to $PORT when the platform provides one, else 8000.
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PORT=8000

COPY requirements.txt requirements-app.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-app.txt

COPY src ./src
COPY config.yaml ./config.yaml

# The scraper's catalog snapshot ships with the image so the dashboard has PA
# data on boot (game names, prices, prize counts). The bulky raw/snapshot
# archives stay out of the image — see .dockerignore.
COPY data/state.json data/originals.json ./data/

EXPOSE 8000

# gunicorn serves the Flask app (login + guided scan capture + daily reports).
CMD ["sh", "-c", "gunicorn 'lottery_tracker.web.app:create_app()' --workers 1 --threads 4 --bind 0.0.0.0:${PORT:-8000} --timeout 60"]
