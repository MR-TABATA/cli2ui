#!/usr/bin/env python3
"""配布まわりの数字を1コマンドで出す。

    python3 scripts/metrics.py report      # 今の数字を表示する
    python3 scripts/metrics.py snapshot    # 非公開 Gist に今日の分を追記（CI 用）

数えているもの:

  * **Docker Hub の pull 数** — 公開 API・認証不要。`git clone` しか配布経路が
    無かった間、GitHub が数える「ダウンロード」は構造的にゼロだった。イメージを
    出して初めて本物の DL 数が立ち上がる。
  * **リリースアセットの download_count** — アセットを添付したリリースのみ。
    自動生成の Source code (zip/tar.gz) は GitHub が数えない。
  * **traffic の clone / view** — GitHub は**直近14日しか持たない**。毎日
    Gist に落とし込むのはそのため。取り逃がした日は永久に戻らない。

環境変数:
    METRICS_TOKEN / GITHUB_TOKEN   GitHub API 用（未設定なら `gh auth token`）
    GIST_ID                        snapshot の保存先（非公開 Gist の ID）
    DOCKERHUB_NAMESPACE            Docker Hub のユーザー名（既定 'jiniie'）
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

REPO = os.getenv('METRICS_REPO', 'MR-TABATA/cli2ui')
DOCKER_NAMESPACE = os.getenv('DOCKERHUB_NAMESPACE', 'jiniie')
DOCKER_IMAGE = os.getenv('DOCKERHUB_IMAGE', 'cli2ui')
GIST_FILENAME = 'cli2ui-metrics.json'


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _token() -> str:
    for var in ('METRICS_TOKEN', 'GITHUB_TOKEN', 'GH_TOKEN'):
        if os.getenv(var):
            return os.environ[var]
    try:
        return subprocess.check_output(['gh', 'auth', 'token'], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit('GitHub のトークンが無い。METRICS_TOKEN を設定するか `gh auth login` を実行する。')


def _get(url: str, token: str | None = None, method: str = 'GET', body=None):
    """JSON を返す。404 は None（まだ公開していないイメージ等）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Accept', 'application/vnd.github+json')
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    if data:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b'null')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# ---------------------------------------------------------------------------
# 収集
# ---------------------------------------------------------------------------

def docker_pulls() -> dict | None:
    """Docker Hub の pull 数。未公開／名前未設定なら None。"""
    if not DOCKER_NAMESPACE:
        return None
    url = f'https://hub.docker.com/v2/repositories/{DOCKER_NAMESPACE}/{DOCKER_IMAGE}/'
    info = _get(url)
    if not info:
        return None
    return {'pulls': info.get('pull_count', 0),
            'stars': info.get('star_count', 0),
            'last_updated': (info.get('last_updated') or '')[:10]}


def release_downloads(token: str) -> list:
    releases = _get(f'https://api.github.com/repos/{REPO}/releases', token) or []
    out = []
    for rel in releases:
        assets = [{'name': a['name'], 'downloads': a['download_count']}
                  for a in rel.get('assets', [])]
        out.append({'tag': rel['tag_name'],
                    'published': (rel.get('published_at') or '')[:10],
                    'assets': assets,
                    'downloads': sum(a['downloads'] for a in assets)})
    return out


def traffic(token: str) -> dict:
    """直近14日の clone / view。push 権限が要る。"""
    clones = _get(f'https://api.github.com/repos/{REPO}/traffic/clones', token) or {}
    views = _get(f'https://api.github.com/repos/{REPO}/traffic/views', token) or {}
    days: dict[str, dict] = {}
    for row in clones.get('clones', []):
        days.setdefault(row['timestamp'][:10], {}).update(
            clones=row['count'], clone_uniques=row['uniques'])
    for row in views.get('views', []):
        days.setdefault(row['timestamp'][:10], {}).update(
            views=row['count'], view_uniques=row['uniques'])
    return {'days': days,
            'clones_14d': clones.get('count', 0),
            'clone_uniques_14d': clones.get('uniques', 0),
            'views_14d': views.get('count', 0),
            'view_uniques_14d': views.get('uniques', 0)}


# ---------------------------------------------------------------------------
# Gist（履歴の置き場）
# ---------------------------------------------------------------------------

