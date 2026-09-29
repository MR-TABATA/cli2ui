#!/usr/bin/env bash
# hosted-mode の実機検証を 1 コマンドで行う。 使い方: scripts/verify_hosted.sh
# 一時的な管理DB・ポートで runserver を起動し、curl で確認して後始末する。
# ブラウザで見る項目(詳細画面で drop/rename が消えている)だけは手動。
cd "$(dirname "$0")/.." || exit 1
PY=${PYTHON:-.venv/bin/python}
PORT=${PORT:-18766}   # run_hosted.sh(8766)とは別。 起動中のサーバと取り違えないため
TMP=$(mktemp -d)
# 使用中のポートに繋ぐと、古いサーバ(設定が違う)を検証してしまい大量に NG になる。 先に止める。
if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then
  echo "port $PORT is already in use — stop that server or run: PORT=<free port> $0"; exit 2
fi
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
  # TCP probe, not HTTP: an HTTP probe would be counted by the rate limiter.
  for _ in $(seq 1 40); do (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null && return 0; sleep 0.25; done
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

echo "== 7. 接続先 allowlist (connect を許可して検証)"
post_connect() {  # post_connect <host> -> response body
  local jar="$TMP/jar"; rm -f "$jar"
  curl -s -c "$jar" -o /dev/null $URL/
  local tok; tok=$(awk '$6=="csrftoken"{print $7}' "$jar")
  curl -s -b "$jar" -H "X-CSRFToken: $tok" -X POST \
    -d "name=t&kind=postgres&host=$1&port=5432&dbname=d&user=u&password=p" $URL/connect
}
start CLI2UI_HOSTED_ALLOW=connection_admin || exit 1
b=$(post_connect db.example.com)
echo "$b" | grep -q "No target databases are allowed" && ok "empty allowlist refuses every connection" || ng "empty allowlist" "$b"
start CLI2UI_HOSTED_ALLOW=connection_admin CLI2UI_HOSTED_TARGETS=db.example.com:5432 || exit 1
b=$(post_connect other.example.org)
echo "$b" | grep -q "not in CLI2UI_HOSTED_TARGETS" && ok "host outside allowlist refused" || ng "host outside allowlist" "$b"
start CLI2UI_HOSTED_ALLOW=connection_admin CLI2UI_HOSTED_TARGETS=127.0.0.1:5432 || exit 1
b=$(post_connect 127.0.0.1)
echo "$b" | grep -q "not a public address" && ok "allowlisted loopback still refused (not a public address)" || ng "loopback refused" "$b"
start CLI2UI_HOSTED_ALLOW=connection_admin CLI2UI_HOSTED_TARGETS=127.0.0.1:5432 CLI2UI_HOSTED_PRIVATE_NETS=127.0.0.0/8 || exit 1
b=$(post_connect 127.0.0.1)
echo "$b" | grep -q "not a public address\|not in CLI2UI_HOSTED_TARGETS\|No target databases" && ng "range listed" "$b" || ok "with PRIVATE_NETS it passes the guard (fails later only if no DB there)"

echo "== 8. レート制限"
# 制限は 1 分の固定窓。窓の切り替わりをまたぐと数え直しで誤判定するので、末尾なら次の窓まで待つ。
[ "$(date +%S | sed 's/^0//')" -ge 50 ] && sleep $((61 - $(date +%S | sed 's/^0//')))
start CLI2UI_HOSTED_RATE_ALL=5 || exit 1
codes=$(for _ in 1 2 3 4 5 6; do code $URL/; echo -n " "; done)
[ "$codes" = "200 200 200 200 200 429 " ] && ok "6th request in a minute -> 429" || ng "overall limit" "$codes"
hdr=$(curl -s -D - -o /dev/null $URL/ | tr -d '\r' | grep -i "^retry-after:")
[ -n "$hdr" ] && ok "429 carries Retry-After ($hdr)" || ng "Retry-After header" "missing"
start CLI2UI_HOSTED_RATE_ALL=100 CLI2UI_HOSTED_RATE_WRITE=3 || exit 1
codes=$(for _ in 1 2 3 4; do code -X POST $URL/c/1/table/drop; echo -n " "; done)
[ "$codes" = "403 403 403 429 " ] && ok "write bucket: 4th POST -> 429" || ng "write limit" "$codes"
expect "reads unaffected by write bucket" "$(code $URL/)" 200
start CLI2UI_HOSTED_RATE_ALL=2 CLI2UI_HOSTED_TRUSTED_PROXIES=127.0.0.1/32 || exit 1
codes=$(for i in 1 2 3; do code -H "X-Forwarded-For: 9.9.9.$i, 198.51.100.7" $URL/; echo -n " "; done)
[ "$codes" = "200 200 429 " ] && ok "spoofed left X-Forwarded-For entries do not evade the limit" || ng "XFF spoof" "$codes"
codes=$(for i in 1 2 3; do code -H "X-Forwarded-For: 198.51.100.$i" $URL/; echo -n " "; done)
[ "$codes" = "200 200 200 " ] && ok "distinct real clients (via trusted proxy) are counted separately" || ng "per-client" "$codes"
start CLI2UI_HOSTED_RATE_ALL=2 || exit 1
codes=$(for i in 1 2 3; do code -H "X-Forwarded-For: 198.51.100.$i" $URL/; echo -n " "; done)
[ "$codes" = "200 200 429 " ] && ok "without TRUSTED_PROXIES the header is ignored" || ng "untrusted XFF" "$codes"

echo "== 9. 追加アプリ(CLI2UI_EXTRA_APPS)と hosted"
mkdir -p "$TMP/dx/dummyext"
: > "$TMP/dx/dummyext/__init__.py"
cat > "$TMP/dx/dummyext/apps.py" <<'PYEOF'
from django.apps import AppConfig


class DummyConfig(AppConfig):
    name = "dummyext"

    def ready(self):
        from core import hosted
        hosted.declare_capability("dummy_declared", "dummy_write", "changes dummy things")
PYEOF
cat > "$TMP/dx/dummyext/urls.py" <<'PYEOF'
from django.http import HttpResponse
from django.urls import path
from django.views.decorators.csrf import csrf_exempt


def read(request):
    return HttpResponse("read ok")


@csrf_exempt
def undeclared(request):
    return HttpResponse("undeclared ran")


@csrf_exempt
def declared(request):
    return HttpResponse("declared ran")


urlpatterns = [
    path("dummy/read", read, name="dummy_read"),
    path("dummy/undeclared", undeclared, name="dummy_undeclared"),
    path("dummy/declared", declared, name="dummy_declared"),
]
PYEOF
export PYTHONPATH="$TMP/dx"
start CLI2UI_EXTRA_APPS=dummyext || exit 1
b=$(body $URL/dummy/read); [ "$b" = "read ok" ] && ok "extra app: GET passes" || ng "extra app GET" "$b"
b=$(body -X POST $URL/dummy/undeclared)
echo "$b" | grep -q "does not declare" && ok "extra app: undeclared write is shut by default" || ng "undeclared write" "$b"
b=$(body -X POST $URL/dummy/declared)
echo "$b" | grep -q "changes dummy things" && echo "$b" | grep -q "CLI2UI_HOSTED_ALLOW=dummy_write" \
  && ok "extra app: declared capability is off until named" || ng "declared capability" "$b"
start CLI2UI_EXTRA_APPS=dummyext CLI2UI_HOSTED_ALLOW=dummy_write || exit 1
b=$(body -X POST $URL/dummy/declared); [ "$b" = "declared ran" ] && ok "extra app: named in ALLOW -> runs" || ng "declared allowed" "$b"
b=$(body -X POST $URL/dummy/undeclared)
echo "$b" | grep -q "does not declare" && ok "extra app: ALLOW does not open undeclared routes" || ng "undeclared stays shut" "$b"
env "${GOOD[@]}" CLI2UI_EXTRA_APPS=dummyext CLI2UI_HOSTED_ALLOW=typo_write $PY manage.py check_hosted >/dev/null 2>&1
expect "unknown name in ALLOW still fails preflight" "$?" 1
start || exit 1
expect "without CLI2UI_EXTRA_APPS the extra routes do not exist" "$(code $URL/dummy/read)" 404
unset PYTHONPATH

echo
echo "結果: $pass OK / $fail NG"
echo "手動: ブラウザで実接続のテーブル詳細を開き、drop / rename / add column 等が見えないこと"
[ "$fail" = 0 ]
