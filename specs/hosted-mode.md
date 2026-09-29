# hosted-mode 安全ガード — 設計・仕様(ドラフト)

状態: **レビュー待ち(未実装)** / 作成: 2026-09-29

## 1. 目的と前提

cli2ui は「ローカル専用・認証なし」が既定の設計(SECURITY.md)。これを **外向けに公開する運用(hosted)** で使う人が、
うっかり危険な状態で晒さないための安全弁を足す。**ローカル運用の UX は 1 ミリも変えない**(hosted を明示したときだけ効く)。

現状で公開時に問題になる点(コード確認済):

| 現状 | 公開時のリスク |
|---|---|
| `SECRET_KEY` に安全でない既定値 | CSRF/署名の偽造 |
| `ALLOWED_HOSTS = ["*"]` | Host ヘッダ攻撃 |
| `CSRF_TRUSTED_ORIGINS` が localhost 固定(+env追加) | 公開ドメインで POST が全滅 or 設定ミスで緩める |
| **認証なし** | 誰でも接続追加・DROP・任意SQL |
| 接続先 host を利用者が自由入力 | サーバを踏み台にした内部ネットワーク走査(SSRF 類似) |
| 書込SQL / drop / role / ALTER SYSTEM / dump・restore / import が常時有効 | 被害の大きい操作が既定で開いている |
| レート制限なし | 総当たり・重いクエリ連打 |

## 2. 有効化

`CLI2UI_HOSTED=1`(既定 0)。0 のとき本機能は **完全に不活性**(既存挙動と同一。テストで担保)。

## 3. 機能(5 本)と段階

### Phase 1(最初に出す・実機で検証できる)

**A. 起動前チェック(preflight)** — `core/hosted.py`
- `CLI2UI_HOSTED=1` のとき、起動時(`AppConfig.ready` + `manage.py check` 経由)に検査。**違反があれば起動拒否**(`ImproperlyConfigured`、違反を全件まとめて表示)。
- 検査項目(すべて「エラー」):
  1. `DEBUG` が True
  2. `SECRET_KEY` が既定値 / 空 / 長さ < 50 / 既知の placeholder
  3. `ALLOWED_HOSTS` が空 or `*` を含む
  4. `CSRF_TRUSTED_ORIGINS` に `http://localhost*` / `127.0.0.1` のみ(=公開オリジンが 1 つも無い)、または `https://` 以外の公開オリジン
  5. 認証の宣言が無い(→ §5 Q1)
- 検査項目(「警告」= 起動は許可、`--strict` 相当の env で昇格可):
  `SESSION/CSRF_COOKIE_SECURE` 未設定、`SECURE_SSL_REDIRECT`/HSTS 未設定、`CLI2UI_DB_PATH` が既定のまま。
- `python manage.py check_hosted` を追加: 起動せずに検査結果を表で出す(**検証用の入口**。終了コード 0/1)。
  Django の system check framework(`@register(Tags.security)`)にも登録し `manage.py check --deploy` で同じ結果が出る。

**B. 危険機能の既定オフ** — 機能フラグを `core/hosted.py` に集約
- hosted 時、以下は **既定で無効**。個別に `CLI2UI_HOSTED_ALLOW=write_sql,ddl,...` で明示解除:

  | フラグ名 | 対象 |
  |---|---|
  | `write_sql` | SQLランナーの write モード(`query_run` の `write=1`) |
  | `ddl` | table/column/index/schema の rename/drop/truncate/alter |
  | `role_admin` | roles の create/alter/delete |
  | `database_admin` | database create/drop/rename/restore、backup restore |
  | `server_settings` | settings update/reset(`ALTER SYSTEM`) |
  | `session_control` | activity/locks の cancel・kill、slot create/drop |
  | `data_transfer` | table import、dump/export(データ持ち出し) |
  | `connection_admin` | 接続の新規追加・削除・clear(→ C で allowlist 運用に置換) |

- 実装(変更): view へのデコレータではなく、URL 名→機能の対応表 `ROUTE_CAPABILITY` を持つミドルウェア `HostedGuardMiddleware` が全メソッドを一括で止める(付け忘れ防止)。拒否時は 403 + 「hosted モードで無効。`CLI2UI_HOSTED_ALLOW=...` で解除」の説明。
  UI 側は base.html の小さな JS が、無効なルートを指すフォーム/ボタン/リンクと、それを開くドロワーのトリガーを DOM から除去する(見た目だけ。強制はミドルウェア)。**二重化**。
- 読み取り系(overview/health/explain/read-only クエリ)は常に有効。

### Phase 2

