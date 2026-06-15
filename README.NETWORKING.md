# Connecting cli2ui to your database — a networking guide

> 日本語: **[README.NETWORKING.ja.md](README.NETWORKING.ja.md)**

> If you've ever typed `localhost` into the connection form and gotten
> "connection refused" even though your database is *right there*, this page is
> for you. It explains **why** that happens and gives you a copy-paste fix for
> every common setup.

## The one rule to remember

cli2ui runs **inside its own Docker container**. To code running inside a
container, `localhost` means *"this container"* — **not your machine**. Your
database almost never lives inside the cli2ui container, so `localhost` points
at the wrong place and the connection fails.

```
   ┌─────────────────────── your machine (the "host") ───────────────────────┐
   │                                                                          │
   │   ┌── cli2ui container ──┐        ┌── postgres container ──┐             │
   │   │                      │        │                        │             │
   │   │  localhost  ─────────┼──┐     │   listening on 5432    │             │
   │   │  = THIS container  ✗ │  │     │                        │             │
   │   └──────────────────────┘  │     └────────────────────────┘             │
   │                             │                                            │
   │                             └─►  points back at cli2ui itself, not the   │
   │                                  database → "connection refused"         │
   └──────────────────────────────────────────────────────────────────────────┘
```

So the whole game is: **what name does cli2ui use to find the database?** The
answer depends on where your database is running. Find your situation below.

---

## Which situation are you in?

