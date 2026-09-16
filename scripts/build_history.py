#!/usr/bin/env python3
"""Generate the development-history page (`docs/history.{ja,en}.html`).

Unlike SyncVey's page of the same name, this one is built from **commits**,
not merged pull requests. cli2ui mixes PR merges and direct pushes to `main`
depending on the day, and a PR-only generator silently drops every commit
that took the direct route — that is exactly what happened on SyncVey's page
(three commits in a row, one of them a version cut, never got an entry,
no matter how many times the rebuild ran). Reading from `git log` instead
means every commit gets a place on the page, regardless of how it landed.

Facts come from git at build time:

    * commit list, dates, sha       -> `git log`
    * lines added/removed           -> `git log --shortstat`
    * version boundaries            -> `git tag` (peeled to the commit each
                                        tag actually points at)

The bilingual text is the one thing a machine cannot derive: commit messages
here are written in whichever language was natural at the time (mostly
English for the first two weeks, mostly Japanese since), never both. COMMITS
below pairs every commit's sha with a ja/en translation written by hand. A
commit that lands without an entry here is not an error — same as SyncVey's
"recent changes" fallback, it is shown using its own raw subject line on
both pages until someone adds a translated pair.

    python3 scripts/build_history.py

`.github/workflows/history.yml` re-runs this on every push to main and
commits the result if it changed.
"""

from __future__ import annotations

import datetime
import html
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO = 'MR-TABATA/cli2ui'

PR_SUFFIX_RE = re.compile(r'\s*\(#(\d+)\)\s*$')


# ---------------------------------------------------------------------------
# Editorial layer — the one thing a machine cannot derive: a translation of
# each commit's subject into the language it was not written in. (sha, ja, en)
# A PR reference embedded in the raw subject (" (#N)") is stripped before
# translation and re-attached as a separate link at build time, so it is not
# duplicated here.
# ---------------------------------------------------------------------------

