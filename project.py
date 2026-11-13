#!/usr/bin/env python3
import os
import shutil
import subprocess
import threading
import time
import datetime
import re
import atexit
import tempfile
import asyncio
from collections import deque
from werkzeug.exceptions import HTTPException
from flask import Flask, render_template_string, request, jsonify, send_file

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    edge_tts = None
    EDGE_TTS_AVAILABLE = False

app = Flask(__name__)

# Version is kept in the VERSION file next to this script so that the `sdmp3`
# CLI can read it without importing the app.
APP_VERSION_FALLBACK = "1.0.0"

# Toggled by --no-wakelock (see main() at the bottom).
WAKELOCK_ENABLED = True


def get_app_version():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'VERSION'), encoding='utf-8') as fh:
            return fh.read().strip() or APP_VERSION_FALLBACK
    except OSError:
        return APP_VERSION_FALLBACK


# Read/write block size. 2MB keeps copies sequential (order is the whole point
# of this app) while cutting the number of FUSE round-trips vs the old 512KB.
COPY_BUFFER_SIZE = 2 * 1024 * 1024
# Force hardware sync every 64MB: fewer stalls, same crash-safety granularity.
SYNC_THRESHOLD = 64 * 1024 * 1024

transfer_lock = threading.RLock()

DEFAULT_STATE = {
    'status': 'idle',
    'action': '',  # Tracks if current operation is 'copy' or 'spoof'
    'total': 0,
    'copied': 0,
    'failed': 0,
    'file': '',
    'progress': 0,
    'log': '',
    'errors': [],
    'cancel': False,
    'start_time': 0,
    'time_taken': ''
}
transfer_state = dict(DEFAULT_STATE)

def reset_state(**overrides):
    with transfer_lock:
        transfer_state.clear()
        transfer_state.update(DEFAULT_STATE)
        transfer_state.update(overrides)

def set_state(**kwargs):
    with transfer_lock:
        transfer_state.update(kwargs)
        # Prevent error array bloat to protect browser memory
        if 'errors' in transfer_state and len(transfer_state['errors']) > 50:
            transfer_state['errors'] = transfer_state['errors'][:50]

def get_state_snapshot():
    with transfer_lock:
        return dict(transfer_state)

def is_busy():
    with transfer_lock:
        return transfer_state['status'] == 'copying'

def is_cancelled():
    with transfer_lock:
        return transfer_state['cancel']

class TransferCancelled(Exception):
    pass

def finish_transfer(final_status, msg, extra=None):
    snap = get_state_snapshot()
    elapsed = int(time.time() - snap['start_time'])
    mins, secs = divmod(elapsed, 60)
    kwargs = dict(status=final_status, file=msg, time_taken=f"{mins} دقيقة و {secs} ثانية")
    if extra:
        kwargs.update(extra)
    set_state(**kwargs)

# ------------- TTS Cache Setup -------------
TTS_CACHE_DIR = os.path.join(tempfile.gettempdir(), f"tts_cache_{os.getpid()}")
TTS_CACHE_LOCK = threading.Lock()

def setup_tts_cache():
    try:
        os.makedirs(TTS_CACHE_DIR, exist_ok=True)
    except Exception:
        pass
    return TTS_CACHE_DIR

def cleanup_tts_cache():
    """Delete all TTS temp files/chunks/final MP3s when the server closes (Ctrl+C)."""
    with TTS_CACHE_LOCK:
        if os.path.isdir(TTS_CACHE_DIR):
            try:
                shutil.rmtree(TTS_CACHE_DIR, ignore_errors=True)
            except Exception:
                pass

atexit.register(cleanup_tts_cache)

