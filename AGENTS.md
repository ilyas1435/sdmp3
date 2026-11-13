# AGENTS.md

Android/Termux Flask app that copies + timestamp-spoofs MP3s to SD cards, generates JOC text indexes, and converts them to audio via Edge TTS. Runs entirely on the device.

## Single-file app
Everything lives in **`project.py`** (no package split): Flask backend, HTML, CSS, and — critically — **all page JS in ONE inline `<script>` block** defined inside the Python string `HTML_TEMPLATE` (starts ~line 559).

- Run: `python project.py` (serves `0.0.0.0:5050`, **not** the Flask default 5000). UI at `http://localhost:5050` / `<device-ip>:5050`.
  Flags: `--host H`, `--port P`, `--no-wakelock`, `--version` (parsed in `main()` at the bottom of `project.py`).
- No test framework, linter, or package manifest. Verify via `python -m py_compile project.py sdmp3_cli.py`, `bash -n install.sh`, and manual browser testing.
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
| `~/.local/state/sdmp3/` | `instance.json` (pid/port/host) + `sdmp3.log` for `start`/`stop`/`status`. Kept outside the checkout so `git pull` stays clean. |

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
`VERSION` is rendered into the header as `v{{ app_version }}` and also served by
`/api/system` together with the Shizuku state and SD write access; the JS
`loadSystemInfo()` fills the pills (version / `Shizuku` / `كتابة الكارت`). Bump
`VERSION` for user-visible changes.

## CRITICAL: escape handling inside HTML_TEMPLATE
`HTML_TEMPLATE` is a **plain `"""..."""` Python string**, not raw and not an f-string. Python processes backslash escapes in the embedded JS at parse time. To emit a literal backslash sequence to the browser you must **double the backslash**:
- Write `txt.split("\\n")` in Python so the browser receives `split('\n')`. A single `\n` becomes a real newline and **breaks the whole script block**.
- Same applies to `\.`, `\uXXXX` regex/unescape tokens: use `\\.`, `\\uXXXX` in the Python source.

## One bad JS token disables the entire UI
Since all JS is a single `<script>`, one syntax error kills every handler — symptoms are "no storage shown, no folders, can't select anything." `python -m py_compile` will NOT catch JS errors (the JS is just a string to Python). To lint the JS: render/extract the `<script>` contents (with Jinja `{*...}` stripped) and run `node --check`.

## Copy engine — do not regress these
The copy path is the product, and it is tuned; changes must keep the queue
byte-identical (DFS pre-order + `natural_sort_key`) and stay single-threaded.

- **`plan_transfer()`** builds the queue *and* the totals in **one** traversal.
  The old code walked the tree twice (`scan_source` then `build_dfs_queue`), which
  doubled the FUSE round-trips. It returns `(tasks, total_files, total_bytes,
  skipped)`; `skipped` reports unreadable source folders instead of dropping them
  silently (`except Exception: pass`).
- **`copy_file_sequential()`** opens src/dst with `os.open`, uses a 4 MB buffer,
  `posix_fadvise(POSIX_FADV_SEQUENTIAL)` hints and a partial-write loop. On any
  error it deletes the half-written destination file so playback order can't be
  polluted.
- **No `fsync` per file.** The transfer calls `os.sync()` every `SYNC_THRESHOLD`
  (256 MB) and once at the end. Ordering comes from *sequential writes*, never
  from fsync — the per-file fsync was removed because it cost tens of ms per MP3
  (the main cause of "copy is very slow"). Do not add it back, and keep the
  threshold large: `os.sync()` flushes *every* filesystem on the device, so a
  small value turns one big copy into hundreds of short stalls.
- **`check_destination()`** writes a 5-byte probe with an 8s timeout before every
  copy / spoof / benchmark. A card that is mounted but dead would otherwise block
  the worker inside `write()` forever. Timed-out destinations are cached in
  `_UNHEALTHY_DESTS` for 120s so retries fail fast instead of spawning more stuck
  threads.
- **Stall detection**: the worker calls `touch_progress()` per 4 MB chunk and per
  folder during planning; `get_state_snapshot()` reports `stalled`/`stall_seconds`
  after `STALL_SECONDS` (25s) with no bytes. The UI turns that into
  «الكارت لا يستجيب». The worker itself cannot report a hang — it is blocked in a
  syscall. The card wording applies only to `action in ('copy','spoof')`; TTS is
  network-bound and never writes a volume, and reporting it as a dead card was a
  false alarm that told users to pull the card out mid-generation. Every slow
  phase must keep the heartbeat alive: `count_files(on_progress=...)` while
  counting for spoof, `touch_progress()` per TTS chunk, and `_run_ffmpeg()` (which
  polls ffmpeg every second) instead of `subprocess.run`.
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

`storage_kind()` looks the volume up in `/proc/mounts` as `/mnt/media_rw/<UUID>` and passes the device to `classify_volume_device()`. `df -h` alone cannot tell an SD card from a USB stick — both are `/storage/XXXX-XXXX`. The icons are one SVG sprite (`<symbol id="ico-*">` right after `<body>`) referenced with `<use href="#ico-...">`; the select-based pickers were replaced by `.storage-card` buttons (`chooseStorage()` / `syncStorageCards()`), and the same icons appear in the path picker badge.

