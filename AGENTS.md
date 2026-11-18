# AGENTS.md

Android/Termux Flask app that copies + timestamp-spoofs MP3s to SD cards, generates JOC text indexes, and converts them to audio via Edge TTS. Runs entirely on the device.

## Module layout
`project.py` is still the app: Flask backend, HTML, CSS, and — critically — **all
page JS in ONE inline `<script>` block** defined inside the Python string
`HTML_TEMPLATE` (starts ~line 1790). Four pure sibling modules sit next to it
(flat files, **not** a package, no `__init__.py`) because they are logic worth
testing without Flask:

| Module | Owns | Self-test |
|--------|------|-----------|
| `sdmp3_themes.py` | theme registry, CSS tokens, WCAG contrast contract | `python sdmp3_themes.py` |
| `sdmp3_state.py` | atomic `settings.json` / `search_history.json` / `index_settings.json` | `python sdmp3_state.py` |
| `sdmp3_groups.py` | group paths (nested groups) validation + folder chain | `python sdmp3_groups.py` |
| `sdmp3_fonts.py` | font registry (11 Arabic Google fonts, variable + static weights), `@font-face` CSS, size presets | `python sdmp3_fonts.py` |

They must stay **Flask-free and global-free**: `project.py` imports them, never
the other way round, and each one runs its own checks when executed directly.

- Run: `python project.py` (serves `0.0.0.0:5050`, **not** the Flask default 5000). UI at `http://localhost:5050` / `<device-ip>:5050`.
  Flags: `--host H`, `--port P`, `--no-wakelock`, `--version` (parsed in `main()` at the bottom of `project.py`).
- No test framework, linter, or package manifest. Verify via
  `python -m py_compile project.py sdmp3_cli.py sdmp3_themes.py sdmp3_state.py sdmp3_groups.py sdmp3_fonts.py`,
  `bash -n install.sh`, the four module self-tests, and manual browser testing.
- Stop with Ctrl+C / SIGTERM; this cleans the TTS temp cache (`tempfile.gettempdir()/tts_cache_<pid>`).

## Install / `sdmp3` entry point
The app is installed as the Termux command **`sdmp3`** so users never call `python project.py` directly.

| Path | Role |
|------|------|
| `sdmp3_cli.py` | CLI dispatcher: `run`/`start`/`stop`/`restart`/`status`/`update`/`version`/`doctor`/`open`. Any unrecognised flag is forwarded to `project.py`. |
| `install.sh` | Installer **and** updater. Clones to `$SDMP3_HOME` (default `~/.local/share/sdmp3`), installs deps, writes `$PREFIX/bin/sdmp3`. Re-running it updates. Must stay **heredoc-free** — it is designed to be piped into `bash`. |
| `requirements.txt` | `flask`, `edge-tts`; `update` re-runs pip only when this file changed. |
| `VERSION` | Single source of version + git tags; read by both `sdmp3_cli.py` and `get_app_version()`. |
| `$PREFIX/bin/sdmp3` | Generated `sh` wrapper with absolute `$PREFIX` paths (Termux has no `/usr/bin/env`). Not committed. |
| `~/.local/state/sdmp3/` | `instance.json` (pid/port/host) + `sdmp3.log` for `start`/`stop`/`status`, plus `settings.json`, `search_history.json`, `search_cache.json`. Kept outside the checkout so `git pull` stays clean. |

Gotchas: `/sdcard` is `noexec`, so the command must live in `$PREFIX/bin`; git checkouts on `/sdcard` need `git config --global --add safe.directory <path>` (dubious-ownership), which `install.sh` and `sdmp3 update` handle.

## Rule: never parallelize file writes
The product is **playback order**. Old MP3 players order a folder by its FAT32
directory-entry order and expect contiguous files. `build_dfs_queue` (DFS
pre-order + `natural_sort_key`) plus a single-threaded, one-file-at-a-time copy
loop is what guarantees this. Parallel copies interleave both directory entries
and clusters and **break the app's main feature** — do not add them, even as an
option.

## Shizuku is optional (never required)
`rish`/Shizuku is used only for (a) reading its status and (b) `sdmp3 unlock` →
`appops set com.termux MANAGE_EXTERNAL_STORAGE allow`. Every helper in
`project.py` (`rish`, `shizuku_status`, `shizuku_refresh`, `removable_sd`) must
degrade to `None`/`False` without raising, so the app fully works with no
Shizuku installed. Status is cached (`SHIZUKU_TTL`) and refreshed by a daemon
thread so no HTTP request ever blocks on a shell call.

## Version + status in the UI
`VERSION` is rendered into the header as `v{{ app_version }}` (`.pill.num`) and also
served by `/api/system` together with the Shizuku state and SD write access; the JS
`loadSystemInfo()` fills the three header items (version / `Shizuku` /
`كتابة الكارت`) and sets `pill-ok`/`pill-warn`/`pill-kind-*`, which colour the
status **dot** (see «Page layout»). Bump `VERSION` for user-visible changes.

## CRITICAL: escape handling inside HTML_TEMPLATE
`HTML_TEMPLATE` is a **plain `"""..."""` Python string**, not raw and not an f-string. Python processes backslash escapes in the embedded JS at parse time. To emit a literal backslash sequence to the browser you must **double the backslash**:
- Write `txt.split("\\n")` in Python so the browser receives `split('\n')`. A single `\n` becomes a real newline and **breaks the whole script block**.
- Same applies to `\.`, `\uXXXX` regex/unescape tokens: use `\\.`, `\\uXXXX` in the Python source.

## One bad JS token disables the entire UI
Since all JS is a single `<script>`, one syntax error kills every handler — symptoms are "no storage shown, no folders, can't select anything." `python -m py_compile` will NOT catch JS errors (the JS is just a string to Python). To lint the JS: render/extract the `<script>` contents (with Jinja `{*...}` stripped) and run `node --check`.

The working recipe (a throwaway harness is kept outside the checkout, in
`$TMPDIR/uitest/`, so it never ships):

```sh
python - <<'PY'
import re, sys; sys.path.insert(0, '.')
import project
project.app.testing = True
html = project.app.test_client().get('/').get_data(as_text=True)
assert html.count('<script>') == 1
open('/tmp/app.js', 'w').write(re.findall(r'<script>(.*?)</script>', html, re.S)[-1])
PY
node --check /tmp/app.js        # the hard gate
```

