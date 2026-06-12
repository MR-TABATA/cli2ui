# cli2ui UI conventions

The UI grew one panel at a time, so this file is the constitution that keeps new
panels consistent. CDN Tailwind is used (no build step), so shared component
styles live as raw CSS in `base.html`'s `<style>` — `@apply` is not available.

## Colour has meaning

A colour is a signal, never decoration. Pick by intent, not by looks:

| Colour    | Means                                  | Used for                            |
|-----------|----------------------------------------|-------------------------------------|
| `emerald` | primary action / success               | Create, Save, Add, Rename confirm   |
| `red`     | destructive / danger                   | Drop, Delete, Truncate              |
| `amber`   | warning / proceed-with-care            | restore-into-existing, oversized    |
| `sky`     | information / neutral link             | "open in SQL", download, read links |
| `zinc`    | structure (surfaces, borders, text)    | everything else                     |

Do **not** introduce a decorative blue/indigo/violet. If a thing isn't an
action, a warning, or a link, it's `zinc`.

## Buttons — use the shared classes, not ad-hoc Tailwind

Defined in `base.html`. Compose `.btn` with one intent modifier:

- `.btn .btn-primary` — emerald fill (the main action of a form)
- `.btn .btn-neutral` — zinc outline (secondary / toggle)
- `.btn .btn-danger`  — red outline (destructive)
- `.btn .btn-warn`    — amber outline (proceed-with-care)
- `.btn .btn-link`    — borderless sky text link ("open in SQL", downloads)
- add `.btn-sm` for the compact size used inside table rows

`:disabled` is handled by the base class (40% opacity, not-allowed).

## Form controls

- `.field` — the standard input/select (zinc-950 bg, zinc-700 border,
  `rounded-lg`, emerald focus ring). Add `.field-sm` for the in-row size.
- Checkboxes: `class="accent-emerald-400"`.

## Shape & spacing

- Cards / panels: `rounded-xl`. Controls (buttons, inputs): `rounded-lg`.
  Never bare `rounded` (4px) or `rounded-md` — those read as a third radius.
- Card header padding: `px-5 py-2.5`. Control padding comes from `.btn` / `.field`.

## Banners

Use `{% include "partials/_banner.html" %}` for error / notice messages instead
of hand-rolling the red/amber/sky box. It picks the tone from `error` (red) or
`notice` (amber if it contains "⚠", else sky).

## Contrast (dark theme)

Keep the text hierarchy legible on `zinc-950`: primary text `zinc-100`,
secondary `zinc-300`, muted/labels `zinc-400` (not `zinc-500`), faint hints
`zinc-500`. Borders `zinc-700` for controls, `zinc-800` for card edges.
