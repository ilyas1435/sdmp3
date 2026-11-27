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
served by `/api/system`; `loadSystemInfo()` refreshes it. The header carries **that
one pill** — no Shizuku and no card-write claim — because a status that can be
wrong is worse than no status. Both moved to الإعدادات › عام:

- `#generalShizukuStatus` — a read-only row: غير مثبّت / متوقف / مفعّل from
  `/api/system`. Shizuku stays optional; the note under the rows says so.
- `#generalCardStatus` — filled **only** by a real write test
  (`/api/benchmark`, button «فحص الكارت» in the destination picker), stored in
  `localStorage sdmp3_card_check` as `{path, at, mbps|error}` and rendered by
  `renderCardStatus()`. `/api/system`'s `sd.writable` is deliberately **not**
  shown any more: it is a 5-byte, 5-second probe that reports a healthy but slow
  card as dead. Before any test the row says «لم يُفحص بعد».

Bump `VERSION` for **every** change, even a single new field or a one-line CSS
tweak — one commit/change, one bump. Never bundle a later fix into an old version.

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
  - **Bounded re-check on suspicious stats** (`VERIFY_RECHECK_SECONDS` = 2s):
    Android FUSE can briefly answer a `stat()` with a stale size (often 0) or
    `ENOENT` for a file written seconds earlier, and a volume can be inaccessible
    for a moment right after `syncfs()`. A single stat was therefore reporting
    correct files as «الحجم على الوجهة 0 بايت» / «غير موجود». When a first look is
    suspicious, verification gives the path one fresh fd-based look (`os.open` +
    `os.fstat`, which forces FUSE to refresh attributes) after one **shared**,
    cancel-aware pause — one sleep for the whole pass, not one per file. A file
    that is genuinely wrong still fails after it; no real mismatch is suppressed.
    Logged as `verify recheck resolved N stale destination stat(s)`.
- **`copy_file_sequential()`** opens src/dst with `os.open`, `posix_fadvise(POSIX_FADV_SEQUENTIAL)` hints, then uses `os.sendfile` (4 MB chunks) when the kernel/filesystem supports it; on `EINVAL`/`ENOSYS`/`EXDEV`/`EOPNOTSUPP`/`EAGAIN` it continues with the 4 MB buffered loop from the same offset. sendfile measured ~2× faster on Android FUSE. Both paths write front-to-back, and on any error the half-written destination file is deleted so playback order can't be polluted.
- **No `fsync` per file.** The transfer calls `sync_with_heartbeat()` every `SYNC_THRESHOLD` (256 MB) and once at the end. The threshold is also the length of the single «تثبيت البيانات على الكارت…» step: 1 GB of dirty pages on a card writing at 10 MB/s was a 100-second blocking `syncfs`, which users read as a hang. 256 MB caps that step at ~25 s worst case on a slow card and still flushes once per ~1 GB of a real internal copy. Ordering comes from *sequential writes*, never from fsync — the per-file fsync was removed because it cost tens of ms per MP3 (the main cause of "copy is very slow"). Do not add it back, and keep the threshold large: a small value turns one big copy into hundreds of short stalls.
  - The flush **does not rewrite `file`**. `sync_with_heartbeat()` sets
    `phase='syncing'` / `syncing=True` only, so the current line stays «track.mp3»;
    the UI appends «— تثبيت البيانات على الكارت (N ث)» once `sync_seconds >= 3`
    and leaves a fast flush alone. Writing the sentence into `file` made it
    appear over and over for every ordinary flush.
  - `sync_with_heartbeat(phase, dest=...)` flushes with **`flush_destination(dest)`**:
    `syncfs(fd)` from libc via `ctypes` (one filesystem) and only falls back to
    `os.sync()` (every filesystem on the phone) when libc has no `syncfs`. The
    copy worker always passes `dest`; spoof/delete still use the global flush.