Theme = **Coffee Blues** (https://www.color-hex.com/color-palette/102595): teal `#39918c`, sand `#d0b49f`, tan `#be9078`, clay `#ab6b51`, slate `#2f435a`. The darks are that slate darkened — page `--bg #151e28` (×0.45), card `--card #212f3f` (×0.70), one lift `--card-2 #2a3a4c` (×0.87) — and the slate verbatim is `--border #2f435a`. `--field #151e28` (same as the page) is the recessed surface for inputs, the tree, storage rows and progress tracks. The character is **warm sand on cool slate** (`--text #ece2d8` 10.7:1 on a card, muted `#b0a59b` 5.6:1): do not neutralise either side, or the theme dies.

Teal is the accent, with a split because the palette teal is light: `--accent #39918c` marks (icons, focus, checkbox, disk bar — 3.6:1, fine for marks but not for 11px text), while filled buttons use the darker palette teal `--accent-fill #2e7a75` with a white label (4.6:1; hover `--accent-fill-hover #276a66` 5.7:1). `--accent-ink #151e28` labels the warning/success/danger fills (7.8 / 7.4 / 4.6:1). Danger: `--danger #f43f5e` fill and `--danger-ink #fc8b9b` text (6.0:1 on a card, 5.2:1 raised). Nothing glows — no gradient, no coloured button shadow, no inset card sheen; secondary buttons stay neutral `btn-outline` so only the primary action wears teal.

Each volume kind owns a hue: `.storage-card.kind-*` sets a local `--kind`/`--kind-ink`/`--kind-soft`/`--kind-tile` used by the card's start border, icon tile, active fill, active name and "الحالي/الوجهة" badge; `.pill-kind-*` and `.kind-border-*` reuse the same variables for the picker badge, the header `sdWritePill` and the path inputs. Internal = sand `#d0b49f`, SD = bright clay `#d98b63` (ink `#dda88c`), USB = blue `#8fa9c4` (ink `#a8bfd6`), unknown = olive `#a3b18a` (ink `#b7c39f`). The four are told apart by icon + label as well as hue — the palette only has so many warm earths — and `-ink` exists because the fill colours are too dark for 11px text. Only a writable card gets its kind hue in the header pill; a refusing card stays `pill-warn`.

The spoofing button is `btn-sand` (`--sand-fill #d0b49f` on `--accent-ink`), not amber: `--warning` is reserved for text and warning pills, so no button introduces a hue the palette does not own.

## UI invariants

- **No remote assets.** The page used to load Google Fonts, which blocked first
  paint on a phone that is offline or behind a slow DNS; the font stack is the
  system face (`system-ui, … 'Noto Sans Arabic'`). Keep the page self-contained.
- Markup built with `innerHTML` must escape names/paths with `esc()` (quotes
  included). Row buttons carry no data: one delegated listener reads the path
  from the row, so a filename with `"` or `'` can't break or inject markup.
- `/` is rendered per request; do not add a cache header to it — it embeds the
  per-volume storage list.
- `theme-color` must stay `#151e28` (the page colour) so the Android status bar
  matches the app.

## Path picker (تحديد / تصفح)
`/api/sizes` returns sizes for a whole selection in one request (parents are
walked once and nested selections are counted once); use it instead of calling
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

## Search cache
The instant-search index (`FOLDER_CACHE`) is mirrored to
`~/.local/state/sdmp3/search_cache.json` (outside the checkout, next to
`instance.json`/`sdmp3.log`) after every build and on a 2s debounce after
invalidation. It is restored in `main()` on startup, and roots that no longer
exist (card pulled out) are dropped. `joc_source_root` / `joc_last_dest` are in
`localStorage`, so an F5 or a restart returns to the same folder *with* its
index — that is why `checkCacheStatus()` is called at load.

## TTS specifics

- Chunks are synthesised **in parallel** (`TTS_PARALLEL_CHUNKS = 4`): Edge TTS is
  network-bound, and chunks land in numbered `chunk_XXX.mp3` files, so the merge
  order — and therefore playback order — is unchanged. This is the only place
  concurrency is welcome; the copy engine stays single-threaded.
- `combine_mp3_chunks()` runs through `_run_ffmpeg()`, which heartbeats and makes
  Cancel effective mid-merge; a cancelled/failed merge deletes its partial MP3.
- `chunk_*.mp3` are removed after every run (finally block); a full index used to
  leave a copy of itself in `/data` until the process exited.

## TTS prerequisites
- `edge-tts` is **optional** (import check sets `EDGE_TTS_AVAILABLE`; TTS-only features). Copy/JOC/spoof work without it.
- `ffmpeg` is required for combining/encoding MP3 chunks and for obfuscation.
- Long indexes are split by `chunk_index_text` (line ~481) and merged to 64kbps mono via ffmpeg.

## JOC index → TXT → MP3 data flow (no on-disk .txt)
The index text is generated in-memory by `/api/joc_index`, held in the JS var `currentJocIndexText`, and passed to `/api/tts_index` as `index`. There is **no server-side .txt file** for the flow.

### Review/edit-text gate (recent feature)
A `jocReviewTxt` checkbox in the JOC modal defers txt-download/copy/MP3 actions. When enabled, after generation it opens an editor modal; **`saveEditTxt()` writes the textarea back to `currentJocIndexText`** before `runPendingJocActions()` sends it to `/api/tts_index` — so the MP3 reflects the edited text. `pendingJocActions` carries `{txtName, downloadTxt, doMp3, mp3Name, voice, rate, obfuscate}`. Cancel keeps the text and shows a success notify with reopen/copy buttons rather than auto-running MP3.

## Behavior notes
- File/dir traversal and copying intentionally use DFS + natural sort so hardware players replay in order.
- Timestamp spoofing processes children before parents (bottom-up).
- Wake lock is acquired during transfers via Termux (`acquire_wakelock`).