def _gist_load(gist_id: str, token: str) -> dict:
    gist = _get(f'https://api.github.com/gists/{gist_id}', token)
    if not gist:
        sys.exit(f'Gist {gist_id} が見つからない。GIST_ID とトークンの scope を確認する。')
    files = gist.get('files') or {}
    blob = files.get(GIST_FILENAME)
    if not blob:
        return {'repo': REPO, 'days': {}, 'totals': {}}
    if blob.get('truncated'):
        content = urllib.request.urlopen(blob['raw_url'], timeout=30).read().decode()
    else:
        content = blob['content']
    return json.loads(content or '{}') or {'repo': REPO, 'days': {}, 'totals': {}}


def _gist_save(gist_id: str, token: str, payload: dict) -> None:
    body = {'files': {GIST_FILENAME: {'content': json.dumps(payload, indent=2,
                                                            ensure_ascii=False)}}}
    _get(f'https://api.github.com/gists/{gist_id}', token, method='PATCH', body=body)


def snapshot() -> None:
    """今日ぶんを取り込んで Gist を更新する。同じ日は新しい値で上書き。"""
    gist_id = os.getenv('GIST_ID')
    if not gist_id:
        sys.exit('GIST_ID が未設定。')
    token = _token()

    store = _gist_load(gist_id, token)
    store.setdefault('repo', REPO)
    store.setdefault('days', {})
    store.setdefault('totals', {})

    t = traffic(token)
    # 当日の値は日中に増える。取り直したら常に新しい方で置き換える。
    for day, row in t['days'].items():
        store['days'][day] = {**store['days'].get(day, {}), **row}

    today = date.today().isoformat()
    pulls = docker_pulls()
    releases = release_downloads(token)
    store['totals'][today] = {
        'docker_pulls': (pulls or {}).get('pulls'),
        'release_downloads': sum(r['downloads'] for r in releases),
        'releases': {r['tag']: r['downloads'] for r in releases},
    }
    store['updated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    _gist_save(gist_id, token, store)

    print(f"snapshot ok — {len(store['days'])} 日分, 最新 {today}")
    print(f"  clone(14d): {t['clones_14d']} / unique {t['clone_uniques_14d']}")
    print(f"  docker pulls: {(pulls or {}).get('pulls', '未公開')}")


# ---------------------------------------------------------------------------
# 表示
# ---------------------------------------------------------------------------

def report() -> None:
    token = _token()
    print(f'== {REPO} ==\n')

    pulls = docker_pulls()
    print('-- Docker Hub --')
    if pulls:
        print(f"  pull 数     : {pulls['pulls']}")
        print(f"  star        : {pulls['stars']}")
        print(f"  最終更新    : {pulls['last_updated']}")
    elif DOCKER_NAMESPACE:
        print(f'  {DOCKER_NAMESPACE}/{DOCKER_IMAGE} はまだ Docker Hub に無い'
              '（v* タグを push すると公開される）')
    else:
        print('  DOCKERHUB_NAMESPACE 未設定 — Docker Hub のユーザー名を入れると pull 数を出す')

    print('\n-- リリースアセット --')
    releases = release_downloads(token)
    if not releases:
        print('  リリースなし')
    for rel in releases:
        if rel['assets']:
            print(f"  {rel['tag']} ({rel['published']}): {rel['downloads']} DL")
            for a in rel['assets']:
                print(f"      {a['name']}: {a['downloads']}")
        else:
            print(f"  {rel['tag']} ({rel['published']}): アセット無し "
                  f"— 自動生成の Source code は GitHub が数えない")

    print('\n-- GitHub traffic（直近14日・ここだけ消える）--')
    t = traffic(token)
    print(f"  clone  : {t['clones_14d']} (unique {t['clone_uniques_14d']})")
    print(f"  view   : {t['views_14d']} (unique {t['view_uniques_14d']})")

    gist_id = os.getenv('GIST_ID')
    if gist_id:
        store = _gist_load(gist_id, token)
        days = store.get('days', {})
        if days:
            first, last = min(days), max(days)
            tot_c = sum(d.get('clones', 0) for d in days.values())
            tot_u = sum(d.get('clone_uniques', 0) for d in days.values())
            print(f"\n-- 蓄積した履歴（Gist）--")
            print(f"  期間     : {first} 〜 {last}（{len(days)} 日）")
            print(f"  clone累計: {tot_c}（日ごと unique の合計 {tot_u}）")
            print("  ※ unique は日単位。別の日に来た同じ人は二重に数える。")
    else:
        print('\n（GIST_ID を設定すると、蓄積した履歴も併せて表示する）')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=('report', 'snapshot'))
    args = ap.parse_args()
    (report if args.command == 'report' else snapshot)()


if __name__ == '__main__':
    main()
