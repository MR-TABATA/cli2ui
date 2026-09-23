"""Japanese paraphrases (not literal translations) for the postgresql.conf
parameters shown by default in the settings panel (`COMMON_SETTINGS` in
core/engines/postgres.py).

`pg_settings.short_desc` has no localization — Postgres always returns
English regardless of client locale, so there's nothing to ask the database
for here. This is cli2ui's own small, hand-picked dictionary instead of a
general i18n mechanism: scoped to the ~19 parameters people actually see on
first opening 設定 (the "★ Commonly tuned" view), not all ~300+ pg_settings
rows across every category. Browsing into a specific category still shows
Postgres's own English short_desc unchanged — translating the long tail
isn't worth chasing across Postgres versions for settings almost nobody
opens this panel to look at.

Deliberately paraphrased, not translated word-for-word: each line says what
the knob does *for you*, the way an ops person would explain it out loud,
rather than mirroring Postgres's terse catalog wording.
"""

JA_DESCRIPTIONS = {
    "max_connections":
        "同時に張れるDB接続数の上限。増やすほどメモリも消費する。",
    "shared_buffers":
        "PostgreSQLが専有するキャッシュ用メモリ。既定値（128MB）は動作確認用の最小構成で、"
        "本番では搭載メモリの25%程度まで増やすのが目安。",
    "effective_cache_size":
        "OSのキャッシュも含めた「実際に使えそうなキャッシュ量」の見積り"
        "（メモリを確保するわけではなく、プランナがインデックスを使うか判断する材料）。"
        "搭載メモリの50〜75%程度を目安に設定する。",
    "work_mem":
        "ソートやハッシュ結合1回あたりに使えるメモリ。大きすぎると同時実行数ぶん重なって圧迫する。",
    "maintenance_work_mem":
        "VACUUMやインデックス作成など、保守系の操作に使うメモリ。work_memより大きめでよい。",
    "wal_buffers":
        "コミット前のWAL（更新ログ）を一時的に溜めておくメモリ。",
    "min_wal_size":
        "WALファイルをこれ以下には縮めない、という下限サイズ。",
    "max_wal_size":
        "これを超えるとチェックポイントが走る、WALサイズの目安上限。",
    "checkpoint_completion_target":
        "チェックポイントの書き込みを、次のチェックポイントまでの何割に引き延ばすか。"
        "大きくするほどI/Oの山がならされる。",
    "random_page_cost":
        "ランダムI/O1回のコスト見積り。HDD前提の既定値は4.0。SSD/NVMeなら"
        "1.1〜2.0程度まで下げると、インデックスが選ばれやすくなる。",
    "effective_io_concurrency":
        "ディスクが同時にさばけるI/O要求数の見積り。SSDなら大きめにしてよい。",
    "default_statistics_target":
        "ANALYZEで集める統計の精度。上げると実行計画の精度が上がる代わりにANALYZE自体が重くなる。",
    "log_min_duration_statement":
        "これより遅いクエリだけをログへ残す閾値（ミリ秒）。既定の-1（出力なし）のままだと"
        "スロークエリを検出できない。調査用には1000（1秒以上）や500（0.5秒以上）などを設定する。",
    "log_statement":
        "実行したSQL文をどこまでログへ残すか（none / ddl / mod / all）。",
    "log_connections":
        "接続が成立するたびにログへ残すか。",
    "log_disconnections":
        "切断のたびに、接続していた時間つきでログへ残すか。",
    "log_lock_waits":
        "ロック待ちが長引いたときにログへ残すか。",
    "idle_in_transaction_session_timeout":
        "トランザクションを開始したまま何もしない状態が続いたら、その接続を強制的に切るまでの時間。"
        "既定の0（無効）のままだと、コネクションプールやアプリのバグでロックが残り"
        "障害につながることがある。実運用では数分〜数十分を目安に設定する。",
    "statement_timeout":
        "1つのSQL文の実行に許す最大時間。超えると強制的に打ち切られる。",
    "timezone":
        "このセッションが日時を表示・解釈するときに使うタイムゾーン。",
}


def localize_description(name: str, fallback: str, language_code: str) -> str:
    """Swap in the Japanese paraphrase when the UI is set to Japanese and one
    exists for this parameter; otherwise return Postgres's own short_desc
    unchanged (covers English mode and every setting outside the dictionary)."""
    if language_code == "ja":
        return JA_DESCRIPTIONS.get(name, fallback)
    return fallback
