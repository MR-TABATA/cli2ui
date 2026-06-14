# cli2ui をデータベースに繋ぐ — ネットワーク解説

> English: **[README.NETWORKING.md](README.NETWORKING.md)**

> 接続フォームに `localhost` と入れたのに、データベースは*すぐそこにある*のに
> 「connection refused」になった——そんな経験があるなら、このページはあなたの
> ためのものです。**なぜそうなるのか**を説明し、よくある構成ごとにコピペで効く
> 対処を載せます。

## 覚えるべき大原則はひとつ

cli2ui は**自分専用の Docker コンテナの中**で動いています。コンテナの中で動く
コードにとって `localhost` は *「このコンテナ自身」* を指します——**あなたの
マシンではありません**。データベースが cli2ui のコンテナの中にいることはまず
ないので、`localhost` は見当違いの場所を指し、接続に失敗します。

```
   ┌─────────────────── あなたのマシン（"ホスト"）───────────────────┐
   │                                                                  │
   │   ┌── cli2ui コンテナ ──┐      ┌── postgres コンテナ ──┐         │
   │   │                     │      │                       │         │
   │   │  localhost ─────────┼──┐   │   5432 で待ち受け中    │         │
   │   │  = このコンテナ ✗   │  │   │                       │         │
   │   └─────────────────────┘  │   └───────────────────────┘         │
   │                            │                                     │
   │                            └─►  cli2ui 自身に戻ってしまい、       │
   │                                 DB ではない → "connection refused"│
   └──────────────────────────────────────────────────────────────────┘
```

つまり勝負どころは **「cli2ui がどんな名前でデータベースを探すか」** です。答えは
データベースがどこで動いているかで変わります。下から自分の状況を探してください。

---

## 自分はどの状況？

