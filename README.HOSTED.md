# Running cli2ui in hosted mode

> 日本語: **[README.HOSTED.ja.md](README.HOSTED.ja.md)**

cli2ui is built to run on your machine or inside a trusted network. If you must
reach it from another network, set `CLI2UI_HOSTED=1`.

> **What this is — and isn't.** Hosted mode is a guard against *misconfiguration*.
> It is **not** a promise that cli2ui is safe on the public internet: there are
> still no user accounts, and it stays a single-user tool. Put a VPN or an
> IP allowlist in front as well. Unset, hosted mode does nothing and local use
> is unchanged.

## 1. It refuses to start on an unsafe setup

`python manage.py check_hosted` prints every problem without starting the server
(exit code 1 if any). `runserver`, `manage.py check`, and gunicorn/wsgi all
refuse to start on the same errors.

| Problem | Fix |
|---|---|
| `DEBUG` is on | leave `DJANGO_DEBUG` unset |
| `SECRET_KEY` is the built-in default or under 50 chars | `DJANGO_SECRET_KEY=<long random value>` |
| `ALLOWED_HOSTS` is `*` or empty | `CLI2UI_ALLOWED_HOSTS=cli2ui.example.com` (comma-separated) |
| no public `https://` origin in CSRF trusted origins | `CLI2UI_EXTRA_CSRF_ORIGINS=https://cli2ui.example.com` |
| no declared access control | `CLI2UI_HOSTED_AUTH=proxy` or `basic` (below) |

cli2ui has no login of its own, so you must say how access is protected:

- `proxy` — a reverse proxy, VPN or SSO gateway in front authenticates people.
  cli2ui trusts that you did.
- `basic` — built-in HTTP Basic auth: set `CLI2UI_HOSTED_BASIC_USER` and
  `CLI2UI_HOSTED_BASIC_PASSWORD` (12+ characters). Use it only over HTTPS.

Reported as **warnings** (the server still starts): the CSRF cookie is not marked
secure (`CLI2UI_SECURE_COOKIES=1`), and neither SSL redirect nor HSTS is on
(fine if your proxy terminates TLS).

## 2. Dangerous operations are off until you turn them on

In hosted mode these answer `403` and their buttons are removed from the UI.
Turn each back on by name in `CLI2UI_HOSTED_ALLOW` (comma-separated):

| name | enables |
|---|---|
| `write_sql` | SQL runner write mode |
| `ddl` | table / column / index / schema changes |
| `role_admin` | role create / alter / delete |
| `database_admin` | database create / drop / rename / restore, backup restore |
| `server_settings` | server settings (`ALTER SYSTEM`) |
| `session_control` | cancel / kill sessions, replication slots |
| `data_transfer` | import, dump and export of data |
| `connection_admin` | add / delete saved connections |

Reads (overview, health, locks, EXPLAIN, read-only queries) always work. A typo in
`CLI2UI_HOSTED_ALLOW` is a startup error, not a silently ignored value.

## Example

```bash
CLI2UI_HOSTED=1 \
DJANGO_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(50))')" \
CLI2UI_ALLOWED_HOSTS=cli2ui.example.com \
CLI2UI_EXTRA_CSRF_ORIGINS=https://cli2ui.example.com \
CLI2UI_HOSTED_AUTH=proxy \
CLI2UI_HOSTED_ALLOW=write_sql \
python manage.py runserver
```

## Not implemented yet

A target-database / outbound allowlist and rate limiting. Until then, restrict
which databases cli2ui can reach, and how fast, at the network layer.

## Trying it

- `scripts/run_hosted.sh` — start a hosted instance and open the browser
  (`CLI2UI_HOSTED_ALLOW=... scripts/run_hosted.sh` to compare).
- `scripts/verify_hosted.sh` — end-to-end check of the behaviour above.
- Design notes: [specs/hosted-mode.md](specs/hosted-mode.md).