Then boot the rendered page under jsdom with a `fetch` stub and drive the real
functions — that is what catches "handler exists but throws" (the success path
must call `process.exit(0)`, or jsdom's `setInterval` keeps node alive).

## Copy engine — do not regress these
The copy path is the product, and it is tuned; changes must keep the queue
byte-identical (DFS pre-order + `natural_sort_key`) and stay single-threaded.

- **`plan_transfer()`** builds the queue *and* the totals in **one** traversal.
  The old code walked the tree twice (`scan_source` then `build_dfs_queue`), which
  doubled the FUSE round-trips. It returns `(tasks, total_files, total_bytes,
  skipped)`; `skipped` reports unreadable source folders instead of dropping them
  silently (`except Exception: pass`).
- **Copy groups** (`groups: [{name, paths}]` on `/transfer/start`): one or more
  FAT-legal parent folder paths, each created inside the destination *first*, then
  the roots assigned to it are copied inside it. The worker plans each group as its
  own segment (`plan_segment(gpaths, dest/name)`), inserts the group's whole
  directory chain at that segment's front (parents first), then plans the
  **ungrouped** roots straight into `dest` last. So the queue stays one flat,
  ordered list — the group folder never contains another segment's files, and
  ungrouped items keep today's behaviour. The source tree is never modified.
  - **Nested groups are paths**, not a tree: `name` may be `"العلماء/ابن باز"`
    (a breadcrumb). `sdmp3_groups.py` owns the rules: `validate_group_name` (one
    FAT segment), `validate_group_path` (collapse `//`, drop leading/trailing `/`,
    every segment legal, depth ≤ `GROUP_MAX_DEPTH` = 3, whole path ≤
    `GROUP_PATH_MAX` = 180), `group_segments`, `group_dir_chain(dest, name)` →
    `[dest/A, dest/A/B]`. `/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|` are refused;
    a name may not end in a space or a dot.
  - `prepare_copy_groups()` rejects duplicate full paths (casefold: FAT is
    case-insensitive) — `A` and `A/B` are **not** duplicates, that is nesting —
    rejects a path that is not in the selection and a path that sits in two
    groups, and **drops empty groups**. An old single `group` field is still
    accepted (wrapped into one group).
  - A group's **first segment** equal (casefold) to the basename of an
    **ungrouped** root is refused in the route *and* in the worker: otherwise that
    root would silently land inside the group folder and mix two trees.
  - Any existing group folder is a merge, so the route returns one
    `needs_group_confirm` listing **all** of them (`groups: [names]`) until the
    request carries `confirm_merge: true`; the UI asks once, before anything is
    written. Every level of a nested chain is checked: a *file* where a folder
    must go is refused before the copy starts.
  - Queue order for groups `A` + `A/B`: dir `A`, dir `A/B`, then `A/B`'s tree
    (`A/B/Books`, its files), then `A`'s own tree. Verified by test.

- **Post-copy verification** (`verify: true` by default): after the final
  `sync_with_heartbeat()`, `verify_copied_files()` makes **one metadata-only walk**
  over the destination roots and compares each planned file's size with the bytes
  the copy loop actually wrote. Pre-existing extras in a merge are ignored, so a
  normal merge cannot produce a false mismatch. It reports
  `verified`/`verify_total`/`verify_failed`, never reads file contents, and never
  changes the playback order. `verify: false` from the UI skips the pass.
- **`copy_file_sequential()`** opens src/dst with `os.open`, `posix_fadvise(POSIX_FADV_SEQUENTIAL)` hints, then uses `os.sendfile` (4 MB chunks) when the kernel/filesystem supports it; on `EINVAL`/`ENOSYS`/`EXDEV`/`EOPNOTSUPP`/`EAGAIN` it continues with the 4 MB buffered loop from the same offset. sendfile measured ~2× faster on Android FUSE. Both paths write front-to-back, and on any error the half-written destination file is deleted so playback order can't be polluted.
- **No `fsync` per file.** The transfer calls `sync_with_heartbeat()` every `SYNC_THRESHOLD` (1 GB) and once at the end. Ordering comes from *sequential writes*, never from fsync — the per-file fsync was removed because it cost tens of ms per MP3 (the main cause of "copy is very slow"). Do not add it back, and keep the threshold large: a small value turns one big copy into hundreds of short stalls.
  - `sync_with_heartbeat(phase, dest=...)` flushes with **`flush_destination(dest)`**:
    `syncfs(fd)` from libc via `ctypes` (one filesystem) and only falls back to
    `os.sync()` (every filesystem on the phone) when libc has no `syncfs`. The
    copy worker always passes `dest`; spoof/delete still use the global flush.
- **Honest speeds and times.** Copy time and verify time are measured separately:
  `copy_seconds` ends after the final flush, `verify_seconds` covers only
  `verify_copied_files()`. `avg_mbps = bytes / copy_seconds` (verify never
  inflates it), `speed_mbps` stays the 6s-window EMA. The UI shows
  «اللحظية … • المتوسط …» while running and «النسخ X ث • متوسط السرعة … • التحقق Y ث»
  when done. Do not hide these numbers: the cost is ~2 ms/file for verify and
  negligible CPU for the display, and without them a slow card is indistinguishable
  from a slow app.
- **Card speed probe**: before a copy whose total is ≥ `CARD_SPEED_PROBE_MIN_BYTES`
  (48 MB), the worker writes `CARD_SPEED_PROBE_MB` (8 MB) sequentially with one
  end flush (`probe_card_speed`) and stores `card_mbps`; below `SLOW_CARD_MBPS`
  (5 MB/s) it sets `card_warning`, which the UI shows as an amber note with no
  "pull the card out" instruction. The probe file is deleted; it is written before
  the first planned file so it cannot reorder the real copy. A probe that hangs
  marks the destination unhealthy in `_UNHEALTHY_DESTS` (`destination_is_unhealthy`)
  and the worker **aborts before writing any planned file** — the timeout is 25 s
  for 8 MB (4× slower than `SLOW_CARD_MBPS`), so a merely slow card is reported as
  slow, never called dead.

- **`check_destination()`** writes a 5-byte probe with an 8s timeout before every
  copy / spoof / benchmark. A card that is mounted but dead would otherwise block
  the worker inside `write()` forever. Timed-out destinations are cached in
  `_UNHEALTHY_DESTS` for 120s so retries fail fast instead of spawning more stuck
  threads.
- **Stall detection**: the worker calls `touch_progress()` per 4 MB chunk, per file/dir entry, and while counting for spoof; `get_state_snapshot()` reports `slow` after `SLOW_SECONDS` (15s, amber “الكارت بطيء” with no removal instruction) and `stalled`/`stall_seconds` after `STALL_SECONDS` (60s, red). **`sync_with_heartbeat()`** is mandatory around every `os.sync()`: the flush is one blocking syscall, and a normal 30–60s flush on a slow card used to trip the old 25s alarm while the copy was perfectly fine. It starts a small heartbeat thread (3s, capped at 180s so a genuinely hung flush is still reported) and sets `syncing`/`sync_seconds`. The worker itself cannot report a hang — it is blocked in a syscall. The card wording applies only to `action in ('copy','spoof','delete')`; TTS is network-bound and never writes a volume. Every slow phase must keep the heartbeat alive: `count_files(on_progress=...)` while counting for spoof, `touch_progress()` per TTS chunk, and `_run_ffmpeg()` (which polls ffmpeg every second) instead of `subprocess.run`.
- **Never copy onto itself.** `dest_inside_source()` rejects a destination inside
  the source; `copy_targets_source()` rejects the mirror case (destination is the
  *parent* of the source, e.g. `/sdcard/A` → `/sdcard`), where the planned target
  is the source file itself — `O_TRUNC` empties it and the error path deletes it.
  Both are checked in the route *and* in the worker.
- **Destructive routes are guarded, not neutered.** `/api/delete` is
  asynchronous and forced: `force: true` (the UI always sends it) means normal
  unlink → `chmod 777` retry → Shizuku `rm -rf`. Only `protected_target_reason()`
  refuses a path: volume roots (`_is_storage_root`) and `/`, `/data`, Termux's
  prefix, `$HOME` or this app's own checkout (and any ancestor of those). An
  Android-private folder (`Android/data`, `Android/obb`) is refused **only** when
  Shizuku is not running, because the app itself can never remove it.
- **Never build the root list from `os.listdir('/storage')`.** Android denies
  that to Termux (`Errno 13`), so the old `is_inside_storage()` saw only `/sdcard`
  and refused every delete on a real SD card. `_allowed_storage_roots()` uses
  `get_available_storages()` (df) + `/mnt/media_rw`, with a 5s cache.
- **`ACTIVE_THREAD` + `is_thread_alive()`** recover a stale `status='copying'`
  left by a dead worker (otherwise every later copy fails with «هناك عملية قيد
  التشغيل بالفعل»). The start routes `reset_state(...)` **synchronously** before
  launching the thread, so a poll can't read the previous run's `done`/`error`.
- **`/api/benchmark`** writes 32 MB with 4 MB blocks and a single end-of-file
  flush — the same shape as a real copy. The old version fsynced every 1 MB, which
  measured flush latency and always looked slow.
- Expected numbers on this device: ~180 MB/s (5×40 MB files) and ~28 MB/s for
  150×160 KB MP3s on internal storage. Small-file speed is bound by per-file FUSE
  create/close, not by the buffer size.

## Storage detection (server-side, not JS)
Options are rendered at route `/` via Jinja `{% for st in storages %}`, backed by `get_available_storages()`: always `/sdcard` (internal), plus external SD cards found from `df -h` `/storage/...` mounts (emulated mounts are skipped). If the list is empty, check this Python function — not the JS.

Each storage dict carries `kind` + `icon` so the UI can label it honestly:

| kind | How it is detected | Icon / label | Colour (palette) |
|---|---|---|---|
| `internal` | `/sdcard` | `ico-internal` — الذاكرة الداخلية | `--kind-internal` sand `#d0b49f` |
| `sdcard` | `/proc/mounts` device major **179** (MMC) | `ico-sd` — كارت SD (UUID) | `--kind-sdcard` bright clay `#d98b63` (ink `#dda88c`) |
| `usb` | device major **8** (SCSI/USB OTG) | `ico-usb` — USB OTG (UUID) | `--kind-usb` blue `#8fa9c4` (ink `#a8bfd6`) |
| `card` | anything unrecognised (dm-*, unknown fs) | `ico-card` — كارت ذاكرة (UUID) | `--kind-card` olive `#a3b18a` (ink `#b7c39f`) |

`storage_kind()` looks the volume up in `/proc/mounts` as `/mnt/media_rw/<UUID>` and passes the device to `classify_volume_device()`. `df -h` alone cannot tell an SD card from a USB stick — both are `/storage/XXXX-XXXX`. The icons are one SVG sprite (`<symbol id="ico-*">` right after `<body>`) referenced with `<use href="#ico-...">`; the pickers are `.storage-card` **icon tiles** in one horizontal row (`chooseStorage()` / `syncStorageCards()`), captioned with `STORAGE_KINDS[*]['short']` (الهاتف / SD / OTG / كارت) that `get_available_storages()` copies onto each dict — no UUID name, no raw path, no badge on the tile (the full name + path are in its `title`).

Theme = **Coffee Blues** (https://www.color-hex.com/color-palette/102595) is still the default of the 16-theme registry, but v1.10 hue-rotated it while preserving relative luminance: page `--bg #192133`, card `--card #222b40`, lift `--card-2 #2d374d`, `--field #161c2b`, `--border #2f3c5a`, `--text #e9e2d1`, muted `#aaa297`, accent `--accent #3d8e9c`, accent fill `--accent-fill #327883` (hover `#2a6973`). The warm-sand-on-cool-slate character survives (text ≈ 10.9:1 on a card, muted ≈ 5.6:1); only the hues moved, so contrast stayed inside the contract. `--radius` is 12px; `--rule` (translucent `--border`) is the hairline between sections, `--field-line` outlines inputs/cards, `--bar-bg`/`--bar-line` belong to the opaque action bar, and `--num-font` (`NUM_FONT_STACK`) is the Latin mono face used by `.num`. **No colour literal belongs in the template** — every token, alpha variant and layout colour comes from `sdmp3_themes.py`.

The accent split stays: `--accent` marks (icons, focus, checkbox, disk bar, small status text only where the ratio allows), while filled buttons use `--accent-fill` with `--on-accent`. `--accent-ink #151e28` labels warning/success/danger fills. Danger (`--danger #f43f5e`, text `--danger-ink #fc8b9b`) and success (`--success #7fbf93`) are semantic and were deliberately **not** rotated in v1.10, along with the four `--kind-*` volume hues, so their meaning survives every theme. Nothing glows — no gradient, no coloured button shadow, no inset card sheen; secondary buttons stay neutral `btn-outline` so only the primary action wears the accent.

Each volume kind owns a hue: `.storage-card.kind-*` sets a local `--kind`/`--kind-ink`/`--kind-soft`/`--kind-tile` used by the tile's border, icon tile, active fill and active caption; `.pill-kind-*` and `.kind-border-*` reuse the same variables for the route kind pills, the header `sdWritePill` and the path inputs. Internal = sand `#d0b49f`, SD = bright clay `#d98b63` (ink `#dda88c`), USB = blue `#8fa9c4` (ink `#a8bfd6`), unknown = olive `#a3b18a` (ink `#b7c39f`). The four are told apart by icon + label as well as hue — the palette only has so many warm earths — and `-ink` exists because the fill colours are too dark for 11px text. Only a writable card gets its kind hue in the header pill; a refusing card stays `pill-warn`.

The spoofing button is `btn-sand` (`--sand-fill #d0b49f` on `--accent-ink`), not amber: `--warning` is reserved for text and warning pills, so no button introduces a hue the palette does not own.

## Page layout (v1.9.0 — design, not features)

One document, not a stack of boxes. The page is `<header class="app-header">`, then
three `<section class="section">` blocks separated by a hairline (`.section + .section
{ border-top: 1px solid var(--rule) }`), each with `.section-head` / `.section-title`
/ `.section-kicker`. There is **no `.card`** and no `.card-title` any more.

- **Header**: 36px badge, `h1` at 17px, no tagline; the status items (version,
  Shizuku, card write) are one quiet `.pill-row`. State is carried by a 6px dot
  (`.pill::before` plus `.pill-ok`/`.pill-warn`/`.pill-kind-*`), while the label
  stays `--text-muted` so it clears 4.5:1 on every theme. Pills outside the header
  (picker badge, group chip) keep the older tinted-chip look.
- **Route section**: the whole block is a `<details class="route-details"
  id="routeDetails">`, **closed by default**, because the source/destination pair
  is chosen rarely while the rest of the page is used constantly. Its `<summary>`
  names **which volume**, never which folder: «المصدر» + `#sourceKindTag` and
  «الوجهة» + `#destKindTag`, both kind pills coloured by `--kind-*`
  (`updateRouteSummary()` after every change). The full path lives in the
  expanded block and in each pill's `title`; an unset destination reads
  «لم تُحدَّد» and an unknown source «مسار مخصص», both wearing `.is-empty`.
  Open/closed is remembered in `sdmp3_route_open`. Inside are source
  (`.section-step` ١) → `.route-link` (hairline + arrow) → destination (٢) — the
  only place the step numbers live now; each node holds its `.storage-grid` and
  one `.path-line` (input + buttons). This is the only place the copy direction
  is chosen. Two always-visible buttons sit **outside** the fold in
  `.route-joc-row`: «إنشاء فهرس JOC» (`.route-joc`) and «مجلد البحث»
  (`#scopeToggleBtn` → `openSearchScope()`); both are separate flows and must
  stay one tap away.
- **File browser section**: the sort row, the search row (`#searchControls`) and
  `#treeContainer` are one attached block; search sits directly above the
  results, so typing never leaves the input far from them. The folder-scope
  controls are **not** inline: they live in `#searchScopeModal` (see «Search
  scope»).
- **Tools section**: spoofing lives in `<details class="tools">`, closed by
  default — an occasional action must not out-weigh the copy.
- **Action bar** (`#actionBar`, `.actionbar`): fixed, **opaque** `--bar-bg` with a
  `--bar-line` top border (never `backdrop-filter`), and a 3px
  `.actionbar-progress` line at its top edge. The left side is a button
  (`.actionbar-summary` → `openSelectedModal()`) showing `#selectedCount`,
  `#barSize`, `#barEta`; the primary «بدء النسخ» is the only filled control. Body
  bottom padding is 112px (116px on phones) to clear it.
- `#barSize` is filled by `scheduleSelectionSize()`: a 400ms debounced
  `POST /api/sizes` whose signature (`selectionSignature()` = sorted selected keys
  + excludes) also prevents refetches; `updateSizeDisplay()` writes it. «عرض
  المحدد» is therefore optional, not the only way to see the copy size.
- `#barEta` is a guess, so it is written with a tilde: it reads `sdmp3_last_mbps`
  from `localStorage`, saved from `avg_mbps` when a copy finishes, and stays hidden
  until a copy has measured that card. `mirrorProgressInBar(data)` is called from
  `checkTransferStatus()` and paints the bar's line (a delete sweeps it,
  verification uses `verify_progress`).
- **Icons, not emoji**: `ico-check`/`ico-alert`/`ico-plus`/`ico-spark` joined the
  sprite and `spriteIcon(id)` builds the inline `<svg><use>` for generated notices
  (`showInlineDone`/`showInlineError`, JOC text notices). Emoji carry their own
  colour and metrics and fight the theme.
- **Esc** closes the front `.modal` (the `.modal-front` one, else the last visible)
  but **never** `#progressModal`: that dialog owns a running copy and is closed
  with its own button after the run ends.
- `.num` (with `--num-font`, tabular figures, `direction:ltr`) is for counts, sizes,
  speeds, timestamps and `.storage-path`. `#selectedCount`/`#barSize`/`#barEta` are
  written with `textContent`, never `innerText` (jsdom has no `innerText`, and that
  is the gate's environment).

## Groups window: folder tree, inline rename, drag & drop
`#selectedModal` turns the flat group list into a **folder tree** (`#groupTree`).
The data model did not change: a group is still `{id, name, paths}` and `name` is
a breadcrumb (`"A/B"`), so the tree is **derived on every render**
(`groupTreeRows()` → nested nodes) and there is no second model to keep in sync.

- Rows are `.gt-row` with a twisty, a folder icon, the segment label, a count and
  the actions. Only *closed* nodes are remembered (`groupNodeClosed`): a folder
  the user just created must appear expanded.
- **Renaming happens in the row** (`startRenameGroup` → `#gtRenameInput`): Enter
  saves, Esc cancels, ✓/✕ do the same. Only the **last segment** is edited;
  `commitGroupRename()` renames every nested group under it (`A/B` follows `A`).
  `groupRenameError` validates the segment (`groupSegmentError`), the whole path
  length and casefold duplicates *including* the renamed descendants, and Save
  is disabled while there is an error. No `showInputModal`, no second sheet.
- **Creating is one click**: «مجلد جديد» (`startNewGroup`) adds `مجلد N` (unique
  via `uniqueGroupName`) with the checked rows as its items and opens the rename;
  the `＋` on a folder (`startChildGroup`) creates `parent/فرع N`, disabled at
  `GROUP_MAX_DEPTH`. Empty groups are allowed here — «ضم» later fills them.
- **Delete** (`deleteGroup`) removes the folder *and* its nested groups; its
  files stay selected and fall back to «بدون مجلد».
- **Drag & drop** moves one item: rows in `.gt-path` carry
  `text/x-sdmp3-path`, `.gt-row[data-drop]` is the target and
  `movePathToGroup(path, gid|0)` re-assigns it (an item lives in exactly one
  group). The permanent **«بدون مجلد»** node (`ungroupedPaths()`) lists what
  belongs to no folder and is the drop area that takes an item out of every
  group. The tree has one **delegated** click/drag listener: no group name or
  path is ever interpolated into an inline `onclick` (only `esc()`ed attributes).
- Checkboxes in the selected-file list still use `value="pick"` and `data-path`;
  the group tree never writes a path into a checkbox value.

## «تنظيم ذكي»: propose, preview, apply
`smartOrganizeGroups()` maps **each selection** to its own parent path relative
to `currentSourceRoot` (`sourceRelativeParent`), keeps the last
`GROUP_MAX_DEPTH` segments, and falls back to `sourceBasename()` for an item
sitting directly in the root. That fixes the two old failure modes (a flat
selection and selections from different parents).

The proposal is a **preview** (`smartPreview` → `#smartPreview`), one checkbox
per folder with its item count and «موجود/جديد»: nothing is written until
«تطبيق على N مجلد». `applySmartPreview()` takes the checked rows only, removes
their paths from any previous group, reuses a folder whose path already exists
(`groupPathKey`) and drops whatever became empty. `cancelSmartPreview()` throws
the proposal away.

## Remembered UI state (`localStorage sdmp3_ui_state_v1`)
A refresh used to lose the cart. Now `scheduleUiSave()` (300ms debounce, called
from `renderGroupUI`) writes
`{v:1, source, search, paths:[[path,isDir]], excludes, groups:[{name,paths}]}` and
`restoreUiState()` (from `window.onload`, after the tree is requested) rebuilds
the selection, the groups and the search folder. Additive and cheap:

- `uiStateReady` gates both directions: nothing is written before the restore
  has run, so the page cannot overwrite the saved state with an empty one.
- More than `UI_STATE_MAX_PATHS` (3000) selected paths skips the write rather
  than throwing a quota error on a phone.
- The restored selection is verified once against the server:
  `pruneMissingSelection()` posts to `/api/sizes` and drops every path the
  response lists in **`missing`** (`dropMissingPath`). `/api/sizes` computes it
  from the same `os.path.exists(root)` it already needed — one test per walked
  root, never a second stat per item.
- A card that was pulled out comes back as nothing, not as a ghost selection.
- Saved groups whose name no longer validates, or whose paths all vanished, are
  dropped silently (`groupNameError`); ids are re-issued from `nextGroupId`.

## UI invariants

- **No remote assets.** The page used to load Google Fonts, which blocked first
  paint on a phone that is offline or behind a slow DNS; the font stack is the
  system face (`system-ui, … 'Noto Sans Arabic'`). Keep the page self-contained:
  the 10 selectable fonts are self-hosted woff2 in `static/fonts/` (see «General
  settings: fonts»), never a `fonts.googleapis.com` link.
- **Every `font-size` is `calc(Npx * var(--font-scale))`.** A raw `Npx` silently
  ignores the size preset, so it is a bug even though it renders. Same for the
  inline `style="font-size:…"` attributes in the template — they are scaled too.
- **Markup built with `innerHTML` must escape names/paths with `esc()` (quotes
  included). Row buttons carry no data: one delegated listener reads the path
  from the row, so a filename with `"` or `'` can't break or inject markup.
- **The group UI has its own checkbox namespace.** Rows inside «الملفات المحددة»
  use checkboxes with `value="pick"` (never a path) plus `data-path`, so
  `refreshCheckboxStates()` — which syncs every `.checkbox` whose value starts
  with `/` — can never treat them as tree selections. `pickPaths` is the separate
  Set. Group-card and assign-sheet buttons are handled by **delegated** listeners
  reading `data-gid`/`data-act`, so no group name is ever interpolated into an
  `onclick`.
- **`modal-selected` has a fixed height** (`min(84vh, 760px)`, `92vh` on phones)
  with `#selectedList` as the flex child that scrolls, so the pick bar and the
  confirm action cannot slide under the finger while rows are checked. The assign
  sheet is `position:absolute` inside it (`bottom:14px`) for the same reason.
  `modal-settings` uses the same fixed-height rule for its tabs.
- Group data lives only in JS: `copyGroups = [{id, name, paths}]`, one path per
  group; `name` is a breadcrumb (`A/B`), so nesting needs no tree model. It is
  cleared when a copy finishes with `action === 'copy'` and when the selection
  becomes empty; cancelled/failed copies keep it for retry. It is mirrored to
  `sdmp3_ui_state_v1` (see «Remembered UI state») so a refresh keeps the cart.
- **Groups are typed as paths, validated client-side too**: `GROUP_BAD_CHARS`
  deliberately does **not** contain `/` (it is the nesting separator), depth ≤
  `GROUP_MAX_DEPTH` (3), full path ≤ `GROUP_PATH_MAX` (180), each segment ≤ 80.
  The card shows the breadcrumb (`A › B`) and a «＋ فرع» button (`startChildGroup`)
  that pre-fills `parent/`, disabled at depth 3. `saveGroupEditor`/`renameGroup`
  store the canonical `groupPathParts(...).join('/')`.
- `/` is rendered per request; do not add a cache header to it — it embeds the
  per-volume storage list, the active theme **and** the saved font/size.
- `theme-color` is the active theme's `--bg` (`{{ theme_bg }}`), not a constant:
  the Android status bar must match whichever theme is saved.

## Settings modal
`#settingsModal` (`modal-content modal-settings`, fixed height like the picker)
has **four tabs in this order**: «المظهر», «عام», «الفهرس», «حول». One pane per
tab, all in the DOM (`#settingsPaneAppearance` / `General` / `Index` / `About`);
`showSettingsTab(tab)` only toggles `display`, so a form value is never rebuilt
or lost. `openIndexSettings()` (the create dialog's button) opens the modal
straight on «الفهرس».

- **المظهر** owns the look: `#themeSelect` **and** the typography controls —
  `#fontSelect` (12 options with `data-font-id`), `#sizeSelect` (4 options with
  `data-size-id`) and a live `#fontPreview` styled with `{{ app_font }}`. The
  theme select is rendered server-side by Jinja from `themes` (each `<option>`
  carries `data-theme-id` and is selected server-side), so the page needs no
  theme JSON in JS. `pickTheme(id)` POSTs `/api/settings`, then `applyTheme()`
  applies the returned `tokens`/`theme_bg`, sets `<html data-theme>` and syncs
  the select. `pickFont(id)` / `pickFontSize(id)` POST one key each, apply
  `app_font` / `font_scale` from the response and report in
  `#settingsSaveState`. No reload, no re-render.
- **عام** owns only the `auto_spoof` switch (`#autoSpoofCheck` →
  `pickAutoSpoof`), which reports in `#generalSaveState`.
- **الفهرس** owns the JOC index settings — see «Index settings» below. It is the
  only place they are written.
- `setSettingsState(text, kind, elementId)` writes one status line per pane
  (`#settingsSaveState`, `#generalSaveState`, `#indexSaveState`). The header gear
  button (`.header-settings`) opens the modal on «المظهر».

## Typography: fonts (`sdmp3_fonts.py` + `static/fonts/`)
Eleven Arabic Google fonts plus the `system` default, all **self-hosted**: 28
woff2 files in `static/fonts/`. Ten families are variable 400..700 and ship an
`arabic` + `latin` subset each (20 files); `IBM Plex Sans Arabic` is only
published as **static weights**, so it ships one file per weight and subset
(`<id>-<weight>-<subset>.woff2`, 8 files for 400/500/600/700). Nothing is fetched
from `fonts.googleapis.com` — the page must stay self-contained on a phone that
is offline or behind a slow DNS — and `fetch_fonts.py` (a dev tool) verifies the
Google unicode-ranges still match the module.

- A font is one dict in `sdmp3_fonts.py`: id, family, name, desc and — for a
  static-weight family — `weights: FONT_WEIGHTS`. Everything else is derived:
  `font_assets(font)` is `[(weight|None, subset, filename)]` and is the single
  source of truth for the `@font-face` rules, the self-test and `fetch_fonts.py`.
  A file name comes from `font_file(id, subset, weight)`; `weight` is required
  (and part of the name) only for a static family. `font_face_css(version)`
  emits one rule per asset with `?v=<version>` cache-busting and the real
  `font-weight`; `font_stack(id)` gives the `--app-font` value;
  `font_scale(size_id)` gives the `--font-scale` multiplier; `FONT_SIZES` is the
  4 presets (`small`/`normal`/`large`/`xlarge`).
- `fetch_fonts.py` asks for `wght@400..700` for a variable family and for the
  explicit `wght@400;500;600;700` list for a static one, then reads the weight
  from each `@font-face` body (`parse_faces`): Google repeats the same
  `/* arabic */` label once per weight, so the label alone cannot identify a
  file.
- Typography is **two CSS variables** and both are rendered server-side:
  `--app-font` in `:root` (via `{{ app_font|safe }}`) and `--font-scale` on
  `<html>` (via `{{ font_scale }}`). Applying a pick is a property write, no
  reload and no re-render.
- Every `font-size` in the template is `calc(Npx * var(--font-scale))` — a raw
  `Npx` silently ignores the size preset. The gate checks that there is none.
- Only the chosen family is downloaded: an `@font-face` is inert until text
  actually uses it, which is why the `system` default costs zero font requests.
- `font_css` and `app_font` must be piped through `|safe` inside `<style>`: Jinja
  escapes `'` to `&#39;`, which is **not** decoded in CSS. The
  `style="font-family: {{ f.css_stack }}"` attribute is fine with escaping.
- `str.format` breaks on CSS braces, so `font_face_css()` uses `%`-formatting.
- Run `python sdmp3_fonts.py` for the self-test (every id resolves, sizes are
  ordered, the `@font-face` output is complete, every asset has a rule and every
  file is on disk). Adding a variable font is one dict plus two woff2 files;
  adding a static one is one dict with `weights` plus two files per weight.
  `project.py` does not change, the Jinja loop picks it up.

## Path picker (تحديد / تصفح)
`/api/sizes` returns sizes for a whole selection in one request (parents are
walked once and nested selections are counted once) **plus `missing`**: the
selected paths whose walked root does not exist. It reuses the single
`os.path.exists(root)` the walk already needs, so a restored selection can drop
a pulled-out card without another stat per item. Use it instead of calling
`/api/size` in a loop. `/api/items` returns `{name, path, is_dir, size}` in one `scandir` pass (folders first, then files, name-sorted). The picker renders folders (navigable, with rename/delete actions) and files with a formatted size **and the same rename/delete icons** — a destination must be a folder, so a file row is never selectable, but it can still be tidied up from here. Long folders are paged: `PICKER_FILES_STEP` (300) files per page with «عرض المزيد», so a 4 000-file folder opens instantly.

The dialog has a **fixed height** (`.modal-content.modal-picker`: `min(84vh, 720px)`, `92vh` on phones) with `#destTree` as the flex child that scrolls. Without it the dialog resized with every folder and moved the Confirm button under the user's finger.

## Delete (forced, asynchronous)
`POST /api/delete {path, force:true}` starts `delete_thread_task()` and answers
immediately; the UI follows the normal `/transfer/status` poll with
`action='delete'`, so the existing progress modal becomes a delete monitor:

- The bar is **indeterminate** (`.progress-fill.indeterminate`, a sweeping
  highlight) because a delete has no known total — counting the tree first would
  double the card I/O for a number nobody needs. `countText` shows «تم حذف N».
- `force_delete_tree(progress_cb=...)` calls the callback **per entry**, which is
  also the stall heartbeat; the worker throttles the UI writes to ~4/s.
- When the tree survives, `shizuku_delete()` runs `rish rm -rf` once as the last
  resort (works for `Android/data` and `obb`). `shlex.quote` on the path, and the
  result is only accepted when a follow-up `[ -e ]` says it is gone.
- Failure is reported as data, not prose: `delete_failures=[{path, reason}]` plus
  `card_damage` land in the state and open the detailed list modal.
- A completed delete **prunes** its entries out of the instant-search index
  (`prune_cache_for_path`) instead of discarding the whole folder index; only a
  partial delete, whose survivors are unknown, falls back to invalidation.

## Search scope (the folder the search runs in)
The search used to walk `currentSourceRoot` silently. It now walks
**`searchScopePath`**, which is its own value and its own localStorage key
(`sdmp3_search_scope`) — narrowing a search must never move the source root.

- `setSearchScope(path, remember)` is the only writer: it normalises the path,
  writes the label (`#searchScopePath`), reloads the sub-folder menu and schedules
  a UI save. `searchDeep()` sends it as `&root=`.
- The folder picker is a **dismissible dialog** (`#searchScopeModal`), opened by
  `#scopeToggleBtn` in the always-visible `.route-joc-row` beside «إنشاء فهرس
  JOC» via `openSearchScope()`, and closed by `closeSearchScope(event)` (backdrop
  click, «إغلاق», or Esc). Inside: `#searchScopePath`, `#searchSubfolder`,
  «أعلى» (`searchScopeUp`), «تحديد مجلد» (`openPathBrowser('search')`, the third
  `pickerMode`) and «المجلدات فقط». It sits **after** `#progressModal` in the
  DOM so the Esc handler (which closes the last open `.modal`) closes this dialog
  first and never the copy dialog. The old `#searchScopePanel` block,
  `toggleSearchScope()`, the `#searchScopeItems` list and `SCOPE_FILES_STEP` are
  gone. Within the search UI `/api/items` now feeds only `#searchSubfolder` (the
  browse tree and the path picker still use it), and it loads when the dialog
  opens.
- `#searchSubfolder` is a `<select>` of the folder's child folders, rebuilt only
  when a folder loads — never on a keystroke.
- `enterSearchSubfolder(name)` joins one segment onto the current scope; the
  select resets to its placeholder so the same folder can be chosen again.
- Search results are **not** a lookalike panel: `searchDeep()` puts them in
  `#treeContainer` behind a sticky `#searchViewHead` with a back button. Both
  browsing and searching go through `appendItemRows()` → `itemRowHtml(item, opts)`,
  so folder toggles, checkboxes, rename, delete and file sizes behave
  identically; `exitSearchView()` restores the last browsed folder and the last
  query is re-run after a mutation. `/api/search` returns `size` on file hits
  for that shared row.

## Search cache
The instant-search index (`FOLDER_CACHE`) is mirrored to
`state_file('search_cache.json')` (`~/.local/state/sdmp3/`, outside the checkout,
next to `instance.json`/`sdmp3.log`) after every build and on a 2s debounce after
invalidation. It is restored in `main()` on startup, and roots that no longer
exist (card pulled out) are dropped. `joc_source_root` / `joc_last_dest` are in
`localStorage`, so an F5 or a restart returns to the same folder *with* its
index — that is why `checkCacheStatus()` is called at load.

## Settings, themes and search history

Three JSON files, all written through `sdmp3_state.py` (atomic `tmp` +
`os.replace`, chmod 0600, a corrupt file is moved to `.bak` and defaults are used):

| File | Contents |
|------|----------|
| `state_file('settings.json')` | `{version, theme, font, font_size, auto_spoof, updated}` — schema version 2 |
| `state_file('index_settings.json')` | the JOC index settings — schema version 2, see «Index settings» |
| `state_file('search_history.json')` | `{items: [last 25 results], queries: [last 25 queries]}` |

All three are loaded in `main()` (so the first request already has them) and served
by `GET/POST /api/settings`, `GET/POST /api/index_settings`, `GET /api/history`,
`POST /api/history/clear`, `POST /api/history/remove`.

`POST /api/settings` takes **any subset** of `theme`, `font`, `font_size` and
`auto_spoof`: a missing key keeps its stored value, an unknown id is a 400, and
a bad boolean is dropped (old files default `auto_spoof` to `false`), so
`settings.json` can never hold a value the page cannot render.

**Auto-spoof (v2 additive setting).** The saved General switch is the consent —
there is no per-copy prompt, so it also works with the browser closed. When it
is on, `copy_thread_task()` runs `count_files()` then `spoof_tree()` **inside the
same worker**, after the final flush and verification. It spoofs the whole
destination, skipping the selected source paths, protected paths and symlinks;
a failure or cancellation keeps the copy `status='done'` and reports
`spoof_total` / `spoof_done` / `spoof_error` / `spoof_cancelled` (error is `''`
on success). Phase `spoofing` renders «تم تمويه N» in the progress modal.

### Index settings (`index_settings.json`)
The JOC form lives in الإعدادات › الفهرس and is written **only** by its
«حفظ إعدادات الفهرس» button (`saveIndexSettingsNow()` → `POST /api/index_settings`).
`joc_start`, `joc_limit`, `joc_mode`, `joc_template`, `joc_strip`,
`joc_download_txt`, `joc_review_txt`, `joc_mp3_index`, `mp3_to_dest`, `tts_voice`,
`tts_rate`, `mask_mode`.

- Validation is `_coerce_index_settings()`: ints are clamped
  (`joc_start` 1..999999, `joc_limit` 10..5000), enums are whitelisted
  (`joc_mode` 1/2/3, `joc_template` 1..4, `tts_rate` in `TTS_RATES`, `mask_mode`
  none/noise/file), booleans accept real bools, 0/1 and `'true'/'on'/'yes'`.
  A value that does not fit is **dropped** (the previous value stays) — the file
  is user-editable and one bad number must not make the form unreadable.
  The route echoes the canonical settings so the form shows what was written.
- The **output name is not part of this shape** (older files may still carry a
  `joc_filename`; it is ignored, never rewritten). It is generated by
  `openJocModal()` per creation (`makeRandomName(5)`) and stays editable until
  «إنشاء». `currentJocTxtName` remembers the name the index was built with.
- The create dialog (`#jocModal`) is now just the name field plus
  `renderJocSummary()` — a read-out of the saved settings — with buttons
  «الإعدادات» (`openIndexSettings()`), «إلغاء» and «إنشاء». `submitJocIndex()`
  does **not** save settings (that was the old, confusing behaviour).

### Theme registry (`sdmp3_themes.py`)
A theme is one dict of palette colours; **every** alpha variant (`--accent-soft`,
`--kind-sdcard-tile`, `--pill-bg`, `--shine`, `--shadow-*`, …) is derived in
`build_tokens()`, so a theme can never drift out of sync with the CSS. **16
themes**: the 14 v1.9 names (`coffee-blues` default — no longer byte-identical
to the old hand-written `:root` — `midnight-teal`, `slate-sand`, `olive-grove`,
`clay-rose`, `graphite`, `night-oxford`, `deep-charcoal`, `paper`, `sandstone`,
`clear-dawn`, `pearl-breath`, `github-dark`, `github-light`) hue-rotated in
v1.10, plus `forest-mist` (light sage) and `dusk-plum` (dark plum). All 16 keep
66 derived tokens and pass the contrast contract at import.

- A **dark** theme uses light *fills* (`success_fill`, `danger_fill`,
  `sand_fill`) with a dark label (`on_fill`/`accent_ink`); that is why
  `github-dark` sets `on_fill '#0d1117'` and keeps `success #3fb950` as the
  fill. A dark fill with a white label is what the contrast contract rejects
  (`label on sand`), not the bright hue itself.

- **Adding a theme is one dict** in `_THEME_DEFS` with the `BASE_KEYS`. Nothing in
  `project.py` changes. Run `python sdmp3_themes.py`: it prints every contrast
  ratio and the module **raises at import** if any theme breaks `CONTRAST_RULES`
  (text ≥ 7:1 on page/card, muted/labels/kind-inks ≥ 4.5:1).
- The layout tokens `--rule`, `--field-line`, `--bar-bg`, `--bar-line` and
  `--num-font` are derived in `build_tokens()` as well, so the `:root` block is
  always complete (66 tokens per theme) and a theme dict still holds palette
  colours only — never add one of these to a theme by hand.
- Tokens are rendered **server-side** into `:root` (`theme_css_pairs`) plus
  `<html data-theme>` and `<meta name="theme-color">` (`theme_color` = `--bg`),
  so there is no flash of the default theme. The `/` route passes `active_theme`,
  `theme_css`, `theme_bg`, `themes` (and the typography vars: `font_css`,
  `app_font`, `font_scale`, `fonts`, `font_sizes`, `active_font`,
  `active_font_size`); do not add a cache header to `/`.
- Switching a theme in the settings modal only assigns the returned tokens on
  `documentElement.style` (`applyTheme` in the page JS). No reload, no re-render.
- **Never hard-code a colour in the template** — not in CSS and not in inline JS
  (`showInlineNotify` uses `--ok-soft`/`--danger-soft`). A new colour goes into the
  registry. This is what makes the light themes actually light.

### Search history
Only a **committed** search is recorded (`remember=1`, sent by Enter / the بحث
button). The 400 ms typing debounce calls the same endpoint without it, otherwise
the list would fill with `م`, `مج`, `مجل`. Each search contributes its top
`HISTORY_RESULTS_PER_SEARCH` (10) results; items dedupe by `path`, queries by `q`,
both capped at 25 (`HISTORY_ITEM_LIMIT` / `HISTORY_QUERY_LIMIT`). Startup prunes
items whose `root` no longer exists (`prune_history_to_roots`) — never per request,
which would stat a sleeping card. The UI block (`#historyBlock`) is rendered by
`renderSearchHistory()` from `/api/history` and is hidden when empty; clicking a
chip re-runs that query, clicking a row adds the path to the selection, and every
mutation returns the new list so client and file cannot drift.


## TTS specifics

- Chunks are synthesised **in parallel** (`TTS_PARALLEL_CHUNKS = 4`): Edge TTS is
  network-bound, and chunks land in numbered `chunk_XXX.mp3` files, so the merge
  order — and therefore playback order — is unchanged. This is the only place
  concurrency is welcome; the copy engine stays single-threaded.
- `combine_mp3_chunks()` runs through `_run_ffmpeg()`, which heartbeats and makes
  Cancel effective mid-merge; a cancelled/failed merge deletes its partial MP3.
  It calls `setup_tts_cache()` itself, so it also works outside a TTS run.
- `chunk_*.mp3` are removed after every run (finally block); a full index used to
  leave a copy of itself in `/data` until the process exited.
- **MP3 straight into the destination** (`mp3_to_dest`, saved index setting,
  default on): `/api/tts_index` takes `save_to_dest` + `save_dir`, validates that
  the folder exists, is writable and has no file of that name, then runs
  `check_destination()` before ffmpeg starts. `tts_thread_task(..., dest_dir)`
  makes the final path `dest_dir/out_name`, so ffmpeg builds the MP3 **in place**
  — no copy, no download, playback order untouched. The result travels in the
  state snapshot as `tts_name` + `tts_saved_path` (`''` when it went to the temp
  cache). `downloadTtsFile(snapshot)` branches on it: with a path it refreshes
  the destination listing and offers no download; otherwise it clicks the
  `/api/tts/download` link as before. Existing file → the request is refused, a
  run never silently overwrites.
- `ttsAvailable` mirrors `/api/tts/voices.available`. When `edge-tts` is missing
  the switch is disabled and `#ttsNotice` explains why — `#ttsOptions` is **not**
  replaced, because its controls are part of the saved form and destroying them
  made «إنشاء» throw.

## TTS prerequisites
- `edge-tts` is **optional** (import check sets `EDGE_TTS_AVAILABLE`; TTS-only features). Copy/JOC/spoof work without it.
- `ffmpeg` is required for combining/encoding MP3 chunks and for obfuscation.
- Long indexes are split by `chunk_index_text` (line ~481) and merged to 64kbps mono via ffmpeg.

## JOC index → TXT → MP3 data flow (no on-disk .txt)
The index text is generated in-memory by `/api/joc_index`, held in the JS var `currentJocIndexText`, and passed to `/api/tts_index` as `index`. There is **no server-side .txt file** for the flow. The index settings it is built with come from the «الفهرس» pane (loaded once at startup into `savedIndexSettings`), not from the create dialog.

### Review/edit-text gate (recent feature)
A `jocReviewTxt` checkbox (in الإعدادات › الفهرس) defers txt-download/copy/MP3 actions. When enabled, after generation it opens an editor modal; **`saveEditTxt()` writes the textarea back to `currentJocIndexText`** before `runPendingJocActions()` sends it to `/api/tts_index` — so the MP3 reflects the edited text. `pendingJocActions` carries `{txtName, downloadTxt, doMp3, mp3Name, voice, rate, obfuscate, saveToDest, saveDir}`; `currentJocTxtName` is set at creation so the copy/notify text names the right `.txt` even if the field changes later. Cancel keeps the text and shows a success notify with reopen/copy buttons rather than auto-running MP3.

## Behavior notes
- File/dir traversal and copying intentionally use DFS + natural sort so hardware players replay in order.
- Timestamp spoofing processes children before parents (bottom-up). With
  `auto_spoof` on it runs in the copy worker itself, after verification.
- Wake lock is acquired during transfers via Termux (`acquire_wakelock`);
  `release_wakelock()` is guarded by `WAKELOCK_ENABLED`, so `--no-wakelock`
  never releases a lock another Termux session owns.
- **A copy lives in the Termux worker, not in Chrome.** Closing the browser (or
  the progress dialog) does not stop it and costs no throughput: the writer is
  one sequential thread either way. `/transfer/status` is a *view* of that
  worker; `worker_alive` (bool, in every snapshot) is informational and never
  drives progress. Regression proof: a harness POSTs `/transfer/start`, never
  polls, joins `ACTIVE_THREAD`, and finds `status='done'` in `transfer_state`.
- **The progress dialog is minimizable.** `#minimizeProgressBtn` calls
  `minimizeProgressModal()` (`display:none`, `progressMinimized = true`), so the
  page can be browsed and the next batch selected during a copy; a running poll
  must never reopen it (the auto-open is gated on `!progressMinimized`).
  `restoreProgressModal()` brings it back from the action bar's primary button,
  whose label becomes «استعادة النسخ NN٪» while `transferRunning` — and a
  terminal snapshot (`done`/`error`/`cancelled`) restores it once by itself,
  because verification and the error list only exist there. There is no
  `.background-note` any more.
- **Queue hook, not a queue.** `transferQueue = []` + `renderQueue()` fill
  `#queueBlock`/`#queueRows`/`#queueCount` and the `#barQueueBadge`; the block is
  hidden while the list is empty and **nothing enqueues yet**. The scheduled-copy
  batch only has to push `{label, sizeText, …}` and call `renderQueue()`. Backend
  and copy engine stay untouched: one file at a time.
- Icons are one SVG sprite plus `spriteIcon(id)` for generated markup. Text
  glyphs (`▶`, `▼`, `↑`, `✎`, `✕`, `✓`) are not used as icons.
