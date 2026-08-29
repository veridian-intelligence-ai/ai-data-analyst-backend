# AI Data Analyst API — single-service image.
#
# python:3.12-slim, no ODBC and no system SQL drivers: the Power BI adapter
# talks plain HTTPS (executeQueries REST endpoint), so the image stays small
# and there is no driver matrix to maintain.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Dependencies first — this layer only rebuilds when the pins change.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY migrations/ ./migrations/

EXPOSE 8000

# Respect the platform's PORT (Railway/Heroku-style), default 8000.
# Proper package layout: `app.main:app` from the backend root — no sys.path
# hacks anywhere in the codebase.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
