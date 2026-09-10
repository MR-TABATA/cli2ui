# cli2ui UI conventions

The UI grew one panel at a time, so this file is the constitution that keeps new
panels consistent. CDN Tailwind is used (no build step), so shared component
styles live as raw CSS in `base.html`'s `<style>` — `@apply` is not available.

## Colour is a role, never a shade

**Templates do not name palette colours.** There is no `text-zinc-400`, no
`bg-emerald-500`. A template names what the colour *is for* — `text-muted`,
`bg-accent` — and the active theme decides the shade.

This is not a preference. A named shade is the same shade in every theme, so it
survives into the theme where it can't be read. Going dark-only → light had to
touch **1,459 of them across 989 lines** for exactly that reason (the grep below,
run against `db8dda0`); the roles exist so it never has to happen again.

### The 17 roles

Defined as `--c-*` triplets in `templates/base.html` and exposed to Tailwind
under the same names, so `bg-panel`, `text-body`, `border-line`, and the opacity
forms (`bg-accent/10`, `text-danger/90`) all work.

| Role | What it is | Where it goes |
| :--- | :--- | :--- |
| `canvas` | the page ground | `<body>`; also the text on a `warn`/`danger` fill |
| `panel` | a panel's surface | cards, the drawer |
| `raised` | one step above `panel` | hovered rows, chips |
| `input` | an input's surface | `.field` (sinks on dark, stays white on light) |
| `line` | hairline | card edges, dividers, tab strips |
| `line-strong` | the stronger line | control and input borders |
| `heading` | the highest-contrast text | headings, an active tab's label |
| `strong` | strong body text | table cells, labels that carry a value |
| `body` | ordinary text — the most common one | paragraphs, help text |
| `muted` | secondary text | column labels, counts, captions |
| `faint` | the faintest readable text | placeholders, hints |
| `accent` | primary action / success | Create, Save, Add, Rename confirm |
| `on-accent` | the text on an `accent` fill | see below — it is not interchangeable |
| `danger` | destructive | Drop, Delete, Truncate |
| `warn` | proceed with care | restore-into-existing, oversized, write mode |
| `info` | information / neutral link | "open in SQL", downloads, read links |
| `catalog` | wayfinding for Catalog & data | the bento group only |

Two more `--c-*` values are opacities, not colours: `--c-scrim` (the drawer's
backdrop) and `--c-shadow` (its shadow). Both follow the ground's lightness.

If you want a colour that isn't in the table, you don't need a new colour — work
out which of these roles it already is. A thing that isn't an action, a warning,
a link, or wayfinding is text or structure, and those are all in the table.

### Text on a filled button — the one that goes wrong

- `bg-accent` → **`text-on-accent`**. The accent is user-chosen in a custom
  theme, so the readable text on it is derived from *the accent's* lightness.
- `bg-warn`, `bg-danger` → **`text-canvas`**. The intent colours are fixed pairs
  (light shades on a dark ground, dark shades on a light one), so the ground
  itself is always the readable side.

Using `text-on-accent` on a warn fill looks right in both presets and breaks in
a custom theme: seed a dark ground with a dark accent and `on-accent` becomes
white, which lands white text on amber at **1.9 : 1**. With `text-canvas` the
same theme measures **11.1 : 1**.

### The check

```bash
grep -rEn '(text|bg|border|ring|fill|stroke|divide|accent|placeholder|from|via|to)-[a-z]+-[0-9]{2,3}' \
  --include='*.html' templates core planner_lab
```

Must print nothing. It currently does, across all 38 templates. (`docs/` is the
marketing site — standalone pages with their own palette, not part of the app.)

### The bento's group accents

The workspace home (`partials/_bento.html`) is a launcher, so its groups carry a
colour purely for wayfinding: Query & planner = `accent`, Live ops = `info`,
Catalog & data = `catalog`, applied to the icon and a faint tint/border. This is
the one place colour means "which group" rather than "what happens". Every real
panel follows the intent rules above.

## Adding a role

A role has to be declared in **five** places in `base.html`, or it will be
undefined in one theme and inherit whatever came before:

1. the `colors` map in the inline `tailwind.config` (so Tailwind emits the class)
2. `:root` — the dark defaults
3. `[data-theme="light"]` **and** the `prefers-color-scheme: dark` block that
   backs 自動
4. `derive()` — how a custom theme computes it from its three seeds
5. `clearInline()` — the list of properties stripped when leaving custom

