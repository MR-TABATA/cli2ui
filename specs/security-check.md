# cli2ui — セキュリティ静的チェック 仕様・結果

実施日: 2026-06-11（初回） / **2026-06-13 再実行**（i18n・公開準備の差分込み。`views.py` 分割後 = `core/views/` パッケージ、`set_language`/`LocaleMiddleware` 追加後）/ **2026-06-18 再実行**（MySQL エンジン Phase 1 の差分込み。`core/engines/mysql.py` 追加後）
対象: `core` / `cli2ui`（Django バックエンド。テスト・マイグレーション除く）
結果: **要対応の指摘はすべて是正済み**（bandit Low/Medium/High = 0 / pip-audit runtime = 0 / `check --deploy` は HTTPS 環境専用の警告 3 件のみ＝ローカル HTTP では受容）。2026-06-13 再実行でも同判定（新規 Low 1 件 = best-effort ログの try/except/pass を理由付き `# nosec B110` で抑制）。2026-06-18 再実行でも同判定（MySQL エンジンの識別子 SQL 4 件 = 誤検出を理由付き `# nosec B608` で抑制）。

> 目的: 著者（実装者）の自己レビューは盲点が相関するため、まず**独立・決定論的なツール**で客観シグナルを取る。本書はその実施仕様・結果・是正の証跡。

---

## 脅威モデル（cli2ui 固有）

cli2ui は **ローカル専用**の PostgreSQL 運用コンソール。SaaS・認証・外部送信・Webhook・AI は無い。SyncVey と脅威面が根本的に違うので、チェックもそれに合わせる。

| 主な攻撃面 | 対策（設計） |
|---|---|
| **SQL インジェクション（識別子）** — schema/table/column/index 名は識別子でバインド不可 | すべて `psycopg2.sql.Identifier` で安全クォート。index の access method など raw SQL に差す箇所は固定 allow-list（`INDEX_METHODS`）で enum 照合。列名は実列リストにホワイトリスト照合 |
| **任意 SQL の実行（仕様上の機能）** | SQL ランナー / EXPLAIN は別トランザクションで `SET TRANSACTION READ ONLY` ＝ **DB 側が書込を拒否**（正規表現スキャンに頼らない）＋ `statement_timeout` ＋ 行キャップ |
| **what-if 系（scale sim / index lab）が catalog を触る** | `autocommit=False` ＋ **必ず ROLLBACK**。仮の catalog 編集・仮 index・ANALYZE の副作用は一切 commit されず他セッション不可視（MVCC） |
| **ドライブバイ CSRF**（ローカルでも他サイトから localhost に破壊操作 POST を撃たれる） | CSRF 有効（htmx が `<body>` の hx-headers で X-CSRFToken 送出）。`CSRF_TRUSTED_ORIGINS` を localhost/127.0.0.1 に限定 |
| **クリックジャッキング**（隠し iframe で drop ボタンを踏ませる） | `XFrameOptionsMiddleware`（X-Frame-Options: DENY） |

機能面の安全性検証は `core/tests.py` を参照（read-only が書込拒否 / simulate_scale が痕跡ゼロ / preview_index が痕跡ゼロ 等を統合テストで実証、計 60 件）。

---

## 使用ツール

| ツール | 目的 | 実行コマンド |
|---|---|---|
| `manage.py check --deploy` | 本番設定の問題検出（DEBUG/SECRET_KEY/SSL/Cookie/clickjacking 等） | `python manage.py check --deploy` |
| bandit | Python 静的セキュリティ解析（テスト/マイグレーション除外） | `bandit -r core cli2ui -x '*/tests.py,*/migrations/*'` |
| pip-audit | 依存ライブラリの既知 CVE（runtime 依存） | `pip-audit -r requirements.txt --desc` |

※ bandit / pip-audit は dev 依存。未導入時は `pip install bandit pip-audit` で一時導入可。
※ DAST（OWASP ZAP 等）は未実施。ローカル専用・認証なしで攻撃面が小さく、優先度は低い（将来の任意項目）。

---

## 指摘と是正

| 重大度 | 指摘 | 箇所 | 是正 | コミット |
|---|---|---|---|---|
| 🟡 Medium | **依存の既知 CVE** — Django 5.1.4 に多数（5.1.x 系で修正済み）。SQL インジェクション系は ORM の `annotate/alias/filter` に細工 dict を `**kwargs` で渡す経路で、cli2ui は該当 API に外部入力を渡さず実影響は低いが、版を上げるのが正解 | `requirements.txt` | **Django 5.1.4 → 5.1.15**（5.1 系最新）。pip-audit runtime = 0 件に | (本コミット) |
| 🟡 低 | **クリックジャッキング** — `XFrameOptionsMiddleware` 不在で X-Frame-Options 無し。破壊ボタン（drop schema/role/index）を隠し iframe で踏ませる余地 | `cli2ui/settings.py` | `XFrameOptionsMiddleware` 追加（X-Frame-Options: DENY）。`check --deploy` W002 解消 | (本コミット) |
| ⚪ 誤検出 | **B608 SQL injection**（f-string SQL）×2 | `core/views.py`（`query_sql` / lab の starter SQL） | **誤検出**。これらは SQL エディタに**プレフィル表示する文字列**で、サーバ側では実行されない。実行経路（`run_query`）は read-only 強制。理由付き `# nosec B608` で抑制 | (本コミット) |
| ⚪ 誤検出 | **B105 hardcoded password** `'demo'` | `core/views.py` `SAMPLE_INITIAL` | **誤検出**。同梱デモ DB 用フォーム初期値で秘密ではない。`# nosec B105` で抑制 | (本コミット) |
| ⚪ ビルド時 | **pip 自体の CVE**（wheel/zip 展開のパストラバーサル等） | Docker イメージの pip | runtime 依存ではなくインストーラの問題。`Dockerfile` でビルド時 `pip install --upgrade pip`。tar の CVE-2025-8869 は base が Python 3.13（PEP 706 実装）で既に緩和 | (本コミット) |
| ⚪ 誤検出 | **B110 try_except_pass**（Low）— best-effort のコマンド履歴ログ。書込失敗を握り潰す | `core/views/runner.py` `_log_command` | **意図的**。履歴記録は付随機能で、失敗してもユーザーのクエリ結果を壊してはならない（docstring に明記）。理由付き `# nosec B110` で抑制 | 2026-06-13 |
| ⚪ 誤検出 | **B110 try_except_pass**（Low）— 自動バックアップの保持上限プルーニングの握り潰し | `core/views/_shared.py` `_auto_backup`（`_prune_old_backups` 呼び出し） | **意図的**。スナップショットは既に保存済みで、後段の掃除（古い世代削除）が失敗してもユーザーの破壊的操作をブロックしてはならない。理由付き `# nosec B110` で抑制 | 2026-06-15 |

