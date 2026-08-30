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

### Prerequisites
- [Termux](https://termux.com/) on Android
- Python 3.8+
- `ffmpeg` (required for audio combining) — install with:
  ```bash
  pkg install ffmpeg
  ```

### 1. Install dependencies
```bash
pkg install python ffmpeg
pip install flask
pip install edge-tts
```
> 🔑 `edge-tts` is optional: the file-copy/JOC/spoof features work without it, but the 🎧 audio-index (TTS) feature requires it.

### 2. Run the app
```bash
python project.py
```

### 3. Open the UI
Open a browser (on the phone or any device on the same Wi‑Fi) and go to:
```
http://<device-ip>:5050
```
On the phone itself: `http://localhost:5050`

---

## 📦 Dependencies

| Package        | Purpose                              | Required |
|----------------|--------------------------------------|----------|
| **Flask**      | Web server & UI                      | ✅ Yes   |
| **edge-tts**   | Text-to-Speech audio index synthesis | 🔶 For TTS audio only |
| **ffmpeg**     | Combine/encode MP3 chunks            | 🔶 For TTS audio only & obfuscation |

---

## 🧑‍💻 Development

Single-file app — everything (Flask server, HTML/CSS/JS UI) lives in **`project.py`**. Runs on **port 5050**:

```bash
python project.py
```
Stop cleanly with `Ctrl+C` (cleans the TTS temp cache automatically).

---

## 📜 License

النسخة العربية:
هذا البرنامج صدقة جارية مجانية بالكامل؛ يُحظر بيعه، أو تأجيره، أو التكسب منه بأي شكل من الأشكال. يُشترط لاستخدامه أو تعديله أو توزيعه ألا يُستعمل في أي عمل يخالف الشريعة الإسلامية أو في نقل ونشر المواد المحرمة.
English Version:
This software is a free, ongoing charity (Sadaqah Jariyah); selling, renting, or commercializing it is strictly prohibited. Use, modification, and distribution are permitted solely for purposes that comply with Islamic Sharia and do not involve prohibited content. 

والحمد لله رب العالمين. 
ilyas/إلياس