- **Honest speeds and times.** Copy time and verify time are measured separately:
  `copy_seconds` ends after the final flush, `verify_seconds` covers only
  `verify_copied_files()`. `avg_mbps = bytes / copy_seconds` (verify never
  inflates it), `speed_mbps` stays the 6s-window EMA. The progress dialog shows
  **one** figure — «المتوسط N MB/s» (plus the server's remaining time) — and the
  same average appears in the report's «النسخ» row. Printing the instantaneous
  rate beside it was removed: it dips with every small file and made the honest
  number look wrong. Do not hide either measurement: the cost is ~2 ms/file for
  verify and negligible CPU for the display, and without them a slow card is
  indistinguishable from a slow app.
- **The end-of-run report is rows, not a paragraph.** `#timeTakenDiv` is
  `.report` and is filled by `buildTransferReport(data)` → `renderTransferReport()`
  (`checkTransferStatus` done branch), never by string concatenation: a row is
  `reportRow(iconId, label, value, tone)` — a sprite icon, a muted label and a
  bold value pushed to the far end — and a row with nothing to say is simply not
  built. The shape is one headline row (icon + «اكتمل النسخ» / «تم الحذف» /
  «تم إنشاء الفهرس الصوتي» / «اكتمل التمويه» + count), then «النسخ»
  (`copy_seconds` · `avg_mbps`, one row because they are two halves of the same
  statement) or «الزمن» for the non-copy actions, «التحقق من الحجم»
  (`verified/total` + `verify_seconds`, `warn` when cancelled, `bad` when
  mismatches exist), «تمويه التواريخ» when it ran, and the saved file name for a
  TTS run. `tone` is `ok`/`warn`/`bad` and only colours the icon. A cancelled run
  is one `warn` row with its time; failures keep the detailed `#errorsBox`, which
  is the only place paths and reasons belong. **The report is the only completion
  summary**: `finish_transfer()` puts the one-sentence summary in the state's
  `file`, and `checkTransferStatus()` used to echo it into `#currentFile` under
  the rows — the same facts twice. Every terminal state now blanks
  `#currentFile` (`data.status !== 'copying'`), so the icon rows are all the user
  reads. The same `report` renderer also serves the card check.
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
  measured flush latency and always looked slow. It is the **card check** now:
  the UI button «فحص الكارت» (`runDiagnostic()`, destination picker footer) calls
  it with the folder being browsed, shows the result as `report` rows (speed,
  estimated 10 GB time, folder) instead of a blocking `alert()`, and records it in
  `localStorage sdmp3_card_check` — that record is the only card-write status the
  app shows.
- Expected numbers on this device: ~180 MB/s (5×40 MB files) and ~28 MB/s for
  150×160 KB MP3s on internal storage. Small-file speed is bound by per-file FUSE
  create/close, not by the buffer size.

### Selected `.zip` archives: unpacked by the copy worker, one write pass
A `.zip` the user selects in the source is **not copied**: it is unpacked into
`<dest>/<archive name>` (the basename without `.zip`), or
`<dest>/<group>/<archive name>` when it was assigned to a copy group. The bytes
are decompressed in 4 MB chunks and written straight to the card by that same
single-threaded worker — no staging folder and no second copy, so the card is
written once (`extract_zip_to_dest` → `copy_stream_sequential`). This is why the
feature lives in the worker and not in the route: a 2 GB archive must show
progress, stay cancellable and be reported like any other transfer.

- Members are sorted with `natural_sort_key` before writing, so the FAT
  directory-entry order (playback order) is the folder order the user sees.
  `zip_member_infos` drops macOS/Windows junk (`__MACOSX/`, `._*`, `.DS_Store`,
  `Thumbs.db`, `desktop.ini`); written to a card it would sit between tracks.
- `plan_transfer` emits a `('dir', folder)` task followed by a `('zip', folder)`
  one, so the folder exists before its contents and the archive is a single
  queue entry. Totals use each member's **uncompressed** size, so the progress
  bar, the ETA, the free-space check and the 48 MB speed probe stay honest.
- Only **top-level selections** expand: a zip inside a selected folder is copied
  as a file (`walk(..., expand_zip=False)` for children), so copying a tree
  still reproduces that tree exactly.
- `verify` size-checks the members like any planned file. With `verify_content`
  they are size-only (`size_only=zip_targets`) — there is no source file to hash,
  and reporting that as a mismatch would be a lie.
- Encrypted archive → `ZipArchiveError` **before** anything is written. Corrupt
  or unreadable archive → reported as `skipped` while planning (or in
  `failed_details` if it dies mid-unpack) and the rest of the transfer continues;
  a failure that left the folder empty removes it (`_remove_if_empty_dir`).
- Two archives with the same base name are refused: they would unpack into one
  folder and mix two trees. Every member path is confined to the extraction
  folder — `zip_member_target` rejects `..`, Windows drive paths and anything
  resolving outside it, and an absolute member is stripped *into* the folder.
- Merge: an existing `<dest>/<archive name>` folder means merging, so the route
  answers `needs_extract_confirm` **before anything is written**
  (`confirm_extract` from the UI), the same pattern as `needs_group_confirm`.
  A *file* with that name is refused outright. `startTransfer(confirmMerge,
  confirmExtract)` passes both flags explicitly; never mutate a request object
  and re-call, the rebuilt body loses it.
- An archive root is exempt from `copy_targets_source` (its target is a folder,
  never the file) but **never** from `dest_inside_source`. A *folder* named
  `x.zip` stays a folder — `selected_archives()` = suffix **and** is-file — so it
  keeps the full self-copy guard.
- The end-of-run report gets one «فك الضغط» row from `zip_archives`/`zip_files`,
  and the progress line names the archive while it unpacks
  («… — فك الضغط 12/40»); `publish_progress()` is throttled and called per chunk
  so a single big archive does not freeze the dialog.
- The file browser marks `.zip` rows with `ico-zip` via `fileIcon(name)` (same
  glyph in tree, search results, destination picker and the selection list).

## Storage detection (server-side, not JS)
Options are rendered at route `/` via Jinja `{% for st in storages %}`, backed by `get_available_storages()`: always `/sdcard` (internal), plus external SD cards found from `df -h` `/storage/...` mounts (emulated mounts are skipped). If the list is empty, check this Python function — not the JS.

Each storage dict carries `kind` + `icon` so the UI can label it honestly:

| kind | How it is detected | Icon / label | Colour (palette) |
|---|---|---|---|
| `internal` | `/sdcard` | `ico-internal` — الذاكرة الداخلية | `--kind-internal` sand `#d0b49f` |
| `sdcard` | `/proc/mounts` device major **179** (MMC) | `ico-sd` — كارت SD (UUID) | `--kind-sdcard` bright clay `#d98b63` (ink `#dda88c`) |
| `usb` | device major **8** (SCSI/USB OTG) | `ico-usb` — USB OTG (UUID) | `--kind-usb` blue `#8fa9c4` (ink `#a8bfd6`) |
| `card` | anything unrecognised (dm-*, unknown fs) | `ico-card` — كارت ذاكرة (UUID) | `--kind-card` olive `#a3b18a` (ink `#b7c39f`) |

`storage_kind()` looks the volume up in `/proc/mounts` as `/mnt/media_rw/<UUID>` and passes the device to `classify_volume_device()`. `df -h` alone cannot tell an SD card from a USB stick — both are `/storage/XXXX-XXXX`. The icons are one SVG sprite (`<symbol id="ico-*">` right after `<body>`) referenced with `<use href="#ico-...">`; the pickers are `.storage-card` **icon tiles** in one horizontal row (`chooseStorage()` / `syncStorageCards()`), captioned with `STORAGE_KINDS[*]['short']` (الهاتف / SD / OTG / كارت) that `get_available_storages()` copies onto each dict — no UUID name, no raw path (the full name + path are in its `title`). A tile is `flex: 0 0 auto` with 5px/6px padding around a 32px icon, i.e. barely bigger than the icon: three volumes must not fill the row, and the whole row is centred so the pair hugs the arrow. The kind hue is always present but **only the selected tile wears it at full strength**: an unselected `.storage-ico` is `--kind` at `opacity:.42` on `--field`, the active one switches to `--kind-tile` at `opacity:1`, and only the active caption uses `--kind-ink`. The single badge a tile can carry is the cache bolt (`.storage-fresh`, `is-fresh` on the active **source** tile only, painted by `renderCacheStatus`/`syncFreshBadge`) — it is not a volume badge.

Theme = **Coffee Blues** (https://www.color-hex.com/color-palette/102595) is still the default of the 16-theme registry, but v1.10 hue-rotated it while preserving relative luminance: page `--bg #192133`, card `--card #222b40`, lift `--card-2 #2d374d`, `--field #161c2b`, `--border #2f3c5a`, `--text #e9e2d1`, muted `#aaa297`, accent `--accent #3d8e9c`, accent fill `--accent-fill #327883` (hover `#2a6973`). The warm-sand-on-cool-slate character survives (text ≈ 10.9:1 on a card, muted ≈ 5.6:1); only the hues moved, so contrast stayed inside the contract. `--radius` is 12px; `--rule` (translucent `--border`) is the hairline between sections, `--field-line` outlines inputs/cards, `--bar-bg`/`--bar-line` belong to the opaque action bar, and `--num-font` (`NUM_FONT_STACK`) is the Latin mono face used by `.num`. **No colour literal belongs in the template** — every token, alpha variant and layout colour comes from `sdmp3_themes.py`.

The accent split stays: `--accent` marks (icons, focus, checkbox, disk bar, small status text only where the ratio allows), while filled buttons use `--accent-fill` with `--on-accent`. `--accent-ink #151e28` labels warning/success/danger fills. Danger (`--danger #f43f5e`, text `--danger-ink #fc8b9b`) and success (`--success #7fbf93`) are semantic and were deliberately **not** rotated in v1.10, along with the four `--kind-*` volume hues, so their meaning survives every theme. Nothing glows — no gradient, no coloured button shadow, no inset card sheen; secondary buttons stay neutral `btn-outline` so only the primary action wears the accent.

Each volume kind owns a hue: `.storage-card.kind-*` sets a local `--kind`/`--kind-ink`/`--kind-soft`/`--kind-tile` used by the tile's border, icon tile, active fill and active caption; `.pill-kind-*` and `.kind-border-*` reuse the same variables for the picker's volume badge and the path inputs. Internal = sand `#d0b49f`, **SD = cyan** (`#2eb9cb` / ink `#8fdde9` in `coffee-blues`; `#0e7490` in the light themes), USB = blue `#8fa9c4` (ink `#a8bfd6`), unknown = olive `#a3b18a` (ink `#b7c39f`). SD used to be a second warm clay, which made it read as internal storage on the same row — `KIND_HUE_RULES` in `sdmp3_themes.py` now refuses any theme whose internal-vs-SD hue gap is under 45° (actual minimum 143°), so the two can never blur again. The four are still told apart by icon + label as well as hue, and `-ink` exists because the fill colours are too dark for 11px text. The header no longer carries a card pill at all: the card answer is a measured `#generalCardStatus` row in الإعدادات › عام.

The spoofing button is `btn-sand` (`--sand-fill #d0b49f` on `--accent-ink`), not amber: `--warning` is reserved for text and warning pills, so no button introduces a hue the palette does not own.

## Page layout (v1.9.0 — design, not features)

One document, not a stack of boxes. The page is `<header class="app-header">`, then
three `<section class="section">` blocks separated by a hairline (`.section + .section
{ border-top: 1px solid var(--rule) }`), each with `.section-head` / `.section-title`
/ `.section-kicker`. There is **no `.card`** and no `.card-title` any more.

- **Header**: 36px badge, `h1` at 17px, no tagline, and exactly **one** pill:
  the version (`#appVersionPill`, accent text). Shizuku and «كتابة الكارت» are not
  header material — a status that can be wrong out-shouts the page for nothing —
  so they are rows in الإعدادات › عام (see «Version + status in the UI»). `.pill`
  outside the header (picker badge, group chip, action-bar badge) keeps the
  tinted-chip look.
- **Route section**: one always-visible `.route-row`, no `<details>`/`<summary>`
  and no heading at all. It is a centred flex row — `.storage-grid`
  `#source_storage_cards` → `.route-arrow` → `.storage-grid`
  `#dest_storage_cards` — not a `1fr auto 1fr` grid, so both groups sit next to
  the arrow instead of in the two corners of a wide bar (`justify-content:center`
  plus a `safe center` fallback for the rare four-volume phone, where the row
  **wraps**). The row must never set `overflow`: any overflow on this container
  clips `.storage-fresh` — the cache bolt sits at `top:-7px` on the source tile
  and was cut in half once the icons grew. The words «المسار» / «المصدر» / «الوجهة» are gone from this
  block (the section has no `.section-head`): the captions under the icons are
  the only labels. There is **no source path** in the UI — the source is the
  folder open in the browser (`currentSourceRoot`), which is also where
  `openPathBrowser('source')` starts. Under the tiles sits `.dest-line`, centred:
  one `.dest-path` row (`#destPathRow`, `role="button"`) holding a folder icon,
  the **read-only** `#custom_dest_path` and a chevron. There is no «تحديد»
  button and the input is not editable (`readonly`, `tabindex="-1"`,
  `pointer-events:none`): tapping anywhere on the row runs
  `openPathBrowser('dest')`, and the same works with Enter/Space on the row.
  `syncStorageCards()` tints `#destPathRow` with the volume's
  `kind-border-*`, never the input. `#diskSpaceDiv` stays under it.
  `chooseStorage()` / `syncStorageCards()` are otherwise unchanged.
- **File browser section**: the sort row, the search row (`#searchControls`) and
  `#treeContainer` are one attached block; search sits directly above the
  results, so typing never leaves the input far from them. The row is the input
  plus **two** 36px `.icon-btn.search-tool`s on one non-wrapping line: a clock
  (`#historyToggleBtn` → `toggleHistoryBlock()`) and a sliders icon
  (`#scopeToggleBtn` → `openSearchScope()`, the folder and options the search runs
  in, dialog titled «إعدادات البحث»). There is **no magnifier button**: Enter
  (`.keydown` → `searchDeep(true)`) runs and commits the search, while typing
  still runs the 400 ms debounced preview. No word buttons either — «بحث» /
  «السجل» / «مجلد البحث» were costing a third of the row for symbols, and the
  magnifier was the third icon on a row that only needs two. Focusing `#searchInput` calls `this.select()` (the next keystroke
  replaces the old query) and scrolls this section to the top edge with
  `scrollIntoView({block:'start', behavior:'auto'})` — instant on purpose,
  because the phone keyboard resizes the viewport and a smooth animation would
  fight it. `revealSearchResults()` then brings the first result rows above the
  keyboard with the same instant behaviour (a 96px margin keeps the sticky head
  plus one row clear). The folder-scope *options* are still in
  `#searchScopeModal` (see «Search scope»).
  `#treeContainer` has a **fixed height** (`.tree { height: clamp(320px, 54vh,
  560px) }`), not a content-driven one: without it a search result set of a few
  rows collapsed the browser to a sliver and every folder open resized the page.
  It scrolls inside, so browsing and searching keep the same box. `--kb-inset`
  and `syncViewportInset()` are gone — the keyboard is handled by
  `revealSearchResults()` scrolling, never by shrinking the tree.
- **Tools section**: the last section of the page. **One** `.tools-actions` row
  with two index tools side by side: «إنشاء فهرس JOC» and «بحث فوري»
  (`#cacheFolderBtn`, with `#cacheStatusText` under the row). They are the same
  kind of action (build an index), so they share a row. The JOC button is the one
  accented control (`btn-joc`: `--accent-soft` fill, `--accent` border,
  `--accent-text` label, the `ico-index` sprite icon — a sheet with lines and a
  radio dot), because the index is the important tool; a filled accent is still
  reserved for «بدء النسخ». The card speed test is **not** here any more: it lives
  in the destination picker as «فحص الكارت» (`#cardCheckBtn`, `btn-sm`), where a
  folder is already chosen. Spoofing lives in `<details class="tools">`,
  closed by default — an occasional action must not out-weigh the copy — and
  **«تطبيق التمويه» runs immediately**: there is no `confirm()` at all, and an
  empty destination means `/sdcard`, exactly as the hint under the field says.
- **Action bar** (`#actionBar`, `.actionbar`): fixed, **opaque** `--bar-bg` with a
  `--bar-line` top border (never `backdrop-filter`), and a 3px
  `.actionbar-progress` line at its top edge. The left side is a button
  (`.actionbar-summary` → `openSelectedModal()`) showing `#selectedCount` and
  `#barSize`; the primary «بدء النسخ» is the only filled control. Body bottom
  padding is 112px (116px on phones) to clear it.
- `#barSize` is filled by `scheduleSelectionSize()`: a 400ms debounced
  `POST /api/sizes` whose signature (`selectionSignature()` = sorted selected keys
  + excludes) also prevents refetches; `updateSizeDisplay()` writes it. «عرض
  المحدد» is therefore optional, not the only way to see the copy size.
- **No time estimate in the action bar.** A `#barEta` used to extrapolate a
  duration from one remembered `avg_mbps`; it was wrong by a wide margin on every
  real card, so the element, `LAST_SPEED_KEY`, `rememberedSpeed`, `formatDuration`
  and `updateBarEta` are gone. Size is the only honest number before a copy.
  `mirrorProgressInBar(data)` is called from `checkTransferStatus()` and paints the
  bar's line (a delete sweeps it, verification uses `verify_progress`).
- **Icons, not emoji**: `ico-check`/`ico-alert`/`ico-plus`/`ico-spark` joined the
  sprite and `spriteIcon(id)` builds the inline `<svg><use>` for generated notices
  (`showInlineDone`/`showInlineError`, JOC text notices). Emoji carry their own
  colour and metrics and fight the theme.
- **Esc** closes the front `.modal` (the `.modal-front` one, else the last visible)
  but **never** `#progressModal`: `#progressModal` is filtered out of the
  candidate list *before* the choice (not rejected after it), because a finished
  copy can restore itself over another open dialog — rejecting it afterwards made
  Esc a silent no-op. The copy dialog is closed with its own button after the run
  ends.
- `.num` (with `--num-font`, tabular figures, `direction:ltr`) is for counts, sizes,
  speeds, timestamps and the queue row's number. `#selectedCount`/`#barSize` and
  the progress dialog's `#speedText` are written with `textContent`, never
  `innerText` (jsdom has no `innerText`, and that is the gate's environment).

## Groups window: «مجلدات النسخ», flat list, drag & drop
`#selectedModal` shows the copy folders as **one flat list** (`#groupTree`). A
group is still `{id, name, paths}` and a path lives in exactly one group; every
new folder is a single FAT segment. A legacy saved name may still contain `/`
and is displayed as a breadcrumb, but the UI never creates nesting any more.

- Rows are `.gt-row` with a twisty, a folder icon, the name, a count and two
  small actions (rename, delete). Only *closed* rows are remembered
  (`groupNodeClosed`): a folder the user just created appears expanded.
- **Renaming happens in the row** (`startRenameGroup` → `#gtRenameInput`): Enter
  saves, Esc cancels, ✓/✕ do the same. `groupRenameError` validates the segment
  (`groupSegmentError`), the whole path length and casefold duplicates, and Save
  is disabled while there is an error. No `showInputModal`, no second sheet.
- **Creating is one click**: «مجلد جديد» (`startNewGroup`) adds an **empty**
  folder named `مجلد N` (unique via `uniqueGroupName`) and opens the rename. No
  checkboxes are involved: files are moved into it by drag & drop or the chip.
- **Delete** (`deleteGroup`) removes the folder; its files stay selected and
  fall back to «بدون مجلد».
- **Assignment has two equally simple paths.** Drag & drop moves one item: rows
  in `.gt-path` and `.selected-row` carry `text/x-sdmp3-path`,
  `.gt-row[data-drop]` is the target, and `movePathToGroup(path, gid)` re-assigns
  it. The touch path is the **folder chip** under each selected file
  (`button.group-chip[data-path]`): it opens `#assignGroupModal`, one row per
  folder plus a «بدون مجلد» row and a «مجلد جديد» field. There is no
  «تحديد الكل للتجميع», no «ضم N» button, no pick bar and no «بدون مجلد» tree
  node.
- The group tree has one **delegated** click/drag listener: no group name or
  path is ever interpolated into an inline `onclick` (only `esc()`ed attributes).
  Files are never identified by a checkbox `value` any more; the selected-file
  list has no pick checkboxes at all.

## «تنظيم ذكي»: propose from the selection, preview, apply
`smartOrganizeGroups()` builds its proposal **only from `selectedPaths`** — the
files and folders the user actually checked — never from the whole source tree.
Each selected item maps to its own parent path relative to `currentSourceRoot`
(`sourceRelativeParent`), keeping the last `SMART_DEPTH` segments and falling
back to `sourceBasename()` for an item sitting directly in the root.

`SMART_DEPTH` is now the constant **1**: one folder named after the item's own
parent, not the whole original path chain. The General setting and
`pickSmartDepth` are gone; `clampGroupSegments()` still enforces the FAT/length
rules and `GROUP_MAX_DEPTH` (3) before the preview, so it can never propose a
group the server would refuse. A legacy saved `smart_depth` in `settings.json`
is still loaded/clamped by `sdmp3_state` but no longer reaches the page.

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
- **The favicon is self-hosted too.** `static/favicon.svg` draws an SD card with
  music notes in the Coffee Blues palette and is linked from `<head>` with
  `?v={{ app_version }}`, so every version bump refreshes the tab icon without
  a manual cache clear.
- **Every `font-size` is `calc(Npx * var(--font-scale))`.** A raw `Npx` silently
  ignores the size preset, so it is a bug even though it renders. Same for the
  inline `style="font-size:…"` attributes in the template — they are scaled too.
- **Markup built with `innerHTML` must escape names/paths with `esc()` (quotes
  included). Row buttons carry no data: one delegated listener reads the path
  from the row, so a filename with `"` or `'` can't break or inject markup.
- **The selected-file list has no pick checkboxes.** Rows are `.selected-row`
  with a folder chip (`button.group-chip[data-path]`) instead, so the picker's
  `.checkbox` sync (`refreshCheckboxStates()`) can never mistake a group control
  for a tree selection. Group-card and assign-sheet buttons are handled by
  **delegated** listeners reading `data-gid`/`data-act`, so no group name is ever
  interpolated into an `onclick`.
- **`modal-selected` has a fixed height** (`min(84vh, 760px)`, `92vh` on phones)
  with `#selectedList` as the flex child that scrolls, so the folder list and the
  confirm action cannot slide under the finger. The assign
  sheet is `position:absolute` inside it (`bottom:14px`) for the same reason.
  `modal-settings` uses the same fixed-height rule for its tabs.
- Group data lives only in JS: `copyGroups = [{id, name, paths}]`, one path per
  group. A legacy `name` may still be a breadcrumb (`A/B`) and is shown as one,
  but the UI never creates nesting. It is
  cleared when a copy finishes with `action === 'copy'` and when the selection
  becomes empty; cancelled/failed copies keep it for retry. It is mirrored to
  `sdmp3_ui_state_v1` (see «Remembered UI state») so a refresh keeps the cart.
- **Group names are still validated as paths** (`sdmp3_groups.py` is the
  authority): `GROUP_BAD_CHARS` deliberately does **not** contain `/`,
  depth ≤ `GROUP_MAX_DEPTH` (3), full path ≤ `GROUP_PATH_MAX` (180), each segment
  ≤ 80. The page applies the same rules in `groupSegmentError` /
  `groupRenameError`, but there is no «＋ فرع» / `startChildGroup` any more:
  new folders are one segment.
- `/` is rendered per request; do not add a cache header to it — it embeds the
  per-volume storage list, the active theme **and** the saved font/size.
- `theme-color` is the active theme's `--bg` (`{{ theme_bg }}`), not a constant:
  the Android status bar must match whichever theme is saved.

## Settings modal
`#settingsModal` (`modal-content modal-settings`, fixed height like the picker)
has **four tabs in this order**: «المظهر», «عام», «الفهرس», «حول». One pane per
tab, all in the DOM (`#settingsPaneAppearance` / `General` / `Index` / `About`);
`showSettingsTab(tab)` only toggles `display`, so a form value is never rebuilt
or lost. `openIndexSettings()` (the create dialog's button) returns to the
«الفهرس» pane in draft mode, keeping whatever is currently in the form.

- **المظهر** owns the look: `#themeSelect` **and** the typography controls —
  `#fontSelect` (12 options with `data-font-id`), the `#sizeSeg` segmented
  control (4 `.size-seg-btn` buttons with `data-size-id`) and a live
  `#fontPreview` styled with `{{ app_font }}`. There is **no `#sizeSelect`** any
  more: a four-item choice does not need a dropdown. The theme select is rendered
  server-side by Jinja from `themes` (each `<option>` carries `data-theme-id` and
  is selected server-side), so the page needs no theme JSON in JS. `pickTheme(id)`
  POSTs `/api/settings`, then `applyTheme()` applies the returned
  `tokens`/`theme_bg`, sets `<html data-theme>` and syncs the select.
  `pickFont(id)` / `pickFontSize(id)` POST one key each, apply `app_font` /
  `font_scale` from the response, toggle the active segment button and report in
  `#settingsSaveState`. No reload, no re-render.
- **عام** owns the `auto_spoof` switch (`#autoSpoofCheck` → `pickAutoSpoof`), the
  copy-verification switch (`#verifyAfterCopy`, moved here from the route; it
  still writes `sdmp3_verify_after_copy` and `startTransfer()` still reads it)
  and — at the bottom — the read-only «الجهاز» block: `#generalShizukuStatus`
  and `#generalCardStatus` (see «Version + status in the UI»). The «تنظيم ذكي»
  depth field is gone. Settings switches report in `#generalSaveState`; the two
  status rows are filled by `loadSystemInfo()` / `renderCardStatus()`.
- **الفهرس** owns the JOC index settings — see «Index settings» below. It is the
  only place they are written, and it also serves as a throw-away draft (banner,
  no save button) when «إنشاء فهرس JOC» opens it.
- `setSettingsState(text, kind, elementId)` writes one status line per pane
  (`#settingsSaveState`, `#generalSaveState`, `#indexSaveState`). The header gear
  button (`.header-settings`) opens the modal on «المظهر»; its footer «إغلاق»
  calls `closeSettingsModal()` (never a raw `display='none'`), because closing
  must also reset the index draft.

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
  writes it into the path field and schedules a UI save. `searchDeep()` sends it
  as `&root=`.
- The dialog (`#searchScopeModal`) is titled «إعدادات البحث», opened by
  `#scopeToggleBtn` (the sliders icon at the end of the search row) via
  `openSearchScope()`, and closed by `closeSearchScope(event)` (backdrop click,
  «إغلاق», or Esc). It is the **destination picker pattern**: a read-only
  `#searchScopePath` input, «أعلى» (`searchScopeUp`), «تحديد مجلد»
  (`openPathBrowser('search')`, the third `pickerMode`, which writes back through
  `setSearchScope`) and «المجلدات فقط». `loadSearchScope()` only mirrors the value
  into the field — it makes **no request**, so opening the dialog costs nothing.
  The sub-folder `<select>` (`#searchSubfolder`), `enterSearchSubfolder()` and
  `renderSearchSubfolders()` are gone: the same picker already browses every
  folder, so a second, weaker navigator was pure noise.
- The dialog sits **after** `#progressModal` in the DOM so the Esc handler (which
  closes the last open `.modal`) closes it first and never the copy dialog.
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
| `state_file('settings.json')` | `{version, theme, font, font_size, auto_spoof, smart_depth, updated}` — schema version 2 |
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
  `openJocCreate()` per creation (`makeRandomName(5)`) and stays editable until
  «إنشاء». `currentJocTxtName` remembers the name the index was built with.
- **«إنشاء فهرس JOC» opens الإعدادات › الفهرس first, as a draft.** `openJocModal()`
  calls `openIndexDraft(true)`: the pane is editable, but `indexDraftActive` is
  true, the draft banner (`#indexDraftBanner`) is shown, the save button
  (`#indexSaveActions`) is hidden and the draft actions (`#indexDraftActions`:
  «إلغاء» / «متابعة إلى الإنشاء») replace it. `saveIndexSettingsNow()` returns
  immediately in that mode, so a one-off tweak **never** overwrites the saved
  defaults — closing the pane (`closeSettingsModal`) or cancelling
  (`cancelJocDraft`) restores them via `resetIndexForm()`.
- «متابعة إلى الإنشاء» (`continueJocDraft()`) closes the settings modal and calls
  `openJocCreate()`, which generates the name, renders `renderJocSummary()` (a
  read-out of the settings the run will actually use) and opens `#jocModal`.
  That dialog is just the name field plus the summary, with buttons «الإعدادات»
  (`openIndexSettings()`, back to the pane in draft mode), «إلغاء» and «إنشاء».
  `submitJocIndex()` snapshots everything into `pendingJocActions` and calls
  `resetIndexForm()` before starting, so a draft never leaks into the next
  creation; it does **not** save settings.

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
- The layout tokens `--rule`, `--field-line`, `--bar-bg`, `--bar-line`,
  `--num-font` and the surface channels `--card-rgb`, `--card-2-rgb`,
  `--field-rgb`, `--bar-rgb` are derived in `build_tokens()` as well, so the
  `:root` block is always complete (70 tokens per theme) and a theme dict still
  holds palette colours only — never add one of these to a theme by hand. The
  channels feed `--surface-*` with the saved opacity from الإعدادات › المظهر;
  100% is the original opaque look and lower values fade cards, fields, dialogs
  and the action bar together.
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
  `check_destination()` **before** `reset_state()` or the worker exists — a dead
  folder is refused without spawning a thread. Proof lives outside the checkout
  (`$TMPDIR/uitest/test_tts.py`): it stubs only the Edge synthesiser and runs the
  real `tts_thread_task` + real ffmpeg merge into a temp destination, checking
  the merged MP3 is there and playable, `tts_saved_path` points at it, the
  `chunk_*` files are gone, and a second run with the same name is refused.
  `tts_thread_task(..., dest_dir)`
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
  whose label becomes «استعادة النسخ/الحذف/الفهرس NN٪» (the word follows
  `data.action`) while `transferRunning` — and a terminal snapshot
  (`done`/`error`/`cancelled`) restores it once by itself, because verification
  and the error list only exist there. The «تصغير» button is hidden in that
  terminal branch and re-shown by `showProgressModal()`: minimizing a finished
  run would be undone by the next poll, so the control would do nothing. There
  is no `.background-note` any more.
- **Photo background scrolls with the document.**
  `body.photo-background-enabled::before` is `position:absolute; inset:0`
  inside the already-positioned `body` — **never** `position:fixed` or
  `background-attachment:fixed`. Android resizes the visual viewport when the
  URL bar hides; even a stable-sized fixed layer still re-cropped its `cover`
  image and jittered. An absolute layer is sized to the document, so neither
  scrolling nor the URL bar can resize or re-crop it; `will-change:transform`
  keeps it on one compositor layer.
- **Queue hook, not a queue.** `transferQueue = []` + `renderQueue()` fill
  `#queueBlock`/`#queueRows`/`#queueCount` and the `#barQueueBadge`; the block is
  hidden while the list is empty and **nothing enqueues yet**. The scheduled-copy
  batch only has to push `{label, sizeText, …}` and call `renderQueue()`. Backend
  and copy engine stay untouched: one file at a time.
- Icons are one SVG sprite plus `spriteIcon(id)` for generated markup. Text
  glyphs (`▶`, `▼`, `↑`, `✎`, `✕`, `✓`) are not used as icons.