**C. target DB allowlist** — `CLI2UI_HOSTED_TARGETS="host:port,host:port,*.internal.example:5432"`
- `connect` 時と、保存済み接続を使う全 view の入口で host:port を照合。不一致は拒否。
- hosted 時は **未設定なら接続追加を拒否**(fail closed)。
- 名前解決後の IP も検査し、loopback / link-local(169.254.169.254 等)/ RFC1918 は allowlist に明記が無い限り拒否(DNS rebinding 対策)。

**D. egress allowlist** — アプリ層のガード
- cli2ui が張る外向き接続は「DB 接続」と「pg_dump/mysqldump の子プロセス」のみ。**全て C の許可済み host に限定**する共通関数 `assert_egress(host, port)` を engine の接続生成点に 1 箇所挟む。
- 注: アプリ層の制限は **ネットワーク層(SG/iptables)の代替ではない**。ドキュメントで「二重に張れ」と明記。

### Phase 3

**E. rate limit** — 依存追加なし(Django キャッシュ `LocMemCache` ベース、単一プロセス前提)
- ミドルウェア。IP 単位(`X-Forwarded-For` は `CLI2UI_HOSTED_TRUSTED_PROXIES` 指定時のみ信用)。
- 既定: 全体 120 req/分、書込系(POST)30 req/分、`query_run`/`explain_run` 20 req/分。超過は 429 + `Retry-After`。
- 複数ワーカー構成では効きが割れる旨を明記(その場合は前段 proxy で制限)。

## 4. 検証計画(Phase 1)

- **自動テスト**(`core/tests.py` に追加): 
  - hosted=0 で既存テストが全て無変更で緑(不活性の証明)
  - 各違反(DEBUG/既定鍵/`*`/localhost のみ CSRF)で preflight が個別に失敗、全部正しければ通過
  - フラグ off の view が 403、`ALLOW` で解除、読み取り系は常に 200
  - 二重化: ボタン非表示 + 直 POST も 403
- **実機**(あなたが検証):
  1. `python manage.py check_hosted`(ローカル既定)→ 「hosted 無効」で 0
  2. `CLI2UI_HOSTED=1 python manage.py check_hosted` → 違反一覧が出て終了コード 1
  3. 正しい env を渡して 0 → `runserver` が起動
  4. 画面: 危険ボタンが消え、curl で直 POST すると 403、`CLI2UI_HOSTED_ALLOW=write_sql` で write が復活

## 5. 決めてほしい点(推奨つき)

- **Q1 認証**: cli2ui に認証は無く、ガードだけでは「誰でも読み取り」は防げない。
  推奨: `CLI2UI_HOSTED_AUTH` を必須にし、値は `proxy`(前段の Basic/OIDC/VPN 等に任せると宣言)か `basic`(env の ID/PW で HTTP Basic を掛ける小さな middleware、Phase 1 に含める)。宣言なしは起動拒否。
  → 私の推奨は **`proxy` 宣言 + `basic` 同梱**。認証機構の作り込み(ユーザー管理等)はスコープ外。
- **Q2 名称**: 環境変数の接頭辞は `CLI2UI_HOSTED_*` でよいか。
- **Q3 警告の扱い**: HSTS/Secure cookie 未設定は「警告」止まりでよいか(推奨: 警告。HTTPS 終端が proxy 側の構成が多いため)。
- **Q4 置き場所**: 全エディション共通のコア(`core/hosted.py`)に置く(決定)。

## 6. スコープ外

ユーザー管理/RBAC、監査ログ、IAM(別機能)、WAF、CSP。

## 7. 変更予定ファイル(Phase 1)

新規: `core/hosted.py`, `core/management/commands/check_hosted.py`, `core/middleware.py`(basic 認証, Q1 次第)
変更: `cli2ui/settings.py`(env 読み取り), `core/apps.py`(check 登録), `core/views/*`(デコレータ付与), `core/context_processors.py`, 該当テンプレート(ボタン非表示), `core/tests.py`, `SECURITY.md`

※ 作業ツリーに既存の未コミット変更(edition 関連: settings.py / features.py / context_processors.py 等)がある。実装はこれと同じファイルに触れるため、
コミット時にどう分けるかは §8 で確認したい。

## 8. コミット運用

ご指示どおり、実機検証はあなた → OK 後にのみコミット。コミット前に git がクリーンであることを確認し、ブランチを切ってそこへ。
既存の未コミット変更(6 ファイル)が残っている場合、「クリーン」条件を満たさないため、先にそちらの扱い(先にコミット/stash/別ブランチ)を指示してください。
