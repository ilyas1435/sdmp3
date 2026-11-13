# ☕ SD Card Coffee / SD-Copy-MP3-Manager

**SD Copy MP3 Manager** — a lightweight **Python / Flask** web app that runs on Android (Termux) to reliably **copy, sequence, and timestamp-spoof** audio files to SD Cards for hardware MP3 players (FAT32 compatibility), complete with **JOC indexing** and **Text-to-Speech (Edge TTS)** audio index generation.

> 💡 Access the web UI from any browser on the same network — the app runs entirely on your device (localhost).

---

## ✨ Features

### 🗂️ File Transfer & Organizing
- 📁 **Multi-storage support** — internal storage (`/sdcard`) and external SD cards (`/storage/XXXX-XXXX`) auto-detected.
- 📦 **Stable chunked copying** — resilient, crash-safe file copy with periodic `fsync`, so interrupted transfers are recoverable.
- 🧭 **JOC physical-order sequencing** — files are copied in a natural, physical order (DFS traversal + natural sort) so hardware MP3 players play them reliably in sequence.
- 🖥️ **Built-in file browser** — expandable tree view to browse, select, and exclude folders/files.
- 🛒 **Selection cart** — view total size of selections and copy everything with one tap.
- ⏱️ **Instant folder indexing** — cache a folder's contents once for lightning-fast subsequent lookups.

### 🔍 Search
- ⚡ **Deep instant search** — find folders/files by name with a live "instant" speed badge.
- 🗂️ **Folders-only toggle** — restrict search to folder names only.

### 📝 JOC Index Generator
- 🔢 **Flexible numbering** — configurable start number and max characters per line.
- 🧩 **Multiple templates** — choose among several Arabic output formats:
  - `الرقم : الاسم .` (e.g. `1 : سورة البقرة .`)
  - `الاسم من الرقم` (e.g. `سورة البقرة من 1`)
  - `الاسم من رقم الرقم .`
  - `الاسم : الرقم .`
- ✂️ **Symbol stripping** — optionally remove English letters, numbers, and symbols.
- 🎲 **Random filename** — auto-generates a random 5-character alphanumeric name (shared between `.txt` and `.mp3`).
- ✏️ **Review & edit before export** — optional editor shows the generated index text so you can tweak it before it's downloaded, copied, or converted to MP3. The edited text is exactly what gets used.

### 🔊 Audio Index — Text-to-Speech (Edge TTS)
- 🗣️ **Microsoft Edge neural voices** (free, no API key) with multiple Arabic dialects plus a **multilingual male** voice.
- 🎚️ **Speed control** — normal, ±10%, ±20%.
- 🔢 **Chunked synthesis** — long indexes are split into safe chunks, synthesized, then combined into one MP3.
- 🎵 **64 kbps mono output** — small, hardware-player-friendly MP3s.
- 🔇 **Masking / obfuscation** — hide what the audio actually is by prepending a segment:
  - 🌊 **Subtle noise** — 2 minutes of low, non-intrusive soft hum (brown noise, gently filtered).
  - 🎼 **Custom MP3** — prepend a file of your choice (uploaded from your device).
  - 🚫 **None** — start the audio index immediately.
- 📥 **One-click download** — both the `.txt` index and the `.mp3` audio index download with the same base name.
- 🧹 **Automatic cache cleanup** — all temp chunks/files are deleted when the server stops.

### 🕓 Timestamp Spoofing
- 🗓️ **This spoof mode** — obfuscate last-modified/last-accessed dates of all files & folders (whole destination, or the entire storage if destination is empty).
- 🎲 **Randomised start date** — auto-generates a random timestamp (2019–2023).
- 🛡️ **Bottom-up traversal** — children are processed before parents so parents' timestamps aren't reset by their own edits.

### 🚫 Delete Support
- 🗑️ **Force-delete** — remove files/folders directly from the browser, with protected-item reporting.

### 🛡️ Reliability
- ⚡ **Termux wake lock** — keeps the CPU awake during long transfers.
- ♻️ **Safe shutdown** — Ctrl+C / SIGTERM cleans up TTS cache cleanly.
- 🌐 **Network access** — serves on `0.0.0.0:5050` for access from any device.

---

## 🚀 Getting Started (Termux / Android)

### One-line install (Termux)

```bash
curl -fsSL https://raw.githubusercontent.com/ilyas1435/sdmp3/main/install.sh | bash
```

That installs the **`sdmp3`** command into `$PREFIX/bin`, clones the source to
`~/.local/share/sdmp3`, and installs the Python dependencies. `python`, `ffmpeg`
and `git` are installed with `pkg` automatically if they are missing.

Prefer to read the script before running it?

```bash
git clone https://github.com/ilyas1435/sdmp3.git
cd sdmp3 && bash install.sh
```

### Run it

```bash
sdmp3                 # foreground server (Ctrl+C to stop)
sdmp3 start           # run in the background
sdmp3 status          # pid, port and URLs
sdmp3 stop            # stop the background server
sdmp3 --port 8080     # any flag is passed through to the app
```

Then open a browser — `http://localhost:5050` on the phone, or
`http://<device-ip>:5050` from any device on the same Wi-Fi.

> 🔑 `edge-tts` is optional: file-copy / JOC / spoof features work without it,
> but the 🎧 audio-index (TTS) feature requires it.

### Update / diagnose / remove

