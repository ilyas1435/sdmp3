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
  (64 MB) and once at the end. Ordering comes from *sequential writes*, never from
  fsync — the per-file fsync was removed because it cost tens of ms per MP3 (the
  main cause of "copy is very slow"). Do not add it back.
- **`check_destination()`** writes a 5-byte probe with an 8s timeout before every
  copy / spoof / benchmark. A card that is mounted but dead would otherwise block
  the worker inside `write()` forever. Timed-out destinations are cached in
  `_UNHEALTHY_DESTS` for 120s so retries fail fast instead of spawning more stuck
  threads.
- **Stall detection**: the worker calls `touch_progress()` per 4 MB chunk and per
  folder during planning; `get_state_snapshot()` reports `stalled`/`stall_seconds`
  after `STALL_SECONDS` (25s) with no bytes. The UI turns that into
  «الكارت لا يستجيب». The worker itself cannot report a hang — it is blocked in a
  syscall.
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
| `internal` | `/sdcard` | `ico-internal` — الذاكرة الداخلية | `--kind-internal` cool grey `#c4cbca` |
| `sdcard` | `/proc/mounts` device major **179** (MMC) | `ico-sd` — كارت SD (UUID) | `--kind-sdcard` teal `#3cbbb1` |
| `usb` | device major **8** (SCSI/USB OTG) | `ico-usb` — USB OTG (UUID) | `--kind-usb` violet `#b0a0f0` |
| `card` | anything unrecognised (dm-*, unknown fs) | `ico-card` — كارت ذاكرة (UUID) | `--kind-card` sage `#7f918e` (ink `#9fb0ad`) |

`storage_kind()` looks the volume up in `/proc/mounts` as `/mnt/media_rw/<UUID>` and passes the device to `classify_volume_device()`. `df -h` alone cannot tell an SD card from a USB stick — both are `/storage/XXXX-XXXX`. The icons are one SVG sprite (`<symbol id="ico-*">` right after `<body>`) referenced with `<use href="#ico-...">`; the select-based pickers were replaced by `.storage-card` buttons (`chooseStorage()` / `syncStorageCards()`), and the same icons appear in the path picker badge.

Theme = the **Coolors palette** (https://coolors.co/ee4266-2a1e5c-0a0f0d-c4cbca-3cbbb1), but the five hues deliberately get **unequal shares** — using each one at full strength was the mistake that made the UI loud:
- `#0a0f0d` near-black = the page, verbatim (`--bg`).
- `#2a1e5c` indigo = the card colour, **darkened to about a third of its lightness** (`--card #1e1842`; chroma 42.7 → 30.4) with one lift `--card-2 #2b2358` for hovered rows / outline buttons. At full strength it is a saturated purple filling most of the screen and fighting every label on top of it; as an inky plum it still carries the palette's hue while behaving like a surface.
- `#c4cbca` cool grey = all text (`--text #e8ebe9`, `--text-muted #a6b1ae`) and, at 15% alpha, the single neutral hairline `--border rgba(196,203,202,0.15)` that reads the same on the page and on a card. Never outline boxes in a bright accent.
- `#3cbbb1` teal = the accent (`--accent`): primary button, focus ring, progress bar, small card-title icons, current tab. `#ee4266` rose = errors/destructive (`--danger` fill, `--danger-ink #f78ca0` text).

Only the one primary action wears teal — secondary buttons stay neutral `btn-outline`, otherwise the accent stops meaning anything. `--success #4ade80` and `--warning #f5b942` are text/pill semantics only; the progress bar uses the accent, not green. `--accent-ink #0a0f0d` is the label on **every** bright fill (white on teal is 2.35:1, on rose 3.75:1, on green 1.7:1 — the near-black is 8.2, 5.2 and 11:1). Never hardcode a theme colour: `--accent`/`--accent-hover`/`--accent-ink`/`--accent-soft`/`--accent-glow` own the accent.

Each volume kind owns a hue: `.storage-card.kind-*` sets a local `--kind`/`--kind-ink`/`--kind-soft` used by the card's start border, icon, active fill, active name and "الحالي/الوجهة" badge; `.pill-kind-*` and `.kind-border-*` reuse the same variables for the picker badge, the header `sdWritePill` and the path inputs. The SD card intentionally wears the accent teal — it is the volume this app exists for, so it gets the signature colour; the other three are far from it and from each other (worst pair ΔE76 22.9). `-ink` exists because `#7f918e` is 5.0:1 on the card — fine for a border/icon, too dark for 11px text. Only a writable card gets its kind hue in the header pill; a refusing card stays `pill-warn`. Danger also has two shades: `--danger #ee4266` is the palette rose used as a *fill* (its dark label is 5.2:1) and `--danger-ink #f78ca0` is danger *text* (7.3:1 on a card, 6.2:1 on a raised one), because the fill would only be ~4.5:1 as text.

## Path picker (تحديد / تصفح)
`/api/items` returns `{name, path, is_dir, size}` in one `scandir` pass (folders first, then files, name-sorted). The picker renders folders (navigable, with rename/delete actions) and **files as read-only rows** with a formatted size — a destination must be a folder, and single files are ticked in the source tree instead. Long folders are paged: `PICKER_FILES_STEP` (300) files per page with «عرض المزيد», so a 4 000-file folder opens instantly.

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