# SDMP3 compact copy window, folded route, search-scope modal — design

## Intent

After v2.0.0 the page still carries three pieces of vertical weight that the user
does not need while working, and the progress dialog blocks browsing:

1. `#progressModal` must be an interruption, not a wall: it has a useless
   «يستمر النسخ في الخلفية» note, and while a copy runs the page cannot be used.
   The user cannot pre-select the next batch, which is what the planned
   *scheduled copy* feature needs.
2. The folded route line (`#routeDetails > summary`) prints two long paths and two
   decorative step numbers, and its row is too short to read.
3. The folder-scope panel is a permanent block inside the search row, crowding the
   browser. Its button should live beside the index buttons and open a dismissible
   dialog instead.
4. Each storage card is a full-width icon + Arabic name + raw path row; three
   volumes take three rows of the most-used section. The user wants a compact
   horizontal icon row with a one-word caption (الهاتف / OTG / SD).

## Decisions

### 1. Progress dialog: slim, minimizable, with a queue hook (structure only)

- **Delete** `#backgroundNote` and its CSS. The same fact already lives in
  `AGENTS.md`; a running copy is now visible in the action bar, so the modal does
  not need to explain it.
- **Delete** the fake first line `جاري التحضير...`: `#currentFile` starts empty and
  is filled on the first poll. Everything that carries data stays: percentage,
  count, bar, current file, speed/failed, card warning, error list, time taken,
  cancel/close.
- **Minimize**: a small «تصغير» button in the dialog header calls
  `minimizeProgressModal()` → `display:none`, `progressMinimized = true`.
  `checkTransferStatus()` must **not** re-open a minimized dialog (today it calls
  `showProgressModal()` whenever `!progressModalVisible`), so the auto-open path
  is gated on `!progressMinimized`.
- **Restore**: while a run is active the action bar's primary button becomes
  «استعادة النسخ NN٪» (its label is driven from the same poll that already paints
  the 3px line) and calls `restoreProgressModal()`. The selection summary button
  stays live, so items can be added to the selection during a copy.
- **A finished run is never silent**: when the status reaches `done`/`error`/
  `cancelled`, the dialog is restored automatically if it was minimized — the
  result, verification and error list only exist there.
- **Queue structure (no behaviour yet)**: `transferQueue = []`, `renderQueue()`,
  a `#queueBlock` section in the progress dialog that renders rows only when the
  list is non-empty, and a `+N في الانتظار` badge beside the action-bar label.
  No button enqueues anything in this batch, so no dead control ships; the next
  batch (scheduling) only has to push items and call `renderQueue()`.
  Backend untouched: the copy engine remains one file at a time.

### 2. Folded route line: kinds, not paths

- Summary becomes: «المصدر · [pill: المساحة الداخلية/كارت SD/USB OTG]» and
  «الوجهة · [pill]» — `#routeSourceSummary`/`#routeDestSummary` disappear; the
  existing `#sourceKindTag` is reused and a `#destKindTag` twin is added.
- Step numbers ١/٢ leave the **summary only**; the expanded nodes keep them
  because `.route-link` connects the two numbered steps.
- Full paths remain available in the expanded block and in the pill `title`.
- `.route-details > summary` gets more vertical padding (and the pill a slightly
  larger line-height) so the row reads as a control, not a caption.

### 3. Search-scope: button beside the index buttons, panel becomes a dialog

- `#scopeToggleBtn` moves out of `#searchControls` into the always-visible
  `.route-joc` row beside «إنشاء فهرس JOC» (the «تخزين الفهرس» button the user
  named is inside the folded route block, so putting the button there would hide
  it most of the time). The cache button itself stays where it is.
- `#searchScopePanel` (inline block) becomes `#searchScopeModal`
  (`.modal`): same ids inside (`#searchScopePath`, `#searchSubfolder`,
  `searchScopeUp()`, `openPathBrowser('search')`, `#foldersOnlyCheck`) plus
  «إغلاق». Opening/closing goes through one function; Esc and a backdrop click
  close it like every other `.modal`.
- Nothing about `searchScopePath`, its localStorage key, or `/api/items` changes.

### 4. Storage picker: icon tiles

- `.storage-grid` becomes a horizontal row (`flex-direction:row`), one tile per
  volume: icon on top, one-word caption below — `الهاتف`, `OTG`, `SD`, `كارت`.
  No `storage-name` (UUID form), no `storage-path`, no «الحالي/الوجهة» badge:
  the active tile is already tinted by `--kind`/`--kind-soft`.
- The caption comes from the server: each storage dict in
  `get_available_storages()` gains `short` (`STORAGE_KINDS[kind]['short']`), so the
  template, `STORAGE_ROOTS` and future JS read one source of truth.
- The path inputs and «تصفح/تحديد/فحص السرعة» stay: they are the only way to pick
  a subfolder or a custom destination.

## Goals

- The page can be browsed and a next batch selected while a copy runs.
- No control on screen is dead, duplicated, or explains a fact the UI already shows.
- The folded route states *which volume* on each side in one short line.
- Fewer boxes: search scope becomes a dialog, storage becomes one row.

## Non-goals

- A working queue/scheduler (next batch), backend changes, parallel copies.
- Changing `searchScopePath`, the search cache, the copy engine, or verification.
- Redesigning the settings dialog or the action bar beyond the running label.

## Constraints

- One inline `<script>`; `HTML_TEMPLATE` is a plain Python string (`\\` escapes).
- No colour literals; every new look uses `--kind*`, `--accent*`, `--field*`,
  `--rule`, `--text-muted`; all `font-size` are `calc(Npx * var(--font-scale))`.
- `innerHTML` paths/names escaped with `esc()`; tiles carry `data-path` only.
- `#progressModal` still owns a running copy: Esc must never close it (unchanged).
- Copy queue stays single-threaded, one file at a time.

## Builder's calls

- Exact paddings for the taller summary and the tiles.
- Whether `#queueBlock` also lists a count in its title.
- Adding `data-short` to the tiles vs. reading `STORAGE_ROOTS`: use the server dict
  and keep JS free of Arabic volume words beyond the existing
  `SOURCE_KIND_LABELS`.