## The four themes

The header offers 自動 / 明 / 暗 / 自作. The choice lives in `localStorage`
(`cli2ui-theme`, plus `cli2ui-custom` for the three seed colours) and is applied
in `<head>` before first paint, so there is no flash of the wrong theme. **It is
never sent to the server** — no cookie, no request. cli2ui runs locally and the
theme is a property of the browser looking at it.

A custom theme takes three colours — ground, text, accent — and derives the rest.
Two things it deliberately does:

- **Faint text is clamped.** `muted` and `faint` are made by pulling the text
  colour toward the ground, but a theme whose ground and text are already close
  (Solarized) sank to 2.0 : 1. `fade()` binary-searches back to the last point
  that still holds 3.0 : 1.
- **It says when the seeds are the problem.** If ground and text are closer than
  3.0 : 1, no derivation saves the screen, so the panel says so instead. The
  threshold is 3.0 on purpose: 4.5 fired on Solarized Light (4.1 : 1), a theme
  people really use, and **a warning that cries wolf stops being read.**

## Buttons — use the shared classes, not ad-hoc Tailwind

Defined in `base.html`. Compose `.btn` with one intent modifier:

- `.btn .btn-primary` — `accent` fill, `on-accent` text (the main action)
- `.btn .btn-neutral` — `line-strong` outline (secondary / toggle)
- `.btn .btn-danger` — `danger` outline (destructive)
- `.btn .btn-warn` — `warn` outline (proceed-with-care)
- `.btn .btn-link` — borderless `info` text link ("open in SQL", downloads)
- add `.btn-sm` for the compact size used inside table rows

`:disabled` is handled by the base class (40% opacity, not-allowed).

## Form controls

- `.field` — the standard input/select: `input` surface, `line-strong` border,
  `rounded-lg`, `accent` focus border, `faint` placeholder. Add `.field-sm` for
  the in-row size.
- Checkboxes: `class="accent-accent"` — Tailwind's `accent-` utility (the CSS
  `accent-color` property) set to the `accent` role. The doubled word is not a
  typo.

## Shape & spacing

- Cards / panels: `rounded-xl`. Controls (buttons, inputs): `rounded-lg`.
  Never bare `rounded` (4px) or `rounded-md` — those read as a third radius.
- Card header padding: `px-5 py-2.5`. Control padding comes from `.btn` / `.field`.

## Banners

Use `{% include "partials/_banner.html" %}` for error / notice messages instead
of hand-rolling the box. It picks the tone from `error` (`danger`) or `notice`
(`warn` if the text contains "⚠", else `info`).

## Contrast

The floor is **3.0 : 1** for any text against the surface behind it, in every
theme — not just the two presets. Two things make this easy to get wrong:

- **A role is only safe on the surfaces it was built for.** `line` and
  `line-strong` are border colours; used as text they measure around 1.7 : 1
  even on dark. Faint text is `faint`, which is clamped for exactly this.
- **Alpine `:class` bindings don't apply to injected HTML.** When you check a
  panel, check the state htmx actually renders, not the one the binding would
  have produced.

Measure rather than judge by eye — read the computed colours and compute the
WCAG ratio, `(L1 + .05) / (L2 + .05)` over relative luminance.

The measured worst case, across both presets and two custom seeds, is the
**filled warn button on a light ground**: 3.2 : 1 on the light preset, and
2.95 : 1 seeded with Solarized Light. That is the floor's weakest point — a
fixed amber can only be so dark before it stops reading as amber. Anything new
that lands under it is a bug, not a precedent.

## i18n (JP / EN)

Every panel is translatable. New templates need `{% load i18n %}` at the top,
then wrap **user-visible text** with `{% trans "…" %}` (or `{% blocktrans %}` for
text with `{{ vars }}` / plurals). Python user-facing messages (errors, notices)
use `from django.utils.translation import gettext as _` and `_("…")`.

Do **not** wrap: SQL, `pg_*` identifiers, config keys, code samples, or anything
inside `mono`/`<code>` — those stay verbatim in both languages. JA strings live in
`locale/ja/LC_MESSAGES/django.po`; after adding strings run
`django-admin makemessages -l ja` then `compilemessages -l ja` and commit both
`.po` and `.mo`. The header toggle (`set_language`, cookie-based) switches
language; with no cookie it falls back to the browser's `Accept-Language`.
