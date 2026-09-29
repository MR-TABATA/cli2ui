# cli2ui を hosted モードで動かす

> English: **[README.HOSTED.md](README.HOSTED.md)**

cli2ui は「自分のマシン、または信頼できるネットワークの中」で動かす前提の道具です。
どうしても別のネットワークから届く形にするなら、`CLI2UI_HOSTED=1` を付けます。

> **これは何で、何でないか。** hosted モードは*設定ミスを防ぐ*ための安全弁です。
> cli2ui をインターネットに出して安全にする保証では**ありません**。ユーザーアカウントは
> 依然としてなく、1 人用の道具のままです。VPN や IP 制限も前段に置いてください。
> 設定しなければ何もせず、ローカルの使い方は今までどおりです。

## 1. 危うい設定なら、起動を拒否する

`python manage.py check_hosted` は、サーバを起動せずに問題を全部表示します(1 つでも
あれば終了コード 1)。`runserver`、`manage.py check`、gunicorn/wsgi も同じエラーで
起動を拒否します。

| 問題 | 直し方 |
|---|---|
| `DEBUG` が有効 | `DJANGO_DEBUG` を設定しない |
| `SECRET_KEY` が組み込みの既定値、または 50 文字未満 | `DJANGO_SECRET_KEY=<長いランダム値>` |
| `ALLOWED_HOSTS` が `*` または空 | `CLI2UI_ALLOWED_HOSTS=cli2ui.example.com`(カンマ区切り) |
| CSRF の信頼オリジンに公開 `https://` が無い | `CLI2UI_EXTRA_CSRF_ORIGINS=https://cli2ui.example.com` |
| アクセス制御の宣言が無い | `CLI2UI_HOSTED_AUTH=proxy` か `basic`(下記) |

cli2ui 自体にはログイン機能が無いので、アクセスをどう守るかを宣言してもらいます。

- `proxy` — 前段のリバースプロキシ、VPN、SSO ゲートウェイが認証する。cli2ui は
  「あなたが守っている」と信じます。
- `basic` — 内蔵の HTTP Basic 認証。`CLI2UI_HOSTED_BASIC_USER` と
  `CLI2UI_HOSTED_BASIC_PASSWORD`(12 文字以上)を設定します。HTTPS 越しでのみ使ってください。

**警告**(起動は止まりません)になるもの: CSRF クッキーに secure が付いていない
(`CLI2UI_SECURE_COOKIES=1`)、SSL リダイレクトも HSTS も無い(前段のプロキシが TLS を
終端しているなら問題ありません)。

## 2. 危険な操作は、明示するまで無効

hosted モードでは、次の操作は `403` になり、ボタンも画面から消えます。戻すときは
`CLI2UI_HOSTED_ALLOW` に名前をカンマ区切りで書きます。

| 名前 | 有効になるもの |
|---|---|
| `write_sql` | SQL ランナーの書込モード |
| `ddl` | テーブル / 列 / インデックス / スキーマの変更 |
| `role_admin` | ロールの作成 / 変更 / 削除 |
| `database_admin` | データベースの作成 / 削除 / 改名 / リストア、バックアップの復元 |
| `server_settings` | サーバ設定(`ALTER SYSTEM`) |
| `session_control` | セッションのキャンセル / kill、レプリケーションスロット |
| `data_transfer` | データのインポート、ダンプ、エクスポート |
| `connection_admin` | 保存済み接続の追加 / 削除 |

読み取り(overview、health、locks、EXPLAIN、読み取り専用クエリ)は常に使えます。
`CLI2UI_HOSTED_ALLOW` の綴り間違いは、黙って無視されず起動エラーになります。

## 例

```bash
CLI2UI_HOSTED=1 \
DJANGO_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(50))')" \
CLI2UI_ALLOWED_HOSTS=cli2ui.example.com \
CLI2UI_EXTRA_CSRF_ORIGINS=https://cli2ui.example.com \
CLI2UI_HOSTED_AUTH=proxy \
CLI2UI_HOSTED_ALLOW=write_sql \
CLI2UI_HOSTED_TARGETS=db.example.com:5432 \
python manage.py runserver
```