| ⚪ 誤検出 | **B608 SQL injection**（Medium/Confidence:Low）×4 — 識別子を差した f-string SQL（preview / filter / CSV import の INSERT / table stream） | `core/engines/mysql.py:174,240,297,324` | **誤検出**。schema/table/column は `_ident()`（MySQL 規則の backtick クォート＝埋め込み backtick を二重化）で安全クォートし、値はすべて `%s` バインド。PG エンジン側で受容済みの B608 と同一クラス。フィルタ演算子は `FILTER_OPS` allow-list、index method は `INDEX_METHODS` allow-list、列名は実テーブルにホワイトリスト照合。理由付き `# nosec B608` で抑制 | 2026-06-18 |

> **2026-06-18 再実行メモ**: MySQL エンジン Phase 1（`core/engines/mysql.py`）を追加。新たな攻撃面は PG と同型で、識別子は `_ident()` で安全クォート・値は `%s` バインド・raw 補間（index method 等）は allow-list 照合のため、injection 面は PG と同等に閉じている。bandit 新規 Medium 4 件（B608）はすべて識別子 f-string の誤検出で、理由付き `# nosec B608` を 4 箇所に付与し bandit Low/Medium/High = 0 を維持。pip-audit runtime = 0、`check --deploy` は HTTPS 専用警告 3 件のみで変化なし。なお `list_blocking` 等が「未対応」と「空」を同じ `[]` で返す**偽陰性**（ロック見落とし）は SAST の対象外の機能安全課題で、MySQL Phase 2 で別途対応する。

> **2026-06-15 再実行メモ**: バックアップ保持上限（合計サイズ）追加分は攻撃面を増やさない — 削除対象は接続自身の過去スナップショットのみ、判定は `byte_size` のみ参照（ユーザー入力・SQL を一切扱わない）。新規 Low 1 件（上記 B110）を理由付き抑制し bandit Low/Medium/High = 0 を維持。

> **2026-06-13 再実行メモ**: i18n 追加分の新コード（`LocaleMiddleware`、`set_language` ルート）は新たな攻撃面を増やさない — `set_language` は Django 組み込みビューで CSRF 保護下、言語は cookie 保存のみ（ユーザー SQL を一切扱わない）。`views.py` 分割で B608/B105 の `# nosec` は `core/views/runner.py`・`core/views/connection.py` へ移動済み（バンディットの「nosec encountered but no failed test」警告は行ズレによる無害な情報）。

---

## 受容したリスク（判断して残す）

- **`check --deploy` 残り 3 件（W004 HSTS / W008 SSL_REDIRECT / W016 CSRF_COOKIE_SECURE）** — いずれも **HTTPS 前提**。cli2ui はローカル HTTP 運用が既定なので N/A。TLS（リバースプロキシ）越しに公開する場合のみ env で有効化する。
- **`DEBUG` 既定 ON / `SECRET_KEY` の安全でない既定 / `ALLOWED_HOSTS=["*"]`** — **ローカルファースト設計の意図的な既定**。`DJANGO_DEBUG=0` / `DJANGO_SECRET_KEY=…` の env 上書きを用意済み。ネットワークに晒す場合は両方を設定すること（README の運用注記）。SyncVey のような「既定鍵 + DEBUG=False で起動拒否」までは、ローカル UX を損ねるため採用しない。
- **CSP 未設定 / Tailwind Play CDN** — ローカル専用・第三者コンテンツを描画しない前提で現状未対応。公開を本格化する場合に検討。

---

## 再現手順

```bash
# dev 依存を一時導入
pip install -q bandit pip-audit

# ① 本番設定チェック（DEBUG=False 想定の値で）
DJANGO_DEBUG=0 DJANGO_SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(50))") \
  python manage.py check --deploy

# ② 静的セキュリティ解析（テスト/マイグレーション除外。本番コードは 0 件であること）
bandit -r core cli2ui -x '*/tests.py,*/migrations/*'

# ③ 依存の CVE（runtime 依存のみ）
pip-audit -r requirements.txt --desc
```

判定基準: **bandit Low/Medium/High = 0**、**pip-audit runtime = 0**、`check --deploy` は HTTPS 環境専用の警告のみであること。
