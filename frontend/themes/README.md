# Look: "Vellum & Ink" (baked default)

`editorial.css` is the app's permanent visual theme: a printed chess column — warm
vellum paper, serif display type, a burgundy + cobalt ink palette, hairline rules,
and a classic warm-wood board with gently sepia'd pieces. Typographic discipline:
**serif for prose/headings, monospace for moves and scores.**

It is loaded directly in `../index.html` **after** `style.css`, so it outranks the
base palette with no JavaScript required:

```html
<link rel="stylesheet" href="/style.css?v=20260926_1">
<link rel="stylesheet" href="/themes/editorial.css?v=20261012_1">
```

## Editing
- Tweak the palette/type at the top (`:root { … }`) or any component rule below it.
- Uses **system fonts only** — never reference a web font (the CSP is `font-src 'self' data:`).
- Keep the constant-height strips (`#repOpeningLine`, `#repEngineLine`, breadcrumbs, banners)
  at their locked heights — recolour them, don't resize them (zero-CLS contract).
- Assets are served `no-store`, so edits apply on reload.

The interaction layer (nav / layout workflows A/B/C) lives in `../workflows/`.
