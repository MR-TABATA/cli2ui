#!/usr/bin/env bash
# hosted-mode の実機検証を 1 コマンドで行う。 使い方: scripts/verify_hosted.sh
# 一時的な管理DB・ポートで runserver を起動し、curl で確認して後始末する。
# ブラウザで見る項目(詳細画面で drop/rename が消えている)だけは手動。
cd "$(dirname "$0")/.." || exit 1
PY=${PYTHON:-.venv/bin/python}
PORT=${PORT:-8766}
TMP=$(mktemp -d)
PID=""
pass=0; fail=0
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null && wait "$PID" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT

ok()  { echo "  OK   $1"; pass=$((pass+1)); }
ng()  { echo "  NG   $1  (got: $2)"; fail=$((fail+1)); }
expect() { [ "$2" = "$3" ] && ok "$1" || ng "$1" "$2, want $3"; }
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }
body() { curl -s "$@"; }

KEY=$($PY -c "import secrets;print(secrets.token_urlsafe(50))")
export CLI2UI_DB_PATH="$TMP/m.sqlite3" DJANGO_SECRET_KEY="$KEY" DJANGO_DEBUG=0
GOOD=(CLI2UI_HOSTED=1 CLI2UI_ALLOWED_HOSTS=localhost CLI2UI_EXTRA_CSRF_ORIGINS=https://cli2ui.example.com CLI2UI_HOSTED_AUTH=proxy)
URL=http://localhost:$PORT

start() {  # start <extra env...>
  [ -n "$PID" ] && kill "$PID" 2>/dev/null && wait "$PID" 2>/dev/null
  env "${GOOD[@]}" "$@" $PY manage.py runserver "$PORT" --noreload >"$TMP/log" 2>&1 &
  PID=$!
  for _ in $(seq 1 40); do [ "$(code $URL/)" != 000 ] && return 0; sleep 0.25; done
  echo "server did not start:"; tail -5 "$TMP/log"; return 1
}

echo "== 1. ローカル既定 (hosted OFF)"
out=$(env -u CLI2UI_HOSTED $PY manage.py check_hosted 2>&1); rc=$?
expect "check_hosted exit 0" "$rc" 0
echo "$out" | grep -q "OFF" && ok "reports hosted OFF" || ng "reports hosted OFF" "$out"
$PY manage.py migrate -v0 >/dev/null 2>&1

echo "== 2. 危険な設定は拒否"
out=$(env -u DJANGO_SECRET_KEY CLI2UI_HOSTED=1 $PY manage.py check_hosted 2>&1); rc=$?
expect "check_hosted exit 1" "$rc" 1
for id in ALLOWED_HOSTS AUTH CSRF_TRUSTED_ORIGINS SECRET_KEY; do
  echo "$out" | grep -q "cli2ui_hosted.$id\|\[$id\]" && ok "flags $id" || ng "flags $id" "missing"
done
env -u DJANGO_SECRET_KEY CLI2UI_HOSTED=1 $PY manage.py runserver "$PORT" --noreload >"$TMP/bad.log" 2>&1
grep -q "cli2ui_hosted" "$TMP/bad.log" && ok "runserver refuses to start" || ng "runserver refuses to start" "$(tail -2 "$TMP/bad.log")"

echo "== 3. 正しい設定で起動"
env "${GOOD[@]}" $PY manage.py check_hosted >/dev/null 2>&1; expect "check_hosted exit 0" "$?" 0
start || exit 1
expect "GET / (read is open)" "$(code $URL/)" 200

echo "== 4. 危険な操作は 403"
for p in /c/1/table/drop /c/1/columns/add /c/1/roles/delete /c/1/activity/kill /c/1/settings/update /c/1/table/import /connect; do
  expect "POST $p" "$(code -X POST $URL$p)" 403
done
expect "GET  /c/1/table/dump" "$(code "$URL/c/1/table/dump?schema=a&table=b")" 403
b=$(body -X POST -d "write=1&sql=select 1" $URL/c/1/query/run)
echo "$b" | grep -q "Disabled in hosted mode" && ok "write query -> Disabled in hosted mode" || ng "write query" "$b"

echo "== 5. ALLOW で復活 (write_sql のみ)"
start CLI2UI_HOSTED_ALLOW=write_sql || exit 1
b=$(body -X POST -d "write=1&sql=select 1" $URL/c/1/query/run)
echo "$b" | grep -q "Disabled in hosted mode" && ng "write_sql lifted" "still gated" || ok "write_sql lifted (gate passes)"
expect "table/drop still 403" "$(code -X POST $URL/c/1/table/drop)" 403

echo "== 6. Basic 認証"
start CLI2UI_HOSTED_AUTH=basic CLI2UI_HOSTED_BASIC_USER=admin CLI2UI_HOSTED_BASIC_PASSWORD='long-enough-pw!' || exit 1
expect "no credentials -> 401" "$(code $URL/)" 401
expect "wrong password -> 401" "$(code -u admin:wrong $URL/)" 401
expect "correct -> 200" "$(code -u 'admin:long-enough-pw!' $URL/)" 200
env "${GOOD[@]}" CLI2UI_HOSTED_AUTH=basic CLI2UI_HOSTED_BASIC_USER=admin CLI2UI_HOSTED_BASIC_PASSWORD=short $PY manage.py check_hosted >/dev/null 2>&1
expect "short password rejected by preflight" "$?" 1

echo
echo "結果: $pass OK / $fail NG"
echo "手動: ブラウザで実接続のテーブル詳細を開き、drop / rename / add column 等が見えないこと"
[ "$fail" = 0 ]
