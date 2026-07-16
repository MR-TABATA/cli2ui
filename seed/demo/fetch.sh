#!/bin/sh
# Fetch the Postgres Pro "Airlines" demo database dump and place it where
# docker-compose expects it (seed/demo/demo.sql). The dump is ~60MB zipped,
# so it is downloaded on demand instead of being committed to the repo.
#
# Usage: seed/demo/fetch.sh [small|medium|big]   (default: medium)
#
# Dataset home: https://postgrespro.com/education/demodb
set -eu

SIZE="${1:-medium}"
case "$SIZE" in
  small|medium|big) ;;
  *) echo "usage: $0 [small|medium|big]" >&2; exit 1 ;;
esac

DIR="$(cd "$(dirname "$0")" && pwd)"
URL="https://edu.postgrespro.com/demo-$SIZE-en.zip"
ZIP="$DIR/demo-$SIZE-en.zip"
TMP="$DIR/.extract"

echo "Downloading $URL ..."
curl -fL -o "$ZIP" "$URL"

echo "Extracting ..."
rm -rf "$TMP"
mkdir "$TMP"
unzip -oq "$ZIP" -d "$TMP"

# The zip contains a dated filename (e.g. demo-medium-en-20170815.sql);
# normalise it to demo.sql, which is the name mounted into the container.
SQL="$(find "$TMP" -name '*.sql' | head -n 1)"
if [ -z "$SQL" ]; then
  echo "error: no .sql file found inside $ZIP" >&2
  exit 1
fi
mv "$SQL" "$DIR/demo.sql"
rm -rf "$TMP" "$ZIP"

echo "Wrote $DIR/demo.sql ($(du -h "$DIR/demo.sql" | cut -f1))"
echo "Next: docker compose --profile demo up -d airlines"
