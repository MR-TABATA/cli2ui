FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# postgresql-client provides pg_dump for the backup feature. A dump fails if the
# client is older than the server it dumps; the slim base (Debian trixie) ships a
# recent enough client for the bundled PG. Pull the PGDG repo if you target newer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Upgrade pip first: the base image's pip has known archive-extraction CVEs
# (path traversal in wheel/zip handling) that only matter at install time.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate && python manage.py runserver 0.0.0.0:8000"]
