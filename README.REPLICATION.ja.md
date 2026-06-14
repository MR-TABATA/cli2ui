# レプリケーションをローカルで試す

> English: **[README.REPLICATION.md](README.REPLICATION.md)**

cli2ui の **Replication（レプリケーション）** パネルは `pg_stat_replication` /
`pg_replication_slots` を読むため、**Standbys** テーブルは実際にレプリカが接続
されるまで空のままです。パネル自体は単一データベースでも機能します（準備チェック
——`wal_level`、`max_wal_senders`——現在の WAL 位置を表示し、スロットの作成 / 削除が
できます）が、standby が現れる様子を見るには、本物の primary + standby のペアが
必要です。

このファイルは、それを単独で立ち上げる方法です。cli2ui が別コンテナのデータベース
に一般的にどう到達するかは、
[メイン README](README.md#connecting-to-a-database-in-another-container)
（詳細は **[README.NETWORKING.ja.md](README.NETWORKING.ja.md)**）を参照してください。

## primary + standby を立ち上げる

以下を `docker-compose.replication.yml` として保存し、
`docker compose -f docker-compose.replication.yml up` を実行します：

```yaml
# 既定イメージは localhost からのレプリケーションしか許可しないため、この
# インライン設定で hba ルールを追記し、standby コンテナからの接続を許可する。
# （$$ は Compose による $PGDATA の展開を防ぐ——スクリプトにそのまま届く必要がある）
configs:
  init_repl:
    content: |
      #!/bin/bash
      echo "host replication all all trust" >> "$$PGDATA/pg_hba.conf"

services:
  primary:
    image: postgres:16
    environment:
      POSTGRES_USER: demo
      POSTGRES_PASSWORD: demo
      POSTGRES_DB: shop
      POSTGRES_HOST_AUTH_METHOD: trust
    configs:
      - source: init_repl
        target: /docker-entrypoint-initdb.d/10-repl.sh
        mode: 0755
    command: >
      postgres -c wal_level=replica -c max_wal_senders=10
               -c hot_standby=on -c listen_addresses=*
    ports: ["5433:5432"]   # cli2ui が host.docker.internal:5433 で到達できるように

  standby:
    image: postgres:16
    user: postgres
    depends_on: [primary]
    # primary を待ち、そこから base-backup を取り、Postgres が要求する
    # データディレクトリの権限（0700）に直してから、hot standby として起動する。
    entrypoint: >
      bash -c '
        until pg_isready -h primary -U demo -d postgres; do sleep 1; done;
        rm -rf /var/lib/postgresql/data/*;
        pg_basebackup -h primary -U demo -D /var/lib/postgresql/data -R -X stream;
        chmod 0700 /var/lib/postgresql/data;
        exec postgres'
```

## cli2ui を向ける

cli2ui で **primary** に接続します：

| 項目     | 値                     |
| :------- | :--------------------- |
| host     | `host.docker.internal` |
| port     | `5433`                 |
| db       | `shop`                 |
| user     | `demo`                 |
| password | `demo`                 |

**Replication** パネルを開きます。数秒以内に standby が **Standbys** の下に
`walreceiver / streaming` として現れます。

## 注意

- これは **デモであって本番レシピではありません**——共有パスワード1つ、`trust`
  認証、TLS なし。hba ルールを本番に持ち込まないこと。
- このレシピが回避している2つの落とし穴：標準イメージは localhost からの
  レプリケーションしか許可しない（だからインライン hba ルール）、そして
  `pg_basebackup` はデータディレクトリを Postgres が起動を拒否する権限のまま
  残す（だから `chmod 0700`）。
- 後片付けは `docker compose -f docker-compose.replication.yml down -v`。