```bash
sdmp3 update                                # pull the latest version from GitHub
sdmp3 update --restart                      # ... and restart a running server
sdmp3 --version
sdmp3 doctor                                # check python / ffmpeg / edge-tts / storage / port

bash ~/.local/share/sdmp3/install.sh --uninstall          # remove the command
bash ~/.local/share/sdmp3/install.sh --uninstall --purge  # also delete the source
```

| Command            | What it does                                              |
|--------------------|-----------------------------------------------------------|
| `sdmp3` / `run`    | Start the web UI in the foreground                        |
| `start`            | Start detached, with a pidfile and log                    |
| `stop` / `restart` | Manage the background instance                            |
| `status`           | Show version, pid, port, uptime and URLs                  |
| `update`           | `git pull` from GitHub (refuses if you edited the source) |
| `version`          | Print version + git commit                                |
| `doctor`           | Environment check                                         |
| `check-card`       | Find folders Android can no longer read (FAT damage)      |
| `unlock`           | Grant SD-card all-files access via Shizuku (optional)      |
| `open`             | Open the UI in the Android browser                        |

### 🔓 SD card write access (optional — Shizuku or settings)

On Android 11+ the SD card is write-protected for apps unless Termux gets
**All files access**. The app works without it, but copies to the card may be
blocked. If you use [Shizuku](https://shizuku.rikka.app/):

```bash
sdmp3 unlock             # grants com.termux MANAGE_EXTERNAL_STORAGE
sdmp3 unlock --revert    # undo
```

Without Shizuku: **Settings → Apps → Termux → Special app access → All files
access → Allow**.

Shizuku is **never required**: if `rish`/Shizuku is missing the app runs exactly
as before, and the UI badge shows `Shizuku: غير مثبت`. The header also shows the
app version and whether the card is writable.

### 🧯 Troubleshooting: "تعذر حذف بعض العناصر المحمية / تعذر إكمال الحذف"

A failed delete opens a dialog that names every blocked item with its reason
(`صلاحية مرفوضة`, `الكارت للقراءة فقط`, `مشغول`, `مجلد غير فارغ`...) and has a
**نسخ التفاصيل** (copy details) button, so the exact problem can be shared.

- `صلاحية مرفوضة` → run `sdmp3 unlock` (or grant All files access in settings).
- Folders under `Android/data` and `Android/obb` belong to other apps and stay
  protected even with All files access — Android does not allow removing them.
  The app now refuses them up front with a clear message instead of a partial
  delete.
- `الكارت للقراءة فقط` → the card's lock switch is on, or the card is failing.

#### Damaged folder (FAT): "خطأ إدخال/إخراج" + "مجلد غير فارغ"

If a delete reports `خطأ إدخال/إخراج في الكارت` together with `مجلد غير فارغ`
and nothing was deleted, that folder's **FAT directory entry is broken** (usually
written years ago by a Windows PC or an old MP3 player). Android hides the
contents, so no app can read or delete it — not even with all-files access,
Shizuku, or a card remount (all verified). It is *not* a permission problem.

Repair it on a computer (`chkdsk X: /f` on Windows, `sudo fsck.vfat -a /dev/sdX`
on Linux), then delete the folder normally. To find every damaged folder on your
cards:

```bash
sdmp3 check-card
```

Every delete attempt (path, deleted count, blocked items and reasons) is written
to the log, so it can be inspected later:

```bash
tail -f ~/.local/state/sdmp3/sdmp3.log | grep '\[sdmp3\]'
```

---

## 📦 Dependencies

| Package        | Purpose                              | Required |
|----------------|--------------------------------------|----------|
| **Flask**      | Web server & UI                      | ✅ Yes   |
| **edge-tts**   | Text-to-Speech audio index synthesis | 🔶 For TTS audio only |
| **ffmpeg**     | Combine/encode MP3 chunks            | 🔶 For TTS audio only & obfuscation |

---

## 🧑‍💻 Development

The app is still a single file — everything (Flask server, HTML/CSS/JS UI) lives
in **`project.py`**. The `sdmp3` command is a thin wrapper around it:

| File                | Purpose                                                        |
|---------------------|----------------------------------------------------------------|
| `project.py`        | The whole app (routes + inline HTML/CSS/JS in `HTML_TEMPLATE`) |
| `sdmp3_cli.py`      | `sdmp3` command: run/start/stop/status/update/doctor            |
| `install.sh`        | Installer/updater; generates `$PREFIX/bin/sdmp3`                |
| `requirements.txt`  | Python deps (`flask`, `edge-tts`) — used by install/update      |
| `VERSION`           | Current version, read by both the CLI and the app               |

Run the checkout directly while developing (no install needed):

```bash
python project.py            # same as before
sdmp3 update                 # after committing, to refresh an installed copy
```

Installed copies live in `~/.local/share/sdmp3`; runtime state (pidfile, log)
lives in `~/.local/state/sdmp3`. Stop cleanly with `Ctrl+C` /
`sdmp3 stop` (the TTS temp cache is cleaned automatically).

---

## 📜 License

النسخة العربية:
هذا البرنامج صدقة جارية مجانية بالكامل؛ يُحظر بيعه، أو تأجيره، أو التكسب منه بأي شكل من الأشكال. يُشترط لاستخدامه أو تعديله أو توزيعه ألا يُستعمل في أي عمل يخالف الشريعة الإسلامية أو في نقل ونشر المواد المحرمة.
English Version:
This software is a free, ongoing charity (Sadaqah Jariyah); selling, renting, or commercializing it is strictly prohibited. Use, modification, and distribution are permitted solely for purposes that comply with Islamic Sharia and do not involve prohibited content. 

والحمد لله رب العالمين. 
ilyas/إلياس
