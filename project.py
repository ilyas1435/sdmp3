#!/usr/bin/env python3
import os
import shutil
import subprocess
import threading
import time
import datetime
import json
import re
import shlex
import atexit
import tempfile
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
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
APP_VERSION_FALLBACK = "1.5.3"

# Toggled by --no-wakelock (see main() at the bottom).
WAKELOCK_ENABLED = True


def get_app_version():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'VERSION'), encoding='utf-8') as fh:
            return fh.read().strip() or APP_VERSION_FALLBACK
    except OSError:
        return APP_VERSION_FALLBACK


# Read/write block size. 4MB keeps copies sequential (order is the whole point of
# this app) while cutting the number of FUSE round-trips to a minimum.
COPY_BUFFER_SIZE = 4 * 1024 * 1024
# Flush the whole transfer to the card every 64MB.
#
# This is a *coarse* flush on purpose: it used to be an fsync after every single
# file, which cost tens of milliseconds per MP3 and made folders full of small
# tracks painfully slow. Ordering is guaranteed by writing the files one at a
# time in DFS order - never by fsync - so dropping the per-file flush changes
# speed only, not the on-disk playback order.
# os.sync() flushes *every* filesystem on the device, not just this copy, so a
# small threshold turns a big transfer into hundreds of short stalls. Ordering
# comes from the sequential writes themselves, never from syncing.
SYNC_THRESHOLD = 256 * 1024 * 1024

# A transfer that has moved no bytes for this long is reported as stalled.
# A pulled or dying SD card blocks the worker *inside* write() forever, so the
# worker cannot report anything itself - the clock is watched from the request
# side instead. 25s is well above the worst case for one 4MB chunk on a slow
# (Class 4) card.
STALL_SECONDS = 25
# How long to refuse a destination after a probe timed out, instead of spawning
# another worker that would hang in exactly the same place.
UNHEALTHY_DEST_TTL = 120
# Seconds to wait for the destination to answer a tiny write before a transfer.
DEST_PROBE_TIMEOUT = 8

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
    'skipped': [],
    'hint': '',
    'copied_bytes': 0,
    'total_bytes': 0,
    'speed_mbps': 0,
    'eta_seconds': 0,
    'stalled': False,
    'stall_seconds': 0,
    'last_progress_ts': 0,
    'cancel': False,
    'start_time': 0,
    'time_taken': '',
    'delete_failures': [],
    'card_damage': False
}
transfer_state = dict(DEFAULT_STATE)

# Worker currently running a copy or a spoof pass. Tracking it lets us detect a
# dead worker: otherwise a crashed transfer would leave status='copying' forever
# and every new attempt would fail with "already running".
ACTIVE_THREAD = None


def is_thread_alive(thread):
    try:
        return bool(thread is not None and thread.is_alive())
    except Exception:
        return False

def reset_state(**overrides):
    with transfer_lock:
        transfer_state.clear()
        transfer_state.update(DEFAULT_STATE)
        # Start the stall clock at "now". Without this a fresh run keeps
        # last_progress_ts=0 and falls back to start_time, so the first 25s of
        # any slow phase looked like a hang.
        transfer_state['last_progress_ts'] = time.time()
        transfer_state.update(overrides)

def set_state(**kwargs):
    with transfer_lock:
        transfer_state.update(kwargs)
        # Prevent error array bloat to protect browser memory
        if 'errors' in transfer_state and len(transfer_state['errors']) > 50:
            transfer_state['errors'] = transfer_state['errors'][:50]

def get_state_snapshot():
    with transfer_lock:
        snap = dict(transfer_state)
    # Stall detection is computed on read: while a card hangs, the worker is
    # blocked in a syscall and cannot update its own state. Reporting the stall
    # is what turns a silent freeze into a message the user can act on.
    #
    # Only copy/spoof/delete can hang on a volume. TTS is network-bound (Edge
    # TTS) and never writes to the card, so applying the card wording to it
    # produced the false "pull the SD card out" warning while the audio was
    # being generated.
    if snap.get('status') == 'copying' and snap.get('action') in ('copy', 'spoof', 'delete'):
        last = snap.get('last_progress_ts') or snap.get('start_time') or 0
        idle = (time.time() - last) if last else 0
        if idle >= STALL_SECONDS:
            snap['stalled'] = True
            snap['stall_seconds'] = int(idle)
    else:
        snap['stalled'] = False
        snap['stall_seconds'] = 0
    return snap


def touch_progress():
    """Heartbeat for the stall detector (never raises, never blocks long)."""
    try:
        with transfer_lock:
            transfer_state['last_progress_ts'] = time.time()
    except Exception:
        pass

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
# A recursive rm through rish can take a while on a big tree, but it must never
# hang the delete worker forever.
SHIZUKU_DELETE_TIMEOUT = 300
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


def shizuku_delete(path):
    """Remove `path` through Shizuku (shell uid 2000).

    Last resort only: the shell user can delete Android/data, Android/obb and
    read-only entries that the app itself is denied. Returns True only when the
    path is really gone afterwards, never on a hopeful exit code.
    """
    if not path:
        return False
    try:
        if not shizuku_status().get('running'):
            return False
    except Exception:
        return False
    try:
        quoted = shlex.quote(str(path))
        command = ('rm -rf -- {} ; if [ -e {} ]; then echo SDMP3_STILL_THERE; '
                   'else echo SDMP3_GONE; fi'.format(quoted, quoted))
    except Exception:
        return False

    # rish can block for minutes on a big tree. Run it aside and keep the
    # progress heartbeat alive, otherwise the UI announces a dead card during a
    # delete that is actually working.
    box = {}

    def _run():
        box['out'] = rish(command, timeout=SHIZUKU_DELETE_TIMEOUT)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    started = time.time()
    while worker.is_alive():
        touch_progress()
        if time.time() - started > SHIZUKU_DELETE_TIMEOUT + 10:
            log_event('delete via Shizuku timed out: {!r}'.format(path))
            return False
        worker.join(1.0)

    text = box.get('out') or ''
    return 'SDMP3_GONE' in text and 'SDMP3_STILL_THERE' not in text


def shizuku_refresher():
    """Background loop so no request ever pays the rish cost."""
    while True:
        try:
            shizuku_refresh()
        except Exception:
            pass
        time.sleep(SHIZUKU_TTL)


# Cached answer for the header pill: probing a card on every page load would be
# wasteful (and would hammer a dying card).
_SD_STATUS_TTL = 60
_sd_status_cache = {'path': None, 'ts': 0.0, 'value': None}


def removable_sd():
    """First external (non-emulated) /storage mount + whether we can really write.

    `os.access(W_OK)` lies on Android: it says yes for a card whose every write
    fails with EIO. So the pill is driven by the same 5-byte probe the copy path
    uses, cached for a minute.
    """
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

    now = time.time()
    cached = _sd_status_cache
    if cached['path'] == path and (now - cached['ts']) < _SD_STATUS_TTL:
        return dict(cached['value'])

    problem = check_destination(path, timeout=5)
    value = {'path': path, 'writable': problem is None,
             'kind': storage_kind(path) or 'card'}
    if problem:
        value['error'] = problem
    _sd_status_cache.update({'path': path, 'ts': now, 'value': value})
    return dict(value)


def natural_sort_key(text):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