COMMITS: list[tuple[str, str, str]] = [
    ('311827d', '最初のコミット: cli2ui MVP — PostgreSQL のテーブル一覧', 'Initial commit: cli2ui MVP — PostgreSQL table list'),
    ('2370130', 'DB クライアント風のワークスペースと postgresql.conf エディタを追加', 'Add DB-client workspace and postgresql.conf editor'),
    ('23688de', '再起動待ちの設定向けに、再起動コマンドのヘルパーを追加', 'Add restart-command helper for pending-restart settings'),
    ('c815e29', 'オブジェクトブラウザを追加: データベース・スキーマ・ロール（\\l, \\dn, \\du）', 'Add Objects browser: databases, schemas, roles (\\l, \\dn, \\du)'),
    ('48b5bbe', 'オブジェクトブラウザを README のステータス一覧に記載', 'Document Objects browser in README status list'),
    ('7ee33d8', 'CSRF 保護を有効化（htmx が X-CSRFToken ヘッダーでトークンを送信）', 'Enable CSRF protection (htmx sends token via X-CSRFToken header)'),
    ('e9b4697', 'オブジェクトブラウザにスキーマの作成・削除を追加', 'Add schema create/drop to the Objects browser'),
    ('990c87d', 'オブジェクトブラウザにロールの作成・削除を追加', 'Add role create/drop to the Objects browser'),
    ('80e8e53', '読み取り専用の SQL ランナーを追加', 'Add read-only SQL runner'),
    ('1151876', 'EXPLAIN プランのスナップショットを追加: 保存・表示・差分', 'Add EXPLAIN plan snapshots: save, view plan, and diff'),
    ('b0438f2', 'クエリが 0 行のとき、分かりやすいメッセージを表示', 'Show a clear message when a query returns no rows'),
    ('b45c8bd', 'アクティビティパネルとワークスペース概要を追加', 'Add Activity panel and workspace overview'),
    ('0c1156e', 'MIT ライセンスを追加', 'Add MIT LICENSE'),
    ('fb7c9bc', '構造化された EXPLAIN 差分とスケールシミュレーションを追加', 'Add structured EXPLAIN diff and scale simulation'),
    ('9fa3f47', 'テーブルごとのインデックス管理を追加: 一覧・作成・削除', 'Add index management: list, create, drop per table'),
    ('8ebbc56', 'インデックス作成・削除の E2E スモークテストを追加。ブラウザ用の共通土台も整備', 'Add E2E smoke test for index create/drop; share browser scaffolding'),
    ('0064cb6', 'インデックスのレビュー指摘に対応: 列順・valid フラグ・列パースを堅牢に', 'Address index review: ordered columns, valid flag, robust column parse'),
    ('a77fa20', 'what-if インデックスラボを追加: 試して測ってから作成', 'Add what-if index lab: try an index, measure it, then create it'),
    ('5ef403b', 'schema_delete / role_delete で名前を検証', 'Validate name in schema_delete / role_delete'),
    ('5ebc764', 'セキュリティ強化: 依存関係・クリックジャッキング対策・監査ドキュメント', 'Security hardening: deps, clickjacking, audit doc'),
    ('85ead4a', 'ヘルスパネルを追加: テーブルサイズと未使用インデックス', 'Add Health panel: table sizes and unused indexes'),
    ('bd43ddd', 'ヘルスパネルにデッドタプル/VACUUM のカードを追加', 'Add dead-rows / vacuum card to the Health panel'),
    ('ed83ec4', 'データベースの作成・複製・リネーム・削除を追加', 'Add database create / clone / rename / drop'),
    ('e92eab6', 'スキーマ・ロールの ALTER を追加（リネーム・所有者・属性）', 'Add ALTER for schemas and roles (rename, owner, attributes)'),
    ('5fa000b', 'テーブル単位の操作を追加: リネーム・TRUNCATE・DROP', 'Add table-level operations: rename / truncate / drop'),
    ('032b1ae', '列操作、pg_dump によるバックアップ/リストア、自動セーフティネットのスナップショットを追加', 'Add column ops, pg_dump backup/restore, and an automatic safety-net snapshot'),
    ('38397b4', 'ロック/ブロッキングとレプリケーションのパネルを追加', 'Add locks/blocking and replication panels'),
    ('fc03d11', '他コンテナの DB への到達方法とレプリケーションの試し方をドキュメント化', 'Document reaching DBs in other containers + replication trial'),
    ('61feab7', 'ヘルスパネルにテーブルの肥大化（bloat）推定を追加', 'Add table bloat estimate to the Health panel'),
    ('7179ae2', 'コマンド履歴パネルを追加', 'Add command history panel'),
    ('dfcb8bb', 'SQL ランナーに書き込みモードを追加', 'Add write mode to the SQL runner'),
    ('283ad26', 'リストアをストリーミング化し、既存データベースへの復元も許可', 'Stream restores and allow restoring into an existing database'),
    ('9b9a301', '自動スナップショットのセーフティネットをリネーム/ALTER にも拡張', 'Extend the auto-snapshot safety net to rename / alter'),
    ('2166a19', 'PostgreSQL のクエリ文を pg_sql.py に切り出し', 'Extract PostgreSQL query text into pg_sql.py'),
    ('10ae239', 'views.py をドメインごとに core/views パッケージへ分割', 'Split views.py into a core/views package by domain'),
    ('d1d41b7', 'UI デザインシステムの土台を追加（STYLE.md、.btn-*/.field、_banner）', 'Add UI design-system foundation (STYLE.md, .btn-*/.field, _banner)'),
    ('9ca6c70', 'テーブル詳細パネルを再設計: サブタブ＋共通ドロワー', 'Redesign the table detail panel: subtabs + shared drawer'),
    ('dba65f0', 'オブジェクトパネルを再設計: サブタブ＋共通ドロワー', 'Redesign the Objects panel: subtabs + shared drawer'),
    ('47ae164', 'ワークスペースのホームを、稼働状況が分かる bento レイアウトに', 'Make the workspace home a live operational bento'),
    ('3a79e15', '残りのパネルにも共通のデザインシステムクラスを適用', 'Adopt shared design-system classes in the remaining panels'),
    ('0da83a6', 'サイドバーの区域切り替え用に .nav-btn クラスを切り出し', 'Extract a .nav-btn class for the sidebar section switcher'),
    ('e5b96b0', '各パネルの薄い文字のコントラストを底上げ', 'Lift muted text contrast across the panels'),
    ('2717d43', '区域ナビをサイドバーからトップバーへ移動', 'Move the section nav from the sidebar into a top bar'),
    ('d6c6e12', '概要をホバー式メガメニューに（区域ごとにまとめた bento）', 'Make the overview a hover mega-menu (grouped section bento)'),
    ('3e7cbb8', 'bento を枠付きボックスにし、概要クリックでも表示', 'Make the bento a walled box and show it on overview click too'),
    ('643110a', 'bento のセルサイズを変え、テーブルへのリンクも修正', 'Vary the bento cell sizes and fix the Tables link'),
    ('c828ba9', 'bento を大きさの異なるモザイク状に敷き詰め', 'Lay the bento out as an interlocking size-varied mosaic'),
    ('1c01c8f', 'bento を同じ大きさの正方形タイルに変更（Windows 風）', 'Switch the bento to equal square tiles (Windows-style)'),
    ('93147d6', '4 列のタイルグリッドに（タイルを大きく）', 'Use a 4-column tile grid (bigger tiles)'),
    ('b87be8d', 'スケッチ通りに bento を配置: 大きな機能タイル 2 枚＋タイルグリッド', 'Lay out the bento per the sketch: two feature tiles + tile grid'),
    ('f6bc464', 'bento タイルに SVG アイコンとグループ別アクセントカラーを追加', 'Add SVG icons and per-group accent colour to the bento tiles'),
    ('144e74d', '機能タイルに、うっすらとした装飾用 SVG モチーフを追加', 'Add faint decorative SVG motifs to the feature tiles'),
    ('0857f82', 'bento を 4×3 の均一タイルにし、グループごとに色帯を付与', 'Switch bento to uniform 4x3 tiles with per-group colour bands'),
    ('d0d01b4', 'bento の先頭に主力機能（SQL ランナー、インデックスラボ）を配置', 'Lead the bento with the headline tools (SQL runner, Index lab)'),
    ('82557de', '概要のホバーメニューが溢れたらスクロールするように', 'Make the overview hover menu scroll when it overflows'),
    ('29c2d4d', '概要のホバーメニューをコンパクトなテキスト一覧に', 'Make the overview hover menu a compact text list'),
    ('b9c1d5c', '概要トップをメニューに合わせたテキストダッシュボードに', 'Make the overview landing a text dashboard to match the menu'),
    ('79913fa', '概要ダッシュボードの行間を広げる', 'Loosen the overview dashboard row spacing'),
    ('a848d67', 'docs/ 配下にバイリンガルのランディングページを追加', 'Add bilingual landing page under docs/'),
    ('6254df9', 'アプリ全体に日本語/英語の国際化対応を追加', 'Add JP/EN internationalization across the app'),
    ('96be7db', '公開に向けたドキュメント整備: README 刷新、CONTRIBUTING、SECURITY', 'Prep public release docs: README refresh, CONTRIBUTING, SECURITY'),
    ('d2d73f3', 'i18n/リリース差分に対してセキュリティ静的チェックを再実行', 'Re-run security static checks for the i18n / release diff'),
    ('234db7d', 'README と日英ランディングページに、ローカル専用/無認証の警告を追加', 'Add local-only / no-auth warning to README and EN/JA landing pages'),
    ('fd05b6b', 'ワークスペース概要を 1 つの DB 接続にまとめる', 'Gather the workspace overview over one DB connection'),
    ('51e0d91', 'ドキュメント: ポート衝突の注意書き＋ Docker ネットワーキングガイド（日英）', 'Docs: port-clash note + Docker networking guide (EN/JA)'),
    ('a26aee3', '未実装の MySQL を宣伝文句から外す', 'Drop MySQL from the marketing copy (not implemented yet)'),
    ('e0e6b71', 'cli2ui.com を GitHub Pages のカスタムドメインに設定', 'Set cli2ui.com as the GitHub Pages custom domain'),
    ('ac8ac65', 'OG/Twitter のメタ URL を cli2ui.com のカスタムドメインへ向ける', 'Point OG/Twitter meta URLs at the cli2ui.com custom domain'),
    ('608ecf0', '接続ごとの自動バックアップ容量に合計サイズの上限を設定', 'Cap auto-backup storage per connection by total size'),
    ('3935216', 'ドキュメント: 自動バックアップの保持上限を記載し、nosec もログに残す', "Docs: note auto-backup retention cap + log its nosec"),
    ('86c21f8', 'レプリケーション: 現在の値を使ったスタンバイ構築手順を生成', 'Replication: generate a standby-setup recipe with current values'),
    ('9f6b136', 'ネットワーキングガイド: 自分のプロジェクトの compose 内で cli2ui を動かす方法を記載', "Networking guide: document running cli2ui inside your project's compose"),
    ('06bcf38', 'クエリ結果の CSV / JSON エクスポートを追加', 'Add CSV / JSON export of query results'),
    ('8aefbe3', 'README/LP を実装の現状に合わせて修正', "Bring README/LP in line with what's actually implemented"),
    ('66dde68', 'スケールシミュレーションとインデックスラボを planner_lab アプリへ分離', 'Split scale simulation and the index lab out into the planner_lab app'),
    ('f94bc33', 'CONTRIBUTING: オプション機能アプリ（planner_lab）のパターンを追記', 'CONTRIBUTING: document the optional-feature-app pattern (planner_lab)'),
    ('efeabc4', 'コメントの表現を中立化（planner_lab の説明）', 'Neutralize wording in comments (the planner_lab description)'),
    ('7321cb3', 'features.py のコメント表現を中立化', "Neutralize wording in features.py's comments"),
    ('740549c', 'バージョニング導入: __version__/CHANGELOG/フッター表示', 'Introduce versioning: __version__ / CHANGELOG / footer display'),
    ('6961f9e', 'v0.9.0: フィルタビルダー / CSV インポート / テーブル export / CLI2UI_DB_PATH', 'v0.9.0: filter builder / CSV import / table export / CLI2UI_DB_PATH'),
    ('8b876a8', 'MySQL エンジン Phase 1: 閲覧 / クエリ / DDL 対応', 'MySQL engine Phase 1: browsing / queries / DDL support'),
    ('d4a655f', 'MySQL エンジンのセキュリティ静的チェック再実行', 'Re-run security static checks on the MySQL engine'),
    ('06a20ac', 'MySQL エンジン Phase 2: ロック競合検出と偽陰性の解消', 'MySQL engine Phase 2: lock-contention detection, closing false negatives'),
    ('51f78b0', 'CHANGELOG: MySQL Phase 2 を Unreleased に追記', 'CHANGELOG: log MySQL Phase 2 under Unreleased'),
    ('9dee6fa', 'MySQL Phase 3a: バックアップ（mysqldump/mysql）と破壊的操作の修復', 'MySQL Phase 3a: backup (mysqldump/mysql) and repair for destructive operations'),
    ('e01be74', 'MySQL Phase 3b: 設定エディタ（SET PERSIST）', 'MySQL Phase 3b: settings editor (SET PERSIST)'),
    ('20d059f', 'MySQL Phase 3c: レプリケーション（状態＋セットアップ手順）', 'MySQL Phase 3c: replication (status + setup recipe)'),
    ('133c289', 'CHANGELOG: MySQL Phase 3（backup/settings/replication）を Unreleased に追記', 'CHANGELOG: log MySQL Phase 3 (backup/settings/replication) under Unreleased'),
    ('b6d7864', 'MySQL: 数値システム変数の SET PERSIST を未クォートで送る', 'MySQL: send SET PERSIST for numeric system variables unquoted'),
    ('9d785f8', 'v1.0.0 リリース: MySQL 対応（マルチ DB 対応の運用コンソールへ）', 'Release v1.0.0: MySQL support (multi-DB ops console)'),
    ('8cea249', 'LP: 対応データベースの注記を追加（PostgreSQL & MySQL）', 'LP: add a "supported databases" note (PostgreSQL & MySQL)'),
    ('1257e2b', 'LP: ヒーローコピーを「psql / mysql」に（psql だけでなく）', 'LP: hero copy says "psql / mysql" (not just psql)'),
    ('1a746ae', 'LP: 英語メタ情報にマルチ DB を反映し、MySQL コマンド例も追加', 'LP: multi-DB in EN meta + a MySQL command example'),
    ('5d993dc', 'アクティビティ: 接続の余裕（使用数 vs max_connections）を表示', 'Activity: show connection headroom (used vs max_connections)'),
    ('251ae47', 'i18n(ja): MySQL の UI 文字列を翻訳（Phase 作業以来未翻訳だったもの）', 'i18n(ja): translate the MySQL UI strings (untranslated since Phase work)'),
    ('f2573c4', 'docs: LP のメタ情報と README のステータスで、マルチ DB の文言を揃える', 'docs: sync multi-DB wording in LP metadata + README status'),
    ('7d072cf', 'テーブル詳細: 列コメントを表示', 'Table detail: surface column comments'),
    ('061f138', 'テーブル詳細: テーブルコメントを表示', 'Table detail: surface the table comment'),
    ('0bf538d', 'docs: README+LP（日英）に列/テーブルコメントの記載を追加', 'docs: mention column & table comments in README + LP (en/ja)'),
    ('f2363a9', '列ビューア: 生成列を表示', 'Column viewer: surface generated columns'),
    ('dbb5ece', 'docs: README+LP（日英）に生成列の記載を追加', 'docs: mention generated columns in README + LP (en/ja)'),
    ('a3c2180', '依存関係パネル: 安全な TRUNCATE 順序と FK 循環検出', 'Dependencies panel: safe TRUNCATE order + FK cycle detection'),
    ('d1ce89f', 'v1.1.0 リリース', 'Release v1.1.0'),
    ('7b3f0a0', 'ヘルス: 未インデックスの外部キーと冗長インデックスの検出', 'Health: unindexed foreign keys & redundant index detection'),
    ('5263ddb', 'v1.2.0 リリース', 'Release v1.2.0'),
    ('ce669c0', 'ForeignKeyEdge の docstring にある、自己参照 FK についての不正確な注記を修正', 'Fix inaccurate self-referential FK note in ForeignKeyEdge docstring'),
    ('883919b', 'ヘルス: 部分インデックスと無効インデックスを、FK のインデックスとして数えないように', "Health: partial and invalid indexes no longer count as an FK's index"),
    ('d1e5081', '拡張機能パネル: \\dx の GUI 版。導入済み/導入可能な拡張機能を表示', 'Extensions panel: \\dx GUI showing installed + available extensions'),
    ('4d778d3', 'docs: 拡張機能パネルを README とランディングページに記載', 'Docs: list the Extensions panel in README and the landing page'),
    ('b23cc4c', 'JSONB 構造: JSON 列の実際の構造をオンデマンドでサンプリング', "JSONB shape: on-demand sampling of a JSON column's observed structure"),
    ('477bc9b', 'デモデータ: オプトインの Airlines データベース（compose の "demo" プロファイル）', 'Demo data: opt-in Airlines database service (compose profile "demo")'),
    ('86780e3', 'docs: README・ランディングページ・CHANGELOG を実装内容に合わせる', 'Docs: sync README, landing page, and CHANGELOG with what shipped'),
    ('1bdc63b', 'docs: 同梱のデモデータを主要な訴求点として打ち出す', 'Docs: pitch the bundled demo data as a first-class selling point'),
    ('0bde611', 'ヘルス: 孤立行 — NOT VALID な FK と、推測される *_id 参照', 'Health: orphan rows — NOT VALID FKs + inferred *_id references'),
    ('a547d35', 'ロック: 待機グラフを、大元のブロッカーを頂点とする木構造に畳む', 'Locks: fold the wait-for graph into head-blocker trees'),
    ('99d7782', 'レプリケーション: スタンバイの再生遅延をバイト数だけでなく時間でも表示', 'Replication: show standby replay lag in time, not just bytes'),
    ('2f78503', 'DDL: ロック待ちに上限を設け、変更がテーブルを止まらせないように', "DDL: bound the lock wait so a change can't stall the table"),
    ('173aa9a', 'ランナー: 書き込みモードでも DDL のロックガードを提供し、トグルを実際に送信するよう修正', 'Runner: offer the DDL lock guard in write mode — and actually send the toggles'),
    ('4a5ad93', 'v1.3.0 リリース', 'Release v1.3.0'),
    ('58ce324', 'ランディングページ: 「実行する前に何が分かるか」を前面に', 'Landing page: lead with what the tool answers before you run it'),
    ('cf156b4', 'ロック: 本来説明すべきロックの後ろにワークスペース自身が並んでしまうのを解消', 'Locks: stop the workspace from queueing behind the lock it should explain'),
    ('f0a3b3b', 'メトリクス: GitHub が 14 日で捨てるダウンロード数を保持する', 'Metrics: keep the download numbers GitHub throws away after 14 days'),
    ('9b1ad11', '配布: ビルドせず公開済みイメージから起動するように', 'Distribution: start from the published image instead of building it'),
    ('c226e83', 'v1.4.0 リリース', 'Release 1.4.0'),
    ('103a42d', 'v1.5.0 の 2 本: 無効インデックスの横断表示と、複合 UNIQUE の NULL すり抜け', "v1.5.0's two pieces: a cross-table view of invalid indexes, and NULLs slipping through a composite UNIQUE"),
    ('bb03b40', '1.5.0: 版上げと CHANGELOG', '1.5.0: version bump and CHANGELOG'),
    ('b58b3ee', '押す前に見せる（v1.6.0）: これから流す SQL と、TRUNCATE / DROP が持っていくもの', 'See it before you press it (v1.6.0): the SQL about to run, and what TRUNCATE / DROP will take with it'),
    ('05297a2', 'DROP … CASCADE で一緒に消えるものを、押す前に見せる', 'Show what DROP … CASCADE takes with it, before you press it'),
    ('4215b07', '1.6.0: 版上げと CHANGELOG', '1.6.0: version bump and CHANGELOG'),
    ('d35a8cb', '1.6.1: データ喪失バグ 2 件（パネルを開くと DB が消える／リストアが戻したものを捨てる）', '1.6.1: two data-loss bugs (opening a panel could wipe a database / a restore discarding what it had just restored)'),
    ('df54f3b', 'テストを PR で走らせる', 'Run the test suite on pull requests'),
    ('2eaa794', '1.6.1 の見出しを実態に合わせる（1 件ではなく 2 件）', 'Correct the 1.6.1 heading to match reality (two bugs, not one)'),
    ('5feab37', 'DDL リハーサル（v1.7.0）: 本物の ALTER を試して、捨てる', 'DDL rehearsal (v1.7.0): run a real ALTER, then throw it away'),
    ('85a6e06', '画面がデータベースの時刻を書き換えていたのを直す', 'Fix the screen rewriting timestamps that came from the database'),
    ('8f31f62', '1.7.0: 版上げと CHANGELOG', '1.7.0: version bump and CHANGELOG'),
    ('aea0a50', 'テーブル一覧が、空でないテーブルを「0 行」と言っていたのを直す', 'Fix the table list calling a non-empty table "0 rows"'),
    ('2a32199', 'テーブル名を押したら中身を出す（1000 行ずつのページ送り）', 'Clicking a table now shows its rows, 1,000 at a time'),
    ('d3891ef', '1.8.0: 版上げと CHANGELOG', '1.8.0: version bump and CHANGELOG'),
    ('bb74fc6', 'テーブル画面の並びを変える（グリッドを窓の底まで／区域を見えるところへ）', "Rework the table screen's layout (the grid now reaches the bottom of the window / sections stay visible)"),
    ('db8dda0', 'CLAUDE.md を git 管理から外す', 'Stop tracking CLAUDE.md in git'),
    ('b658c05', '色を役割で持ち、テーマを差し替えられるようにする', 'Hold colour by role so the theme can be swapped'),
    ('c8e1203', 'planner_lab のテンプレートも役割へ（前のコミットの取りこぼし）', "Bring planner_lab's templates onto roles too (a leftover from the previous commit)"),
    ('62f0f6e', '自作テーマ ── 3 色だけ決めて、残りは導く', 'A custom theme — pick three colours, derive the rest'),
    ('523876a', 'テーマの取りこぼし 2 件 ── 読めない組み合わせと、翻訳を通していない文言', 'Two things the theme work missed — an unreadable combination, and text that never went through translation'),
    ('1ba4bc8', 'サイトをライトにして、同じ機能を二度言うのをやめる', 'Make the site light, and stop saying the same feature twice'),
    ('d3c341c', 'テーブル一覧を pg_class ベースへ ── 1 クエリで UNLOGGED・パーティション・マテビュー・外部テーブルを直す', 'Move the table list onto pg_class — one query fixes UNLOGGED, partitions, materialized views, and foreign tables'),
    ('dac98ff', 'airlines サンプル DB に restart: unless-stopped を足す', 'Add restart: unless-stopped to the airlines sample database'),
    ('ab1bbb6', '1.9.0: 版上げと CHANGELOG、翻訳の抜けを直す', '1.9.0: version bump, CHANGELOG, and fixing gaps in translation'),
]

