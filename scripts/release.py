#!/usr/bin/env python3
"""リリースの「準備」を 1 コマンドで行う。**push もタグも絶対にしない。**

    .venv/bin/python scripts/release.py              # 次の版の候補を出すだけ(何も変えない)
    .venv/bin/python scripts/release.py 1.11.0       # 準備する
    .venv/bin/python scripts/release.py 1.11.0 --dry-run   # 何をするか表示するだけ

やること(この順):
  1. 前提の確認  … main にいる / 作業ツリーがクリーン / 版が新しく未使用 /
                    CHANGELOG の [Unreleased] が空でない
  2. 検証        … テスト → verify_hosted.sh(まだ何も変えていない main の状態で)
  3. ブランチ    … release/vX.Y.Z を作る
  4. 版の更新    … .consistency.json の versions に載る全箇所 + CHANGELOG の見出しと
                    末尾のリンク([Unreleased] → [X.Y.Z] - 日付)
  5. 整合性      … scripts/check_consistency.py(版が全部そろったか)
  途中で落ちたら、変えたものを元に戻して main に戻る。

やらないこと:
  * コミット … 差分を見てから、頼んでコミットする(このリポジトリの運用)。
  * push / タグ … `v*` タグの push は Docker Hub へのイメージ公開を起動する。
    最後に手で打つコマンドを表示するだけ。
"""
import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / ".consistency.json").read_text(encoding="utf-8"))
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
CHANGELOG = ROOT / "CHANGELOG.md"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class Abort(Exception):
    """準備を続けられない。メッセージは人に見せる。"""


def sh(*cmd, check=True, capture=True):
    r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture)
    if check and r.returncode != 0:
        raise Abort(f"コマンドが失敗: {' '.join(cmd)}\n{(r.stdout or '') + (r.stderr or '')}".rstrip())
    return r


def parse(v: str) -> tuple[int, int, int]:
    return tuple(int(x) for x in v.split("."))  # type: ignore[return-value]


# --- CHANGELOG ---------------------------------------------------------------