| データベースはどこで動いている？ | 使う **host** の値 | 移動先 |
| --- | --- | --- |
| マシンにネイティブ導入（Homebrew, Postgres.app, インストーラ） | `host.docker.internal` | [ケース A](#ケース-a-データベースがマシンに導入されている) |
| **別の** Docker コンテナ（別の `docker compose` プロジェクト） | データベースの**コンテナ名** | [ケース B](#ケース-b-データベースが別コンテナにいる) |
| ポートを公開しているコンテナ（`-p 5432:5432`） | `host.docker.internal` | [ケース C](#ケース-c-データベースがポートを公開している) |

**port** は常にデータベース*自身*が待ち受けるポートです（PostgreSQL の既定は
`5432`）——例外は各ケースの注記を参照。

---

## ケース A: データベースがマシンに導入されている

データベースが Docker ではなく、マシン上の普通のプログラムとして動いている場合。

cli2ui の `docker-compose.yml` には、これを成立させる仕掛けが最初から入って
います——コンテナの中から「ホストマシン」を指す名前を与える `extra_hosts` です：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

接続フォームには：

| 項目 | 値 |
| --- | --- |
| host | `host.docker.internal` |
| port | `5432`（ローカル Postgres が使うポート） |
| db / user / password | あなたのデータベースのもの |

`host.docker.internal` は、コンテナの中から *あなたのマシン* を指す特別な名前で、
Docker が解決します。ここでは macOS・Windows・Linux のいずれでも同じように動き
ます。

---

## ケース B: データベースが別コンテナにいる

「実プロジェクト」で最もよくあるケースです：アプリとその DB が既に独自の
`docker compose` 構成で動いていて、その DB を cli2ui から覗きたい、という状況。

コンテナ同士は **同じネットワーク上にいるときだけ** 名前で呼び合えます。cli2ui は
自分専用のネットワーク（`cli2ui_default`）で起動するので、まず cli2ui を DB の
ネットワークに参加させ、その上で DB を **コンテナ名** で指定します。

### 手順 1 — DB のネットワーク名とコンテナ名を調べる

```bash
docker ps                 # 動いているコンテナ名を表示
docker network ls         # ネットワーク一覧
```

DB コンテナの `NETWORKS` 列を見ます。`myapp` という名前の compose プロジェクトは
普通 `myapp_default` というネットワークを作ります。

### 手順 2 — cli2ui をそのネットワークに参加させる

```bash
docker network connect <db-network> cli2ui-app-1
```

（`cli2ui-app-1` は `docker compose up` で起動したときの cli2ui コンテナ名です。
`docker ps` で確認してください。）

### 手順 3 — コンテナ名で接続する

| 項目 | 値 |
| --- | --- |
| host | DB の **コンテナ名**（例 `myapp-db`） |
| port | `5432` |
| db / user / password | あなたのデータベースのもの |

これで完了です——誰の compose ファイルも編集せず、コンテナを止めるまで有効です。
後で外すには `docker network disconnect <db-network> cli2ui-app-1`。

> **恒久的にしたい？** `network connect` の代わりに、両プロジェクトを1つの共有
> 外部ネットワークに載せます：
> ```bash
> docker network create shared-net
> ```
> ```yaml
> # 両方の docker-compose.yml に追記：
> networks:
>   default:
>     name: shared-net
>     external: true
> ```
> こうすると常にコンテナ名で到達でき、手動手順が不要になります。

---

## ケース C: データベースがポートを公開している

DB のコンテナがポートをマシンにマッピングしている場合——`docker ps` に
`0.0.0.0:5432->5432/tcp` のように出ます——マシンから見れば DB はホスト上で到達
可能です。ケース A と同じくホスト経由を使います：

| 項目 | 値 |
| --- | --- |
| host | `host.docker.internal` |
| port | **公開側**（左側）のポート——例 `5432`、`5433:5432` なら `5433` |
| db / user / password | あなたのデータベースのもの |

これならネットワークを一切いじらずに済みます。**ただし** トラブルシュートの macOS
注意点を参照——*別の* データベースが既にそのポートをマシン上で握っている場合は、
ケース B のほうが確実です。

---

## 通しの実例

別アプリ **SyncVey** が Docker で動いていて、その DB コンテナが
`syncvey-db`、ネットワークが `syncvey_default` だとします。一連の流れは
こうです（これはケース B）：

```bash
# 1. 何がどこで動いているか確認
docker ps
#   NAMES         IMAGE                  NETWORKS
#   syncvey-db    postgres:18-alpine     syncvey_default   ← 接続先

# 2. cli2ui を起動（8000 が埋まっている場合に備えホスト側 8001）
docker run -d --name cli2ui-app-1 -p 8001:8000 cli2ui-app

# 3. cli2ui を SyncVey のネットワークに参加させる
docker network connect syncvey_default cli2ui-app-1

# 4. http://localhost:8001 を開きフォームに入力：
#      host = syncvey-db      port = 5432
#      db   = <相手のDB>      user/password = <相手のもの>
```

これで cli2ui は `syncvey-db` を名前で解決して接続できます。完了。

---

## トラブルシュート

| 症状 | 考えられる原因 | 対処 |
| --- | --- | --- |
| host を `localhost` にすると `connection refused` | `localhost` = cli2ui コンテナ自身であり DB ではない | `host.docker.internal`（ケース A/C）か、コンテナ名（ケース B）を使う |
| `could not translate host name "<名前>"` | cli2ui がそのコンテナと同じネットワークにいない | `docker network connect <db-network> cli2ui-app-1`（ケース B） |
| `host.docker.internal` で繋がるが **別の** DB に当たる | そのポートを別の DB が既にマシン上で握っている（macOS でネイティブ Postgres が `5432` を握り、コンテナの公開 `5432` を隠すのが典型） | ケース B（共有ネットワーク上でコンテナ名）を使う——曖昧さがない |
| コンテナの **IP**（例 `172.x.x.x`）にマシンから届かない | Docker Desktop（macOS/Windows）ではコンテナ IP はホストからルーティング不可 | IP を使わず、ケース B（コンテナ名）かケース C（公開ポート）で |
| 動いていたのに再起動後に壊れる | `docker network connect` はコンテナの寿命限り | `up` のたびに再実行するか、ケース B の恒久共有ネットワーク構成にする |

---

## メンタルモデルの要約

- **コンテナ内の `localhost` = そのコンテナ自身。** ほぼ確実にあなたの DB ではない。
- **`host.docker.internal` = あなたのマシン**（ネイティブ導入の DB や、ホスト
  ポートに公開された DB 向け）。
- **コンテナ名は同じ Docker ネットワークにいるときだけ有効**——まず
  `docker network connect` で参加する。
- **macOS/Windows の Docker Desktop** では、コンテナの **IP アドレス** はマシン
  から到達不可。常に名前か公開ポートで。

それでも詰まったら、`docker ps` と `docker network ls` の出力、そしてフォームに
どう入力したかを添えて issue を開いてください。
