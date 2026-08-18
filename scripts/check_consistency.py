#!/usr/bin/env python3
"""LP・README・リリース・i18n の整合性を機械的に確かめる。

人の目では必ず漏れる（実際、公表値が 4 つ間違ったまま配られ、LP の OG タグには
古い数字がハードコードされたまま残っていた）。**リリースの前に必ず通す。**

    python3 scripts/check_consistency.py

**製品ごとの事情は `.consistency.json` に全部出してある。** このファイルは製品間で
そのままコピーして使う（MrEditor / MrkEditor / MR Down / MrkDown / cli2ui）。設定に
無い節は検査ごと飛ばすので、LP を持たない製品でも「版だけ」「i18n だけ」で回せる。
言語や作りの違いは節ごとの `mode` / `format` で選ぶ（Swift の .strings と Django の
gettext、単一ソースの日英併記と言語別ファイル、どちらも同じ節が見る）。

見るもの（設定にある節だけ走る）:
  1. versions       … バージョン文字列が全部そろっているか
  2. generated_site … 生成物が src から作り直された状態か（置き去りの検出）
  3. lang_parity    … 日英の片落ち（単一ソースの data-en/data-ja、または言語別ファイル）
  4. i18n           … キーと書式指定子の一致・未定義キーの使用・.mo の作り直し漏れ
  5. release_docs   … 公開したタグが文書に載っているか
  6. measured       … 公表値が文書間（とコード）で食い違っていないか
  7. optional_app   … 機能フラグで外せる層が、疎結合のまま保たれているか
"""

import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / ".consistency.json"
FAIL: list[str] = []
WARN: list[str] = []


def read(p: str) -> str:
    return (ROOT / p).read_text(encoding="utf-8")


def head(title: str) -> None:
    print(f"\n──── {title}")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"設定が無い: {CONFIG_PATH.name}", file=sys.stderr)
        sys.exit(2)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# 1. バージョン文字列 ---------------------------------------------------------

def check_versions(sources: list[dict]) -> None:
    head("バージョン文字列")
    versions = {}
    for src in sources:
        m = re.search(src["pattern"], read(src["path"]))
        if not m:
            FAIL.append(f"バージョンが見つからない: {src['name']}（{src['path']}）")
            continue
        versions[src["name"]] = m.group(1)
        print(f"  {src['name']:24s} {m.group(1)}")

    if len(set(versions.values())) > 1:
        FAIL.append(f"バージョンが食い違っている: {versions}")
    elif versions:
        print("  → すべて一致 ✅")


# 2. 生成物のドリフト ---------------------------------------------------------

def check_site_drift(cfg: dict) -> None:
    """再生成して**中身が変わるか**で見る。

    git の差分で見てはいけない（リリースでバージョンを上げた直後は必ず差分が出るので、
    毎回誤検知する。実際に誤検知した）。
    """
    out_dir, glob, builder = cfg["dir"], cfg.get("glob", "*.html"), cfg["builder"]
    head(f"{out_dir}/ が {Path(builder).name} から再生成された状態か")

    def digest() -> dict[str, str]:
        return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
                for f in sorted((ROOT / out_dir).glob(glob))}

    before = digest()
    subprocess.run([sys.executable, builder], cwd=ROOT, check=True, capture_output=True)
    after = digest()

    changed = [k for k in after if before.get(k) != after[k]]
    if changed:
        FAIL.append(f"{out_dir}/ が古い（{builder} を通していない）: {', '.join(changed)}")
    else:
        print("  ドリフト無し ✅")


# 3. 日英パリティ -------------------------------------------------------------

