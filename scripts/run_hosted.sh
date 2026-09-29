#!/usr/bin/env bash
# hosted モードで手元起動し、ブラウザを開く(目視確認用)。 Ctrl+C で停止。
#   scripts/run_hosted.sh
#   CLI2UI_HOSTED_ALLOW=write_sql,ddl scripts/run_hosted.sh   # 機能を戻して比較
#   PORT=9000 scripts/run_hosted.sh
# 普段の db.sqlite3(保存済み接続)を使う。 接続の追加/削除は hosted では無効。
cd "$(dirname "$0")/.." || exit 1
PY=${PYTHON:-.venv/bin/python}
PORT=${PORT:-8766}
URL=http://127.0.0.1:$PORT/

export CLI2UI_HOSTED=1
export DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY:-$($PY -c "import secrets;print(secrets.token_urlsafe(50))")}
export CLI2UI_ALLOWED_HOSTS=${CLI2UI_ALLOWED_HOSTS:-127.0.0.1,localhost}
export CLI2UI_EXTRA_CSRF_ORIGINS=${CLI2UI_EXTRA_CSRF_ORIGINS:-https://cli2ui.example.com}
export CLI2UI_HOSTED_AUTH=${CLI2UI_HOSTED_AUTH:-proxy}

echo "hosted mode · ALLOW=${CLI2UI_HOSTED_ALLOW:-(none)} · $URL"
$PY manage.py check_hosted || exit 1

(
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null "$URL"; then
      if command -v open >/dev/null; then open "$URL"
      elif command -v xdg-open >/dev/null; then xdg-open "$URL"
      else echo "open $URL in your browser"; fi
      exit 0
    fi
    sleep 0.25
  done
) &

exec $PY manage.py runserver "$PORT" --noreload
