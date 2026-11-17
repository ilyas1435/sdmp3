# SDMP3 UI polish — search scope and source label

## Problem

Three small UX defects remain after v1.10.0:

1. The search scope bar shows both a heading («بحث عن مجلد أو ملف») and a long,
   redundant list of every file in the current folder. The files duplicate the
   browse tree below and are not needed to choose a folder or file for search.
2. On a phone, the on-screen keyboard covers the folder dropdown and results;
   the user must scroll down and then back up to the search box. Search must
   reveal results automatically.
3. When the selected source is `/sdcard`, the route summary and source path
   look like an SD card. The internal storage must be named and coloured so it
   cannot be mistaken for a removable card.

## Approach

### 1. Remove the search-scope file list

- Delete the visible heading phrase «بحث عن مجلد أو ملف» and the entire file
  list (`#searchScopeItems` plus its «عرض المزيد» control).
- Keep the scope path label, sub-folder `<select>`, up button, folder chooser,
  search field and clear button.
- Keep `/api/items` fetching: the sub-folder `<select>` is built from the same
  response. Remove the list-specific JS (`renderSearchScopeItems`,
  `loadMoreScopeItems`, the delegated list listener) and its scroll/page state.
- Search itself remains unchanged: it still walks `searchScopePath` and renders
  hits into `#treeContainer` through `appendItemRows()`/`itemRowHtml()`.

### 2. Keyboard-aware, self-revealing search

- Group the search controls in a sticky container below the page header. Use a
  CSS custom property for the keyboard inset; update it from
  `window.visualViewport` (`offsetTop`, `height`) and `resize`/`scroll`.
- Cap the tree height to the visible viewport above the keyboard so the results
  panel scrolls internally; keep a small bottom inset so the last row is never
  hidden behind the action bar.
- After a committed search (`remember=1`, Enter/بحث) and after the debounced
  typing search, call `revealSearchResults()`: scroll the search controls into
  view, then smoothly scroll the results heading into view without moving the
  controls off-screen. Do not use a separate results panel.
- Keep results usable while typing: repeated calls must not fight the user’s
  scroll once results are already visible.

### 3. Name and colour the source kind

- Add a compact kind tag beside the source route summary and source path input.
  Resolve the volume kind from the path via the existing storage list; `/sdcard`
  is `internal` and is labelled exactly «المساحة الداخلية».
- Use existing kind colours/tokens (never hard-coded colours): internal sand,
  SD clay, USB blue, unknown olive. Keep the raw path visible, smaller and
  muted, for precision.
- Improve the route/source line spacing and contrast around the tag without
  changing the chosen source, folder selection, copy flow or queue order.

## Verification

- Add failing harness assertions first:
  - no `#searchScopeItems`, `#searchScopeMore`, or «بحث عن مجلد أو ملف»
  - sticky search controls, `--kb-inset`, `revealSearchResults`
  - internal kind label in route summary and source path line
- Run every gate: `py_compile` (7 files), `bash -n install.sh`, four module
  self-tests, render→`node --check`, jsdom UI assertions, backend assertions,
  and a live smoke test after restart.
- Bump `VERSION` and commit the completed fixes.
