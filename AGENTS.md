# AGENTS.md

Android/Termux Flask app that copies + timestamp-spoofs MP3s to SD cards, generates JOC text indexes, and converts them to audio via Edge TTS. Runs entirely on the device.

## Single-file app
Everything lives in **`project.py`** (no package split): Flask backend, HTML, CSS, and — critically — **all page JS in ONE inline `<script>` block** defined inside the Python string `HTML_TEMPLATE` (starts ~line 559).

- Run: `python project.py` (serves `0.0.0.0:5050`, **not** the Flask default 5000). UI at `http://localhost:5050` / `<device-ip>:5050`.
- No test framework, linter, or package manifest. Verify via `python -m py_compile project.py` and manual browser testing.
- Stop with Ctrl+C / SIGTERM; this cleans the TTS temp cache (`tempfile.gettempdir()/tts_cache_<pid>`).

## CRITICAL: escape handling inside HTML_TEMPLATE
`HTML_TEMPLATE` is a **plain `"""..."""` Python string**, not raw and not an f-string. Python processes backslash escapes in the embedded JS at parse time. To emit a literal backslash sequence to the browser you must **double the backslash**:
- Write `txt.split("\\n")` in Python so the browser receives `split('\n')`. A single `\n` becomes a real newline and **breaks the whole script block**.
- Same applies to `\.`, `\uXXXX` regex/unescape tokens: use `\\.`, `\\uXXXX` in the Python source.

## One bad JS token disables the entire UI
Since all JS is a single `<script>`, one syntax error kills every handler — symptoms are "no storage shown, no folders, can't select anything." `python -m py_compile` will NOT catch JS errors (the JS is just a string to Python). To lint the JS: render/extract the `<script>` contents (with Jinja `{*...}` stripped) and run `node --check`.

## Storage detection (server-side, not JS)
Options in the source/destination `<select>` are rendered at route `/` via Jinja `{% for st in storages %}`, backed by `get_available_storages()` (~line 122): always `/sdcard` (internal), plus external SD cards found from `df -h` `/storage/...` mounts (emulated mounts are skipped). If the select is empty, check this Python function — not the JS.

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