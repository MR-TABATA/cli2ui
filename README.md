# cli2ui

**CLI database ops, as buttons.** A web UI over the database CLI commands you
keep half-remembering. No AI, no magic — just `psql`/`mysql` operations turned
into clicks, running fully on your machine.

```
psql -c "SELECT * FROM pg_stat_activity"   →  one "running queries" button
pg_dump -t users mydb                       →  one "back up this table" button
SELECT pg_terminate_backend(pid)            →  one "kill this process" button
```

Built for **app developers and solo developers** — not DBAs. The pitch is
*"you're looking at your tables 3 minutes after deciding to"* — no install
marathon, no connection-wizard maze, no digging through nested trees.

## Quick start

```bash
docker compose up
```

Then open <http://localhost:8000>. The connection form is pre-filled to point at
a bundled sample database — just hit **Connect** and you'll see its tables.

To point at your own PostgreSQL, change the form fields (host, port, db, user,
password) and connect.

## Status

MVP. `docker compose up` → connect → browse your tables in a DB-client layout
(table list in the sidebar, table detail in the main pane).

- ✅ PostgreSQL: connect + list tables (estimated row counts)
- ✅ Table detail: column definitions (`\d table`) + row preview (`SELECT * … LIMIT`)
- ✅ Objects browser: databases (`\l`), schemas (`\dn`), roles (`\du`) — read-only
- ✅ `postgresql.conf` editor: read/edit parameters via `pg_settings` +
  `ALTER SYSTEM SET` + `pg_reload_conf()`, with reload-vs-restart badges
- ⬜ MySQL (the engine layer is ready for it)
- ⬜ One-click ops (backup table, kill query, …) + safety net
- ⬜ Command history

## Stack

| Layer        | Choice                | Why |
| :----------- | :-------------------- | :-- |
| Frontend     | htmx + Alpine.js      | Click → swap in HTML. No SPA build needed. |
| Backend      | Django                | Battle-tested psycopg2; management plumbing for free. |
| Management DB| SQLite                | Saved connections + (later) history. Zero extra infra. |
| Infra        | Docker                | Local-only, self-contained. No DB creds ever leave your network. |

## Not doing (on purpose)

- **No AI.** API key management + sending your schema to a third party is a
  non-starter for the people this is for.
- **No SaaS.** The moment we'd hold your DB connection info, the liability and
  encryption story swamps the project. Local-only keeps it honest.

## Local development (without Docker)

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

You'll need a PostgreSQL to connect to (the `sampledb` service in
`docker-compose.yml` is one option, exposed on `localhost:5433`).
