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
python manage.py runserver
```

## まだ実装していないもの

接続先データベース / 外向き接続の許可リストと、レート制限です。それまでは、cli2ui が
どの DB に届くか、どれくらいの頻度で叩けるかを、ネットワーク層で制限してください。

## 試す

- `scripts/run_hosted.sh` — hosted モードで起動してブラウザを開く
  (`CLI2UI_HOSTED_ALLOW=... scripts/run_hosted.sh` で比較)。
- `scripts/verify_hosted.sh` — 上記の挙動を端から端まで検証する。
- 設計メモ: [specs/hosted-mode.md](specs/hosted-mode.md)
