# SDMP3 search + settings polish — design

## Problem

Three usability defects reported after v1.10.1:

1. The standalone «البحث» section and its always-visible `search-scope` bar occupy
   vertical space, and the search box sits far above the file tree, so search
   never feels like part of browsing. The scope bar's only real job is choosing a
   folder for the search.
2. After a search the user still has to scroll by hand to see the results; the
   `scrollIntoView({block:'nearest'})` reveal can silently do nothing when the
   result head is only partially visible.
3. Font family and font size live in the «عام» settings tab, away from the theme
   they visually belong with.

## Decisions (approved with the user)

- **Scope stays available behind a small button.** The folder picker is useful, so
  it is collapsed by default and toggled by a small «خيارات البحث» button instead
  of being deleted. (User chose this over deletion.)
- **Search moves into the file browser.** The separate «البحث» section head and the
  sticky wrapper are removed. Inside «متصفح الملفات» the order becomes:
  sort row → search row (input + بحث + السجل + small scope toggle) → collapsible
  scope panel (path, sub-folder select, up, choose-folder, folders-only) →
  optional history block → `#treeContainer`.
- **Search row is not sticky.** It now sits directly above the tree, so the tree
  (its own scroll container) is always the next thing below it. This removes the
  sticky overlay that could hide the first results.
- **Reveal is deterministic and minimal.** After every rendered search:
  - reset the tree's internal scroll (`scrollTop = 0`) so the first result is shown;
  - if the result head is outside, or within ~72px of the bottom of the visible
    area (accounting for `visualViewport` keyboard inset and the fixed action
    bar), scroll the page so the head sits ~12px below the top;
  - if the head is already comfortably visible, do not move the page.
- **Typography moves to «المظهر».** `#fontSelect`, `#fontPreview`, `#sizeSelect`
  move into the Appearance pane under the theme selector; their save status uses
  `#settingsSaveState`. «عام» keeps only the auto-spoof switch.
- `VERSION` bumps to `2.0.0`; `AGENTS.md` is updated to match.

## Constraints

- All page JS stays in the single inline `<script>`; `HTML_TEMPLATE` is a plain
  Python string, so escape backslashes twice.
- No colour literals; theme tokens and existing kind classes only.
- Every `font-size` is `calc(Npx * var(--font-scale))`.
- `innerHTML` built from names/paths escapes with `esc()`; the new scope toggle
  reads no path.
- Keep `/api/items` (it feeds `#searchSubfolder`).
- Search still renders into `#treeContainer` via `appendItemRows()`/`itemRowHtml()`.
- Copy engine untouched.

## What the builder decides

- Exact CSS for the attached browser header (top radius on the sort/search/scope
  block, bottom radius on the tree) using existing tokens.
- Whether the scope panel open/closed state is remembered; default is closed, and
  no new persistence key is required.
- How to stub `getBoundingClientRect`/`scrollBy` in the throwaway jsdom harness to
  prove the out-of-view and in-view reveal branches.