| Where is your database running? | Use this **host** value | Jump to |
| --- | --- | --- |
| Installed natively on your Mac/PC (Homebrew, Postgres.app, an installer) | `host.docker.internal` | [Case A](#case-a-database-installed-on-your-machine) |
| In **another** Docker container (a different `docker compose` project) | the database's **container name** | [Case B](#case-b-database-in-another-container) |
| In a container that **publishes a port** to your machine (`-p 5432:5432`) | `host.docker.internal` | [Case C](#case-c-database-publishes-a-port) |

The **port** is always the port the database *itself* listens on (PostgreSQL's
default is `5432`) — see the per-case notes for the one exception.

---

## Case A: database installed on your machine

Your database is a normal program on your Mac/PC, not in Docker.

cli2ui's `docker-compose.yml` already ships the magic that makes this work — an
`extra_hosts` entry that gives the container a name for "the host machine":

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

So in the connection form:

| Field | Value |
| --- | --- |
| host | `host.docker.internal` |
| port | `5432` (whatever your local Postgres uses) |
| db / user / password | your database's |

`host.docker.internal` is a special name Docker resolves to *your machine* from
inside a container. It works the same on macOS, Windows, and Linux here.

---

## Case B: database in another container

This is the most common "real project" case: your app and its database are
already running in their own `docker compose` setup, and you want cli2ui to peek
into that database.

Containers can only call each other by name **if they're on the same network**.
cli2ui starts on its own network (`cli2ui_default`), so first you join cli2ui to
the database's network, then you address the database by its **container name**.

### Step 1 — find the database's network and container name

```bash
docker ps                 # shows running containers + their names
docker network ls         # shows networks
```

Look at the `NETWORKS` column for your database's container. A compose project
named `myapp` typically creates a network called `myapp_default`.

### Step 2 — attach cli2ui to that network

```bash
docker network connect <db-network> cli2ui-app-1
```

(`cli2ui-app-1` is the cli2ui container's name when you start it with
`docker compose up`. Confirm it with `docker ps`.)

### Step 3 — connect by container name

| Field | Value |
| --- | --- |
| host | the database's **container name** (e.g. `myapp-db`) |
| port | `5432` |
| db / user / password | your database's |

That's it — no editing of anyone's compose file, and it survives until you stop
the containers. To undo it later: `docker network disconnect <db-network> cli2ui-app-1`.

> **Prefer a permanent setup?** Instead of `network connect`, put both projects
> on one shared external network:
> ```bash
> docker network create shared-net
> ```
> ```yaml
> # add to BOTH docker-compose.yml files:
> networks:
>   default:
>     name: shared-net
>     external: true
> ```
> Then they can always reach each other by container name, no manual step.

---

## Case C: database publishes a port

If the database's container maps a port to your machine — you'll see something
like `0.0.0.0:5432->5432/tcp` in `docker ps` — then from your machine's point of
view the database *is* reachable on the host. Use the host route, exactly like
Case A:

| Field | Value |
| --- | --- |
| host | `host.docker.internal` |
| port | the **published** (left-hand) port — e.g. `5432`, or `5433` if it maps `5433:5432` |
| db / user / password | your database's |

This avoids touching networks at all. **But** see the macOS caveat in
Troubleshooting — if a *different* database already owns that port on your
machine, Case B is more reliable.

---

## Tidiest of all: run cli2ui inside your project's compose

If the database already lives in a `docker compose` project you control, the
cleanest setup is to **add cli2ui as a service to that same compose** instead of
running it as a separate stack. They share the project's network automatically,
so cli2ui reaches the database by its **container name** with zero network
wiring — and you don't have a second set of containers to start.

Add this to your project's `docker-compose.yml`:

```yaml
services:
  # ... your app, your db (e.g. a service named `db`) ...

  cli2ui:
    image: cli2ui-app            # build once: docker build -t cli2ui-app /path/to/cli2ui
    profiles: ["tools"]          # only starts when you ask for it (see below)
    ports:
      - "8001:8000"              # 8000 is usually taken by your app — pick a free host port
    restart: unless-stopped
```

Then bring it up **on demand**:

```bash
docker compose --profile tools up    # starts your stack + cli2ui
```

Open `http://localhost:8001` and connect with **host = your db's service name**
(e.g. `db`), port `5432`, and your credentials.

Three things to get right:

1. **Keep it dev-only with `profiles`.** cli2ui has no authentication. The
   `profiles: ["tools"]` key means a plain `docker compose up` *won't* start it —
   it only comes up with `--profile tools`. That's your guard against it ever
   riding along into a shared or production stack.
2. **Don't copy cli2ui's bundled `sampledb`.** You want it pointing at *your*
   database (by service name), not the demo one. Just the one `cli2ui` service
   above is enough — referencing the prebuilt `cli2ui-app` image keeps it
   decoupled from cli2ui's own compose file and build context.
3. **Use a free host port** (`8001:8000` here) so it doesn't clash with your app.

> **Saved connections / history** live in `db.sqlite3` *inside* the container, so
> they reset when it's recreated — fine for a dev tool (re-entering a connection
> takes seconds). To persist them, bind-mount the file
> (`./.cli2ui-db.sqlite3:/app/db.sqlite3`, created beforehand).

---

## A complete worked example

Say you have a separate app called **SyncVey** running in Docker, and its
database container is `syncvey-db` on the network `syncvey_default`. Here's the
whole flow (this is Case B):

```bash
# 1. See what's running and where
docker ps
#   NAMES         IMAGE                  NETWORKS
#   syncvey-db    postgres:18-alpine     syncvey_default   ← target

# 2. Start cli2ui (host port 8001 here in case 8000 is busy)
docker run -d --name cli2ui-app-1 -p 8001:8000 cli2ui-app

# 3. Join cli2ui to SyncVey's network
docker network connect syncvey_default cli2ui-app-1

# 4. Open http://localhost:8001 and fill the form:
#      host = syncvey-db      port = 5432
#      db   = <their db>      user/password = <theirs>
```

cli2ui can now resolve `syncvey-db` by name and connect. Done.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `connection refused` with host `localhost` | `localhost` = the cli2ui container, not your DB | Use `host.docker.internal` (Case A/C) or the container name (Case B) |
| `could not translate host name "<name>"` | cli2ui isn't on the same network as that container | `docker network connect <db-network> cli2ui-app-1` (Case B) |
| Host `host.docker.internal` connects but to the **wrong** database | Another DB already owns that port on your machine (common on macOS, where a native Postgres on `5432` shadows a container's published `5432`) | Use Case B (container name on a shared network) — it's unambiguous |
| Can't reach a container by its **IP** (e.g. `172.x.x.x`) from your machine | On Docker Desktop (macOS/Windows) container IPs aren't routable from the host | Don't use the IP — use Case B (container name) or Case C (published port) |
| Works, then breaks after a restart | `docker network connect` is per-container-lifetime | Re-run it after `up`, or use the permanent shared-network setup in Case B |

---

## Mental model recap

- **`localhost` inside a container = that container.** Almost never your DB.
- **`host.docker.internal` = your machine** (for DBs installed natively or
  published on a host port).
- **A container name only works if you're on the same Docker network** — join it
  first with `docker network connect`.
- On **macOS/Windows Docker Desktop**, container **IP addresses** aren't
  reachable from your machine; always go by name or published port.

Still stuck? Open an issue with the output of `docker ps` and `docker network ls`
and how you filled the form.
