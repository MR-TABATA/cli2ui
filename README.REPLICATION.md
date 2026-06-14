# Trying replication locally

> 日本語: **[README.REPLICATION.ja.md](README.REPLICATION.ja.md)**

The **Replication** panel in cli2ui reads `pg_stat_replication` /
`pg_replication_slots`, so its **Standbys** table stays empty until a replica is
actually attached. The panel still works against a single database (it shows the
readiness check — `wal_level`, `max_wal_senders` — the current WAL position, and
lets you create / drop slots), but to watch a standby show up you need a real
primary + standby pair.

This file is a self-contained way to stand one up. For how cli2ui reaches a
database in another container in general, see the
[main README](README.md#connecting-to-a-database-in-another-container).

## Spin up a primary + standby

Save this as `docker-compose.replication.yml` and run
`docker compose -f docker-compose.replication.yml up`:

```yaml
# The default image only allows replication from localhost, so this inline
# config appends an hba rule letting the standby container connect. ($$ keeps
# Compose from expanding $PGDATA — it must reach the script literally.)
configs:
  init_repl:
    content: |
      #!/bin/bash
      echo "host replication all all trust" >> "$$PGDATA/pg_hba.conf"

services:
  primary:
    image: postgres:16
    environment:
      POSTGRES_USER: demo
      POSTGRES_PASSWORD: demo
      POSTGRES_DB: shop
      POSTGRES_HOST_AUTH_METHOD: trust
    configs:
      - source: init_repl
        target: /docker-entrypoint-initdb.d/10-repl.sh
        mode: 0755
    command: >
      postgres -c wal_level=replica -c max_wal_senders=10
               -c hot_standby=on -c listen_addresses=*
    ports: ["5433:5432"]   # so cli2ui can reach it via host.docker.internal:5433

  standby:
    image: postgres:16
    user: postgres
    depends_on: [primary]
    # Wait for the primary, base-backup from it, fix the data-dir mode Postgres
    # insists on (0700), then start as a hot standby.
    entrypoint: >
      bash -c '
        until pg_isready -h primary -U demo -d postgres; do sleep 1; done;
        rm -rf /var/lib/postgresql/data/*;
        pg_basebackup -h primary -U demo -D /var/lib/postgresql/data -R -X stream;
        chmod 0700 /var/lib/postgresql/data;
        exec postgres'
```

## Point cli2ui at it

In cli2ui, connect to the **primary**:

| field    | value                  |
| :------- | :--------------------- |
| host     | `host.docker.internal` |
| port     | `5433`                 |
| db       | `shop`                 |
| user     | `demo`                 |
| password | `demo`                 |

Open the **Replication** panel. Within a few seconds the standby appears under
**Standbys** as `walreceiver / streaming`.

## Notes

- This is a **demo, not a production recipe** — single shared password, `trust`
  auth, no TLS. Don't copy the hba rule into anything real.
- The two footguns the recipe works around: the stock image only permits
  replication from localhost (hence the inline hba rule), and `pg_basebackup`
  leaves the data directory at a mode Postgres refuses to start on (hence the
  `chmod 0700`).
- Tear it down with `docker compose -f docker-compose.replication.yml down -v`.
