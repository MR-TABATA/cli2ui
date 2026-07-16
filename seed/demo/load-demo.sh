#!/bin/bash
# Runs inside the postgres image's /docker-entrypoint-initdb.d/ on first boot.
#
# Why a .sh wrapper instead of mounting demo.sql directly: the official
# entrypoint feeds .sql init files to psql with ON_ERROR_STOP=1, and the dump
# opens with a bare `DROP DATABASE demo;` / ownership statements written for
# the dataset's original cluster — any of those aborts the whole load. So we
# run psql ourselves without ON_ERROR_STOP, then strictly verify the tables
# actually arrived (an error-tolerant load is only acceptable if checked).
set -u

DUMP=/seed/demo.sql
if [ ! -f "$DUMP" ]; then
    echo "----------------------------------------------------------------" >&2
    echo "demo dump not found at $DUMP." >&2
    echo "Run seed/demo/fetch.sh on the host first, then recreate this" >&2
    echo "container: docker compose --profile demo up -d --force-recreate airlines" >&2
    echo "----------------------------------------------------------------" >&2
    exit 1
fi

echo "Loading demo dump (takes a few minutes on first boot) ..."
psql --username "$POSTGRES_USER" --dbname postgres --quiet -f "$DUMP"

# Verify the load really succeeded before the volume is marked initialised.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname demo \
    -c "SELECT count(*) AS flights FROM bookings.flights;"
echo "Demo database loaded."
