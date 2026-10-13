# cli2ui

**Your database CLI commands, as buttons.** A local-only web UI for PostgreSQL and MySQL — run the operations you'd
otherwise half-remember in `psql` / `mysql`, and see what they would do before you press the button. No AI, no SaaS:
everything runs on your own machine.

## Start (Docker only — no clone)

```bash
docker run --rm -p 8000:8000 jiniie/cli2ui
```

Then open <http://localhost:8000/> and enter your database's host, port, database, user and password in the
connection form. To reach a database on your own machine from inside the container, use the host
`host.docker.internal` (on Linux, add `--add-host=host.docker.internal:host-gateway` to the `docker run`).

Your connections are kept in a SQLite file inside the container; add `-v cli2ui-data:/data` and
`-e CLI2UI_DB_PATH=/data/db.sqlite3` to keep them between runs.

## Or try it with a bundled sample database

```bash
git clone https://github.com/MR-TABATA/cli2ui
cd cli2ui
docker compose up
```

The compose file starts cli2ui together with a sample PostgreSQL database; the connection form is pre-filled, so
you just hit **Connect**.

## Tags

- `latest` — the newest release
- `X.Y.Z` — a specific release (pin it with `CLI2UI_IMAGE=jiniie/cli2ui:X.Y.Z`)

Platforms: `linux/amd64`, `linux/arm64`.

## Links

- Source and docs: <https://github.com/MR-TABATA/cli2ui>
- Product page: <https://mrktools.app/en/cli2ui/>
- License: MIT

---

### 日本語

cli2ui は、psql / mysql の操作をボタンにする、ローカル完結の Web UI です（PostgreSQL・MySQL）。AI も SaaS も使わず、すべて自分のマシンの上で動きます。

**Docker だけで起動できます**（上の `docker run`）。サンプルの DB 付きで試すなら、clone して `docker compose up` です。
詳細は [GitHub の README](https://github.com/MR-TABATA/cli2ui) と [紹介ページ](https://mrktools.app/ja/cli2ui/) にあります。
