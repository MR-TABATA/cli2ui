# Security Policy

## Design: local-only

cli2ui is built to run **on your machine or inside your own network** — there is
no SaaS, no authentication layer, no outbound calls, and no AI. Your database
credentials never leave your network. This narrows the threat model
considerably: cli2ui is a trusted local tool sitting next to your database, not a
multi-tenant service exposed to the internet.

**Do not expose cli2ui to an untrusted network.** It has no user accounts and is
not designed to be a public endpoint.

## What's already hardened

Even as a local tool, the destructive surface is taken seriously:

- **Identifier safety** — schema / table / column / index names are bound with
  `psycopg2.sql.Identifier`; raw-SQL spots (e.g. index access method) use a fixed
  allow-list.
- **The SQL runner** runs ad-hoc queries in a separate transaction with
  `SET TRANSACTION READ ONLY` (the *server* refuses writes), a `statement_timeout`,
  and a row cap. Write mode is opt-in and snapshots the database first.
- **What-if features** (scale simulation, index lab) run with `autocommit=False`
  and always `ROLLBACK` — nothing they touch is committed or visible to other
  sessions.
- **CSRF** is enforced (htmx sends the token via the `<body>` `hx-headers`), with
  `CSRF_TRUSTED_ORIGINS` limited to localhost / 127.0.0.1.
- **`DEBUG` is off by default**, so error pages don't leak tracebacks, settings,
  or SQL.

The full threat model and static-analysis results (bandit / pip-audit /
`check --deploy`) are documented in [specs/security-check.md](specs/security-check.md).

## Reporting a vulnerability

Please report security issues **privately**, not via a public issue:

- Use GitHub's **“Report a vulnerability”** button under the repository's
  **Security** tab (private vulnerability reporting), or
- open a regular issue **only** for non-sensitive, already-public concerns.

Please include reproduction steps and the affected version / commit. As a small
project there's no formal SLA, but reports are taken seriously and triaged as
quickly as possible.

## Scope

In scope: anything that lets a **local** webpage or another origin trick cli2ui
into running unintended destructive operations (e.g. drive-by CSRF), identifier
or SQL injection beyond the intentional SQL runner, or leaks of connection
credentials.

Out of scope: the SQL runner executing arbitrary SQL you typed (that's the
feature), and any scenario that assumes cli2ui is deployed as a public,
multi-user service — that's explicitly unsupported.