def storage_kind(mount_point):
    """'sdcard', 'usb' or None for an external /storage/XXXX-XXXX volume.

    Android mounts SD cards *and* USB sticks as /storage/UUID, so the name says
    nothing about the device. /proc/mounts pairs the volume with its block device
    (/dev/block/vold/public:MAJOR:MINOR) and the major tells them apart: 179 is
    the MMC driver (SD card), 8 is the SCSI/USB one (USB OTG).
    """
    base = os.path.basename(str(mount_point).rstrip('/'))
    if not base:
        return None
    device = None
    try:
        with open('/proc/mounts', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == '/mnt/media_rw/' + base:
                    device = parts[0]
                    break
    except OSError:
        return None
    if not device:
        return None
    return classify_volume_device(device)


def classify_volume_device(device):
    """Map a vold block device to 'sdcard' / 'usb' / None."""
    match = re.search(r'public:(\d+):(\d+)', device)
    if match:
        major = int(match.group(1))
        if major == 179:
            return 'sdcard'
        if major == 8:
            return 'usb'
        return None
    if 'mmcblk' in device:
        return 'sdcard'
    if re.search(r'/sd[a-z]\d*$', device):
        return 'usb'
    return None


# Icon (SVG symbol id) + Arabic label per volume kind. Anything unrecognised
# still gets a usable card icon and label, never a blank entry.
STORAGE_KINDS = {
    'internal': {'label': 'الذاكرة الداخلية', 'icon': 'ico-internal'},
    'sdcard': {'label': 'كارت SD', 'icon': 'ico-sd'},
    'usb': {'label': 'USB OTG', 'icon': 'ico-usb'},
    'card': {'label': 'كارت ذاكرة', 'icon': 'ico-card'},
}


def get_available_storages():
    storages = []
    seen_names = set()
    if os.path.exists('/sdcard'):
        storages.append({
            'name': STORAGE_KINDS['internal']['label'],
            'path': '/sdcard',
            'kind': 'internal',
            'icon': STORAGE_KINDS['internal']['icon'],
        })
    try:
        df_output = subprocess.check_output("df -h", shell=True, text=True, timeout=5)
        for line in df_output.splitlines():
            # Only use valid /storage/ mounts, avoid duplicates and permission-denied /mnt/media_rw
            if '/storage/' in line and 'emulated' not in line:
                mount_point = line.split()[-1]
                name = os.path.basename(mount_point)
                if name not in seen_names:
                    kind = storage_kind(mount_point) or 'card'
                    storages.append({
                        'name': '{} ({})'.format(STORAGE_KINDS[kind]['label'], name),
                        'path': mount_point,
                        'kind': kind,
                        'icon': STORAGE_KINDS[kind]['icon'],
                    })
                    seen_names.add(name)
    except Exception:
        pass
    return storages

def count_files(paths, cancel_check=None, count_dirs=False, on_progress=None):
    total = 0
    stack = list(paths)
    counter = 0
    while stack:
        counter += 1
        if counter % 300 == 0:
            # Counting a big folder is a pure read pass that can take minutes;
            # without a heartbeat the UI reported the card as dead.
            if on_progress is not None:
                on_progress()
            if cancel_check is not None and cancel_check():
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

def format_size(size_bytes):
    if size_bytes == 0: return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while size_bytes >= 1024 and i < len(size_name) - 1:
        size_bytes /= 1024.0
        i += 1
    if i == 0:
        return "{} B".format(int(round(size_bytes)))
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

CARD_DAMAGE_HINT = (
    'هذا المجلد تالف على مستوى الكارت (نظام ملفات FAT): أندرويد يرفض حذفه '
    'بأنه «غير فارغ» لكنه لا يستطيع قراءة محتوياته (خطأ إدخال/إخراج). '
    'ليست مشكلة صلاحيات — أصلح الكارت من الكمبيوتر: chkdsk /f في ويندوز أو '
    'fsck.vfat -a في لينكس، ثم احذف المجلد عادةً'
)

# Shown when the card refuses *writes* (errno 5 EIO / 30 read-only).
CARD_WRITE_HINT = (
    'الكارت يرفض الكتابة (خطأ إدخال/إخراج) وليس مشكلة صلاحيات. انسخ بياناتك '
    'منه ثم أصلح نظام الملفات من الكمبيوتر: chkdsk X: /f في ويندوز أو '
    'fsck.vfat -a في لينكس. إذا تكرر الخطأ بعد الإصلاح فالكارت متعب أو '
    'سعته غير حقيقية — استبدله'
)


def looks_like_card_damage(failures, deleted):
    """EIO + 'not empty' with nothing deleted = broken FAT directory entry."""
    if deleted or not failures:
        return False
    text = ' '.join(str(reason) for _, reason in failures)
    return 'إدخال/إخراج' in text or 'غير فارغ' in text


def log_event(message):
    """Write a support line to the server log (sdmp3.log). Never raises."""
    try:
        print('[sdmp3] ' + str(message), flush=True)
    except Exception:
        pass


def _allowed_storage_roots():
    """Every directory a destructive operation may touch.

    Android denies `os.listdir('/storage')` to Termux, so this list *cannot* be
    built by listing that directory. The old version did exactly that, which
    silently dropped every real volume root (a card, a USB stick) and made the
    delete route refuse `/storage/XXXX-XXXX/...` with "internal storage only".
    Volumes are discovered the same way the storage cards are: through
    get_available_storages() (df + /proc/mounts).
    """
    global _STORAGE_ROOTS_CACHE
    now = time.time()
    if _STORAGE_ROOTS_CACHE['ts'] and now - _STORAGE_ROOTS_CACHE['ts'] < 5.0:
        return _STORAGE_ROOTS_CACHE['roots']

    candidates = ['/sdcard', '/storage/emulated/0', '/mnt/sdcard']
    try:
        candidates += [st.get('path', '') for st in get_available_storages()]
    except Exception:
        pass
    roots = set()
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if os.path.exists(candidate):
                roots.add(os.path.realpath(candidate))
        except OSError:
            pass
    # Where Android really mounts removable volumes. Listing this needs no
    # special permission and covers a card that df could not parse.
    try:
        for name in os.listdir('/mnt/media_rw'):
            full = os.path.join('/mnt/media_rw', name)
            try:
                if os.path.isdir(full):
                    roots.add(os.path.realpath(full))
            except OSError:
                pass
    except OSError:
        pass
    _STORAGE_ROOTS_CACHE = {'ts': now, 'roots': roots}
    return roots


_STORAGE_ROOTS_CACHE = {'ts': 0.0, 'roots': set()}


def _is_storage_root(path):
    """True for a volume root like /sdcard, /storage/emulated/0 or /storage/ABCD-1234."""
    p = str(path).rstrip('/') or '/'
    if p in ('/', '/storage', '/mnt', '/sdcard', '/mnt/sdcard',
             '/storage/emulated', '/storage/emulated/0'):
        return True
    try:
        real = os.path.realpath(p)
    except OSError:
        real = p
    return real in _allowed_storage_roots()


def protected_target_reason(path):
    """Why `path` must never be deleted, or None if it is fair game.

    The delete is deliberately strong (the user wants to remove anything, fast),
    so the only things kept out of reach are the volume roots themselves and the
    paths whose loss would break the device or the app: `/`, `/data`, Termux's
    prefix and this app's own checkout. Everything inside a volume - including
    Android/data and Android/obb - is allowed; Shizuku removes what Android
    refuses to the app.
    """
    try:
        real = os.path.realpath(path).rstrip('/') or '/'
    except OSError:
        real = str(path).rstrip('/') or '/'

    if _is_storage_root(real):
        return 'لا يمكن حذف جذر التخزين نفسه'

    protected = {'/', '/data', '/data/data', '/data/user', '/system', '/vendor',
                 '/proc', '/sys', '/storage'}
    for extra in (os.path.expanduser('~'),
                  os.environ.get('PREFIX') or '/data/data/com.termux/files/usr',
                  os.path.dirname(os.path.abspath(__file__))):
        try:
            if extra:
                protected.add(os.path.realpath(extra).rstrip('/') or '/')
        except OSError:
            pass

    for item in protected:
        # Refuse the protected path itself *and* any ancestor of it: deleting
        # /data/data would take Termux and every other app with it.
        if real == item or item.startswith(real + '/'):
            return 'هذا مسار نظام أو مسار التطبيق نفسه — محمي من الحذف'
    return None



def android_private_reason(path):
    """Android/data and Android/obb belong to other apps - always undeletable.

    Even with MANAGE_EXTERNAL_STORAGE Android refuses to remove another app's
    private folder, so we say so up front instead of failing item by item.
    Only a *storage root* Android folder counts; a user folder that merely
    happens to be named "Android/data" deeper in the tree is not affected.
    """
    try:
        real = os.path.realpath(path)
    except Exception:
        real = str(path)
    parts = [p for p in real.split('/') if p]
    for i in range(len(parts) - 1):
        if parts[i].lower() == 'android' and parts[i + 1].lower() in ('data', 'obb'):
            parent = '/' + '/'.join(parts[:i])
            if _is_storage_root(parent):
                return ('مجلد خاص بتطبيق آخر (Android/{}) — أندرويد يمنع حذفه '
                        'حتى مع الوصول الكامل'.format(parts[i + 1]))

    return None


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


# Destinations whose probe timed out -> absolute time until which we refuse them.
_UNHEALTHY_DESTS = {}


def _probe_write(dest, result):
    probe = os.path.join(dest, '.sdmp3_probe')
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        try:
            os.write(fd, b'sdmp3')
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.remove(probe)
        except OSError:
            pass
        result.append(None)
    except Exception as exc:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except Exception:
            pass
        result.append(exc)


def check_destination(dest, timeout=DEST_PROBE_TIMEOUT):
    """Return None if the destination answers, otherwise an Arabic error.

    Writing a 5-byte probe file with a hard timeout catches the case that made
    the whole app look dead: a card that is still mounted but no longer answers,
    so every write blocks forever inside the kernel. Without this the UI sits at
    "copying" with 0% and no error, which is exactly how it used to fail.
    """
    if not dest or not os.path.isdir(dest):
        return 'مسار الوجهة غير صالح'

    now = time.time()
    dead_until = _UNHEALTHY_DESTS.get(dest)
    if dead_until is not None:
        if now < dead_until:
            return ('الكارت لا يستجيب (تم اكتشاف ذلك قبل قليل). أخرجه وأدخله '
                    'جيداً ثم أعد المحاولة.')
        _UNHEALTHY_DESTS.pop(dest, None)

    result = []
    worker = threading.Thread(target=_probe_write, args=(dest, result), daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        # The probe is stuck in the kernel; remember it and stop probing.
        _UNHEALTHY_DESTS[dest] = time.time() + UNHEALTHY_DEST_TTL
        log_event('destination NOT responding (>{:.0f}s): {!r}'.format(timeout, dest))
        return ('الوجهة لا تستجيب (الكارت معلّق). أخرجه وأدخله ثم أعد المحاولة. '
                'إذا استمرت المشكلة أعد تشغيل التطبيق: sdmp3 restart')
    if not result:
        return 'تعذر اختبار الوجهة'
    if result[0] is None:
        return None
    exc = result[0]
    log_event('destination probe failed: {!r} -> {}'.format(dest, exc))
    reason = describe_os_error(exc)
    if isinstance(exc, OSError) and getattr(exc, 'errno', None) in (5, 30):
        return 'تعذر الكتابة على الوجهة — ' + CARD_WRITE_HINT
    return 'تعذر الكتابة على الوجهة: ' + reason

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
                # One callback per entry: each unlink is also the heartbeat that
                # keeps stall detection honest on a slow or dying card.
                if progress_cb: progress_cb(deleted, len(failures))
            for name in dirs:
                dp = os.path.join(root, name)
                ok, reason = try_remove_entry(dp)
                if ok: deleted += 1
                else: note(dp, reason)
                if progress_cb: progress_cb(deleted, len(failures))
    except Exception as exc:
        note(target, describe_os_error(exc))

    if os.path.lexists(target):
        ok, reason = try_remove_entry(target)
        if ok: deleted += 1
        else: note(target, reason)

    if progress_cb: progress_cb(deleted, len(failures))
    return deleted, failures

def _advise_sequential(fd):
    """Tell the kernel this file is read/written front-to-back.

    Lets the FUSE/vfat layer read ahead and batch writes. Purely a hint: if the
    platform does not support it, nothing happens.
    """
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
    except (AttributeError, OSError):
        pass


def copy_file_sequential(src, dst, cancel_check=None, on_bytes=None):
    """Copy one file from start to finish, in order, with a large buffer.

    Deliberately single-file and single-threaded: the FAT directory-entry order
    is what old MP3 players use to play a folder in sequence, so this must never
    be parallelised. Returns the number of bytes copied.

    There is no fsync per file any more - the caller flushes every SYNC_THRESHOLD
    bytes and once at the end with os.sync().
    """
    if cancel_check is not None and cancel_check():
        raise TransferCancelled()

    written_total = 0
    src_fd = None
    dst_fd = None
    try:
        src_fd = os.open(src, os.O_RDONLY)
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        _advise_sequential(src_fd)
        _advise_sequential(dst_fd)
        while True:
            if cancel_check is not None and cancel_check():
                raise TransferCancelled()
            chunk = os.read(src_fd, COPY_BUFFER_SIZE)
            if not chunk:
                break
            view = memoryview(chunk)
            offset = 0
            while offset < len(chunk):
                offset += os.write(dst_fd, view[offset:])
            written_total += len(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    except BaseException:
        # Never leave a half-written MP3 behind: it would break playback order.
        if dst_fd is not None:
            try: os.close(dst_fd)
            except OSError: pass
            dst_fd = None
        try:
            if os.path.exists(dst): os.remove(dst)
        except OSError: pass
        raise
    finally:
        if src_fd is not None:
            try: os.close(src_fd)
            except OSError: pass
        if dst_fd is not None:
            try: os.close(dst_fd)
            except OSError: pass
    return written_total

def plan_transfer(paths, dest, excludes=None, cancel_check=None, on_progress=None):
    """Build the copy queue AND the totals in a single traversal.

    Returns (tasks, total_files, total_bytes, skipped):

      tasks       exact write order (folder first, then its children, natural
                  sort) - the queue that guarantees JOC playback order
      skipped     source folders that could not be read at all, with a reason
                  (e.g. the damaged FAT entry that Android answers with EIO)

    The old code walked the whole tree twice (scan_source, then build_dfs_queue);
    on FUSE every stat is a round-trip, so this halves the planning cost.

    `on_progress` is called once per folder so a huge tree over a slow card does
    not look like a stalled transfer while it is still being scanned.
    """
    excludes = excludes or set()
    tasks = []
    total_files = 0
    total_bytes = 0
    skipped = []

    def walk(src, dst, entry=None):
        nonlocal total_files, total_bytes
        if src in excludes:
            return
        if cancel_check is not None and cancel_check():
            raise TransferCancelled()

        name = entry.name if entry is not None else os.path.basename(src)
        dst_path = os.path.join(dst, name)
        try:
            # DirEntry.is_file()/is_dir() reuse the stat scandir already did, so
            # a file costs no extra FUSE round-trip here.
            is_file = entry.is_file() if entry is not None else os.path.isfile(src)
            is_dir = entry.is_dir() if entry is not None else os.path.isdir(src)
        except OSError as exc:
            skipped.append((src, describe_os_error(exc)))
            return

        if is_file:
            tasks.append((src, dst_path, 'file'))
            total_files += 1
            try:
                size = (entry.stat(follow_symlinks=False).st_size
                        if entry is not None else os.path.getsize(src))
                total_bytes += max(size, 0)
            except OSError:
                pass
        elif is_dir:
            tasks.append((src, dst_path, 'dir'))
            if on_progress is not None:
                try:
                    on_progress()
                except Exception:
                    pass
            try:
                with os.scandir(src) as it:
                    children = sorted(it, key=lambda e: natural_sort_key(e.name))
            except OSError as exc:
                # Report it instead of silently dropping the folder.
                skipped.append((src, describe_os_error(exc)))
                return
            for child in children:
                walk(child.path, dst_path, child)

    for root in sorted(paths, key=natural_sort_key):
        walk(root, dest)
    return tasks, total_files, total_bytes, skipped


def copy_targets_source(paths, dest):
    """True when the planned copy would write a source *onto itself*.

    dest_inside_source catches "destination inside the source". This catches the
    mirror case, which is just as destructive and much easier to hit by accident:
    choosing the parent of a source folder as the destination, e.g.
    source /sdcard/A with dest /sdcard. The planned target is then
    /sdcard/A/<file> — the source file itself. copy_file_sequential opens the
    target with O_TRUNC, so the file is emptied and the error path deletes it:
    silent data loss reported as a normal copy.
    """
    try:
        dest_real = os.path.realpath(dest)
    except OSError:
        return False
    for path in paths:
        try:
            src_real = os.path.realpath(path)
        except OSError:
            continue
        name = os.path.basename(src_real.rstrip(os.sep))
        if name and os.path.join(dest_real, name) == src_real:
            return True
    return False


def dest_inside_source(paths, dest):
    """True when the destination lives inside one of the sources.

    Copying a folder into itself would duplicate the tree into itself for as
    long as there is free space, so it is refused before anything is written.
    """
    try:
        dest_real = os.path.realpath(dest)
    except OSError:
        return False
    for path in paths:
        try:
            src_real = os.path.realpath(path)
        except OSError:
            continue
        if dest_real == src_real or dest_real.startswith(src_real + os.sep):
            return True
    return False


def copy_thread_task(paths, dest, excludes=None):
    acquire_wakelock()
    reset_state(status='copying', action='copy', start_time=time.time())
    copied = 0
    bytes_done = 0
    bytes_since_sync = 0
    failed_details = []
    write_failed = False
    excludes = excludes or set()
    last_ui_update = time.time()
    UI_UPDATE_INTERVAL = 0.5
    started = time.time()

    try:
        # Safety net: the route already refuses this, but the worker must never
        # be able to duplicate a tree into itself either.
        if dest_inside_source(paths, dest):
            raise RuntimeError('لا يمكن النسخ إلى مجلد داخل المصدر: {}'.format(dest))
        if copy_targets_source(paths, dest):
            raise RuntimeError('الوجهة هي مجلد المصدر نفسه — اختر مجلداً جديداً داخلها')

        set_state(file='جاري فحص الملفات وترتيبها بتسلسل JOC...')
        touch_progress()
        tasks, total, needed_bytes, skipped = plan_transfer(
            paths, dest, excludes, cancel_check=is_cancelled, on_progress=touch_progress)
        if is_cancelled():
            raise TransferCancelled()
        touch_progress()
        set_state(total=total, total_bytes=needed_bytes)

        log_event('copy start: {} file(s), {}, dest={!r}, skipped={}'.format(
            total, format_size(needed_bytes), dest, len(skipped)))
        if skipped:
            set_state(skipped=['{} — {}'.format(p, r) for p, r in skipped[:20]])
            for p, r in skipped[:10]:
                log_event('copy skipped (unreadable): {!r} -> {}'.format(p, r))

        if not has_enough_space(dest, needed_bytes):
            raise RuntimeError(
                'المساحة المتوفرة غير كافية. المطلوب تقريباً: {}'.format(format_size(needed_bytes)))

        set_state(file='جاري النقل المستقر المباشر...')

        def on_bytes(count):
            nonlocal bytes_done, bytes_since_sync
            bytes_done += count
            bytes_since_sync += count
            # Heartbeat per chunk: a slow-but-working card keeps the transfer
            # "alive", only a really hung one falls silent long enough to flag.
            touch_progress()

        for src, dst_path, kind in tasks:
            if is_cancelled():
                raise TransferCancelled()
            name = os.path.basename(src)

            if kind == 'file':
                try:
                    copy_file_sequential(src, dst_path, cancel_check=is_cancelled, on_bytes=on_bytes)
                    copied += 1
                except TransferCancelled:
                    raise
                except Exception as e:
                    reason = describe_os_error(e)
                    failed_details.append((src, reason))
                    log_event('copy failed: {!r} -> {}'.format(src, reason))
                    if isinstance(e, OSError) and getattr(e, 'errno', None) in (5, 30):
                        # The card itself refuses writes (a dying card or broken
                        # FAT). Keep going so the user sees the full picture, but
                        # flag it for the final hint.
                        write_failed = True
                    if isinstance(e, OSError) and getattr(e, 'errno', None) == 28:
                        raise RuntimeError('انتهت المساحة الخالية في الوجهة أثناء النسخ.')

                # Flush the whole transfer to the card occasionally, never per file.
                if bytes_since_sync >= SYNC_THRESHOLD:
                    try: os.sync()
                    except OSError: pass
                    bytes_since_sync = 0

            elif kind == 'dir':
                try:
                    os.makedirs(dst_path, exist_ok=True)
                except Exception as e:
                    reason = describe_os_error(e)
                    failed_details.append((src, reason))
                    log_event('mkdir failed: {!r} -> {}'.format(src, reason))

            now = time.time()
            if now - last_ui_update >= UI_UPDATE_INTERVAL:
                elapsed = max(now - started, 0.001)
                speed = bytes_done / elapsed / (1024 * 1024)
                if needed_bytes:
                    progress = int(min(bytes_done / needed_bytes, 1) * 100)
                    remaining = max(needed_bytes - bytes_done, 0)
                    eta = int(remaining / (speed * 1024 * 1024)) if speed > 0 else 0
                else:
                    done_count = copied + len(failed_details)
                    progress = int((done_count / total) * 100) if total else 0
                    eta = 0
                set_state(copied=copied, failed=len(failed_details),
                          progress=min(progress, 99), file=name,
                          copied_bytes=bytes_done, speed_mbps=round(speed, 2),
                          eta_seconds=eta)
                last_ui_update = now

        set_state(file='جاري تأكيد كتابة البيانات النهائية...')
        try: os.sync()
        except OSError: pass

        elapsed = max(time.time() - started, 0.001)
        avg_speed = bytes_done / elapsed / (1024 * 1024)
        if failed_details:
            final_msg = 'اكتمل النسخ مع فشل {} عنصر - راجع التفاصيل'.format(len(failed_details))
        else:
            final_msg = 'اكتمل النسخ بنجاح.'

        log_event('copy done: copied={} failed={} bytes={} in {:.1f}s ({:.2f} MB/s)'.format(
            copied, len(failed_details), bytes_done, elapsed, avg_speed))

        invalidate_cache_for_path(dest)
        finish_transfer('done', final_msg, extra={
            'progress': 100,
            'copied': copied,
            'failed': len(failed_details),
            'errors': ['{} — {}'.format(os.path.basename(p), r) for p, r in failed_details[:30]],
            'skipped': ['{} — {}'.format(p, r) for p, r in skipped[:20]],
            'copied_bytes': bytes_done,
            'speed_mbps': round(avg_speed, 2),
            'eta_seconds': 0,
            'hint': CARD_WRITE_HINT if write_failed else '',
        })

    except TransferCancelled:
        try: os.sync()
        except OSError: pass
        log_event('copy cancelled by user after {}s'.format(int(time.time() - started)))
        finish_transfer('cancelled', 'تم إلغاء النسخ.')
    except Exception as e:
        try: os.sync()
        except OSError: pass
        log_event('copy ERROR: {}: {}'.format(type(e).__name__, e))
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
        total_items = count_files([dest_path], cancel_check=is_cancelled, count_dirs=True,
                                 on_progress=touch_progress)
        if is_cancelled(): raise TransferCancelled()
        set_state(total=total_items)
        
        current_time = start_timestamp

        # topdown=False is REQUIRED here. Modifying a file inherently updates the directory it sits in.
        for root, dirs, files in os.walk(dest_path, topdown=False):
            touch_progress()
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
# Edge TTS is network-bound. Chunks are independent and are written to numbered
# files, so synthesising several at once cannot change the merge order.
TTS_PARALLEL_CHUNKS = 4

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

def _run_ffmpeg(cmd, cancel_check=None, max_seconds=1800):
    """Run ffmpeg while keeping the progress heartbeat alive.

    Merging a long index can take minutes. subprocess.run() left the state
    untouched for that whole time, so the UI announced a dead SD card during a
    merge that never touches a card. Polling also makes Cancel effective while
    ffmpeg is running instead of only between chunks.
    """
    started = time.time()
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf)
        try:
            while True:
                try:
                    proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    touch_progress()
                    if cancel_check is not None and cancel_check():
                        proc.kill()
                        proc.wait()
                        raise TransferCancelled()
                    if time.time() - started > max_seconds:
                        proc.kill()
                        proc.wait()
                        raise RuntimeError('ffmpeg تجاوز الوقت المسموح')
        finally:
            if proc.poll() is None:
                try: proc.kill()
                except OSError: pass
                proc.wait()
        if proc.returncode != 0:
            errf.seek(0)
            tail = errf.read().decode('utf-8', 'replace').strip().splitlines()
            raise RuntimeError('ffmpeg: ' + (tail[-1] if tail else 'فشل بلا رسالة'))


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
    _run_ffmpeg(cmd)

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
    try:
        _run_ffmpeg(cmd, cancel_check=is_cancelled)
    except BaseException:
        # A partial MP3 would be offered as a finished index.
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
        except OSError:
            pass
        raise

# ------------------ HTML & JS Template ---------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SD Card Coffee</title>
    <meta name="theme-color" content="#151e28">
    <style>
        :root {
            /* "Coffee Blues" palette — https://www.color-hex.com/color-palette/102595
               #39918c teal · #d0b49f sand · #be9078 tan · #ab6b51 clay · #2f435a slate
               The page/card shades are the palette slate darkened (x0.45 / x0.70 /
               x0.87) so the UI stays dark; the slate itself is the border. Warm
               sand light (#ece2d8) on cool slate is the whole character of this
               theme — do not neutralise either side. */
            --bg: #151e28;
            --card: #212f3f;
            --card-2: #2a3a4c;
            --field: #151e28;        /* recessed: inputs, tree, storage rows, tracks */
            --border: #2f435a;
            --text: #ece2d8;
            --text-muted: #b0a59b;

            /* Teal is the accent. The palette teal is light enough to *mark*
               (#39918c = 3.6:1 on a card) but a filled button needs a label at
               4.5:1+, and white on #39918c is only 3.75 — so filled buttons use
               the palette's darker teal (#2e7a75, white label 5.05:1). */
            --accent: #39918c;
            --accent-hover: #2e7a75;
            --accent-fill: #2e7a75;
            --accent-fill-hover: #276a66;
            --accent-ink: #151e28;   /* label on warning/success/danger fills */
            --accent-soft: rgba(57, 145, 140, 0.18);
            --accent-line: rgba(57, 145, 140, 0.42);
            /* Teal readable at 11px on the dark card (accent itself is 3.6:1). */
            --accent-text: #7fc4bf;
            --accent-glow: rgba(57, 145, 140, 0.40);
            --success: #22c55e;
            --danger: #f43f5e;
            --danger-ink: #fc8b9b;   /* danger *text* (6.0:1 card, 5.2:1 raised) */
            --danger-soft: rgba(244, 63, 94, 0.12);
            --warning: #f59e0b;
            --warning-soft: rgba(245, 158, 11, 0.10);
            --warning-line: rgba(245, 158, 11, 0.38);
            /* Focus ring: teal at low alpha so it reads on every surface. */
            --focus: rgba(57, 145, 140, 0.45);

            /* One hue per volume type, all from the same palette. Clay is too
               dark for 11px pill text, so the kinds carry an -ink variant. */
            --kind-internal: #d0b49f;
            --kind-internal-ink: #d0b49f;
            --kind-internal-soft: rgba(208, 180, 159, 0.16);
            /* Brighter clay: the palette clay (#ab6b51) disappeared on a dark
               tile and sat too close to the sand of internal storage. */
            --kind-sdcard: #d98b63;
            --kind-sdcard-ink: #dda88c;
            --kind-sdcard-soft: rgba(217, 139, 99, 0.20);
            --kind-usb: #8fa9c4;
            --kind-usb-ink: #a8bfd6;
            --kind-usb-soft: rgba(143, 169, 196, 0.16);
            /* Unknown volume: quiet olive, the one hue the palette was missing. */
            --kind-card: #a3b18a;
            --kind-card-ink: #b7c39f;
            --kind-card-soft: rgba(163, 177, 138, 0.18);
            /* Icon tiles need more colour than a pill background to read. */
            --kind-internal-tile: rgba(208, 180, 159, 0.22);
            --kind-sdcard-tile: rgba(217, 139, 99, 0.24);
            --kind-usb-tile: rgba(143, 169, 196, 0.22);
            --kind-card-tile: rgba(163, 177, 138, 0.24);
            /* Border tints derived from each kind's *ink* so a pill's outline
               matches its text instead of borrowing another theme's hue. */
            --kind-internal-line: rgba(208, 180, 159, 0.42);
            --kind-sdcard-line: rgba(221, 168, 140, 0.50);
            --kind-usb-line: rgba(168, 191, 214, 0.42);
            --kind-card-line: rgba(183, 195, 159, 0.45);
            /* Sand fill for the secondary "apply spoofing" action: warm, part of
               the palette, and clearly not the teal primary button. */
            --sand-fill: #d0b49f;
            --sand-fill-hover: #c4a88f;
            --radius: 14px;
        }
        * { box-sizing: border-box; font-family: system-ui, -apple-system, 'Segoe UI', Roboto, 'Noto Sans Arabic', 'IBM Plex Sans Arabic', sans-serif; -webkit-tap-highlight-color: transparent; }
        body { background-color: var(--bg); color: var(--text); margin: 0; padding: 16px 14px 110px; font-size: 14px; line-height: 1.55; -webkit-font-smoothing: antialiased; }
        .container { max-width: 900px; margin: 0 auto; }

        .app-header { display:flex; align-items:center; gap:13px; padding-bottom: 16px; margin-top: 4px; margin-bottom: 14px; border-bottom: 1px solid var(--border); }
        .app-header .badge { width:44px; height:44px; border-radius:13px; background:var(--accent-soft); border:1px solid var(--accent-line); display:flex; align-items:center; justify-content:center; flex-shrink:0; }
        .app-header .badge svg { stroke: var(--accent); width:22px; height:22px; }
        .app-header h1 { font-size: 19px; color: var(--text); margin:0; font-weight: 700; letter-spacing: -0.2px; }
        .app-header p { margin: 3px 0 0; font-size: 12.5px; color: var(--text-muted); }
        .pill-row { display:flex; flex-wrap:wrap; gap:6px; margin-top:9px; }
        .pill { font-size: 11px; line-height: 1; padding: 6px 10px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-muted); background: rgba(236,226,216,0.04); white-space: nowrap; }
        /* The version is the app's signature, so it wears the accent. */
        #appVersionPill { color: var(--accent-text); border-color: var(--accent-line); background: var(--accent-soft); font-weight: 600; }
        .pill-ok { color: var(--success); border-color: rgba(34,197,94,0.35); background: rgba(34,197,94,0.08); }
        .pill-warn { color: var(--warning); border-color: var(--warning-line); background: var(--warning-soft); }
        /* Pills tinted by volume type: SD card / USB OTG / internal / unknown.
           Text uses the -ink variant so 11px labels stay legible (4.5:1+). */
        .pill-kind-internal { color: var(--kind-internal-ink); border-color: var(--kind-internal-line); background: var(--kind-internal-soft); }
        .pill-kind-sdcard { color: var(--kind-sdcard-ink); border-color: var(--kind-sdcard-line); background: var(--kind-sdcard-soft); }
        .pill-kind-usb { color: var(--kind-usb-ink); border-color: var(--kind-usb-line); background: var(--kind-usb-soft); }
        .pill-kind-card { color: var(--kind-card-ink); border-color: var(--kind-card-line); background: var(--kind-card-soft); }

        .card { background: var(--card); padding: 18px; border-radius: var(--radius); border: 1px solid var(--border); margin-bottom: 16px; }
        @media (max-width: 480px) { .card { padding: 15px; } body { padding: 12px 10px 110px; } }
        .card-title { display:flex; align-items:center; gap:9px; font-size: 14.5px; font-weight:600; color: var(--text); margin: 0 0 15px; }
        .card-title svg { width:17px; height:17px; stroke: var(--accent); }

        label { font-weight: 500; margin-bottom: 8px; display: block; color: var(--text-muted); font-size: 13px; }
        select, input[type="text"], input[type="number"], input[type="datetime-local"] { width: 100%; padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; font-size: 14px; background-color: var(--field); color: var(--text); outline: none; margin-bottom: 12px; transition: border-color 0.15s, box-shadow 0.15s; min-height: 46px; }
        select:focus, input[type="text"]:focus, input[type="number"]:focus, input[type="datetime-local"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
        textarea { width: 100%; padding: 12px 14px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 10px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 14px; line-height: 1.7; background-color: var(--field); color: var(--text); outline: none; resize: none; }
        textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

        .btn { padding: 11px 16px; min-height: 44px; border: none; border-radius: 10px; cursor: pointer; font-weight: 600; font-size: 13.5px; color: var(--text); transition: transform 0.08s ease, filter 0.15s ease, background-color 0.15s ease, border-color 0.15s ease; display: inline-flex; align-items: center; justify-content: center; gap: 7px; }
        .btn:active { transform: scale(0.97); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
        .btn:focus-visible, .icon-btn:focus-visible, .storage-card:focus-visible, .toggle-btn:focus-visible {
            outline: 2px solid var(--accent); outline-offset: 2px; }
        .btn-primary { background: var(--accent-fill); color: #f7f4f0; }
        .btn-primary:hover { background: var(--accent-fill-hover); }
        .btn-success { background: var(--success); color: var(--accent-ink); }
        .btn-success:hover { filter: brightness(1.08); }
        .btn-danger { background: var(--danger); color: var(--accent-ink); }
        .btn-danger:hover { filter: brightness(1.08); }
        .btn-sand { background: var(--sand-fill); color: var(--accent-ink); }
        .btn-sand:hover { background: var(--sand-fill-hover); }
        .btn-outline { background: var(--card-2); border: 1px solid var(--border); color: var(--text); }
        .btn-outline:hover { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-soft); }
        .btn-sm { padding: 8px 12px; min-height: 38px; font-size: 12.5px; }

        .tree { max-height: 62vh; overflow: auto; background: var(--field); border: 1px solid var(--border); border-radius: 10px; padding: 8px; scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
        .tree::-webkit-scrollbar, .modal-content .modal-body::-webkit-scrollbar { width: 8px; height: 8px; }
        .tree::-webkit-scrollbar-thumb, .modal-content .modal-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; }
        .tree::-webkit-scrollbar-track, .modal-content .modal-body::-webkit-scrollbar-track { background: transparent; }
        .item-row { display: flex; align-items: center; padding: 9px 8px; border-radius: 8px; border-bottom: 1px solid var(--border); min-width: max-content; transition: background-color 0.15s; }
        .item-row:last-child { border-bottom: none; }
        .item-row:hover { background: var(--card-2); }
        .toggle-btn { background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: 12px; width: 26px; height: 26px; padding: 0; flex-shrink:0; }
        .excluded-badge { color: var(--warning); font-size: 11px; margin-right: 8px; background: var(--warning-soft); border: 1px solid var(--warning-line); padding: 2px 6px; border-radius: 6px;}
        .checkbox { accent-color: var(--accent); width: 18px; height: 18px; margin: 0 10px; flex-shrink: 0; cursor: pointer;}
        .item-name { cursor: pointer; display: flex; align-items: center; gap: 8px; white-space: nowrap; padding-right: 6px; color: var(--text); font-size:14px;}
        .children { margin-right: 24px; display: none; border-right: 1px solid var(--border); padding-right: 12px; }
        .children.open { display: block; }

        .row-actions { display:flex; gap:6px; margin-inline-start:auto; flex-shrink:0; padding-inline-start:10px; }
        /* Files in the picker keep their size on the far side, so the delete and
           rename buttons line up with the folder rows above them. */
        .picker-file-row .row-actions { margin-inline-start: 0; padding-inline-start: 6px; }
        .picker-file-row .file-size { margin-inline-start: auto; }
        .icon-btn { width:30px; height:30px; border-radius:8px; border:1px solid var(--border); background:var(--field); color:var(--text-muted); display:flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; }
        .icon-btn svg { width:14px; height:14px; }
        .icon-btn:active { transform: scale(0.92); }

        /* Storage picker: one card per volume, icon + Arabic label + real path.
           Each kind sets --kind/--kind-soft so the card is coloured by *what it
           is* (SD / USB / internal) instead of by the global accent. */
        .storage-grid { display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px; }
        .storage-card { display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px 12px;
            border: 1px solid var(--border); border-inline-start: 3px solid var(--kind, var(--border));
            border-radius: 10px; background: var(--field); color: var(--text);
            cursor: pointer; text-align: right; transition: border-color .15s, background .15s;
            font-family: inherit; appearance: none; -webkit-appearance: none; }
        .storage-card:hover { border-color: var(--kind, var(--accent)); }
        .storage-card.active { border-color: var(--kind, var(--accent)); background: var(--kind-soft, var(--accent-soft)); }
        .storage-card .storage-ico { width: 36px; height: 36px; padding: 7px; flex-shrink: 0; box-sizing: border-box;
            border-radius: 10px; background: var(--kind-tile, var(--accent-soft)); color: var(--kind, var(--text-muted)); }
        .storage-card.kind-internal { --kind: var(--kind-internal); --kind-ink: var(--kind-internal-ink); --kind-soft: var(--kind-internal-soft); --kind-tile: var(--kind-internal-tile); }
        .storage-card.kind-sdcard { --kind: var(--kind-sdcard); --kind-ink: var(--kind-sdcard-ink); --kind-soft: var(--kind-sdcard-soft); --kind-tile: var(--kind-sdcard-tile); }
        .storage-card.kind-usb { --kind: var(--kind-usb); --kind-ink: var(--kind-usb-ink); --kind-soft: var(--kind-usb-soft); --kind-tile: var(--kind-usb-tile); }
        .storage-card.kind-card { --kind: var(--kind-card); --kind-ink: var(--kind-card-ink); --kind-soft: var(--kind-card-soft); --kind-tile: var(--kind-card-tile); }
        .storage-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex-grow: 1; }
        .storage-name { font-size: 14px; font-weight: 600; }
        .storage-path { font-size: 11px; color: var(--text-muted); direction: ltr; text-align: right;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .storage-card.active .storage-name { color: var(--kind-ink, var(--accent)); }
        .storage-badge { display: none; margin-inline-start: auto; font-size: 10px; padding: 3px 7px; border-radius: 999px;
            border: 1px solid var(--border); color: var(--text-muted); white-space: nowrap; }
        .storage-card.active .storage-badge { display: inline-block; color: var(--kind-ink, var(--accent)); border-color: var(--kind, var(--accent)); }
        /* Path box in the picker: border says which volume you are browsing. */
        .kind-border-internal { border-color: var(--kind-internal) !important; }
        .kind-border-sdcard { border-color: var(--kind-sdcard) !important; }
        .kind-border-usb { border-color: var(--kind-usb) !important; }
        .kind-border-card { border-color: var(--kind-card) !important; }
        .picker-hint { font-size: 11.5px; color: var(--text-muted); margin: 6px 2px 8px; }
        .picker-file-row .item-name { color: var(--text-muted); cursor: default; }
        .file-size { margin-inline-start: auto; font-size: 11px; color: var(--text-muted); direction: ltr; padding-inline-start: 10px; }
        .icon-btn:hover { color: var(--accent); border-color: var(--accent); }
        .icon-btn.danger:hover { color: var(--danger-ink); border-color: var(--danger); }

        .cart { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(24, 33, 44, 0.97); backdrop-filter: blur(6px); border-top: 1px solid var(--border); padding: 14px 18px calc(14px + env(safe-area-inset-bottom)); display: flex; justify-content: space-between; align-items: center; z-index: 50; box-shadow: 0 -8px 24px -8px rgba(0,0,0,0.5); }
        .cart-buttons { display: flex; gap: 12px; }

        .modal { display: none; position: fixed; inset: 0; width: 100%; height: 100%; background: rgba(8, 12, 16, 0.88); z-index: 1000; justify-content: center; align-items: safe center; padding: 16px; overflow: auto; -webkit-overflow-scrolling: touch; }
        .modal-content { background: var(--card); padding: 20px; border-radius: 18px; width: 100%; max-width: 600px; border: 1px solid var(--border); max-height: max(70vh, 480px); display: flex; flex-direction: column; box-shadow: 0 24px 48px -12px rgba(0,0,0,0.6); margin: auto; }
        .modal-content .modal-body { overflow-y: auto; flex: 1 1 auto; min-height: 0; -webkit-overflow-scrolling: touch; }
        .modal-content .modal-footer { display: flex; gap: 12px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); flex-shrink: 0; }
        /* The folder picker keeps one height whatever the folder holds: a 3-item
           folder and a 3 000-item folder used to resize the dialog, which moved
           the Confirm button under the finger. */
        .modal-content.modal-picker { height: min(84vh, 720px); max-height: 84vh; }
        .modal-content.modal-picker #destTree { max-height: none; }
        @media (max-width: 480px) {
            .modal { padding: 8px; align-items: stretch; }
            .modal-content { padding: 16px; max-height: 92vh; border-radius: 14px; }
            .modal-content.modal-picker { height: 92vh; max-height: 92vh; }
        }

        .progress-bar { width: 100%; height: 10px; background: var(--field); border-radius: 999px; overflow: hidden; margin: 15px 0; border: 1px solid var(--border);}
        .progress-fill { height: 100%; background: var(--accent); width: 0%; border-radius: 999px; transition: width 0.3s ease; }
        /* A delete has no known total, so its bar sweeps instead of pretending
           to know a percentage. The solid fill keeps it readable when the user
           asked the system for reduced motion. */
        .progress-fill.indeterminate {
            width: 100% !important; background-color: var(--accent);
            background-image: linear-gradient(90deg, rgba(236,226,216,0) 0%, rgba(236,226,216,0.55) 50%, rgba(236,226,216,0) 100%);
            background-size: 40% 100%; background-repeat: no-repeat;
            animation: sdmp3-sweep 1.3s linear infinite;
        }
        @keyframes sdmp3-sweep {
            0%   { background-position: -40% 0; }
            100% { background-position: 140% 0; }
        }
        .ltr-input { direction: ltr; text-align: left; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }

        .disk-space-container { margin-bottom: 10px; font-size: 13px; color: var(--text-muted); display: none;}
        .disk-bar { width: 100%; height: 6px; background: var(--field); border-radius: 3px; overflow: hidden; margin-top: 6px; border: 1px solid var(--border);}
        .disk-used { height: 100%; background: var(--accent); width: 0%; transition: width 0.3s ease; }

        .spoof-settings { background: var(--field); padding: 16px; border-radius: 10px; border: 1px dashed var(--border); margin-bottom: 10px; display:flex; flex-direction:column; gap:10px;}

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

        .seg.option.active { background: var(--accent-fill); color: #f7f4f0; font-weight: 600; }
        .seg.option.active svg { stroke: #f7f4f0; }
        .seg.option:hover:not(.active) { color: var(--accent); border-radius:7px; }

        svg.icon { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }

        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { transition: none !important; animation: none !important; }
        }
    </style>
</head>
<body>
<!-- SVG sprite: storage icons. Referenced with <use href="#ico-..."> so the same
     stroke icon is used in the cards and in the path picker. -->
<svg width="0" height="0" style="position:absolute; overflow:hidden;" aria-hidden="true" focusable="false">
    <symbol id="ico-internal" viewBox="0 0 24 24">
        <rect x="6" y="2" width="12" height="20" rx="2.5"></rect>
        <line x1="10.5" y1="18.5" x2="13.5" y2="18.5"></line>
    </symbol>
    <symbol id="ico-sd" viewBox="0 0 24 24">
        <path d="M9 2h5l4 4v14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V7z"></path>
        <path d="M9.5 6.5v2.5M12 6.5v2.5M14.5 6.5v2.5"></path>
    </symbol>
    <symbol id="ico-usb" viewBox="0 0 24 24">
        <path d="M9 2h6v5H9z"></path>
        <rect x="6.5" y="7" width="11" height="14" rx="2.5"></rect>
        <line x1="10" y1="12" x2="14" y2="12"></line>
        <line x1="10" y1="16" x2="14" y2="16"></line>
    </symbol>
    <symbol id="ico-card" viewBox="0 0 24 24">
        <rect x="4" y="4" width="16" height="16" rx="2"></rect>
        <line x1="4" y1="10" x2="20" y2="10"></line>
        <circle cx="8" cy="15.5" r="1"></circle>
        <line x1="12" y1="15.5" x2="17" y2="15.5"></line>
    </symbol>
</svg>

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
        <div class="storage-grid" id="source_storage_cards">
            {% for st in storages %}
            <button type="button" class="storage-card kind-{{ st.kind }}" data-path="{{ st.path }}" onclick="chooseStorage('source', this)">
                <svg class="icon storage-ico"><use href="#{{ st.icon }}"></use></svg>
                <span class="storage-meta">
                    <span class="storage-name">{{ st.name }}</span>
                    <span class="storage-path">{{ st.path }}</span>
                </span>
                <span class="storage-badge">الحالي</span>
            </button>
            {% endfor %}
        </div>
        <div style="display: flex; gap: 8px; align-items: center;">
            <input type="text" id="custom_source_path" class="ltr-input" style="margin-bottom: 0; flex-grow: 1;" placeholder="/sdcard/..." readonly>
            <button class="btn btn-outline btn-sm" style="white-space: nowrap;" onclick="openPathBrowser('source')">
                <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
                تصفح
            </button>
        </div>
        <div style="display:flex; gap:10px; align-items:center; margin-top:10px; flex-wrap:wrap;">
            <button class="btn btn-outline btn-sm" id="cacheFolderBtn" style="white-space: nowrap;" onclick="cacheCurrentFolder()">
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
        <div class="storage-grid" id="dest_storage_cards">
            {% for st in storages %}
            <button type="button" class="storage-card kind-{{ st.kind }}" data-path="{{ st.path }}" onclick="chooseStorage('dest', this)">
                <svg class="icon storage-ico"><use href="#{{ st.icon }}"></use></svg>
                <span class="storage-meta">
                    <span class="storage-name">{{ st.name }}</span>
                    <span class="storage-path">{{ st.path }}</span>
                </span>
                <span class="storage-badge">الوجهة</span>
            </button>
            {% endfor %}
        </div>

        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <input type="text" id="custom_dest_path" class="ltr-input" style="margin-bottom: 0; flex-grow: 1;" placeholder="/storage/XXXX-XXXX/..." oninput="checkDiskSpace(this.value)">
            <button class="btn btn-outline btn-sm" style="white-space: nowrap;" onclick="openPathBrowser('dest')">
                <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
                تحديد
            </button>
            <button class="btn btn-outline btn-sm" style="white-space: nowrap;" onclick="runDiagnostic()">
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
            <button class="btn btn-outline" onclick="openJocModal()">
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
        <button class="btn btn-sand" style="margin-top:10px; width:100%;" onclick="startSpoofOnly()">
            تطبيق التمويه
        </button>
    </div>

    <div class="cart">
        <div style="display: flex; align-items: center; gap: 12px;">
            <div style="font-size: 14px; font-weight: 500; color:var(--text-muted);">محدد: <b id="selectedCount" style="color:var(--text); font-size:16px;">0</b></div>
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
    <div class="modal-content modal-picker">
        <h3 id="pathBrowserTitle" style="margin-top:0; font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 15px;">اختر مجلد</h3>
        <div style="display:flex; gap: 8px; margin-bottom: 12px; margin-top: 10px;">
            <button class="btn btn-outline btn-sm" style="padding: 10px 14px;" onclick="goUpDest()">
                <svg class="icon" viewBox="0 0 24 24"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>
            </button>
            <input type="text" id="currentDestView" class="ltr-input" readonly style="margin: 0; background: var(--field);">
        </div>
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:4px; min-height:22px;">
            <span id="pickerStorageBadge" class="pill" style="display:none;"></span>
            <span id="pickerCounts" style="font-size:11.5px; color:var(--text-muted);"></span>
        </div>
        <div class="picker-hint">المجلدات قابلة للاختيار • الملفات يمكن حذفها أو إعادة تسميتها</div>
        <div id="destTree" class="tree" style="flex: 1 1 auto; min-height: 0;"></div>
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

<div class="modal" id="deleteErrorModal">
    <div class="modal-content" style="max-width: 460px;">
        <h3 style="margin-top:0; font-size: 16px; border-bottom: 1px solid var(--border); padding-bottom: 15px;">تعذر إكمال الحذف</h3>
        <p id="deleteErrorSummary" style="color: var(--text); font-size: 13.5px; line-height: 1.6; margin: 4px 0 10px; white-space: pre-line;"></p>
        <div id="deleteErrorList" style="display:flex; flex-direction:column; gap:6px; overflow-y:auto; max-height: 36vh;"></div>
        <div style="display:flex; gap:10px; margin-top:14px;">
            <button class="btn btn-outline" style="flex:1;" onclick="document.getElementById('deleteErrorModal').style.display='none'">إغلاق</button>
            <button class="btn btn-primary" style="flex:1;" id="copyDeleteErrBtn" onclick="copyDeleteErrors()">نسخ التفاصيل</button>
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
            <div id="totalSizeSelected" style="font-size:13px; font-weight:500; background:var(--field); padding:12px; border-radius:6px; border: 1px solid var(--border);">جاري حساب المساحة...</div>
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
                    <div style="display:flex; gap:6px; background:var(--field); border:1px solid var(--border); border-radius:10px; padding:4px;">
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
                        <div style="display:flex; align-items:center; gap:10px; background:var(--field); border:1px dashed var(--border); border-radius:10px; padding:10px 12px;">
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
        <div style="display:flex; justify-content:space-between; font-size:12px; color:var(--text-muted); margin-top:8px;">
            <span id="speedText" style="color:var(--accent); font-weight:500;"></span>
            <span id="failedText"></span>
        </div>
        <div id="errorsBox" style="display:none; max-height:200px; overflow:auto; background:var(--field); border:1px solid var(--border); border-radius:6px; padding:10px; margin-top:10px; font-size:12px; color:var(--danger-ink);"></div>
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

    // Row buttons carry no data of their own: one delegated listener reads the
    // path from the row. Embedding a filename into an inline onclick breaks the
    // row as soon as the name contains a quote.
    function rowActionsHtml(path, name, isDir) {
        return `<div class="row-actions" data-isdir="${isDir ? 1 : 0}">
            <button type="button" class="icon-btn" data-act="rename" title="إعادة تسمية">${iconEdit}</button>
            <button type="button" class="icon-btn danger" data-act="delete" title="حذف">${iconTrash}</button>
        </div>`;
    }

    function rowPath(row) {
        let cb = row.querySelector('input.checkbox');
        if (cb && cb.value) return cb.value;
        return row.dataset.path || '';
    }

    document.addEventListener('click', function (e) {
        if (!e.target.closest) return;
        let act = e.target.closest('[data-act]');
        if (act) {
            let row = act.closest('.item-row');
            if (!row) return;
            e.stopPropagation();
            let path = rowPath(row);
            if (!path) return;
            let name = path.split('/').filter(Boolean).pop() || path;
            let wrap = act.closest('.row-actions');
            let isDir = wrap ? wrap.dataset.isdir === '1' : false;
            if (act.dataset.act === 'rename') handleRenameClick(path, name, act);
            else if (act.dataset.act === 'delete') handleDeleteClick(path, isDir, act);
            return;
        }
        let toggle = e.target.closest('[data-toggle]');
        if (toggle) {
            let row = toggle.closest('.item-row');
            if (!row) return;
            let path = rowPath(row);
            if (path) { e.stopPropagation(); toggleFolder(toggle, path); }
        }
    });

    window.onload = function() {
        document.getElementById('custom_source_path').value = currentSourceRoot;
        syncStorageCards('source', currentSourceRoot);
        let savedDest = localStorage.getItem('joc_last_dest');
        if (savedDest) {
            document.getElementById('custom_dest_path').value = savedDest;
            syncStorageCards('dest', savedDest);
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
        syncStorageCards('source', path);
        loadSubFolders(path, document.getElementById('treeContainer'));
        checkCacheStatus(path);
    }

    // ---- storage cards (internal / SD card / USB OTG) -------------------
    // STORAGE_ROOTS comes from the server (name, path, kind, icon) so the
    // browser shows the same labels + icons as the selection cards.
    const STORAGE_ROOTS = {{ storages|tojson }};
    // Every kind the server can emit; used to reset the picker path-box colour.
    const STORAGE_KIND_LIST = ['internal', 'sdcard', 'usb', 'card'];

    function chooseStorage(mode, btn) {
        let path = btn.dataset.path;
        if (mode === 'source') updateSourceRoot(path);
        else updateDestPath(path);
    }

    function syncStorageCards(mode, path) {
        let wrap = document.getElementById(mode === 'source' ? 'source_storage_cards' : 'dest_storage_cards');
        if (!wrap) return;
        let current = String(path || '').replace(/\\/+$/, '');
        wrap.querySelectorAll('.storage-card').forEach(function(card) {
            let root = String(card.dataset.path || '').replace(/\\/+$/, '');
            // A subfolder of the card still counts as being on that storage.
            let active = !!root && (current === root || current.startsWith(root + '/'));
            card.classList.toggle('active', active);
        });
        // Tint the matching path box so the input itself says which volume it is.
        let box = document.getElementById(mode === 'source' ? 'custom_source_path' : 'custom_dest_path');
        if (box) {
            let st = storageForPath(current);
            STORAGE_KIND_LIST.forEach(function(k) { box.classList.remove('kind-border-' + k); });
            if (st) box.classList.add('kind-border-' + st.kind);
        }
    }

    function storageForPath(path) {
        // Longest matching root wins, so a card inside a mount never gets
        // mistaken for its parent storage.
        let target = String(path || '').replace(/\\/+$/, '');
        let best = null, bestLen = -1;
        STORAGE_ROOTS.forEach(function(st) {
            let root = String(st.path || '').replace(/\\/+$/, '');
            if (!root) return;
            if (target === root || target.startsWith(root + '/')) {
                if (root.length > bestLen) { best = st; bestLen = root.length; }
            }
        });
        return best;
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
        // Default spoof year is 2023 (random day + time inside that year).
        let start = new Date(2023, 0, 1).getTime();
        let end = new Date(2023, 11, 31, 23, 59).getTime();
        let randDate = new Date(start + Math.random() * (end - start));
        let tzoffset = randDate.getTimezoneOffset() * 60000;
        let localISOTime = (new Date(randDate - tzoffset)).toISOString().slice(0, 16);
        document.getElementById('spoofDateInput').value = localISOTime;
    }

    function updateDestPath(path) {
        if (path) {
            document.getElementById('custom_dest_path').value = path;
            localStorage.setItem('joc_last_dest', path);
            syncStorageCards('dest', path);
            checkDiskSpace(path);
        }
    }

    function checkDiskSpace(path) {
        if(!path) { syncStorageCards('dest', ''); return; }
        localStorage.setItem('joc_last_dest', path);
        syncStorageCards('dest', path);
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

    // Files shown per "page" in the picker; big folders stay instant to open.
    const PICKER_FILES_STEP = 300;
    let pickerFiles = [];
    let pickerShown = PICKER_FILES_STEP;

    function formatBytes(n) {
        n = Number(n) || 0;
        if (n < 1024) return n + ' B';
        if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
        if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
        return (n / 1073741824).toFixed(2) + ' GB';
    }

    function loadDestBrowser(path) {
        document.getElementById('currentDestView').value = path;
        let tree = document.getElementById('destTree');
        tree.dataset.dirPath = path;
        tree.innerHTML = '<div style="padding:10px;">جاري التحميل...</div>';

        // Which storage are we inside? (internal / SD card / USB OTG)
        let storage = storageForPath(path);
        let badge = document.getElementById('pickerStorageBadge');
        let pathBox = document.getElementById('currentDestView');
        let kind = storage ? storage.kind : null;
        if (storage) {
            badge.textContent = storage.name;
            badge.className = 'pill pill-kind-' + kind;
            badge.style.display = 'inline-block';
        } else {
            badge.style.display = 'none';
        }
        // Same hue on the path box, so a deep folder still names its volume.
        STORAGE_KIND_LIST.forEach(function(k) { pathBox.classList.remove('kind-border-' + k); });
        if (kind) pathBox.classList.add('kind-border-' + kind);

        fetch('/api/items?path=' + encodeURIComponent(path))
            .then(res => res.json())
            .then(data => {
                tree.innerHTML = '';
                let dirs = data.items.filter(i => i.is_dir);
                pickerFiles = data.items.filter(i => !i.is_dir);
                pickerShown = PICKER_FILES_STEP;
                document.getElementById('pickerCounts').textContent =
                    dirs.length + ' مجلد • ' + pickerFiles.length + ' ملف';

                if (data.items.length === 0) {
                    tree.innerHTML = '<div style="padding:10px; color:var(--text-muted);">مجلد فارغ</div>';
                    return;
                }
                dirs.forEach(item => {
                    let div = document.createElement('div');
                    div.className = 'item-row';
                    div.dataset.path = item.path;
                    div.innerHTML = `<div class="item-name" style="cursor:pointer;">${iconDir} <span></span></div>${rowActionsHtml(item.path, item.name, true)}`;
                    div.querySelector('.item-name span').textContent = item.name;
                    div.querySelector('.item-name').onclick = () => loadDestBrowser(item.path);
                    tree.appendChild(div);
                });
                renderPickerFiles(tree);
            })
            .catch(() => {
                pickerFiles = [];
                document.getElementById('pickerCounts').textContent = '';
                tree.innerHTML = '<div style="padding:10px; color:var(--warning);">تعذر قراءة هذا المجلد — تحقق من صلاحية الوصول</div>';
            });
    }

    // Folders are navigable in the picker; files are context, but they can still
    // be renamed or deleted from here (same row actions as the source tree).
    function renderPickerFiles(tree) {
        tree.querySelectorAll('.picker-file-row, .picker-more').forEach(el => el.remove());
        pickerFiles.slice(0, pickerShown).forEach(item => {
            let div = document.createElement('div');
            div.className = 'item-row picker-file-row';
            div.dataset.path = item.path;
            div.innerHTML = `<div class="item-name">${iconFile} <span></span></div><span class="file-size"></span>${rowActionsHtml(item.path, item.name, false)}`;
            div.querySelector('.item-name span').textContent = item.name;
            div.querySelector('.file-size').textContent = formatBytes(item.size);
            tree.appendChild(div);
        });
        if (pickerFiles.length > pickerShown) {
            let more = document.createElement('button');
            more.type = 'button';
            more.className = 'btn btn-outline btn-sm picker-more';
            more.style.cssText = 'display:block; margin:8px auto 0;';
            more.textContent = 'عرض المزيد (' + (pickerFiles.length - pickerShown) + ')';
            more.onclick = function() {
                pickerShown += PICKER_FILES_STEP;
                renderPickerFiles(tree);
            };
            tree.appendChild(more);
        }
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
            updateDestPath(p);
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
                let div = document.createElement('div');
                div.innerHTML = `
                    <div class="item-row">
                        ${item.is_dir ? `<button class="toggle-btn" data-toggle="1" aria-label="فتح المجلد">▶</button>` : `<span style="width:26px;display:inline-block;"></span>`}
                        <input type="checkbox" class="checkbox" value="${esc(item.path)}" ${isChecked} onchange="toggleSelect(this, ${item.is_dir})">
                        <div class="item-name" ${item.is_dir ? 'data-toggle="1"' : `onclick="this.previousElementSibling.click()"`}>
                            ${item.is_dir ? iconDir : iconFile} <span>${esc(item.name)}</span>${excludedMark}
                        </div>
                        ${item.is_dir ? '' : `<span class="file-size">${formatBytes(item.size)}</span>`}
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
            window._deleteTarget = {path: path, btn: btnEl, isDir: isDir};
            // Ignore whatever the *previous* run left in the state: the request
            // below resets it, and only then may the poll act on a result.
            window._deleteHandled = true;
            fetch('/api/delete', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: path, force: true})
            }).then(r => r.json()).then(res => {
                if (!res.success) {
                    // Refused up front (busy, protected, missing): no worker to
                    // follow, so report immediately.
                    window._deleteHandled = true;
                    showDeleteError(res, path);
                    return;
                }
                window._deleteHandled = false;
                showProgressModal();
                document.getElementById('modalTitle').innerText = isDir ? 'جاري حذف المجلد...' : 'جاري حذف الملف...';
                document.getElementById('progressText').innerText = 'حذف';
                document.getElementById('countText').innerText = 'تم حذف 0';
                document.getElementById('speedText').innerText = '';
                document.getElementById('failedText').innerText = '';
                document.getElementById('timeTakenDiv').style.display = 'none';
                document.getElementById('errorsBox').style.display = 'none';
                document.getElementById('inlineNotify').style.display = 'none';
                document.getElementById('currentFile').innerText = name;
                document.getElementById('currentFile').style.color = 'var(--text-muted)';
            }).catch(() => {
                window._deleteHandled = true;
                alert('خطأ في الاتصال بالخادم');
            });
        });
    }

    // Readable delete failure: names every blocked item with its reason instead of
    // a single cryptic line, and offers a copy button so it can be reported.
    function showDeleteError(res, path) {
        let failures = res.failures || [];
        let summary = document.getElementById('deleteErrorSummary');
        let list = document.getElementById('deleteErrorList');
        summary.textContent = (res.error || 'فشل الحذف')
            + (res.card_damage ? '\\n\\nلفحص الكارت بالكامل: sdmp3 check-card' : '');
        list.innerHTML = '';
        failures.forEach(function(f) {
            let row = document.createElement('div');
            row.style.cssText = 'background:var(--field); border:1px solid var(--border); border-radius:8px; padding:8px; word-break:break-all;';
            let nameEl = document.createElement('div');
            nameEl.style.cssText = 'color:var(--text); font-weight:500;';
            nameEl.textContent = String(f.path || '').split('/').filter(Boolean).pop() || f.path;
            let reasonEl = document.createElement('div');
            reasonEl.style.cssText = 'color:var(--danger-ink); margin-top:2px;';
            reasonEl.textContent = f.reason || '';
            let pathEl = document.createElement('div');
            pathEl.style.cssText = 'color:var(--text-muted); margin-top:2px; font-size:11px;';
            pathEl.textContent = f.path || '';
            row.appendChild(nameEl); row.appendChild(reasonEl); row.appendChild(pathEl);
            list.appendChild(row);
        });
        list.style.display = failures.length ? 'flex' : 'none';
        let lines = [res.error || 'فشل الحذف', 'path: ' + (path || '')];
        failures.forEach(function(f){ lines.push((f.path || '') + ' => ' + (f.reason || '')); });
        window._deleteErrorText = lines.join('\\n');
        let btn = document.getElementById('copyDeleteErrBtn');
        if (btn) btn.textContent = 'نسخ التفاصيل';
        document.getElementById('deleteErrorModal').style.display = 'flex';
    }

    function copyDeleteErrors() {
        let text = window._deleteErrorText || '';
        if (!text) return;
        let done = function() {
            let btn = document.getElementById('copyDeleteErrBtn');
            if (btn) { btn.textContent = 'تم النسخ ✔'; setTimeout(function(){ btn.textContent = 'نسخ التفاصيل'; }, 1800); }
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(function(){ fallbackCopyDeleteErrors(text, done); });
        } else {
            fallbackCopyDeleteErrors(text, done);
        }
    }

    function fallbackCopyDeleteErrors(text, done) {
        let ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, ta.value.length);
        try { if (document.execCommand('copy')) done(); } catch(e) {}
        document.body.removeChild(ta);
    }
    
    function toggleFolder(btn, path) {
        let row = btn.closest('.item-row');
        if (!row) return;
        let children = row.nextElementSibling;
        if (!children || !children.classList.contains('children')) return;
        let arrow = row.querySelector('.toggle-btn');
        if (children.classList.contains('open')) {
            children.classList.remove('open');
            if (arrow) arrow.innerText = '▶';
        } else {
            if (!children.innerHTML) loadSubFolders(path, children);
            children.classList.add('open');
            if (arrow) arrow.innerText = '▼';
        }
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
                resBox.innerHTML += `<div class="item-row"><input type="checkbox" class="checkbox" value="${esc(item.path)}" ${isChecked} onchange="toggleSelect(this, ${item.is_dir})">
                    <div class="item-name" style="flex-direction:column;align-items:start;gap:4px;" onclick="this.previousElementSibling.click()">
                        <div style="display:flex;gap:6px;">${item.is_dir?iconDir:iconFile}${esc(item.name)}</div>
                        <div class="ltr-input truncate-text" style="font-size:11px;color:var(--text-muted); width: 100%;">${esc(item.path)}</div>
                    </div>${rowActionsHtml(item.path, item.name, item.is_dir)}</div>`;
            });
        }).catch(() => { if (myToken === searchRequestToken) resBox.innerHTML = '<div style="padding:10px;color:var(--danger-ink);">تعذر إتمام البحث.</div>'; });
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
                sizeHtml += `<br><span style="color:var(--danger-ink); font-size:12px; margin-top:4px; display:block;">المساحة لا تكفي (عجز: -${diffFmt})</span>`;
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
            // Built with DOM nodes: a track named `01 "live".mp3` must not be able
            // to break out of an attribute or inject markup.
            pathsArray.forEach(function(path) {
                let isDir = selectedPaths.get(path);
                let itemName = path.split('/').filter(Boolean).pop() || path;

                let row = document.createElement('div');
                row.className = 'item-row';
                row.style.cssText = 'justify-content: space-between; margin-bottom: 8px; background: var(--field); padding: 12px; border: 1px solid var(--border); border-radius: 8px;';

                let left = document.createElement('div');
                left.style.cssText = 'display:flex; align-items:center; gap:10px; overflow:hidden; width:65%;';
                left.title = path;
                left.innerHTML = isDir ? iconDir : iconFile;
                let nameEl = document.createElement('div');
                nameEl.className = 'truncate-text';
                nameEl.style.cssText = 'font-size:13px; color:var(--text); width:100%; font-weight:500;';
                nameEl.textContent = itemName;
                left.appendChild(nameEl);

                let right = document.createElement('div');
                right.style.cssText = 'display:flex; align-items:center; gap:12px; flex-shrink: 0;';
                let sizeEl = document.createElement('span');
                sizeEl.id = 'size_label_' + idx;
                sizeEl.style.cssText = 'font-size:11px; color:var(--text-muted); min-width: 52px; text-align: right; direction:ltr;';
                sizeEl.textContent = '...';
                let removeBtn = document.createElement('button');
                removeBtn.className = 'btn btn-outline';
                removeBtn.style.cssText = 'padding: 6px 10px; font-size: 12px; color:var(--danger-ink); border-color:var(--danger-ink);';
                removeBtn.textContent = 'إزالة';
                removeBtn.onclick = function () { unselectFromModal(path); };
                right.appendChild(sizeEl);
                right.appendChild(removeBtn);

                row.appendChild(left);
                row.appendChild(right);
                list.appendChild(row);
                idx++;
            });
            if (excludedPaths.size > 0) {
                let head = document.createElement('div');
                head.style.cssText = 'margin:20px 0 10px; font-size:13px; color:var(--warning); font-weight:500;';
                head.textContent = 'عناصر مستثناة (' + excludedPaths.size + '):';
                list.appendChild(head);
                Array.from(excludedPaths).forEach(function(path) {
                    let itemName = path.split('/').filter(Boolean).pop() || path;
                    let row = document.createElement('div');
                    row.className = 'item-row';
                    row.style.cssText = 'justify-content: space-between; margin-bottom: 8px; background: var(--warning-soft); padding: 12px; border: 1px dashed var(--warning-line); border-radius: 8px;';
                    let nameEl = document.createElement('div');
                    nameEl.className = 'truncate-text';
                    nameEl.style.cssText = 'font-size:13px; color:var(--danger-ink); width:70%;';
                    nameEl.title = path;
                    nameEl.textContent = itemName;
                    let addBtn = document.createElement('button');
                    addBtn.className = 'btn btn-outline';
                    addBtn.style.cssText = 'padding: 6px 10px; font-size: 12px; flex-shrink: 0;';
                    addBtn.textContent = 'إرجاع';
                    addBtn.onclick = function () { reincludeFromModal(path, false); };
                    row.appendChild(nameEl);
                    row.appendChild(addBtn);
                    list.appendChild(row);
                });
            }
            document.getElementById('selectedModal').style.display = 'flex';
            
            let totalBytes = 0;
            updateSizeDisplay(totalBytes);
            // One request for the whole selection; the server walks each tree once
            // and drops nested duplicates (a child is already inside its parent).
            fetch('/api/sizes', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({paths: pathsArray, excludes: Array.from(excludedPaths)})
            }).then(function(r){ return r.json(); }).then(function(data) {
                let sizes = data.sizes || {};
                let counted = {};
                pathsArray.forEach(function(path, i) {
                    let info = sizes[path] || {bytes: 0, formatted: '0 B', owner: path};
                    let el = document.getElementById('size_label_' + i);
                    if (el) el.innerText = info.formatted;
                    if (!counted[info.owner]) { counted[info.owner] = true; totalBytes += info.bytes || 0; }
                });
                updateSizeDisplay(totalBytes);
            }).catch(function(){
                document.getElementById('totalSizeSelected').innerText = 'تعذر حساب الحجم — تحقق من الاتصال.';
            });
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
            // A refused copy (dead card, no permission, self-copy) is shown in
            // the modal banner so the whole reason stays readable, not in alert().
            if(data.error) showInlineError('تعذر بدء النسخ: ' + data.error); else showProgressModal();
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
        }).then(r=>r.json()).then(data=>{ if(data.error) showInlineError('تعذر بدء التمويه: ' + data.error); else showProgressModal(); });
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
                let estMin = Math.round(data.est_10gb / 60);
                let est = estMin >= 60 ? (estMin / 60).toFixed(1) + ' ساعة' : estMin + ' دقيقة';
                let msg = 'سرعة الكتابة على الوجهة: ' + data.speed_mbps + ' MB/s\\n\\n';
                msg += 'الوقت التقديري لنسخ 10 جيجابايت: حوالي ' + est + '.\\n\\n';
                msg += 'القياس يتم بنفس طريقة النسخ الحقيقية (' + (data.size_mb || 32) + ' ميجابايت، دفعة واحدة).';
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
                    // Say *which* volume it is (SD card / USB OTG), not just "الكارت".
                    var kindLabel = {sdcard: 'كارت SD', usb: 'USB OTG', card: 'كارت', internal: 'ذاكرة'}[sd.kind] || 'الكارت';
                    // Writable -> the volume's own hue; refusing -> warning amber,
                    // so a dead card can never look healthy.
                    if (sd.writable === true) { sdEl.textContent = 'كتابة ' + kindLabel + ': نعم'; sdEl.className = 'pill pill-kind-' + (sd.kind || 'card'); }
                    else if (sd.writable === false) { sdEl.textContent = 'كتابة ' + kindLabel + ': لا'; sdEl.className = 'pill pill-warn'; }
                    else { sdEl.textContent = 'كتابة الكارت: لا يوجد كارت'; sdEl.className = 'pill'; }
                    // Hovering explains *why* the card is not writable (EIO, lock, ...).
                    sdEl.title = sd.error || (sd.path ? sd.path : '');
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
                document.getElementById('ttsOptions').innerHTML = '<small style="color:var(--danger-ink);">حزمة edge-tts غير مثبتة (يرجى تشغيل: pip install edge-tts)</small>';
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
        copyTextToClipboard(currentJocIndexText, 'تم نسخ الفهرس بنجاح <b>' + esc((document.getElementById('jocFileName').value || '') + '.txt') + '</b> — الصقه في أي مكان.');
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
        showInlineNotify('<span>✔</span> تم إنشاء الفهرس النصي <b>' + esc(txtName) + '</b> بنجاح.'
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
                    chunk_size: 3000,
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
        showInlineNotify('<span>✔</span> تم إنشاء وتحميل MP3 الصوتي <b>' + esc(currentTtsFileName) + '</b>، وكذا الفهرس النصي <b>' + esc(txtName) + '</b>.'
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

    // Escape server text before it goes into innerHTML. Quotes matter as much as
    // angle brackets: a track called `01 "live".mp3` used to break the whole row.
    function esc(s) {
        return String(s === undefined || s === null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function showInlineNotify(html, type) {
        // The modal must be reset *before* the banner is filled in: showProgressModal()
        // hides #inlineNotify, which used to wipe the message right after setting it.
        if(!progressModalVisible) {
            showProgressModal();
            document.getElementById('modalTitle').innerText =
                (type === 'success') ? 'تمت العملية' : 'تعذر إكمال العملية';
        }
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
            el.style.color = 'var(--danger-ink)';
        }
        document.getElementById('cancelProgressBtn').style.display = 'none';
        document.getElementById('closeProgressBtn').style.display = 'block';
    }
    function showInlineDone(msg) { showInlineNotify('<span>✔</span> ' + msg, 'success'); }
    function showInlineError(msg) { showInlineNotify('<span>⚠</span> ' + esc(msg), 'error'); }

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
                // A delete has no known total (counting a tree first would double
                // the card I/O), so its bar sweeps and the count is "deleted so
                // far" instead of a fake percentage.
                let fillEl = document.getElementById('progressFill');
                if (data.action === 'delete') {
                    if (data.status === 'copying') {
                        fillEl.classList.add('indeterminate');
                        document.getElementById('progressText').innerText = 'حذف';
                        document.getElementById('countText').innerText = 'تم حذف ' + (data.copied || 0);
                    } else {
                        // Finished, stopped or failed: freeze the sweep so it
                        // cannot keep implying work that is no longer happening.
                        fillEl.classList.remove('indeterminate');
                        fillEl.style.width = (data.status === 'done') ? '100%' : '0%';
                        document.getElementById('progressText').innerText =
                            (data.status === 'done') ? '100%' : '';
                    }
                } else {
                    fillEl.classList.remove('indeterminate');
                    document.getElementById('progressText').innerText = data.progress + '%';
                    fillEl.style.width = data.progress + '%';
                    if(data.total > 0) document.getElementById('countText').innerText = data.copied + ' / ' + data.total;
                }
                // A stalled card must say so instead of pretending to work.
                // TTS never touches a card: it is network-bound, so it can be
                // slow without any volume being in trouble.
                if (data.stalled && data.action !== 'tts') {
                    document.getElementById('currentFile').innerText = (data.action === 'delete')
                        ? 'الكارت لا يستجيب — لم يُحذف أي عنصر منذ ' + data.stall_seconds + ' ثانية. ' +
                          'أخرج الكارت وأدخله جيداً. إذا لم تعُد العملية، أعد تشغيل التطبيق: sdmp3 restart'
                        : 'الكارت لا يستجيب — لم يتم نقل أي بيانات منذ ' + data.stall_seconds + ' ثانية. ' +
                          'أخرج الكارت وأدخله جيداً. إذا لم تعُد العملية، أعد تشغيل التطبيق: sdmp3 restart';
                    document.getElementById('currentFile').style.color = 'var(--danger-ink)';
                } else {
                    document.getElementById('currentFile').innerText = data.file;
                    document.getElementById('currentFile').style.color = 'var(--text-muted)';
                }
                document.getElementById('failedText').innerText = data.failed ? ('فشل: ' + data.failed) : '';

                // Live throughput + remaining time, so "slow" is a number.
                let speedEl = document.getElementById('speedText');
                if (speedEl) {
                    if (data.speed_mbps > 0) {
                        let eta = '';
                        if (data.eta_seconds > 0) {
                            let m = Math.floor(data.eta_seconds / 60);
                            let s = data.eta_seconds % 60;
                            eta = ' • متبقي ~' + (m > 0 ? m + ' د ' : '') + s + ' ث';
                        }
                        speedEl.innerText = data.speed_mbps + ' MB/s' + eta;
                    } else {
                        speedEl.innerText = '';
                    }
                }

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

                    // Delete finished (or was stopped half-way): the row may no
                    // longer exist, so refresh the list it came from and drop it
                    // from the selection.
                    if (data.action === 'delete' && (data.status === 'done' || data.status === 'cancelled')
                        && !window._deleteHandled) {
                        window._deleteHandled = true;
                        let target = window._deleteTarget || {};
                        purgePathFromSelections(target.path || '');
                        refreshNearestContainer(target.btn || document.getElementById('treeContainer'));
                        checkDiskSpace(document.getElementById('custom_dest_path').value);
                    }

                    document.getElementById('cancelProgressBtn').style.display = 'none';
                    document.getElementById('closeProgressBtn').style.display = 'block';
                    
                    if(data.status === 'error') {
                        document.getElementById('currentFile').innerText = 'خطأ: ' + data.log;
                        document.getElementById('currentFile').style.color = 'var(--danger-ink)';
                        showInlineError('فشلت العملية: ' + data.log);
                    } else if (data.status === 'done' && data.time_taken) {
                        document.getElementById('timeTakenDiv').style.display = 'block';
                        document.getElementById('timeTakenDiv').innerText = 'استغرق: ' + data.time_taken
                            + (data.speed_mbps ? ' • متوسط السرعة: ' + data.speed_mbps + ' MB/s' : '');
                    } else if (data.status === 'cancelled' && data.time_taken) {
                        document.getElementById('timeTakenDiv').style.display = 'block';
                        document.getElementById('timeTakenDiv').innerText = 'تم الإيقاف بعد: ' + data.time_taken;
                    }
                    
                    // Show why items failed (path + reason) and which source
                    // folders were unreadable, not just a list of names.
                    let box = document.getElementById('errorsBox');
                    let lines = [];
                    if (data.errors && data.errors.length > 0) {
                        lines.push('<b>عناصر فشلت:</b>');
                        data.errors.forEach(function(n){ lines.push('- ' + esc(n)); });
                    }
                    if (data.skipped && data.skipped.length > 0) {
                        lines.push('<b>تم تخطي (غير مقروء):</b>');
                        data.skipped.forEach(function(n){ lines.push('- ' + esc(n)); });
                    }
                    if (data.hint) {
                        lines.push('<b>ملاحظة:</b> ' + esc(data.hint));
                    }
                    if (lines.length) {
                        box.style.display = 'block';
                        box.innerHTML = lines.join('<br>');
                    } else {
                        box.style.display = 'none';
                    }

                    // A failed delete opens the detailed list: every blocked item
                    // with its reason, instead of one vague line.
                    if (data.status === 'error' && data.action === 'delete' && !window._deleteHandled) {
                        window._deleteHandled = true;
                        showDeleteError({
                            error: data.log,
                            failures: data.delete_failures || [],
                            card_damage: data.card_damage
                        }, (window._deleteTarget || {}).path || '');
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
            # One scandir pass gives name + type + size without extra stats.
            with os.scandir(req_path) as it:
                entries = list(it)
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                    size = 0 if is_dir else entry.stat().st_size
                except OSError:
                    continue
                items.append({'name': entry.name, 'path': entry.path,
                              'is_dir': is_dir, 'size': size})
            items.sort(key=lambda i: (not i['is_dir'], i['name'].lower()))
        except OSError:
            items = []
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
# Manually built per source folder via the "Cache Folder" button. Kept in memory
# *and* mirrored to disk (see SEARCH_CACHE_FILE below), so refreshing the page or
# restarting sdmp3 does not throw away a long indexing pass. Entries are still
# invalidated whenever the app itself changes something inside a cached path
# (rename/delete/mkdir/copy) so results never point at items that no longer exist.
FOLDER_CACHE = {}          # root_path -> {'items': [...], 'count': int, 'built_at': float}
folder_cache_lock = threading.RLock()
folder_cache_building = set()  # root paths currently being indexed

# The index lives outside the checkout so `git pull` stays clean, next to the
# CLI's own state (instance.json / sdmp3.log).
SEARCH_CACHE_FILE = os.path.join(
    os.path.expanduser('~'), '.local', 'state', 'sdmp3', 'search_cache.json')
SEARCH_CACHE_MAX_ROOTS = 32
_cache_save_timer = {'handle': None}


def _save_folder_cache():
    """Persist the search index. Never raises, never matters for correctness."""
    try:
        with folder_cache_lock:
            payload = dict(FOLDER_CACHE)
        if not payload:
            try:
                if os.path.exists(SEARCH_CACHE_FILE):
                    os.remove(SEARCH_CACHE_FILE)
            except OSError:
                pass
            return
        # Keep the newest roots only: an old card's index is worthless and a
        # 100k-entry JSON written on every change would be a real cost.
        if len(payload) > SEARCH_CACHE_MAX_ROOTS:
            newest = sorted(payload.items(),
                            key=lambda kv: kv[1].get('built_at', 0), reverse=True)
            payload = dict(newest[:SEARCH_CACHE_MAX_ROOTS])
        folder = os.path.dirname(SEARCH_CACHE_FILE)
        os.makedirs(folder, exist_ok=True)
        tmp = SEARCH_CACHE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, SEARCH_CACHE_FILE)
    except Exception as exc:
        log_event('search cache: could not be saved ({})'.format(exc))


def _schedule_cache_save(delay=2.0):
    """Debounced save: mkdir/rename/delete come in bursts."""
    try:
        handle = _cache_save_timer.get('handle')
        if handle is not None:
            handle.cancel()
        timer = threading.Timer(delay, _save_folder_cache)
        timer.daemon = True
        timer.start()
        _cache_save_timer['handle'] = timer
    except Exception:
        _save_folder_cache()


def _load_folder_cache():
    """Restore the index saved by a previous run."""
    try:
        with open(SEARCH_CACHE_FILE, encoding='utf-8') as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    restored = 0
    with folder_cache_lock:
        for root, info in payload.items():
            if not root or not isinstance(info, dict):
                continue
            items = info.get('items')
            if not isinstance(items, list) or not items:
                continue
            try:
                # A cached root that no longer exists (card pulled out, folder
                # deleted outside the app) is dropped instead of returning
                # results that cannot be opened.
                if not os.path.isdir(root):
                    continue
            except OSError:
                continue
            FOLDER_CACHE[root] = {'items': items, 'count': len(items),
                                  'built_at': float(info.get('built_at') or 0)}
            restored += 1
    if restored:
        log_event('search cache restored from disk: {} folder(s)'.format(restored))


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
        _save_folder_cache()
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
    if stale:
        _schedule_cache_save()

def prune_cache_for_path(path):
    """Remove one deleted subtree from the cached indexes instead of dropping them.

    Deleting a file used to discard the whole folder index, which is exactly what
    feels like "the search cache keeps disappearing". The deleted path is known,
    so its entries can simply be filtered out.
    """
    if not path:
        return
    p = str(path).rstrip('/')
    if not p:
        return
    changed = False
    with folder_cache_lock:
        for root in list(FOLDER_CACHE):
            info = FOLDER_CACHE[root]
            r = root.rstrip('/')
            if p == r or r.startswith(p + '/'):
                # The cached folder itself (or a parent of it) is gone.
                del FOLDER_CACHE[root]
                changed = True
                continue
            if p.startswith(r + '/'):
                items = info.get('items') or []
                kept = [it for it in items
                        if not (it.get('path') == p
                                or str(it.get('path', '')).startswith(p + '/'))]
                if len(kept) != info.get('count'):
                    FOLDER_CACHE[root] = {'items': kept, 'count': len(kept),
                                          'built_at': info.get('built_at', 0)}
                    changed = True
    if changed:
        _schedule_cache_save()


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
    _save_folder_cache()
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

@app.route('/api/sizes', methods=['POST'])
def get_items_sizes():
    """Sizes for a whole selection in one request.

    The modal used to fire one /api/size per selected item, sequentially, so a
    dozen folders meant a dozen full walks and a long row of "يحسب..".
    """
    body = request.json or {}
    paths = [p for p in body.get('paths', []) if p]
    excludes = set(p for p in body.get('excludes', []) if p)
    # A child's bytes are already inside its parent: walk each tree once.
    roots = []
    for path in sorted(paths, key=len):
        if not any(path == r or path.startswith(r.rstrip('/') + '/') for r in roots):
            roots.append(path)
    root_bytes = {}
    for root in roots:
        root_bytes[root] = get_dir_size(root, excludes) if os.path.exists(root) else 0
    sizes = {}
    for path in paths:
        owner = next(r for r in roots if path == r or path.startswith(r.rstrip('/') + '/'))
        total = root_bytes[owner]
        sizes[path] = {'bytes': total, 'formatted': format_size(total), 'owner': owner}
    return jsonify({'sizes': sizes})


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
    """Measure real sequential write speed on the destination.

    Writes 32MB with the same 4MB blocks and the same single end-of-file flush
    that the copy engine uses, so the number matches what a real copy achieves.
    (The old version called fsync after every 1MB, which measures flush latency
    and always looked slow.)
    """
    data = request.json or {}
    dest = data.get('dest', '')
    if not dest or not os.path.isdir(dest):
        return jsonify({'error': 'مسار الوجهة غير صالح'})

    problem = check_destination(dest)
    if problem: return jsonify({'error': problem})

    test_file = os.path.join(dest, '.speedtest_tmp')
    size_mb = 32
    try:
        block = b'\0' * COPY_BUFFER_SIZE
        written = 0
        target = size_mb * 1024 * 1024

        start_time = time.perf_counter()
        fd = os.open(test_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        try:
            _advise_sequential(fd)
            while written < target:
                chunk = block[:min(len(block), target - written)]
                written += os.write(fd, chunk)
            os.fsync(fd)          # one flush, exactly like a real transfer
        finally:
            os.close(fd)
        duration = max(time.perf_counter() - start_time, 1e-6)

        speed_mbps = written / duration / (1024 * 1024)
        est_seconds_10gb = (10 * 1024) / speed_mbps if speed_mbps > 0 else 0
        log_event('benchmark: {!r} {:.2f} MB/s ({}MB)'.format(dest, speed_mbps, size_mb))

        return jsonify({
            'success': True,
            'speed_mbps': round(speed_mbps, 2),
            'est_10gb': round(est_seconds_10gb),
            'size_mb': size_mb,
        })
    except Exception as e:
        log_event('benchmark ERROR: {}'.format(e))
        return jsonify({'error': str(e)})
    finally:
        try:
            if os.path.exists(test_file):
                os.remove(test_file)
        except OSError:
            pass

def delete_thread_task(target):
    """Delete one path (file or whole tree) with live progress.

    Runs in its own thread so the UI can show a bar and a running count instead
    of a frozen page: a deep tree on a card takes real time, and a card that
    stops answering must be visible as a stall, not as a dead request.
    """
    acquire_wakelock()
    try:
        set_state(file='جاري الحذف…')
        touch_progress()

        state = {'last_ui': 0.0}

        def on_progress(done, failed):
            # One heartbeat per removed entry, but only a few UI writes per
            # second (the browser polls once a second anyway).
            touch_progress()
            now = time.time()
            if now - state['last_ui'] < 0.25:
                return
            state['last_ui'] = now
            set_state(copied=done, failed=failed,
                      file='جاري الحذف… {} عنصر'.format(done + failed))

        deleted, failures = force_delete_tree(target, cancel_check=is_cancelled,
                                              progress_cb=on_progress)
        if is_cancelled():
            finish_transfer('cancelled', 'تم إيقاف الحذف — ما حُذف لا يعود.')
            return

        # Last resort: Android denies some entries to the app itself
        # (Android/data, Android/obb, read-only files). The shell user does not
        # have that limit, so one rish rm -rf finishes the job.
        if os.path.lexists(target):
            if shizuku_delete(target):
                deleted += 1
                failures = []
                log_event('delete via Shizuku ok: {!r}'.format(target))
            elif failures:
                log_event('delete blocked: {!r} ({} entries) -> {}'.format(
                    target, len(failures),
                    '; '.join('{} => {}'.format(p, r) for p, r in failures[:10])))

        if not os.path.lexists(target):
            # Prune the deleted subtree out of the instant-search index. A
            # complete delete is exact; a partial one cannot be, so it falls
            # back to dropping the affected index.
            prune_cache_for_path(target)
            set_state(copied=deleted, failed=0, delete_failures=[], card_damage=False)
            finish_transfer('done', 'تم حذف {} نهائياً.'.format(
                os.path.basename(str(target).rstrip('/')) or target),
                extra={'progress': 100, 'deleted': deleted})
            return

        invalidate_cache_for_path(target)

        # Still there: say exactly what is blocked and why, not a vague failure.
        detail = ''
        if failures:
            shown = failures[:4]
            detail = ' — ' + '، '.join(
                '{}: {}'.format(os.path.basename(str(p).rstrip('/')) or p, reason)
                for p, reason in shown)
            if len(failures) > len(shown):
                detail += ' (+{})'.format(len(failures) - len(shown))
        hint = ''
        if any('صلاحية' in str(reason) for _, reason in failures):
            hint = ' — شغّل: sdmp3 unlock أو فعّل Shizuku للحذف الإجباري'
        card_damage = looks_like_card_damage(failures, deleted)
        message = CARD_DAMAGE_HINT if card_damage else (
            'تعذر حذف {} عنصر من أصل {}{}{}'.format(
                len(failures), deleted + len(failures), detail, hint))
        set_state(status='error', log=message, failed=len(failures), copied=deleted,
                  card_damage=card_damage,
                  errors=[p for p, _ in failures],
                  delete_failures=[{'path': p, 'reason': r} for p, r in failures[:25]])
    except TransferCancelled:
        finish_transfer('cancelled', 'تم إيقاف الحذف.')
    except Exception as e:
        log_event('delete EXCEPTION: {!r} -> {}: {}'.format(target, type(e).__name__, e))
        set_state(status='error', log=str(e))
    finally:
        release_wakelock()


@app.route('/api/delete', methods=['POST'])
def delete_item():
    """Start a forced, asynchronous delete.

    `force: true` (what the UI always sends) means "remove whatever it takes":
    normal unlink, then a chmod retry, then Shizuku. Only volume roots and
    system/app paths are refused, because those would break the device rather
    than free space.
    """
    data = request.json or {}
    path = (data.get('path') or '').strip()
    force = bool(data.get('force'))
    log_event('delete request: {!r} (force={})'.format(path, force))

    if not path:
        return jsonify({'success': False, 'error': 'لم يتم تحديد عنصر'})
    if not os.path.lexists(path):
        return jsonify({'success': False, 'error': 'العنصر غير موجود'})

    reason = protected_target_reason(path)
    if reason:
        log_event('delete refused (protected): {!r} -> {}'.format(path, reason))
        return jsonify({'success': False, 'error': reason, 'protected': True})

    private_reason = android_private_reason(path)
    if private_reason and not shizuku_status().get('running'):
        log_event('delete refused (android private dir, no Shizuku): {!r}'.format(path))
        return jsonify({
            'success': False, 'protected': True,
            'error': private_reason + ' — فعّل Shizuku (sdmp3 unlock) ليُحذف بالقوة'})

    global ACTIVE_THREAD
    with transfer_lock:
        if transfer_state['status'] == 'copying' and is_thread_alive(ACTIVE_THREAD):
            return jsonify({'success': False,
                            'error': 'هناك عملية قيد التشغيل بالفعل، انتظر انتهاءها'})
        if transfer_state['status'] == 'copying':
            # The previous worker died without clearing the flag.
            log_event('recovering a stale delete state (worker thread is gone)')
        # Cleared here, before the thread starts: otherwise a poll that lands in
        # between still reads the previous run's done/error state.
        reset_state(status='copying', action='delete', start_time=time.time(),
                    file='جاري الحذف…')
        ACTIVE_THREAD = threading.Thread(target=delete_thread_task, args=(path,),
                                         daemon=True)
        ACTIVE_THREAD.start()
    return jsonify({'success': True, 'async': True, 'force': force})


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

    # One scan of the tree builds everything the index needs: the play order
    # (readdir/FAT order, deliberately not sorted) and which folders hold MP3s.
    # The old version walked the tree with os.walk and then walked it again with
    # scandir, doubling every FUSE round-trip on the card.
    tree = {}      # dir -> (mp3 names, child dirs), both in readdir order
    has_mp3 = set()
    skipped = []
    stack = [target_dir]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                children = [e for e in it if not e.name.startswith('.')]
        except OSError as exc:
            skipped.append('{} — {}'.format(current, describe_os_error(exc)))
            log_event('joc index: cannot read {!r} ({})'.format(current, describe_os_error(exc)))
            continue
        mp3_names, child_dirs = [], []
        for entry in children:
            try:
                if entry.is_dir(follow_symlinks=False):
                    child_dirs.append(entry.path)
                elif entry.is_file(follow_symlinks=False) and entry.name.lower().endswith('.mp3'):
                    mp3_names.append(entry.name)
            except OSError:
                continue
        tree[current] = (mp3_names, child_dirs)
        if mp3_names:
            has_mp3.add(current)
            parent = os.path.dirname(current)
            while parent and parent != '/' and parent not in has_mp3:
                has_mp3.add(parent)
                parent = os.path.dirname(parent)
        stack.extend(child_dirs)

    def process_dir(current_path, indent_level):
        nonlocal current_x
        if current_path not in has_mp3:
            return
        mp3_files, dirs = tree.get(current_path, ([], []))

        dir_name = clean_and_cut(os.path.basename(current_path), char_limit, strip_symbols)

        if mode == 1:
            if current_path != target_dir:
                if mp3_files:
                    output_lines.append(f"{'  ' * indent_level}{format_entry(dir_name, current_x)}")
                else:
                    output_lines.append(f"{'  ' * indent_level}{dir_name}")
            current_x += len(mp3_files)
            for d in dirs: process_dir(d, indent_level + 1)

        elif mode == 2:
            if current_path != target_dir:
                output_lines.append(f"\n{'  ' * indent_level}[{dir_name}]")
            for f in mp3_files:
                f_name = clean_and_cut(os.path.splitext(f)[0], char_limit, strip_symbols)
                output_lines.append(f"{'  ' * (indent_level + 1) if current_path != target_dir else ''}{format_entry(f_name, current_x)}")
                current_x += 1
            for d in dirs: process_dir(d, indent_level + 1)

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

    return jsonify({'success': True, 'output': '\n'.join(output_lines), 'skipped': skipped[:10]})

# ------------- TTS (Edge) Routes -------------
@app.route('/api/tts/voices')
def tts_voices():
    return jsonify({'voices': TTS_VOICES, 'default': TTS_DEFAULT_VOICE, 'available': EDGE_TTS_AVAILABLE})

def tts_thread_task(index_text, voice, rate, out_name, chunk_size, obfuscate=False):
    acquire_wakelock()
    # State (status/action/start_time) is set by the route before the thread
    # starts, so a poll can never read the previous run's result.
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

        final_path = os.path.join(TTS_CACHE_DIR, out_name)
        chunk_files = [os.path.join(TTS_CACHE_DIR, f"chunk_{i:03d}.mp3")
                       for i in range(1, total + 1)]
        workers = max(1, min(TTS_PARALLEL_CHUNKS, total))
        set_state(file=f'الصوت: {voice_label} | توليد {total} جزء ({workers} بالتوازي)...')
        touch_progress()
        finished = 0
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {
                pool.submit(synthesize_chunk_mp3, chunk, voice, rate, path): i
                for i, (chunk, path) in enumerate(zip(chunks, chunk_files), 1)
            }
            for fut in as_completed(futures):
                index = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    raise RuntimeError(f"فشل توليد الجزء {index}: {e}")
                finished += 1
                touch_progress()
                set_state(copied=finished,
                          progress=min(5 + int((finished / max(total, 1)) * 82), 88),
                          file=f'الصوت: {voice_label} | تم {finished} من {total} جزء')
                if is_cancelled():
                    raise TransferCancelled()
        finally:
            # Never wait for the remaining chunks after a cancel or an error.
            pool.shutdown(wait=False, cancel_futures=True)

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
        # Chunks are inputs only. Leaving them behind filled /data with a copy
        # of every index ever generated.
        try:
            for name in os.listdir(TTS_CACHE_DIR):
                if name.startswith('chunk_') and name.endswith('.mp3'):
                    try: os.remove(os.path.join(TTS_CACHE_DIR, name))
                    except OSError: pass
        except OSError:
            pass

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
    chunk_size = int(data.get('chunk_size', 3000))
    obf = data.get('obfuscate', False)
    if obf is True or obf is False:
        obfuscate = bool(obf)
    elif isinstance(obf, str) and obf.lower() in ('file', 'custom'):
        obfuscate = 'file'
    else:
        obfuscate = bool(obf)

    global ACTIVE_THREAD
    with transfer_lock:
        if transfer_state['status'] == 'copying' and is_thread_alive(ACTIVE_THREAD):
            return jsonify({'error': 'هناك عملية قيد التشغيل بالفعل'})
        if transfer_state['status'] == 'copying':
            log_event('recovering a stale tts state (worker thread is gone)')
        reset_state(status='copying', action='tts', start_time=time.time(),
                    file='جاري تحضير الصوت…')
        ACTIVE_THREAD = threading.Thread(
            target=tts_thread_task,
            args=(index_text, voice, rate, out_name, chunk_size, obfuscate),
            daemon=True)
        ACTIVE_THREAD.start()
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

    # A card can be mounted but dead. Check it now (with a timeout) so the user
    # gets "الكارت لا يستجيب" instead of a transfer that freezes at 0%.
    problem = check_destination(dest)
    if problem: return jsonify({'error': problem})
    if dest_inside_source(paths, dest):
        return jsonify({'error': 'لا يمكن النسخ إلى مجلد داخل المصدر: ' + dest})
    if copy_targets_source(paths, dest):
        return jsonify({'error': 'الوجهة هي مجلد المصدر نفسه — اختر مجلداً جديداً داخلها بدل اختيارها هي'})

    global ACTIVE_THREAD
    with transfer_lock:
        if transfer_state['status'] == 'copying' and is_thread_alive(ACTIVE_THREAD):
            return jsonify({'error': 'هناك عملية قيد التشغيل بالفعل'})
        if transfer_state['status'] == 'copying':
            # The previous worker died without clearing the flag; recover instead
            # of blocking every future copy with "already running" forever.
            log_event('recovering a stale copy state (worker thread is gone)')
            transfer_state['status'] = 'idle'
        # Clear the previous run here, not in the worker: otherwise a poll that
        # lands between this request and the thread start still sees the old
        # 'done'/'error' state and the UI reports the wrong result.
        reset_state(status='copying', action='copy', start_time=time.time())
        ACTIVE_THREAD = threading.Thread(
            target=copy_thread_task,
            args=(paths, dest, set(p for p in data.get('excludes', []) if p)),
            daemon=True)
        ACTIVE_THREAD.start()
    return jsonify({'success': True})

@app.route('/transfer/spoof_only', methods=['POST'])
def start_spoof_only():
    data = request.json or {}
    dest = data.get('dest', '')
    spoof_timestamp = parse_spoof_date(data.get('spoof_date'))
    if not dest or not os.path.isdir(dest): return jsonify({'error': 'مسار الوجهة غير صالح'})
    if not spoof_timestamp: return jsonify({'error': 'تاريخ التمويه غير صالح'})

    problem = check_destination(dest)
    if problem: return jsonify({'error': problem})

    global ACTIVE_THREAD
    with transfer_lock:
        if transfer_state['status'] == 'copying' and is_thread_alive(ACTIVE_THREAD):
            return jsonify({'error': 'النظام مشغول'})
        if transfer_state['status'] == 'copying':
            log_event('recovering a stale spoof state (worker thread is gone)')
            transfer_state['status'] = 'idle'
        # Same race as copy: clear the previous result before the worker starts.
        reset_state(status='copying', action='spoof', start_time=time.time())
        ACTIVE_THREAD = threading.Thread(
            target=spoof_only_thread, args=(dest, spoof_timestamp), daemon=True)
        ACTIVE_THREAD.start()
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
    # The instant-search index survives a restart; restoring it here means the
    # first search after `sdmp3 restart` is already fast.
    try:
        _load_folder_cache()
    except Exception:
        pass
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