## 3. どのデータベースに繋いでよいか

cli2ui は、保存された接続に書かれたホストへそのまま接続します。公開して動かすと、
接続フォームがサーバから届くネットワークを探るのに使われかねません。hosted モードでは
すべての接続(ドライバの接続と、`pg_dump` / `mysqldump` / `psql` の子プロセス)を、
先に次の検査にかけます。

- **`CLI2UI_HOSTED_TARGETS`** — 届いてよい `host:port` のパターンをカンマ区切りで
  (`db.example.com:5432`、`*.corp.example.com:*`)。**空なら、どの接続も拒否**されます
  (起動時に警告が出ます)。
- 名前の解決先は**公開アドレス**でなければなりません。ループバック、リンクローカル
  (`169.254.169.254` のようなクラウドのメタデータを含む)、プライベート範囲は、許可リストに
  ある名前でも拒否します。DNS を書き換えて、許可済みの名前を内部ホストへ向けられる
  のを防ぐためです。データベースが本当にプライベートネットワーク(同じ VPC)にあるなら、
  その範囲を **`CLI2UI_HOSTED_PRIVATE_NETS`**(`10.0.0.0/16`)に書きます。
- 接続は、2 回目の DNS 参照ではなく、**検査済みの IP** に対して行います。

これはアプリ層の検査で、セキュリティグループやファイアウォールの代わりにはなりません。
そちらも必ず設定してください。

## 4. 追加アプリ

`CLI2UI_EXTRA_APPS` で差し込んだアプリも対象です。hosted モードは追加アプリのルートを
知らないので、**追加アプリのルートのうち状態を変えるもの(GET/HEAD/OPTIONS 以外)は、
既定で `403` で拒否**します。GET は通るので、GET のルートは安全な読み取りにしてください。

追加アプリは、`AppConfig.ready()` から、そのルートが何を必要とするかを申告して開けます。

```python
from core import hosted
hosted.declare_capability("my_route_name", "ddl")                       # 組み込みの権限
hosted.declare_capability("my_other_route", "my_write", "X を変更する")  # 新しい権限(説明が必要)
```

申告した権限は組み込みのものと同じ扱いで、`CLI2UI_HOSTED_ALLOW` に書くまで無効、
ボタンも画面から消えます。何も申告していないルートは、`CLI2UI_HOSTED_ALLOW` に何を
書いても開きません。

## 5. レート制限

クライアント IP ごと、1 分間の固定窓で数えます。超えると `429` と `Retry-After`
ヘッダを返します。数えるのは認証の**前**なので、`basic` へのパスワード総当たりも
抑えられます。

| 変数 | 既定値 | 数えるもの |
|---|---|---|
| `CLI2UI_HOSTED_RATE_ALL` | 120 | すべてのリクエスト |
| `CLI2UI_HOSTED_RATE_WRITE` | 30 | GET 以外のリクエスト |
| `CLI2UI_HOSTED_RATE_QUERY` | 20 | SQL ランナーと EXPLAIN |

`0` でその制限を切ります(3 つとも `0` だと起動時に警告)。

リバースプロキシの後ろでは、すべてのリクエストがプロキシのアドレスから届きます。
どの相手を信じるかを `CLI2UI_HOSTED_TRUSTED_PROXIES`(IP か CIDR をカンマ区切り)で
教えてください。その場合にだけ `X-Forwarded-For` を読みます。右側から、自分のプロキシを
飛ばして読むので、左側に偽の値を足しても制限は回避できません。設定しなければ、
このヘッダは無視します。

**この検査の限界:** カウンタはプロセス内のメモリにあります。gunicorn のワーカーが
複数あると、ワーカーごとに別々に数えるため、実効の上限はワーカー単位になります。
複数ワーカーで動かすなら、プロキシ側でもレート制限をかけてください。

## 試す

- `scripts/run_hosted.sh` — hosted モードで起動してブラウザを開く
  (`CLI2UI_HOSTED_ALLOW=... scripts/run_hosted.sh` で比較)。
- `scripts/verify_hosted.sh` — 上記の挙動を端から端まで検証する。
- 設計メモ: [specs/hosted-mode.md](specs/hosted-mode.md)
