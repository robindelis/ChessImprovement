# Console — the permanent power-user shell

The interaction layer is now a **single, fixed layout** (no more A/B/C switching —
the Workbench/Library variants and the picker were removed). `workflow.css` renders
the **Console** shell: a fixed left grouped sidebar plus dense, full-bleed
workspaces that fill the viewport with no dead space. The look is **Vellum & Ink**
(`../themes/editorial.css`).

## What changed (the dense pass)
- **Killed the empty right third.** The Repertoire Builder is a true two-column
  flex (`board | data`): both stretch to fill the viewport width, so nothing is
  stranded. The right pane is a pure data stack — the **2000+ database table**
  over the **Explored Lines / Recent Games / Coverage Gaps** deck, each filling
  its share of the height.
- **Moved the move-inspector dock under the board.** Notes, arrows, importance and
  the line quick-actions are contextual to the board, so they left the data pane and
  now sit in the left column (single DB id/class lookup — no behaviour change). The
  left column is finally as tall as the right one; the collapsed vertical gap is gone.
- **De-duplicated the brand.** The top bar keeps the crown lockup; the sidebar
  footer is now a subtle `Vellum & Ink · Console` tag instead of repeating the name.
- **Tidier control bar.** Grouped clusters, consistent gaps, wrap-safe (no item can
  ride over another when the window narrows); destructive Reset buttons pushed to
  the end.
- **Trainer** is now `board | tutor dock` with the teaching blocks (prompt,
  feedback, notes, deviation banner) spanning the dock and the denser panels
  sitting two-per-row. **Scout** studio reflows `board | video | tree`.

## Layout invariants kept
- Element **IDs and classes are unchanged**; only CSS arrangement + moving the
  (statically-proven safe) inspector dock. Zero-CLS constant-height strips are
  recoloured, never resized — so the frontend contract suite still passes (514/514).
- **System fonts only** (CSP `font-src 'self' data:`); same-origin CSS/JS only.
- Responsive: below 900px the sidebar collapses to a horizontal top rail and panels
  stack.

## Tuning
- Sidebar width: `--wf-side` (default 244px) and `--wf-gap` in `workflow.css`.
- Table vs deck split: `#repExplorerCard{flex:1 1 0}` vs
  `#repBottomSidebarCard{flex:1.32 1 0}`.
- Assets are served `no-store`, so edits apply on reload.
