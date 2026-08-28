"""行ごとの「これから流す SQL」を引くためだけの filter。

ビューが `{kind}:{name}` をキーにした dict を渡し、テンプレートはそれを引く。
Django のテンプレートは dict の添字アクセスができないので、総当たりの
`{% for %}{% if %}` になってしまう ── 20 行 × 3 種で毎回回すことになる。
"""

from django import template

register = template.Library()


@register.filter
def sql_for(previews, key):
    """`previews` から `key` を引く。無ければ空文字（＝プレビューを出さない）。"""
    if not previews:
        return ""
    return previews.get(key, "")