def unreleased_body(text: str) -> str:
    m = re.search(r"^## \[Unreleased\]\s*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    if not m:
        raise Abort("CHANGELOG.md に `## [Unreleased]` が無い。")
    return m.group(1).strip()


def suggest(text: str, current: str) -> tuple[str, str]:
    """[Unreleased] の中身から次の版を提案する(1.x は機能追加で minor を上げる規約)。"""
    body = unreleased_body(text)
    major, minor, patch = parse(current)
    if re.search(r"^### (Removed)|BREAKING", body, re.M):
        return f"{major + 1}.0.0", "互換性を壊す変更(Removed / BREAKING)がある"
    if re.search(r"^### (Added|Changed)", body, re.M):
        return f"{major}.{minor + 1}.0", "Added / Changed がある(機能追加は minor)"
    return f"{major}.{minor}.{patch + 1}", "Fixed だけ(patch)"


def apply_changelog(text: str, version: str, prev: str, date: str) -> str:
    if not unreleased_body(text):
        raise Abort("CHANGELOG の [Unreleased] が空。書いてから出す(機能ごとに都度書く運用)。")
    text = re.sub(r"^## \[Unreleased\]\s*\n", f"## [Unreleased]\n\n## [{version}] - {date}\n\n",
                  text, count=1, flags=re.M)
    link = re.search(r"^\[Unreleased\]: (\S+?)/compare/v[\d.]+\.\.\.HEAD$", text, re.M)
    if not link:
        raise Abort("CHANGELOG 末尾に `[Unreleased]: …/compare/vX...HEAD` のリンクが無い。")
    base = link.group(1)
    new_links = (f"[Unreleased]: {base}/compare/v{version}...HEAD\n"
                 f"[{version}]: {base}/compare/v{prev}...v{version}")
    return text.replace(link.group(0), new_links, 1)


# --- version sources ---------------------------------------------------------

def sources():
    """CHANGELOG 以外の版の書き場所(.consistency.json が正)。"""
    return [s for s in CONFIG["versions"] if Path(s["path"]).name != "CHANGELOG.md"]


def current_version() -> str:
    first = CONFIG["versions"][0]
    m = re.search(first["pattern"], (ROOT / first["path"]).read_text(encoding="utf-8"))
    if not m:
        raise Abort(f"{first['path']} から現在の版を読めない。")
    return m.group(1)


def bump_sources(version: str) -> list[str]:
    changed = []
    for s in sources():
        p = ROOT / s["path"]
        text = p.read_text(encoding="utf-8")
        m = re.search(s["pattern"], text)
        if not m:
            raise Abort(f"{s['path']} に版のパターンが無い: {s['pattern']}")
        new = text[:m.start(1)] + version + text[m.end(1):]
        p.write_text(new, encoding="utf-8")
        changed.append(s["path"])
    return changed


# --- steps -------------------------------------------------------------------

def preflight(version: str, current: str) -> None:
    if not SEMVER.match(version):
        raise Abort(f"版は X.Y.Z の形で: {version!r}")
    if parse(version) <= parse(current):
        raise Abort(f"新しい版は現在({current})より大きく: {version}")
    branch = sh("git", "branch", "--show-current").stdout.strip()
    if branch != "main":
        raise Abort(f"main で実行する(今は {branch!r})。")
    if sh("git", "status", "--porcelain").stdout.strip():
        raise Abort("作業ツリーがクリーンでない。コミットするか片付けてから。")
    if sh("git", "tag", "-l", f"v{version}").stdout.strip():
        raise Abort(f"タグ v{version} は既にある。")
    if sh("git", "branch", "--list", f"release/v{version}").stdout.strip():
        raise Abort(f"ブランチ release/v{version} は既にある。")
    behind = sh("git", "rev-list", "--count", "HEAD..origin/main", check=False)
    if behind.returncode == 0 and behind.stdout.strip() not in ("", "0"):
        raise Abort(f"origin/main が {behind.stdout.strip()} コミット先行している(取り込んでから)。")
    if not unreleased_body(CHANGELOG.read_text(encoding="utf-8")):
        raise Abort("CHANGELOG の [Unreleased] が空。書いてから出す(機能ごとに都度書く運用)。")


def verify(skip: bool) -> None:
    print("\n── 検証(変更前の main で)")
    steps = [("テスト", [PY, "manage.py", "test"])]
    script = ROOT / "scripts/verify_hosted.sh"
    if script.exists():
        steps.append(("hosted 実サーバ検証", [str(script)]))
    for label, cmd in steps:
        if skip:
            print(f"  ⏭  {label}(--skip-verify)")
            continue
        r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
        lines = (r.stdout + r.stderr).strip().splitlines()
        if r.returncode != 0:
            raise Abort(f"{label} が失敗:\n  " + "\n  ".join(lines[-6:]))
        # 結果の行(「Ran N tests」「OK (…)」「結果: …」)だけを拾って見せる
        result = [ln for ln in lines if re.match(r"(Ran \d+ tests|OK\b|結果:)", ln)]
        print(f"  ✅ {label}: {' / '.join(result) or 'OK'}")


def prepare(version: str, date: str, skip_verify: bool) -> None:
    current = current_version()
    preflight(version, current)
    verify(skip_verify)

    print(f"\n── 準備: {current} → {version}")
    sh("git", "switch", "-c", f"release/v{version}")
    try:
        changed = bump_sources(version)
        text = CHANGELOG.read_text(encoding="utf-8")
        CHANGELOG.write_text(apply_changelog(text, version, current, date), encoding="utf-8")
        changed.append("CHANGELOG.md")
        for f in changed:
            print(f"  ✏️  {f}")
        print("\n── 整合性")
        r = subprocess.run([PY, "scripts/check_consistency.py"], cwd=ROOT, text=True, capture_output=True)
        if r.returncode != 0:
            raise Abort("整合性チェックが失敗:\n" + "\n".join((r.stdout + r.stderr).strip().splitlines()[-12:]))
        print("  ✅ 整合性 OK")
    except BaseException:
        print("\n巻き戻す(変更を捨てて main に戻る)…", file=sys.stderr)
        sh("git", "checkout", "--", ".", check=False)
        sh("git", "switch", "main", check=False)
        sh("git", "branch", "-D", f"release/v{version}", check=False)
        raise


def next_steps(version: str) -> str:
    return f"""
準備できた。ブランチ release/v{version} に未コミットの差分がある。

  1. 差分を確認:            git diff
  2. コミットを頼む        (このリポジトリの運用: 確認してから)
  3. main に取り込む:       git switch main && git merge --ff-only release/v{version}
  4. ★ここから先は手で(このスクリプトはやらない)
        git tag -a v{version} -m "v{version}"
        git push origin main v{version}
     ※ `v*` タグの push は Docker Hub へのイメージ公開を起動する。
     ※ main の push は履歴ページの自動生成コミットも起動する。
  5. 作業ブランチを消す:    git branch -d release/v{version}
"""


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # 標準出力とエラーの表示順を保つ
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", nargs="?", help="新しい版(X.Y.Z)。省略すると候補を出すだけ")
    ap.add_argument("--dry-run", action="store_true", help="前提の確認と予定の表示だけ。何も変えない")
    ap.add_argument("--skip-verify", action="store_true", help="テストと hosted 検証を飛ばす(急ぎ用)")
    ap.add_argument("--date", default=datetime.date.today().isoformat(), help="CHANGELOG の日付(既定: 今日)")
    args = ap.parse_args()
    try:
        current = current_version()
        text = CHANGELOG.read_text(encoding="utf-8")
        if not args.version:
            print(f"現在の版: {current}")
            if not unreleased_body(text):
                print("[Unreleased] が空 — 出すものが無い。")
                return 0
            v, why = suggest(text, current)
            print(f"[Unreleased] の中身から: {v}  ({why})\n\n  {PY} scripts/release.py {v}")
            return 0
        if args.dry_run:
            preflight(args.version, current)
            print(f"前提 OK。{current} → {args.version} ({args.date})")
            print("更新する場所: " + ", ".join([s["path"] for s in sources()] + ["CHANGELOG.md"]))
            print("実行は: --dry-run を外す。 push とタグは行わない。")
            return 0
        prepare(args.version, args.date, args.skip_verify)
        print(next_steps(args.version))
        return 0
    except Abort as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
