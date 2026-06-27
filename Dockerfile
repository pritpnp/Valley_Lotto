# Valley Lotto multi-store web app.
# Build:  docker build -t valley-lotto .
# Run:    docker run -p 8000:8000 -e LOTTO_SECRET=... -v $PWD/data:/app/data valley-lotto
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

COPY requirements.txt requirements-app.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-app.txt

COPY src ./src
COPY config.yaml ./config.yaml

# data/ (app.db + the scraper's state.json) is a mounted volume in production.
VOLUME ["/app/data"]
EXPOSE 8000

# LOTTO_SECRET must be supplied at runtime for secure session cookies.
CMD ["uvicorn", "lottery_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