# Trailing-bucket title for commits on main that have not been tagged yet.
UNRELEASED_TITLE = ('未リリース', 'Unreleased')


# ---------------------------------------------------------------------------
# Measured layer
# ---------------------------------------------------------------------------

def _git(args: list[str]) -> str:
    return subprocess.run(['git', *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, check=True).stdout.strip()


def _tag_commits() -> list[tuple[str, str]]:
    """[(tag, short_sha), ...] in version order, peeled to the commit each
    annotated tag actually points at (not the tag object's own sha)."""
    tags = _git(['tag', '--sort=v:refname']).splitlines()
    out = []
    for t in tags:
        if not t.startswith('v'):
            continue
        sha = _git(['rev-parse', '--short', f'{t}^{{commit}}'])
        out.append((t, sha))
    return out


def collect():
    if _git(['rev-parse', '--is-shallow-repository']) == 'true':
        raise RuntimeError(
            'the repository is a shallow clone, so commit history is not '
            'available and the statistics would be wrong.\n'
            '  In CI, check out with `fetch-depth: 0`. Locally, run '
            '`git fetch --unshallow`.'
        )

    log = _git(['log', '--reverse', '--format=%h|%H|%ad|%s', '--date=short'])
    stats = _git(['log', '--reverse', '--format=@@%h', '--shortstat'])

    added: dict[str, int] = {}
    removed: dict[str, int] = {}
    cur = None
    for line in stats.splitlines():
        if line.startswith('@@'):
            cur = line[2:]
        elif line.strip() and cur is not None:
            m_ins = re.search(r'(\d+) insertion', line)
            m_del = re.search(r'(\d+) deletion', line)
            added[cur] = int(m_ins.group(1)) if m_ins else 0
            removed[cur] = int(m_del.group(1)) if m_del else 0

    translations = {sha: (ja, en) for sha, ja, en in COMMITS}

    commits = []
    for line in log.splitlines():
        short, full, date_s, subject = line.split('|', 3)
        m = PR_SUFFIX_RE.search(subject)
        pr = int(m.group(1)) if m else None
        clean_subject = PR_SUFFIX_RE.sub('', subject)
        ja, en = translations.get(short, (clean_subject, clean_subject))
        commits.append({
            'sha': short,
            'date': datetime.date.fromisoformat(date_s),
            'ja': ja,
            'en': en,
            'pr': pr,
            'added': added.get(short, 0),
            'removed': removed.get(short, 0),
        })

    unknown = set(translations) - {c['sha'] for c in commits}
    if unknown:
        print(f'warning: translations exist for commits not in history: {sorted(unknown)}',
              file=sys.stderr)
    missing = [c['sha'] for c in commits if c['sha'] not in translations]
    if missing:
        print(f'note: {len(missing)} commit(s) shown with their raw subject on both '
              f'pages — add a translated pair to COMMITS in {Path(__file__).name}: '
              f'{missing}', file=sys.stderr)

    tags = _tag_commits()
    first = commits[0]['date']
    last = commits[-1]['date']

    return {'commits': commits, 'tags': tags, 'first': first, 'last': last}


def group_by_version(data):
    """[(ja_title, en_title, tag_or_none, [commit, ...]), ...] in chronological
    order. A bucket runs up to and including the commit a tag points at, and
    is titled after that tag — so "v0.8.0" is everything that had landed by
    the time v0.8.0 shipped, first commit included. Whatever is left after
    the last tag (commits not yet released) becomes its own trailing bucket."""
    commits = data['commits']
    tag_shas = {sha: tag for tag, sha in data['tags']}
    groups = []
    bucket = []
    for c in commits:
        bucket.append(c)
        if c['sha'] in tag_shas:
            tag = tag_shas[c['sha']]
            groups.append((tag, tag, tag, bucket))
            bucket = []
    if bucket:
        ja_t, en_t = UNRELEASED_TITLE
        groups.append((ja_t, en_t, None, bucket))
    return groups


def weekly_counts(commits, first, last):
    weeks: dict[int, int] = {}
    total_weeks = ((last - first).days // 7) + 1
    for index in range(total_weeks):
        weeks[index] = 0
    for c in commits:
        idx = (c['date'] - first).days // 7
        weeks[idx] = weeks.get(idx, 0) + 1
    return [(first + datetime.timedelta(days=i * 7), weeks[i]) for i in sorted(weeks)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def e(text) -> str:
    return html.escape(str(text), quote=True)


STYLE = """
    html { scroll-behavior: smooth; }
    body { font-family: "Inter", ui-sans-serif, system-ui, sans-serif; margin: 0;
           background: #ffffff; color: #18181b; -webkit-font-smoothing: auto; }
    .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace; }
    a { color: inherit; }
    .container { max-width: 880px; margin: 0 auto; padding: 0 1.5rem; }

    header.site { position: sticky; top: 0; z-index: 20; border-bottom: 1px solid #e4e4e7;
                  background: rgba(255,255,255,.85); backdrop-filter: blur(8px); }
    .nav-inner { max-width: 72rem; margin: 0 auto; padding: 0 1.5rem; height: 3.5rem;
                 display: flex; align-items: center; gap: 1rem; }
    .brand { font-size: 1.125rem; font-weight: 600; letter-spacing: -0.01em; text-decoration: none; }
    .brand .two { color: #047857; }
    .nav-links { margin-left: auto; display: flex; align-items: center; gap: .25rem;
                 font-size: .875rem; color: #3f3f46; }
    .nav-links a { padding: .375rem .75rem; border-radius: .5rem; text-decoration: none;
                   transition: background-color .15s, color .15s; }
    .nav-links a:hover { background: #f4f4f5; color: #09090b; }
    .lang select { background: transparent; color: #52525b; border: 1px solid #d4d4d8;
                   border-radius: .5rem; padding: .25rem .5rem; font-size: .8rem; cursor: pointer; }

    .page-head { padding: 4rem 0 2.5rem; text-align: center; }
    .badge { display: inline-block; padding: .25rem .75rem; border-radius: 999px; font-size: .75rem;
             font-weight: 600; background: rgba(5,150,105,.1); color: #047857;
             border: 1px solid rgba(5,150,105,.25); text-transform: uppercase; letter-spacing: .05em; }
    .page-head h1 { font-size: clamp(1.6rem, 4vw, 2.4rem); font-weight: 800; margin: .75rem 0 .75rem;
                    line-height: 1.25; }
    .page-head p { color: #52525b; max-width: 42rem; margin: 0 auto; }

    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 1rem;
             margin: 2.5rem auto 0; }
    .stat { background: #fafafa; border: 1px solid #e4e4e7; border-radius: 1rem; padding: 1.2rem 1rem; }
    .stat-value { font-size: 1.7rem; font-weight: 800; }
    .stat-label { font-size: .75rem; color: #71717a; margin-top: .25rem; }

    section { padding: 2.5rem 0; }
    .chart-wrap { background: #fafafa; border: 1px solid #e4e4e7; border-radius: 1rem; padding: 1.5rem; }
    .chart-title { font-size: .75rem; font-weight: 700; color: #71717a; text-transform: uppercase;
                   letter-spacing: .05em; margin-bottom: 1.1rem; }
    .chart { display: flex; align-items: flex-end; gap: 3px; height: 100px; }
    .bar-col { flex: 1; min-width: 0; }
    .bar { width: 100%; border-radius: 3px 3px 0 0; background: #059669; min-height: 2px; }
    .bar.zero { background: #e4e4e7; }

    .version { margin-bottom: 2.75rem; }
    .version-head { border-left: 3px solid #059669; padding-left: 1rem; margin-bottom: 1.1rem; }
    .version-tag { font-size: .75rem; font-weight: 700; color: #059669; letter-spacing: .04em; }
    .version-head h2 { font-size: 1.15rem; font-weight: 700; margin: .2rem 0 0; }

    .entries { display: flex; flex-direction: column; gap: .5rem; }
    .entry { background: #fff; border: 1px solid #e4e4e7; border-radius: .75rem; padding: .8rem 1rem;
             display: grid; grid-template-columns: 76px 1fr auto; gap: 1rem; align-items: baseline; }
    .entry:hover { border-color: #a7f3d0; }
    .entry-date { font-size: .72rem; color: #a1a1aa; white-space: nowrap; }
    .entry-pr { display: block; font-weight: 700; color: #059669; text-decoration: none; }
    .entry-text { font-size: .875rem; }
    .entry-diff { font-size: .68rem; color: #a1a1aa; white-space: nowrap; }
    .entry-diff .add { color: #15803d; } .entry-diff .del { color: #b91c1c; }

    .note { margin-top: 2.5rem; padding: 1.4rem 1.5rem; background: #fafafa; border-radius: 1rem;
            font-size: .8rem; color: #52525b; }
    .note h3 { font-size: .75rem; font-weight: 700; color: #18181b; margin: 0 0 .6rem;
               text-transform: uppercase; letter-spacing: .05em; }
    .note ul { margin: 0; padding-left: 1.1rem; } .note li { margin-bottom: .35rem; }

    footer.site { border-top: 1px solid #e4e4e7; padding: 2rem 0; text-align: center;
                  font-size: .8rem; color: #71717a; }

    @media (max-width: 640px) {
      .entry { grid-template-columns: 1fr; gap: .35rem; }
      .entry-diff { display: none; }
    }
"""

STRINGS = {
    'ja': {
        'lang': 'ja', 'other': 'history.en.html', 'index': 'index.ja.html',
        'title': 'cli2ui — 開発の記録',
        'eyebrow': '開発の記録',
        'h1': 'ここまでに何をしてきたか',
        'lead': '全コミットを時系列で並べたもの。プルリクエストだけを見ると、main へ直接積んだ'
                '変更が抜け落ちる ── 日付・行数・件数は毎回 git から測り直しており、手で足していない。',
        'nav_home': 'トップ', 'nav_features': '機能', 'nav_history': '開発の記録',
        'stat_days': '開発日数', 'stat_commits': 'コミット', 'stat_releases': 'リリース',
        'chart_title': '週ごとのコミット数',
        'note_head': 'このページについて',
        'notes': [
            '単位はコミット。プルリクエストの一覧ではないので、main へ直接積んだ変更も漏れない。',
            '区切りはタグ（バージョン）が付いた地点。SyncVey の「フェーズ」のような主観的な'
            '区切りは付けていない ── 客観的に決まる境界だけを使っている。',
            '日英の文面は手作業の翻訳（コミットメッセージは英語期・日本語期のどちらか一方でしか'
            '書かれていない）。それ以外の数値はすべて生成時に git から計測している。',
            '翻訳がまだ無いコミットは、原文をそのまま両言語に出す。抜けにはならない。',
        ],
        'week_label': '{m}/{d}',
        'pr_link': '#{n}',
    },
    'en': {
        'lang': 'en', 'other': 'history.ja.html', 'index': 'index.en.html',
        'title': 'cli2ui — Development history',
        'eyebrow': 'Development history',
        'h1': 'What has actually been built so far',
        'lead': 'Every commit, in order. A pull-request-only list drops whatever landed by a direct '
                'push to main — dates, line counts and totals are re-measured from git on every '
                'build, never hand-maintained.',
        'nav_home': 'Home', 'nav_features': 'Features', 'nav_history': 'History',
        'stat_days': 'Days', 'stat_commits': 'Commits', 'stat_releases': 'Releases',
        'chart_title': 'Commits per week',
        'note_head': 'About this page',
        'notes': [
            'The unit is the commit, not the pull request — so a direct push to main is never dropped.',
            'Sections break at tagged versions only. There is no editorial "phase" grouping like '
            "SyncVey's page of the same name — only boundaries that are objectively decidable.",
            'The bilingual text is translated by hand (each commit message was written in one '
            'language, not both). Every other number on this page is measured from git at build time.',
            'A commit without a translation yet shows its original subject line on both pages — '
            'it is never simply missing.',
        ],
        'week_label': '{m}/{d}',
        'pr_link': '#{n}',
    },
}


def render(lang, data, groups):
    s = STRINGS[lang]
    idx = 0 if lang == 'ja' else 1
    commits = data['commits']
    days = (data['last'] - data['first']).days + 1
    n_releases = len(data['tags'])

    weeks = weekly_counts(commits, data['first'], data['last'])
    peak = max((count for _start, count in weeks), default=0) or 1

    out = []
    add = out.append

    add(f'<!DOCTYPE html>\n<html lang="{s["lang"]}">\n<head>')
    add('<meta charset="UTF-8" />')
    add('<meta name="viewport" content="width=device-width, initial-scale=1" />')
    add(f'<title>{e(s["title"])}</title>')
    add(f'<meta name="description" content="{e(s["lead"])}" />')
    add('<link rel="icon" href="data:image/svg+xml,'
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<text y='25' font-size='24' fill='%2334d399' font-family='monospace'>%E2%96%B8</text></svg>\" />")
    add('<script src="https://cdn.tailwindcss.com"></script>')
    add('<link rel="preconnect" href="https://fonts.googleapis.com">')
    add('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
    add('<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400..700&display=swap" '
        'rel="stylesheet">')
    add(f'<style>{STYLE}</style>')
    add('</head>\n<body>')

    add('<header class="site"><div class="nav-inner">')
    add(f'<a href="{s["index"]}" class="brand">cli<span class="two">2</span>ui</a>')
    add('<div class="nav-links">')
    add(f'<a href="{s["index"]}">{e(s["nav_home"])}</a>')
    add(f'<a href="{s["index"]}#features">{e(s["nav_features"])}</a>')
    add(f'<a href="history.{s["lang"]}.html">{e(s["nav_history"])}</a>')
    add(f'<a href="https://github.com/{REPO}">GitHub</a>')
    other_label = 'EN' if lang == 'ja' else 'JP'
    this_label = 'JP' if lang == 'ja' else 'EN'
    add('<span class="lang"><select onchange="location.href=this.value;">'
        f'<option value="history.{s["lang"]}.html" selected>{this_label}</option>'
        f'<option value="{s["other"]}">{other_label}</option></select></span>')
    add('</div></div></header>')

    add('<header class="page-head"><div class="container">')
    add(f'<span class="badge">{e(s["eyebrow"])}</span>')
    add(f'<h1>{e(s["h1"])}</h1>')
    add(f'<p>{e(s["lead"])}</p>')
    add('<div class="stats">')
    for value, label in ((days, s['stat_days']), (len(commits), s['stat_commits']),
                         (n_releases, s['stat_releases'])):
        add(f'<div class="stat"><div class="stat-value">{value}</div>'
            f'<div class="stat-label">{e(label)}</div></div>')
    add('</div></div></header>')

    add('<section><div class="container"><div class="chart-wrap">')
    add(f'<div class="chart-title">{e(s["chart_title"])}</div>')
    add('<div class="chart">')
    for _start, count in weeks:
        height = round(count / peak * 100)
        cls = 'bar zero' if count == 0 else 'bar'
        add(f'<div class="bar-col"><div class="{cls}" style="height:{height}%"></div></div>')
    add('</div></div></div></section>')

    add('<section><div class="container">')
    for ja_t, en_t, tag, listed in groups:
        title = (ja_t, en_t)[idx]
        lo, hi = listed[0]['date'], listed[-1]['date']
        span = f'{lo:%Y-%m-%d}' if lo == hi else f'{lo:%Y-%m-%d} – {hi:%Y-%m-%d}'
        anchor = tag or 'unreleased'
        add(f'<div class="version" id="{e(anchor)}"><div class="version-head">')
        add(f'<div class="version-tag">{e(span)}</div><h2>{e(title)}</h2>')
        add('</div><div class="entries">')
        for c in listed:
            text = (c['ja'], c['en'])[idx]
            add('<div class="entry">')
            add(f'<div class="entry-date">{c["date"]:%Y-%m-%d}')
            if c['pr']:
                add(f'<br><a class="entry-pr" href="https://github.com/{REPO}/pull/{c["pr"]}">'
                    f'{e(s["pr_link"].format(n=c["pr"]))}</a>')
            add('</div>')
            add(f'<div class="entry-text">{e(text)}</div>')
            add(f'<div class="entry-diff"><span class="add">+{c["added"]}</span> '
                f'<span class="del">-{c["removed"]}</span></div>')
            add('</div>')
        add('</div></div>')

    add(f'<div class="note"><h3>{e(s["note_head"])}</h3><ul>')
    for line in s['notes']:
        add(f'<li>{e(line)}</li>')
    add('</ul></div>')
    add('</div></section>')

    add('<footer class="site"><div class="container">'
        f'<span>cli<span class="two" style="color:#047857">2</span>ui &middot; '
        f'<a href="https://github.com/{REPO}">GitHub</a></span></div></footer>')
    add('</body>\n</html>')
    return '\n'.join(out) + '\n'


def main():
    data = collect()
    groups = group_by_version(data)

    for lang in ('ja', 'en'):
        path = REPO_ROOT / 'docs' / f'history.{lang}.html'
        path.write_text(render(lang, data, groups), encoding='utf-8')
        print(f'wrote {path.relative_to(REPO_ROOT)}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