class LangCheck(HTMLParser):
    """単一ソースに日英を併記する作り（data-en / data-ja）の片落ちを見る。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.pairs = 0
        self.bad: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        en, ja = "data-en" in d, "data-ja" in d
        if en or ja:
            self.pairs += 1
            if not (en and ja):
                missing = "data-ja" if en else "data-en"
                text = (d.get("data-en") or d.get("data-ja") or "")[:40]
                self.bad.append(f"<{tag}> に {missing} が無い: {text}")

    handle_startendtag = handle_starttag


class Shape(HTMLParser):
    """言語別ファイルを突き合わせるための「骨組み」を数える。

    訳文そのものは比べられないので、**構造**だけを見る。片方の言語にだけ節や箇条を
    足した（＝もう片方の読者には存在しない）のを見つけるのが目的。
    """

    def __init__(self, tags: list[str]) -> None:
        super().__init__(convert_charrefs=False)
        self.tags = {t.lower() for t in tags}
        self.counts: dict[str, int] = {t: 0 for t in self.tags}
        self.ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag in self.tags:
            self.counts[tag] += 1
        got = dict(attrs).get("id")
        if got:
            self.ids.add(got)

    handle_startendtag = handle_starttag


def check_lang_parity(cfg) -> None:
    """設定が配列なら単一ソース（data-en/data-ja）、辞書なら言語別ファイルの突き合わせ。"""
    head("日英パリティ")
    before = len(FAIL)

    if isinstance(cfg, list):                       # 単一ソースに併記する作り
        for src in cfg:
            c = LangCheck()
            c.feed(read(src))
            print(f"  {src}: 日英対を持つ要素 {c.pairs}")
            FAIL.extend(f"{src}: {b}" for b in c.bad)
        if len(FAIL) == before:
            print("  片落ち無し ✅")
        return

    tags = cfg.get("count_tags", ["section", "h2", "h3", "li"])
    for a, b in cfg["pairs"]:
        sa, sb = Shape(tags), Shape(tags)
        sa.feed(read(a))
        sb.feed(read(b))
        print(f"  {a} ↔ {b}: " + " ".join(f"{t}={sa.counts[t]}" for t in sorted(tags)))

        for t in sorted(tags):
            if sa.counts[t] != sb.counts[t]:
                FAIL.append(f"{a} と {b} で <{t}> の数が違う"
                            f"（片方の言語にだけ書いた？）: {sa.counts[t]} 対 {sb.counts[t]}")
        if cfg.get("compare_ids", True):
            for only, where in ((sa.ids - sb.ids, b), (sb.ids - sa.ids, a)):
                for i in sorted(only):
                    FAIL.append(f'{where} に id="{i}" が無い（節が片落ち／リンク切れ）')

    if len(FAIL) == before:
        print("  片落ち無し ✅")


# 4. i18n --------------------------------------------------------------------

def check_i18n(cfg: dict) -> None:
    if cfg.get("format", "strings") == "gettext":
        check_i18n_gettext(cfg)
    else:
        check_i18n_strings(cfg)


def check_i18n_strings(cfg: dict) -> None:
    """Apple の .strings（"key" = "value";）を突き合わせる。"""
    head("i18n のキー")
    langs = cfg.get("langs", ["ja", "en"])
    tables = {lang: {m.group(1): m.group(2)
                     for m in re.finditer(r'^"([^"]+)"\s*=\s*"(.*)";',
                                          read(cfg["table"].format(lang=lang)), re.M)}
              for lang in langs}
    print("  " + " / ".join(f"{lang}: {len(t)}" for lang, t in tables.items()))

    base = langs[0]
    for lang in langs[1:]:
        for miss in sorted(set(tables[base]) - set(tables[lang])):
            FAIL.append(f"{lang} に無いキー: {miss}")
        for miss in sorted(set(tables[lang]) - set(tables[base])):
            FAIL.append(f"{base} に無いキー: {miss}")

        # 書式指定子の数が食い違うと、実行時に落ちる
        for k in sorted(set(tables[base]) & set(tables[lang])):
            fb = re.findall(r'%[@dfs]|%\d\$[@dfs]', tables[base][k])
            fl = re.findall(r'%[@dfs]|%\d\$[@dfs]', tables[lang][k])
            if len(fb) != len(fl):
                FAIL.append(f"書式指定子の数が違う（実行時に落ちる）: {k}  {base}={fb} {lang}={fl}")

    # コードが使うキーが実在するか。動的キー（L("a.\(x)")）は補間を含むので除外する。
    used: set[str] = set()
    for f in (ROOT / cfg["code_dir"]).rglob(cfg.get("code_glob", "*.swift")):
        used |= set(re.findall(cfg["call"], f.read_text()))
    static_used = {k for k in used if "\\(" not in k}
    for k in sorted(static_used - set(tables[base])):
        FAIL.append(f"未定義のキーを使っている（画面にキー名が出る）: {k}")

    # 未使用の疑い（変数経由 L(key) で使う分は検出できないので警告どまり）
    dynamic_prefixes = {k.split("\\(")[0] for k in used if "\\(" in k}
    suspicious = {k for k in set(tables[base]) - static_used
                  if not any(k.startswith(p) for p in dynamic_prefixes)}
    if suspicious:
        WARN.append(f"未使用の疑いがあるキー {len(suspicious)} 件（変数経由なら問題なし）")

    print("  キー・書式指定子とも一致 ✅")


# gettext の書式指定子。名前付き・波括弧は「同じ集合か」、位置指定子は「同じ数か」を見る。
_NAMED = re.compile(r'%\([^)]*\)[sdfrgi]')
_BRACE = re.compile(r'\{[^{}]*\}')
_POSIT = re.compile(r'%(?!\()[-#0-9.+ ]*[sdfrgi%]')


def _placeholders(s: str) -> tuple[set[str], set[str], int]:
    return (set(_NAMED.findall(s)), set(_BRACE.findall(s)),
            len([p for p in _POSIT.findall(s) if not p.endswith("%")]))


def _catalog(entries) -> dict[str, tuple]:
    """msgid → 訳文（複数形はまとめて 1 つの値に）。po と mo を同じ形で比べるため。"""
    out = {}
    for e in entries:
        key = e.msgid if not e.msgid_plural else f"{e.msgid}\x00{e.msgid_plural}"
        out[key] = (e.msgstr, tuple(v for _, v in sorted(e.msgstr_plural.items())))
    return out


def check_i18n_gettext(cfg: dict) -> None:
    """Django の gettext カタログ（.po と、**コミット済みの .mo**）を見る。

    .po と .mo を両方コミットする運用なので、`compilemessages` を忘れると「訳したのに
    画面は英語」が起きる。git の差分では気づけない（.mo は読めない）ので、.po から
    作った表と .mo の中身を突き合わせる。
    """
    head("i18n（gettext のカタログ）")
    try:
        import polib
    except ImportError:
        print("  polib が無いため飛ばす（pip install -r requirements-dev.txt）")
        return

    for lang in cfg.get("langs", ["ja"]):
        po_path, mo_path = cfg["catalog"].format(lang=lang), cfg["compiled"].format(lang=lang)
        po = polib.pofile(str(ROOT / po_path))
        print(f"  {lang}: {len(po)} 件（訳あり {len(po.translated_entries())}）")

        # fuzzy は gettext が「訳が無い」として扱うので、画面には原文が出る
        for e in po.fuzzy_entries():
            FAIL.append(f"{lang}: fuzzy のまま（画面には原文が出る）: {e.msgid[:50]}")

        if po.untranslated_entries():
            WARN.append(f"{lang}: 未訳 {len(po.untranslated_entries())} 件（画面には原文が出る）")
        if po.obsolete_entries():
            WARN.append(f"{lang}: 使われていない訳 {len(po.obsolete_entries())} 件"
                        f"（makemessages の掃除漏れ）")

        # 書式指定子の食い違いは、その画面を開いた瞬間に落ちる
        for e in po.translated_entries():
            pairs = ([("", e.msgid, e.msgstr)] if not e.msgid_plural else
                     [(f"[{k}]", e.msgid if k == 0 else e.msgid_plural, v)
                      for k, v in sorted(e.msgstr_plural.items())])
            for label, src, msgstr in pairs:
                ns, bs, ps = _placeholders(src)
                nt, bt, pt = _placeholders(msgstr)
                if ns != nt or bs != bt or ps != pt:
                    FAIL.append(f"{lang}: 書式指定子が原文と違う（表示時に落ちる）"
                                f"{label}: {e.msgid[:40]}  原文={sorted(ns | bs)}+{ps} "
                                f"訳={sorted(nt | bt)}+{pt}")

        if not (ROOT / mo_path).exists():
            FAIL.append(f"{lang}: {mo_path} が無い（compilemessages を通していない）")
            continue
        po_cat = _catalog(po.translated_entries())
        mo_cat = _catalog(polib.mofile(str(ROOT / mo_path)))
        stale = [k for k in po_cat if mo_cat.get(k) != po_cat[k]]
        if stale:
            FAIL.append(f"{lang}: .mo が .po より古い（compilemessages を通していない）"
                        f" — 反映されていない訳 {len(stale)} 件: {stale[0][:40]}")
        else:
            print(f"  {lang}: .mo は .po と一致 ✅")


# 5. 出したものが文書に載っているか --------------------------------------------

def published_versions(prefix: str) -> list[str]:
    """公開済みのタグ（gh が使えないときは空＝この検査を飛ばす）。"""
    try:
        out = subprocess.run(["gh", "release", "list", "--limit", "100", "--json", "tagName",
                              "--jq", ".[].tagName"], capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [t.strip().removeprefix(prefix) for t in out.stdout.splitlines() if t.strip()]


def check_release_coverage(cfg: dict) -> None:
    """**出したのに書いていない版**を見つける。

    リリースのたびに複数の文書へ手で足しており、どれか 1 つを忘れても誰も気づかない。
    タグを正として突き合わせる。
    """
    head("公開した版が文書に載っているか")
    tags = published_versions(cfg.get("tag_prefix", "v"))
    if not tags:
        print("  gh が使えないため飛ばす")
        return

    targets: list[tuple[str, dict, str | None]] = []
    if "history" in cfg:
        targets.append((cfg["history"]["path"], cfg["history"], cfg["history"].get("skip_prefix")))
    targets += [(r["path"], r, r.get("skip_prefix")) for r in cfg.get("roadmaps", [])]

    ok = True
    for label, spec, skip in targets:
        listed = set(re.findall(spec["pattern"], read(spec["path"]), re.M))
        want = [t for t in tags if not (skip and t.startswith(skip))]
        missing = [t for t in want if t not in listed]
        print(f"  {label}: {len(listed)} 件（対象タグ {len(want)}）")
        if missing:
            FAIL.append(f"{label} に載っていない版: {', '.join(sorted(missing))}")
            ok = False
    if ok:
        print("  全部載っている ✅")


# 6. 公表値の食い違い ---------------------------------------------------------

def check_measured_numbers(measured: dict, default_docs: list[str]) -> None:
    """**測り直した数字の書き忘れ**を見つける（同じ指標に別の値が残っていないか）。

    `family` で候補を拾い、`canonical` に合わない値を落とす。項目ごとに `docs` を
    与えれば、文書だけでなく**コードの定数**も同じやり方で押さえられる（LP に「2 秒で
    諦める」と書いたまま、コード側の既定だけ変える事故を止める）。
    `family` に捕獲グループを書かないこと（findall が壊れる。要るなら `(?:…)`）。
    """
    head("公表値の食い違い")
    cache: dict[str, str] = {}
    for label, spec in measured.items():
        if label.startswith("_"):
            continue                        # 設定ファイル内のコメント行
        bad: list[str] = []
        for doc in spec.get("docs", default_docs):
            text = cache.setdefault(doc, read(doc))
            for hit in set(re.findall(spec["family"], text)):
                if not re.fullmatch(spec["canonical"], hit):
                    bad.append(f"{doc}:{hit}")
        if bad:
            FAIL.append(f"{label} に別の値がある: {', '.join(sorted(bad))}")
        else:
            print(f"  {label} ✅")


# 7. 外せる層の分離 ----------------------------------------------------------

def check_optional_app(cfg: dict) -> None:
    """**機能フラグで外せる層が、外しても動く形のまま保たれているか。**

    分離は書いた日には成立していても、あとから足したコードが直に import した時点で
    黙って壊れる（フラグを外した構成でしか落ちないので、手元では気づけない）。

    見るもの:
      a. 本体側が任意アプリを直 import していないこと（外すと ImportError で落ちる）
      b. 任意アプリに触るテンプレートが、必ずフラグ越しであること
      c. 入口が「入っていれば繋ぐ」の口を通っていること
    """
    app = cfg["app"]
    head(f"外せる層の分離（{app}）")
    before = len(FAIL)
    gate_files = {ROOT / g for g in cfg.get("gate_files", [])}

    imports = re.compile(rf'^\s*(?:from|import)\s+{re.escape(app)}\b', re.M)
    scanned = 0
    for d in cfg.get("code_dirs", []):
        for f in sorted((ROOT / d).rglob("*.py")):
            if f in gate_files or app in f.parts:
                continue                    # 継ぎ目そのものと、任意アプリ自身は対象外
            scanned += 1
            if imports.search(f.read_text(encoding="utf-8")):
                FAIL.append(f"{f.relative_to(ROOT)}: {app} を直 import している"
                            f"（フラグを外した構成で落ちる）")
    print(f"  本体側 {scanned} ファイルに直 import なし")

    gate = cfg.get("template_gate")
    if gate:
        touched = 0
        for d in cfg.get("template_dirs", []):
            for f in sorted((ROOT / d).rglob("*.html")):
                src = f.read_text(encoding="utf-8")
                if app not in src:
                    continue
                touched += 1
                if gate not in src:
                    FAIL.append(f"{f.relative_to(ROOT)}: {app} に触るのにフラグ"
                                f"（{gate}）を通していない")
        print(f"  {app} に触るテンプレート {touched} 件はフラグ越し")

    for g in cfg.get("gate_files", []):
        if cfg["install_gate"] not in read(g):
            FAIL.append(f"{g}: 入口が想定の口（{cfg['install_gate']}）を通っていない")

    if len(FAIL) == before:
        print("  分離は保たれている ✅")


def main() -> int:
    cfg = load_config()
    print(f"整合性チェック: {cfg.get('product', ROOT.name)}")

    if cfg.get("versions"):       check_versions(cfg["versions"])
    if cfg.get("generated_site"): check_site_drift(cfg["generated_site"])
    if cfg.get("lang_parity"):    check_lang_parity(cfg["lang_parity"])
    if cfg.get("i18n"):           check_i18n(cfg["i18n"])
    if cfg.get("release_docs"):   check_release_coverage(cfg["release_docs"])
    if cfg.get("measured"):       check_measured_numbers(cfg["measured"], cfg.get("docs", []))
    if cfg.get("optional_app"):   check_optional_app(cfg["optional_app"])

    print()
    for w in WARN:
        print(f"  ⚠️  {w}")
    if FAIL:
        print(f"\n❌ {len(FAIL)} 件の不整合:")
        for f in FAIL:
            print(f"   - {f}")
        return 1
    print("\n✅ 整合性 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
