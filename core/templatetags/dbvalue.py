"""1 マスぶんの値を、**データベースが返したまま**出すための filter。

Django のテンプレートは、描画するときに aware な datetime を
`settings.TIME_ZONE` へ変換し、さらに現在のロケールの書式へ直す。アプリ自身が
持っている時刻ならそれでよい。**ここに来るのは利用者のデータ**なので、そうでは
ない ── 変換した瞬間、画面はデータベースの中身と違うものを表示する。

実害があった（2026-09-04）: `timestamptz` の 2026-09-04 00:10 UTC が
画面では 2026年9月3日19:10 と出ていた。`TIME_ZONE` が未設定で Django の既定
`America/Chicago` が効き、そこへ `Accept-Language: ja` の書式が重なっていた。
**データベースの値は正しく、画面だけが嘘をついていた。**

だから datetime は `str()` にしてから渡す。文字列になっていれば描画側は何もせず、
`psql` が印字するのと同じ形（オフセット付きの ISO 8601）がそのまま出る。
他の型は素通しする。
"""
import datetime

from django import template

register = template.Library()

# 変換されると困る型。日付・時刻はオフセットまで含めて、来たとおりに見せる。
_AS_IS = (datetime.datetime, datetime.date, datetime.time)


@register.filter
def cell(value):
    """行データの 1 マス。日付・時刻だけ、データベースが返した形のまま返す。"""
    if isinstance(value, _AS_IS):
        return str(value)
    return value