def acquire_wakelock():
    if not WAKELOCK_ENABLED:
        return
    try:
        subprocess.run(["termux-wake-lock"], timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def release_wakelock():
    try:
        subprocess.run(["termux-wake-unlock"], timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ------------- Shizuku (100% optional) -------------
# Shizuku runs ADB-level shell commands without root. It is used ONLY to read
# its status and to grant "all files access" to the SD card. Nothing in the app
# depends on it: when the app or the `rish` helper is missing, every call below
# returns None quickly and the rest of the app keeps working normally.
SHIZUKU_LOCK = threading.Lock()
SHIZUKU_CACHE = {'ts': 0.0, 'data': None}
SHIZUKU_TTL = 60
SHIZUKU_TIMEOUT = 6


def rish(command, timeout=SHIZUKU_TIMEOUT):
    """Run a shell command through Shizuku. Returns its output, or None if unavailable.

    rish forwards the remote command's output on stderr, so both streams are
    merged here - reading stdout only would always look empty.
    """
    rish_path = shutil.which('rish')
    if not rish_path:
        return None
    try:
        res = subprocess.run([rish_path, '-c', command], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except Exception:
        return None
    return res.stdout or ''


def shizuku_refresh():
    """Ask Shizuku for its state (slow: up to a few seconds)."""
    data = {'installed': bool(shutil.which('rish')), 'running': False,
            'all_files': False, 'checked': True, 'detail': 'غير مثبت'}
    if data['installed']:
        out = rish('id')
        if out and 'uid=2000' in out:
            data['running'] = True
            data['detail'] = 'مفعّل'
            ops = rish('appops get com.termux MANAGE_EXTERNAL_STORAGE') or ''
            data['all_files'] = 'allow' in ops.lower()
        else:
            data['detail'] = 'متوقف'
    with SHIZUKU_LOCK:
        SHIZUKU_CACHE['ts'] = time.time()
        SHIZUKU_CACHE['data'] = data
    return dict(data)


def shizuku_status():
    """Cached Shizuku state - never blocks the web UI."""
    with SHIZUKU_LOCK:
        data = SHIZUKU_CACHE.get('data')
    if data is not None:
        return dict(data)
    return {'installed': bool(shutil.which('rish')), 'running': False,
            'all_files': False, 'checked': False, 'detail': 'جاري الفحص'}


def shizuku_refresher():
    """Background loop so no request ever pays the rish cost."""
    while True:
        try:
            shizuku_refresh()
        except Exception:
            pass
        time.sleep(SHIZUKU_TTL)


def removable_sd():
    """First external (non-emulated) /storage mount + whether we may write to it."""
    path = None
    try:
        df_output = subprocess.check_output("df -h", shell=True, text=True, timeout=5)
        for line in df_output.splitlines():
            if '/storage/' in line and 'emulated' not in line:
                path = line.split()[-1]
                break
    except Exception:
        return {'path': None, 'writable': None}
    if not path:
        return {'path': None, 'writable': None}
    try:
        return {'path': path, 'writable': bool(os.access(path, os.W_OK))}
    except Exception:
        return {'path': path, 'writable': None}


def natural_sort_key(text):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

def get_available_storages():
    storages = []
    seen_names = set()
    if os.path.exists('/sdcard'):
        storages.append({'name': 'الذاكرة الداخلية', 'path': '/sdcard'})
    try:
        df_output = subprocess.check_output("df -h", shell=True, text=True, timeout=5)
        for line in df_output.splitlines():
            # Only use valid /storage/ mounts, avoid duplicates and permission-denied /mnt/media_rw
            if '/storage/' in line and 'emulated' not in line:
                mount_point = line.split()[-1]
                name = os.path.basename(mount_point)
                if name not in seen_names:
                    storages.append({'name': f'كارت ذاكرة ({name})', 'path': mount_point})
                    seen_names.add(name)
    except Exception:
        pass
    return storages

def count_files(paths, cancel_check=None, count_dirs=False):
    total = 0
    stack = list(paths)
    counter = 0
    while stack:
        if cancel_check is not None:
            counter += 1
            if counter % 300 == 0 and cancel_check():
                return total
        p = stack.pop()
        try:
            if os.path.islink(p): continue
            if os.path.isdir(p):
                if count_dirs: total += 1
                with os.scandir(p) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink(): continue
                            if entry.is_dir(follow_symlinks=False): 
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False): 
                                total += 1
                        except Exception: pass
            elif os.path.isfile(p): total += 1
        except Exception: pass
    return total

def scan_source(paths, excludes=None, cancel_check=None):
    excludes = excludes or set()
    total_files = 0
    total_bytes = 0
    stack = [p for p in paths if p not in excludes]
    counter = 0
    while stack:
        if cancel_check is not None:
            counter += 1
            if counter % 300 == 0 and cancel_check():
                return total_files, total_bytes
        p = stack.pop()
        if p in excludes: continue
        try:
            if os.path.islink(p): continue
            if os.path.isdir(p):
                with os.scandir(p) as it:
                    for entry in it:
                        if entry.path in excludes: continue
                        try:
                            if entry.is_symlink(): continue
                            if entry.is_dir(follow_symlinks=False): stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                total_files += 1
                                total_bytes += entry.stat(follow_symlinks=False).st_size
                        except Exception: pass
            elif os.path.isfile(p):
                total_files += 1
                total_bytes += os.path.getsize(p)
        except Exception: pass
    return total_files, total_bytes

def format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while size_bytes >= 1024 and i < len(size_name) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.2f} {size_name[i]}"

def has_enough_space(dest, needed_bytes, margin_bytes=20 * 1024 * 1024):
    try:
        _, _, free = shutil.disk_usage(dest)
        return free >= (needed_bytes + margin_bytes)
    except Exception:
        return True

def apply_spoof_time(path, timestamp):
    """Best-effort setting of Last Modified / Last Accessed times."""
    try:
        os.utime(path, (timestamp, timestamp))
        return True
    except Exception:
        try:
            # Fallback for protected files: loosen permissions temporarily
            os.chmod(path, 0o777)
            os.utime(path, (timestamp, timestamp))
            return True
        except Exception:
            return False

def describe_os_error(exc):
    """Human (Arabic) reason for a failed filesystem operation."""
    if exc is None:
        return 'تعذر الحذف'
    if isinstance(exc, PermissionError):
        return 'صلاحية مرفوضة (محمي من أندرويد)'
    if isinstance(exc, FileNotFoundError):
        return 'غير موجود'
    if isinstance(exc, IsADirectoryError):
        return 'مجلد وليس ملفاً'
    if isinstance(exc, OSError):
        code = getattr(exc, 'errno', None)
        reasons = {
            1: 'عملية غير مسموحة',
            5: 'خطأ إدخال/إخراج في الكارت',
            13: 'صلاحية مرفوضة (محمي من أندرويد)',
            16: 'مشغول (قيد الاستخدام)',
            22: 'مسار غير صالح',
            28: 'المساحة ممتلئة',
            30: 'الكارت للقراءة فقط',
            39: 'مجلد غير فارغ',
        }
        if code in reasons:
            return reasons[code]
    text = str(exc).strip()
    return text or 'خطأ غير معروف'

def try_remove_entry(path):
    """Remove one file/link/dir. Returns (ok, reason)."""
    try:
        is_dir = os.path.isdir(path) and not os.path.islink(path)
    except Exception:
        is_dir = False
    try:
        if is_dir: os.rmdir(path)
        else: os.remove(path)
        return True, None
    except Exception as first_error:
        # Fallback for "protected" entries: loosen permissions and retry once.
        try: os.chmod(path, 0o777)
        except Exception: pass
        try:
            try:
                is_dir = os.path.isdir(path) and not os.path.islink(path)
            except Exception:
                is_dir = False
            if is_dir: os.rmdir(path)
            else: os.remove(path)
            return True, None
        except Exception:
            return False, describe_os_error(first_error)

def force_delete_tree(target, cancel_check=None, progress_cb=None):
    """Delete a tree bottom-up. Returns (deleted, failures) where failures is a
    list of (path, reason) for every entry that is still there afterwards, so the
    UI can say exactly what is protected instead of a generic message."""
    deleted = 0
    failures = []

    def note(path, reason):
        if len(failures) < 25:
            failures.append((path or target, reason or 'تعذر الحذف'))

    if not os.path.lexists(target):
        return deleted, failures

    if os.path.isfile(target) or os.path.islink(target):
        ok, reason = try_remove_entry(target)
        if ok:
            return 1, failures
        note(target, reason)
        return 0, failures

    def on_walk_error(exc):
        # os.walk could not list this folder: report it instead of skipping silently
        note(getattr(exc, 'filename', None), describe_os_error(exc))

    try:
        for root, dirs, files in os.walk(target, topdown=False, onerror=on_walk_error):
            if cancel_check is not None and cancel_check():
                return deleted, failures
            for name in files:
                fp = os.path.join(root, name)
                ok, reason = try_remove_entry(fp)
                if ok: deleted += 1
                else: note(fp, reason)
                if progress_cb and (deleted + len(failures)) % 20 == 0:
                    progress_cb(deleted, len(failures))
            for name in dirs:
                dp = os.path.join(root, name)
                ok, reason = try_remove_entry(dp)
                if ok: deleted += 1
                else: note(dp, reason)
    except Exception as exc:
        note(target, describe_os_error(exc))

    if os.path.lexists(target):
        ok, reason = try_remove_entry(target)
        if ok: deleted += 1
        else: note(target, reason)

    if progress_cb: progress_cb(deleted, len(failures))
    return deleted, failures

def stable_chunked_copy(src, dst, cancel_check=None):
    if cancel_check is not None and cancel_check():
        raise TransferCancelled()
    
    bytes_written_since_sync = 0
    try:
        with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
            while True:
                if cancel_check is not None and cancel_check():
                    raise TransferCancelled()
                
                buf = fsrc.read(COPY_BUFFER_SIZE)
                if not buf: break
                
                fdst.write(buf)
                bytes_written_since_sync += len(buf)
                
                if bytes_written_since_sync >= SYNC_THRESHOLD:
                    fdst.flush()
                    os.fsync(fdst.fileno())
                    bytes_written_since_sync = 0
                    
            fdst.flush()
            os.fsync(fdst.fileno())
    except Exception as e:
        try:
            if os.path.exists(dst): os.remove(dst)
        except Exception: pass
        raise e

def copy_thread_task(paths, dest, excludes=None):
    acquire_wakelock()
    reset_state(status='copying', action='copy', start_time=time.time())
    copied = 0
    failed_names = []
    excludes = excludes or set()

    last_ui_update = time.time()
    UI_UPDATE_INTERVAL = 1.0  

    try:
        set_state(file='جاري فحص الملفات (قد يأخذ ثواني قليلة)...')
        total, needed_bytes = scan_source(paths, excludes, cancel_check=is_cancelled)
        if is_cancelled(): raise TransferCancelled()
        set_state(total=total)

        if not has_enough_space(dest, needed_bytes):
            raise RuntimeError(f"المساحة المتوفرة غير كافية. المطلوب تقريباً: {format_size(needed_bytes)}")

        set_state(file='جاري ترتيب الملفات بتسلسل JOC...')
        
        ordered_tasks = []
        def build_dfs_queue(current_src, current_dest, entry=None):
            """DFS pre-order + natural sort.

            NEVER parallelize this copy: the FAT directory-entry order (and the
            cluster order) is exactly what old MP3 players use to play files in
            sequence, so files must be written one at a time in this order.
            """
            if current_src in excludes: return
            name = entry.name if entry is not None else os.path.basename(current_src)
            dst_path = os.path.join(current_dest, name)

            # DirEntry.is_file()/is_dir() reuse the stat that scandir already did,
            # so we no longer pay 2-3 extra FUSE round-trips per file.
            if (entry.is_file() if entry is not None else os.path.isfile(current_src)):
                ordered_tasks.append((current_src, dst_path, 'file'))
            elif (entry.is_dir() if entry is not None else os.path.isdir(current_src)):
                ordered_tasks.append((current_src, dst_path, 'dir'))
                try:
                    with os.scandir(current_src) as it:
                        children = sorted(it, key=lambda e: natural_sort_key(e.name))
                        for child in children:
                            build_dfs_queue(child.path, dst_path, child)
                except Exception: pass

        for p in sorted(paths, key=natural_sort_key):
            build_dfs_queue(p, dest)
            
        set_state(file='جاري النقل المستقر المباشر...')

        for src, dst_path, kind in ordered_tasks:
            if is_cancelled(): raise TransferCancelled()
            name = os.path.basename(src)

            if kind == 'file':
                try:
                    stable_chunked_copy(src, dst_path, cancel_check=is_cancelled)
                    copied += 1
                except TransferCancelled:
                    raise
                except Exception as e:
                    failed_names.append(name)
                    if isinstance(e, OSError) and getattr(e, 'errno', None) == 28:
                        raise RuntimeError("انتهت المساحة الخالية في الوجهة أثناء النسخ.")

                current_time = time.time()
                if current_time - last_ui_update >= UI_UPDATE_INTERVAL:
                    done_count = copied + len(failed_names)
                    progress = int((done_count / total) * 100) if total else 0
                    set_state(copied=copied, failed=len(failed_names), progress=progress, file=name)
                    last_ui_update = current_time

            elif kind == 'dir':
                try:
                    os.makedirs(dst_path, exist_ok=True)
                except Exception:
                    failed_names.append(name)

        set_state(file='جاري تأكيد كتابة البيانات النهائية...')
        try: os.sync()
        except Exception: pass

        if failed_names:
            final_msg = f'اكتمل النسخ مع فشل {len(failed_names)} ملف - راجع التفاصيل'
        else:
            final_msg = 'اكتمل النسخ بنجاح.'
        
        invalidate_cache_for_path(dest)
        finish_transfer('done', final_msg, extra={'progress': 100, 'copied': copied, 'errors': failed_names[:30]})

    except TransferCancelled:
        try: os.sync()
        except Exception: pass
        finish_transfer('cancelled', 'تم إلغاء النسخ.')
    except Exception as e:
        set_state(status='error', log=str(e))
    finally:
        release_wakelock()

def spoof_only_thread(dest_path, start_timestamp):
    """
    Bottom-up directory traversal to obfuscate dates.
    Processing children before parents ensures the parent's modified time 
    isn't overwritten when its files are edited.
    """
    acquire_wakelock()
    reset_state(status='copying', action='spoof', start_time=time.time())
    copied = 0
    try:
        set_state(file='جاري فحص المسار (قد تستغرق العملية وقتاً)...')
        total_items = count_files([dest_path], cancel_check=is_cancelled, count_dirs=True)
        if is_cancelled(): raise TransferCancelled()
        set_state(total=total_items)
        
        current_time = start_timestamp

        # topdown=False is REQUIRED here. Modifying a file inherently updates the directory it sits in.
        for root, dirs, files in os.walk(dest_path, topdown=False):
            if is_cancelled(): raise TransferCancelled()
            
            # 1. Spoof files in this level
            for name in files:
                p = os.path.join(root, name)
                if os.path.islink(p): continue
                if apply_spoof_time(p, current_time):
                    current_time += 60
                    copied += 1
                
                if copied % 50 == 0:
                    snap_total = get_state_snapshot()['total']
                    progress = int((copied / max(snap_total, 1)) * 100)
                    set_state(copied=copied, progress=min(progress, 99), file=name)

            # 2. Spoof directories in this level 
            for name in dirs:
                p = os.path.join(root, name)
                if os.path.islink(p): continue
                if apply_spoof_time(p, current_time):
                    current_time += 60
                    copied += 1
                
                if copied % 50 == 0:
                    snap_total = get_state_snapshot()['total']
                    progress = int((copied / max(snap_total, 1)) * 100)
                    set_state(copied=copied, progress=min(progress, 99), file=name)
                    
        # 3. Spoof the root parent directory itself last
        if not os.path.islink(dest_path):
            if apply_spoof_time(dest_path, current_time):
                copied += 1
                
        finish_transfer('done', 'تم تطبيق التمويه الجذري بنجاح.', extra={'progress': 100, 'copied': copied})
    except TransferCancelled: finish_transfer('cancelled', 'تم إلغاء التمويه.')
    except Exception as e: set_state(status='error', log=str(e))
    finally: release_wakelock()

# ------------- JOC Index Helpers -------------
def clean_and_cut(text, limit, strip_symbols):
    text = re.sub(r'[-_ـ]', ' ', text)
    if strip_symbols:
        text = re.sub(r'[a-zA-Z0-9!"#$%&\'()*+,./:;<=>?@[\]^_{|}~-]', '', text)
    else:
        text = re.sub(r'[!"#$%&\'()*+,./:;<=>?@[\]^_{|}~-]', '', text)
        
    text = ' '.join(text.split())
    words = text.split()
    result = ""
    for w in words:
        if not result: result = w
        elif len(result) + 1 + len(w) <= limit: result += " " + w
        else: break
    return result

# ------------- TTS (Edge) Helpers -------------
TTS_VOICES = {
    "ar-AE-HamdanNeural": "عربي (الإمارات) – حمدان (رجل)",
    "ar-SA-HamedNeural": "عربي (السعودية) – حامد (رجل)",
    "ar-EG-ShakirNeural": "عربي (مصر) – شاكر (رجل)",
    "ar-IQ-BasselNeural": "عربي (العراق) – باسل (رجل)",
    "ar-MA-JamalNeural": "عربي (المغرب) – جمال (رجل)",
    "en-US-AndrewMultilingualNeural": "متعدد اللغات – أندرو (رجل)",
}
TTS_DEFAULT_VOICE = "ar-AE-HamdanNeural"

def _cut_at_word_boundary(text, limit):
    """Split text at limit, ending on a complete word.

    Returns (head_chunk, remainder) where head_chunk ends at a whitespace
    boundary so no word is truncated across chunks. If the text contains no
    whitespace within limit (a single unbroken over-long word), falls back to
    a character split so no text is ever lost.
    """
    text = text.rstrip()
    if len(text) <= limit:
        return text, ""
    pos = text.rfind(' ', 0, limit + 1)
    if pos <= 0:
        pos = text.rfind('\t', 0, limit + 1)
    if pos <= 0:
        pos = text.rfind('\u00a0', 0, limit + 1)
    if pos <= 0:
        return text[:limit], text[limit:]
    return text[:pos].rstrip(), text[pos + 1:].lstrip()

def chunk_index_text(index_text, max_chars=1800):
    """Split big JOC index text into safe chunks for TTS (line-aware, word-safe)."""
    lines = index_text.split('\n')
    chunks = []
    current = ""
    for line in lines:
        if not line:
            if current:
                chunks.append(current.rstrip())
                current = ""
            continue
        if len(current) + 1 + len(line) <= max_chars:
            current = (current + "\n" + line) if current else line
        else:
            if current:
                chunks.append(current.rstrip())
            # Split a single over-long line at word boundaries so no word is
            # truncated across chunks (which would distort pronunciation).
            if len(line) > max_chars:
                while line and len(line) > max_chars:
                    head, line = _cut_at_word_boundary(line, max_chars)
                    if head:
                        chunks.append(head)
                current = line
            else:
                current = line
    if current:
        chunks.append(current.rstrip())
    return chunks or [""]

def synthesize_chunk_mp3(text, voice, rate, out_path):
    """Synthesize one chunk with Edge TTS and save MP3 to out_path."""
    async def _run():
        communicate = edge_tts.Communicate(text or " ", voice=voice, rate=rate)
        await communicate.save(out_path)
    asyncio.run(_run())

def generate_static_noise(out_path, seconds=120, bitrate="64k"):
    """Generate 'seconds' of subtle, low, non-intrusive noise (MP3, mono).

    Uses brown noise (deep, rumbling) at very low amplitude with a low-pass
    filter so the result sounds like a barely-audible soft hum - much gentler
    than harsh white static."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        "anoisesrc=color=brown:duration=%d:amplitude=0.04:seed=42" % seconds,
        "-af", "lowpass=f=300,volume=0.5",
        "-c:a", "libmp3lame", "-b:a", bitrate, "-ar", "44100", "-ac", "1",
        out_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)

def combine_mp3_chunks(chunk_files, final_path, bitrate="64k", obfuscate=False, static_seconds=120, custom_audio_path=None):
    """Concatenate chunk MP3s into one MP3 at the requested bitrate (mono 44.1kHz).

    If obfuscate is True, a masking segment is prepended so the real audio index
    only begins after it. The masking segment is:
      - 'file' mode  -> the user's custom_audio_path (whole file, as-is), or
      - noise mode   -> 'static_seconds' of subtle generated noise."""
    concat_list = os.path.join(TTS_CACHE_DIR, "concat_list.txt")
    entries = []
    if obfuscate:
        if custom_audio_path and os.path.isfile(custom_audio_path):
            entries.append(custom_audio_path)
        else:
            static_path = os.path.join(TTS_CACHE_DIR, "static_noise.mp3")
            generate_static_noise(static_path, seconds=static_seconds, bitrate=bitrate)
            entries.append(static_path)
    entries.extend(chunk_files)
    with open(concat_list, "w", encoding="utf-8") as f:
        for cf in entries:
            f.write(f"file '{cf}'\n")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c:a", "libmp3lame", "-b:a", bitrate, "-ar", "44100", "-ac", "1",
        final_path
    ]
    subprocess.run(cmd, check=True, capture_output=True)

# ------------------ HTML & JS Template ---------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SD Card Coffee</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=IBM+Plex+Sans+Arabic:wght@400;500;600&display=swap" rel="stylesheet">
    <meta name="theme-color" content="#0a0f1e">
    <style>
        :root {
            --bg: #0a0f1e;
            --card: #131b2e;
            --card-2: #182238;
            --border: #26314a;
            --text: #f5f7fb;
            --text-muted: #8b96ac;
            --accent: #c17f3e;
            --accent-hover: #a8672e;
            --accent-soft: rgba(193, 127, 62, 0.16);
            --success: #22c55e;
            --danger: #f43f5e;
            --danger-soft: rgba(244, 63, 94, 0.12);
            --warning: #f59e0b;
            --radius: 14px;
        }
        * { box-sizing: border-box; font-family: 'IBM Plex Sans Arabic', 'Inter', sans-serif; -webkit-tap-highlight-color: transparent; }
        body { background-color: var(--bg); color: var(--text); margin: 0; padding: 14px; font-size: 14px; padding-bottom: 110px; }
        .container { max-width: 900px; margin: 0 auto; }

        .app-header { display:flex; align-items:center; gap:12px; padding-bottom: 18px; margin-top: 6px; margin-bottom: 6px; border-bottom: 1px solid var(--border); }
        .app-header .badge { width:42px; height:42px; border-radius:12px; background:var(--accent-soft); display:flex; align-items:center; justify-content:center; flex-shrink:0; }
        .app-header .badge svg { stroke: var(--accent); width:22px; height:22px; }
        .app-header h1 { font-size: 18px; color: #fff; margin:0; font-weight: 700; letter-spacing: -0.2px; }
        .app-header p { margin: 2px 0 0; font-size: 12.5px; color: var(--text-muted); }
        .pill-row { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
        .pill { font-size: 11px; line-height: 1; padding: 5px 9px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-muted); background: rgba(255,255,255,0.03); white-space: nowrap; }
        .pill-ok { color: var(--success); border-color: rgba(34,197,94,0.35); background: rgba(34,197,94,0.08); }
        .pill-warn { color: var(--warning); border-color: rgba(245,158,11,0.35); background: rgba(245,158,11,0.08); }

        .card { background: var(--card); padding: 18px; border-radius: var(--radius); border: 1px solid var(--border); margin-bottom: 16px; }
        .card-title { display:flex; align-items:center; gap:8px; font-size: 14px; font-weight:600; color:#fff; margin: 0 0 14px; }
        .card-title svg { width:17px; height:17px; stroke: var(--accent); }

        label { font-weight: 500; margin-bottom: 8px; display: block; color: var(--text-muted); font-size: 13px; }
        select, input[type="text"], input[type="number"], input[type="datetime-local"] { width: 100%; padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; font-size: 14px; background-color: var(--bg); color: #fff; outline: none; margin-bottom: 12px; transition: border-color 0.15s, box-shadow 0.15s; min-height: 46px; }
        select:focus, input[type="text"]:focus, input[type="number"]:focus, input[type="datetime-local"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
        textarea { width: 100%; padding: 12px 14px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 10px; font-family: 'Inter', monospace; font-size: 14px; line-height: 1.7; background-color: var(--bg); color: #fff; outline: none; resize: none; }
        textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

        .btn { padding: 11px 16px; min-height: 44px; border: none; border-radius: 10px; cursor: pointer; font-weight: 600; font-size: 13.5px; color: #fff; transition: transform 0.08s ease, filter 0.15s ease, background 0.15s ease; display: inline-flex; align-items: center; justify-content: center; gap: 7px; }
        .btn:active { transform: scale(0.97); }
        .btn-primary { background: linear-gradient(135deg, var(--accent), var(--accent-hover)); box-shadow: 0 6px 16px -6px rgba(99,102,241,0.6); }
        .btn-primary:hover { filter: brightness(1.08); }
        .btn-success { background: var(--success); }
        .btn-success:hover { filter: brightness(1.08); }
        .btn-danger { background: var(--danger); }
        .btn-danger:hover { filter: brightness(1.08); }
        .btn-warning { background: var(--warning); color: #1a1200; }
        .btn-warning:hover { filter: brightness(1.08); }
        .btn-outline { background: var(--card-2); border: 1px solid var(--border); color: var(--text); }
        .btn-outline:hover { border-color: var(--accent); color: var(--accent); }
        .btn-sm { padding: 8px 12px; min-height: 38px; font-size: 12.5px; }

        .tree { max-height: 65vh; overflow: auto; background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 8px; }
        .item-row { display: flex; align-items: center; padding: 9px 6px; border-radius: 8px; border-bottom: 1px solid var(--border); min-width: max-content; transition: background 0.15s; }
        .item-row:last-child { border-bottom: none; }
        .item-row:hover { background: var(--card-2); }
        .toggle-btn { background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: 12px; width: 26px; height: 26px; padding: 0; flex-shrink:0; }
        .excluded-badge { color: var(--warning); font-size: 11px; margin-right: 8px; background: rgba(245, 158, 11, 0.1); padding: 2px 6px; border-radius: 4px;}
        .checkbox { accent-color: var(--accent); width: 18px; height: 18px; margin: 0 10px; flex-shrink: 0; cursor: pointer;}
        .item-name { cursor: pointer; display: flex; align-items: center; gap: 8px; white-space: nowrap; padding-right: 6px; color: #e7ebf3; font-size:14px;}
        .children { margin-right: 24px; display: none; border-right: 1px solid var(--border); padding-right: 12px; }
        .children.open { display: block; }

        .row-actions { display:flex; gap:6px; margin-inline-start:auto; flex-shrink:0; padding-inline-start:10px; }
        .icon-btn { width:30px; height:30px; border-radius:8px; border:1px solid var(--border); background:var(--bg); color:var(--text-muted); display:flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; }
        .icon-btn svg { width:14px; height:14px; }
        .icon-btn:active { transform: scale(0.92); }
        .icon-btn:hover { color: var(--accent); border-color: var(--accent); }
        .icon-btn.danger:hover { color: var(--danger); border-color: var(--danger); }

        .cart { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(19, 27, 46, 0.98); backdrop-filter: blur(6px); border-top: 1px solid var(--border); padding: 14px 18px calc(14px + env(safe-area-inset-bottom)); display: flex; justify-content: space-between; align-items: center; z-index: 50; box-shadow: 0 -8px 24px -8px rgba(0,0,0,0.5); }
        .cart-buttons { display: flex; gap: 12px; }

        .modal { display: none; position: fixed; inset: 0; width: 100%; height: 100%; background: rgba(6, 9, 18, 0.85); z-index: 1000; justify-content: center; align-items: safe center; padding: 16px; overflow: auto; -webkit-overflow-scrolling: touch; }
        .modal-content { background: var(--card); padding: 20px; border-radius: 18px; width: 100%; max-width: 600px; border: 1px solid var(--border); max-height: max(70vh, 480px); display: flex; flex-direction: column; box-shadow: 0 24px 48px -12px rgba(0,0,0,0.6); margin: auto; }
        .modal-content .modal-body { overflow-y: auto; flex: 1 1 auto; min-height: 0; -webkit-overflow-scrolling: touch; }
        .modal-content .modal-footer { display: flex; gap: 12px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); flex-shrink: 0; }
        @media (max-width: 480px) {
            .modal { padding: 8px; align-items: stretch; }
            .modal-content { padding: 16px; max-height: 92vh; border-radius: 14px; }
        }

        .progress-bar { width: 100%; height: 8px; background: var(--bg); border-radius: 4px; overflow: hidden; margin: 15px 0; border: 1px solid var(--border);}
        .progress-fill { height: 100%; background: var(--success); width: 0%; transition: width 0.3s ease; }
        .ltr-input { direction: ltr; text-align: left; font-family: 'Inter', monospace; font-size: 12px; }

        .disk-space-container { margin-bottom: 10px; font-size: 13px; color: var(--text-muted); display: none;}
        .disk-bar { width: 100%; height: 6px; background: var(--bg); border-radius: 3px; overflow: hidden; margin-top: 6px; border: 1px solid var(--border);}
        .disk-used { height: 100%; background: var(--accent); width: 0%; transition: width 0.3s ease; }

        .spoof-settings { background: var(--bg); padding: 16px; border-radius: 10px; border: 1px dashed var(--border); margin-bottom: 10px; display:flex; flex-direction:column; gap:10px;}

        .truncate-text {
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 100%;
            display: inline-block;
            vertical-align: middle;
        }

        .grid-2 { display:grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        @media (max-width: 420px) { .grid-2 { grid-template-columns: 1fr; } }

        .seg.option.active { background: linear-gradient(135deg, var(--accent), var(--accent-hover)); color: #fff; font-weight: 600; box-shadow: 0 4px 12px -4px rgba(193,127,62,0.6); }
        .seg.option.active svg { stroke: #fff; }
        .seg.option:hover:not(.active) { color: var(--accent); border-radius:7px; }

        svg.icon { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    </style>
</head>
<body>

<div class="container">
    <div class="app-header">
        <div class="badge">
            <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8h1a4 4 0 0 1 0 8h-1"></path><path d="M2 8h16v9a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V8z"></path><line x1="6" y1="1" x2="6" y2="4"></line><line x1="10" y1="1" x2="10" y2="4"></line><line x1="14" y1="1" x2="14" y2="4"></line></svg>
        </div>
        <div>
            <h1>SD Card Coffee</h1>
            <p>نقل، تنظيم، وتمويه الملفات باحترافية</p>
            <div class="pill-row">
                <span class="pill" id="appVersionPill">v{{ app_version }}</span>
                <span class="pill" id="shizukuPill" title="Shizuku أداة اختيارية: تمنح Termux وصولاً كاملاً لكارت الذاكرة">Shizuku: جاري الفحص</span>
                <span class="pill" id="sdWritePill" title="هل يمكن الكتابة على كارت الذاكرة الخارجي؟">كتابة الكارت: جاري الفحص</span>
            </div>
        </div>
    </div>

    <div class="card">
        <div class="card-title">
            <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
            مصدر الملفات
        </div>
        <select id="source_select" onchange="updateSourceRoot(this.value)" style="margin-bottom: 12px;">
            {% for st in storages %}
                <option value="{{ st.path }}">{{ st.name }}</option>
            {% endfor %}
        </select>
        <div style="display: flex; gap: 8px; align-items: center;">
            <input type="text" id="custom_source_path" class="ltr-input" style="margin-bottom: 0; flex-grow: 1;" placeholder="/sdcard/..." readonly>
            <button class="btn btn-outline btn-sm" style="white-space: nowrap;" onclick="openPathBrowser('source')">
                <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
                تصفح
            </button>
        </div>
        <div style="display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap;">
            <button class="btn btn-outline btn-sm" id="cacheFolderBtn" style="white-space: nowrap; color: var(--accent); border-color: var(--accent);" onclick="cacheCurrentFolder()">
                <svg class="icon" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
                تخزين الفهرس (بحث فوري)
            </button>
            <span id="cacheStatusText" style="font-size:12px; color:var(--text-muted);">جاري التحقق من الفهرس...</span>
        </div>
    </div>

    <div class="card">
        <div class="card-title">
            <svg class="icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
            وجهة النقل
        </div>
        <select id="dest_select" onchange="updateDestPath(this)" style="margin-bottom: 12px;">
            <option value="">-- اضغط هنا لاختيار الوجهة --</option>
            {% for st in storages %}
                <option value="{{ st.path }}">{{ st.name }}</option>
            {% endfor %}
        </select>

        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <input type="text" id="custom_dest_path" class="ltr-input" style="margin-bottom: 0; flex-grow: 1;" placeholder="/storage/XXXX-XXXX/..." oninput="checkDiskSpace(this.value)">
            <button class="btn btn-outline btn-sm" style="white-space: nowrap;" onclick="openPathBrowser('dest')">
                <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
                تحديد
            </button>
            <button class="btn btn-outline btn-sm" style="white-space: nowrap; color: var(--accent); border-color: var(--accent);" onclick="runDiagnostic()">
                <svg class="icon" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg>
                فحص السرعة
            </button>
        </div>

        <div class="disk-space-container" id="diskSpaceDiv" style="margin-top: 5px;">
            <div style="display:flex; justify-content:space-between;">
                <span id="diskText">جاري الحساب...</span>
                <span id="diskPercent">0% مستخدم</span>
            </div>
            <div class="disk-bar"><div class="disk-used" id="diskFill"></div></div>
        </div>

        <div class="grid-2" style="margin-top: 14px;">
            <button class="btn btn-outline" style="color: var(--accent); border-color: var(--accent);" onclick="openJocModal()">
                <svg class="icon" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path></svg>
                إنشاء فهرس JOC
            </button>
        </div>
    </div>

    <div class="card">
        <div class="card-title">
            <svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
            بحث عن مجلد أو ملف
            <span id="searchSpeedBadge" style="display:none; font-size:10.5px; font-weight:600; color:var(--accent); background:var(--accent-soft); padding:2px 8px; border-radius:20px; margin-inline-start:4px;">⚡ فوري</span>
        </div>
        <div style="display:flex; gap:8px; margin-bottom: 12px;">
            <input type="text" id="searchInput" placeholder="اكتب اسم الملف للبحث..." style="margin-bottom:0;">
            <button class="btn btn-primary" onclick="searchDeep()">بحث</button>
        </div>
        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color: var(--text-muted); font-size:13px; margin-bottom:0;">
            <input type="checkbox" id="foldersOnlyCheck" class="checkbox" checked style="width:16px; height:16px; margin:0;">
            بحث في أسماء المجلدات فقط
        </label>
        <div id="searchResults" class="tree" style="display:none; max-height: 250px; margin-top: 12px;"></div>
    </div>

    <div class="card">
        <div class="card-title">
            <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
            متصفح الملفات (المصدر)
        </div>
        <div class="tree" id="treeContainer"></div>
    </div>

    <div class="card spoof-settings">
        <h4 style="margin:0 0 5px 0; font-size:14px; color:var(--text); font-weight: 500;">التمويه المستقل للتواريخ (على كامل الوجهة)</h4>
        <label>تاريخ ووقت البدء العشوائي:</label>
        <input type="datetime-local" id="spoofDateInput">
        <small style="color:var(--text-muted); line-height: 1.5;">عملية مستقلة لتعديل تواريخ الملفات والمجلدات لإخفاء آثار النقل. <br>في حال ترك الوجهة فارغة سيتم تطبيق التمويه على كامل الذاكرة (/sdcard).</small>
        <button class="btn btn-warning" style="margin-top:10px; width:100%;" onclick="startSpoofOnly()">
            تطبيق التمويه
        </button>
    </div>

    <div class="cart">
        <div style="display: flex; align-items: center; gap: 12px;">
            <div style="font-size: 14px; font-weight: 500; color:var(--text-muted);">محدد: <b id="selectedCount" style="color:#fff; font-size:16px;">0</b></div>
            <button class="btn btn-outline" style="padding: 8px 14px; font-size: 13px;" onclick="openSelectedModal()">
                <svg class="icon" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
                عرض المحدد
            </button>
        </div>
        <div class="cart-buttons">
            <button class="btn btn-primary" style="font-size: 14px; padding: 10px 24px;" onclick="startTransfer()">
                <svg class="icon" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                بدء النسخ
            </button>
        </div>
    </div>
</div>

<!-- Modals -->
<div class="modal" id="destBrowserModal">
    <div class="modal-content">
        <h3 id="pathBrowserTitle" style="margin-top:0; font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 15px;">اختر مجلد</h3>
        <div style="display:flex; gap: 8px; margin-bottom: 12px; margin-top: 10px;">
            <button class="btn btn-outline btn-sm" style="padding: 10px 14px;" onclick="goUpDest()">
                <svg class="icon" viewBox="0 0 24 24"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>
            </button>
            <input type="text" id="currentDestView" class="ltr-input" readonly style="margin: 0; background: var(--bg);">
        </div>
        <div id="destTree" class="tree" style="flex-grow: 1; max-height: 50vh;"></div>
        <div style="display:flex; justify-content:space-between; margin-top:15px; padding-top: 15px; border-top: 1px solid var(--border);">
            <button class="btn btn-outline btn-sm" onclick="createNewFolder()">+ مجلد جديد</button>
            <div style="display:flex; gap: 10px;">
                <button class="btn btn-outline btn-sm" onclick="document.getElementById('destBrowserModal').style.display='none'">إلغاء</button>
                <button class="btn btn-primary btn-sm" onclick="confirmPathPicker()">تأكيد</button>
            </div>
        </div>
    </div>
</div>

<div class="modal" id="inputModal">
    <div class="modal-content" style="max-width: 420px;">
        <h3 id="inputModalTitle" style="margin-top:0; font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 15px; display:flex; align-items:center; gap:8px;">
            <svg class="icon" style="color:var(--accent);" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
            <span id="inputModalTitleText">إعادة تسمية</span>
        </h3>
        <label id="inputModalLabel">الاسم الجديد:</label>
        <input type="text" id="inputModalField" style="margin-bottom: 4px;" onkeydown="if(event.key==='Enter') submitInputModal()">
        <div style="display:flex; gap:10px; margin-top:14px;">
            <button class="btn btn-outline" style="flex:1;" onclick="document.getElementById('inputModal').style.display='none'">إلغاء</button>
            <button class="btn btn-primary" style="flex:1;" onclick="submitInputModal()">حفظ</button>
        </div>
    </div>
</div>

<div class="modal" id="confirmModal">
    <div class="modal-content" style="max-width: 420px;">
        <h3 style="margin-top:0; font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 15px;">تأكيد الإجراء</h3>
        <p id="confirmMessage" style="color: var(--text); font-size: 14px; line-height: 1.6; margin: 4px 0 6px;"></p>
        <div style="display:flex; gap:10px; margin-top:14px;">
            <button class="btn btn-outline" style="flex:1;" onclick="cancelConfirm()">إلغاء</button>
            <button class="btn btn-danger" id="confirmActionBtn" style="flex:1;" onclick="submitConfirm()">تأكيد</button>
        </div>
    </div>
</div>

<div class="modal" id="selectedModal" onclick="closeSelectedModal(event)">
    <div class="modal-content" onclick="event.stopPropagation()">
        <div style="display:flex; flex-direction:column; gap:8px; border-bottom:1px solid var(--border); padding-bottom:15px; margin-bottom:12px;">
            <div style="display:flex; justify-content:space-between; align-items:center;">
                <h3 style="margin:0; font-size: 16px;">الملفات المحددة</h3>
                <button class="btn btn-outline" style="padding: 6px 10px; border:none; box-shadow: none;" onclick="document.getElementById('selectedModal').style.display='none'">
                    <svg class="icon" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                </button>
            </div>
            <div id="totalSizeSelected" style="font-size:13px; font-weight:500; background:var(--bg); padding:12px; border-radius:6px; border: 1px solid var(--border);">جاري حساب المساحة...</div>
        </div>
        <div id="selectedList" style="overflow-y:auto; flex-grow:1; max-height: 50vh;"></div>
    </div>
</div>

<!-- JOC Settings Modal -->
<div class="modal" id="jocModal">
    <div class="modal-content">
        <h3 style="margin-top:0; border-bottom: 1px solid var(--border); padding-bottom: 15px; display:flex; align-items:center; gap:8px; flex-shrink:0;">
            <svg class="icon" style="color:var(--accent);" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path></svg>
            إعدادات فهرس JOC
        </h3>
        <div class="modal-body">
        
        <div style="display: flex; gap: 10px;">
            <div style="flex: 1;">
                <label>رقم البداية (يبدأ من):</label>
                <input type="number" id="jocStart" value="1" min="1">
            </div>
            <div style="flex: 1;">
                <label>الحد الأقصى للحروف:</label>
                <input type="number" id="jocLimit" value="93" min="10">
            </div>
        </div>
        
        <label>الوضع (Mode):</label>
        <select id="jocMode">
            <option value="1">مجلدات فقط (فهرس رئيسي)</option>
            <option value="2">مجلدات وملفات (الوضع الشامل)</option>
            <option value="3">المجلد الحالي فقط (ملفات بدون تفرع)</option>
        </select>
        
        <label>تنسيق الإخراج (Template):</label>
        <select id="jocTemplate">
            <option value="4">الاسم : الرقم.</option>
            <option value="1">الرقم : الاسم . (مثال: 1 : سورة البقرة .)</option>
            <option value="2">الاسم من الرقم (مثال: سورة البقرة من 1)</option>
            <option value="3">الاسم من رقم الرقم . (مثال: سورة البقرة من رقم 1 .)</option>
        </select>
        
        <label>اسم الملف الناتج (نفس الاسم للنص والصوت):</label>
        <input type="text" id="jocFileName" class="ltr-input" placeholder="يتم توليد اسم عشوائي تلقائياً...">
        <small style="color:var(--text-muted); display:block; margin-top:-6px; margin-bottom:10px;">سيتم توليد: <span class="ltr-input" style="display:inline;">XXX.txt</span> (النص، يُحمَّل اختيارياً) و <span class="ltr-input" style="display:inline;">XXX.mp3</span> (الصوت) بنفس الاسم.<br>يُحبَّذ تركه فارغاً لإنشاء اسم عشوائي من 5 أحرف.</small>
        
        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color: var(--text); margin-top: 10px; margin-bottom: 10px;">
            <input type="checkbox" id="jocStrip" class="checkbox" checked style="margin:0;">
            حذف الحروف الإنجليزية والأرقام والرموز
        </label>

        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color: var(--text); margin-bottom: 10px;">
            <input type="checkbox" id="jocDownloadTxt" class="checkbox" style="margin:0;">
            تحميل ملف النص (.txt) بعد الإنشاء
        </label>
        <small style="color:var(--text-muted); line-height:1.6; display:block; margin-top:-6px; margin-bottom:14px;">يُولّد الفهرس نصياً دائماً في الخلفية، لكنه لا يُحمَّل إلا عند تفعيل هذا الخيار (التحميل ليس ضرورياً في كل مرة).</small>

        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color: var(--text); margin-bottom: 10px;">
            <input type="checkbox" id="jocReviewTxt" class="checkbox" style="margin:0;">
            مراجعة وتحرير الفهرس النصي قبل متابعة العملية
        </label>
        <small style="color:var(--text-muted); line-height:1.6; display:block; margin-top:-6px; margin-bottom:14px;">يفتح محرر النص بعد إنشاء الفهرس للسماح بالتعديل قبل التحميل/النسخ/التحويل إلى MP3.</small>

        <div style="background:var(--accent-soft); border:1px solid var(--accent); border-radius:10px; padding:14px; margin-bottom:14px;">
            <label style="display:flex; align-items:center; gap:10px; cursor:pointer; color:var(--text); font-size:14px; margin-bottom:8px;">
                <input type="checkbox" id="jocMp3Index" class="checkbox" style="margin:0; width:18px; height:18px;" onchange="toggleMp3Index()">
                تحويل الفهرس إلى MP3 صوتي (Edge TTS) 64kbps
            </label>
            <small style="color:var(--text-muted); line-height:1.6; display:block; margin-bottom:10px;">يتم توليد MP3 من نص الفهرس بشكل تلقائي (مقسم لأجزاء ثم يُدمج)، ويتطلب اتصال بالإنترنت. يتم حذف الملفات المؤقتة عند إغلاق الخادم.</small>
            <div id="ttsOptions" style="display:none; gap:10px; flex-direction:column;">
                <div class="grid-2">
                    <div>
                        <label>الصوت (اللكنة):</label>
                        <select id="ttsVoice"></select>
                    </div>
                    <div>
                        <label>السرعة:</label>
                        <select id="ttsRate">
                            <option value="+0%">عادية (+0%)</option>
                            <option value="-10%">أبطأ (-10%)</option>
                            <option value="-20%">أبطأ بكثير (-20%)</option>
                            <option value="+10%">أسرع (+10%)</option>
                            <option value="+20%">أسرع بكثير (+20%)</option>
                        </select>
                    </div>
                </div>

                <div style="margin-top:6px;">
                    <div style="display:flex; align-items:center; gap:8px; color:var(--text); font-size:13.5px; font-weight:600; margin-bottom:8px;">
                        <svg class="icon" style="width:16px;height:16px;" viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4V5z"></path><path d="M15.54 8.46a5 5 0 0 1 0 7.07"></path><path d="M19.07 4.93a10 10 0 0 1 0 14.14"></path></svg>
                        إخفاء الملف الصوتي (تمويه)
                    </div>
                    <div style="display:flex; gap:6px; background:var(--bg); border:1px solid var(--border); border-radius:10px; padding:4px;">
                        <label class="seg option" data-mode="none" style="flex:1; text-align:center; padding:9px 6px; border-radius:7px; cursor:pointer; font-size:12.5px; color:var(--text-muted); display:flex; align-items:center; justify-content:center; gap:5px;">
                            <svg class="icon" style="width:15px;height:15px;" viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4V5z"></path></svg>
                            بدون
                        </label>
                        <label class="seg option" data-mode="noise" style="flex:1; text-align:center; padding:9px 6px; border-radius:7px; cursor:pointer; font-size:12.5px; color:var(--text-muted); display:flex; align-items:center; justify-content:center; gap:5px;">
                            <svg class="icon" style="width:15px;height:15px;" viewBox="0 0 24 24"><line x1="1" y1="12" x2="6" y2="12"></line><line x1="23" y1="12" x2="18" y2="12"></line><line x1="9" y1="9" x2="15" y2="15"></line><line x1="15" y1="9" x2="9" y2="15"></line></svg>
                            ضوضاء خفيفة
                        </label>
                        <label class="seg option" data-mode="file" style="flex:1; text-align:center; padding:9px 6px; border-radius:7px; cursor:pointer; font-size:12.5px; color:var(--text-muted); display:flex; align-items:center; justify-content:center; gap:5px;">
                            <svg class="icon" style="width:15px;height:15px;" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>
                            ملف مخصص
                        </label>
                    </div>
                    <small id="maskHint" style="color:var(--text-muted); line-height:1.6; display:block; margin-top:6px;">إضافة ضوضاء منخفضة وغير مزعجة لمدة دقيقتين قبل بدء الفهرس الصوتي.</small>

                    <div id="customFileBox" style="display:none; margin-top:8px;">
                        <div style="display:flex; align-items:center; gap:10px; background:var(--bg); border:1px dashed var(--border); border-radius:10px; padding:10px 12px;">
                            <input type="file" id="jocCustomAudio" accept=".mp3,audio/mpeg" hidden>
                            <button class="btn btn-outline btn-sm" style="white-space:nowrap; flex-shrink:0;" onclick="document.getElementById('jocCustomAudio').click()">
                                <svg class="icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
                                اختر ملف MP3
                            </button>
                            <span id="customAudioName" style="flex:1; font-size:12.5px; color:var(--text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">لم يتم اختيار ملف بعد</span>
                        </div>
                        <small style="color:var(--text-muted); line-height:1.5; display:block; margin-top:6px;">يُضاف ملف MP3 المختار كاملاً في بداية الفهرس الصوتي.</small>
                    </div>
                </div>
            </div>
        </div>
        </div>
        <div class="modal-footer">
            <button class="btn btn-outline" style="flex:1;" onclick="document.getElementById('jocModal').style.display='none'">إلغاء</button>
            <button class="btn btn-primary" style="flex:2;" onclick="submitJocIndex()">إنشاء</button>
        </div>
    </div>
</div>

<div class="modal" id="editTxtModal">
    <div class="modal-content" style="max-width: 760px;">
        <h3 style="margin-top:0; border-bottom: 1px solid var(--border); padding-bottom: 15px; display:flex; align-items:center; gap:8px; flex-shrink:0;">
            <svg class="icon" style="color:var(--accent);" viewBox="0 0 24 24"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>
            مراجعة وتحرير الفهرس النصي
        </h3>
        <div class="modal-body" style="display:flex; flex-direction:column; gap:10px; min-height:0;">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap;">
                <small style="color:var(--text-muted);">عدّل النص ثم اضغط «حفظ ومتابعة» ليُستخدم في عملية التحميل/النسخ/التحويل إلى MP3.</small>
                <div style="display:flex; align-items:center; gap:8px;">
                    <small id="editTxtCount" style="color:var(--accent); font-weight:600; white-space:nowrap; flex-shrink:0;">0 حرف / 0 سطر</small>
                    <button class="btn btn-outline btn-sm" style="white-space:nowrap;" onclick="copyEditTxt()">
                        <svg class="icon" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                        نسخ
                    </button>
                </div>
            </div>
            <textarea id="editTxtArea" spellcheck="false" autocomplete="off" autocapitalize="off" autocorrect="off" style="flex:1; min-height:360px;" oninput="updateEditTxtCount()"></textarea>
        </div>
        <div class="modal-footer">
            <button class="btn btn-outline" style="flex:1;" onclick="cancelEditTxt()">إلغاء</button>
            <button class="btn btn-primary" style="flex:2;" onclick="saveEditTxt()">حفظ ومتابعة</button>
        </div>
    </div>
</div>

<div class="modal" id="progressModal">
    <div class="modal-content" style="max-height: max-content;">
        <h2 id="modalTitle" style="margin-top:0; font-size:16px; border-bottom: 1px solid var(--border); padding-bottom: 15px;">العملية قيد التنفيذ...</h2>
        <div style="display:flex; justify-content:space-between; font-size:13px; color:var(--text-muted); font-weight: 500; margin-top: 10px;">
            <span id="progressText">0%</span>
            <span id="countText">0 / 0</span>
        </div>
        <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
        <div id="currentFile" class="ltr-input truncate-text" style="font-size:12px; color:var(--text-muted); background:transparent; border:none; padding:0; width: 100%;">جاري التحضير...</div>
        <div style="display:flex; justify-content:flex-end; font-size:12px; color:var(--text-muted); margin-top:8px;">
            <span id="failedText"></span>
        </div>
        <div id="errorsBox" style="display:none; max-height:120px; overflow:auto; background:var(--bg); border:1px solid var(--border); border-radius:6px; padding:10px; margin-top:10px; font-size:12px; color:var(--danger);"></div>
        <div id="timeTakenDiv" style="display:none; color:var(--success); font-weight:500; margin-top:15px; font-size:13px; text-align:center;"></div>
        <div id="inlineNotify" style="display:none; flex-wrap:wrap; margin-top:14px; padding:12px 14px; border-radius:10px; font-size:13.5px; line-height:1.7; text-align:center; font-weight:600; border:1px solid; align-items:center; justify-content:center; gap:8px;"></div>
        <div style="display:flex; gap:12px; margin-top:20px;">
            <button class="btn btn-danger" id="cancelProgressBtn" style="width:100%; padding: 12px;" onclick="cancelTransfer()">إيقاف وإلغاء</button>
            <button class="btn btn-primary" id="closeProgressBtn" style="display:none; width:100%; padding: 12px;" onclick="resetTransfer()">تم وإغلاق</button>
        </div>
    </div>
</div>

<script>
    let selectedPaths = new Map();
    let excludedPaths = new Set();
    let currentDestFreeBytes = 0; 
    let statusTimeout = null;
    let currentSourceRoot = localStorage.getItem('joc_source_root') || '/sdcard';
    let pickerMode = 'dest'; 

    function isPathSelected(path) {
        let bestRoot = null;
        for (let root of selectedPaths.keys()) {
            if (path === root || path.startsWith(root + '/')) {
                if (bestRoot === null || root.length > bestRoot.length) bestRoot = root;
            }
        }
        if (bestRoot === null) return false;
        for (let excl of excludedPaths) {
            if (excl !== bestRoot && !excl.startsWith(bestRoot + '/')) continue;
            if (path === excl || path.startsWith(excl + '/')) return false;
        }
        return true;
    }

    function isPathExplicitlyExcluded(path) { return excludedPaths.has(path); }

    function includePath(path, isDir) {
        excludedPaths.delete(path);
        for (let excl of Array.from(excludedPaths)) {
            if (excl.startsWith(path + '/')) excludedPaths.delete(excl);
        }
        let hasAncestorRoot = false;
        for (let root of selectedPaths.keys()) {
            if (path.startsWith(root + '/')) { hasAncestorRoot = true; break; }
        }
        if (!hasAncestorRoot) {
            selectedPaths.set(path, isDir);
            for (let root of Array.from(selectedPaths.keys())) {
                if (root !== path && root.startsWith(path + '/')) selectedPaths.delete(root);
            }
        }
    }

    function excludePath(path) {
        if (selectedPaths.has(path)) {
            selectedPaths.delete(path);
            for (let excl of Array.from(excludedPaths)) {
                if (excl === path || excl.startsWith(path + '/')) excludedPaths.delete(excl);
            }
            return;
        }
        let covered = false;
        for (let root of selectedPaths.keys()) {
            if (path.startsWith(root + '/')) { covered = true; break; }
        }
        if (covered) {
            excludedPaths.add(path);
            for (let excl of Array.from(excludedPaths)) {
                if (excl !== path && excl.startsWith(path + '/')) excludedPaths.delete(excl);
            }
        }
    }

    function refreshCheckboxStates() {
        document.querySelectorAll('.checkbox').forEach(cb => { if(cb.value && cb.value.startsWith('/')) cb.checked = isPathSelected(cb.value); });
        document.getElementById('selectedCount').innerText = selectedPaths.size;
    }
    
    const iconDir = `<svg class="icon" style="min-width:20px; color:var(--accent)" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>`;
    const iconFile = `<svg class="icon" style="min-width:20px; color:var(--text-muted)" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>`;
    const iconEdit = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>`;
    const iconTrash = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>`;

    function rowActionsHtml(path, name, isDir) {
        let safePath = path.replace(/'/g, "\\'");
        let safeName = name.replace(/'/g, "\\'");
        return `<div class="row-actions">
            <button type="button" class="icon-btn" title="إعادة تسمية" onclick="event.stopPropagation(); handleRenameClick('${safePath}', '${safeName}', this)">${iconEdit}</button>
            <button type="button" class="icon-btn danger" title="حذف" onclick="event.stopPropagation(); handleDeleteClick('${safePath}', ${isDir}, this)">${iconTrash}</button>
        </div>`;
    }

    window.onload = function() {
        document.getElementById('source_select').value = currentSourceRoot;
        document.getElementById('custom_source_path').value = currentSourceRoot;
        let savedDest = localStorage.getItem('joc_last_dest');
        if (savedDest) {
            document.getElementById('custom_dest_path').value = savedDest;
            checkDiskSpace(savedDest);
        }
        loadSubFolders(currentSourceRoot, document.getElementById('treeContainer'));
        generateRandomSpoofDate();
        checkTransferStatus(); 
        checkCacheStatus(currentSourceRoot);
    };

    function updateSourceRoot(path) {
        if (!path) return;
        currentSourceRoot = path;
        localStorage.setItem('joc_source_root', path);
        document.getElementById('custom_source_path').value = path;
        loadSubFolders(path, document.getElementById('treeContainer'));
        checkCacheStatus(path);
    }

    function renderCacheStatus(data) {
        let el = document.getElementById('cacheStatusText');
        let badge = document.getElementById('searchSpeedBadge');
        if (data.cached) {
            let d = new Date(data.built_at * 1000);
            let timeStr = d.toLocaleTimeString('ar-EG', {hour: '2-digit', minute:'2-digit'});
            el.innerHTML = `<span style="color:var(--success);">✓ مفهرس (${data.count} عنصر) — ${timeStr}</span>`;
            badge.style.display = 'inline-block';
        } else {
            el.innerHTML = `<span style="color:var(--text-muted);">غير مفهرس — البحث سيكون أبطأ في المجلدات الكبيرة</span>`;
            badge.style.display = 'none';
        }
    }

    function pollCacheStatus(path) {
        fetch('/api/cache/status?path=' + encodeURIComponent(path)).then(r=>r.json()).then(data=>{
            if (path !== currentSourceRoot) return; 
            if (data.building) {
                document.getElementById('cacheStatusText').innerText = 'جاري الفهرسة...';
                setTimeout(() => pollCacheStatus(path), 800);
            } else {
                document.getElementById('cacheFolderBtn').disabled = false;
                renderCacheStatus(data);
            }
        }).catch(() => { document.getElementById('cacheFolderBtn').disabled = false; });
    }

    function checkCacheStatus(path) {
        if (!path) return;
        fetch('/api/cache/status?path=' + encodeURIComponent(path)).then(r=>r.json()).then(data=>{
            if (path !== currentSourceRoot) return;
            if (data.building) pollCacheStatus(path);
            else renderCacheStatus(data);
        }).catch(() => {});
    }

    function cacheCurrentFolder() {
        let path = currentSourceRoot;
        if (!path) return;
        document.getElementById('cacheFolderBtn').disabled = true;
        document.getElementById('cacheStatusText').innerText = 'جاري الفهرسة...';
        fetch('/api/cache/build', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: path})
        }).then(r => r.json()).then(res => {
            if (res.error) {
                alert(res.error);
                document.getElementById('cacheFolderBtn').disabled = false;
                checkCacheStatus(path);
                return;
            }
            pollCacheStatus(path);
        }).catch(() => { document.getElementById('cacheFolderBtn').disabled = false; });
    }

    function generateRandomSpoofDate() {
        let start = new Date(2019, 0, 1).getTime();
        let end = new Date(2023, 11, 31).getTime();
        let randDate = new Date(start + Math.random() * (end - start));
        let tzoffset = randDate.getTimezoneOffset() * 60000;
        let localISOTime = (new Date(randDate - tzoffset)).toISOString().slice(0, 16);
        document.getElementById('spoofDateInput').value = localISOTime;
    }

    function updateDestPath(select) {
        if(select.value !== "") {
            document.getElementById('custom_dest_path').value = select.value;
            localStorage.setItem('joc_last_dest', select.value);
            checkDiskSpace(select.value);
        }
    }

    function checkDiskSpace(path) {
        if(!path) return;
        localStorage.setItem('joc_last_dest', path);
        fetch('/api/disk_usage?path=' + encodeURIComponent(path))
            .then(r => r.json())
            .then(data => {
                if(!data.error) {
                    currentDestFreeBytes = data.free_bytes; 
                    document.getElementById('diskSpaceDiv').style.display = 'block';
                    document.getElementById('diskText').innerText = `المتبقي: ${data.free} / الإجمالي: ${data.total}`;
                    document.getElementById('diskPercent').innerText = `${data.percent}% مستخدم`;
                    document.getElementById('diskFill').style.width = `${data.percent}%`;
                    if(data.percent > 90) document.getElementById('diskFill').style.background = 'var(--danger)';
                    else document.getElementById('diskFill').style.background = 'var(--accent)';
                }
            });
    }

    function openPathBrowser(mode) {
        pickerMode = mode;
        document.getElementById('pathBrowserTitle').innerText = (mode === 'dest') ? 'اختر مجلد الوجهة' : 'اختر مجلد المصدر';
        let inputId = (mode === 'dest') ? 'custom_dest_path' : 'custom_source_path';
        let val = document.getElementById(inputId).value;
        loadDestBrowser(val ? val : (mode === 'dest' ? '/sdcard' : currentSourceRoot));
        document.getElementById('destBrowserModal').style.display = 'flex';
    }

    function loadDestBrowser(path) {
        document.getElementById('currentDestView').value = path;
        let tree = document.getElementById('destTree');
        tree.dataset.dirPath = path;
        tree.innerHTML = '<div style="padding:10px;">جاري التحميل...</div>';
        fetch('/api/items?path=' + encodeURIComponent(path))
            .then(res => res.json())
            .then(data => {
                tree.innerHTML = '';
                let dirsOnly = data.items.filter(i => i.is_dir);
                if (dirsOnly.length === 0) { tree.innerHTML = '<div style="padding:10px; color:var(--text-muted);">لا توجد مجلدات فرعية</div>'; }
                dirsOnly.forEach(item => {
                    let div = document.createElement('div');
                    div.className = 'item-row';
                    div.innerHTML = `<div class="item-name" style="cursor:pointer;">${iconDir} <span>${item.name}</span></div>${rowActionsHtml(item.path, item.name, true)}`;
                    div.querySelector('.item-name').onclick = () => loadDestBrowser(item.path);
                    tree.appendChild(div);
                });
            });
    }
    
    function goUpDest() {
        let parts = document.getElementById('currentDestView').value.split('/').filter(p=>p);
        parts.pop();
        loadDestBrowser('/' + parts.join('/'));
    }
    function confirmPathPicker() {
        let p = document.getElementById('currentDestView').value;
        if (pickerMode === 'source') {
            updateSourceRoot(p);
        } else {
            document.getElementById('custom_dest_path').value = p;
            checkDiskSpace(p);
        }
        document.getElementById('destBrowserModal').style.display = 'none';
    }
    function createNewFolder() {
        showInputModal('مجلد جديد', 'اسم المجلد:', '', (name) => {
            if(!name) return;
            fetch('/api/mkdir', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({path: document.getElementById('currentDestView').value, name: name})
            }).then(r=>r.json()).then(res=>{
                if(res.success) loadDestBrowser(res.new_path);
                else alert(res.error || 'تعذر إنشاء المجلد');
            });
        });
    }
    function loadSubFolders(path, parentElement) {
        parentElement.innerHTML = '<div style="padding:10px; color:var(--text-muted);">جاري التحميل...</div>';
        parentElement.dataset.dirPath = path;
        fetch('/api/items?path=' + encodeURIComponent(path)).then(r=>r.json()).then(data=>{
            parentElement.innerHTML = '';
            if (data.items.length === 0) { parentElement.innerHTML = '<div style="padding:10px; color:var(--text-muted);">مجلد فارغ</div>'; return; }
            data.items.forEach(item => {
                let isChecked = isPathSelected(item.path) ? 'checked' : '';
                let excludedMark = isPathExplicitlyExcluded(item.path) ? '<span class="excluded-badge">مستثنى</span>' : '';
                let safePath = item.path.replace(/'/g, "\\'");
                let div = document.createElement('div');
                div.innerHTML = `
                    <div class="item-row">
                        ${item.is_dir ? `<button class="toggle-btn" onclick="toggleFolder(this, '${safePath}')">▶</button>` : `<span style="width:26px;display:inline-block;"></span>`}
                        <input type="checkbox" class="checkbox" value="${item.path}" ${isChecked} onchange="toggleSelect(this, ${item.is_dir})">
                        <div class="item-name" onclick="${item.is_dir ? `toggleFolder(this.previousElementSibling.previousElementSibling, '${safePath}')` : `this.previousElementSibling.click()`}">
                            ${item.is_dir ? iconDir : iconFile} <span>${item.name}</span>${excludedMark}
                        </div>
                        ${rowActionsHtml(item.path, item.name, item.is_dir)}
                    </div>
                    ${item.is_dir ? '<div class="children"></div>' : ''}
                `;
                parentElement.appendChild(div);
            });
        });
    }

    function showInputModal(title, label, defaultValue, onConfirm) {
        document.getElementById('inputModalTitleText').innerText = title;
        document.getElementById('inputModalLabel').innerText = label;
        let field = document.getElementById('inputModalField');
        field.value = defaultValue || '';
        document.getElementById('inputModal').style.display = 'flex';
        window._inputModalCallback = onConfirm;
        setTimeout(() => { field.focus(); field.select(); }, 60);
    }
    function submitInputModal() {
        let val = document.getElementById('inputModalField').value.trim();
        document.getElementById('inputModal').style.display = 'none';
        if (window._inputModalCallback) window._inputModalCallback(val);
    }

    function showConfirmModal(message, onConfirm) {
        document.getElementById('confirmMessage').innerText = message;
        document.getElementById('confirmModal').style.display = 'flex';
        window._confirmModalCallback = onConfirm;
    }
    function submitConfirm() {
        document.getElementById('confirmModal').style.display = 'none';
        if (window._confirmModalCallback) window._confirmModalCallback();
        window._confirmModalCallback = null;
    }
    function cancelConfirm() {
        document.getElementById('confirmModal').style.display = 'none';
        window._confirmModalCallback = null;
    }

    function purgePathFromSelections(path) {
        selectedPaths.delete(path);
        for (let k of Array.from(selectedPaths.keys())) if (k.startsWith(path + '/')) selectedPaths.delete(k);
        excludedPaths.delete(path);
        for (let k of Array.from(excludedPaths)) if (k.startsWith(path + '/')) excludedPaths.delete(k);
        refreshCheckboxStates();
    }

    function renamePathInSelections(oldPath, newPath) {
        if (selectedPaths.has(oldPath)) { selectedPaths.set(newPath, selectedPaths.get(oldPath)); selectedPaths.delete(oldPath); }
        for (let k of Array.from(selectedPaths.keys())) {
            if (k.startsWith(oldPath + '/')) { let nk = newPath + k.slice(oldPath.length); selectedPaths.set(nk, selectedPaths.get(k)); selectedPaths.delete(k); }
        }
        if (excludedPaths.has(oldPath)) { excludedPaths.delete(oldPath); excludedPaths.add(newPath); }
        for (let k of Array.from(excludedPaths)) {
            if (k.startsWith(oldPath + '/')) { excludedPaths.delete(k); excludedPaths.add(newPath + k.slice(oldPath.length)); }
        }
        refreshCheckboxStates();
    }

    function refreshNearestContainer(btnEl) {
        if (btnEl.closest('#searchResults')) { searchDeep(); return; }
        if (btnEl.closest('#destTree')) { loadDestBrowser(document.getElementById('currentDestView').value); return; }
        let container = btnEl.closest('.children') || btnEl.closest('#treeContainer');
        if (container && container.dataset && container.dataset.dirPath) loadSubFolders(container.dataset.dirPath, container);
    }

    function handleRenameClick(path, currentName, btnEl) {
        showInputModal('إعادة تسمية', 'الاسم الجديد:', currentName, (newName) => {
            if (!newName || newName === currentName) return;
            fetch('/api/rename', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: path, new_name: newName})
            }).then(r => r.json()).then(res => {
                if (!res.success) { alert(res.error || 'فشل التعديل'); return; }
                renamePathInSelections(res.old_path, res.new_path);
                refreshNearestContainer(btnEl);
            }).catch(() => alert('خطأ في الاتصال بالخادم'));
        });
    }

    function handleDeleteClick(path, isDir, btnEl) {
        let name = path.split('/').filter(Boolean).pop() || path;
        showConfirmModal(`سيتم حذف "${name}" نهائياً${isDir ? ' مع كل محتوياته' : ''}. لا يمكن التراجع عن هذا الإجراء. هل تريد المتابعة؟`, () => {
            fetch('/api/delete', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: path})
            }).then(r => r.json()).then(res => {
                if (!res.success) { alert(res.error || 'فشل الحذف'); return; }
                purgePathFromSelections(path);
                refreshNearestContainer(btnEl);
            }).catch(() => alert('خطأ في الاتصال بالخادم'));
        });
    }
    
    function toggleFolder(btn, path) {
        let children = btn.closest('.item-row').nextElementSibling;
        if(children.classList.contains('open')){ children.classList.remove('open'); btn.innerText='▶';}
        else { if(!children.innerHTML) loadSubFolders(path, children); children.classList.add('open'); btn.innerText='▼';}
    }

    function toggleSelect(cb, isDir) {
        if(cb.checked) includePath(cb.value, isDir);
        else excludePath(cb.value);
        refreshCheckboxStates();
    }

    let searchDebounceTimer = null;
    let searchRequestToken = 0;
    function searchDeep() {
        let q = document.getElementById('searchInput').value.trim();
        let foldersOnly = document.getElementById('foldersOnlyCheck').checked; 
        let resBox = document.getElementById('searchResults');
        if(!q) { resBox.style.display = 'none'; resBox.innerHTML = ''; return; }
        resBox.style.display = 'block'; resBox.innerHTML = '<div style="padding:10px;color:var(--text-muted);">جاري البحث السريع...</div>';

        let myToken = ++searchRequestToken;
        fetch('/api/search?q=' + encodeURIComponent(q) + '&folders_only=' + foldersOnly + '&root=' + encodeURIComponent(currentSourceRoot))
            .then(r=>r.json()).then(data=>{
            if (myToken !== searchRequestToken) return; 
            document.getElementById('searchSpeedBadge').style.display = data.from_cache ? 'inline-block' : 'none';
            resBox.innerHTML = '';
            if(data.results.length === 0) { resBox.innerHTML = '<div style="padding:10px;color:var(--text-muted);">لم يتم العثور على نتائج.</div>'; return; }
            data.results.forEach(item => {
                let isChecked = isPathSelected(item.path) ? 'checked' : '';
                resBox.innerHTML += `<div class="item-row"><input type="checkbox" class="checkbox" value="${item.path}" ${isChecked} onchange="toggleSelect(this, ${item.is_dir})"><div class="item-name" style="flex-direction:column;align-items:start;gap:4px;" onclick="this.previousElementSibling.click()"><div style="display:flex;gap:6px;">${item.is_dir?iconDir:iconFile}${item.name}</div><div class="ltr-input truncate-text" style="font-size:11px;color:var(--text-muted); width: 100%;">${item.path}</div></div>${rowActionsHtml(item.path, item.name, item.is_dir)}</div>`;
            });
        }).catch(() => { if (myToken === searchRequestToken) resBox.innerHTML = '<div style="padding:10px;color:var(--danger);">تعذر إتمام البحث.</div>'; });
    }

    document.getElementById('searchInput').addEventListener('input', function() {
        clearTimeout(searchDebounceTimer);
        if (this.value.trim().length === 0) { document.getElementById('searchResults').style.display = 'none'; return; }
        searchDebounceTimer = setTimeout(searchDeep, 400);
    });
    document.getElementById('searchInput').addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { clearTimeout(searchDebounceTimer); searchDeep(); }
    });

    function updateSizeDisplay(totalBytes) {
        let formattedSize = '';
        let mbThreshold = 1000 * 1024 * 1024; 
        if (totalBytes >= mbThreshold) formattedSize = (totalBytes / (1024*1024*1024)).toFixed(2) + ' GB';
        else formattedSize = (totalBytes / (1024*1024)).toFixed(2) + ' MB';
        
        let sizeHtml = `إجمالي الحجم المحدد: <span style="color:var(--text); font-size:14px; font-weight:600;">${formattedSize}</span>`;
        if (currentDestFreeBytes > 0) {
            let diff = currentDestFreeBytes - totalBytes;
            if (diff >= 0) {
                let diffFmt = diff >= mbThreshold ? (diff/(1024*1024*1024)).toFixed(2) + ' GB' : (diff/(1024*1024)).toFixed(2) + ' MB';
                sizeHtml += `<br><span style="color:var(--success); font-size:12px; margin-top:4px; display:block;">المساحة كافية (المتبقي: ${diffFmt})</span>`;
            } else {
                let absDiff = Math.abs(diff);
                let diffFmt = absDiff >= mbThreshold ? (absDiff/(1024*1024*1024)).toFixed(2) + ' GB' : (absDiff/(1024*1024)).toFixed(2) + ' MB';
                sizeHtml += `<br><span style="color:var(--danger); font-size:12px; margin-top:4px; display:block;">المساحة لا تكفي (عجز: -${diffFmt})</span>`;
            }
        } else { sizeHtml += `<br><span style="color:var(--text-muted); font-size:12px; margin-top:4px; display:block;">يرجى تحديد الوجهة لمعرفة المساحة.</span>`; }
        document.getElementById('totalSizeSelected').innerHTML = sizeHtml;
    }

    async function openSelectedModal() {
        let list = document.getElementById('selectedList');
        list.innerHTML = '';
        document.getElementById('totalSizeSelected').innerHTML = 'جاري الحساب...';
        
        if (selectedPaths.size === 0) {
            list.innerHTML = '<div style="text-align:center; padding:40px; color:var(--text-muted);">لا يوجد عناصر محددة</div>';
            document.getElementById('totalSizeSelected').innerHTML = 'لا توجد ملفات محددة';
        } else {
            let idx = 0;
            let pathsArray = Array.from(selectedPaths.keys());
            pathsArray.forEach(path => {
                let isDir = selectedPaths.get(path);
                let itemName = path.split('/').filter(Boolean).pop() || path;
                list.innerHTML += `
                    <div class="item-row" style="justify-content: space-between; margin-bottom: 8px; background: var(--bg); padding: 12px; border: 1px solid var(--border); border-radius: 6px;">
                        <div style="display:flex; align-items:center; gap:10px; overflow:hidden; width:65%;" title="${path}">
                            ${isDir ? iconDir : iconFile}
                            <div class="truncate-text" style="font-size:13px; color:#e2e8f0; width:100%; font-weight:500;">${itemName}</div>
                        </div>
                        <div style="display:flex; align-items:center; gap:12px; flex-shrink: 0;">
                            <span id="size_label_${idx}" style="font-size:11px; color:var(--text-muted); min-width: 50px; text-align: right;">يحسب..</span>
                            <button class="btn btn-outline" style="padding: 6px 10px; font-size: 12px; color:var(--danger); border-color:var(--danger);" onclick="unselectFromModal('${path.replace(/'/g, "\\'")}')">إزالة</button>
                        </div>
                    </div>
                `;
                idx++;
            });
            if (excludedPaths.size > 0) {
                list.innerHTML += `<div style="margin:20px 0 10px; font-size:13px; color:var(--warning); font-weight:500;">عناصر مستثناة (${excludedPaths.size}):</div>`;
                Array.from(excludedPaths).forEach(path => {
                    let safePath = path.replace(/'/g, "\\'");
                    let itemName = path.split('/').filter(Boolean).pop() || path;
                    list.innerHTML += `
                        <div class="item-row" style="justify-content: space-between; margin-bottom: 8px; background: rgba(245, 158, 11, 0.05); padding: 12px; border: 1px dashed rgba(245, 158, 11, 0.3); border-radius: 6px;">
                            <div class="truncate-text" style="font-size:13px; color:#fca5a5; width:70%;" title="${path}">${itemName}</div>
                            <button class="btn btn-outline" style="padding: 6px 10px; font-size: 12px; flex-shrink: 0;" onclick="reincludeFromModal('${safePath}', false)">إرجاع</button>
                        </div>
                    `;
                });
            }
            document.getElementById('selectedModal').style.display = 'flex';
            
            let totalBytes = 0;
            updateSizeDisplay(totalBytes);
            for(let i=0; i<pathsArray.length; i++) {
                try {
                    let res = await fetch('/api/size', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({path: pathsArray[i], excludes: Array.from(excludedPaths)})
                    });
                    let data = await res.json();
                    document.getElementById('size_label_' + i).innerText = data.formatted;
                    totalBytes += data.bytes;
                    updateSizeDisplay(totalBytes);
                } catch(e) {}
            }
        }
    }
    
    function unselectFromModal(path) { excludePath(path); refreshCheckboxStates(); openSelectedModal(); }
    function reincludeFromModal(path, isDir) { includePath(path, isDir); refreshCheckboxStates(); openSelectedModal(); }
    function closeSelectedModal(e) { if(e.target.id === 'selectedModal') e.target.style.display = 'none'; }

    function startTransfer() {
        if(selectedPaths.size === 0) return alert('اختر ملفات أولاً.');
        let dest = document.getElementById('custom_dest_path').value;
        if(!dest) return alert('حدد مسار الوجهة.');

        let reqData = { dest: dest, paths: Array.from(selectedPaths.keys()), excludes: Array.from(excludedPaths) };

        fetch('/transfer/start', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reqData)
        }).then(r=>r.json()).then(data=>{
            if(data.error) alert(data.error); else showProgressModal();
        });
    }

    function startSpoofOnly() {
        let dest = document.getElementById('custom_dest_path').value;
        let spoofDate = document.getElementById('spoofDateInput').value;
        if(!spoofDate) return alert('حدد التاريخ والوقت أولاً.');

        if(!dest) {
            if(confirm('لم تقم بتحديد مسار وجهة.\\nهل تريد تطبيق التمويه على كامل الذاكرة الداخلية (/sdcard)؟')) {
                dest = '/sdcard';
            } else {
                return;
            }
        } else {
            if(!confirm(`سيتم تغيير تواريخ كل شيء داخل:\\n${dest}\\nهل أنت متأكد؟`)) return;
        }

        fetch('/transfer/spoof_only', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({dest: dest, spoof_date: spoofDate})
        }).then(r=>r.json()).then(data=>{ if(data.error) alert(data.error); else showProgressModal(); });
    }

    function runDiagnostic() {
        let dest = document.getElementById('custom_dest_path').value;
        if(!dest) return alert('حدد مسار الوجهة أولاً للفحص.');
        
        document.getElementById('currentFile').innerText = 'جاري إجراء اختبار كتابة البيانات الفعلي (I/O)...';
        showProgressModal();
        document.getElementById('modalTitle').innerText = 'جاري فحص الوجهة...';
        document.getElementById('cancelProgressBtn').style.display = 'none';

        fetch('/api/benchmark', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({dest: dest})
        }).then(r=>r.json()).then(data => {
            if(data.error) {
                alert('خطأ أثناء الفحص: ' + data.error);
                resetTransfer();
            } else {
                let estMin = Math.floor(data.est_10gb / 60);
                let msg = `سرعة الكتابة الفعلية: ${data.speed_mbps} MB/s\\n\\n`;
                msg += `الوقت التقديري لنسخ 10 جيجابايت: حوالي ${estMin} دقيقة.`;
                alert(msg);
                resetTransfer();
                document.getElementById('modalTitle').innerText = 'العملية قيد التنفيذ...';
            }
        });
    }

    let maskMode = 'noise';   // 'none' | 'noise' | 'file'
    let customUploaded = null;  // original filename of uploaded custom mask

    function toggleMp3Index() {
        let on = document.getElementById('jocMp3Index').checked;
        document.getElementById('ttsOptions').style.display = on ? 'flex' : 'none';
    }

    function setMaskMode(mode) {
        maskMode = mode;
        document.querySelectorAll('.seg.option').forEach(el => {
            el.classList.toggle('active', el.getAttribute('data-mode') === mode);
        });
        document.getElementById('customFileBox').style.display = (mode === 'file') ? 'block' : 'none';
        let hint = document.getElementById('maskHint');
        if(mode === 'none') hint.innerHTML = 'يبدأ الفهرس الصوتي مباشرة دون أي تمويه.';
        else if(mode === 'noise') hint.innerHTML = 'إضافة ضوضاء منخفضة وغير مزعجة لمدة دقيقتين قبل بدء الفهرس الصوتي لإخفائه.';
        else hint.innerHTML = 'إضافة ملف MP3 مخصص كاملاً في بداية الفهرس الصوتي لإخفائه.';
    }

    function uploadCustomAudio(file) {
        if(!file) return;
        if(!/\\.mp3$/i.test(file.name)) { showInlineError('يرجى اختيار ملف بصيغة MP3 فقط.'); setMaskMode('none'); return; }
        let fd = new FormData();
        fd.append('file', file);
        fetch('/api/tts/custom', { method: 'POST', body: fd })
            .then(r=>r.json()).then(res=>{
                if(res.error) { showInlineError(res.error); setMaskMode('none'); document.getElementById('customAudioName').textContent = 'لم يتم اختيار ملف بعد'; return; }
                customUploaded = res.name || file.name;
                document.getElementById('customAudioName').textContent = 'سيُضاف: ' + customUploaded;
            }).catch(()=>{ showInlineError('فشل رفع الملف المخصص.'); setMaskMode('none'); });
    }

    function loadSystemInfo(force, isRetry) {
        fetch('/api/system' + (force ? '?refresh=1' : ''))
            .then(function(r){ return r.json(); })
            .then(function(d){
                var vEl = document.getElementById('appVersionPill');
                if (vEl && d.version) vEl.textContent = 'v' + d.version;

                var s = d.shizuku || {};
                var sEl = document.getElementById('shizukuPill');
                if (sEl) {
                    if (!s.installed) { sEl.textContent = 'Shizuku: غير مثبت'; sEl.className = 'pill'; }
                    else if (!s.running) { sEl.textContent = 'Shizuku: متوقف'; sEl.className = 'pill pill-warn'; }
                    else { sEl.textContent = 'Shizuku: مفعّل'; sEl.className = 'pill pill-ok'; }
                }

                var sd = d.sd || {};
                var sdEl = document.getElementById('sdWritePill');
                if (sdEl) {
                    if (sd.writable === true) { sdEl.textContent = 'كتابة الكارت: نعم'; sdEl.className = 'pill pill-ok'; }
                    else if (sd.writable === false) { sdEl.textContent = 'كتابة الكارت: لا'; sdEl.className = 'pill pill-warn'; }
                    else { sdEl.textContent = 'كتابة الكارت: لا يوجد كارت'; sdEl.className = 'pill'; }
                }

                if (s.checked === false && !isRetry) {
                    setTimeout(function(){ loadSystemInfo(true, true); }, 2000);
                }
            })
            .catch(function(){});
    }

    document.addEventListener('DOMContentLoaded', function() {
        document.querySelectorAll('.seg.option').forEach(el => {
            el.addEventListener('click', function() { setMaskMode(this.getAttribute('data-mode')); });
        });
        const fin = document.getElementById('jocCustomAudio');
        if(fin) fin.addEventListener('change', function(ev) {
            let f = ev.target.files && ev.target.files[0];
            if(f) uploadCustomAudio(f);
        });
        loadSystemInfo(false, false);
        setInterval(function(){ loadSystemInfo(false, true); }, 120000);
    });

    function loadTtsVoices() {
        fetch('/api/tts/voices').then(r=>r.json()).then(data=>{
            let sel = document.getElementById('ttsVoice');
            sel.innerHTML = '';
            let defaultVoice = data.default || 'ar-AE-HamdanNeural';
            Object.keys(data.voices || {}).forEach(v => {
                let o = document.createElement('option');
                o.value = v;
                o.textContent = data.voices[v] + ' — ' + v;
                o.selected = (v === defaultVoice);
                sel.appendChild(o);
            });
            if(!data.available) {
                sel.disabled = true;
                document.getElementById('ttsOptions').innerHTML = '<small style="color:var(--danger);">حزمة edge-tts غير مثبتة (يرجى تشغيل: pip install edge-tts)</small>';
            }
        }).catch(()=>{});
    }

    function makeRandomName(len) {
        const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
        let s = '';
        for (let i = 0; i < len; i++) s += chars[Math.floor(Math.random() * chars.length)];
        return s;
    }

    function openJocModal() {
        let dest = document.getElementById('custom_dest_path').value;
        if(!dest) return alert('يرجى تحديد مسار الوجهة أولاً.');
        // Default name: random 5-char alphanumeric (kept if user typed one before)
        document.getElementById('jocFileName').value = makeRandomName(5);
        loadTtsVoices();
        setMaskMode('noise');
        document.getElementById('jocModal').style.display = 'flex';
    }

    let currentTtsFileName = null;
    let currentJocIndexText = null;
    let pendingJocActions = null;

    function copyTextToClipboard(text, successMsg) {
        if(text === null || text === undefined || text === '') { showInlineError('لا يوجد محتوى لنسخه.'); return false; }
        let ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        ta.style.top = '0';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        ta.setSelectionRange(0, ta.value.length);
        let ok = false;
        try { ok = document.execCommand('copy'); } catch(e) {}
        document.body.removeChild(ta);
        if(ok) { showInlineDone(successMsg); return true; }
        else { showInlineError('تعذر النسخ تلقائياً، يرجى نسخ النص يدوياً.'); return false; }
    }

    function copyJocIndex() {
        copyTextToClipboard(currentJocIndexText, 'تم نسخ الفهرس بنجاح <b>' + (document.getElementById('jocFileName').value || '') + '.txt</b> — الصقه في أي مكان.');
    }

    function copyEditTxt() {
        let txtArea = document.getElementById('editTxtArea');
        let txt = txtArea ? txtArea.value : '';
        copyTextToClipboard(txt, 'تم نسخ النص من المحرر بنجاح — الصقه في أي مكان.');
    }

    function submitJocIndex() {
        let dest = document.getElementById('custom_dest_path').value;
        let startX = document.getElementById('jocStart').value;
        let limit = document.getElementById('jocLimit').value;
        let mode = document.getElementById('jocMode').value;
        let template = document.getElementById('jocTemplate').value;
        let nameBase = (document.getElementById('jocFileName').value || '').trim().replace(/\\.(txt|mp3)$/i, '');
        if(!nameBase) nameBase = makeRandomName(5);
        let txtName = nameBase + '.txt';
        let strip = document.getElementById('jocStrip').checked;
        let doMp3 = document.getElementById('jocMp3Index').checked;
        let downloadTxt = document.getElementById('jocDownloadTxt').checked;
        let reviewTxt = document.getElementById('jocReviewTxt').checked;
        let obfuscate = false;
        if(doMp3) {
            if(maskMode === 'file') {
                if(!customUploaded) { alert('يرجى اختيار ملف MP3 مخصص أولاً، أو اختر وضع تمويه آخر.'); return; }
                obfuscate = 'file';
            } else if(maskMode === 'noise') {
                obfuscate = true;
            }
        }
        let mp3Name = nameBase + '.mp3';
        let voice = document.getElementById('ttsVoice').value || 'ar-AE-HamdanNeural';
        let rate = document.getElementById('ttsRate').value || '+0%';
        pendingJocActions = { txtName: txtName, downloadTxt: downloadTxt, doMp3: doMp3, mp3Name: mp3Name, voice: voice, rate: rate, obfuscate: obfuscate };
        
        document.getElementById('jocModal').style.display = 'none';
        
        document.getElementById('modalTitle').innerText = 'جاري إنشاء الفهرس النصي...';
        document.getElementById('currentFile').innerText = 'قد تستغرق العملية بضع ثوانٍ للتحليل...';
        showProgressModal();
        document.getElementById('cancelProgressBtn').style.display = 'none';

        fetch('/api/joc_index', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                path: dest, 
                mode: mode,
                start_x: startX,
                char_limit: limit,
                template: template,
                strip_symbols: strip
            })
        }).then(r=>r.json()).then(res=>{
            if(res.error) {
                showInlineError(res.error);
                return;
            }
            document.getElementById('modalTitle').innerText = 'تم إنشاء الفهرس النصي';
            checkDiskSpace(document.getElementById('custom_dest_path').value);
            currentJocIndexText = res.output;
            if(reviewTxt) {
                openEditTxtModal(null);
            } else {
                runPendingJocActions();
            }
        }).catch(e=>{
            showInlineError('حدث خطأ أثناء الإنشاء.');
        });
    }

    function updateEditTxtCount() {
        let el = document.getElementById('editTxtArea');
        let txt = el.value || '';
        let chars = txt.length;
        let lines = txt.split("\\n").length;
        let countEl = document.getElementById('editTxtCount');
        if(countEl) countEl.innerText = chars + ' حرف / ' + lines + ' سطر';
    }

    function openEditTxtModal(onSave) {
        let txtArea = document.getElementById('editTxtArea');
        txtArea.value = (currentJocIndexText === null || currentJocIndexText === undefined) ? '' : currentJocIndexText;
        window._editContinueActions = (onSave === null);
        window._editFromSubmit = (onSave === null);
        document.getElementById('editTxtModal').style.display = 'flex';
        if(document.getElementById('progressModal')) document.getElementById('progressModal').style.display = 'none';
        updateEditTxtCount();
        setTimeout(function(){ txtArea.focus(); }, 60);
    }

    function saveEditTxt() {
        let edited = document.getElementById('editTxtArea').value;
        let trimmed = edited.trim();
        if(!trimmed) { showInlineError('النص فارغ — اكتب بعض المحتوى قبل المتابعة.'); return; }
        currentJocIndexText = edited;
        document.getElementById('editTxtModal').style.display = 'none';
        updateEditTxtCount();
        let shouldContinue = window._editContinueActions;
        window._editContinueActions = false;
        if(shouldContinue) runPendingJocActions();
    }

    function cancelEditTxt() {
        let wasFromSubmit = window._editFromSubmit;
        document.getElementById('editTxtModal').style.display = 'none';
        window._editContinueActions = false;
        window._editFromSubmit = false;
        if(wasFromSubmit && pendingJocActions) showJocSuccessNotify(pendingJocActions.txtName);
    }

    function showJocSuccessNotify(txtName) {
        showInlineNotify('<span>✔</span> تم إنشاء الفهرس النصي <b>' + txtName + '</b> بنجاح.'
            + '<div style="display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;">'
            + '<button class="btn btn-primary btn-sm" style="flex:1;" onclick="copyJocIndex()">'
            + '<svg class="icon" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>'
            + 'نسخ الفهرس</button>'
            + '<button class="btn btn-outline btn-sm" style="flex:1;" onclick="openEditTxtModal(1)">'
            + '<svg class="icon" viewBox="0 0 24 24"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>'
            + 'تحرير النص</button>'
            + '</div>', 'success');
    }

    function runPendingJocActions() {
        if(!pendingJocActions) return;
        let a = pendingJocActions;
        let txt = currentJocIndexText || '';
        let txtName = a.txtName;

        if(a.downloadTxt && txt) {
            let blob = new Blob(["\uFEFF" + txt], {type: "text/plain;charset=utf-8"});
            let url = URL.createObjectURL(blob);
            let dl = document.createElement("a");
            dl.href = url;
            dl.download = txtName;
            document.body.appendChild(dl);
            dl.click();
            document.body.removeChild(dl);
        }
        showJocSuccessNotify(txtName);

        if(a.doMp3) {
            currentTtsFileName = a.mp3Name;
            document.getElementById('modalTitle').innerText = 'جاري تحويل الفهرس إلى MP3 (Edge TTS)...';
            document.getElementById('cancelProgressBtn').style.display = 'block';
            showProgressModal();
            fetch('/api/tts_index', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    index: txt,
                    voice: a.voice,
                    rate: a.rate,
                    filename: a.mp3Name,
                    chunk_size: 1800,
                    obfuscate: a.obfuscate
                })
            }).then(r=>r.json()).then(r2=>{
                if(r2.error) { showInlineError(r2.error); resetTransfer(); return; }
            }).catch(e=>{ resetTransfer(); showInlineError('حدث خطأ أثناء تشغيل TTS.'); });
        }
    }

    function downloadTtsFile() {
        if(!currentTtsFileName) return;
        let d = document.createElement('a');
        d.href = '/api/tts/download?name=' + encodeURIComponent(currentTtsFileName);
        d.download = currentTtsFileName;
        document.body.appendChild(d);
        d.click();
        document.body.removeChild(d);
        let txtName = currentTtsFileName.replace(/\\.mp3$/i, '') + '.txt';
        showInlineNotify('<span>✔</span> تم إنشاء وتحميل MP3 الصوتي <b>' + currentTtsFileName + '</b>، وكذا الفهرس النصي <b>' + txtName + '</b>.'
            + '<div style="display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;">'
            + '<button class="btn btn-primary btn-sm" style="flex:1;" onclick="copyJocIndex()">'
            + '<svg class="icon" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>'
            + 'نسخ الفهرس</button>'
            + '<button class="btn btn-outline btn-sm" style="flex:1;" onclick="openEditTxtModal(1)">'
            + '<svg class="icon" viewBox="0 0 24 24"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>'
            + 'تحرير النص</button>'
            + '</div>', 'success');
        currentTtsFileName = null;
    }

    let progressModalVisible = false;
    function showProgressModal() {
        document.getElementById('progressModal').style.display = 'flex';
        progressModalVisible = true;
        document.getElementById('cancelProgressBtn').style.display = 'block';
        document.getElementById('closeProgressBtn').style.display = 'none';
        document.getElementById('timeTakenDiv').style.display = 'none';
        document.getElementById('inlineNotify').style.display = 'none';
        document.getElementById('currentFile').style.color = 'var(--text-muted)';
    }

    function showInlineNotify(html, type) {
        let el = document.getElementById('inlineNotify');
        el.innerHTML = html;
        el.style.display = 'flex';
        if(type === 'success') {
            el.style.background = 'rgba(34,197,94,0.12)';
            el.style.borderColor = 'var(--success)';
            el.style.color = 'var(--success)';
        } else {
            el.style.background = 'var(--danger-soft)';
            el.style.borderColor = 'var(--danger)';
            el.style.color = 'var(--danger)';
        }
        if(!progressModalVisible) showProgressModal();
        document.getElementById('closeProgressBtn').style.display = 'block';
    }
    function showInlineDone(msg) { showInlineNotify('<span>✔</span> ' + msg, 'success'); }
    function showInlineError(msg) { showInlineNotify('<span>⚠</span> ' + msg, 'error'); }

    function cancelTransfer() {
        if(confirm('متأكد من الإيقاف؟')) {
            document.getElementById('cancelProgressBtn').disabled = true;
            fetch('/transfer/cancel').then(()=>{
                document.getElementById('cancelProgressBtn').style.display = 'none';
                document.getElementById('currentFile').innerText = 'جاري الإيقاف بشكل آمن...';
            }).catch(()=>{ document.getElementById('cancelProgressBtn').disabled = false; });
        }
    }

    function checkTransferStatus() {
        if (statusTimeout) clearTimeout(statusTimeout);

        fetch('/transfer/status?_t=' + new Date().getTime(), { cache: 'no-store' })
        .then(r=>r.json())
        .then(data => {
            if(data.status !== 'idle') {
                if(!progressModalVisible) showProgressModal();
                document.getElementById('progressText').innerText = data.progress + '%';
                document.getElementById('progressFill').style.width = data.progress + '%';
                if(data.total > 0) document.getElementById('countText').innerText = data.copied + ' / ' + data.total;
                document.getElementById('currentFile').innerText = data.file;
                document.getElementById('failedText').innerText = data.failed ? ('فشل: ' + data.failed) : '';

                if(data.status === 'done' || data.status === 'cancelled' || data.status === 'error') {
                    
                    // Clear selections ONLY if a file copy was successful.
                    if (data.status === 'done' && data.action === 'copy') {
                        selectedPaths.clear();
                        excludedPaths.clear();
                        refreshCheckboxStates();
                    }

                    // Trigger MP3 download once TTS finishes successfully.
                    if (data.status === 'done' && data.action === 'tts') {
                        downloadTtsFile();
                    }

                    document.getElementById('cancelProgressBtn').style.display = 'none';
                    document.getElementById('closeProgressBtn').style.display = 'block';
                    
                    if(data.status === 'error') {
                        document.getElementById('currentFile').innerText = 'خطأ: ' + data.log;
                        document.getElementById('currentFile').style.color = 'var(--danger)';
                        showInlineError('فشلت العملية: ' + data.log);
                    } else if (data.status === 'done' && data.time_taken) {
                        document.getElementById('timeTakenDiv').style.display = 'block';
                        document.getElementById('timeTakenDiv').innerText = 'استغرق: ' + data.time_taken;
                    } else if (data.status === 'cancelled' && data.time_taken) {
                        document.getElementById('timeTakenDiv').style.display = 'block';
                        document.getElementById('timeTakenDiv').innerText = 'تم الإيقاف بعد: ' + data.time_taken;
                    }
                    
                    if(data.errors && data.errors.length > 0) {
                        let box = document.getElementById('errorsBox');
                        box.style.display = 'block';
                        box.innerHTML = 'ملفات فشلت:<br>' + data.errors.map(n => '- ' + n).join('<br>');
                    }
                }
            }
        })
        .catch(err => console.log('Network sync delayed...'))
        .finally(() => {
            statusTimeout = setTimeout(checkTransferStatus, 1000);
        });
    }
    
    function resetTransfer() {
        fetch('/transfer/reset').then(() => {
            checkDiskSpace(document.getElementById('custom_dest_path').value);
            document.getElementById('progressModal').style.display = 'none';
            progressModalVisible = false;
            document.getElementById('errorsBox').style.display = 'none';
            document.getElementById('inlineNotify').style.display = 'none';
            document.getElementById('cancelProgressBtn').disabled = false;
            document.getElementById('modalTitle').innerText = 'العملية قيد التنفيذ...';
        }).catch(()=>{});
    }
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, storages=get_available_storages(),
                                  app_version=get_app_version())

@app.route('/api/system')
def system_info():
    """Version + optional Shizuku state + SD write access, for the header pills."""
    if request.args.get('refresh') == '1':
        shizuku = shizuku_refresh()
    else:
        shizuku = shizuku_status()
    return jsonify({
        'version': get_app_version(),
        'edge_tts': EDGE_TTS_AVAILABLE,
        'ffmpeg': bool(shutil.which('ffmpeg')),
        'shizuku': shizuku,
        'sd': removable_sd(),
    })

@app.route('/api/items')
def get_items():
    req_path = request.args.get('path', '/sdcard')
    items = []
    if os.path.exists(req_path) and os.path.isdir(req_path):
        try:
            for item in sorted(os.listdir(req_path), key=str.lower):
                full_p = os.path.join(req_path, item)
                items.append({'name': item, 'path': full_p, 'is_dir': os.path.isdir(full_p)})
        except: pass
    return jsonify({'items': items})

@app.route('/api/mkdir', methods=['POST'])
def make_dir():
    data = request.json or {}
    if not data.get('path') or not data.get('name'): return jsonify({'success': False, 'error': 'بيانات ناقصة'})
    target_dir = os.path.join(data['path'], data['name'])
    try:
        os.makedirs(target_dir, exist_ok=True)
        invalidate_cache_for_path(data['path'])
        return jsonify({'success': True, 'new_path': target_dir})
    except Exception as e: return jsonify({'success': False, 'error': str(e)})

@app.route('/api/rename', methods=['POST'])
def rename_item():
    data = request.json or {}
    old_path = data.get('path', '')
    new_name = (data.get('new_name') or '').strip()
    if not old_path or not os.path.exists(old_path): return jsonify({'success': False, 'error': 'غير موجود'})
    if not new_name: return jsonify({'success': False, 'error': 'فارغ'})
    if '/' in new_name or '\\' in new_name: return jsonify({'success': False, 'error': 'رمز غير صالح'})
    if is_busy(): return jsonify({'success': False, 'error': 'مشغول بعملية نسخ'})
    new_path = os.path.join(os.path.dirname(old_path.rstrip('/')), new_name)
    if os.path.exists(new_path): return jsonify({'success': False, 'error': 'موجود مسبقاً'})
    try:
        os.rename(old_path, new_path)
        invalidate_cache_for_path(old_path)
        invalidate_cache_for_path(new_path)
        return jsonify({'success': True, 'old_path': old_path, 'new_path': new_path})
    except Exception as e: return jsonify({'success': False, 'error': str(e)})

SEARCH_SKIP_NAMES = {'.thumbnails', '.trashed', 'lost.dir', '.trash'}
SEARCH_MAX_RESULTS = 40
SEARCH_MAX_DIRS_SCANNED = 15000  

def _search_should_skip(entry_name, parent_name):
    if entry_name.lower() in SEARCH_SKIP_NAMES: return True
    if parent_name == 'Android' and entry_name in ('data', 'obb'): return True
    return False

def _rank_search_results(results, q):
    """Sort so exact/startswith matches surface first, then natural-sort the rest."""
    q_lower = q.lower()
    def rank_key(r):
        name_lower = r['name'].lower()
        if name_lower == q_lower: tier = 0
        elif name_lower.startswith(q_lower): tier = 1
        else: tier = 2
        return (tier, natural_sort_key(r['name']))
    results.sort(key=rank_key)
    return results

# ------------- Folder Cache (instant search index) -------------
# Manually built per source folder via the "Cache Folder" button. Persists in memory
# for the lifetime of the server process (no automatic expiry), but is invalidated
# whenever the app itself changes something inside a cached path (rename/delete/mkdir/copy)
# so results never point at items that no longer exist.
FOLDER_CACHE = {}          # root_path -> {'items': [...], 'count': int, 'built_at': float}
folder_cache_lock = threading.RLock()
folder_cache_building = set()  # root paths currently being indexed

def build_cache_index(root, cancel_check=None):
    items = []
    stack = deque([root])
    dirs_scanned = 0
    while stack:
        if cancel_check is not None and dirs_scanned % 300 == 0 and cancel_check():
            break
        current_dir = stack.popleft()
        dirs_scanned += 1
        parent_name = os.path.basename(current_dir)
        try:
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if _search_should_skip(entry.name, parent_name):
                        continue
                    items.append({'name': entry.name, 'name_lower': entry.name.lower(), 'path': entry.path, 'is_dir': is_dir})
                    if is_dir:
                        stack.append(entry.path)
        except (PermissionError, OSError):
            continue
    return items

def _cache_build_thread(root):
    try:
        items = build_cache_index(root)
        with folder_cache_lock:
            FOLDER_CACHE[root] = {'items': items, 'count': len(items), 'built_at': time.time()}
    except Exception:
        pass
    finally:
        with folder_cache_lock:
            folder_cache_building.discard(root)

def invalidate_cache_for_path(path):
    """Drop any cached index that could now contain stale entries because of a
    change at `path` (the path itself was modified, sits inside a cached root,
    or was itself a cached root that just got renamed/deleted)."""
    if not path: return
    with folder_cache_lock:
        stale = [r for r in FOLDER_CACHE if path == r or path.startswith(r + '/') or r.startswith(path + '/')]
        for r in stale:
            del FOLDER_CACHE[r]

@app.route('/api/cache/build', methods=['POST'])
def cache_build():
    data = request.json or {}
    root = data.get('path', '')
    if not root or not os.path.isdir(root):
        return jsonify({'error': 'مسار غير صالح'})
    with folder_cache_lock:
        if root in folder_cache_building:
            return jsonify({'error': 'جاري الفهرسة بالفعل لهذا المجلد'})
        folder_cache_building.add(root)
    threading.Thread(target=_cache_build_thread, args=(root,), daemon=True).start()
    return jsonify({'success': True})

@app.route('/api/cache/status')
def cache_status():
    root = request.args.get('path', '')
    with folder_cache_lock:
        building = root in folder_cache_building
        info = FOLDER_CACHE.get(root)
        if info:
            return jsonify({'cached': True, 'building': building, 'count': info['count'], 'built_at': info['built_at']})
    return jsonify({'cached': False, 'building': building})

@app.route('/api/cache/clear', methods=['POST'])
def cache_clear():
    data = request.json or {}
    root = data.get('path', '')
    with folder_cache_lock:
        FOLDER_CACHE.pop(root, None)
    return jsonify({'success': True})

@app.route('/api/search')
def deep_search():
    q = request.args.get('q', '').lower().strip()
    folders_only = request.args.get('folders_only', 'true') == 'true'
    root = request.args.get('root', '/sdcard') or '/sdcard'
    if not q: return jsonify({'results': []})
    if not os.path.isdir(root): root = '/sdcard'

    with folder_cache_lock:
        cached = FOLDER_CACHE.get(root)

    if cached:
        # Instant path: filter the pre-built in-memory index, no disk access at all.
        results = []
        for it in cached['items']:
            if folders_only and not it['is_dir']:
                continue
            if q in it['name_lower']:
                results.append({'name': it['name'], 'path': it['path'], 'is_dir': it['is_dir']})
        _rank_search_results(results, q)
        return jsonify({'results': results[:SEARCH_MAX_RESULTS], 'from_cache': True, 'cache_count': cached['count']})

    results = []
    stack = deque([root])  
    dirs_scanned = 0
    try:
        while stack and len(results) < SEARCH_MAX_RESULTS and dirs_scanned < SEARCH_MAX_DIRS_SCANNED:
            current_dir = stack.popleft()  
            dirs_scanned += 1
            parent_name = os.path.basename(current_dir)
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        if _search_should_skip(entry.name, parent_name):
                            continue
                        if is_dir:
                            stack.append(entry.path)
                        if q in entry.name.lower():
                            if not folders_only or is_dir:
                                results.append({'name': entry.name, 'path': entry.path, 'is_dir': is_dir})
                                if len(results) >= SEARCH_MAX_RESULTS:
                                    break
            except (PermissionError, OSError):
                continue
    except Exception:
        pass

    _rank_search_results(results, q)
    return jsonify({'results': results, 'from_cache': False})

def get_dir_size(path, excludes):
    total = 0
    try:
        if os.path.isfile(path): return os.path.getsize(path)
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if os.path.join(dirpath, d) not in excludes]
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if fp not in excludes and not os.path.islink(fp):
                    total += os.path.getsize(fp)
    except: pass
    return total

@app.route('/api/size', methods=['POST'])
def get_item_size():
    body = request.json or {}
    path = body.get('path', '')
    excludes = set(p for p in body.get('excludes', []) if p)
    bytes_size = get_dir_size(path, excludes) if path and os.path.exists(path) else 0
    return jsonify({'bytes': bytes_size, 'formatted': format_size(bytes_size)})

@app.route('/api/disk_usage')
def disk_usage_route():
    try:
        total, used, free = shutil.disk_usage(request.args.get('path', '/sdcard'))
        return jsonify({'total': format_size(total), 'free': format_size(free), 'free_bytes': free, 'percent': int((used / total) * 100) if total > 0 else 0})
    except Exception as e: return jsonify({'error': str(e)})

@app.route('/api/benchmark', methods=['POST'])
def benchmark():
    data = request.json or {}
    dest = data.get('dest', '')
    if not dest or not os.path.isdir(dest):
        return jsonify({'error': 'مسار الوجهة غير صالح'})
    
    test_file = os.path.join(dest, '.speedtest_tmp')
    try:
        chunk = bytearray(1024 * 1024) 
        start_time = time.time()
        
        with open(test_file, 'wb') as f:
            for _ in range(50): 
                f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        
        duration = time.time() - start_time
        speed_mbps = 50 / duration if duration > 0 else 0
        est_seconds_10gb = (10 * 1024) / speed_mbps if speed_mbps > 0 else 0
        
        os.remove(test_file)
        
        return jsonify({
            'success': True,
            'speed_mbps': round(speed_mbps, 2),
            'est_10gb': round(est_seconds_10gb)
        })
    except Exception as e:
        if os.path.exists(test_file):
            try: os.remove(test_file)
            except: pass
        return jsonify({'error': str(e)})

@app.route('/api/delete', methods=['POST'])
def delete_item():
    data = request.json or {}
    path = data.get('path', '')
    if not path or not os.path.lexists(path): return jsonify({'success': False, 'error': 'العنصر غير موجود'})
    if is_busy(): return jsonify({'success': False, 'error': 'النظام مشغول بعملية أخرى، حاول لاحقاً'})

    try:
        if os.path.isdir(path) and not os.path.islink(path):
            deleted, failures = force_delete_tree(path)
            invalidate_cache_for_path(path)
            if os.path.lexists(path):
                detail = ''
                if failures:
                    shown = failures[:4]
                    detail = ' — ' + '، '.join(
                        '{}: {}'.format(os.path.basename(str(p).rstrip('/')) or p, reason)
                        for p, reason in shown
                    )
                    if len(failures) > len(shown):
                        detail += ' (+{})'.format(len(failures) - len(shown))
                hint = ''
                if any('صلاحية' in str(reason) for _, reason in failures):
                    hint = ' — جرّب: sdmp3 unlock لمنح الوصول الكامل للكارت'
                return jsonify({
                    'success': False,
                    'error': 'تعذر حذف {} عنصر من أصل {}{}{}'.format(
                        len(failures), deleted + len(failures), detail, hint),
                    'failures': [{'path': p, 'reason': r} for p, r in failures[:10]],
                })
            return jsonify({'success': True, 'deleted': deleted})
        else:
            ok, reason = try_remove_entry(path)
            if ok:
                invalidate_cache_for_path(path)
                return jsonify({'success': True})
            return jsonify({'success': False, 'error': 'تعذر الحذف: {}'.format(reason)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/joc_index', methods=['POST'])
def generate_joc_index():
    data = request.json or {}
    target_dir = data.get('path', '')
    mode = int(data.get('mode', 2))
    start_x = int(data.get('start_x', 1))
    char_limit = int(data.get('char_limit', 93))
    template = int(data.get('template', 4))
    strip_symbols = data.get('strip_symbols', True)
    
    if not os.path.isdir(target_dir):
        return jsonify({'error': 'مسار غير صالح'})

    output_lines = []
    current_x = start_x

    def format_entry(name, num):
        if template == 1: return f"{num} : {name} ."
        elif template == 2: return f"{name} من {num}"
        elif template == 3: return f"{name} من رقم {num} ."
        else: return f"{name} : {num} ."

    has_mp3 = set()
    for root, _, files in os.walk(target_dir):
        if any(f.lower().endswith('.mp3') for f in files):
            has_mp3.add(root)
            parent = os.path.dirname(root)
            while parent and parent != '/' and parent not in has_mp3:
                has_mp3.add(parent)
                parent = os.path.dirname(parent)

    def process_dir(current_path, indent_level):
        nonlocal current_x
        if current_path not in has_mp3: return

        try:
            with os.scandir(current_path) as it:
                # ترتيب حقيقي كما هو مخزّن على الفلاشة (FAT) بدل الترتيب الأبجدي:
                # os.scandir بدون sorted() يعيد العناصر بنفس ترتيب readdir()
                # النظامي، وهو نفس ترتيب "ls -1U" المستخدم في سكربت الفهرسة.
                children = [e for e in it if not e.name.startswith('.')]
        except: return

        mp3_files = [f for f in children if f.is_file() and f.name.lower().endswith('.mp3')]
        dirs = [d for d in children if d.is_dir()]

        dir_name = clean_and_cut(os.path.basename(current_path), char_limit, strip_symbols)

        if mode == 1: 
            if current_path != target_dir:
                if mp3_files:
                    output_lines.append(f"{'  ' * indent_level}{format_entry(dir_name, current_x)}")
                else:
                    output_lines.append(f"{'  ' * indent_level}{dir_name}")
            current_x += len(mp3_files)
            for d in dirs: process_dir(d.path, indent_level + 1)

        elif mode == 2: 
            if current_path != target_dir:
                output_lines.append(f"\n{'  ' * indent_level}[{dir_name}]")
            for f in mp3_files:
                f_name = clean_and_cut(os.path.splitext(f.name)[0], char_limit, strip_symbols)
                output_lines.append(f"{'  ' * (indent_level + 1) if current_path != target_dir else ''}{format_entry(f_name, current_x)}")
                current_x += 1
            for d in dirs: process_dir(d.path, indent_level + 1)

    if mode in [1, 2]:
        process_dir(target_dir, 0)
    elif mode == 3: 
        try:
            with os.scandir(target_dir) as it:
                # نفس منطق FAT الحقيقي: بدون فرز أبجدي
                for f in it:
                    if f.name.startswith('.'): continue
                    if f.is_file() and f.name.lower().endswith('.mp3'):
                        f_name = clean_and_cut(os.path.splitext(f.name)[0], char_limit, strip_symbols)
                        output_lines.append(format_entry(f_name, current_x))
                        current_x += 1
        except: pass

    return jsonify({'success': True, 'output': '\n'.join(output_lines)})

# ------------- TTS (Edge) Routes -------------
@app.route('/api/tts/voices')
def tts_voices():
    return jsonify({'voices': TTS_VOICES, 'default': TTS_DEFAULT_VOICE, 'available': EDGE_TTS_AVAILABLE})

def tts_thread_task(index_text, voice, rate, out_name, chunk_size, obfuscate=False):
    acquire_wakelock()
    reset_state(status='copying', action='tts', start_time=time.time())
    custom_path = os.path.join(TTS_CACHE_DIR, "custom_audio.mp3")
    try:
        if not EDGE_TTS_AVAILABLE:
            raise RuntimeError("حزمة edge-tts غير مثبتة. قم بتشغيل: pip install edge-tts")
        if not index_text.strip():
            raise RuntimeError("الفهرس فارغ - لا يوجد نص للتحويل")

        setup_tts_cache()
        chunks = chunk_index_text(index_text, chunk_size)
        total = len(chunks)
        voice_label = TTS_VOICES.get(voice, voice)
        try: rate_show = rate if rate else '+0%'
        except Exception: rate_show = '+0%'
        set_state(total=total, file=f'الصوت: {voice_label} — تم تقسيم النص إلى {total} جزء')
        set_state(progress=2)

        chunk_files = []
        final_path = os.path.join(TTS_CACHE_DIR, out_name)
        for i, chunk in enumerate(chunks, 1):
            if is_cancelled(): raise TransferCancelled()
            cpath = os.path.join(TTS_CACHE_DIR, f"chunk_{i:03d}.mp3")
            set_state(copied=i, file=f'الصوت: {voice_label} | توليد الجزء {i} من {total}...')
            progress = 5 + int(((i - 1) / total) * 82)
            set_state(progress=min(progress, 88))
            try:
                synthesize_chunk_mp3(chunk, voice, rate, cpath)
                chunk_files.append(cpath)
            except Exception as e:
                raise RuntimeError(f"فشل توليد الجزء {i}: {e}")
            if is_cancelled(): raise TransferCancelled()

        if obfuscate == 'file':
            set_state(file='جاري إضافة الملف الصوتي المخصص في البداية...')
            set_state(progress=92)
            combine_mp3_chunks(chunk_files, final_path, bitrate="64k", obfuscate=True,
                               custom_audio_path=custom_path if os.path.isfile(custom_path) else None)
        elif obfuscate:
            set_state(file='جاري إضافة ضوضاء تشويش خفيفة في البداية...')
            set_state(progress=92)
            combine_mp3_chunks(chunk_files, final_path, bitrate="64k", obfuscate=True, static_seconds=120)
        else:
            set_state(file='جاري دمج الأجزاء في ملف MP3 واحد (64kbps)...')
            set_state(progress=92)
            combine_mp3_chunks(chunk_files, final_path, bitrate="64k")
        set_state(progress=100, copied=total)
        finish_transfer('done', 'تم إنشاء MP3 الصوتي بنجاح.', extra={'progress': 100, 'tts_name': out_name})
    except TransferCancelled:
        finish_transfer('cancelled', 'تم إلغاء تحويل الفهرس إلى MP3.')
    except Exception as e:
        set_state(status='error', log=str(e))
    finally:
        release_wakelock()

@app.route('/api/tts_index', methods=['POST'])
def tts_index():
    data = request.json or {}
    index_text = data.get('index') or ''
    voice = data.get('voice') or TTS_DEFAULT_VOICE
    rate = data.get('rate') or '+0%'
    out_name = data.get('filename') or 'JOC_Index.mp3'
    if not out_name.lower().endswith('.mp3'):
        out_name += '.mp3'
    out_name = os.path.basename(out_name)
    chunk_size = int(data.get('chunk_size', 1800))
    obf = data.get('obfuscate', False)
    if obf is True or obf is False:
        obfuscate = bool(obf)
    elif isinstance(obf, str) and obf.lower() in ('file', 'custom'):
        obfuscate = 'file'
    else:
        obfuscate = bool(obf)

    with transfer_lock:
        if transfer_state['status'] == 'copying':
            return jsonify({'error': 'هناك عملية قيد التشغيل بالفعل'})
        transfer_state['status'] = 'copying'

    threading.Thread(target=tts_thread_task, args=(index_text, voice, rate, out_name, chunk_size, obfuscate), daemon=True).start()
    return jsonify({'success': True})

@app.route('/api/tts/custom', methods=['POST'])
def tts_custom_upload():
    """Upload a custom masking MP3 that will be prepended before the audio index."""
    if 'file' not in request.files:
        return jsonify({'error': 'لم يتم إرسال ملف.'}), 400
    f = request.files['file']
    if not f or f.filename == '':
        return jsonify({'error': 'لم يتم اختيار ملف.'}), 400
    if not f.filename.lower().endswith('.mp3'):
        return jsonify({'error': 'يرجى اختيار ملف بصيغة MP3 فقط.'}), 400
    setup_tts_cache()
    dest = os.path.join(TTS_CACHE_DIR, "custom_audio.mp3")
    f.save(dest)
    return jsonify({'success': True, 'name': os.path.basename(f.filename)})

@app.route('/api/tts/download')
def tts_download():
    name = os.path.basename(request.args.get('name', 'JOC_Index.mp3'))
    path = os.path.join(TTS_CACHE_DIR, name)
    if not os.path.isfile(path):
        return jsonify({'error': 'الملف غير موجود أو تم حذف الكاش.'}), 404
    resp = send_file(path, as_attachment=True, download_name=name, mimetype='audio/mpeg')
    resp.headers['Cache-Control'] = 'no-store'
    return resp

def parse_spoof_date(date_str):
    if not date_str: return None
    try: return int(datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M").timestamp())
    except: return None

@app.route('/transfer/start', methods=['POST'])
def start_transfer():
    data = request.json or {}
    paths = [p for p in data.get('paths', []) if p]
    dest = data.get('dest', '')
    if not paths: return jsonify({'error': 'لم يتم تحديد أي ملفات'})
    if not dest or not os.path.isdir(dest): return jsonify({'error': 'مسار الوجهة غير صالح'})
    if not os.access(dest, os.W_OK): return jsonify({'error': 'لا توجد صلاحية كتابة'})

    with transfer_lock:
        if transfer_state['status'] == 'copying': return jsonify({'error': 'هناك عملية قيد التشغيل بالفعل'})
        transfer_state['status'] = 'copying'

    threading.Thread(target=copy_thread_task, args=(paths, dest, set(p for p in data.get('excludes', []) if p)), daemon=True).start()
    return jsonify({'success': True})

@app.route('/transfer/spoof_only', methods=['POST'])
def start_spoof_only():
    data = request.json or {}
    dest = data.get('dest', '')
    spoof_timestamp = parse_spoof_date(data.get('spoof_date'))
    if not dest or not os.path.isdir(dest): return jsonify({'error': 'مسار الوجهة غير صالح'})
    if not spoof_timestamp: return jsonify({'error': 'تاريخ التمويه غير صالح'})

    with transfer_lock:
        if transfer_state['status'] == 'copying': return jsonify({'error': 'النظام مشغول'})
        transfer_state['status'] = 'copying'

    threading.Thread(target=spoof_only_thread, args=(dest, spoof_timestamp), daemon=True).start()
    return jsonify({'success': True})

@app.route('/transfer/cancel')
def cancel_transfer():
    set_state(cancel=True)
    return jsonify({'success': True})

@app.route('/transfer/status')
def get_status():
    resp = jsonify(get_state_snapshot())
    resp.headers['Cache-Control'] = 'no-store'
    return resp

@app.route('/transfer/reset')
def reset_status():
    with transfer_lock:
        if transfer_state['status'] != 'copying': reset_state()
    return jsonify({'success': True})

@app.errorhandler(Exception)
def handle_any_exception(e):
    if isinstance(e, HTTPException): return e
    return jsonify({'error': f'خطأ داخلي: {e}'}), 500

def _signal_exit(signum, frame):
    """On Ctrl+C / SIGTERM: clean the TTS cache, then exit cleanly."""
    cleanup_tts_cache()
    import signal as _s
    raise SystemExit(0)

def _lan_ip():
    """Best-effort LAN address for the startup banner (None if offline)."""
    import socket as _socket
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        sock.connect(('8.8.8.8', 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def main(argv=None):
    """Entry point: python project.py [--host H] [--port P] [--no-wakelock]."""
    global WAKELOCK_ENABLED

    import argparse
    parser = argparse.ArgumentParser(
        prog='sdmp3',
        description='SD Copy MP3 Manager - copy, index and timestamp-spoof MP3s on Android SD cards.',
    )
    parser.add_argument('--host', default='0.0.0.0',
                        help='bind address (default: 0.0.0.0, reachable from your network)')
    parser.add_argument('--port', type=int, default=5050,
                        help='HTTP port (default: 5050)')
    parser.add_argument('--no-wakelock', action='store_true',
                        help='do not keep the CPU awake during transfers')
    parser.add_argument('--version', action='version',
                        version=f'sdmp3 {get_app_version()}')
    args = parser.parse_args(argv)

    WAKELOCK_ENABLED = not args.no_wakelock

    import signal as _sig
    for _sname in ('SIGINT', 'SIGTERM'):
        if hasattr(_sig, _sname):
            try:
                _sig.signal(getattr(_sig, _sname), _signal_exit)
            except Exception:
                pass

    print(f"sdmp3 {get_app_version()} - serving on {args.host}:{args.port}", flush=True)
    # Keep the optional Shizuku status warm; harmless no-op when it is not installed.
    try:
        threading.Thread(target=shizuku_refresher, daemon=True).start()
    except Exception:
        pass
    print(f"  local:   http://localhost:{args.port}", flush=True)
    ip = _lan_ip()
    if ip and args.host in ('0.0.0.0', '', '::'):
        print(f"  network: http://{ip}:{args.port}", flush=True)
    print("  press Ctrl+C to stop", flush=True)

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()

