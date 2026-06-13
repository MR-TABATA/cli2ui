# Contributing to cli2ui

Thanks for your interest! cli2ui is a deliberately small, **local-only**
PostgreSQL ops console. Contributions that keep it focused — depth over breadth,
no SaaS, no AI — are very welcome.

## Run it

```bash
docker compose up        # app on http://localhost:8000 + a bundled sample DB
```

Or without Docker:

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

You'll need a PostgreSQL to point at — the `sampledb` service in
`docker-compose.yml` (exposed on `localhost:5433`) is one option.

`DEBUG` is off by default; set `DJANGO_DEBUG=1` for Django's rich error pages
while developing.

## Tests

```bash
python manage.py test
```

There's also a Playwright smoke test (`LiveServerTestCase`) that drives a real
browser; it **skips itself** unless a sample DB is reachable, so the suite runs
fine without it. To include it:

```bash
pip install -r requirements-dev.txt
playwright install chromium
```

Please run `python manage.py check` and the test suite before opening a PR.

## How the code is laid out

Each panel is self-contained and follows the same pattern:

> **one engine method + one view + one template + one nav button**

- `core/engines/postgres.py` — the SQL/`pg_dump` logic behind a panel.
- `core/views/` — thin views (split by area: `tables`, `ops`, `objects`,
  `runner`, `snapshots`, `connection`). They render `templates/partials/*.html`
  back into `#detail` via htmx.
- `templates/partials/` — one HTML partial per panel.
- `cli2ui/urls.py` — wire the new view in.

The frontend is **htmx + Alpine.js** (CDN, no build step): a click does an
`hx-get`/`hx-post` that swaps in server-rendered HTML.

## UI conventions

New panels must follow [STYLE.md](STYLE.md): colour = intent (emerald = primary,
red = destructive, amber = warning, sky = link, zinc = structure), the shared
`.btn` / `.field` classes, and `rounded-xl` cards / `rounded-lg` controls.

## Internationalization

The UI is English + Japanese. Wrap user-visible text with `{% trans %}` /
`{% blocktrans %}` in templates and `gettext` in Python — **not** SQL, `pg_*`
identifiers, or code samples. After adding strings:

```bash
django-admin makemessages -l ja
# fill the new msgstr entries in locale/ja/LC_MESSAGES/django.po
django-admin compilemessages -l ja
```

Commit both `django.po` and `django.mo`. See the i18n section of
[STYLE.md](STYLE.md) for the full rules.

## Git

- Branch off `main` with a `feature/` or `fix/` prefix.
- Keep PRs focused; describe the change and how you tested it.
- Don't commit `db.sqlite3` (it's gitignored).
