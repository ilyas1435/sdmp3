#!/usr/bin/env python3
import os
import shutil
import subprocess
import threading
import time
import datetime
import json
import re
import random
import shlex
import atexit
import tempfile
import asyncio
import errno
import hashlib
import heapq
import stat
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from werkzeug.exceptions import HTTPException
from flask import Flask, render_template_string, request, jsonify, send_file

# Pure sibling modules (no Flask, no globals) so they can be tested on their own:
#   sdmp3_themes.py  palette registry + contrast contract + CSS tokens
#   sdmp3_state.py   atomic settings.json / search_history.json
#   sdmp3_groups.py  copy-group paths (groups, including nested ones)
from sdmp3_themes import (THEMES, DEFAULT_THEME, theme_ids, get_theme,
                          theme_css_pairs, theme_color, catalog)
from sdmp3_state import (state_file, load_settings, save_settings,
                         load_search_history, save_search_history,
                         load_index_settings, save_index_settings,
                         load_saved_operations, save_saved_operations,
                         SAVED_OPERATIONS_LIMIT, SAVED_OPERATION_PATH_LIMIT,
                         SAVED_OPERATION_GROUP_LIMIT,
                         DEFAULT_INDEX_SETTINGS, SETTINGS_VERSION,
                         upsert_recent, prune_history_to_roots,
                         HISTORY_ITEM_LIMIT, HISTORY_QUERY_LIMIT)
from sdmp3_groups import (prepare_copy_groups, group_segments, group_dir_chain,
                          validate_group_path)
from sdmp3_fonts import (DEFAULT_FONT, DEFAULT_FONT_SIZE, FONT_SIZES,
                         font_face_css, font_ids, font_scale, font_size_ids,
                         font_stack, get_font, catalog as font_catalog)
from sdmp3_version import validate_version

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    edge_tts = None
    EDGE_TTS_AVAILABLE = False

app = Flask(__name__)

# Version is kept in the VERSION file next to this script so that the `sdmp3`
# CLI can read it without importing the app.
APP_VERSION_FALLBACK = "3.14"

# Toggled by --no-wakelock (see main() at the bottom).
WAKELOCK_ENABLED = True


def _read_app_version():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'VERSION'), encoding='utf-8') as fh:
            version = fh.read().strip() or APP_VERSION_FALLBACK
    except OSError:
        version = APP_VERSION_FALLBACK
    return validate_version(version)


# Read once when the server process starts. Reading VERSION on each request let
# an old, still-running Termux process display a new version after `sdmp3 update`
# replaced the file, even though its Python code had not been restarted.
APP_VERSION = _read_app_version()


def get_app_version():
    return APP_VERSION


# Copied in 4MB units. A single sequential write per unit is what protects the
# FAT directory-entry order that old players follow.
COPY_BUFFER_SIZE = 4 * 1024 * 1024
SENDFILE_CHUNK = 4 * 1024 * 1024
# Errors that mean "this kernel/filesystem cannot splice", not "the card failed".
# On Android FUSE sendfile is roughly twice as fast as read()+write(); when it is
# not available we continue with the normal buffered loop from the same offset.
SENDFILE_UNSUPPORTED = {
    errno.EINVAL, errno.ENOSYS, errno.EXDEV, errno.EOPNOTSUPP,
    getattr(errno, 'ENOTSUP', errno.EOPNOTSUPP), errno.EAGAIN,
}
# Flush the whole transfer to the card every 1GB.
#
# This is a *coarse* flush on purpose: it used to be an fsync after every single
# file, which cost tens of milliseconds per MP3 and made folders full of small
# tracks painfully slow. Ordering is guaranteed by writing the files one at a
# time in DFS order - never by fsync - so dropping the per-file flush changes
# speed only, not the on-disk playback order.
# os.sync() flushes *every* filesystem on the device, not just this copy, so a
# tiny threshold turns a big transfer into hundreds of short stalls. Ordering
# comes from the sequential writes themselves, never from syncing.
#
# The threshold is also the *length* of the single «تثبيت البيانات على الكارت»
# step: 1 GB of dirty pages on a card that writes at 10 MB/s is a 100-second
# blocking syscall, which users read as a hang. 256 MB caps that step at ~25s
# worst case on a slow card, and on internal storage it is still one flush per
# couple of seconds of copying - nowhere near a per-file flush.
SYNC_THRESHOLD = 256 * 1024 * 1024

# os.sync() is one long syscall that cannot report progress. Without a heartbeat
# the UI used to mistake a normal 30-60s flush for a dead card and tell the user
# to pull it out while the copy was actually fine. A tiny companion thread keeps
# the stall clock alive while the kernel flushes (up to SYNC_HEARTBEAT_LIMIT, so
# a genuinely hung sync is still reported eventually).
SYNC_HEARTBEAT_SECONDS = 3
SYNC_HEARTBEAT_LIMIT = 180

# A transfer that has moved no bytes for this long is reported as stalled.
# A pulled or dying SD card blocks the worker *inside* write() forever, so the
# worker cannot report anything itself - the clock is watched from the request
# side instead. 60s is well above the worst case for one 4MB chunk on a slow
# (Class 4) card, and above a normal global flush.
STALL_SECONDS = 60
# Before the hard stall warning, say calmly that the card is slow. This is a
# status, not an instruction to remove the card.
SLOW_SECONDS = 15
# How long to refuse a destination after a probe timed out, instead of spawning
# another worker that would hang in exactly the same place. Kept short: after the
# user re-inserts the card a retry must be able to prove it recovered without an
# app restart.
UNHEALTHY_DEST_TTL = 20
# Seconds to wait for the destination to answer a tiny write before a transfer.
# The probe no longer fsyncs (see _probe_write), so this is only consumed by an
# actually hung mount, not by a slow-but-working card's flush.
DEST_PROBE_TIMEOUT = 15

# Android's FUSE/sdcardfs can briefly answer a stat() with a stale size (often 0)
# or ENOENT for a file written seconds earlier, and a volume can be inaccessible
# for a moment right after syncfs(). A single stat immediately after the final
# flush is therefore not a reliable verdict. Verification gives a suspicious
# path one fresh look after this short pause. The pause is shared by the whole
# pass (one sleep, not one per file) and a file that is genuinely wrong still
# fails after it, so real mismatches are never suppressed.
VERIFY_RECHECK_SECONDS = 2.0

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
    'avg_mbps': 0,
    'copy_seconds': 0,
    'verify_seconds': 0,
    'eta_seconds': 0,
    'phase': '',            # planning | copying | syncing | verifying | done
    'syncing': False,
    'sync_started': 0,
    'sync_seconds': 0,
    'slow': False,
    'slow_seconds': 0,
    'stalled': False,
    'stall_seconds': 0,
    'verified': 0,
    'verify_total': 0,
    'verify_done': 0,
    'verify_progress': 0,
    'verify_failed': [],
    'verify_cancelled': False,
    'verify_mode': 'off',
    'verify_content': False,
    'verify_bytes_done': 0,
    'verify_bytes_total': 0,
    'flush_warning': '',
    # Auto-spoof runs inside the copy worker, after verification. These fields
    # keep its result in the SAME final snapshot as the copy numbers.
    'spoof_total': 0,
    'spoof_done': 0,
    'spoof_error': '',
    'spoof_cancelled': False,
    'card_mbps': 0,
    'card_warning': '',
    'last_progress_ts': 0,
    'cancel': False,
    'start_time': 0,
    'time_taken': '',
    'delete_failures': [],
    'card_damage': False,
    # TTS result: the file name, and where it went. `tts_saved_path` is set only
    # when the MP3 was written into the destination instead of the temp cache.
    'tts_name': '',
    'tts_saved_path': ''
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


def busy_worker_reason(message, stale_note='copy'):
    """Return `message` while a worker owns the card, else None.

    Every start route must call this *before* it probes the destination. A probe
    writes a tiny file, and writing into a card that the single copy worker is
    writing to is exactly the interleaving the copy engine must never allow (it
    can split FAT clusters and change playback order). It is also what made a
    running copy look like a dead card: the probe waited behind the worker's own
    dirty pages and timed out.

    A status='copying' whose thread is gone is recovered here instead of
    blocking every future attempt forever.
    """
    if transfer_state['status'] == 'copying':
        if is_thread_alive(ACTIVE_THREAD):
            return message
        log_event('recovering a stale {} state (worker thread is gone)'.format(stale_note))
        transfer_state['status'] = 'idle'
    return None


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
        # Informational only: the browser uses this to say "still running" if a
        # poll resumed after Chrome was closed. It never drives the progress
        # bar or the copy logic — the worker owns both end-to-end.
        snap['worker_alive'] = is_thread_alive(ACTIVE_THREAD)
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
        elif idle >= SLOW_SECONDS:
            # Working, just slow (a busy card, a long global flush). Do not turn
            # this into the red "pull the card out" message.
            snap['slow'] = True
            snap['slow_seconds'] = int(idle)
        if snap.get('syncing') and snap.get('sync_started'):
            snap['sync_seconds'] = int(max(time.time() - snap['sync_started'], 0))
    else:
        snap['stalled'] = False
        snap['stall_seconds'] = 0
        snap['slow'] = False
        snap['slow_seconds'] = 0
        snap['syncing'] = False
        snap['sync_seconds'] = 0
    return snap


def touch_progress():
    """Heartbeat for the stall detector (never raises, never blocks long)."""
    try:
        with transfer_lock:
            transfer_state['last_progress_ts'] = time.time()
    except Exception:
        pass

_syncfs_ref = {'resolved': False, 'fn': None}
_SYNCFS_FALLBACK_ERRNOS = frozenset(
    value for value in (
        getattr(errno, 'EINVAL', None),
        getattr(errno, 'ENOSYS', None),
        getattr(errno, 'EOPNOTSUPP', None),
        getattr(errno, 'ENOTSUP', None),
        getattr(errno, 'EPERM', None),
        getattr(errno, 'EACCES', None),
    ) if value is not None)
_SYNCFS_UNSUPPORTED_DEVICES = set()
_SYNCFS_UNSUPPORTED_LOCK = threading.Lock()


def _syncfs_fn():
    """libc's syncfs(fd), or None where it does not exist (never raises)."""
    if not _syncfs_ref['resolved']:
        _syncfs_ref['resolved'] = True
        try:
            import ctypes
            fn = ctypes.CDLL(None, use_errno=True).syncfs
            fn.argtypes = [ctypes.c_int]
            fn.restype = ctypes.c_int
            _syncfs_ref['fn'] = fn
        except Exception:
            _syncfs_ref['fn'] = None
    return _syncfs_ref['fn']


def flush_destination(dest):
    """Flush *only* the filesystem that holds `dest`.

    os.sync() flushes every mounted filesystem on the phone (/data, /sdcard,
    any other volume), which is why one big copy could stall for 30-60s inside a
    single syscall. syncfs(fd) on the destination touches exactly one filesystem.

    Returns a user-facing warning if syncfs hit a real error but the global
    fallback ran. Filesystems that report syncfs as unsupported or policy-
    restricted are cached and use the fallback without being reported as card
    failures.
    Raises when even the fallback fails; a successful global fallback is silent
    when syncfs is simply unavailable on this platform.
    """
    fn = _syncfs_fn()
    syncfs_error = None
    syncfs_stage = 'open'
    if fn is not None and dest:
        fd = -1
        try:
            fd = os.open(dest, os.O_RDONLY)
            syncfs_stage = 'fstat'
            device_id = os.fstat(fd).st_dev
            with _SYNCFS_UNSUPPORTED_LOCK:
                unsupported_for_device = device_id in _SYNCFS_UNSUPPORTED_DEVICES
            if not unsupported_for_device:
                import ctypes
                syncfs_stage = 'syncfs'
                ctypes.set_errno(0)
                result = fn(fd)
                if result == 0:
                    return ''
                error_number = ctypes.get_errno()
                syncfs_fallback_errnos = _SYNCFS_FALLBACK_ERRNOS | {
                    getattr(errno, 'ENOENT', 2)}
                if error_number in syncfs_fallback_errnos:
                    with _SYNCFS_UNSUPPORTED_LOCK:
                        _SYNCFS_UNSUPPORTED_DEVICES.add(device_id)
                    log_event('flush fallback: stage=syncfs errno={} (restricted/unsupported)'
                              .format(error_number))
                elif not error_number:
                    syncfs_error = RuntimeError(
                        'syncfs returned {} without an errno'.format(result))
                else:
                    syncfs_error = OSError(error_number, os.strerror(error_number), dest)
        except OSError as exc:
            syncfs_enoent = (
                syncfs_stage == 'syncfs'
                and exc.errno == getattr(errno, 'ENOENT', 2))
            if exc.errno in _SYNCFS_FALLBACK_ERRNOS or syncfs_enoent:
                # Android may permit copying files but deny opening the directory
                # descriptor needed by syncfs. This is a capability restriction,
                # not evidence that any data write failed. Still run os.sync().
                if syncfs_stage == 'syncfs':
                    with _SYNCFS_UNSUPPORTED_LOCK:
                        _SYNCFS_UNSUPPORTED_DEVICES.add(device_id)
                log_event('flush fallback: stage={} errno={} (restricted/unsupported)'
                          .format(syncfs_stage, exc.errno))
            else:
                syncfs_error = exc
        except Exception as exc:
            syncfs_error = RuntimeError(str(exc))
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
    try:
        os.sync()
    except OSError as fallback_error:
        detail = 'المزامنة العامة: {}'.format(describe_os_error(fallback_error))
        if syncfs_error is not None:
            detail = 'مزامنة الوجهة: {}؛ {}'.format(
                describe_os_error(syncfs_error), detail)
        raise RuntimeError('تعذر تثبيت البيانات على الوجهة: ' + detail) from fallback_error
    if syncfs_error is not None:
        diagnostic = '{}; errno={}'.format(
            syncfs_stage, getattr(syncfs_error, 'errno', None))
        log_event('flush warning: {}: {}'.format(diagnostic, syncfs_error))
        return ('تعذر تثبيت الوجهة مباشرة: {} [{}]. تمت محاولة المزامنة العامة احتياطياً، '
                'لكن لا يمكن تأكيد تثبيت بيانات الكارت.'.format(
                    describe_os_error(syncfs_error), diagnostic))
    return ''


def sync_with_heartbeat(phase_after='copying', dest=None):
    """Flush the destination while keeping the stall clock alive.

    Flushing is a single blocking syscall: it cannot report progress, and on a
    slow card it can easily take longer than the old 25s stall threshold. That
    is what produced the false "pull the card out" warning while the copy was
    perfectly healthy. A companion thread touches the heartbeat every few
    seconds; it stops after SYNC_HEARTBEAT_LIMIT so a truly hung flush is still
    visible instead of being hidden forever.

    `dest` is flushed with syncfs (one filesystem); without it os.sync() flushes
    everything, which is correct but much slower.
    """
    # Deliberately does NOT overwrite `file`: the flush is part of the run, and
    # replacing the current line with «تثبيت البيانات…» made that sentence appear
    # over and over in the dialog. The UI appends it quietly only when the flush
    # is actually slow (sync_seconds >= 3).
    set_state(syncing=True, sync_started=time.time(), phase='syncing')
    # Mark the start of the flush as progress too: the last chunk may have been
    # written a while ago (a long folder scan, a slow file close).
    touch_progress()
    stop = threading.Event()

    def beat():
        deadline = time.time() + SYNC_HEARTBEAT_LIMIT
        while not stop.wait(SYNC_HEARTBEAT_SECONDS):
            if time.time() >= deadline:
                break
            touch_progress()

    worker = threading.Thread(target=beat, daemon=True)
    worker.start()
    warning = ''
    try:
        warning = flush_destination(dest) or ''
    finally:
        stop.set()
        worker.join(timeout=2)
        set_state(syncing=False, sync_started=0, phase=phase_after)
    return warning


def is_busy():
    with transfer_lock:
        return transfer_state['status'] == 'copying'

def is_cancelled():
    with transfer_lock:
        return transfer_state['cancel']

def _format_duration(seconds):
    """Format seconds as ساعة(s):دقيقة(s):ثانية(s), dropping leading zeros."""
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append('{} ساعة'.format(hours))
    if mins:
        parts.append('{} دقيقة'.format(mins))
    if secs or not parts:
        parts.append('{} ثانية'.format(secs))
    return ' و '.join(parts)


class TransferCancelled(Exception):
    pass

def finish_transfer(final_status, msg, extra=None):
    snap = get_state_snapshot()
    elapsed = int(time.time() - snap['start_time'])
    kwargs = dict(status=final_status, file=msg, time_taken=_format_duration(elapsed))
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
    # acquire_wakelock() is a no-op under --no-wakelock. Calling the unlock here
    # anyway would release a lock another Termux session owns, and would leave
    # the phone free to sleep mid-copy. Never unlock what we did not lock.
    if not WAKELOCK_ENABLED:
        return
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


# Cached answer for the header pill: resolving the mount runs `df` and reads
# /proc/mounts, so cache it briefly.
_SD_STATUS_TTL = 60
_sd_status_cache = {'path': None, 'ts': 0.0, 'value': None}


def removable_sd():
    """First external (non-emulated) /storage mount, its path and kind.

    This deliberately does *not* write a probe file. It used to call
    check_destination() with a 5s timeout on every /api/system poll, which runs
    every two minutes while the page is open. On a busy card that flush easily
    took longer than 5s, so the poll marked the destination "not responding" in
    `_UNHEALTHY_DESTS` for two minutes - and the next copy refused to start with
    "الكارت لا يستجيب" even though browsing (and the Android file manager) worked.
    It also wrote to the card while the single-threaded copy worker was writing,
    which is exactly the interleaving the copy engine must never allow.

    Writability is `None` = not measured. The UI answers «هل الكارت يكتب؟» with a
    real user-started write test (`/api/benchmark`), never with this field.
    """
    path = None
    try:
        df_output = subprocess.check_output("df -h", shell=True, text=True, timeout=5)
        for line in df_output.splitlines():
            if '/storage/' in line and 'emulated' not in line:
                path = line.split()[-1]
                break
    except Exception:
        return {'path': None, 'writable': None, 'kind': None}
    if not path:
        return {'path': None, 'writable': None, 'kind': None}

    now = time.time()
    cached = _sd_status_cache
    if cached['path'] == path and (now - cached['ts']) < _SD_STATUS_TTL:
        return dict(cached['value'])

    value = {'path': path, 'writable': None,
             'kind': storage_kind(path) or 'card'}
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
# `short` is the one-word caption under the picker's icon tile: the full label
# and the raw path are too long for a row of three volumes.
STORAGE_KINDS = {
    'internal': {'label': 'الذاكرة الداخلية', 'short': 'الهاتف', 'icon': 'ico-internal'},
    'sdcard': {'label': 'كارت SD', 'short': 'SD', 'icon': 'ico-sd'},
    'usb': {'label': 'USB OTG', 'short': 'OTG', 'icon': 'ico-usb'},
    'card': {'label': 'كارت ذاكرة', 'short': 'كارت', 'icon': 'ico-card'},
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
            'short': STORAGE_KINDS['internal']['short'],
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
                        'short': STORAGE_KINDS[kind]['short'],
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


def random_spoof_timestamp():
    """A random local minute in 2023, matching the page's manual date default."""
    start = datetime.datetime(2023, 1, 1, 0, 0)
    end = datetime.datetime(2023, 12, 31, 23, 59)
    seconds = int((end - start).total_seconds())
    return int((start + datetime.timedelta(seconds=random.randint(0, seconds))).timestamp())


def _path_under_any(path, roots):
    """Realpath-aware 'is this path inside one of these roots?' (no symlink tricks)."""
    try:
        real = os.path.realpath(path)
    except OSError:
        real = os.path.abspath(path)
    real = real.rstrip(os.sep) or os.sep
    for root in roots:
        if not root:
            continue
        try:
            rr = os.path.realpath(root).rstrip(os.sep) or os.sep
        except OSError:
            rr = os.path.abspath(root).rstrip(os.sep) or os.sep
        if real == rr or real.startswith(rr + os.sep):
            return True
    return False


def spoof_tree(dest_path, start_ts, on_progress=None, cancelled=None, skip_roots=()):
    """Spoof a whole tree bottom-up, returning ``(done_count, error_or_None)``.

    Children are stamped before their parent so a later directory mtime write
    cannot undo a child's timestamp. Source paths, protected system paths and
    symlinks are skipped. Cancellation stops early with no error; the caller
    decides whether to report a cancelled spoof. The destination root itself is
    stamped last, as the manual spoof pass has always done.
    """
    skip = list(skip_roots or [])
    done = 0
    try:
        # Collect first (top-down so whole subtrees can be pruned), then apply
        # bottom-up. This keeps protected/source checks off every single file.
        levels = []
        for root, dirs, files in os.walk(dest_path, topdown=True, followlinks=False):
            if cancelled is not None and cancelled():
                return done, None
            keep_dirs = []
            for name in dirs:
                p = os.path.join(root, name)
                if os.path.islink(p) or protected_target_reason(p) or _path_under_any(p, skip):
                    continue
                keep_dirs.append(name)
            dirs[:] = keep_dirs
            keep_files = []
            for name in files:
                p = os.path.join(root, name)
                if os.path.islink(p) or _path_under_any(p, skip):
                    continue
                keep_files.append(name)
            levels.append((root, keep_dirs, keep_files))

        current = int(start_ts)
        for root, dirs, files in reversed(levels):
            for name in files + dirs:
                if cancelled is not None and cancelled():
                    return done, None
                p = os.path.join(root, name)
                if on_progress is not None:
                    on_progress(p)
                if apply_spoof_time(p, current):
                    current += 60
                    done += 1

        if (not os.path.islink(dest_path)
                and not _path_under_any(dest_path, skip)
                and not protected_target_reason(dest_path)):
            if cancelled is not None and cancelled():
                return done, None
            if on_progress is not None:
                on_progress(dest_path)
            if apply_spoof_time(dest_path, current):
                done += 1
        return done, None
    except Exception as exc:
        return done, str(exc)


CARD_DAMAGE_HINT = (
    'هذا المجلد تالف على مستوى الكارت (نظام ملفات FAT): أندرويد يرفض حذفه '
    'بأنه «غير فارغ» لكنه لا يستطيع قراءة محتوياته (خطأ إدخال/إخراج). '
    'ليست مشكلة صلاحيات — أصلح الكارت من الكمبيوتر: chkdsk /f في ويندوز أو '
    'fsck.vfat -a في لينكس، ثم احذف المجلد عادةً'
)

# Shown when the card refuses *writes* (errno 5 EIO / 30 read-only).
# errno.EIO on a weak card is often intermittent: suggest re-seating and
# retrying before telling the user to replace the card.
CARD_WRITE_HINT = (
    'الكارت يرفض الكتابة الآن (خطأ إدخال/إخراج). قد يكون الكارت ضعيفاً أو '
    'غير مثبت بإحكام في المساحة: أخرج الكارت، انتظر لحظة، ثم أدخله جيداً '
    'وجرب النسخ مرة ثانية. ليس مشكلة صلاحيات. إذا تكرر الخطأ بعد الإصلاح من '
    'الكمبيوتر (chkdsk X: /f في ويندوز أو fsck.vfat -a في لينكس) فالكارت '
    'متعب أو سعته غير حقيقية — استبدله'
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

# Below this, a card is not "a bit slow", it needs to be reported before the
# copy starts instead of after 40 minutes of waiting.
SLOW_CARD_MBPS = 5.0
CARD_SPEED_PROBE_MB = 8
# Only bother with the speed probe when the transfer is big enough for the
# measurement to matter (and for its 8MB to be negligible).
CARD_SPEED_PROBE_MIN_BYTES = 48 * 1024 * 1024


def _probe_speed_write(dest, size_mb, result, cancel=None):
    probe = os.path.join(dest, '.sdmp3_speed_probe')
    block = b'\0' * (4 * 1024 * 1024)
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        try:
            started = time.time()
            remaining = size_mb * 1024 * 1024
            while remaining > 0:
                if cancel is not None and cancel.is_set():
                    return
                chunk = block if remaining >= len(block) else block[:remaining]
                os.write(fd, chunk)
                remaining -= len(chunk)
            # No fsync: the real copy has none either, and an end flush measures
            # the kernel's dirty-page backlog rather than this card's write
            # speed. It is what pushed a healthy 5 MB/s card past the 25s probe
            # timeout and aborted the copy before its first file.
            elapsed = max(time.time() - started, 0.001)
        finally:
            os.close(fd)
        result.append(size_mb / elapsed)
    except Exception as exc:
        result.append(exc)
    finally:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except OSError:
            pass


def probe_card_speed(dest, size_mb=CARD_SPEED_PROBE_MB, timeout=None):
    """Write `size_mb` sequentially to `dest` and return MB/s, or None.

    Deliberately the same shape as the copy itself: one file, 4MB sequential
    writes, no per-file flush.

    Returns None when the probe could not be measured. Only a worker still stuck
    in a write after timeout is cached in `_UNHEALTHY_DESTS` (see
    `destination_is_unhealthy`); a slow result is returned as a number and only
    produces a warning in the copy worker.
    """
    if not dest or not os.path.isdir(dest):
        return None
    # A card writing at 0.5 MB/s needs 16s for 8MB; 30s leaves room for a very
    # slow card without calling it dead. A write that blocks the whole 30s is
    # past "slow": it would take hours to move a real album.
    timeout = timeout or max(30, int(size_mb / 0.5) + 10)
    result = []
    cancel = threading.Event()
    worker = threading.Thread(target=_probe_speed_write,
                              args=(dest, size_mb, result, cancel), daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        cancel.set()
        worker.join(3)
    if worker.is_alive():
        _UNHEALTHY_DESTS[dest] = time.time() + UNHEALTHY_DEST_TTL
        log_event('speed probe NOT responding (>{:.0f}s): {!r}'.format(timeout, dest))
        return None
    if cancel.is_set():
        log_event('speed probe cancelled after {:.0f}s: {!r}'.format(timeout, dest))
        return None
    if not result or not isinstance(result[0], float):
        return None
    return result[0]


def destination_is_unhealthy(dest):
    """True while `dest` is cached as not responding (probe timed out)."""
    return _UNHEALTHY_DESTS.get(dest, 0) > time.time()


def _probe_write(dest, result):
    probe = os.path.join(dest, '.sdmp3_probe')
    payload = b'sdmp3'
    try:
        fd = os.open(probe, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o666)
        try:
            os.write(fd, payload)
            # Read it back from the same descriptor. This still proves the
            # open/write path (read-only mount, EACCES, a hung FUSE daemon)
            # without an fsync: on a card busy with a copy the kernel fsync
            # blocks until *every* dirty page is flushed, which routinely takes
            # longer than the probe timeout and used to report a healthy card as
            # dead. A truly hung mount blocks the write itself.
            if os.pread(fd, len(payload), 0) != payload:
                raise OSError(errno.EIO, 'Destination probe did not read back', probe)
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
        return ('الوجهة لا تستجيب (الكارت معلّق). أخرجه وأدخله جيداً ثم أعد '
                'المحاولة؛ لا حاجة لإعادة تشغيل التطبيق.')
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


# ------------- ZIP archives (extracted while copying, never staged) -------------
#
# A selected `.zip` is unpacked straight into `<dest>/<archive name>`: the
# members are decompressed in 4MB chunks and written to the card front-to-back,
# exactly like copy_file_sequential does for a plain file. There is no temporary
# folder and no second copy, so the card is written once and playback order is
# whatever order the members were written in — hence the natural sort below.
ZIP_SUFFIX = '.zip'
# Junk that macOS/Windows add to archives. Writing it would put invisible
# entries between tracks in the folder's playback order, so it is dropped.
ZIP_JUNK_NAMES = frozenset({'.ds_store', 'thumbs.db', 'desktop.ini'})


class ZipArchiveError(RuntimeError):
    """A ZIP that cannot be extracted safely or at all (already user-facing)."""


def looks_like_zip(path):
    """Cheap suffix test. No stat: the route must not add a round-trip per path."""
    return str(path).lower().endswith(ZIP_SUFFIX)


def selected_archives(paths):
    """The selected paths that will be unpacked: a .zip suffix *and* a file.

    A folder that happens to be named `x.zip` is copied as a folder, so the
    guards must not exempt it from the self-copy checks.
    """
    return [p for p in paths if looks_like_zip(p) and os.path.isfile(p)]


def zip_folder_name(zip_path):
    """Folder name a selected archive is unpacked into: its base name."""
    base = os.path.basename(zip_path)
    core = base[:-len(ZIP_SUFFIX)] if base.lower().endswith(ZIP_SUFFIX) else base
    return core or base


def zip_member_name(info):
    """Normalise a member name: archives built on Windows use backslashes."""
    name = info.filename.replace('\\', '/').lstrip('/')
    while name.startswith('./'):
        name = name[2:]
    return name


def _zip_member_is_junk(name):
    parts = [p for p in name.split('/') if p]
    if not parts:
        return True
    if any(p.upper() == '__MACOSX' for p in parts):
        return True
    leaf = parts[-1]
    return leaf.casefold() in ZIP_JUNK_NAMES or leaf.startswith('._')


def zip_member_infos(zf):
    """Usable file members of an open archive, in JOC playback order.

    Raises ZipArchiveError for an encrypted archive *before* anything is
    written: the members would fail one by one otherwise, leaving a half-filled
    folder on the card. Directory entries are not returned — writing each file's
    parents covers them, and doing it from the sorted file list keeps the
    parents-first order.
    """
    infos = [i for i in zf.infolist() if not i.is_dir()]
    encrypted = [i.filename for i in infos if i.flag_bits & 0x1]
    if encrypted:
        raise ZipArchiveError('الأرشيف محمي بكلمة مرور ولا يمكن فك ضغطه هنا')
    members = [(zip_member_name(i), i) for i in infos
               if not _zip_member_is_junk(zip_member_name(i))]
    members.sort(key=lambda pair: natural_sort_key(pair[0]))
    return members


def zip_member_target(dest_dir, rel_name):
    """Absolute target of one member, refusing anything outside `dest_dir`.

    A zip is arbitrary input: `../../x` or an absolute path must never escape
    the folder the user chose, so the resolved path is checked against the root
    instead of trusting the archive.
    """
    parts = [p for p in rel_name.split('/') if p not in ('', '.')]
    if not parts or any(p == '..' for p in parts) or ':' in parts[0]:
        raise ZipArchiveError('مسار غير آمن داخل الأرشيف: {}'.format(rel_name))
    root = os.path.normpath(dest_dir)
    target = os.path.normpath(os.path.join(root, *parts))
    if target != root and not target.startswith(root + os.sep):
        raise ZipArchiveError('مسار غير آمن داخل الأرشيف: {}'.format(rel_name))
    return target


def copy_stream_sequential(reader, dst, cancel_check=None, on_bytes=None):
    """Write a readable stream (one ZIP member) to `dst` front-to-back.

    Same shape as copy_file_sequential — 4MB chunks, --advice sequential, and a
    half-written destination is deleted on any error so it can never pollute the
    folder's playback order. The source is a decompressor, not a file, so
    sendfile cannot be used here.
    """
    dst_fd = None
    destroy = False
    written = 0
    try:
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        _advise_sequential(dst_fd)
        while True:
            if cancel_check is not None and cancel_check():
                raise TransferCancelled()
            chunk = reader.read(COPY_BUFFER_SIZE)
            if not chunk:
                break
            view = memoryview(chunk)
            offset = 0
            while offset < len(chunk):
                try:
                    count = os.write(dst_fd, view[offset:])
                except OSError as write_exc:
                    # Weak cards return EIO for a moment and then recover;
                    # pause so run_with_retries() gets a real second chance.
                    if write_exc.errno == errno.EIO:
                        time.sleep(0.25)
                    raise
                if count <= 0:
                    time.sleep(0.25)
                    raise OSError(errno.EIO, 'Destination write made no progress', dst)
                offset += count
            written += len(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    except BaseException:
        destroy = True
        raise
    finally:
        if dst_fd is not None:
            try: os.close(dst_fd)
            except OSError: pass
        if destroy:
            try:
                if os.path.exists(dst): os.remove(dst)
            except OSError: pass
    return written


def _remove_if_empty_dir(path):
    try:
        if path and os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
    except OSError:
        pass


def extract_zip_to_dest(zip_path, dest_dir, cancel_check=None, on_progress=None,
                        on_bytes=None, retry_budget=None, on_member=None):
    """Unpack one archive straight into `dest_dir`: one write pass, no staging.

    Members are written in natural order, so the FAT directory-entry order (what
    an old MP3 player plays) is the folder order the user sees. Every member is
    decompressed and written immediately: the card is never written twice for
    the same byte, which is what makes this usable on a slow card.

    `on_progress(done, total, name)` and `on_bytes(count)` keep the UI and the
    stall heartbeat alive; `on_member(target, bytes_written)` lets the caller
    feed the verification map. Returns (file_count, bytes_written).

    Raises ZipArchiveError for a corrupt/encrypted/unsafe archive, TransferCancelled
    on cancel, OSError on card failures. A folder left empty by the failure is
    removed; a partially-filled one is kept (it is reported like a partial copy).
    """
    dest_dir = os.path.normpath(dest_dir)
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            members = zip_member_infos(zf)
            if not members:
                return 0, 0
            run_with_retries(os.makedirs, dest_dir, exist_ok=True,
                             _cancel_check=cancel_check, _budget=retry_budget)
            total = len(members)
            count = 0
            written_total = 0
            for index, (rel_name, info) in enumerate(members, 1):
                if cancel_check is not None and cancel_check():
                    raise TransferCancelled()
                target = zip_member_target(dest_dir, rel_name)
                parent = os.path.dirname(target)
                if parent:
                    run_with_retries(os.makedirs, parent, exist_ok=True,
                                     _cancel_check=cancel_check,
                                     _budget=retry_budget)
                reader = zf.open(info, 'r')
                try:
                    def write_member(reader=reader, target=target):
                        # A retry restarts the member from its first byte.
                        if reader.tell():
                            reader.seek(0)
                        return copy_stream_sequential(
                            reader, target, cancel_check=cancel_check,
                            on_bytes=on_bytes)
                    written = run_with_retries(
                        write_member, _cancel_check=cancel_check,
                        _budget=retry_budget)
                finally:
                    try: reader.close()
                    except Exception: pass
                written_total += written
                count += 1
                if on_member is not None:
                    on_member(target, written)
                if on_progress is not None:
                    try: on_progress(index, total, rel_name)
                    except Exception: pass
        log_event('zip extracted: {!r} -> {!r} ({} file(s), {})'.format(
            zip_path, dest_dir, count, format_size(written_total)))
        return count, written_total
    except TransferCancelled:
        _remove_if_empty_dir(dest_dir)
        raise
    except ZipArchiveError:
        _remove_if_empty_dir(dest_dir)
        raise
    except zipfile.BadZipFile as exc:
        _remove_if_empty_dir(dest_dir)
        raise ZipArchiveError('ملف مضغوط تالف أو غير مكتمل') from exc
    except NotImplementedError as exc:
        _remove_if_empty_dir(dest_dir)
        raise ZipArchiveError('طريقة ضغط غير مدعومة في الأرشيف') from exc
    except RuntimeError as exc:
        # zipfile raises RuntimeError for an encrypted member reached directly.
        _remove_if_empty_dir(dest_dir)
        raise ZipArchiveError(str(exc)) from exc
    except BaseException:
        _remove_if_empty_dir(dest_dir)
        raise

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

# Errors a working but weak/busy card can return for a moment. The Android file
# manager retries these and finishes; failing the file on the first one is why a
# slow card looked like a broken card. Bounded in count and in total sleep so a
# genuinely dead card still fails fast.
TRANSIENT_IO_ERRNOS = frozenset({
    errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.EACCES, errno.EIO,
})
# errno.EIO on a weak card is often intermittent: the card recovers after a
# moment. Give it more retries and a longer pause than other transient errors.
EIO_RETRIES = 6
EIO_DELAY = 0.8
TRANSIENT_IO_RETRIES = 3
TRANSIENT_IO_DELAY = 0.35
# Ceiling for the whole sleep budget of one operation (all retries together).
# EIO retries need more room than other transient errors.
TRANSIENT_IO_BUDGET = 10.0


class RetryBudget(object):
    """Shared allowance so one bad card cannot make every file retry forever."""

    def __init__(self, limit=40):
        self.remaining = int(limit)

    def take(self):
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def run_with_retries(fn, *args, _cancel_check=None, _budget=None, **kwargs):
    """Call `fn` and retry a bounded number of transient filesystem errors.

    `fn` must be safe to repeat: copy_file_sequential() deletes its half-written
    destination on failure, and os.makedirs(exist_ok=True) is idempotent.

    The caller's cancel flag is honoured between attempts, and `_budget` (a
    RetryBudget shared by one transfer) stops a dying card from paying the retry
    cost on every single file.

    errno.EIO (weak-card I/O error) gets more attempts and a longer pause than
    other transient errors, because a weak card often recovers after re-seating
    or after a brief pause.
    """
    attempt = 0
    slept = 0.0
    while True:
        try:
            return fn(*args, **kwargs)
        except OSError as exc:
            if exc.errno not in TRANSIENT_IO_ERRNOS:
                raise
            if _cancel_check is not None and _cancel_check():
                raise
            # EIO on a weak card needs more patience: more retries, longer pause.
            if exc.errno == errno.EIO:
                max_attempts = EIO_RETRIES
                base_delay = EIO_DELAY
            else:
                max_attempts = TRANSIENT_IO_RETRIES
                base_delay = TRANSIENT_IO_DELAY
            if attempt >= max_attempts:
                raise
            if _budget is not None and not _budget.take():
                raise
            attempt += 1
            # EIO uses a fixed pause per retry (weak card recovery time),
            # other transient errors use an increasing pause.
            if exc.errno == errno.EIO:
                pause = EIO_DELAY
            else:
                pause = base_delay * attempt
            pause = min(pause, max(TRANSIENT_IO_BUDGET - slept, 0.0))
            if pause <= 0:
                raise
            time.sleep(pause)
            slept += pause


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
    """Copy one file from start to finish, in order.

    Deliberately single-file and single-threaded: the FAT directory-entry order
    is what old MP3 players use to play a folder in sequence, so this must never
    be parallelised. Returns the number of bytes copied.

    Uses sendfile when the kernel/filesystem supports it (about twice as fast on
    Android FUSE) and falls back to a 4MB buffered loop otherwise. Both paths
    write front-to-back, so the on-card order is identical.

    There is no fsync per file any more - the caller flushes every SYNC_THRESHOLD
    bytes and once at the end with os.sync().
    """
    if cancel_check is not None and cancel_check():
        raise TransferCancelled()

    written_total = 0
    src_fd = None
    dst_fd = None
    use_sendfile = hasattr(os, 'sendfile')
    try:
        src_fd = os.open(src, os.O_RDONLY)
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        _advise_sequential(src_fd)
        _advise_sequential(dst_fd)
        while True:
            if cancel_check is not None and cancel_check():
                raise TransferCancelled()
            if use_sendfile:
                try:
                    sent = os.sendfile(dst_fd, src_fd, None, SENDFILE_CHUNK)
                except OSError as exc:
                    if exc.errno in SENDFILE_UNSUPPORTED:
                        # The source position may already have advanced by the
                        # bytes sendfile managed to move; buffered reading simply
                        # continues from there, so no byte is duplicated.
                        use_sendfile = False
                        continue
                    raise
                if sent == 0:
                    break
                written_total += sent
                if on_bytes is not None:
                    on_bytes(sent)
                continue

            chunk = os.read(src_fd, COPY_BUFFER_SIZE)
            if not chunk:
                break
            view = memoryview(chunk)
            offset = 0
            while offset < len(chunk):
                if cancel_check is not None and cancel_check():
                    raise TransferCancelled()
                try:
                    count = os.write(dst_fd, view[offset:])
                except OSError as write_exc:
                    # Weak cards sometimes return EIO briefly then recover.
                    # Pause before raising so run_with_retries gives the card
                    # a moment to recover instead of failing the file outright.
                    if write_exc.errno == errno.EIO:
                        time.sleep(0.25)
                    raise
                if count <= 0:
                    time.sleep(0.25)
                    raise OSError(errno.EIO, 'Destination write made no progress', dst)
                offset += count
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
                  sort) - the queue that guarantees JOC playback order. A
                  selected `.zip` becomes a ('dir', folder) task followed by
                  ('zip', folder): the worker unpacks it there, in one pass.
      skipped     source folders that could not be read at all, with a reason
                  (e.g. the damaged FAT entry that Android answers with EIO),
                  plus archives that are corrupt, encrypted or unsafe.

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

    def walk(src, dst, entry=None, expand_zip=True):
        nonlocal total_files, total_bytes
        if src in excludes:
            return
        if cancel_check is not None and cancel_check():
            raise TransferCancelled()

        name = entry.name if entry is not None else os.path.basename(src)
        dst_path = os.path.join(dst, name)
        try:
            # Do not follow links: a directory link can escape the selected tree
            # or point back to an ancestor, causing unrelated files to be copied
            # or the recursive planner to run until Python raises RecursionError.
            if (entry.is_symlink() if entry is not None else os.path.islink(src)):
                skipped.append((src, 'تم تجاهل الرابط الرمزي'))
                return
            # DirEntry.is_file()/is_dir() reuse scandir's metadata without
            # following links, so regular entries cost no extra FUSE round-trip.
            is_file = (entry.is_file(follow_symlinks=False)
                       if entry is not None else os.path.isfile(src))
            is_dir = (entry.is_dir(follow_symlinks=False)
                      if entry is not None else os.path.isdir(src))
        except OSError as exc:
            skipped.append((src, describe_os_error(exc)))
            return

        if is_file:
            if expand_zip and looks_like_zip(name):
                # A *selected* archive is unpacked into `<dest>/<name>/`, never
                # copied as a .zip. Only top-level selections expand: a zip that
                # merely sits inside a selected folder stays a file, so copying
                # a tree still reproduces that tree exactly.
                try:
                    with zipfile.ZipFile(src, 'r') as zf:
                        members = zip_member_infos(zf)
                except ZipArchiveError as exc:
                    skipped.append((src, str(exc)))
                    return
                except (zipfile.BadZipFile, NotImplementedError, OSError) as exc:
                    reason = (describe_os_error(exc) if isinstance(exc, OSError)
                              else 'ملف مضغوط تالف أو غير قابل للقراءة')
                    skipped.append((src, reason))
                    return
                folder = os.path.join(dst, zip_folder_name(name))
                tasks.append((folder, folder, 'dir'))
                tasks.append((src, folder, 'zip'))
                total_files += len(members)
                total_bytes += sum(max(info.file_size, 0) for _rel, info in members)
                return
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
                walk(child.path, dst_path, child, expand_zip=False)

    for root in sorted(paths, key=natural_sort_key):
        walk(root, dest)
    return tasks, total_files, total_bytes, skipped


def _hash_file_for_verification(path, cancel_check=None, on_bytes_progress=None):
    """Hash one file sequentially while keeping cancellation and progress live."""
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            if cancel_check is not None and cancel_check():
                raise TransferCancelled()
            chunk = fh.read(COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            touch_progress()
            if on_bytes_progress is not None:
                try:
                    on_bytes_progress(len(chunk), path)
                except Exception:
                    pass
    return digest.digest()


def verify_copied_files(planned, roots, on_progress=None, cancel_check=None,
                        source_by_dest=None, verify_content=False,
                        on_bytes_progress=None, size_only=None):
    """Verify copied-file sizes, and optionally compare source/content hashes.

    `planned` maps absolute destination paths to bytes actually written.
    Check those paths directly, not unrelated files in merged destination trees.
    `roots` is retained for compatibility with existing callers.
    Content mode reads each source and destination sequentially and compares
    SHA-256 digests. It is intentionally optional because it adds two full read
    passes; the default size-only check remains metadata-only.
    `size_only` lists paths that came out of an archive: they have no source
    file to hash, so they are verified by size and never reported as a failure
    just because content comparison cannot run for them.

    Returns (verified, failures, cancelled).
    """
    total = len(planned)
    done = 0
    verified = 0
    failures = []
    seen = set()
    cancelled = False
    last_report = 0.0
    # One bounded pause for the whole pass, spent only when a first look is
    # suspicious. A dead volume then costs one short sleep, not one per file.
    recheck_spent = [False]
    recheck_resolved = [0]

    def report(force=False):
        nonlocal last_report
        now = time.time()
        if force or now - last_report >= 0.4:
            last_report = now
            if on_progress is not None:
                try:
                    on_progress(min(done, total), total)
                except Exception:
                    pass

    def pause_for_recheck():
        if recheck_spent[0]:
            return
        recheck_spent[0] = True
        remaining = VERIFY_RECHECK_SECONDS
        if remaining <= 0:
            return
        if cancel_check is not None and cancel_check():
            raise TransferCancelled()
        time.sleep(remaining)

    def fresh_stat(path):
        """Prefer an fd-based stat: opening forces FUSE to refresh attributes."""
        fd = os.open(path, os.O_RDONLY)
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)

    def look_up(path):
        """Return (size, reason). Re-check a suspicious first look once."""
        try:
            metadata = os.stat(path, follow_symlinks=False)
            if stat.S_ISREG(metadata.st_mode):
                return metadata.st_size, None
            reason = 'الوجهة ليست ملفاً عادياً'
        except OSError as exc:
            reason = describe_os_error(exc)

        # Stale FUSE metadata or a volume that was briefly unavailable: give it
        # one fresh look before calling it a failure. Real problems persist.
        pause_for_recheck()
        try:
            metadata = fresh_stat(path)
        except OSError:
            try:
                metadata = os.stat(path, follow_symlinks=False)
            except OSError:
                return None, reason
        if not stat.S_ISREG(metadata.st_mode):
            return None, reason
        recheck_resolved[0] += 1
        return metadata.st_size, None

    def verify_one(path):
        """Return None when correct, else a user-facing reason string."""
        size, reason = look_up(path)
        if reason is not None:
            return reason
        expected = planned.get(path)
        if expected is None:
            return None
        if size != expected:
            mismatch = 'الحجم على الوجهة {} بايت والمتوقع {} بايت'.format(
                size, expected)
            # Even when an earlier file already spent the shared pause, a plain
            # re-stat here is cheap and may see attributes refreshed in the
            # meantime. A genuinely wrong size survives both.
            pause_for_recheck()
            try:
                metadata = fresh_stat(path)
                if stat.S_ISREG(metadata.st_mode) and metadata.st_size == expected:
                    size = metadata.st_size
                    recheck_resolved[0] += 1
            except OSError:
                pass
            if size != expected:
                return mismatch

        if verify_content:
            if size_only and path in size_only:
                # Unpacked from an archive: size is the honest check, there is
                # no source file to hash and no reason to call that a failure.
                return None
            source = (source_by_dest or {}).get(path)
            if not source:
                return 'تعذر تحديد المصدر لمقارنة المحتوى'
            try:
                source_digest = _hash_file_for_verification(
                    source, cancel_check, on_bytes_progress)
                dest_digest = _hash_file_for_verification(
                    path, cancel_check, on_bytes_progress)
            except TransferCancelled:
                raise
            except OSError as exc:
                return 'تعذر قراءة الملف للتحقق: {}'.format(describe_os_error(exc))
            if source_digest != dest_digest:
                return 'محتوى الملف لا يطابق المصدر (SHA-256)'
        return None

    def finish(path, reason):
        nonlocal done, verified
        seen.add(path)
        done += 1
        if reason is None:
            verified += 1
        elif len(failures) < 40:
            failures.append((path, reason))
        report()

    report(force=True)
    for path in planned:
        if cancel_check is not None and cancel_check():
            cancelled = True
            break
        touch_progress()
        try:
            finish(path, verify_one(path))
        except TransferCancelled:
            cancelled = True
            break

    if not cancelled:
        for path in planned:
            if path not in seen and len(failures) < 40:
                failures.append((path, 'الملف غير موجود على الوجهة'))
        done = total
        report(force=True)
    if recheck_resolved[0] and not cancelled:
        log_event('verify recheck resolved {} stale destination stat(s)'.format(
            recheck_resolved[0]))
    return verified, failures, cancelled


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


def copy_thread_task(paths, dest, excludes=None, groups=None, verify=True,
                     verify_content=False):
    acquire_wakelock()
    verify_content = bool(verify_content)
    verify = bool(verify or verify_content)
    verify_mode = 'content' if verify_content else ('size' if verify else 'off')
    reset_state(status='copying', action='copy', start_time=time.time(),
                phase='planning', verify_mode=verify_mode,
                verify_content=verify_content, flush_warning='')
    copied = 0
    bytes_done = 0
    bytes_since_sync = 0
    failed_details = []
    write_failed = False
    readonly_failures = 0
    eio_failures = 0
    excludes = excludes or set()
    last_ui_update = time.time()
    UI_UPDATE_INTERVAL = 0.5
    started = time.time()
    planned_files = {}
    planned_sources = {}
    flush_warnings = []
    dest = os.path.normpath(dest)
    groups = list(groups or [])
    speed_window = deque()
    speed_ema = None
    retry_budget = RetryBudget()

    def flush_copy(phase_after):
        warning = sync_with_heartbeat(phase_after, dest=dest)
        if warning:
            warning = str(warning)
            if warning not in flush_warnings:
                flush_warnings.append(warning)
                log_event('copy flush warning: {}'.format(warning))
            set_state(flush_warning='; '.join(flush_warnings))
        return warning

    try:
        # Re-validate inside the worker: the queue must never be built from an
        # unchecked name, even if the route changes later.
        checked_groups = []
        for name, gpaths in groups:
            clean, group_error = validate_group_path(name)
            if group_error:
                raise RuntimeError(group_error)
            checked_groups.append((clean, list(gpaths)))
        groups = checked_groups

        assigned_norm = {os.path.normpath(p) for _n, gpaths in groups for p in gpaths}
        ungrouped = [p for p in paths if os.path.normpath(p) not in assigned_norm]

        # A group's TOP-LEVEL folder equal to an ungrouped root's name would pull
        # that root inside the group folder and mix two unrelated trees: the
        # route refuses it, and so must the worker. An archive writes into a
        # folder named after itself, so that folder name counts too.
        archives = set(selected_archives(paths))
        ungrouped_names = set()
        for p in ungrouped:
            ungrouped_names.add(os.path.basename(os.path.normpath(p)).casefold())
            if p in archives:
                ungrouped_names.add(zip_folder_name(p).casefold())
        for name, _gpaths in groups:
            top = group_segments(name)[0]
            if top.casefold() in ungrouped_names:
                raise RuntimeError('اسم المجلد «{}» يتعارض مع عنصر غير مجمَّع بنفس الاسم'.format(top))

        # Two archives with the same base name would unpack into one folder and
        # mix two trees. Refuse before writing anything.
        group_of = {os.path.normpath(p): name
                    for name, gpaths in groups for p in gpaths}
        archive_targets = {}
        for p in archives:
            owner = group_of.get(os.path.normpath(p), '')
            chain = group_dir_chain(dest, owner) if owner else []
            base = (chain[-1] if chain else dest)
            target = os.path.join(base, zip_folder_name(p))
            key = os.path.normpath(target).casefold()
            if key in archive_targets:
                raise RuntimeError(
                    'أرشيفان بنفس الاسم سيفكّان في مجلد واحد «{}» — '
                    'أعد تسمية أحدهما'.format(os.path.basename(target)))
            archive_targets[key] = p

        # Safety net: the route already refuses this, but the worker must never
        # be able to duplicate a tree into itself either. An archive is exempt
        # from the mirror check: its target is a folder named after it, never
        # the archive file itself.
        for name, gpaths in groups:
            target = os.path.join(dest, name)
            if dest_inside_source(gpaths, target):
                raise RuntimeError('لا يمكن النسخ إلى مجلد داخل المصدر: {}'.format(target))
            plain = [p for p in gpaths if p not in archives]
            if plain and copy_targets_source(plain, target):
                raise RuntimeError('الوجهة هي مجلد المصدر نفسه — اختر مجلداً جديداً داخلها')
        if ungrouped:
            if dest_inside_source(ungrouped, dest):
                raise RuntimeError('لا يمكن النسخ إلى مجلد داخل المصدر: {}'.format(dest))
            plain_ungrouped = [p for p in ungrouped if p not in archives]
            if plain_ungrouped and copy_targets_source(plain_ungrouped, dest):
                raise RuntimeError('الوجهة هي مجلد المصدر نفسه — اختر مجلداً جديداً داخلها')

        set_state(file='جاري فحص الملفات وترتيبها بتسلسل JOC...', phase='planning')
        touch_progress()

        tasks = []
        verify_roots = []
        skipped = []
        total = 0
        needed_bytes = 0
        group_names = []

        def plan_segment(segment_paths, segment_dest):
            """Plan one destination root and append its queue in order."""
            nonlocal total, needed_bytes
            seg_tasks, seg_total, seg_bytes, seg_skipped = plan_transfer(
                segment_paths, segment_dest, excludes,
                cancel_check=is_cancelled, on_progress=touch_progress)
            total += seg_total
            needed_bytes += seg_bytes
            skipped.extend(seg_skipped)
            # A verify root is the top entry of a copied root, never the group
            # folder itself (a folder has no size to compare).
            for _src, _dst, _kind in seg_tasks:
                if os.path.dirname(_dst) == segment_dest and _dst not in verify_roots:
                    verify_roots.append(_dst)
            tasks.extend(seg_tasks)
            if is_cancelled():
                raise TransferCancelled()

        # Each group folder chain is the first directory entry of its own
        # segment (parents first), so every folder appears before the folders
        # copied inside it. A nested group "A/B" therefore yields: dir A,
        # dir A/B, then its files.
        for name, gpaths in groups:
            chain = group_dir_chain(dest, name)
            if not chain:
                continue
            target = chain[-1]
            before = len(tasks)
            plan_segment(gpaths, target)
            if len(tasks) > before:
                for offset, dir_path in enumerate(chain):
                    tasks.insert(before + offset, (dir_path, dir_path, 'dir'))
                group_names.append(name)

        # Anything not grouped keeps the old behaviour: straight into the chosen
        # destination, after the groups.
        if ungrouped:
            plan_segment(ungrouped, dest)

        touch_progress()
        set_state(total=total, total_bytes=needed_bytes)

        log_event('copy start: {} file(s), {}, dest={!r}, groups={}, skipped={}'.format(
            total, format_size(needed_bytes), dest, group_names, len(skipped)))
        if skipped:
            set_state(skipped=['{} — {}'.format(p, r) for p, r in skipped[:20]])
            for p, r in skipped[:10]:
                log_event('copy skipped (unreadable): {!r} -> {}'.format(p, r))

        # Every selected archive failed to open: say why instead of reporting a
        # successful copy of nothing.
        if not tasks and archives:
            detail = '؛ '.join('{} — {}'.format(os.path.basename(p), r)
                              for p, r in skipped[:3])
            raise RuntimeError('تعذر فك الأرشيف' + (': ' + detail if detail else ''))

        if not has_enough_space(dest, needed_bytes):
            raise RuntimeError(
                'المساحة المتوفرة غير كافية. المطلوب تقريباً: {}'.format(format_size(needed_bytes)))

        set_state(file='جاري النقل المستقر المباشر...', phase='copying')

        # One honest 8MB write before a big copy. On a dying card this is the
        # difference between "the app froze" and "this card writes at 2 MB/s".
        # Small transfers skip it: 8MB would be a noticeable share of them.
        if needed_bytes >= CARD_SPEED_PROBE_MIN_BYTES:
            set_state(phase='probing', file='جاري قياس سرعة الكتابة على الوجهة...')
            touch_progress()
            mbps = probe_card_speed(dest)
            touch_progress()
            if mbps is not None:
                set_state(card_mbps=round(mbps, 2))
                log_event('speed probe: {:.2f} MB/s ({})'.format(mbps, dest))
                if mbps < SLOW_CARD_MBPS:
                    set_state(card_warning=(
                        'سرعة الكتابة على الوجهة الآن ~{:.1f} MB/s فقط، وهذا أبطأ من '
                        'المعتاد بكثير (المتوقع 15 MB/s أو أكثر). إذا كان الكارت قديماً أو '
                        'ممتلئاً فسيستغرق النسخ وقتاً طويلاً — يمكنك إيقافه الآن بلا خسارة.'
                    ).format(mbps))
            elif destination_is_unhealthy(dest):
                # Nothing planned has been written yet, so this is the cheapest
                # possible place to stop: a card that cannot take 8MB in 30s
                # would stall on the first real file.
                raise RuntimeError(
                    'الوجهة لا تستجيب (الكارت معلّق). أخرجه وأدخله جيداً ثم أعد '
                    'المحاولة؛ لا حاجة لإعادة تشغيل التطبيق.')
            else:
                # The probe could not finish (cancelled or a transient error),
                # but the tiny destination check and the card itself answered.
                # Never abort a copy just because a speed *measurement* failed:
                # the real copy reports its own speed within seconds.
                set_state(card_warning=(
                    'تعذر قياس سرعة الكتابة مسبقاً، لكن الكارت يستجيب. '
                    'سيبدأ النسخ وقد يكون بطيئاً؛ تابع السرعة من نافذة التقدم.'
                ))
            set_state(phase='copying', file='جاري النقل المستقر المباشر...')

        # Per-archive counters for the end-of-run report, and the set of files
        # that came out of an archive: content verification cannot hash those
        # against a source file (there isn't one), so they are size-checked only.
        zip_archives = 0
        zip_files = 0
        zip_targets = set()
        current_label = ['جاري النقل المستقر المباشر...']

        def on_bytes(count):
            nonlocal bytes_done, bytes_since_sync
            bytes_done += count
            bytes_since_sync += count
            # Heartbeat per chunk: a slow-but-working card keeps the transfer
            # "alive", only a really hung one falls silent long enough to flag.
            touch_progress()
            # Also refresh the numbers. Unpacking one big archive is a single
            # queue entry, so without this the dialog would sit on one name and
            # one percentage for minutes.
            publish_progress()

        # The clock that the reported copy speed uses starts *after* planning and
        # the probe: walking 2000 source dirs is not the card's fault, and folding
        # it into the average made a fast card look slow.
        copy_started = time.time()

        def publish_progress(label=None, force=False):
            """Write the live numbers. Throttled, so chunk-level calls are cheap."""
            nonlocal last_ui_update, speed_ema
            if label is not None:
                current_label[0] = label
            now = time.time()
            if not force and now - last_ui_update < UI_UPDATE_INTERVAL:
                return
            last_ui_update = now
            # Speed over the last ~6 seconds (EMA-smoothed), not the average
            # since the first byte: the average made a healthy card look
            # slow for the whole transfer after one flush.
            speed_window.append((now, bytes_done))
            while len(speed_window) > 2 and now - speed_window[0][0] > 6:
                speed_window.popleft()
            t0, b0 = speed_window[0]
            dt = now - t0
            if dt >= 1.5:
                inst = (bytes_done - b0) / dt / (1024 * 1024)
                speed_ema = inst if speed_ema is None else (speed_ema * 0.55 + inst * 0.45)
            if speed_ema is None:
                speed = bytes_done / max(now - copy_started, 0.001) / (1024 * 1024)
            else:
                speed = speed_ema
            if needed_bytes:
                progress = int(min(bytes_done / needed_bytes, 1) * 100)
                remaining = max(needed_bytes - bytes_done, 0)
                eta = int(remaining / (speed * 1024 * 1024)) if speed > 0.05 else 0
            else:
                done_count = copied + len(failed_details)
                progress = int((done_count / total) * 100) if total else 0
                eta = 0
            set_state(copied=copied, failed=len(failed_details),
                      progress=min(progress, 99), file=current_label[0],
                      phase='copying',
                      copied_bytes=bytes_done, speed_mbps=round(speed, 2),
                      avg_mbps=round(bytes_done / max(now - copy_started, 0.001)
                                     / (1024 * 1024), 2),
                      eta_seconds=eta, sync_seconds=0)

        def copy_one_file(src, dst_path):
            # One attempt at one file. A failed attempt reports the bytes
            # it managed before dying; rewind those so a retry cannot
            # count the same bytes twice (which would inflate progress
            # and the average speed).
            nonlocal bytes_done, bytes_since_sync
            started_done = bytes_done
            started_since = bytes_since_sync
            try:
                return copy_file_sequential(
                    src, dst_path, cancel_check=is_cancelled,
                    on_bytes=on_bytes)
            except TransferCancelled:
                raise
            except Exception:
                bytes_done = started_done
                bytes_since_sync = started_since
                raise

        def record_failure(src, e, what='copy'):
            """Book one failed entry, and stop only when the card is hopeless."""
            nonlocal write_failed, readonly_failures, eio_failures
            reason = (describe_os_error(e) if isinstance(e, OSError) else str(e))
            failed_details.append((src, reason))
            log_event('{} failed: {!r} -> {}'.format(what, src, reason))
            err_no = getattr(e, 'errno', None) if isinstance(e, OSError) else None
            if err_no == errno.EROFS:
                # The mount flipped to read-only (Android does this after
                # a card I/O error). Nothing else will succeed, so stop
                # instead of burning ten minutes on guaranteed failures.
                readonly_failures += 1
                if readonly_failures >= 3:
                    if copied:
                        raise RuntimeError(
                            'الكارت تحوّل إلى القراءة فقط بعد نسخ {} '
                            'ملفاً سليماً. أخرج الكارت، انتظر ثانيتين، ثم '
                            'أدخله جيداً وأعد المحاولة — سيُكمل النسخ فوق '
                            'الموجود دون تكراره.'.format(copied))
                    raise RuntimeError(
                        'الكارت تحوّل إلى القراءة فقط قبل نسخ أي ملف. '
                        'أخرج الكارت، انتظر ثانيتين، ثم أدخله جيداً وأعد '
                        'المحاولة — لم يُنقل شيء بعد.')
            if err_no == errno.EIO:
                # A weak card often returns EIO for a moment then recovers.
                # Don't flag the card as dead on one or two files: only
                # show the write-failure hint when EIO is widespread.
                eio_failures += 1
                if eio_failures > max(3, len(failed_details) // 2):
                    write_failed = True
            elif err_no == 30:
                # Read-only mount is more serious: flag it immediately.
                write_failed = True
            if err_no == 28:
                raise RuntimeError('انتهت المساحة الخالية في الوجهة أثناء النسخ.')

        def extract_one_zip(zip_src, folder):
            """Unpack one selected archive straight into `folder` (one pass)."""
            nonlocal copied, zip_files
            archive = os.path.basename(zip_src)

            def on_member(target, written):
                nonlocal copied, zip_files
                # Registered like a copied file so verification checks the real
                # bytes that reached the card, and the report count stays honest.
                planned_files[target] = written
                zip_targets.add(target)
                copied += 1
                zip_files += 1

            def on_zip_progress(done, total_members, rel_name):
                touch_progress()
                publish_progress('{} — فك الضغط {}/{}'.format(
                    archive, done, total_members))

            count, _bytes = extract_zip_to_dest(
                zip_src, folder, cancel_check=is_cancelled,
                on_progress=on_zip_progress, on_bytes=on_bytes,
                retry_budget=retry_budget, on_member=on_member)
            return count

        for src, dst_path, kind in tasks:
            # Heartbeat per entry too: a folder full of 0-byte files moves no
            # bytes at all, and must not look like a dead card.
            touch_progress()
            if is_cancelled():
                raise TransferCancelled()
            name = os.path.basename(src)

            if kind == 'file':
                try:
                    written = run_with_retries(
                        copy_one_file, src, dst_path,
                        _cancel_check=is_cancelled,
                        _budget=retry_budget)
                    planned_files[dst_path] = written
                    planned_sources[dst_path] = src
                    copied += 1
                except TransferCancelled:
                    raise
                except Exception as e:
                    record_failure(src, e)

                # Flush the whole transfer to the card occasionally, never per file.
                if bytes_since_sync >= SYNC_THRESHOLD:
                    flush_copy('copying')
                    bytes_since_sync = 0

            elif kind == 'dir':
                try:
                    run_with_retries(os.makedirs, dst_path, exist_ok=True,
                                     _cancel_check=is_cancelled,
                                     _budget=retry_budget)
                except Exception as e:
                    reason = describe_os_error(e)
                    failed_details.append((src, reason))
                    log_event('mkdir failed: {!r} -> {}'.format(src, reason))

            elif kind == 'zip':
                try:
                    set_state(file='{} — فك الضغط...'.format(name))
                    extract_one_zip(src, dst_path)
                    zip_archives += 1
                except TransferCancelled:
                    raise
                except Exception as e:
                    # A corrupt archive must not abort the rest of the copy:
                    # it is reported as data, exactly like a failed file.
                    record_failure(src, e, what='zip extract')
                    _remove_if_empty_dir(dst_path)

                # Decompression also dirties the page cache: same threshold.
                if bytes_since_sync >= SYNC_THRESHOLD:
                    flush_copy('copying')
                    bytes_since_sync = 0

            publish_progress(name)

        # A single flush at the end (plus the 1GB checkpoints above). The
        # heartbeat keeps a slow flush from looking like a hung card.
        flush_copy('verifying' if verify else 'copying')

        # Copy time and verify time are reported separately from now on: verify
        # walks the whole destination tree (~2ms/file), and folding it into the
        # copy number made a healthy card look slower than it is.
        copy_seconds = max(time.time() - copy_started, 0.001)
        avg_speed = bytes_done / copy_seconds / (1024 * 1024)

        verify_cancelled = False
        verify_failures = []
        verified = 0
        verify_seconds = 0.0
        verify_done_count = 0
        # Archive members are size-checked only, so they must not inflate the
        # content-comparison byte total (it would never reach 100%).
        verify_content_bytes = sum(
            value for path, value in planned_files.items()
            if path not in zip_targets)
        verify_bytes_total = (verify_content_bytes * 2
                              if verify_content else 0)
        verify_bytes_done = 0
        if verify and planned_files:
            verify_label = ('جاري قراءة المحتوى ومقارنته بالمصدر...'
                            if verify_content else 'جاري التحقق من الحجم الإجمالي...')
            set_state(phase='verifying', file=verify_label,
                      verify_total=len(planned_files), verify_done=0, verify_progress=0,
                      verify_bytes_done=0, verify_bytes_total=verify_bytes_total,
                      speed_mbps=0, avg_mbps=round(avg_speed, 2), eta_seconds=0)
            touch_progress()
            verify_started = time.time()

            def on_verify(done, total_files):
                nonlocal verify_done_count
                verify_done_count = done
                touch_progress()
                if verify_content and verify_bytes_total:
                    pct = int((verify_bytes_done / verify_bytes_total) * 100)
                else:
                    pct = int((done / total_files) * 100) if total_files else 99
                if done >= total_files and not verify_cancelled:
                    pct = 100
                set_state(verify_done=done, verify_total=total_files,
                          verify_progress=min(pct, 100), file=verify_label)

            last_verify_bytes_update = [0.0]

            def on_verify_bytes(count, path):
                nonlocal verify_bytes_done
                verify_bytes_done += count
                touch_progress()
                now = time.time()
                if (now - last_verify_bytes_update[0] < UI_UPDATE_INTERVAL
                        and verify_bytes_done < verify_bytes_total):
                    return
                last_verify_bytes_update[0] = now
                pct = (int(verify_bytes_done / verify_bytes_total * 100)
                       if verify_bytes_total else 0)
                set_state(verify_bytes_done=verify_bytes_done,
                          verify_progress=min(pct, 99),
                          file='جاري التحقق من المحتوى: {}'.format(
                              os.path.basename(path)))

            verified, verify_failures, verify_cancelled = verify_copied_files(
                planned_files, verify_roots,
                on_progress=on_verify, cancel_check=is_cancelled,
                source_by_dest=planned_sources,
                verify_content=verify_content,
                on_bytes_progress=on_verify_bytes if verify_content else None,
                size_only=zip_targets if verify_content else None)
            verify_seconds = max(time.time() - verify_started, 0.0)
            for p, r in verify_failures[:10]:
                log_event('verify failed: {!r} -> {}'.format(p, r))
            log_event('verify done: {}/{} ok, {} failed, cancelled={}'.format(
                verified, len(planned_files), len(verify_failures), verify_cancelled))

        # Auto-spoof is a setting, not a UI action: it must run here, in the
        # copy worker, so closing Chrome mid-copy cannot skip it. It never
        # resets state or starts a second thread - the copy numbers above stay
        # in this same final snapshot.
        spoof_total = 0
        spoof_done = 0
        spoof_error = ''
        spoof_cancelled = False
        if APP_SETTINGS.get('auto_spoof') and not is_cancelled():
            try:
                set_state(phase='spoofing', file='جاري تمويه تواريخ الوجهة...',
                          spoof_total=0, spoof_done=0, spoof_error='', spoof_cancelled=False)
                touch_progress()
                spoof_total = count_files(
                    [dest], cancel_check=is_cancelled, count_dirs=True,
                    on_progress=touch_progress)
                set_state(spoof_total=spoof_total)

                seen_spoof = {'n': 0}

                def on_auto_spoof(p=None):
                    touch_progress()
                    seen_spoof['n'] += 1
                    if seen_spoof['n'] % 50 == 0:
                        set_state(spoof_done=seen_spoof['n'], progress=99,
                                  file=os.path.basename(p or ''))

                spoof_done, spoof_error = spoof_tree(
                    dest, random_spoof_timestamp(), on_progress=on_auto_spoof,
                    cancelled=is_cancelled, skip_roots=paths)
                # spoof_tree() reports success as None; the state contract (and
                # the JS that renders it) uses the empty string for "no error".
                spoof_error = spoof_error or ''
                spoof_cancelled = is_cancelled()
                log_event('auto spoof done: {}/{} error={!r} cancelled={}'.format(
                    spoof_done, spoof_total, spoof_error, spoof_cancelled))
            except Exception as exc:
                spoof_error = str(exc)
                log_event('auto spoof ERROR: {}: {}'.format(type(exc).__name__, exc))

        if len(group_names) == 1:
            group_note = ' داخل «{}»'.format(group_names[0])
        elif len(group_names) == 2:
            group_note = ' داخل مجلدين'
        elif group_names:
            group_note = ' داخل {} مجلدات'.format(len(group_names))
        else:
            group_note = ''

        # Honest numbers, in the user's terms: copy speed (the only part the
        # card controls), how long the copy took, and the verify time.
        def _dur(seconds):
            return _format_duration(seconds)

        if bytes_done:
            speed_note = ' — {:.1f} MB/s في {}'.format(avg_speed, _dur(copy_seconds))
        else:
            speed_note = ''
        verify_note = ''
        if verify and planned_files:
            verify_note = ' (التحقق {})'.format(_dur(verify_seconds))

        if failed_details:
            final_msg = 'اكتمل النسخ{} مع فشل {} عنصر - راجع التفاصيل{}{}'.format(
                group_note, len(failed_details), speed_note, verify_note)
        elif verify and planned_files:
            if verify_cancelled:
                final_msg = 'اكتمل النسخ{} — تم إيقاف التحقق قبل نهايته{}{}.'.format(
                    group_note, speed_note, verify_note)
            elif verify_failures:
                if verify_content:
                    final_msg = ('اكتمل النسخ{} لكن مقارنة محتوى الملفات وجدت {} ملفاً '
                                 'مخالفاً — راجع التفاصيل{}{}').format(
                                     group_note, len(verify_failures),
                                     speed_note, verify_note)
                else:
                    final_msg = ('اكتمل النسخ{} لكن التحقق وجد {} ملفاً مخالفاً — راجع التفاصيل{}{}'
                                 .format(group_note, len(verify_failures),
                                         speed_note, verify_note))
            else:
                check_name = 'محتوى الملفات' if verify_content else 'الأحجام'
                final_msg = 'اكتمل النسخ{} والتحقق من {} بنجاح — {} ملفاً سليماً{}{}.'.format(
                    group_note, check_name, verified, speed_note, verify_note)
        else:
            final_msg = 'اكتمل النسخ{} بنجاح{}.'.format(group_note, speed_note)

        flush_warning = '; '.join(flush_warnings)
        if flush_warning:
            final_msg += ' • تحذير تثبيت البيانات: ' + flush_warning

        if APP_SETTINGS.get('auto_spoof'):
            if spoof_cancelled:
                final_msg += ' • تم إيقاف تمويه التواريخ'
            elif spoof_error:
                final_msg += ' • تعذر تمويه التواريخ'
            else:
                final_msg += ' • تم تمويه {} عنصراً'.format(spoof_done)

        log_event('copy done: copied={} failed={} bytes={} in {:.1f}s ({:.2f} MB/s) '
                  'verify_mode={} verify={:.1f}s verified={} verify_failed={}'.format(
                      copied, len(failed_details), bytes_done, copy_seconds, avg_speed,
                       verify_mode, verify_seconds, verified, len(verify_failures)))

        invalidate_cache_for_path(dest)
        finish_transfer('done', final_msg, extra={
            'progress': 100,
            'phase': 'done',
            'copied': copied,
            'failed': len(failed_details),
            'errors': ['{} — {}'.format(os.path.basename(p), r) for p, r in failed_details[:30]],
            'skipped': ['{} — {}'.format(p, r) for p, r in skipped[:20]],
            'copied_bytes': bytes_done,
            'speed_mbps': round(avg_speed, 2),
            'avg_mbps': round(avg_speed, 2),
            'copy_seconds': round(copy_seconds, 1),
            'verify_seconds': round(verify_seconds, 1),
            'eta_seconds': 0,
            'verified': verified,
            'verify_total': len(planned_files) if verify else 0,
            'verify_done': verify_done_count if verify else 0,
            'verify_progress': (get_state_snapshot().get('verify_progress', 0)
                                if verify_cancelled else
                                (100 if verify and planned_files else 0)),
            'verify_failed': ['{} — {}'.format(p, r) for p, r in verify_failures[:30]],
            'verify_cancelled': verify_cancelled,
            'verify_mode': verify_mode,
            'verify_content': verify_content,
            'verify_bytes_done': verify_bytes_done,
            'verify_bytes_total': verify_bytes_total,
            'flush_warning': flush_warning,
            'zip_archives': zip_archives,
            'zip_files': zip_files,
            'spoof_total': spoof_total,
            'spoof_done': spoof_done,
            'spoof_error': spoof_error,
            'spoof_cancelled': spoof_cancelled,
            'hint': CARD_WRITE_HINT if write_failed else '',
        })

    except TransferCancelled:
        try:
            flush_copy('copying')
        except Exception as flush_error:
            # Cancellation still needs a terminal state even if the best-effort
            # durability flush fails. Do not leave the UI showing a live copy.
            log_event('copy cancellation flush failed: {}: {}'.format(
                type(flush_error).__name__, flush_error))
            finish_transfer(
                'error',
                'تم إلغاء النسخ، لكن تعذر تثبيت البيانات على الوجهة: {}'.format(
                    describe_os_error(flush_error)),
                extra={'log': str(flush_error), 'phase': 'error',
                       'flush_warning': '; '.join(flush_warnings)})
            return
        log_event('copy cancelled by user after {}s'.format(int(time.time() - started)))
        cancel_msg = 'تم إلغاء النسخ.'
        if flush_warnings:
            cancel_msg += ' تحذير تثبيت البيانات: ' + '; '.join(flush_warnings)
        finish_transfer('cancelled', cancel_msg,
                        extra={'flush_warning': '; '.join(flush_warnings)})
    except Exception as e:
        flush_error = None
        try:
            flush_copy('copying')
        except Exception as exc:
            flush_error = exc
        log_event('copy ERROR: {}: {}'.format(type(e).__name__, e))
        if flush_error is not None:
            log_event('copy error flush failed: {}: {}'.format(
                type(flush_error).__name__, flush_error))
        error_text = str(e)
        if flush_error is not None:
            error_text += ' (flush also failed: {})'.format(flush_error)
        finish_transfer('error', 'تعذر إكمال النسخ: {}'.format(
            describe_os_error(e)), extra={'log': error_text, 'phase': 'error',
                                          'flush_warning': '; '.join(flush_warnings)})
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

        # spoof_tree owns the bottom-up walk; this callback only keeps the UI
        # heartbeat and percentage alive during a large pass.
        seen = {'n': 0}

        def on_spoof(p=None):
            touch_progress()
            seen['n'] += 1
            if seen['n'] % 50 == 0:
                snap_total = get_state_snapshot()['total']
                progress = int((seen['n'] / max(snap_total, 1)) * 100)
                set_state(copied=seen['n'], progress=min(progress, 99),
                          file=os.path.basename(p or ''))

        copied, spoof_error = spoof_tree(dest_path, start_timestamp,
                                         on_progress=on_spoof, cancelled=is_cancelled)
        if spoof_error:
            raise RuntimeError(spoof_error)
        if is_cancelled(): raise TransferCancelled()
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
# The rate presets the UI offers; also the whitelist used when saving settings.
TTS_RATES = ('+0%', '-10%', '-20%', '+10%', '+20%')
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
    # The concat list lives in the temp cache; make sure it exists even when
    # this is called outside a TTS run (tests, a future merge endpoint).
    setup_tts_cache()
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
<html dir="rtl" lang="ar" data-theme="{{ active_theme.id }}" style="--font-scale: {{ font_scale }};">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SD Card Coffee</title>
    <meta name="theme-color" content="{{ theme_bg }}">
    <style>
        /* Self-hosted Arabic fonts (static/fonts/). They are declared here but
           only downloaded when the selected --app-font actually uses one, so the
           default `system` face still paints with zero font requests. */
{{ font_css|safe }}
        :root {
            /* Theme tokens. The palette lives in sdmp3_themes.py (a registry with
               a WCAG contrast contract) and is rendered here from the theme saved
               in settings.json, so the very first paint already has the user's
               theme - no flash of the default. Never hard-code a colour below:
               add it to the registry instead. */
{% for name, value in theme_css %}
            {{ name }}: {{ value }};
{% endfor %}
            /* Typography: the family comes from sdmp3_fonts.py, the scale from
               the size preset. Both are rendered server-side so the first paint
               is already correct. */
            --app-font: {{ app_font|safe }};
            /* Surfaces use the theme's own RGB channels, so the saved opacity
               can fade a card without inventing a colour outside the registry.
               1 means the old opaque look; the settings slider lowers it. */
            --surface-alpha: 1;
            --surface-card: rgba(var(--card-rgb), var(--surface-alpha));
            --surface-card-2: rgba(var(--card-2-rgb), var(--surface-alpha));
            --surface-field: rgba(var(--field-rgb), var(--surface-alpha));
            --surface-bar: rgba(var(--bar-rgb), var(--surface-alpha));
        }
        * { box-sizing: border-box; font-family: var(--app-font); -webkit-tap-highlight-color: transparent; }
        body { position:relative; background-color: var(--bg); color: var(--text); margin: 0; padding: 16px 14px 112px; font-size: calc(14px * var(--font-scale)); line-height: 1.55; -webkit-font-smoothing: antialiased; }
        /* A fixed background-attachment makes Android repaint the whole page on
           every scroll and reads as a jitter. A fixed, non-interactive layer
           behind the content gives the same look and scrolls independently. */
        body.photo-background-enabled::before {
            content:""; position:fixed; inset:0; z-index:-1; pointer-events:none; will-change:transform;
            background-image: linear-gradient(var(--photo-overlay), var(--photo-overlay)), var(--photo-image);
            background-position:center center, center center; background-repeat:no-repeat, no-repeat;
            background-size:cover, cover;
        }
        .container { max-width: 900px; margin: 0 auto; }

        /* ---- header ---------------------------------------------------- */
        /* Compact: the app name and one quiet status line. The status items are
           text separated by hairlines, with a tiny coloured dot carrying the
           state - a row of filled chips would out-shout the actual content. */
        .app-header { display:flex; align-items:center; gap:11px; padding: 4px 0 12px; border-bottom:1px solid var(--rule); }
        .app-header .badge { width:36px; height:36px; border-radius:11px; background:var(--accent-soft); border:1px solid var(--accent-line); display:flex; align-items:center; justify-content:center; flex-shrink:0; }
        .app-header .badge svg { stroke: var(--accent); width:19px; height:19px; }
        .app-header .header-text { min-width:0; }
        .app-header h1 { font-size: calc(17px * var(--font-scale)); color: var(--text); margin:0; font-weight: 700; letter-spacing: -0.1px; line-height:1.3; }
        .app-header .header-settings { margin-inline-start: auto; width:34px; height:34px; flex-shrink:0; }
        .pill-row { display:flex; flex-wrap:wrap; align-items:center; row-gap:4px; margin-top:7px; }
        .app-header .pill { display:inline-flex; align-items:center; gap:6px; padding:0; border:0; background:none; border-radius:0; color:var(--text-muted); font-size:calc(11px * var(--font-scale)); line-height:1.5; white-space:nowrap; }
        /* The header carries the version only. State that can be wrong (Shizuku,
           «كتابة الكارت») lives in الإعدادات › عام, so no header dot is left to
           colour — the card answer is a real write test now. */
        .app-header #appVersionPill { color: var(--accent-text); font-weight:600; }
        /* Pills used outside the header (picker badge, group chip) stay tinted by
           volume type; text uses the -ink variant so 11px labels stay legible. */
        .pill { font-size: calc(11px * var(--font-scale)); line-height: 1; padding: 6px 10px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-muted); background: var(--pill-bg); white-space: nowrap; }
        .pill-ok { color: var(--success); border-color: var(--ok-line); background: var(--ok-soft); }
        .pill-warn { color: var(--warning); border-color: var(--warning-line); background: var(--warning-soft); }
        .pill-kind-internal { color: var(--kind-internal-ink); border-color: var(--kind-internal-line); background: var(--kind-internal-soft); }
        .pill-kind-sdcard { color: var(--kind-sdcard-ink); border-color: var(--kind-sdcard-line); background: var(--kind-sdcard-soft); }
        .pill-kind-usb { color: var(--kind-usb-ink); border-color: var(--kind-usb-line); background: var(--kind-usb-soft); }
        .pill-kind-card { color: var(--kind-card-ink); border-color: var(--kind-card-line); background: var(--kind-card-soft); }

        /* ---- sections -------------------------------------------------- */
        /* One document separated by hairlines instead of five identical boxes:
           visual weight follows the task, and the route comes first. */
        .section { padding: 20px 0 22px; }
        .section + .section { border-top: 1px solid var(--rule); }
        .section-head { display:flex; align-items:center; gap:9px; margin-bottom:14px; }
        .section-head > .icon { width:16px; height:16px; color:var(--accent); flex-shrink:0; }
        .section-title { margin:0; font-size:calc(13px * var(--font-scale)); font-weight:700; color:var(--text); letter-spacing:.2px; }
        .section-kicker { display:inline-flex; align-items:center; gap:5px; margin-inline-start:auto; font-size:calc(10.5px * var(--font-scale)); color:var(--text-muted); letter-spacing:.04em; }
        .inline-note { font-size:calc(11.5px * var(--font-scale)); color:var(--text-muted); }
        @media (max-width: 480px) { .section { padding: 17px 0 19px; } body { padding: 12px 12px 116px; } }

        /* Route: one row — source tiles, a small arrow, destination tiles, all
           pulled to the middle so the two groups read as one pair instead of two
           corners of a wide bar. The row wraps (it never scrolls): `overflow`
           on this container clipped the `.storage-fresh` badge, which sits at
           `top:-7px` on the source tile. */
        .route-row { display:flex; align-items:center; justify-content:center;
            justify-content:safe center; gap:12px; flex-wrap:wrap; row-gap:9px; }
        .route-row .storage-grid { margin-bottom:0; flex:0 1 auto; justify-content:center; }
        .route-arrow { flex:0 0 auto; display:flex; align-items:center; justify-content:center; color:var(--text-muted); }
        .route-arrow .icon { width:15px; height:15px; }
        /* The chosen folder: deliberately quiet. It belongs to the destination
           tiles above it, so it sits on their side, reads like plain text until
           tapped, and never competes with the tiles or the copy button. */
        .dest-line { display:flex; justify-content:center; margin-top:8px; }
        /* The destination is one quiet path row: no «تحديد» button, no editable
           box. Tapping anywhere on it opens the same folder picker. */
        .dest-path { display:flex; align-items:center; gap:7px; max-width:min(520px, 100%);
            min-width:0; padding:5px 10px; border-radius:9px; cursor:pointer;
            border:1px solid transparent; color:var(--text-muted); }
        .dest-path:hover, .dest-path:focus-visible { background:var(--surface-field); color:var(--text); }
        .dest-path .icon { width:14px; height:14px; flex-shrink:0; color:var(--accent); }
        .dest-path .dest-path-go { color:var(--text-muted); }
        #custom_dest_path { flex:1 1 auto; width:auto; min-width:0; min-height:0;
            margin:0; padding:0; border:0; background:transparent;
            font-size:calc(11.5px * var(--font-scale)); color:inherit;
            pointer-events:none; text-overflow:ellipsis; }
        #custom_dest_path::placeholder { color:var(--text-muted); opacity:.75; }
        /* Occasional actions, side by side, in the Tools section. */
        .tools-actions { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
        .tools-actions .btn { flex:1 1 0; min-width:150px; }
        .tools-note { display:block; margin:-2px 0 12px; }
        @media (max-width: 480px) {
            .route-row { gap:9px; }
            .dest-path { padding:5px 7px; }
        }
        @media (max-width: 380px) {
            .route-row { gap:7px; }
            .route-row .storage-grid { gap:4px; }
            .storage-card { padding:4px 5px; }
            .storage-card .storage-ico { width:27px; height:27px; padding:4px; }
        }

        /* The file browser is one attached block: the sort row, the search row
           and the optional folder-scope panel on top, the scrollable tree
           beneath. Search sits directly above the results. */
        .browser-head { border:1px solid var(--field-line); border-bottom:0; border-radius:10px 10px 0 0;
            background:var(--surface-field); }
        .browser-sort { display:flex; align-items:center; gap:8px; padding:6px 11px;
            font-size:calc(12px * var(--font-scale)); color:var(--text-muted);
            border-bottom:1px solid var(--field-line); }
        /* Input + two icon buttons on one line. The magnifier button is gone:
           Enter runs the search, so a third control was only stealing width
           from the field. The two neighbours are 36px — big enough to hit. */
        .browser-search { display:flex; align-items:center; gap:6px; flex-wrap:nowrap; padding:7px 9px; }
        .browser-search > input { flex:1 1 auto; min-width:0; }
        .browser-search > .icon-btn { width:36px; height:36px; }
        .browser-search > .icon-btn svg { width:19px; height:19px; }
        .scope-line { display:flex; align-items:center; gap:7px; flex-wrap:wrap; }
        .scope-line > .icon { width:15px; height:15px; color:var(--accent); flex-shrink:0; }
        /* The scope path is a field like the destination's, so both pickers read
           the same: a locked path on one line, «تحديد» underneath. */
        #searchScopePath { flex:1 1 auto; min-width:0; margin-bottom:0; background:var(--surface-field);
            font-family:var(--num-font); color:var(--text); }
        @media (max-width: 480px) { .browser-search { padding:6px 8px; gap:5px; }
            .browser-search > .icon-btn { width:34px; height:34px; }
            .browser-search > .icon-btn svg { width:18px; height:18px; } }

        /* Monospace digits: counts and speeds must not jitter between polls. */
        .num { font-family: var(--num-font); font-variant-numeric: tabular-nums; direction:ltr; unicode-bidi:isolate; }

        label { font-weight: 500; margin-bottom: 8px; display: block; color: var(--text-muted); font-size: calc(13px * var(--font-scale)); }
        select, input[type="text"], input[type="number"], input[type="datetime-local"] { width: 100%; padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; font-size: calc(14px * var(--font-scale)); background-color: var(--surface-field); color: var(--text); outline: none; margin-bottom: 12px; transition: border-color 0.15s, box-shadow 0.15s; min-height: 46px; }
        select:focus, input[type="text"]:focus, input[type="number"]:focus, input[type="datetime-local"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
        textarea { width: 100%; padding: 12px 14px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 10px; font-family: var(--num-font); font-size: calc(14px * var(--font-scale)); line-height: 1.7; background-color: var(--surface-field); color: var(--text); outline: none; resize: none; }
        textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

        .btn { padding: 11px 16px; min-height: 44px; border: none; border-radius: 10px; cursor: pointer; font-weight: 600; font-size: calc(13.5px * var(--font-scale)); color: var(--text); transition: transform 0.08s ease, filter 0.15s ease, background-color 0.15s ease, border-color 0.15s ease; display: inline-flex; align-items: center; justify-content: center; gap: 7px; }
        .btn:active { transform: scale(0.97); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
        .btn:focus-visible, .icon-btn:focus-visible, .storage-card:focus-visible, .toggle-btn:focus-visible {
            outline: 2px solid var(--accent); outline-offset: 2px; }
        .btn-primary { background: var(--accent-fill); color: var(--on-accent); }
        .btn-primary:hover { background: var(--accent-fill-hover); }
        .btn-success { background: var(--success-fill); color: var(--on-fill); }
        .btn-success:hover { filter: brightness(1.08); }
        .btn-danger { background: var(--danger-fill); color: var(--on-fill); }
        .btn-danger:hover { filter: brightness(1.08); }
        .btn-sand { background: var(--sand-fill); color: var(--on-fill); }
        .btn-sand:hover { background: var(--sand-fill-hover); }
        .btn-outline { background: var(--surface-card-2); border: 1px solid var(--border); color: var(--text); }
        .btn-outline.is-open { background: var(--accent-soft); border-color: var(--accent); color: var(--accent-text); }
        .btn-outline:hover { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-soft); }
        .btn-sm { padding: 8px 12px; min-height: 38px; font-size: calc(12.5px * var(--font-scale)); }
        /* The index is the one tool that matters, so it wears the accent — as a
           soft tint, because the filled button belongs to «بدء النسخ». */
        .btn-joc { background: var(--accent-soft); border: 1px solid var(--accent); color: var(--accent-text); }
        .btn-joc .icon { color: var(--accent); }
        .btn-joc:hover { background: var(--accent-fill); border-color: var(--accent-fill); color: var(--on-accent); }
        .btn-joc:hover .icon { color: var(--on-accent); }

        /* End-of-run report: one row per fact, each with its own icon. A finished
           copy used to be one long line of numbers; half of them repeated the
           progress bar, and none of them was scannable. */
        .report { display:flex; flex-direction:column; gap:0; margin-top:14px;
            border:1px solid var(--field-line); border-radius:10px; overflow:hidden;
            background:var(--surface-field); text-align:start; }
        .report-row { display:flex; align-items:center; gap:9px; padding:9px 11px;
            font-size:calc(12.5px * var(--font-scale)); line-height:1.5; }
        .report-row + .report-row { border-top:1px solid var(--rule); }
        .report-row .icon { width:16px; height:16px; flex-shrink:0; color:var(--accent); }
        .report-row.is-ok .icon { color:var(--success); }
        .report-row.is-warn .icon { color:var(--warning); }
        .report-row.is-bad .icon { color:var(--danger-ink); }
        .report-label { color:var(--text-muted); }
        .report-value { margin-inline-start:auto; font-weight:600; color:var(--text);
            font-variant-numeric:tabular-nums; white-space:nowrap; }
        .report-row.is-bad .report-value { color:var(--danger-ink); }
        .report-row:first-child { font-size:calc(13.5px * var(--font-scale)); }
        .report-row:first-child .report-label { color:var(--text); font-weight:700; }
        .report-row.is-warn:first-child .report-label { color:var(--warning); }
        .report-row.is-bad:first-child .report-label { color:var(--danger-ink); }

        /* A fixed box, not max-height: searching used to resize the browser with
           every result set (three hits = a three-row strip), and the keyboard
           inset shrank it while typing. It is now the same height always, and the
           page scrolls the section into view instead of resizing the list. */
        .tree { height: clamp(320px, 54vh, 560px); overflow: auto; background: var(--surface-field); border: 1px solid var(--border); border-radius: 10px; padding: 8px; scrollbar-width: thin; scrollbar-color: var(--border) transparent; }
        /* Search replaces the tree's contents; the header stays put while rows scroll. */
        .search-view-head { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px; margin: -8px -8px 8px; background: var(--surface-field); border-bottom: 1px solid var(--field-line); }
        .search-view-head .search-view-label { color: var(--text-muted); font-size: calc(12px * var(--font-scale)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .search-view-head .search-view-path { color: var(--text-muted); font-size: calc(11px * var(--font-scale)); direction: ltr; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .tree::-webkit-scrollbar, .modal-content .modal-body::-webkit-scrollbar { width: 8px; height: 8px; }
        .tree::-webkit-scrollbar-thumb, .modal-content .modal-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; }
        .tree::-webkit-scrollbar-track, .modal-content .modal-body::-webkit-scrollbar-track { background: transparent; }
        .item-row { display: flex; align-items: center; padding: 9px 8px; border-radius: 8px; border-bottom: 1px solid var(--border); min-width: max-content; transition: background-color 0.15s; }
        .item-row:last-child { border-bottom: none; }
        .item-row:hover { background: var(--surface-card-2); }
        .toggle-btn { background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: calc(12px * var(--font-scale)); width: 26px; height: 26px; padding: 0; flex-shrink:0; }
        .excluded-badge { color: var(--warning); font-size: calc(11px * var(--font-scale)); margin-right: 8px; background: var(--warning-soft); border: 1px solid var(--warning-line); padding: 2px 6px; border-radius: 6px;}
        .checkbox { accent-color: var(--accent); width: 18px; height: 18px; margin: 0 10px; flex-shrink: 0; cursor: pointer;}
        .item-name { cursor: pointer; display: flex; align-items: center; gap: 8px; white-space: nowrap; padding-right: 6px; color: var(--text); font-size:calc(14px * var(--font-scale));}
        .children { margin-right: 24px; display: none; border-right: 1px solid var(--border); padding-right: 12px; }
        .children.open { display: block; }

        .row-actions { display:flex; gap:6px; margin-inline-start:auto; flex-shrink:0; padding-inline-start:10px; }
        /* Files in the picker keep their size on the far side, so the delete and
           rename buttons line up with the folder rows above them. */
        .picker-file-row .row-actions { margin-inline-start: 0; padding-inline-start: 6px; }
        .picker-file-row .file-size { margin-inline-start: auto; }
        .icon-btn { width:30px; height:30px; border-radius:8px; border:1px solid var(--border); background:var(--surface-field); color:var(--text-muted); display:flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; }
        .icon-btn svg { width:14px; height:14px; }
        .icon-btn:active { transform: scale(0.92); }

        /* Storage picker: one horizontal row of icon tiles, each captioned with a
           single word. Each kind sets --kind/--kind-soft so a tile is coloured by
           *what it is* (SD / USB / internal) instead of by the global accent. */
        .storage-grid { display: flex; flex-direction: row; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; align-items: flex-start; justify-content: center; }
        /* The tile is an icon with a one-word caption: big enough to be the first
           thing the thumb hits, small enough that three volumes still fit one row. */
        .storage-card { position: relative; display: flex; flex: 0 0 auto; flex-direction: column; align-items: center; gap: 4px;
            padding: 6px 8px; border: 1px solid var(--field-line); border-radius: 11px;
            background: var(--surface-field); color: var(--text); cursor: pointer; text-align: center;
            transition: border-color .15s, background .15s; font-family: inherit; appearance: none; -webkit-appearance: none; }
        .storage-card:hover { border-color: var(--kind, var(--accent)); }
        /* The kind hue is always there, but only the selected volume wears it at
           full strength: an unselected tile stays quiet without losing its identity. */
        .storage-card .storage-ico { width: 32px; height: 32px; padding: 5px; flex-shrink: 0; box-sizing: border-box;
            border-radius: 9px; background: var(--surface-field); color: var(--kind, var(--text-muted));
            opacity: .42; transition: opacity .15s, background .15s; }
        .storage-short { font-size: calc(11px * var(--font-scale)); font-weight: 600; color: var(--text-muted); line-height:1.2; opacity: .8; }
        .storage-card.active { border-color: var(--kind, var(--accent)); background: var(--kind-soft, var(--accent-soft)); }
        .storage-card.active .storage-ico { background: var(--kind-tile, var(--accent-soft)); color: var(--kind, var(--accent)); opacity: 1; }
        .storage-card.active .storage-short { color: var(--kind-ink, var(--accent)); opacity: 1; }
        /* A cached source folder is announced by one small bolt above the tile. */
        .storage-fresh { display:none; position:absolute; top:-7px; inset-inline-start:-5px;
            width:16px; height:16px; align-items:center; justify-content:center;
            border-radius:50%; background:var(--accent-fill); color:var(--on-accent); }
        .storage-fresh svg { width:10px; height:10px; fill:currentColor; }
        .storage-card.is-fresh .storage-fresh { display:flex; }
        .storage-card.kind-internal { --kind: var(--kind-internal); --kind-ink: var(--kind-internal-ink); --kind-soft: var(--kind-internal-soft); --kind-tile: var(--kind-internal-tile); }
        .storage-card.kind-sdcard { --kind: var(--kind-sdcard); --kind-ink: var(--kind-sdcard-ink); --kind-soft: var(--kind-sdcard-soft); --kind-tile: var(--kind-sdcard-tile); }
        .storage-card.kind-usb { --kind: var(--kind-usb); --kind-ink: var(--kind-usb-ink); --kind-soft: var(--kind-usb-soft); --kind-tile: var(--kind-usb-tile); }
        .storage-card.kind-card { --kind: var(--kind-card); --kind-ink: var(--kind-card-ink); --kind-soft: var(--kind-card-soft); --kind-tile: var(--kind-card-tile); }
        /* Path box in the picker: border says which volume you are browsing. */
        .kind-border-internal { border-color: var(--kind-internal) !important; }
        .kind-border-sdcard { border-color: var(--kind-sdcard) !important; }
        .kind-border-usb { border-color: var(--kind-usb) !important; }
        .kind-border-card { border-color: var(--kind-card) !important; }
        .picker-hint { font-size: calc(11.5px * var(--font-scale)); color: var(--text-muted); margin: 6px 2px 8px; }
        .picker-file-row .item-name { color: var(--text-muted); cursor: default; }
        .file-size { margin-inline-start: auto; font-size: calc(11px * var(--font-scale)); color: var(--text-muted); direction: ltr; padding-inline-start: 10px; }
        .icon-btn:hover { color: var(--accent); border-color: var(--accent); }
        .icon-btn.is-open { color: var(--accent-text); border-color: var(--accent); background: var(--accent-soft); }
        .icon-btn.danger:hover { color: var(--danger-ink); border-color: var(--danger); }

        /* ---- action bar ------------------------------------------------- */
        /* Opaque on purpose: a translucent bar over a tree of filenames is
           unreadable while scrolling, and backdrop-filter costs the phone GPU
           for nothing. The copy's progress is mirrored here as a 3px line so
           the user never has to open the progress dialog to know where it is. */
        .actionbar { position: fixed; bottom: 0; left: 0; width: 100%; background: var(--surface-bar);
            border-top: 1px solid var(--bar-line); padding: 9px 16px calc(9px + env(safe-area-inset-bottom));
            z-index: 60; box-shadow: 0 -12px 30px -20px var(--shadow-strong); }
        .actionbar-line { display:flex; align-items:center; gap:10px; max-width:900px; margin:0 auto; }
        .actionbar-summary { display:flex; flex-direction:column; align-items:flex-start; gap:2px; flex:1 1 auto;
            min-width:0; background:none; border:0; padding:2px 0; cursor:pointer; color:var(--text);
            text-align:start; font-family:inherit; }
        .actionbar-summary:focus-visible { outline:2px solid var(--accent); outline-offset:3px; border-radius:8px; }
        .actionbar-label { font-size:calc(10px * var(--font-scale)); color:var(--text-muted); letter-spacing:.06em; }
        .actionbar-nums { display:flex; align-items:baseline; gap:7px; flex-wrap:wrap; min-width:0; }
        .actionbar-nums b { font-size:calc(18px * var(--font-scale)); font-weight:700; color:var(--accent-text); line-height:1.25; }
        .actionbar-size, .actionbar-eta { font-size:calc(11.5px * var(--font-scale)); color:var(--text-muted); }
        .actionbar-eta { color:var(--accent-text); }
        .actionbar-go { flex-shrink:0; min-height:46px; padding:0 20px; font-size:calc(14px * var(--font-scale)); }
        .actionbar .pill { max-width:34vw; overflow:hidden; text-overflow:ellipsis; }
        .actionbar-progress { display:none; position:absolute; top:0; left:0; right:0; height:3px;
            background:var(--rule); overflow:hidden; }
        .actionbar-progress.on { display:block; }
        .actionbar-fill { height:100%; width:0%; background:var(--accent); border-radius:2px; transition:width .3s ease; }
        @media (max-width: 480px) {
            .actionbar { padding: 8px 12px calc(8px + env(safe-area-inset-bottom)); }
            .actionbar-go { padding:0 15px; }
            .actionbar .pill { max-width:26vw; }
        }

        .modal { display: none; position: fixed; inset: 0; width: 100%; height: 100%; background: var(--scrim); z-index: 1000; justify-content: center; align-items: safe center; padding: 16px; overflow: auto; -webkit-overflow-scrolling: touch; }
        #confirmModal { z-index: 1100; }
        /* The modal opened last must sit on top of any other open modal (e.g.
           rename/input opening over the selected-items window). */
        .modal-front { z-index: 1001; }
        /* A terminal copy alert sits above every dialog but never takes focus:
           the report and its buttons stay usable underneath. */
        .app-toast { display:none; position:fixed; top:calc(12px + env(safe-area-inset-top)); left:50%; transform:translateX(-50%); z-index:1200;
            width:max-content; max-width:min(92vw, 520px); padding:11px 15px; border-radius:12px; border:1px solid var(--border);
            background:var(--surface-card-2); color:var(--text); box-shadow:0 18px 40px -16px var(--shadow-strong);
            font-size:calc(13px * var(--font-scale)); font-weight:600; line-height:1.6; gap:8px;
            align-items:center; pointer-events:none; }
        .app-toast .icon { width:17px; height:17px; flex:0 0 auto; }
        .app-toast.is-success { background:var(--ok-soft); border-color:var(--ok-line); color:var(--success); }
        .app-toast.is-error { background:var(--danger-soft); border-color:var(--danger); color:var(--danger-ink); }
        .app-toast.is-info { background:var(--accent-soft); border-color:var(--accent-line); color:var(--accent-text); }
        .modal-content { background: var(--surface-card); padding: 20px; border-radius: 18px; width: 100%; max-width: 600px; border: 1px solid var(--border); max-height: max(70vh, 480px); display: flex; flex-direction: column; box-shadow: 0 24px 48px -12px var(--shadow-strong); margin: auto; }
        .modal-content .modal-body { overflow-y: auto; flex: 1 1 auto; min-height: 0; -webkit-overflow-scrolling: touch; }
        .modal-content .modal-footer { display: flex; gap: 12px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); flex-shrink: 0; }
        /* The folder picker keeps one height whatever the folder holds: a 3-item
           folder and a 3 000-item folder used to resize the dialog, which moved
           the Confirm button under the finger. */
        .modal-content.modal-picker { height: min(84vh, 720px); max-height: 84vh; }
        .modal-content.modal-picker #destTree { max-height: none; }

        /* ---- settings ------------------------------------------------- */
        /* Short settings tabs fit their content instead of leaving a large
           blank panel; long tabs remain bounded and scroll inside the dialog. */
        .modal-content.modal-settings { height:auto; max-height:min(84vh, 760px);
            max-height:min(84dvh, 760px); min-height:0; }
        /* Transfer results can contain a long path list. Keep the whole dialog
           inside the viewport while allowing its contents to remain reachable. */
        #progressModal > .modal-content { max-height:min(92dvh, 820px); overflow-y:auto;
            overscroll-behavior:contain; scrollbar-width:thin; }
        #progressModal .modal-head-row { position:sticky; top:-20px; z-index:1;
            background:var(--surface-card); padding-top:2px; }
        #errorsBox { max-height:32dvh; overflow:auto; overflow-wrap:anywhere; word-break:break-word;
            line-height:1.75; scrollbar-width:thin; -webkit-overflow-scrolling:touch; }
        #errorsBox:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
        #progressModal .btn { transition:transform .12s ease, background-color .15s ease, border-color .15s ease; }
        #progressModal .btn:active { transform:translateY(1px); }
        #progressModal .btn:focus-visible, #minimizeProgressBtn:focus-visible {
            outline:2px solid var(--accent); outline-offset:2px; }
        .settings-tabs { display:flex; gap:6px; margin:2px 0 10px; flex-shrink:0; }
        .settings-tab { flex:1; min-width:0; min-height:40px; padding:7px 9px; border-radius:10px; border:1px solid var(--border);
            background:var(--surface-field); color:var(--text-muted); font-size:calc(12.5px * var(--font-scale)); font-weight:600;
            cursor:pointer; font-family:inherit; display:flex; align-items:center; justify-content:center; gap:6px;
            transition:border-color .15s, color .15s, background .15s; }
        .settings-tab .icon { width:14px; height:14px; flex:0 0 auto; }
        .settings-tab.active { color:var(--accent-text); border-color:var(--accent); background:var(--accent-soft); }
        .settings-tab:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
        .settings-pane { display:flex; flex-direction:column; gap:8px; min-width:0; }
        /* Settings are grouped by a title and a hairline, not by a paragraph of
           explanation: the control says what it does. */
        .settings-group { display:flex; flex-direction:column; gap:7px; min-width:0; }
        .settings-group + .settings-group { border-top:1px solid var(--rule); padding-top:8px; }
        .settings-group-title { display:flex; align-items:center; gap:6px; font-size:calc(10.5px * var(--font-scale));
            font-weight:700; color:var(--text-muted); }
        .settings-group-title .icon { width:14px; height:14px; color:var(--accent); flex:0 0 auto; }
        .settings-check-row { display:flex; align-items:flex-start; gap:9px; margin:0; cursor:pointer; color:var(--text);
            font-size:calc(13px * var(--font-scale)); font-weight:500; }
        .settings-check-row .checkbox { width:17px; height:17px; margin:1px 0 0; flex-shrink:0; }
        .settings-check-row > span { min-width:0; overflow-wrap:anywhere; }
        .settings-check-row small { display:block; margin-top:3px; }
        .settings-option-copy { display:flex; align-items:flex-start; gap:7px; min-width:0; }
        .settings-option-icon { width:15px; height:15px; flex:0 0 15px; margin-top:2px; color:var(--accent-text); }
        .settings-field > label { display:flex; align-items:center; gap:6px; }
        .settings-field > label .icon { width:14px; height:14px; flex:0 0 auto; color:var(--accent); }
        .size-seg { display:flex; gap:6px; }
        .size-seg .size-seg-btn { flex:1; min-height:38px; padding:6px 8px; border-radius:9px;
            border:1px solid var(--border); background:var(--surface-field); color:var(--text-muted);
            font-family:inherit; font-size:calc(12px * var(--font-scale)); font-weight:600; cursor:pointer; }
        .size-seg .size-seg-btn.is-active { border-color:var(--accent); background:var(--accent-soft); color:var(--accent-text); }
        .draft-banner { display:flex; align-items:center; gap:8px; padding:9px 11px; border-radius:10px;
            background:var(--accent-soft); border:1px solid var(--accent-line); color:var(--accent-text);
            font-size:calc(11.5px * var(--font-scale)); font-weight:600; line-height:1.5; }
        .draft-banner .icon { width:15px; height:15px; flex-shrink:0; }
        .index-group { display:flex; flex-direction:column; gap:7px; padding:10px 11px;
            border:1px solid var(--border); border-radius:12px; background:var(--surface-card-2); min-width:0; }
        .index-group-title { display:flex; align-items:center; gap:6px; font-size:calc(11px * var(--font-scale));
            font-weight:700; color:var(--text-muted); }
        .index-group-title .icon { width:14px; height:14px; color:var(--accent); }
        .index-fields-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:8px 10px; min-width:0; }
        .index-field-label { display:flex; align-items:center; gap:6px; }
        .index-field-label .icon { width:13px; height:13px; flex:0 0 auto; color:var(--accent-text); }
        .index-options-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:6px; min-width:0; }
        .index-check-row { min-height:40px; align-items:center; padding:7px 8px; border:1px solid var(--border);
            border-radius:9px; background:var(--surface-field); font-size:calc(11.5px * var(--font-scale)); }
        .index-check-row .checkbox { margin-top:0; }
        .index-check-row .settings-option-copy { gap:6px; }
        .index-check-row .settings-option-icon { width:14px; height:14px; flex-basis:14px; margin-top:1px; }
        .index-subtitle { display:flex; align-items:center; gap:6px; font-size:calc(11px * var(--font-scale));
            font-weight:700; color:var(--text-muted); }
        .index-subtitle .icon { width:14px; height:14px; color:var(--accent); }
        /* One labelled dropdown per choice: a grid of 16 colour cards buried the
           save state and made the settings dialog scroll for a trivial change. */
        .settings-field { display:flex; flex-direction:column; gap:4px; min-width:0; }
        .settings-field > label { font-size:calc(12.5px * var(--font-scale)); font-weight:600; color:var(--text); }
        .settings-field select { width:100%; min-height:44px; padding:9px 12px; border-radius:10px;
            border:1px solid var(--border); background:var(--surface-field); color:var(--text);
            font-family:inherit; font-size:calc(13px * var(--font-scale)); cursor:pointer; }
        .settings-field select:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
        .field-hint { font-size:calc(10.5px * var(--font-scale)); color:var(--text-muted); line-height:1.5; }
        .theme-preview-row { display:flex; align-items:center; justify-content:space-between; gap:8px; min-width:0; }
        .theme-preview { display:flex; align-items:center; gap:5px; min-width:0; direction:ltr; }
        .theme-preview-chip { display:block; width:25px; height:19px; flex:0 0 auto;
            border:1px solid var(--border); border-radius:6px; box-shadow:inset 0 0 0 1px rgba(0,0,0,.08); }
        #themePreviewName { min-width:0; text-align:end; overflow-wrap:anywhere; }
        .photo-background-controls { display:flex; flex-direction:column; gap:7px; }
        .photo-background-actions { display:flex; gap:7px; flex-wrap:wrap; }
        .photo-background-actions .btn { flex:1 1 130px; min-height:38px; }
        .photo-background-preview { display:block; width:100%; height:118px; object-fit:cover;
            border:1px solid var(--border); border-radius:10px; background:var(--surface-field); }
        .photo-background-preview[hidden] { display:none; }
        .photo-opacity-row { display:flex; align-items:center; gap:10px; }
        .photo-opacity-row input { flex:1 1 auto; min-width:0; margin:0; accent-color:var(--accent); }
        .photo-opacity-value { min-width:42px; text-align:center; color:var(--accent-text); font-weight:700; }
        #photoBackgroundState { min-height:16px; overflow-wrap:anywhere; }
        #fontPreview { font-size:calc(17px * var(--font-scale)); color:var(--text); }
        .settings-note { font-size:calc(10.5px * var(--font-scale)); color:var(--text-muted); line-height:1.6; }
        .settings-save-state { font-size:calc(11.5px * var(--font-scale)); min-height:16px; color:var(--text-muted); }
        .settings-save-state.ok { color:var(--success); }
        .settings-save-state.err { color:var(--danger-ink); }
        .settings-save-state.is-loading { display:flex; align-items:center; gap:8px; }
        .settings-save-state.is-loading::before,
        .saved-operation-row-actions button.is-loading::before {
            content:""; width:14px; height:14px; flex:0 0 14px; border-radius:50%;
            border:2px solid var(--accent-line); border-top-color:var(--accent-text);
            animation:saved-operation-spin .75s linear infinite;
        }
        .saved-operation-row-actions button.is-loading { cursor:wait; }
        @keyframes saved-operation-spin { to { transform:rotate(360deg); } }
        @media (prefers-reduced-motion:reduce) {
            .settings-save-state.is-loading::before,
            .saved-operation-row-actions button.is-loading::before { animation:none; }
        }
        /* Read-only status rows (device state): a label and a value, no chrome. */
        .status-list { display:flex; flex-direction:column; border:1px solid var(--field-line);
            border-radius:10px; background:var(--surface-field); }
        .status-row { display:flex; align-items:center; gap:10px; padding:9px 11px;
            font-size:calc(12.5px * var(--font-scale)); }
        .status-row + .status-row { border-top:1px solid var(--rule); }
        .status-label { color:var(--text-muted); }
        .status-value { margin-inline-start:auto; font-weight:600; color:var(--text); text-align:end;
            min-width:0; overflow:hidden; text-overflow:ellipsis; }
        .status-value.ok { color:var(--success); }
        .status-value.warn { color:var(--warning); }
        .status-value.bad { color:var(--danger-ink); }

        /* Keep the legacy index form compact by neutralizing its global control
           margins here; labels stay visually attached to their fields. */
        .index-form { display:flex; flex-direction:column; gap:8px; }
        .index-form, .index-form .grid-2, .index-form .grid-2 > div { min-width:0; }
        .index-form label:not(.settings-check-row) { margin:0 0 3px; line-height:1.4; }
        .index-form select, .index-form input[type="number"] { min-width:0; margin:0; }
        .index-form .grid-2 { gap:8px; }
        .about-card { display:flex; flex-direction:column; gap:12px; padding:14px; border:1px solid var(--border);
            border-radius:14px; background:var(--surface-card-2); min-width:0; }
        .about-brand { display:flex; align-items:center; gap:10px; min-width:0; }
        .about-mark { width:42px; height:42px; flex:0 0 42px; display:flex; align-items:center; justify-content:center;
            border-radius:12px; border:1px solid var(--accent-line); background:var(--accent-soft); color:var(--accent-text); }
        .about-mark .icon { width:21px; height:21px; }
        .about-brand-name { min-width:0; color:var(--text); font-size:calc(15px * var(--font-scale)); font-weight:700; }
        .about-brand-caption { color:var(--text-muted); font-size:calc(11px * var(--font-scale)); }
        .about-version { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:9px 11px;
            border:1px solid var(--border); border-radius:10px; background:var(--surface-field); }
        .about-version-label { display:flex; align-items:center; gap:7px; color:var(--text-muted); font-size:calc(12px * var(--font-scale)); }
        .about-version-label .icon { width:15px; height:15px; color:var(--accent); }
        .about-version-value { color:var(--text); font-weight:700; }
        .about-local-note { display:flex; align-items:flex-start; gap:8px; color:var(--text); font-size:calc(11.5px * var(--font-scale)); line-height:1.6; }
        .about-local-note .icon { width:16px; height:16px; flex:0 0 16px; margin-top:2px; color:var(--success); }
        .about-online-note { padding-inline-start:24px; color:var(--text-muted); font-size:calc(10.5px * var(--font-scale)); line-height:1.5; }
        .about-github-link { display:flex; align-items:center; gap:9px; min-height:44px; padding:9px 11px;
            border:1px solid var(--accent-line); border-radius:10px; background:var(--accent-soft);
            color:var(--accent-text); text-decoration:none; font-weight:700; }
        .about-github-link:hover, .about-github-link:focus-visible { border-color:var(--accent); background:var(--accent-line); outline:none; }
        .about-github-link .icon { width:18px; height:18px; }
        .about-github-link .external-icon { width:14px; height:14px; margin-inline-start:auto; }
        .about-state-path { color:var(--text-muted); font-size:calc(10.5px * var(--font-scale)); line-height:1.6; overflow-wrap:anywhere; }
        .joc-summary { background:var(--surface-field); border:1px solid var(--border); border-radius:12px;
            padding:12px 14px; font-size:calc(12px * var(--font-scale)); color:var(--text-muted);
            line-height:1.9; }
        .joc-summary b { color:var(--text); font-weight:600; }
        .joc-summary .summary-title { display:flex; align-items:center; gap:7px; color:var(--text);
            font-weight:600; margin-bottom:6px; font-size:calc(12.5px * var(--font-scale)); }

        /* ---- search history ------------------------------------------- */
        .search-history { margin-top:14px; border-top:1px solid var(--border); padding-top:12px; }
        .history-head { display:flex; align-items:center; gap:8px; margin-bottom:9px; }
        .history-title { font-size:calc(12px * var(--font-scale)); font-weight:600; color:var(--text-muted); }
        .history-head .btn { margin-inline-start:auto; padding:4px 9px; font-size:calc(11px * var(--font-scale)); }
        .history-chips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
        .history-chip { display:inline-flex; align-items:center; gap:6px; padding:5px 11px;
            border-radius:999px; border:1px solid var(--border); background:var(--surface-field);
            color:var(--text); font-family:inherit; font-size:calc(11.5px * var(--font-scale)); cursor:pointer; }
        .history-chip:hover, .history-chip:focus-visible { border-color:var(--accent); color:var(--accent-text); outline:none; }
        .history-chip small { font-size:calc(9.5px * var(--font-scale)); color:var(--text-muted); }
        .history-rows { display:flex; flex-direction:column; gap:6px; max-height:280px; overflow-y:auto; scrollbar-width:thin; }
        .history-row { display:flex; align-items:center; gap:8px; padding:8px 10px;
            border:1px solid var(--border); background:var(--surface-field); border-radius:11px; }
        .history-row-main { flex:1; min-width:0; cursor:pointer; display:flex; flex-direction:column; gap:3px; }
        .history-row-main:hover .history-row-name { color:var(--accent-text); }
        .history-row-name { font-size:calc(12.5px * var(--font-scale)); color:var(--text); display:flex; align-items:center; gap:6px; }
        .history-row-name svg { width:13px; height:13px; flex-shrink:0; }
        .history-row-path { font-size:calc(10.5px * var(--font-scale)); color:var(--text-muted); direction:ltr; text-align:left;
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .history-row-meta { font-size:calc(10px * var(--font-scale)); color:var(--text-muted); white-space:nowrap; flex-shrink:0; }
        .history-empty { padding:10px 2px; font-size:calc(11.5px * var(--font-scale)); color:var(--text-muted); line-height:1.7; }
        .history-row.added { border-color:var(--accent); background:var(--accent-soft); }
        @media (max-width: 480px) {
            .modal { padding: 8px; align-items: stretch; }
            .modal-content { padding: 16px; max-height: 92vh; border-radius: 14px; }
            #progressModal > .modal-content { max-height:94dvh; padding:14px; border-radius:14px; }
            #progressModal .modal-head-row { top:-14px; }
            #progressModal .report-row { align-items:flex-start; }
            #progressModal .report-value { white-space:normal; min-width:0; text-align:end; }
            .modal-content.modal-picker { height: 92vh; max-height: 92vh; }
            .modal-content.modal-selected { height: 92vh; max-height: 92vh; }
            .modal-content.modal-settings { height:auto; max-height:92vh; max-height:92dvh; }
            .modal-content.modal-settings .modal-body { min-width:0; overflow-x:hidden; }
            .settings-tabs { margin-bottom:10px; }
            .settings-tab { gap:4px; padding:7px 4px; font-size:calc(11px * var(--font-scale)); }
            .settings-tab .icon { width:13px; height:13px; }
            .index-options-grid { grid-template-columns:1fr; }
            .index-group { padding:9px; }
        }
        @media (max-width: 330px) {
            .index-fields-grid { grid-template-columns:1fr; }
        }

        .progress-bar { width: 100%; height: 10px; background: var(--surface-field); border-radius: 999px; overflow: hidden; margin: 15px 0; border: 1px solid var(--border);}
        .progress-fill { height: 100%; background: var(--accent); width: 0%; border-radius: 999px; transition: width 0.3s ease; }
        /* The dialog folds into the action bar (its header carries «تصغير») so a
           running copy never blocks the page. */
        .modal-head-row { display:flex; align-items:center; gap:10px; border-bottom:1px solid var(--border); padding-bottom:12px; margin-bottom:6px; }
        .modal-head-row h2 { margin:0; flex:1 1 auto; font-size:calc(16px * var(--font-scale)); border:0; padding:0; }
        /* Queue hook: only rendered once something is waiting, so the section
           never advertises an idle feature. */
        .queue-block { margin-top:14px; border:1px solid var(--field-line); border-radius:10px; background:var(--surface-field); padding:8px 10px; }
        .queue-head { display:flex; align-items:center; justify-content:space-between; gap:8px;
            font-size:calc(11px * var(--font-scale)); color:var(--text-muted); letter-spacing:.04em; }
        .queue-rows { display:flex; flex-direction:column; gap:4px; margin-top:6px; }
        .queue-row { display:flex; align-items:center; gap:8px; font-size:calc(12px * var(--font-scale)); color:var(--text); }
        .queue-row-label { flex:1 1 auto; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .queue-row-size { color:var(--text-muted); }
        /* A delete has no known total, so its bar sweeps instead of pretending
           to know a percentage. The solid fill keeps it readable when the user
           asked the system for reduced motion. */
        .progress-fill.indeterminate, .actionbar-fill.indeterminate {
            width: 100% !important; background-color: var(--accent);
            background-image: linear-gradient(90deg, transparent 0%, var(--shine) 50%, transparent 100%);
            background-size: 40% 100%; background-repeat: no-repeat;
            animation: sdmp3-sweep 1.3s linear infinite;
        }
        @keyframes sdmp3-sweep {
            0%   { background-position: -40% 0; }
            100% { background-position: 140% 0; }
        }
        .ltr-input { direction: ltr; text-align: left; font-family: var(--num-font); font-size: calc(12px * var(--font-scale)); }

        .disk-space-container { margin-bottom: 10px; font-size: calc(13px * var(--font-scale)); color: var(--text-muted); display: none;}
        .disk-bar { width: 100%; height: 6px; background: var(--surface-field); border-radius: 3px; overflow: hidden; margin-top: 6px; border: 1px solid var(--border);}
        .disk-used { height: 100%; background: var(--accent); width: 0%; transition: width 0.3s ease; }

        /* Copy options and the optional parent folders ("groups"). */
        .check-line { display:flex; align-items:center; gap:9px; cursor:pointer; color:var(--text); font-size:calc(13px * var(--font-scale)); margin:12px 0 0; font-weight:500; }
        .check-line .checkbox { width:17px; height:17px; margin:0; flex-shrink:0; }
        .check-hint { display:block; color:var(--text-muted); font-size:calc(11.5px * var(--font-scale)); line-height:1.55; margin:5px 0 0; }
        .check-hint.warning { color:var(--warning); background:var(--warning-soft);
            border:1px solid var(--warning-line); border-radius:9px; padding:8px 10px;
            white-space:pre-line; overflow-wrap:anywhere; max-height:140px; overflow-y:auto; }

        /* The selected-items window keeps one height: the folder list keeps its
           place while the file rows scroll, so the action cannot move under the
           finger. */
        .modal-content.modal-selected { height: min(84vh, 760px); max-height: 84vh; position: relative; overflow: hidden; }
        .selected-head { display:flex; flex-direction:column; gap:9px; flex-shrink:0; padding-bottom:10px; border-bottom:1px solid var(--border); }
        .selected-title-row { display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap; }
        .selected-operation-actions { display:flex; align-items:center; gap:4px; flex-wrap:wrap; }
        .selected-operation-actions .btn { min-height:34px; padding:6px 9px; }
        .saved-operation-active { color:var(--accent-text); font-size:calc(11px * var(--font-scale)); overflow-wrap:anywhere; }
        .selected-list { flex:1 1 auto; min-height:0; overflow-y:auto; padding:10px 2px 4px; display:flex; flex-direction:column; gap:8px; }
        .selected-browse-toggle { width:27px; height:27px; flex:0 0 27px; padding:0; border:1px solid var(--border);
            border-radius:7px; background:var(--surface-field); color:var(--text-muted); display:inline-flex;
            align-items:center; justify-content:center; cursor:pointer; }
        .selected-browse-toggle:hover, .selected-browse-toggle.open { color:var(--accent-text); border-color:var(--accent-line); }
        .selected-browse-toggle .icon { width:13px; height:13px; transition:transform .15s ease; }
        .selected-browse-toggle.open .icon { transform:rotate(90deg); }
        .selected-browse-tree { display:flex; flex-direction:column; gap:4px; padding:7px 8px;
            border-inline-start:2px solid var(--accent-line); border-radius:8px; background:var(--surface-field);
            color:var(--text-muted); font-size:calc(11.5px * var(--font-scale)); }
        .selected-browse-tree.is-empty { font-style:italic; }
        .selected-browse-row { min-width:0; display:flex; align-items:center; gap:7px; padding:5px 6px;
            border:1px solid var(--border); border-radius:8px; background:var(--surface-card); }
        .selected-browse-checkbox { width:16px; height:16px; flex:0 0 16px; margin:0; }
        .selected-browse-icon { width:17px; height:17px; flex:0 0 17px; display:inline-flex; align-items:center; }
        .selected-browse-icon .icon { width:16px; height:16px; }
        .selected-browse-name { min-width:0; flex:1; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .selected-browse-message.error { color:var(--danger-ink); }
        .modal-content.modal-saved-operations { height:auto; max-height:min(84vh, 700px); max-height:min(84dvh, 700px); min-height:0; }
        .saved-operations-list { display:flex; flex-direction:column; gap:7px; min-height:0; max-height:55vh; max-height:55dvh;
            overflow-y:auto; overscroll-behavior:contain; padding:2px 1px; }
        .saved-operation-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:9px 10px;
            border:1px solid var(--border); border-radius:10px; background:var(--surface-field); }
        .saved-operation-row-main { min-width:0; flex:1 1 130px; display:flex; flex-direction:column; gap:3px; }
        .saved-operation-row-name { color:var(--text); font-size:calc(13px * var(--font-scale)); font-weight:600; overflow-wrap:anywhere; }
        .saved-operation-row-detail { color:var(--text-muted); font-size:calc(10.5px * var(--font-scale)); line-height:1.4; }
        .saved-operation-row-actions { display:flex; gap:4px; flex-wrap:wrap; }
        .saved-operation-row-actions .btn { min-height:34px; padding:6px 8px; }
        .modal-content.modal-save-operation-name { max-width:420px; }
        @media (max-width: 380px) {
            .selected-operation-actions { gap:3px; }
            .selected-operation-actions .btn { padding:6px 7px; }
            .saved-operation-row-actions { width:100%; }
            .saved-operation-row-actions .btn { flex:1 1 auto; }
        }

        .group-section { display:flex; flex-direction:column; gap:8px; padding:10px 11px; border-radius:12px;
            background:var(--surface-field); border:1px solid var(--border); max-height:34vh; overflow-y:auto; }
        .group-section-head { display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap; }
        .group-section-title { display:flex; align-items:center; gap:8px; font-size:calc(12.5px * var(--font-scale)); font-weight:600; color:var(--text); }
        .group-section-title .icon { color:var(--accent); width:16px; height:16px; }
        .group-section-actions { display:flex; gap:6px; flex-wrap:wrap; }

        /* Copy folders ("مجلدات النسخ"): nested rows accept both item and
           folder drops; the parent picker provides the same operation on touch. */
        .group-tree { display:flex; flex-direction:column; gap:2px; }
        .gt-row { display:flex; align-items:center; gap:6px; padding:6px 7px; border-radius:9px;
            border:1px solid transparent; background:transparent; cursor:grab; direction:rtl; }
        .gt-row:hover, .gt-row:focus-within { background:var(--accent-soft); }
        .gt-row.is-drop { border-color:var(--accent); border-style:dashed; background:var(--accent-soft); }
        .gt-row.is-group-dragging { opacity:.45; }
        .gt-row.is-renaming { background:var(--surface-card); border-color:var(--accent-line); }
        .gt-twisty { width:22px; height:22px; padding:0; border:0; background:none; color:var(--text-muted);
            display:inline-flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; border-radius:6px; }
        .gt-twisty:hover { color:var(--accent-text); }
        .gt-twisty .icon { width:13px; height:13px; }
        .gt-root-drop { display:flex; align-items:center; justify-content:center; gap:7px; min-height:38px;
            padding:7px 10px; border:1px dashed var(--border); border-radius:9px; color:var(--text-muted);
            font-size:calc(10.5px * var(--font-scale)); text-align:center; transition:background .15s,border-color .15s; }
        .gt-root-drop .icon { width:14px; height:14px; flex:0 0 auto; }
        .gt-root-drop.is-drop { border-color:var(--accent); background:var(--accent-soft); color:var(--accent-text); }
        .gt-folder { width:16px; height:16px; flex-shrink:0; color:var(--accent-text); }
        .gt-name { flex:1; min-width:0; font-size:calc(12.5px * var(--font-scale)); font-weight:600; color:var(--text);
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis; cursor:pointer; }
        .gt-count { flex-shrink:0; font-size:calc(10px * var(--font-scale)); color:var(--text-muted);
            background:var(--surface-card); border:1px solid var(--border); border-radius:999px; padding:1px 7px; }
        .gt-actions { display:flex; align-items:center; gap:2px; flex-shrink:0; }
        .gt-btn { width:26px; height:26px; padding:0; border:1px solid transparent; border-radius:7px;
            background:none; color:var(--text-muted); display:inline-flex; align-items:center;
            justify-content:center; cursor:pointer; }
        .gt-btn:hover { color:var(--accent-text); border-color:var(--accent-line); }
        .gt-btn.danger:hover { color:var(--danger-ink); border-color:var(--danger-line); }
        .gt-btn .icon { width:14px; height:14px; }
        .gt-rename { flex:1; min-width:0; margin:0; padding:3px 8px; border-radius:7px;
            border:1px solid var(--accent); background:var(--surface-field); color:var(--text);
            font-family:inherit; font-size:calc(12.5px * var(--font-scale)); }
        .gt-paths { display:flex; flex-direction:column; gap:3px; padding:2px 0 5px; margin-inline-start:26px; }
        .gt-path { display:flex; align-items:center; gap:7px; padding:4px 8px; border-radius:8px;
            background:var(--surface-card); border:1px solid var(--border); }
        .gt-path.is-dragging { opacity:.45; }
        .gt-path .icon { width:13px; height:13px; color:var(--text-muted); flex-shrink:0; }
        .gt-path-name { flex:1; min-width:0; font-size:calc(11px * var(--font-scale)); color:var(--text);
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .gt-path-x { width:22px; height:22px; padding:0; border:0; border-radius:6px; background:none;
            color:var(--text-muted); display:inline-flex; align-items:center; justify-content:center; cursor:pointer; flex-shrink:0; }
        .gt-path-x:hover { color:var(--danger-ink); background:var(--danger-soft); }
        .gt-path-x .icon { width:12px; height:12px; }
        .gt-empty { padding:3px 8px; margin-inline-start:26px; font-size:calc(10.5px * var(--font-scale)); color:var(--text-muted); }
        .group-crumb-sep { color:var(--accent-text); margin:0 3px; font-weight:400; }
        .btn-ghost-danger { color:var(--danger-ink); border-color:var(--danger-line); }
        .group-empty-hint { font-size:calc(11px * var(--font-scale)); color:var(--text-muted); line-height:1.55; }

        /* Folder assignment sheet: one row per folder, opened from the chip on a
           selected file. It is the touch-friendly twin of drag & drop. */
        .assign-list { display:flex; flex-direction:column; gap:6px; max-height:38vh; overflow-y:auto; }
        .assign-row { display:flex; align-items:center; gap:9px; width:100%; padding:10px 12px; border-radius:10px;
            border:1px solid var(--border); background:var(--surface-field); color:var(--text); cursor:pointer;
            font-family:inherit; font-size:calc(12.5px * var(--font-scale)); text-align:start; }
        .assign-row:hover { border-color:var(--accent); background:var(--accent-soft); }
        .assign-row.is-active { border-color:var(--accent); background:var(--accent-soft); color:var(--accent-text); font-weight:600; }
        .assign-row .icon { width:15px; height:15px; color:var(--accent); flex-shrink:0; }
        .assign-row .assign-count { margin-inline-start:auto; color:var(--text-muted); font-size:calc(10.5px * var(--font-scale)); }
        .assign-new { display:flex; gap:8px; margin-top:10px; }
        .assign-new input { margin:0; flex:1; min-width:0; }
        .assign-new .btn { flex-shrink:0; }
        .group-parent-list { display:flex; flex-direction:column; gap:6px; max-height:44vh; overflow-y:auto; }

        .selected-row { display:flex; align-items:center; gap:10px; padding:10px 11px; border-radius:10px; background:var(--surface-field); border:1px solid var(--border); }
        .selected-row.is-dragging { opacity:.5; }
        .selected-row-check { width:19px; height:19px; margin:0; flex-shrink:0; accent-color:var(--accent); cursor:pointer; }
        .bulk-assign-bar { position:sticky; top:0; z-index:2; display:flex; align-items:center; gap:8px;
            flex-wrap:wrap; padding:8px 10px; border:1px solid var(--accent-line); border-radius:10px;
            background:var(--surface-card); box-shadow:0 3px 10px rgba(0,0,0,.15); }
        .bulk-select-all { display:flex; align-items:center; gap:7px; flex:1; min-width:130px;
            color:var(--text); font-size:calc(11.5px * var(--font-scale)); cursor:pointer; }
        .bulk-select-all input { width:17px; height:17px; margin:0; accent-color:var(--accent); }
        .bulk-count { color:var(--text-muted); font-size:calc(10.5px * var(--font-scale)); }
        .bulk-assign-bar .btn { min-height:34px; }
        .selected-row-main { min-width:0; flex:1; display:flex; flex-direction:column; gap:4px; }
        .selected-row-top { display:flex; align-items:center; gap:8px; min-width:0; }
        .selected-row-name { font-size:calc(13px * var(--font-scale)); font-weight:500; color:var(--text); }
        .selected-row-top .icon { color:var(--text-muted); flex-shrink:0; }
        .selected-row-right { display:flex; align-items:center; gap:8px; flex-shrink:0; }
        .selected-row-size { font-size:calc(11px * var(--font-scale)); color:var(--text-muted); min-width:52px; text-align:left; direction:ltr; }
        .group-chip { align-self:flex-start; max-width:100%; font-size:calc(10.5px * var(--font-scale)); padding:2px 8px; border-radius:20px;
            background:var(--accent-soft); border:1px solid var(--accent-line); color:var(--accent-text);
            white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        button.group-chip { display:inline-flex; align-items:center; gap:5px; cursor:pointer; font-family:inherit; }
        button.group-chip .icon { width:12px; height:12px; flex-shrink:0; }
        button.group-chip:hover { border-color:var(--accent); }
        .group-chip.none { background:transparent; border-color:var(--border); border-style:dashed; color:var(--text-muted); }

        /* «تنظيم ذكي» preview: a proposal the user can trim before it touches
           the groups. Nothing is written until «تطبيق». */
        .smart-preview { display:flex; flex-direction:column; gap:7px; padding:9px 10px;
            border-radius:10px; border:1px solid var(--accent-line); background:var(--accent-soft); }
        .smart-preview-head { display:flex; align-items:center; justify-content:space-between; gap:8px;
            font-size:calc(12px * var(--font-scale)); font-weight:600; color:var(--text); }
        .smart-preview-list { display:flex; flex-direction:column; gap:4px; max-height:22vh; overflow-y:auto; }
        .smart-preview-row { display:flex; align-items:center; gap:8px; padding:5px 8px; border-radius:8px;
            background:var(--surface-card); border:1px solid var(--border); cursor:pointer; }
        .smart-preview-row.is-off { opacity:.5; }
        .smart-preview-name { flex:1; min-width:0; font-size:calc(11.5px * var(--font-scale)); color:var(--text); }
        .smart-preview-meta { flex-shrink:0; font-size:calc(10.5px * var(--font-scale)); color:var(--text-muted); }
        .smart-preview-actions { display:flex; gap:7px; flex-wrap:wrap; }
        .field-error { min-height:14px; font-size:calc(11.5px * var(--font-scale)); color:var(--danger-ink); }

        /* ---- tools (collapsible) --------------------------------------- */
        /* Spoofing is a deliberate, occasional action, so it does not deserve
           the same weight as the copy itself: folded away, one line to open. */
        .tools { border:1px dashed var(--rule); border-radius:12px; padding:0 14px; }
        .tools[open] { padding-bottom:14px; border-style:solid; }
        .tools > summary { list-style:none; cursor:pointer; padding:13px 0; display:flex; align-items:center;
            gap:9px; font-size:calc(12.5px * var(--font-scale)); font-weight:600; color:var(--text); }
        .tools > summary::-webkit-details-marker { display:none; }
        .tools > summary .icon { width:15px; height:15px; color:var(--accent); flex-shrink:0; }
        .tools > summary .tools-chev { margin-inline-start:auto; color:var(--text-muted); transition:transform .15s; }
        .tools[open] > summary .tools-chev { transform:rotate(180deg); }
        .tools-body { display:flex; flex-direction:column; gap:10px; }
        .tools-body input[type="datetime-local"] { margin-bottom:0; }

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

        .seg.option.active { background: var(--accent-fill); color: var(--on-accent); font-weight: 600; }
        .seg.option.active svg { stroke: var(--on-accent); }
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
    <!-- Small status glyphs. Generated notices use these instead of emoji: an
         emoji gets its own colour and metrics and fights the theme. -->
    <symbol id="ico-check" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"></polyline></symbol>
    <symbol id="ico-alert" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><line x1="12" y1="8" x2="12" y2="13"></line><line x1="12" y1="16.5" x2="12.01" y2="16.5"></line></symbol>
    <symbol id="ico-plus" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></symbol>
    <symbol id="ico-spark" viewBox="0 0 24 24"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"></path><path d="M18 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7z"></path></symbol>
    <symbol id="ico-index" viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="18" rx="2"></rect><circle cx="8.2" cy="7.6" r="1.3"></circle><line x1="11.6" y1="7.6" x2="17" y2="7.6"></line><line x1="7" y1="12.2" x2="17" y2="12.2"></line><line x1="7" y1="16.4" x2="13.5" y2="16.4"></line></symbol>
    <symbol id="ico-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><polyline points="12 7 12 12 16 14"></polyline></symbol>
    <symbol id="ico-bolt" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></symbol>
    <symbol id="ico-bell" viewBox="0 0 24 24"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"></path><path d="M10 21h4"></path></symbol>
    <symbol id="ico-sound" viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9z"></path><path d="M16 9a4 4 0 0 1 0 6"></path><path d="M18.5 6.5a8 8 0 0 1 0 11"></path></symbol>
    <symbol id="ico-shield" viewBox="0 0 24 24"><path d="M12 3l7 3v6c0 4-3 7.5-7 9-4-1.5-7-5-7-9V6z"></path><polyline points="9 12 11.2 14.2 15.2 10"></polyline></symbol>
    <symbol id="ico-music" viewBox="0 0 24 24"><path d="M9 18V6l10-2v12"></path><circle cx="6.5" cy="18" r="2.5"></circle><circle cx="16.5" cy="16" r="2.5"></circle></symbol>
    <symbol id="ico-palette" viewBox="0 0 24 24"><path d="M12 3a9 9 0 1 0 0 18h1.2a2 2 0 0 0 1.5-3.3 1.8 1.8 0 0 1 1.4-3h1.1A3.8 3.8 0 0 0 21 10.9C21 6.5 17 3 12 3z"></path><circle cx="7.5" cy="11" r=".8"></circle><circle cx="10" cy="7.5" r=".8"></circle><circle cx="15" cy="7.5" r=".8"></circle></symbol>
    <symbol id="ico-image" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"></rect><circle cx="8.5" cy="9" r="1.5"></circle><path d="m21 15-5-5L5 20"></path></symbol>
    <symbol id="ico-type" viewBox="0 0 24 24"><path d="M4 7V4h16v3"></path><path d="M9 20h6"></path><path d="M12 4v16"></path></symbol>
    <symbol id="ico-text-size" viewBox="0 0 24 24"><path d="M4 8V5h12v3"></path><path d="M10 5v15"></path><path d="M7 20h6"></path><path d="M19 11v9M16.5 14h5"></path></symbol>
    <symbol id="ico-general" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1-2.7 2.7-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5v.2h-3.8v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1-2.7-2.7.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H2.5v-3.8h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1 2.7-2.7.1.1a1.7 1.7 0 0 0 1.8.3 1.7 1.7 0 0 0 1-1.5v-.2h3.8v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1 2.7 2.7-.1.1a1.7 1.7 0 0 0-.3 1.8 1.7 1.7 0 0 0 1.5 1h.2v3.8h-.2a1.7 1.7 0 0 0-1.5 1z"></path></symbol>
    <symbol id="ico-info" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><line x1="12" y1="11" x2="12" y2="16"></line><circle cx="12" cy="7.5" r=".6"></circle></symbol>
    <symbol id="ico-text" viewBox="0 0 24 24"><path d="M4 5h16"></path><path d="M4 10h16"></path><path d="M4 15h10"></path><path d="M4 20h8"></path></symbol>
    <symbol id="ico-template" viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="18" rx="2"></rect><path d="M8 8h8M8 12h8M8 16h4"></path></symbol>
    <symbol id="ico-clean" viewBox="0 0 24 24"><path d="m4 20 9-9"></path><path d="m13 11 4 4"></path><path d="m14 4 .6 1.8L16.5 6.5l-1.9.7L14 9l-.6-1.8-1.9-.7 1.9-.7z"></path><path d="m20 11 .4 1.1 1.1.4-1.1.4L20 14l-.4-1.1-1.1-.4 1.1-.4z"></path></symbol>
    <symbol id="ico-download" viewBox="0 0 24 24"><path d="M12 3v12"></path><path d="m7 10 5 5 5-5"></path><path d="M4 19h16"></path></symbol>
    <symbol id="ico-review" viewBox="0 0 24 24"><rect x="5" y="4" width="14" height="17" rx="2"></rect><path d="M9 4.5h6M8 11l2 2 4-4M8 17h8"></path></symbol>
    <symbol id="ico-device" viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2.5"></rect><path d="M10 18h4"></path></symbol>
    <symbol id="ico-voice" viewBox="0 0 24 24"><path d="M3 10v4h4l5 4V6l-5 4z"></path><path d="M16 9a5 5 0 0 1 0 6M18.5 6.5a9 9 0 0 1 0 11"></path></symbol>
    <symbol id="ico-speed" viewBox="0 0 24 24"><path d="M4.5 18a9 9 0 1 1 15 0"></path><path d="m12 13 4-4"></path><circle cx="12" cy="13" r="1.2"></circle><path d="M7 19h10"></path></symbol>
    <symbol id="ico-mask" viewBox="0 0 24 24"><path d="M3 12s3.3-6 9-6 9 6 9 6-3.3 6-9 6-9-6-9-6z"></path><circle cx="12" cy="12" r="2.5"></circle><path d="m4 4 16 16"></path></symbol>
    <symbol id="ico-github" viewBox="0 0 24 24"><path d="M8 19c-4 1.2-4-2-5.5-2.5M14 21v-3.1c0-.9.1-1.5-.4-2.1 3-.3 6.1-1.5 6.1-6.7 0-1.5-.5-2.7-1.4-3.7.1-.4.6-1.8-.1-3.6 0 0-1.2-.4-3.8 1.4a13 13 0 0 0-6.8 0C5 1.4 3.8 1.8 3.8 1.8 3.1 3.6 3.6 5 3.7 5.4a5.4 5.4 0 0 0-1.4 3.7c0 5.2 3.1 6.4 6.1 6.7-.4.5-.5 1.1-.5 2V21"></path></symbol>
    <symbol id="ico-external" viewBox="0 0 24 24"><path d="M14 4h6v6"></path><path d="m20 4-9 9"></path><path d="M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6"></path></symbol>
    <!-- Navigation / editing glyphs: text arrows vary by font and carry their
         own metrics, so generated rows use these instead. -->
    <symbol id="ico-up" viewBox="0 0 24 24"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></symbol>
    <symbol id="ico-pencil" viewBox="0 0 24 24"><path d="M12 20h9"></path><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"></path></symbol>
    <symbol id="ico-close" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></symbol>
    <symbol id="ico-chevron-right" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"></polyline></symbol>
    <symbol id="ico-chevron-down" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"></polyline></symbol>
    <symbol id="ico-folder" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></symbol>
    <symbol id="ico-zip" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="11" y1="4" x2="11" y2="6"></line><line x1="13" y1="6" x2="13" y2="8"></line><line x1="11" y1="8" x2="11" y2="10"></line><line x1="13" y1="10" x2="13" y2="12"></line><rect x="10.5" y="13" width="3" height="4" rx=".5"></rect></symbol>
</svg>
<div id="appToast" class="app-toast" role="status" aria-live="polite"></div>

<div class="container">
    <header class="app-header">
        <div class="badge">
            <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8h1a4 4 0 0 1 0 8h-1"></path><path d="M2 8h16v9a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V8z"></path><line x1="6" y1="1" x2="6" y2="4"></line><line x1="10" y1="1" x2="10" y2="4"></line><line x1="14" y1="1" x2="14" y2="4"></line></svg>
        </div>
        <div class="header-text">
            <h1>SD Card Coffee</h1>
            <div class="pill-row">
                <!-- One pill only: a status claim that can be wrong (Shizuku,
                     «كتابة الكارت») does not belong in the header. Both moved
                     to الإعدادات › عام, and the card one is now the result of a
                     real write test instead of a probe that can lie. -->
                <span class="pill num" id="appVersionPill">v{{ app_version }}</span>
            </div>
        </div>
        <button type="button" class="icon-btn header-settings" onclick="openSettings()" aria-label="الإعدادات" title="الإعدادات والمظهر">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
        </button>
    </header>

    <!-- The whole task in one row: which volume files come from, the arrow,
         which volume they go to. No headings and no fold: this block is read at
         a glance, and the folders themselves are chosen in the browser below. -->
    <section class="section" id="routeSection">
        <div class="route-row">
            <div class="storage-grid" id="source_storage_cards">
                {% for st in storages %}
                <button type="button" class="storage-card kind-{{ st.kind }}" data-path="{{ st.path }}" onclick="chooseStorage('source', this)" title="{{ st.name }} — {{ st.path }}">
                    <svg class="icon storage-ico"><use href="#{{ st.icon }}"></use></svg>
                    <span class="storage-short">{{ st.short }}</span>
                    <span class="storage-fresh" title="الفهرس جاهز — بحث فوري">
                        <svg viewBox="0 0 24 24" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>
                    </span>
                </button>
                {% endfor %}
            </div>
            <span class="route-arrow" aria-hidden="true">
                <svg class="icon" viewBox="0 0 24 24"><path d="M19 12H5"></path><polyline points="12 19 5 12 12 5"></polyline></svg>
            </span>
            <div class="storage-grid" id="dest_storage_cards">
                {% for st in storages %}
                <button type="button" class="storage-card kind-{{ st.kind }}" data-path="{{ st.path }}" onclick="chooseStorage('dest', this)" title="{{ st.name }} — {{ st.path }}">
                    <svg class="icon storage-ico"><use href="#{{ st.icon }}"></use></svg>
                    <span class="storage-short">{{ st.short }}</span>
                </button>
                {% endfor %}
            </div>
        </div>
        <div class="dest-line">
            <div class="dest-path" id="destPathRow" role="button" tabindex="0"
                 onclick="openPathBrowser('dest')"
                 onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openPathBrowser('dest');}"
                 title="اضغط لاختيار مجلد الحفظ">
                <svg class="icon" aria-hidden="true"><use href="#ico-folder"></use></svg>
                <input type="text" id="custom_dest_path" class="ltr-input" readonly tabindex="-1" placeholder="لم تُحدَّد بعد" aria-label="مجلد الحفظ — اضغط للاختيار">
                <svg class="icon dest-path-go" aria-hidden="true"><use href="#ico-chevron-right"></use></svg>
            </div>
        </div>

        <div class="disk-space-container" id="diskSpaceDiv" style="margin-top: 10px;">
            <div style="display:flex; justify-content:space-between;">
                <span id="diskText">جاري الحساب...</span>
                <span id="diskPercent">0% مستخدم</span>
            </div>
            <div class="disk-bar"><div class="disk-used" id="diskFill"></div></div>
        </div>
    </section>

    <section class="section">
        <div class="section-head">
            <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
            <h2 class="section-title">متصفح الملفات</h2>
            <span id="searchSpeedBadge" class="section-kicker" style="display:none; margin-inline-start:0; color:var(--accent-text);">
                <svg class="icon" style="width:12px;height:12px;" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg> فوري
            </span>
        </div>
        <!-- Search and browsing are one flow: the sort row, the search row and
             the optional folder picker sit on top of the tree that shows the
             results, so typing never leaves the input far from them. -->
        <div class="browser-head">
            <div class="browser-sort">
                <span style="font-weight:600;">ترتيب حسب:</span>
                <select id="sourceSort" style="background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:3px 8px; font-size:calc(12px * var(--font-scale));">
                    <option value="name">الاسم</option>
                    <option value="date_desc">التاريخ (الأحدث أولاً)</option>
                    <option value="date_asc">التاريخ (الأقدم أولاً)</option>
                </select>
            </div>
            <div id="searchControls">
                <div class="browser-search">
                    <input type="text" id="searchInput" placeholder="ابحث في مجلد البحث — ثم Enter" style="margin-bottom:0;" aria-label="نص البحث">
                    <button type="button" class="icon-btn search-tool" id="historyToggleBtn" onclick="toggleHistoryBlock()" title="سجل البحث: آخر النتائج والمجلدات التي حدّدتها" aria-label="سجل البحث">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><polyline points="12 7 12 12 16 14"></polyline></svg>
                    </button>
                    <button type="button" class="icon-btn search-tool" id="scopeToggleBtn" onclick="openSearchScope()" aria-haspopup="dialog" aria-controls="searchScopeModal" title="إعدادات البحث: المجلد والخيارات" aria-label="إعدادات البحث">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="7" x2="20" y2="7"></line><circle cx="9" cy="7" r="2.2"></circle><line x1="4" y1="17" x2="20" y2="17"></line><circle cx="15" cy="17" r="2.2"></circle></svg>
                    </button>
                </div>
            </div>
        </div>
        <div id="historyBlock" class="search-history" style="display:none;">
            <div class="history-head">
                <svg class="icon" style="width:14px;height:14px;color:var(--text-muted);" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><polyline points="12 7 12 12 16 14"></polyline></svg>
                <span class="history-title">آخر نتائج البحث</span>
                <button type="button" class="btn btn-outline" onclick="clearSearchHistory()">مسح الكل</button>
            </div>
            <div id="historyChips" class="history-chips"></div>
            <div id="historyRows" class="history-rows"></div>
        </div>
        <div class="tree" id="treeContainer" style="border-radius:0 0 10px 10px;"></div>
    </section>

    <section class="section">
        <div class="section-head">
            <svg class="icon" viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"></path></svg>
            <h2 class="section-title">أدوات</h2>
            <span class="section-kicker">اختياري</span>
        </div>
        <!-- The index is the one tool that matters: it gets the accent tint,
             and the instant-search index sits beside it — both are index
             actions, so they belong on one row. The card speed test moved to
             the destination picker, where the destination is already chosen. -->
        <div class="tools-actions">
            <button type="button" class="btn btn-joc" onclick="openJocModal()">
                <svg class="icon" aria-hidden="true"><use href="#ico-index"></use></svg>
                إنشاء فهرس JOC
            </button>
            <button type="button" class="btn btn-outline" id="cacheFolderBtn" onclick="cacheCurrentFolder()">
                <svg class="icon" aria-hidden="true"><use href="#ico-bolt"></use></svg>
                بحث فوري
            </button>
        </div>
        <div id="cacheStatusText" class="inline-note tools-note">جاري التحقق من الفهرس...</div>
        <details class="tools">
            <summary>
                <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"></circle><polyline points="12 7 12 12 16 14"></polyline></svg>
                <span>تمويه تواريخ الملفات</span>
                <svg class="icon tools-chev" viewBox="0 0 24 24" aria-hidden="true"><polyline points="6 9 12 15 18 9"></polyline></svg>
            </summary>
            <div class="tools-body">
                <label>تاريخ ووقت البدء العشوائي:</label>
                <input type="datetime-local" id="spoofDateInput">
                <small class="check-hint" style="margin:0;">عملية مستقلة لتعديل تواريخ الملفات والمجلدات لإخفاء آثار النقل، تُطبَّق على كامل الوجهة. في حال ترك الوجهة فارغة سيتم التمويه على كامل الذاكرة (/sdcard).</small>
                <button class="btn btn-sand" onclick="startSpoofOnly()">
                    تطبيق التمويه
                </button>
            </div>
        </details>
    </section>

    <!-- The one persistent control: what is selected, how big, how long it will
         take, and the single primary action. The progress of a running copy is
         mirrored on the 3px line above it. -->
    <div class="actionbar" id="actionBar">
        <div class="actionbar-progress" id="barProgressWrap" aria-hidden="true"><div class="actionbar-fill" id="barProgressFill"></div></div>
        <div class="actionbar-line">
            <button type="button" class="actionbar-summary" onclick="openSelectedModal()" aria-label="عرض الملفات المحددة" title="عرض الملفات المحددة وتنظيمها">
                <span class="actionbar-label">المحدد</span>
                <span class="actionbar-nums">
                    <b id="selectedCount" class="num">0</b>
                    <span class="actionbar-size num" id="barSize">—</span>
                    <span id="barQueueBadge" class="actionbar-eta" style="display:none;"></span>
                </span>
            </button>
            <span id="cartGroupPill" class="pill group-chip" role="button" tabindex="0" onclick="openSelectedModal()" onkeydown="if(event.key==='Enter')openSelectedModal()" style="display:none; cursor:pointer;"></span>
            <button class="btn btn-primary actionbar-go" id="barActionBtn" onclick="barPrimaryAction()">
                <svg class="icon" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
                <span id="barActionLabel">بدء النسخ</span>
            </button>
        </div>
    </div>
</div>

<!-- Modals -->
<div class="modal" id="destBrowserModal">
    <div class="modal-content modal-picker">
        <h3 id="pathBrowserTitle" style="margin-top:0; font-size: calc(16px * var(--font-scale)); border-bottom: 1px solid var(--border); padding-bottom: 15px;">اختر مجلد</h3>
        <div style="display:flex; gap: 8px; margin-bottom: 12px; margin-top: 10px;">
            <button class="btn btn-outline btn-sm" style="padding: 10px 14px;" onclick="goUpDest()">
                <svg class="icon" viewBox="0 0 24 24"><line x1="12" y1="19" x2="12" y2="5"></line><polyline points="5 12 12 5 19 12"></polyline></svg>
            </button>
            <input type="text" id="currentDestView" class="ltr-input" readonly style="margin: 0; background: var(--surface-field);">
        </div>
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:4px; min-height:22px;">
            <span id="pickerStorageBadge" class="pill" style="display:none;"></span>
            <span id="pickerCounts" style="font-size:calc(11.5px * var(--font-scale)); color:var(--text-muted);"></span>
        </div>
        <div class="picker-hint">المجلدات قابلة للاختيار • الملفات يمكن حذفها أو إعادة تسميتها</div>
        <div id="destTree" class="tree" style="flex: 1 1 auto; min-height: 0;"></div>
        <div style="display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; margin-top:15px; padding-top: 15px; border-top: 1px solid var(--border);">
            <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <button class="btn btn-outline btn-sm" onclick="createNewFolder()">+ مجلد جديد</button>
                <!-- The card check lives where the folder is already chosen: it
                     writes 32MB into this folder, so «هل الكارت يكتب؟» is answered
                     by a measurement, not by a status pill guessing from a stat. -->
                <button class="btn btn-outline btn-sm" id="cardCheckBtn" onclick="runDiagnostic()" title="يكتب 32 ميجابايت في المجلد المفتوح ويقيس السرعة الفعلية — هذه النتيجة هي التي تظهر في الإعدادات › عام">
                    <svg class="icon" aria-hidden="true"><use href="#ico-bolt"></use></svg>
                    فحص الكارت
                </button>
            </div>
            <div style="display:flex; gap: 10px;">
                <button class="btn btn-outline btn-sm" onclick="document.getElementById('destBrowserModal').style.display='none'">إلغاء</button>
                <button class="btn btn-primary btn-sm" onclick="confirmPathPicker()">تأكيد</button>
            </div>
        </div>
    </div>
</div>

<div class="modal" id="inputModal">
    <div class="modal-content" style="max-width: 420px;">
        <h3 id="inputModalTitle" style="margin-top:0; font-size: calc(16px * var(--font-scale)); border-bottom: 1px solid var(--border); padding-bottom: 15px; display:flex; align-items:center; gap:8px;">
            <svg class="icon" style="color:var(--accent);" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
            <span id="inputModalTitleText">إعادة تسمية</span>
        </h3>
        <label id="inputModalLabel">الاسم الجديد:</label>
        <input type="text" id="inputModalField" style="margin-bottom: 4px;" onkeydown="if(event.key==='Enter') submitInputModal()" oninput="hideInputModalError()">
        <div id="inputModalError" class="field-error"></div>
        <div style="display:flex; gap:10px; margin-top:14px;">
            <button class="btn btn-outline" style="flex:1;" onclick="document.getElementById('inputModal').style.display='none'">إلغاء</button>
            <button class="btn btn-primary" style="flex:1;" onclick="submitInputModal()">حفظ</button>
        </div>
    </div>
</div>

<div class="modal" id="confirmModal">
    <div class="modal-content" style="max-width: 420px;">
        <h3 style="margin-top:0; font-size: calc(16px * var(--font-scale)); border-bottom: 1px solid var(--border); padding-bottom: 15px;">تأكيد الإجراء</h3>
        <p id="confirmMessage" style="color: var(--text); font-size: calc(14px * var(--font-scale)); line-height: 1.6; margin: 4px 0 6px;"></p>
        <div style="display:flex; gap:10px; margin-top:14px;">
            <button class="btn btn-outline" style="flex:1;" onclick="cancelConfirm()">إلغاء</button>
            <button class="btn btn-danger" id="confirmActionBtn" style="flex:1;" onclick="submitConfirm()">تأكيد</button>
        </div>
    </div>
</div>

<div class="modal" id="deleteErrorModal">
    <div class="modal-content" style="max-width: 460px;">
        <h3 style="margin-top:0; font-size: calc(16px * var(--font-scale)); border-bottom: 1px solid var(--border); padding-bottom: 15px;">تعذر إكمال الحذف</h3>
        <p id="deleteErrorSummary" style="color: var(--text); font-size: calc(13.5px * var(--font-scale)); line-height: 1.6; margin: 4px 0 10px; white-space: pre-line;"></p>
        <div id="deleteErrorList" style="display:flex; flex-direction:column; gap:6px; overflow-y:auto; max-height: 36vh;"></div>
        <div style="display:flex; gap:10px; margin-top:14px;">
            <button class="btn btn-outline" style="flex:1;" onclick="document.getElementById('deleteErrorModal').style.display='none'">إغلاق</button>
            <button class="btn btn-primary" style="flex:1;" id="copyDeleteErrBtn" onclick="copyDeleteErrors()">نسخ التفاصيل</button>
        </div>
    </div>
</div>

<div class="modal" id="selectedModal" onclick="closeSelectedModal(event)">
    <div class="modal-content modal-selected" onclick="event.stopPropagation()">
        <div class="selected-head">
            <div class="selected-title-row">
                <h3 style="margin:0; font-size: calc(16px * var(--font-scale));">الملفات المحددة</h3>
                <div class="selected-operation-actions">
                    <button type="button" id="saveOperationBtn" class="btn btn-outline btn-sm"
                            title="حفظ التحديد والتنظيم" onclick="openSaveOperationDialog('create')">حفظ</button>
                    <button type="button" class="btn btn-outline btn-sm" title="العمليات المحفوظة"
                            onclick="openSavedOperationsModal()">المحفوظة</button>
                    <button type="button" id="updateSavedOperationBtn" class="btn btn-outline btn-sm"
                            style="display:none;" title="تحديث العملية المحفوظة من التحديد الحالي"
                            onclick="updateActiveSavedOperation()">تحديث</button>
                    <button class="btn btn-outline" style="padding: 6px 10px; border:none; box-shadow: none;" aria-label="إغلاق" onclick="document.getElementById('selectedModal').style.display='none'">
                    <svg class="icon" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
                    </button>
                </div>
            </div>
            <div id="savedOperationActiveLabel" class="saved-operation-active" style="display:none;"></div>
            <div id="totalSizeSelected" style="font-size:calc(13px * var(--font-scale)); font-weight:500; background:var(--surface-field); padding:12px; border-radius:10px; border: 1px solid var(--border);">جاري حساب المساحة...</div>

            <div class="group-section">
                <div class="group-section-head">
                    <div class="group-section-title">
                        <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path><line x1="12" y1="11" x2="12" y2="17"></line><line x1="9" y1="14" x2="15" y2="14"></line></svg>
                        <span>مجلدات النسخ</span>
                    </div>
                    <div class="group-section-actions">
                        <button type="button" class="btn btn-outline btn-sm" onclick="smartOrganizeGroups()" title="يقترح مجلداً لكل مسار أب، للمحدد فقط"><svg class="icon" aria-hidden="true"><use href="#ico-spark"></use></svg> تنظيم ذكي</button>
                        <button type="button" class="btn btn-outline btn-sm" onclick="startNewGroup()"><svg class="icon" aria-hidden="true"><use href="#ico-plus"></use></svg> مجلد جديد</button>
                    </div>
                </div>
            <div id="groupStatusHint" class="check-hint" role="status" aria-live="polite"
                 aria-atomic="true" style="display:none; margin:0;"></div>
                <div id="smartPreview" class="smart-preview" style="display:none;"></div>
                <div id="groupTree" class="group-tree"></div>
                <div id="groupEmptyHint" class="group-empty-hint">لا توجد مجلدات. أنشئ مجلداً رئيسياً أو فرعياً، ثم اسحب العناصر إليه أو حدّدها وأسندها معاً.</div>
            </div>
        </div>

        <div id="selectedList" class="selected-list"></div>
    </div>
</div>

<div class="modal" id="savedOperationsModal" onclick="closeSavedOperationsModal(event)">
    <div class="modal-content modal-saved-operations" onclick="event.stopPropagation()">
        <h3 style="margin-top:0; font-size:calc(16px * var(--font-scale));">العمليات المحفوظة</h3>
        <div class="settings-note">تُحفظ العناصر والاستثناءات ومجلدات النسخ فقط. اختر الوجهة عند بدء كل نسخة.</div>
        <div id="savedOperationsStatus" class="settings-save-state" role="status"
             aria-live="polite" aria-atomic="true" aria-busy="false"></div>
        <div id="savedOperationsList" class="saved-operations-list"></div>
        <div class="modal-footer">
            <button type="button" class="btn btn-outline" style="flex:1;" onclick="closeSavedOperationsModal()">إغلاق</button>
        </div>
    </div>
</div>

<div class="modal" id="saveOperationNameModal" onclick="closeSaveOperationDialog(event)">
    <div class="modal-content modal-save-operation-name" onclick="event.stopPropagation()">
        <h3 id="saveOperationNameTitle" style="margin-top:0; font-size:calc(16px * var(--font-scale));">حفظ عملية</h3>
        <label for="saveOperationNameInput">اسم يميز العملية</label>
        <input type="text" id="saveOperationNameInput" maxlength="80" autocomplete="off"
               placeholder="مثال: ألبوم العائلة" onkeydown="if(event.key==='Enter') submitSaveOperationName()">
        <div id="saveOperationNameError" class="settings-save-state err" role="alert"></div>
        <div class="modal-footer">
            <button type="button" class="btn btn-outline" style="flex:1;" onclick="closeSaveOperationDialog()">إلغاء</button>
            <button type="button" class="btn btn-primary" style="flex:1;" id="saveOperationNameSubmit"
                    onclick="submitSaveOperationName()">حفظ</button>
        </div>
    </div>
</div>

<!-- Folder assignment: the touch-friendly twin of drag & drop. It can handle
     one item or a multi-selection from the selected-items list. -->
<div class="modal" id="assignGroupModal" onclick="closeAssignGroup(event)">
    <div class="modal-content" style="max-width:420px;" onclick="event.stopPropagation()">
        <h3 style="margin-top:0; font-size:calc(16px * var(--font-scale)); border-bottom:1px solid var(--border); padding-bottom:14px;">مجلد النسخ</h3>
        <div id="assignPathName" class="settings-note" style="margin-bottom:10px; direction:ltr; text-align:left; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></div>
        <div id="assignGroupList" class="assign-list"></div>
        <div class="assign-new">
            <input type="text" id="assignNewName" placeholder="مجلد جديد..." maxlength="80" autocomplete="off">
            <button type="button" class="btn btn-primary btn-sm" onclick="createAssignGroup()">إنشاء</button>
        </div>
        <div id="assignError" class="settings-save-state err"></div>
        <div class="modal-footer">
            <button type="button" class="btn btn-outline" style="flex:1;" onclick="closeAssignGroup()">إغلاق</button>
        </div>
    </div>
</div>

<!-- Choose a parent folder when nesting an existing copy folder. -->
<div class="modal" id="groupParentModal" onclick="closeGroupParentPicker(event)">
    <div class="modal-content" style="max-width:420px;" onclick="event.stopPropagation()">
        <h3 style="margin-top:0; font-size:calc(16px * var(--font-scale)); border-bottom:1px solid var(--border); padding-bottom:14px;">نقل المجلد إلى</h3>
        <div id="groupParentSummary" class="settings-note" style="margin-bottom:10px;"></div>
        <div id="groupParentList" class="group-parent-list"></div>
        <div id="groupParentError" class="settings-save-state err"></div>
        <div class="modal-footer">
            <button type="button" class="btn btn-outline" style="flex:1;" onclick="closeGroupParentPicker()">إغلاق</button>
        </div>
    </div>
</div>

<!-- Settings Modal -->
<div class="modal" id="settingsModal">
    <div class="modal-content modal-settings">
        <h3 style="margin-top:0; border-bottom: 1px solid var(--border); padding-bottom: 15px; display:flex; align-items:center; gap:8px; flex-shrink:0;">
            <svg class="icon" style="color:var(--accent);" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
            الإعدادات
        </h3>
        <div class="settings-tabs">
            <button type="button" class="settings-tab active" data-tab="appearance" onclick="showSettingsTab('appearance')"><svg class="icon" aria-hidden="true"><use href="#ico-palette"></use></svg>المظهر</button>
            <button type="button" class="settings-tab" data-tab="general" onclick="showSettingsTab('general')"><svg class="icon" aria-hidden="true"><use href="#ico-general"></use></svg>عام</button>
            <button type="button" class="settings-tab" data-tab="index" onclick="showSettingsTab('index')"><svg class="icon" aria-hidden="true"><use href="#ico-index"></use></svg>الفهرس</button>
            <button type="button" class="settings-tab" data-tab="about" onclick="showSettingsTab('about')"><svg class="icon" aria-hidden="true"><use href="#ico-info"></use></svg>حول</button>
        </div>
        <div class="modal-body">
            <div class="settings-pane" id="settingsPaneAppearance">
                <div class="settings-group">
                    <div class="settings-field">
                        <label for="themeSelect"><svg class="icon" aria-hidden="true"><use href="#ico-palette"></use></svg>السمة</label>
                        <select id="themeSelect" onchange="pickTheme(this.value)">
                            {% for t in themes %}
                            <option value="{{ t.id }}" data-theme-id="{{ t.id }}" data-swatches="{{ t.swatches|join(',') }}"{% if t.id == active_theme.id %} selected{% endif %}>{{ t.name }}</option>
                            {% endfor %}
                        </select>
                        <div class="theme-preview-row">
                            <div id="themeColorPreview" class="theme-preview" role="img"
                                 aria-label="معاينة ألوان السمة {{ active_theme.name }}">
                                {% for swatch in active_theme.swatches %}
                                <span class="theme-preview-chip" title="{{ ['الخلفية', 'السطح', 'التمييز', 'التخزين الداخلي', 'بطاقة SD', 'النص'][loop.index0] }}"
                                      style="background-color:{{ swatch }};"></span>
                                {% endfor %}
                            </div>
                            <span id="themePreviewName" class="field-hint">{{ active_theme.name }}</span>
                        </div>
                    </div>
                </div>
                <div class="settings-group">
                    <div class="settings-group-title"><svg class="icon" aria-hidden="true"><use href="#ico-image"></use></svg>خلفية التطبيق</div>
                    <label class="settings-check-row" for="photoBackgroundEnabled">
                        <input type="checkbox" id="photoBackgroundEnabled" class="checkbox" disabled onchange="setPhotoBackgroundEnabled(this.checked)">
                        <span class="settings-option-copy">
                            <svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-image"></use></svg>
                            <span>إظهار الصورة خلف الصفحة
                                <small class="settings-note">تُحفظ الصورة في هذا المتصفح فقط ولا تُرفع إلى الخادم.</small>
                            </span>
                        </span>
                    </label>
                    <div class="photo-background-controls">
                        <input type="file" id="photoBackgroundFile" accept="image/png,image/jpeg,image/webp" hidden aria-label="اختيار صورة خلفية" onchange="handlePhotoBackgroundFile(this)">
                        <div class="photo-background-actions">
                            <button type="button" class="btn btn-outline" onclick="choosePhotoBackground()">اختيار صورة</button>
                            <button type="button" class="btn btn-outline" id="removePhotoBackgroundBtn" disabled onclick="removePhotoBackground()">إزالة الصورة</button>
                        </div>
                        <img id="photoBackgroundPreview" class="photo-background-preview" alt="معاينة صورة الخلفية" hidden>
                        <div class="settings-field">
                            <label for="photoOverlayRange">تعتيم الطبقة فوق الصورة</label>
                            <div class="photo-opacity-row">
                                <input type="range" id="photoOverlayRange" min="0" max="85" step="5" value="60" disabled oninput="setPhotoOverlayOpacity(this.value)">
                                <span id="photoOverlayValue" class="photo-opacity-value">60%</span>
                            </div>
                            <div class="field-hint">القيمة الأعلى تخفف ظهور الصورة وتزيد وضوح المحتوى.</div>
                        </div>
                        <div class="settings-field">
                            <label for="surfaceAlphaRange">شفافية الأسطح</label>
                            <div class="photo-opacity-row">
                                <input type="range" id="surfaceAlphaRange" min="30" max="100" step="5" value="100" oninput="setSurfaceAlpha(this.value)">
                                <span id="surfaceAlphaValue" class="photo-opacity-value">100%</span>
                            </div>
                            <div class="field-hint">100% تعني الأسطح كما هي. قلّل القيمة لتظهر الخلفية عبر البطاقات والقوائم وشريط الإجراءات.</div>
                        </div>
                        <div id="photoBackgroundState" class="settings-save-state" role="status" aria-live="polite">اختر صورة PNG أو JPEG أو WebP بحجم لا يتجاوز 8 ميجابايت.</div>
                    </div>
                </div>
                <div class="settings-group">
                    <div class="settings-field">
                        <label for="fontSelect"><svg class="icon" aria-hidden="true"><use href="#ico-type"></use></svg>الخط</label>
                        <select id="fontSelect" onchange="pickFont(this.value)">
                            {% for f in fonts %}
                            <option value="{{ f.id }}" data-font-id="{{ f.id }}"{% if f.id == active_font %} selected{% endif %}>{{ f.name }}</option>
                            {% endfor %}
                        </select>
                        <div id="fontPreview" class="settings-note" style="font-family: {{ app_font }};">سورة البقرة ١٢٣</div>
                    </div>
                    <div class="settings-field">
                        <label><svg class="icon" aria-hidden="true"><use href="#ico-text-size"></use></svg>حجم الخط</label>
                        <div class="size-seg" id="sizeSeg">
                            {% for s in font_sizes %}
                            <button type="button" class="size-seg-btn{% if s.id == active_font_size %} is-active{% endif %}" data-size-id="{{ s.id }}" onclick="pickFontSize('{{ s.id }}')">{{ s.name }}</button>
                            {% endfor %}
                        </div>
                    </div>
                </div>
                <div id="settingsSaveState" class="settings-save-state"></div>
            </div>

            <!-- General settings: what happens after a copy. Typography lives in
                 «المظهر» next to the theme it visually belongs with. -->
            <div class="settings-pane" id="settingsPaneGeneral" style="display:none;">
                <div class="settings-group">
                    <div class="settings-group-title"><svg class="icon" aria-hidden="true"><use href="#ico-bolt"></use></svg>بعد النسخ</div>
                    <label class="settings-check-row" for="autoSpoofCheck">
                        <input type="checkbox" id="autoSpoofCheck" class="checkbox" {% if auto_spoof %}checked{% endif %} onchange="pickAutoSpoof(this.checked)">
                        <span class="settings-option-copy">
                            <svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-mask"></use></svg>
                            <span>تمويه تواريخ الوجهة تلقائياً
                                <small class="settings-note">بعد التحقق، ويشمل الوجهة كاملة.</small>
                            </span>
                        </span>
                    </label>
                    <label class="settings-check-row" for="verifyAfterCopy">
                        <input type="checkbox" id="verifyAfterCopy" class="checkbox" onchange="localStorage.setItem('sdmp3_verify_after_copy', this.checked ? '1' : '0')">
                        <span class="settings-option-copy">
                            <svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-check"></use></svg>
                            <span>التحقق السريع من الحجم بعد النسخ
                                <small class="settings-note">يقارن حجم كل ملف دون إعادة قراءة محتواه.</small>
                            </span>
                        </span>
                    </label>
                    <label class="settings-check-row" for="verifyContentAfterCopy">
                        <input type="checkbox" id="verifyContentAfterCopy" class="checkbox" onchange="localStorage.setItem('sdmp3_verify_content_after_copy', this.checked ? '1' : '0')">
                        <span class="settings-option-copy">
                            <svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-shield"></use></svg>
                            <span>مقارنة محتوى الملفات (أبطأ)
                                <small class="settings-note">يقرأ المصدر والنسخة ويقارن SHA-256 لاكتشاف اختلاف المحتوى.</small>
                            </span>
                        </span>
                    </label>
                    <label class="settings-check-row" for="browserNotifyCheck">
                        <input type="checkbox" id="browserNotifyCheck" class="checkbox" onchange="pickBrowserNotifications(this.checked)">
                        <span class="settings-option-copy">
                            <svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-bell"></use></svg>
                            <span>إشعار المتصفح عند اكتمال النسخ أو الخطأ
                                <small class="settings-note">يطلب المتصفح الإذن عند التفعيل، أو عند أول عملية نسخ.</small>
                            </span>
                        </span>
                    </label>
                    <label class="settings-check-row" for="completionSoundCheck">
                        <input type="checkbox" id="completionSoundCheck" class="checkbox" checked onchange="pickCompletionSound(this.checked)">
                        <span class="settings-option-copy">
                            <svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-sound"></use></svg>
                            <span>صوت عند انتهاء النسخ أو الخطأ
                                <small class="settings-note">نغمة قصيرة من المتصفح، بلا ملفات أو روابط خارجية.</small>
                            </span>
                        </span>
                    </label>
                    <div id="notifySaveState" class="settings-save-state"></div>
                    <div id="generalSaveState" class="settings-save-state"></div>
                </div>

                <!-- Device state lives here, never in the header: Shizuku is
                     optional, and «الكارت» is only claimed after a real write
                     test. -->
                <div class="settings-group">
                    <div class="settings-group-title"><svg class="icon" aria-hidden="true"><use href="#ico-device"></use></svg>الجهاز</div>
                    <div class="status-list">
                        <div class="status-row">
                            <svg class="icon" style="width:15px;height:15px;color:var(--accent);" aria-hidden="true"><use href="#ico-shield"></use></svg>
                            <span class="status-label">Shizuku</span>
                            <span class="status-value" id="generalShizukuStatus">جاري الفحص…</span>
                        </div>
                        <div class="status-row">
                            <svg class="icon" style="width:15px;height:15px;color:var(--kind-sdcard);" aria-hidden="true"><use href="#ico-sd"></use></svg>
                            <span class="status-label">الكارت</span>
                            <span class="status-value" id="generalCardStatus">لم يُفحص بعد</span>
                        </div>
                    </div>
                    <div class="settings-note">Shizuku اختياري. الكارت يُقاس بفحص فعلي من نافذة الوجهة.</div>
                </div>
            </div>

            <!-- Index settings. These are the *saved* values: the create dialog
                 only names the file and shows a summary of this pane. The same
                 pane doubles as a throw-away draft when the JOC button opens it. -->
            <div class="settings-pane" id="settingsPaneIndex" style="display:none;">
                <div id="indexDraftBanner" class="draft-banner" style="display:none;">
                    <svg class="icon" aria-hidden="true"><use href="#ico-alert"></use></svg>
                    <span>إعدادات هذه المرة فقط — لن تُحفظ.</span>
                </div>
                <div class="index-form" style="gap:8px;">
                    <div class="index-group">
                        <div class="index-group-title">
                            <svg class="icon" aria-hidden="true"><use href="#ico-text"></use></svg>
                            النص
                        </div>
                        <div class="index-fields-grid">
                            <div>
                                <label class="index-field-label" for="jocStart"><svg class="icon" aria-hidden="true"><use href="#ico-index"></use></svg>البداية</label>
                                <input type="number" id="jocStart" value="1" min="1">
                            </div>
                            <div>
                                <label class="index-field-label" for="jocLimit"><svg class="icon" aria-hidden="true"><use href="#ico-text-size"></use></svg>أقصى عدد حروف</label>
                                <input type="number" id="jocLimit" value="93" min="10">
                            </div>
                        </div>
                        <div class="index-fields-grid">
                            <div class="settings-field">
                                <label class="index-field-label" for="jocMode"><svg class="icon" aria-hidden="true"><use href="#ico-folder"></use></svg>الوضع</label>
                                <select id="jocMode">
                                    <option value="1">مجلدات فقط</option>
                                    <option value="2">مجلدات وملفات</option>
                                    <option value="3">المجلد الحالي فقط</option>
                                </select>
                            </div>
                            <div class="settings-field">
                                <label class="index-field-label" for="jocTemplate"><svg class="icon" aria-hidden="true"><use href="#ico-template"></use></svg>التنسيق</label>
                                <select id="jocTemplate">
                                    <option value="4">الاسم : الرقم.</option>
                                    <option value="1">الرقم : الاسم .</option>
                                    <option value="2">الاسم من الرقم</option>
                                    <option value="3">الاسم من رقم الرقم .</option>
                                </select>
                            </div>
                        </div>
                        <div class="index-options-grid">
                            <label class="settings-check-row index-check-row" for="jocStrip">
                                <input type="checkbox" id="jocStrip" class="checkbox" checked>
                                <span class="settings-option-copy"><svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-clean"></use></svg><span>حذف الحروف الإنجليزية والأرقام والرموز</span></span>
                            </label>
                            <label class="settings-check-row index-check-row" for="jocDownloadTxt">
                                <input type="checkbox" id="jocDownloadTxt" class="checkbox">
                                <span class="settings-option-copy"><svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-download"></use></svg><span>تحميل ملف النص (.txt)</span></span>
                            </label>
                            <label class="settings-check-row index-check-row" for="jocReviewTxt">
                                <input type="checkbox" id="jocReviewTxt" class="checkbox">
                                <span class="settings-option-copy"><svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-review"></use></svg><span>مراجعة النص قبل المتابعة</span></span>
                            </label>
                        </div>
                    </div>

                    <div class="index-group">
                        <div class="index-group-title">
                            <svg class="icon" aria-hidden="true"><use href="#ico-music"></use></svg>
                            الصوت
                        </div>
                        <label class="settings-check-row index-check-row" for="jocMp3Index">
                            <input type="checkbox" id="jocMp3Index" class="checkbox" onchange="toggleMp3Index()">
                            <span class="settings-option-copy"><svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-music"></use></svg><span>تحويل الفهرس إلى MP3</span></span>
                        </label>
                        <div id="ttsNotice"></div>
                        <div id="ttsOptions" style="display:none; gap:10px; flex-direction:column;">
                            <label class="settings-check-row index-check-row" for="jocMp3ToDest">
                                <input type="checkbox" id="jocMp3ToDest" class="checkbox">
                                <span class="settings-option-copy"><svg class="icon settings-option-icon" aria-hidden="true"><use href="#ico-sd"></use></svg><span>حفظ MP3 في الوجهة<small class="settings-note">بدل تنزيله على الجهاز.</small></span></span>
                            </label>
                            <div class="index-fields-grid">
                                <div>
                                    <label class="index-field-label" for="ttsVoice"><svg class="icon" aria-hidden="true"><use href="#ico-voice"></use></svg>الصوت</label>
                                    <select id="ttsVoice"></select>
                                </div>
                                <div>
                                    <label class="index-field-label" for="ttsRate"><svg class="icon" aria-hidden="true"><use href="#ico-speed"></use></svg>السرعة</label>
                                    <select id="ttsRate">
                                        <option value="+0%">عادية (+0%)</option>
                                        <option value="-10%">أبطأ (-10%)</option>
                                        <option value="-20%">أبطأ بكثير (-20%)</option>
                                        <option value="+10%">أسرع (+10%)</option>
                                        <option value="+20%">أسرع بكثير (+20%)</option>
                                    </select>
                                </div>
                            </div>

                            <div>
                                <div class="index-subtitle"><svg class="icon" aria-hidden="true"><use href="#ico-mask"></use></svg>تمويه الصوت</div>
                                <div style="display:flex; gap:6px; background:var(--surface-field); border:1px solid var(--border); border-radius:10px; padding:4px;">
                                    <label class="seg option" data-mode="none" style="flex:1; text-align:center; padding:9px 6px; border-radius:7px; cursor:pointer; font-size:calc(12.5px * var(--font-scale)); color:var(--text-muted); display:flex; align-items:center; justify-content:center; gap:5px;">
                                        <svg class="icon" style="width:15px;height:15px;" viewBox="0 0 24 24"><path d="M11 5 6 9H2v6h4l5 4V5z"></path></svg>
                                        بدون
                                    </label>
                                    <label class="seg option" data-mode="noise" style="flex:1; text-align:center; padding:9px 6px; border-radius:7px; cursor:pointer; font-size:calc(12.5px * var(--font-scale)); color:var(--text-muted); display:flex; align-items:center; justify-content:center; gap:5px;">
                                        <svg class="icon" style="width:15px;height:15px;" viewBox="0 0 24 24"><line x1="1" y1="12" x2="6" y2="12"></line><line x1="23" y1="12" x2="18" y2="12"></line><line x1="9" y1="9" x2="15" y2="15"></line><line x1="15" y1="9" x2="9" y2="15"></line></svg>
                                        ضوضاء
                                    </label>
                                    <label class="seg option" data-mode="file" style="flex:1; text-align:center; padding:9px 6px; border-radius:7px; cursor:pointer; font-size:calc(12.5px * var(--font-scale)); color:var(--text-muted); display:flex; align-items:center; justify-content:center; gap:5px;">
                                        <svg class="icon" style="width:15px;height:15px;" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>
                                        ملف
                                    </label>
                                </div>
                                <small id="maskHint" class="settings-note" style="display:block; margin-top:6px;"></small>

                                <div id="customFileBox" style="display:none; margin-top:8px;">
                                    <div style="display:flex; align-items:center; gap:10px; background:var(--surface-field); border:1px dashed var(--border); border-radius:10px; padding:10px 12px;">
                                        <input type="file" id="jocCustomAudio" accept=".mp3,audio/mpeg" hidden>
                                        <button type="button" class="btn btn-outline btn-sm" style="white-space:nowrap; flex-shrink:0;" onclick="document.getElementById('jocCustomAudio').click()">
                                            <svg class="icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
                                            اختر ملف MP3
                                        </button>
                                        <span id="customAudioName" style="flex:1; font-size:calc(12.5px * var(--font-scale)); color:var(--text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">لم يتم اختيار ملف بعد</span>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <div id="indexSaveActions" style="display:flex; gap:10px; align-items:center;">
                    <button type="button" id="saveIndexSettingsBtn" class="btn btn-primary" style="flex:1;" onclick="saveIndexSettingsNow()">حفظ إعدادات الفهرس</button>
                </div>
                <div id="indexSaveState" class="settings-save-state"></div>
            </div>

            <div class="settings-pane" id="settingsPaneAbout" style="display:none;">
                <div class="about-card">
                    <div class="about-brand">
                        <div class="about-mark"><svg class="icon" aria-hidden="true"><use href="#ico-sd"></use></svg></div>
                        <div>
                            <div class="about-brand-name">SD Card Coffee</div>
                            <div class="about-brand-caption">إدارة النسخ والفهارس من جهازك</div>
                        </div>
                    </div>
                    <div class="about-version">
                        <span class="about-version-label"><svg class="icon" aria-hidden="true"><use href="#ico-info"></use></svg>الإصدار</span>
                        <span class="about-version-value num">v{{ app_version }}</span>
                    </div>
                    <div>
                        <div class="about-local-note">
                            <svg class="icon" aria-hidden="true"><use href="#ico-device"></use></svg>
                            <span>إدارة الملفات والنسخ والإعدادات تتم محليًا على الجهاز، دون حساب أو تخزين سحابي.</span>
                        </div>
                        <div class="about-online-note">تحويل النص إلى MP3 يحتاج اتصالًا بالإنترنت عند استخدامه.</div>
                    </div>
                    <a class="about-github-link" href="https://github.com/ilyas1435"
                       target="_blank" rel="noopener noreferrer" aria-label="حساب GitHub لـ ilyas1435">
                        <svg class="icon" aria-hidden="true"><use href="#ico-github"></use></svg>
                        <span>ilyas1435</span>
                        <svg class="icon external-icon" aria-hidden="true"><use href="#ico-external"></use></svg>
                    </a>
                    <div class="about-state-path">
                        تحفظ ملفات الحالة على الجهاز في
                        <span class="ltr-input" style="display:inline-block; padding:1px 6px;">~/.local/state/sdmp3/</span>
                    </div>
                </div>
            </div>
        </div>
        <div style="display:flex; gap:10px; margin-top:14px; flex-shrink:0;">
            <button id="settingsCloseBtn" class="btn btn-outline" style="flex:1;" onclick="closeSettingsModal()">إغلاق</button>
            <div id="indexDraftActions" style="display:none; flex:1; gap:10px;">
                <button type="button" class="btn btn-outline" style="flex:1;" onclick="cancelJocDraft()">إلغاء</button>
                <button type="button" class="btn btn-primary" style="flex:1.4;" onclick="continueJocDraft()">متابعة إلى الإنشاء</button>
            </div>
        </div>
    </div>
</div>

<!-- JOC create dialog. The *settings* live in الإعدادات › الفهرس, so this is
     only the output name plus a read-out of what the saved settings will do. -->
<div class="modal" id="jocModal">
    <div class="modal-content">
        <h3 style="margin-top:0; border-bottom: 1px solid var(--border); padding-bottom: 15px; display:flex; align-items:center; gap:8px; flex-shrink:0;">
            <svg class="icon" style="color:var(--accent);" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path></svg>
            إنشاء فهرس JOC
        </h3>
        <div class="modal-body">
        <label>اسم الملف الناتج (نفس الاسم للنص والصوت):</label>
        <input type="text" id="jocFileName" class="ltr-input" placeholder="اسم الملف...">
        <small style="color:var(--text-muted); display:block; margin-top:-6px; margin-bottom:14px;">يُولَّد اسم عشوائي من 5 أحرف عند كل فتح، ويمكن تعديله الآن. لا يُحفظ بين المرات.</small>

        <div class="joc-summary" id="jocSummary"></div>
        </div>
        <div class="modal-footer">
            <button class="btn btn-outline" style="flex:1;" onclick="openIndexSettings()">
                <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1.08 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
                الإعدادات
            </button>
            <button class="btn btn-outline" style="flex:1;" onclick="document.getElementById('jocModal').style.display='none'">إلغاء</button>
            <button class="btn btn-primary" style="flex:1.4;" onclick="submitJocIndex()">إنشاء</button>
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
    <div class="modal-content">
        <div class="modal-head-row">
            <h2 id="modalTitle">العملية قيد التنفيذ...</h2>
            <button type="button" class="icon-btn" id="minimizeProgressBtn" onclick="minimizeProgressModal()" aria-label="تصغير النافذة" title="تصغير ومتابعة تصفح الموقع">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
            </button>
        </div>
        <div style="display:flex; justify-content:space-between; font-size:calc(13px * var(--font-scale)); color:var(--text-muted); font-weight: 500; margin-top: 10px;">
            <span id="progressText">0%</span>
            <span id="countText">0 / 0</span>
        </div>
        <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
        <div id="currentFile" class="ltr-input truncate-text" style="font-size:calc(12px * var(--font-scale)); color:var(--text-muted); background:transparent; border:none; padding:0; width: 100%;"></div>
        <div style="display:flex; justify-content:space-between; font-size:calc(12px * var(--font-scale)); color:var(--text-muted); margin-top:8px;">
            <span id="speedText" style="color:var(--accent); font-weight:500;"></span>
            <span id="failedText"></span>
        </div>
        <div id="cardWarningBox" style="display:none; margin-top:10px; padding:10px 12px; border-radius:10px; font-size:calc(12px * var(--font-scale)); line-height:1.7; background:var(--warning-soft); border:1px solid var(--warning-line); color:var(--warning);"></div>
        <div id="errorsBox" role="region" aria-label="تفاصيل الأخطاء والملاحظات" tabindex="0" style="display:none; background:var(--surface-field); border:1px solid var(--border); border-radius:8px; padding:11px 12px; margin-top:10px; font-size:calc(12.5px * var(--font-scale)); color:var(--danger-ink);"></div>
        <button type="button" class="btn btn-outline btn-sm" id="copyTransferDetailsBtn" onclick="copyTransferDetails()" style="display:none; align-self:flex-start; margin-top:10px;">نسخ التفاصيل</button>
        <div id="timeTakenDiv" class="report" style="display:none;"></div>
        <div id="queueBlock" class="queue-block" style="display:none;">
            <div class="queue-head">
                <span class="queue-title">قائمة الانتظار</span>
                <span id="queueCount" class="num"></span>
            </div>
            <div id="queueRows" class="queue-rows"></div>
        </div>
        <div id="inlineNotify" style="display:none; flex-wrap:wrap; margin-top:14px; padding:12px 14px; border-radius:10px; font-size:calc(13.5px * var(--font-scale)); line-height:1.7; text-align:center; font-weight:600; border:1px solid; align-items:center; justify-content:center; gap:8px;"></div>
        <div style="display:flex; gap:12px; margin-top:20px;">
            <button class="btn btn-danger" id="cancelProgressBtn" style="width:100%; padding: 12px;" onclick="cancelTransfer()">إيقاف وإلغاء</button>
            <button class="btn btn-primary" id="closeProgressBtn" style="display:none; width:100%; padding: 12px;" onclick="resetTransfer()">تم وإغلاق</button>
        </div>
    </div>
</div>

<!-- Where the search runs. It is its own value (`searchScopePath`) and its own
     dialog: narrowing a search must never move the browsed folder. The folder
     is chosen exactly like the destination — one path field plus «تحديد» that
     opens the same picker — instead of a path line with a second sub-folder
     dropdown. -->
<div class="modal" id="searchScopeModal" onclick="closeSearchScope(event)">
    <div class="modal-content" style="max-width:460px;">
        <h3 style="margin-top:0; font-size:calc(16px * var(--font-scale)); border-bottom:1px solid var(--border); padding-bottom:15px;">إعدادات البحث</h3>
        <div class="settings-note">البحث يجري داخل هذا المجلد وحده، ولا يغيّر مجلد التصفح.</div>
        <div class="scope-line" style="margin-top:12px;">
            <input type="text" id="searchScopePath" class="ltr-input" readonly value="/sdcard" aria-label="مجلد البحث الحالي" style="margin:0;">
        </div>
        <div class="scope-line" style="margin-top:8px;">
            <button type="button" class="btn btn-outline btn-sm" onclick="searchScopeUp()" title="مجلد أعلى">
                <svg class="icon" aria-hidden="true"><use href="#ico-up"></use></svg> أعلى
            </button>
            <button type="button" class="btn btn-outline btn-sm" onclick="openPathBrowser('search')" title="اختيار مجلد البحث من نفس نافذة الوجهة">
                <svg class="icon" aria-hidden="true"><use href="#ico-folder"></use></svg> تحديد مجلد
            </button>
        </div>
        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color:var(--text-muted); font-size:calc(13px * var(--font-scale)); margin:14px 0 0;">
            <input type="checkbox" id="foldersOnlyCheck" class="checkbox" checked style="width:16px; height:16px; margin:0;">
            بحث في أسماء المجلدات فقط
        </label>
        <div style="display:flex; gap:10px; margin-top:16px;">
            <button class="btn btn-outline" style="flex:1;" onclick="closeSearchScope()">إغلاق</button>
        </div>
    </div>
</div>

<script src="/static/sdmp3_group_ops.js"></script>
<script>
    let selectedPaths = new Map();
    let excludedPaths = new Set();
    let bulkSelectedPaths = new Set();
    let activeSavedOperationId = '';
    let activeSavedOperationName = '';
    let savedOperationLoadingId = '';
    let saveOperationDialogMode = 'create';
    let saveOperationDialogId = '';
    // Copy groups: [{id, name, paths: []}]. A selected path belongs to at most
    // one group, and each group becomes one folder inside the destination when
    // the copy starts - the source tree is never modified.
    let copyGroups = [];
    let nextGroupId = 1;
    let selectedSizes = {};      // path -> {bytes, formatted, owner}
    let selectedBrowseExpanded = new Set();
    let selectedBrowseCache = new Map();
    let selectedBrowseLoading = new Set();
    let selectedBrowseErrors = new Map();
    let selectionSizeTimer = null;   // debounce for the action bar's size
    let selectionSizeSig = '';       // selection the cached sizes belong to
    let currentDestFreeBytes = 0; 
    let statusTimeout = null;
    let currentSourceRoot = localStorage.getItem('joc_source_root') || '/sdcard';
    let currentSort = localStorage.getItem('sdmp3_source_sort') || 'name';
    let pickerMode = 'dest'; 

    // Fast size verification stays on by default; full content comparison is
    // opt-in because it reads both source and destination after copying.
    (function restoreCopyPrefs() {
        let saved = localStorage.getItem('sdmp3_verify_after_copy');
        let el = document.getElementById('verifyAfterCopy');
        if (el) el.checked = (saved === null) ? true : (saved === '1');
        let contentSaved = localStorage.getItem('sdmp3_verify_content_after_copy');
        let contentEl = document.getElementById('verifyContentAfterCopy');
        if (contentEl) contentEl.checked = contentSaved === '1';
    })();

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
        document.getElementById('selectedCount').textContent = String(selectedPaths.size);
        pruneSelectedBrowseState();
        if (selectedPaths.size === 0) {
            // A group only means something for the current selection.
            copyGroups = [];
            bulkSelectedPaths.clear();
            selectedSizes = {};
            // The bar must not keep the previous selection's size or ETA.
            selectionSizeSig = '';
            updateSizeDisplay(0);
        } else {
            syncGroupsWithSelection();
            // The bar repeats the size, and the picker window is closed most of
            // the time: ask for it here, not only from «عرض المحدد».
            scheduleSelectionSize();
        }
        renderGroupUI();
        updateSavedOperationControls();
    }

    // ---- selection size behind the action bar -------------------------
    // One debounced request for the whole selection, reusing /api/sizes (the
    // server walks each tree once and drops nested duplicates). Checking twenty
    // boxes must not fire twenty requests, and an unchanged signature is skipped.
    function selectionSignature() {
        return Array.from(selectedPaths.keys()).sort().join('\\n') + '|' +
               Array.from(excludedPaths).sort().join('\\n');
    }

    function scheduleSelectionSize() {
        if (selectionSizeTimer) { clearTimeout(selectionSizeTimer); selectionSizeTimer = null; }
        if (!selectedPaths.size) return;
        let sig = selectionSignature();
        if (sig === selectionSizeSig) { updateSelectedTotal(Array.from(selectedPaths.keys())); return; }
        selectionSizeTimer = setTimeout(function() { fetchSelectionSize(sig); }, 400);
    }

    function fetchSelectionSize(sig) {
        selectionSizeTimer = null;
        if (!selectedPaths.size) return;
        let pathsArray = Array.from(selectedPaths.keys());
        fetch('/api/sizes', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({paths: pathsArray, excludes: Array.from(excludedPaths)})
        }).then(function(r) { return r.json(); }).then(function(data) {
            // A newer selection may have landed while this was in flight.
            if (selectionSignature() !== sig) return;
            selectionSizeSig = sig;
            let sizes = data.sizes || {};
            pathsArray.forEach(function(path) {
                selectedSizes[path] = sizes[path] || {bytes: 0, formatted: '0 B', owner: path};
            });
            updateSelectedTotal(pathsArray);
        }).catch(function() {});
    }

    // ---- Copy groups: named parent folders created at copy time only ----
    // A group is {id, name, paths}. `name` is a breadcrumb ("A/B"): nesting is
    // a plain path, so there is no second tree model to keep in sync and the
    // server (sdmp3_groups.prepare_copy_groups) reads exactly this shape.
    function syncGroupsWithSelection() {
        copyGroups.forEach(function(g) {
            g.paths = g.paths.filter(function(p) { return selectedPaths.has(p); });
        });
        bulkSelectedPaths.forEach(function(path) {
            if (!selectedPaths.has(path)) bulkSelectedPaths.delete(path);
        });
    }

    function findGroup(gid) {
        for (let i = 0; i < copyGroups.length; i++) {
            if (copyGroups[i].id === gid) return copyGroups[i];
        }
        return null;
    }

    function ensureGroupParents(name) {
        let parts = groupPathParts(name);
        for (let i = 1; i < parts.length; i++) {
            let parentName = parts.slice(0, i).join('/');
            if (!copyGroups.some(function(group) {
                return groupPathKey(group.name) === groupPathKey(parentName);
            })) {
                copyGroups.push({id: nextGroupId++, name: parentName, paths: []});
            }
        }
    }

    function groupNameForPath(path) {
        for (let i = 0; i < copyGroups.length; i++) {
            if (copyGroups[i].paths.indexOf(path) !== -1) return copyGroups[i].name;
        }
        return '';
    }

    // FAT is case-insensitive, so "علوم" and "علوم " collide. The backslash is
    // built with fromCharCode: the page is a plain Python string, and a raw
    // backslash here would be eaten before the browser ever sees it.
    // The '/' is NOT in this set: it is the nesting separator, handled below.
    const GROUP_BAD_CHARS = String.fromCharCode(92) + ':*?"<>|';
    const GROUP_MAX_DEPTH = 3;
    const GROUP_PATH_MAX = 180;
    const GROUP_SEG_MAX = 80;
    // "العلماء / ابن باز // دروس" -> ['العلماء','ابن باز','دروس']
    function groupPathParts(raw) {
        return String(raw || '').split('/').map(function(s) { return s.trim(); })
            .filter(function(s) { return s.length > 0; });
    }

    function groupPathKey(raw) {
        return groupPathParts(raw).join('/').toLowerCase();
    }

    // One directory *name* (not a path): the rules the server applies to a
    // segment are checked here first so the UI can refuse before the copy.
    function groupSegmentError(seg) {
        if (!seg) return 'اكتب اسماً للمجلد';
        if (seg === '.' || seg === '..') return 'اسم غير صالح';
        if (seg.length > GROUP_SEG_MAX) return 'طول المقطع أكثر من ' + GROUP_SEG_MAX + ' حرفاً';
        for (let j = 0; j < GROUP_BAD_CHARS.length; j++) {
            if (seg.indexOf(GROUP_BAD_CHARS[j]) !== -1) return 'لا تستخدم الرموز : * ? " < > |';
        }
        if (/[ .]$/.test(seg)) return 'لا ينتهي الاسم بمسافة أو نقطة';
        return '';
    }

    function groupNameError(raw, exceptId) {
        let name = (raw || '').trim();
        if (!name) return 'اكتب اسماً للمجلد';
        if (name.length > GROUP_PATH_MAX) return 'المسار طويل جداً (الحد ' + GROUP_PATH_MAX + ' حرفاً)';
        let parts = groupPathParts(name);
        if (!parts.length) return 'اكتب اسماً للمجلد';
        if (parts.length > GROUP_MAX_DEPTH) return 'أقصى تداخل ' + GROUP_MAX_DEPTH + ' مستويات (مثال: العلماء/ابن باز)';
        for (let i = 0; i < parts.length; i++) {
            let err = groupSegmentError(parts[i]);
            if (err) return err;
        }
        let key = parts.join('/').toLowerCase();
        for (let i = 0; i < copyGroups.length; i++) {
            let g = copyGroups[i];
            if (g.id !== exceptId && groupPathKey(g.name) === key) return 'يوجد مجلد بنفس الاسم';
        }
        return '';
    }

    // "A/B" -> "A › B": the read-out keeps the real nesting, not a slash soup.
    function groupBreadcrumbHtml(name) {
        return groupPathParts(name).map(function(seg) { return esc(seg); })
            .join(' <span class="group-crumb-sep">›</span> ');
    }

    function setGroupStatus(text, kind) {
        let el = document.getElementById('groupStatusHint');
        if (!el) return;
        el.textContent = text || '';
        el.className = 'check-hint' + (kind ? ' ' + kind : '');
        el.style.display = text ? 'block' : 'none';
    }

    // ---- the folder tree -------------------------------------------------
    // Nodes are derived from the breadcrumbs on every render: "A" and "A/B"
    // share one branch, so a nested group looks like a folder inside a folder.
    // Only *closed* nodes are remembered: a new folder must appear expanded,
    // or the user would think it was not created.
    let groupNodeClosed = {};

    function isNodeOpen(key) { return !groupNodeClosed[groupPathKey(key)]; }

    function toggleGroupNode(key) {
        let k = groupPathKey(key);
        if (groupNodeClosed[k]) delete groupNodeClosed[k];
        else groupNodeClosed[k] = true;
        renderGroupTree();
    }

    // Folder paths are rendered parent-first so nested groups read as a tree.
    function groupRows() {
        return copyGroups.slice().sort(function(a, b) {
            let ap = groupPathParts(a.name);
            let bp = groupPathParts(b.name);
            if (ap.length !== bp.length) return ap.length - bp.length;
            for (let i = 0; i < Math.min(ap.length, bp.length); i++) {
                let order = ap[i].localeCompare(bp[i], 'ar');
                if (order) return order;
            }
            return 0;
        });
    }

    let groupRename = null;   // {gid, value} while a folder name is edited inline

    function renameInputHtml(group) {
        return '<input type="text" class="gt-rename" id="gtRenameInput" autocomplete="off" ' +
            'maxlength="' + GROUP_SEG_MAX + '" value="' + esc(groupRename ? groupRename.value : '') + '" ' +
            'aria-label="اسم المجلد">';
    }

    function groupRowActionsHtml(group) {
        let gid = group.id;
        if (groupRename && groupRename.gid === gid) {
            return '<button type="button" class="gt-btn" data-act="rename-save" title="حفظ (Enter)">' +
                    spriteIcon('ico-check') + '</button>' +
                '<button type="button" class="gt-btn danger" data-act="rename-cancel" title="إلغاء (Esc)">' +
                    spriteIcon('ico-close') + '</button>';
        }
        return '<button type="button" class="gt-btn" data-act="child" title="إنشاء مجلد فرعي داخل هذا المجلد" aria-label="إنشاء مجلد فرعي">' +
                spriteIcon('ico-plus') + '</button>' +
            '<button type="button" class="gt-btn" data-act="move" title="تغيير مستوى المجلد أو إخراجه للمستوى الرئيسي" aria-label="تغيير مستوى المجلد">' +
                spriteIcon('ico-folder') + '</button>' +
            '<button type="button" class="gt-btn" data-act="rename" title="إعادة التسمية">' +
                spriteIcon('ico-pencil') + '</button>' +
            '<button type="button" class="gt-btn danger" data-act="delete" title="حذف المجلد">' +
                '<svg class="icon" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path></svg></button>';
    }

    function groupPathsHtml(paths, gid) {
        if (!paths.length) return '<div class="gt-empty">مجلد فارغ — اسحب إليه عناصر.</div>';
        let names = {};
        paths.forEach(function(p) { names[p] = p.split('/').filter(Boolean).pop() || p; });
        return '<div class="gt-paths">' + paths.map(function(p) {
            return '<div class="gt-path" draggable="true" data-path="' + esc(p) + '">' +
                (selectedPaths.get(p) ? iconDir : iconFile) +
                '<span class="gt-path-name" title="' + esc(p) + '">' + esc(names[p]) + '</span>' +
                '<button type="button" class="gt-path-x" data-act="unlink" data-path="' + esc(p) +
                    '" title="أخرجه من المجلد">' + spriteIcon('ico-close') + '</button>' +
            '</div>';
        }).join('') + '</div>';
    }

    function groupRowHtml(g) {
        let gid = g.id;
        let key = groupPathKey(g.name);
        let open = isNodeOpen(key);
        let renaming = !!(groupRename && groupRename.gid === gid);
        let parts = groupPathParts(g.name);
        let depth = Math.max(0, parts.length - 1);
        let leaf = parts.slice(-1)[0] || g.name;
        let label = esc(leaf);
        return '<div class="gt-node" data-key="' + esc(key) + '" data-depth="' + depth + '">' +
            '<div class="gt-row' + (renaming ? ' is-renaming' : '') + '" data-gid="' + gid +
                '" data-key="' + esc(key) + '" data-drop="1" draggable="true" style="margin-inline-start:' +
                (depth * 14) + 'px">' +
                '<button type="button" class="gt-twisty' + (open ? ' open' : '') +
                    '" data-act="toggle" data-key="' + esc(key) + '" aria-expanded="' +
                    (open ? 'true' : 'false') + '" aria-label="' +
                    (open ? 'طي المجلد' : 'فتح المجلد') + '">' +
                    spriteIcon(open ? 'ico-chevron-down' : 'ico-chevron-right') +
                '</button>' +
                '<svg class="gt-folder icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>' +
                (renaming ? renameInputHtml(g)
                          : '<span class="gt-name" data-act="toggle" data-key="' + esc(key) +
                            '" title="' + esc(g.name) + '" aria-label="' + esc(g.name) + '">' + label + '</span>') +
                '<span class="gt-count">' + g.paths.length + '</span>' +
                '<div class="gt-actions">' + groupRowActionsHtml(g) + '</div>' +
            '</div>' +
            (open ? groupPathsHtml(g.paths, gid) : '') +
        '</div>';
    }

    function renderGroupTree() {
        let el = document.getElementById('groupTree');
        if (!el) return;
        let empty = document.getElementById('groupEmptyHint');
        if (empty) empty.style.display = copyGroups.length ? 'none' : 'block';
        if (!copyGroups.length && !smartPreview) { el.innerHTML = ''; return; }
        let visible = groupRows().filter(function(group) {
            let parts = groupPathParts(group.name);
            for (let i = 1; i < parts.length; i++) {
                let parentName = parts.slice(0, i).join('/');
                let parent = copyGroups.find(function(candidate) {
                    return groupPathKey(candidate.name) === groupPathKey(parentName);
                });
                if (parent && !isNodeOpen(parent.name)) return false;
            }
            return true;
        });
        el.innerHTML = visible.map(groupRowHtml).join('') +
            (copyGroups.length
                ? '<div class="gt-root-drop" data-root-drop="1"><svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#ico-up"></use></svg><span>اسحب المجلد هنا لإخراجه إلى المستوى الرئيسي</span></div>'
                : '');
        bindGroupRenameInput();
    }

    function bindGroupRenameInput() {
        let input = document.getElementById('gtRenameInput');
        if (!input || !groupRename) return;
        input.value = groupRename.value;
        input.focus();
        try { input.setSelectionRange(input.value.length, input.value.length); } catch (_e) {}
        input.addEventListener('input', function() {
            groupRename.value = input.value;
            let err = groupRenameError(input.value);
            let save = document.querySelector('#groupTree button[data-act="rename-save"]');
            if (save) save.disabled = !!err;
            setGroupStatus(err || 'Enter يحفظ، Esc يلغي.');
        });
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); commitGroupRename(); }
            else if (e.key === 'Escape') { e.preventDefault(); cancelGroupRename(); }
        });
    }

    // The error for renaming the *last* segment: a nested folder keeps its
    // parent path, and every descendant folder moves with it.
    function groupRenameError(seg) {
        let g = findGroup(groupRename ? groupRename.gid : 0);
        if (!g) return 'المجلد غير موجود';
        seg = (seg || '').trim();
        let err = groupSegmentError(seg);
        if (err) return err;
        // Renaming a folder renames its descendants too. A leaf may contain '/'
        // (the nesting separator), so check the resulting chain depth as well —
        // otherwise the copy route would reject the group after the fact.
        let depthErr = window.SDMP3GroupOps.renameDepthError(copyGroups, g, seg);
        if (depthErr) return depthErr;
        let parts = groupPathParts(g.name);
        let parentParts = parts.slice(0, -1);
        let oldKey = groupPathKey(g.name);
        let mine = {};
        let proposals = [];
        copyGroups.forEach(function(other) {
            let k = groupPathKey(other.name);
            if (k === oldKey || k.indexOf(oldKey + '/') === 0) {
                mine[other.id] = true;
                proposals.push(parentParts.concat([seg])
                    .concat(groupPathParts(other.name).slice(parts.length)));
            }
        });
        for (let i = 0; i < proposals.length; i++) {
            let full = proposals[i].join('/');
            if (full.length > GROUP_PATH_MAX) return 'المسار طويل جداً (الحد ' + GROUP_PATH_MAX + ' حرفاً)';
            let key = full.toLowerCase();
            for (let j = 0; j < copyGroups.length; j++) {
                if (mine[copyGroups[j].id]) continue;
                if (groupPathKey(copyGroups[j].name) === key) return 'يوجد مجلد بنفس الاسم';
            }
        }
        return '';
    }

    function startRenameGroup(gid) {
        let g = findGroup(gid);
        if (!g) return;
        groupRename = {gid: gid, value: groupPathParts(g.name).slice(-1)[0]};
        renderGroupTree();
        setGroupStatus('Enter يحفظ، Esc يلغي.');
    }

    function cancelGroupRename() {
        groupRename = null;
        setGroupStatus('');
        renderGroupTree();
    }

    function commitGroupRename() {
        if (!groupRename) return;
        let g = findGroup(groupRename.gid);
        if (!g) { cancelGroupRename(); return; }
        let seg = (groupRename.value || '').trim();
        let err = groupRenameError(seg);
        if (err) { setGroupStatus(err); return; }
        let parts = groupPathParts(g.name);
        let oldKey = groupPathKey(g.name);
        let base = parts.slice(0, -1).concat([seg]);
        // A renamed folder renames every nested group under it, otherwise the
        // child would point at a parent path that no longer exists.
        copyGroups.forEach(function(other) {
            let k = groupPathKey(other.name);
            if (k === oldKey || k.indexOf(oldKey + '/') === 0) {
                other.name = base.concat(groupPathParts(other.name).slice(parts.length)).join('/');
            }
        });
        groupRename = null;
        setGroupStatus('');
        renderGroupUI();
        setGroupStatus('تم تغيير الاسم.');
    }

    function uniqueGroupName(base, parentName) {
        let n = 1;
        while (n < 500) {
            let leaf = base + ' ' + n;
            let name = parentName ? parentName + '/' + leaf : leaf;
            if (groupNameError(name, null) === '') return name;
            n++;
        }
        let leaf = base + ' ' + Date.now();
        return parentName ? parentName + '/' + leaf : leaf;
    }

    // Selecting the plus button on a group creates a child directly inside it.
    // The inline rename edits only the new folder's last path segment.
    function startNewGroup(parentGid) {
        let parent = parentGid ? findGroup(parentGid) : null;
        if (parentGid && !parent) return;
        if (parent && groupPathParts(parent.name).length >= GROUP_MAX_DEPTH) {
            setGroupStatus('أقصى تداخل ' + GROUP_MAX_DEPTH + ' مستويات.');
            return;
        }
        let name = uniqueGroupName('مجلد', parent ? parent.name : '');
        let g = {id: nextGroupId++, name: name, paths: []};
        copyGroups.push(g);
        if (parent) delete groupNodeClosed[groupPathKey(parent.name)];
        // Rename validation accepts one folder segment, not the full breadcrumb.
        // Passing "A/مجلد 1" here made every new child fail unless the user
        // manually erased its parent path before saving.
        groupRename = {gid: g.id, value: window.SDMP3GroupOps.leafName(name)};
        renderGroupUI();
        setGroupStatus('اكتب اسماً ثم Enter.');
    }

    function deleteGroup(gid) {
        let g = findGroup(gid);
        if (!g) return;
        // Deleting a folder never unselects its files: it only puts them back
        // in "بدون مجلد". A nested folder goes with its parent, because "A/B"
        // without "A" would be a path with no folder of its own.
        let key = groupPathKey(g.name);
        copyGroups = copyGroups.filter(function(other) {
            let k = groupPathKey(other.name);
            return !(k === key || k.indexOf(key + '/') === 0);
        });
        renderGroupUI();
        setGroupStatus('حُذف المجلد.');
    }

    function movePathsToGroup(paths, gid) {
        let wanted = Array.from(new Set(paths || [])).filter(function(path) {
            return selectedPaths.has(path);
        });
        if (!wanted.length) return false;
        let result = window.SDMP3GroupOps.assignPaths(
            copyGroups, wanted, gid, Array.from(selectedPaths.keys()));
        if (result.error) { setGroupStatus(result.error); return false; }
        copyGroups = result.groups;
        // These checkboxes are only the temporary bulk-action selection. Keep
        // the source paths selected for copying, but clear the assignment batch
        // so the next operation starts with an intentional selection.
        bulkSelectedPaths.clear();
        if (gid) {
            let target = findGroup(gid);
            if (!target) return false;
            setGroupStatus('تم إسناد ' + result.moved + ' عنصر إلى «' +
                groupPathParts(target.name).join(' › ') + '».');
        } else {
            setGroupStatus('تم إخراج ' + result.moved + ' عنصر من المجلد.');
        }
        renderGroupUI();
        updateBulkSelectionControls();
        return true;
    }

    function movePathToGroup(path, gid) {
        movePathsToGroup([path], gid);
    }

    function moveGroupToParent(sourceId, parentId) {
        let result = window.SDMP3GroupOps.moveGroup(copyGroups, sourceId, parentId);
        if (result.error) { setGroupStatus(result.error); return false; }
        if (!result.moved) {
            setGroupStatus('المجلد موجود بالفعل في هذا المستوى.');
            return false;
        }
        copyGroups = result.groups;
        renderGroupUI();
        let moved = findGroup(sourceId);
        let parent = parentId ? findGroup(parentId) : null;
        setGroupStatus(parent
            ? 'نُقل المجلد إلى «' + groupPathParts(parent.name).join(' › ') + '».'
            : 'نُقل المجلد إلى المستوى الرئيسي.');
        return !!moved;
    }

    let groupParentSourceId = 0;

    function closeGroupParentPicker(event) {
        if (event && event.target && event.target.id !== 'groupParentModal') return;
        let modal = document.getElementById('groupParentModal');
        if (modal) modal.style.display = 'none';
        groupParentSourceId = 0;
    }

    function openGroupParentPicker(gid) {
        let source = findGroup(gid);
        if (!source) return;
        groupParentSourceId = gid;
        let summary = document.getElementById('groupParentSummary');
        if (summary) summary.textContent = 'اختَر المجلد الأب لـ «' +
            groupPathParts(source.name).join(' › ') + '».';
        let error = document.getElementById('groupParentError');
        if (error) error.textContent = '';
        renderGroupParentList();
        let modal = document.getElementById('groupParentModal');
        if (modal) modal.style.display = 'flex';
    }

    function renderGroupParentList() {
        let list = document.getElementById('groupParentList');
        let source = findGroup(groupParentSourceId);
        if (!list || !source) return;
        let sourceKey = groupPathKey(source.name);
        let sourceParent = groupPathParts(source.name).slice(0, -1).join('/');
        let rows = ['<button type="button" class="assign-row' +
            (!sourceParent ? ' is-active' : '') + '" data-parent-id="0">' +
            spriteIcon('ico-folder') + '<span>' +
            (sourceParent ? 'إخراج إلى المستوى الرئيسي' : 'المستوى الرئيسي') +
            '</span></button>'];
        groupRows().forEach(function(candidate) {
            let candidateKey = groupPathKey(candidate.name);
            if (candidate.id === source.id || candidateKey.indexOf(sourceKey + '/') === 0) return;
            let active = candidateKey === groupPathKey(sourceParent);
            rows.push('<button type="button" class="assign-row' + (active ? ' is-active' : '') +
                '" data-parent-id="' + candidate.id + '">' + spriteIcon('ico-folder') +
                '<span class="truncate-text">' + groupBreadcrumbHtml(candidate.name) + '</span>' +
                '<span class="assign-count">' + candidate.paths.length + '</span></button>');
        });
        list.innerHTML = rows.join('');
    }

    function chooseGroupParent(parentId) {
        if (!groupParentSourceId) return;
        if (moveGroupToParent(groupParentSourceId, parentId)) closeGroupParentPicker();
        else {
            let error = document.getElementById('groupStatusHint');
            let box = document.getElementById('groupParentError');
            if (box && error) box.textContent = error.textContent;
        }
    }

    // Dropping one row on a folder row moves it.
    function clearGroupDropTargets() {
        let targets = document.querySelectorAll('#groupTree .is-drop');
        for (let i = 0; i < targets.length; i++) targets[i].classList.remove('is-drop');
    }

    // ---- «تنظيم ذكي»: propose one folder per source parent, preview it ----
    // The two old failure modes were selections that sit directly in the source
    // root (no common parent) and selections from different parents (a single
    // "common parent" dumped them all together). Here every item is mapped to
    // its own parent path relative to the source root, so both work, and the
    // proposal is shown before anything is written.
    let smartPreview = null;   // [{name, paths, checked, existing}]

    function sourceBasename() {
        let parts = String(currentSourceRoot || '').split('/').filter(Boolean);
        return parts.length ? parts[parts.length - 1] : 'المصدر';
    }

    function sourceRelativeParent(path) {
        let base = String(currentSourceRoot || '').replace(/\\/+$/, '');
        let p = String(path || '').replace(/\\/+$/, '');
        let rel = p;
        if (base && (p === base || p.indexOf(base + '/') === 0)) rel = p.slice(base.length);
        let parts = rel.split('/').filter(Boolean);
        parts.pop();   // the item's own name: we want the folder holding it
        return parts;
    }

    // Every segment must be a legal FAT name and the whole path must fit the
    // limits the server enforces; a value that cannot be fixed is dropped and
    // the caller falls back to the source folder's own name.
    function clampGroupSegments(parts) {
        // Keep the closest source folders, within the app's three-level limit.
        return window.SDMP3GroupOps.clampSegments(parts, GROUP_MAX_DEPTH);
    }

    function buildSmartProposal() {
        let buckets = {};
        let order = [];
        let existing = {};
        copyGroups.forEach(function(g) { existing[groupPathKey(g.name)] = true; });
        Array.from(selectedPaths.keys()).forEach(function(path) {
            let segs = clampGroupSegments(sourceRelativeParent(path));
            if (!segs.length) segs = clampGroupSegments([sourceBasename()]);
            if (!segs.length) segs = ['المصدر'];
            let name = segs.join('/');
            let key = name.toLowerCase();
            if (!buckets[key]) {
                buckets[key] = {name: name, paths: [], checked: true, existing: !!existing[key]};
                order.push(key);
            }
            buckets[key].paths.push(path);
        });
        return order.map(function(k) { return buckets[k]; });
    }

    function smartOrganizeGroups() {
        if (!selectedPaths.size) { setGroupStatus('حدّد عنصراً أولاً.'); return; }
        smartPreview = buildSmartProposal();
        if (!smartPreview.length) { setGroupStatus('لم أجد مساراً مناسباً لهذه العناصر.'); return; }
        setGroupStatus('');
        renderSmartPreview();
    }

    function renderSmartPreview() {
        let el = document.getElementById('smartPreview');
        if (!el) return;
        if (!smartPreview) { el.style.display = 'none'; el.innerHTML = ''; return; }
        let chosen = smartPreview.filter(function(p) { return p.checked; }).length;
        let rows = smartPreview.map(function(p, i) {
            let meta = (p.paths.length === 1 ? 'عنصر واحد' : p.paths.length + ' عناصر') +
                (p.existing ? ' · موجود' : ' · جديد');
            return '<label class="smart-preview-row' + (p.checked ? '' : ' is-off') + '" data-row="' + i + '">' +
                '<input type="checkbox" class="checkbox" data-idx="' + i + '" value="preview"' +
                    (p.checked ? ' checked' : '') + '>' +
                '<span class="smart-preview-name truncate-text" title="' + esc(p.name) + '">' +
                    groupBreadcrumbHtml(p.name) + '</span>' +
                '<span class="smart-preview-meta">' + esc(meta) + '</span>' +
            '</label>';
        }).join('');
        el.innerHTML = '<div class="smart-preview-head"><span>مقترح للمحدد</span>' +
                '<span class="num">' + smartPreview.length + '</span></div>' +
            '<div class="smart-preview-list">' + rows + '</div>' +
            '<div class="smart-preview-actions">' +
                '<button type="button" class="btn btn-primary btn-sm" data-act="apply"' +
                    (chosen ? '' : ' disabled') + '>' + (chosen ? 'تطبيق على ' + chosen + ' مجلد' : 'لا شيء محدد') + '</button>' +
                '<button type="button" class="btn btn-outline btn-sm" data-act="cancel">إلغاء</button>' +
            '</div>';
        el.style.display = 'flex';
    }

    function toggleSmartPreview(index, checked) {
        if (!smartPreview || !smartPreview[index]) return;
        smartPreview[index].checked = checked;
        let el = document.getElementById('smartPreview');
        if (!el) return;
        let row = el.querySelector('[data-row="' + index + '"]');
        if (row) row.classList.toggle('is-off', !checked);
        let n = smartPreview.filter(function(p) { return p.checked; }).length;
        let apply = el.querySelector('button[data-act="apply"]');
        if (apply) {
            apply.disabled = !n;
            apply.textContent = n ? 'تطبيق على ' + n + ' مجلد' : 'لا شيء محدد';
        }
    }

    function cancelSmartPreview() {
        smartPreview = null;
        renderSmartPreview();
        setGroupStatus('');
    }

    function applySmartPreview() {
        if (!smartPreview) return;
        let chosen = smartPreview.filter(function(p) { return p.checked; });
        if (!chosen.length) { setGroupStatus('لم تختر شيئاً للتطبيق.'); return; }
        // Whatever this proposal claims leaves its previous folder first: one
        // item lives in exactly one group.
        let moving = {};
        chosen.forEach(function(p) {
            p.paths.forEach(function(x) { moving[x] = true; });
        });
        copyGroups.forEach(function(g) {
            g.paths = g.paths.filter(function(p) { return !moving[p]; });
        });
        chosen.forEach(function(p) {
            let key = p.name.toLowerCase();
            ensureGroupParents(p.name);
            let target = null;
            for (let i = 0; i < copyGroups.length; i++) {
                if (groupPathKey(copyGroups[i].name) === key) { target = copyGroups[i]; break; }
            }
            if (!target) {
                target = {id: nextGroupId++, name: p.name, paths: []};
                copyGroups.push(target);
            }
            let set = new Set(target.paths);
            p.paths.forEach(function(x) { set.add(x); });
            target.paths = Array.from(selectedPaths.keys()).filter(function(path) {
                return set.has(path);
            });
        });
        smartPreview = null;
        renderSmartPreview();
        renderGroupUI();
        setGroupStatus('تم التنظيم في ' + chosen.length + ' مجلد — راجع الشجرة قبل النسخ.');
    }

    function renderGroupPill() {
        let pill = document.getElementById('cartGroupPill');
        if (!pill) return;
        let used = copyGroups.filter(function(g) { return g.paths.length > 0; });
        if (!selectedPaths.size || !used.length) { pill.style.display = 'none'; return; }
        let names = used.map(function(g) { return groupPathParts(g.name).join(' › '); }).join('، ');
        pill.style.display = 'inline-block';
        pill.textContent = (used.length === 1 ? 'مجلد: ' : 'مجلدات: ') + names;
        pill.title = 'سيتم إنشاء «' + names + '» داخل الوجهة — اضغط للعرض والتعديل';
    }

    function renderGroupUI() {
        renderGroupPill();
        scheduleUiSave();
        let modal = document.getElementById('selectedModal');
        if (!modal || modal.style.display !== 'flex') return;
        renderGroupTree();
        renderSelectedRows();
    }

    // ---- folder assignment sheet ------------------------------------------
    // The folder chip on a selected file opens this list. It is the
    // touch-friendly way to do what drag & drop does on a desktop.
    let assignPaths = [];

    function closeAssignGroup(event) {
        if (event && event.target && event.target.id !== 'assignGroupModal') return;
        let modal = document.getElementById('assignGroupModal');
        if (modal) modal.style.display = 'none';
        assignPaths = [];
    }

    function openAssignGroup(paths) {
        let requested = Array.isArray(paths) ? paths : [paths];
        assignPaths = Array.from(new Set(requested)).filter(function(path) {
            return selectedPaths.has(path);
        });
        if (!assignPaths.length) return;
        let label = document.getElementById('assignPathName');
        if (label) {
            label.textContent = assignPaths.length === 1
                ? assignPaths[0] : 'تعيين ' + assignPaths.length + ' عناصر معاً';
            label.title = assignPaths.length === 1 ? assignPaths[0] : assignPaths.join('\\n');
        }
        let err = document.getElementById('assignError');
        if (err) err.textContent = '';
        let input = document.getElementById('assignNewName');
        if (input) input.value = '';
        renderAssignGroupList();
        let modal = document.getElementById('assignGroupModal');
        if (modal) modal.style.display = 'flex';
    }

    function renderAssignGroupList() {
        let list = document.getElementById('assignGroupList');
        if (!list) return;
        let currentNames = assignPaths.map(groupNameForPath);
        let current = currentNames.length && currentNames.every(function(name) {
            return groupPathKey(name) === groupPathKey(currentNames[0]);
        }) ? currentNames[0] : '';
        let rows = ['<button type="button" class="assign-row' +
            (currentNames.length && currentNames.every(function(name) { return !name; }) ? ' is-active' : '') +
            '" data-gid="0">' +
            spriteIcon('ico-close') + '<span>بدون مجلد</span><span class="assign-count">في الوجهة</span></button>'];
        groupRows().forEach(function(g) {
            let active = current && groupPathKey(current) === groupPathKey(g.name);
            rows.push('<button type="button" class="assign-row' + (active ? ' is-active' : '') +
                '" data-gid="' + g.id + '">' + spriteIcon('ico-folder') +
                '<span class="truncate-text">' + groupBreadcrumbHtml(g.name) + '</span>' +
                '<span class="assign-count">' + g.paths.length + '</span></button>');
        });
        list.innerHTML = rows.join('');
    }

    function assignPathsToGroup(gid) {
        if (!assignPaths.length) return;
        if (movePathsToGroup(assignPaths, gid)) closeAssignGroup();
    }

    function createAssignGroup() {
        if (!assignPaths.length) return;
        let input = document.getElementById('assignNewName');
        let name = ((input && input.value) || '').trim();
        let err = groupSegmentError(name);
        if (err) {
            let box = document.getElementById('assignError');
            if (box) box.textContent = err;
            return;
        }
        if (groupNameError(name, null)) {
            let box = document.getElementById('assignError');
            if (box) box.textContent = 'يوجد مجلد بنفس الاسم';
            return;
        }
        let g = {id: nextGroupId++, name: name, paths: []};
        copyGroups.push(g);
        if (movePathsToGroup(assignPaths, g.id)) closeAssignGroup();
        else {
            copyGroups = copyGroups.filter(function(group) { return group.id !== g.id; });
            renderGroupUI();
        }
    }

    // One delegated listener for the tree: no group name or path is ever
    // interpolated into an inline onclick.
    let groupTreeEl = document.getElementById('groupTree');
    if (groupTreeEl) {
        groupTreeEl.addEventListener('click', function(e) {
            let hit = e.target.closest ? e.target.closest('[data-act]') : null;
            if (!hit) return;
            let act = hit.getAttribute('data-act');
            if (act === 'toggle') { toggleGroupNode(hit.getAttribute('data-key') || ''); return; }
            let row = hit.closest('.gt-row');
            let gid = row ? Number(row.getAttribute('data-gid')) : 0;
            if (act === 'child') startNewGroup(gid);
            else if (act === 'move') openGroupParentPicker(gid);
            else if (act === 'rename') startRenameGroup(gid);
            else if (act === 'delete') deleteGroup(gid);
            else if (act === 'rename-save') commitGroupRename();
            else if (act === 'rename-cancel') cancelGroupRename();
            else if (act === 'unlink') {
                let path = hit.getAttribute('data-path');
                if (path) movePathToGroup(path, 0);
            }
        });
        groupTreeEl.addEventListener('dragover', function(e) {
            let row = e.target.closest ? e.target.closest('.gt-row[data-drop]') : null;
            let root = e.target.closest ? e.target.closest('.gt-root-drop[data-root-drop]') : null;
            if (!row && !root) return;
            e.preventDefault();
            if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
            let rows = groupTreeEl.querySelectorAll('.gt-row');
            for (let i = 0; i < rows.length; i++) rows[i].classList.toggle('is-drop', rows[i] === row);
            let rootDrop = groupTreeEl.querySelector('.gt-root-drop[data-root-drop]');
            if (rootDrop) rootDrop.classList.toggle('is-drop', rootDrop === root);
        });
        groupTreeEl.addEventListener('dragleave', function(e) {
            let row = e.target.closest ? e.target.closest('.gt-row[data-drop]') : null;
            let root = e.target.closest ? e.target.closest('.gt-root-drop[data-root-drop]') : null;
            if (row) row.classList.remove('is-drop');
            if (root) root.classList.remove('is-drop');
        });
        groupTreeEl.addEventListener('drop', function(e) {
            let row = e.target.closest ? e.target.closest('.gt-row[data-drop]') : null;
            let root = e.target.closest ? e.target.closest('.gt-root-drop[data-root-drop]') : null;
            if (!row && !root) return;
            e.preventDefault();
            clearGroupDropTargets();
            let parentId = row ? Number(row.getAttribute('data-gid')) || 0 : 0;
            let draggedGroup = e.dataTransfer
                ? e.dataTransfer.getData('text/x-sdmp3-group') : '';
            if (draggedGroup) {
                moveGroupToParent(Number(draggedGroup), parentId);
                return;
            }
            let path = e.dataTransfer ? e.dataTransfer.getData('text/x-sdmp3-path') : '';
            if (path) movePathToGroup(path, parentId);
        });
        // Items can be moved between folders; folder rows can also be nested by
        // dragging them onto their new parent.
        groupTreeEl.addEventListener('dragstart', function(e) {
            if (!e.target.closest || !e.dataTransfer) return;
            let pathRow = e.target.closest('.gt-path[data-path]');
            if (pathRow) {
                e.dataTransfer.setData('text/x-sdmp3-path', pathRow.getAttribute('data-path'));
                e.dataTransfer.effectAllowed = 'move';
                pathRow.classList.add('is-dragging');
                return;
            }
            if (e.target.closest('button, input')) return;
            let groupRow = e.target.closest('.gt-row[data-gid]');
            if (!groupRow) return;
            e.dataTransfer.setData('text/x-sdmp3-group', groupRow.getAttribute('data-gid'));
            e.dataTransfer.effectAllowed = 'move';
            groupRow.classList.add('is-group-dragging');
        });
        groupTreeEl.addEventListener('dragend', function(e) {
            let pathRow = e.target.closest ? e.target.closest('.gt-path') : null;
            let groupRow = e.target.closest ? e.target.closest('.gt-row') : null;
            if (pathRow) pathRow.classList.remove('is-dragging');
            if (groupRow) groupRow.classList.remove('is-group-dragging');
            clearGroupDropTargets();
        });
    }

    let smartPreviewEl = document.getElementById('smartPreview');
    if (smartPreviewEl) {
        smartPreviewEl.addEventListener('change', function(e) {
            let box = e.target.closest ? e.target.closest('input[data-idx]') : null;
            if (!box) return;
            toggleSmartPreview(Number(box.getAttribute('data-idx')), box.checked);
        });
        smartPreviewEl.addEventListener('click', function(e) {
            let btn = e.target.closest ? e.target.closest('button[data-act]') : null;
            if (!btn) return;
            if (btn.getAttribute('data-act') === 'apply') applySmartPreview();
            else if (btn.getAttribute('data-act') === 'cancel') cancelSmartPreview();
        });
    }

    // The folder chip on a selected row opens the assignment sheet.
    let selectedListEl = document.getElementById('selectedList');
    if (selectedListEl) {
        selectedListEl.addEventListener('click', function(e) {
            let action = e.target.closest ? e.target.closest('[data-bulk-act]') : null;
            if (action) {
                let paths = Array.from(selectedPaths.keys()).filter(function(path) {
                    return bulkSelectedPaths.has(path);
                });
                if (action.getAttribute('data-bulk-act') === 'assign') openAssignGroup(paths);
                else if (action.getAttribute('data-bulk-act') === 'clear') movePathsToGroup(paths, 0);
                return;
            }
            let chip = e.target.closest ? e.target.closest('.group-chip[data-path]') : null;
            if (!chip) return;
            e.stopPropagation();
            openAssignGroup(chip.getAttribute('data-path'));
        });
        selectedListEl.addEventListener('change', function(e) {
            let input = e.target;
            if (input.classList && input.classList.contains('bulk-item-check')) {
                let path = input.getAttribute('data-path');
                if (input.checked) bulkSelectedPaths.add(path);
                else bulkSelectedPaths.delete(path);
                updateBulkSelectionControls();
            } else if (input.id === 'bulkSelectAll') {
                selectedPaths.forEach(function(_isDir, path) {
                    if (input.checked) bulkSelectedPaths.add(path);
                    else bulkSelectedPaths.delete(path);
                });
                selectedListEl.querySelectorAll('.bulk-item-check').forEach(function(box) {
                    box.checked = input.checked;
                });
                updateBulkSelectionControls();
            }
        });
    }

    let assignListEl = document.getElementById('assignGroupList');
    if (assignListEl) {
        assignListEl.addEventListener('click', function(e) {
            let btn = e.target.closest ? e.target.closest('button[data-gid]') : null;
            if (!btn) return;
            assignPathsToGroup(Number(btn.getAttribute('data-gid')));
        });
    }
    let assignNewEl = document.getElementById('assignNewName');
    if (assignNewEl) {
        assignNewEl.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); createAssignGroup(); }
        });
    }
    let groupParentListEl = document.getElementById('groupParentList');
    if (groupParentListEl) {
        groupParentListEl.addEventListener('click', function(e) {
            let btn = e.target.closest ? e.target.closest('button[data-parent-id]') : null;
            if (!btn) return;
            chooseGroupParent(Number(btn.getAttribute('data-parent-id')) || 0);
        });
    }

    // ---- persistence: the cart survives a refresh ------------------------
    // Paths + groups + the search folder live in localStorage. They are written
    // on a 300ms debounce (checking ten boxes must not ten times serialise) and
    // verified against the server on the next load: a card that was pulled out
    // while the tab was closed must not come back as a ghost selection.
    const UI_STATE_KEY = 'sdmp3_ui_state_v1';
    const UI_STATE_MAX_PATHS = 3000;
    let uiSaveTimer = null;
    let uiStateReady = false;

    function saveUiStateNow() {
        uiSaveTimer = null;
        if (!uiStateReady) return;
        try {
            let paths = [];
            selectedPaths.forEach(function(isDir, p) { paths.push([p, isDir ? 1 : 0]); });
            if (paths.length > UI_STATE_MAX_PATHS) return;   // too much for localStorage
            localStorage.setItem(UI_STATE_KEY, JSON.stringify({
                v: 1,
                source: currentSourceRoot,
                search: searchScopePath,
                paths: paths,
                excludes: Array.from(excludedPaths),
                groups: copyGroups.map(function(g) {
                    return {name: g.name, paths: g.paths.slice()};
                })
            }));
        } catch (_e) {}
    }

    function scheduleUiSave() {
        if (!uiStateReady) return;
        if (uiSaveTimer) clearTimeout(uiSaveTimer);
        uiSaveTimer = setTimeout(saveUiStateNow, 300);
    }

    function dropMissingPath(path) {
        selectedPaths.delete(path);
        excludedPaths.delete(path);
        delete selectedSizes[path];
        copyGroups.forEach(function(g) {
            g.paths = g.paths.filter(function(p) { return p !== path; });
        });
        copyGroups = copyGroups.filter(function(g) { return g.paths.length > 0; });
    }

    function pruneMissingSelection() {
        let paths = Array.from(selectedPaths.keys());
        if (!paths.length) return;
        fetch('/api/sizes', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({paths: paths, excludes: Array.from(excludedPaths)})
        }).then(function(r) { return r.json(); }).then(function(data) {
            let missing = data.missing || [];
            if (!missing.length) return;
            missing.forEach(dropMissingPath);
            refreshCheckboxStates();
            setGroupStatus('أُزيل ' + missing.length + ' عنصراً لأنها لم تعد موجودة.');
        }).catch(function() {});
    }

    function restoreUiState() {
        let raw = null;
        try { raw = localStorage.getItem(UI_STATE_KEY); } catch (_e) {}
        let state = null;
        if (raw) { try { state = JSON.parse(raw); } catch (_e) { state = null; } }
        uiStateReady = true;
        if (!state || state.v !== 1) return;

        (Array.isArray(state.paths) ? state.paths : []).slice(0, UI_STATE_MAX_PATHS)
            .forEach(function(pair) {
                if (!Array.isArray(pair) || typeof pair[0] !== 'string') return;
                if (pair[0].charAt(0) !== '/') return;
                selectedPaths.set(pair[0], !!pair[1]);
            });
        (Array.isArray(state.excludes) ? state.excludes : []).forEach(function(p) {
            if (typeof p === 'string' && p.charAt(0) === '/') excludedPaths.add(p);
        });
        if (Array.isArray(state.groups)) {
            state.groups.forEach(function(saved) {
                if (!saved || typeof saved.name !== 'string') return;
                let name = groupPathParts(saved.name).join('/');
                if (!name || groupNameError(name, null)) return;
                let paths = (Array.isArray(saved.paths) ? saved.paths : [])
                    .filter(function(p) { return selectedPaths.has(p); });
                copyGroups.push({id: nextGroupId++, name: name, paths: paths});
            });
            // A nested group may have been restored without an explicit parent
            // record (older saved state or a smart proposal). Keep its parent
            // available for expansion, choosing, and adding sibling folders.
            copyGroups.slice().forEach(function(group) { ensureGroupParents(group.name); });
        }
        if (typeof state.source === 'string' && state.source) updateSourceRoot(state.source);
        if (typeof state.search === 'string' && state.search) setSearchScope(state.search, false);
        refreshCheckboxStates();
        if (selectedPaths.size) pruneMissingSelection();
    }

    const iconDir = `<svg class="icon" style="min-width:20px; color:var(--accent)" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>`;
    const iconFile = `<svg class="icon" style="min-width:20px; color:var(--text-muted)" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>`;
    const iconZip = `<svg class="icon" style="min-width:20px; color:var(--text-muted)" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="11" y1="4" x2="11" y2="6"></line><line x1="13" y1="6" x2="13" y2="8"></line><line x1="11" y1="8" x2="11" y2="10"></line><line x1="13" y1="10" x2="13" y2="12"></line><rect x="10.5" y="13" width="3" height="4" rx=".5"></rect></svg>`;
    // A `.zip` is unpacked automatically on copy, so it gets its own glyph in
    // every list that shows files (tree, search, picker, selection).
    function fileIcon(name) {
        return String(name || '').toLowerCase().endsWith('.zip') ? iconZip : iconFile;
    }
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
        syncStorageCards('source', currentSourceRoot);
        let savedDest = localStorage.getItem('joc_last_dest');
        if (savedDest) {
            document.getElementById('custom_dest_path').value = savedDest;
            syncStorageCards('dest', savedDest);
            checkDiskSpace(savedDest);
        }
        loadSubFolders(currentSourceRoot, document.getElementById('treeContainer'));
        generateRandomSpoofDate();
        // Everything the last visit left behind: the selection, the groups, the
        // search folder. Restoring after the tree has been requested keeps the
        // checkboxes in sync with the rows that arrive a moment later.
        restoreUiState();
        if (!searchScopePath) setSearchScope(currentSourceRoot, false);
        checkTransferStatus(); 
        renderQueue();
        checkCacheStatus(currentSourceRoot);
        loadSearchHistory();
    };

    function updateSourceRoot(path) {
        if (!path) return;
        currentSourceRoot = path;
        localStorage.setItem('joc_source_root', path);
        sourceCacheActive = false;
        syncStorageCards('source', path);
        loadSubFolders(path, document.getElementById('treeContainer'));
        checkCacheStatus(path);
        scheduleUiSave();
    }

    // ---- storage cards (internal / SD card / USB OTG) -------------------
    // STORAGE_ROOTS comes from the server (name, path, kind, icon) so the
    // browser shows the same labels + icons as the selection cards.
    const STORAGE_ROOTS = {{ storages|tojson }};
    // Every kind the server can emit; used to reset the picker path-box colour.
    const STORAGE_KIND_LIST = ['internal', 'sdcard', 'usb', 'card'];
    // The instant-search cache belongs to one source folder, so its badge must
    // follow the selected source tile, not the destination.
    let sourceCacheActive = false;

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
        // Tint the destination path row so it names which volume it is.
        let box = document.getElementById('destPathRow');
        if (box) {
            let st = storageForPath(current);
            STORAGE_KIND_LIST.forEach(function(k) { box.classList.remove('kind-border-' + k); });
            if (st) box.classList.add('kind-border-' + st.kind);
        }
        if (mode === 'source') syncFreshBadge();
    }

    function syncFreshBadge() {
        let wrap = document.getElementById('source_storage_cards');
        if (!wrap) return;
        wrap.querySelectorAll('.storage-card').forEach(function(card) {
            card.classList.toggle('is-fresh', sourceCacheActive && card.classList.contains('active'));
        });
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
        sourceCacheActive = !!(data && data.cached);
        syncFreshBadge();
        let el = document.getElementById('cacheStatusText');
        let badge = document.getElementById('searchSpeedBadge');
        if (data.cached) {
            let d = new Date(data.built_at * 1000);
            let timeStr = d.toLocaleTimeString('ar-EG', {hour: '2-digit', minute:'2-digit'});
            el.innerHTML = `<span style="color:var(--success);">${spriteIcon('ico-check')} مفهرس (${data.count} عنصر) — ${timeStr}</span>`;
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
            scheduleUiSave();
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
        let titles = {dest: 'اختر مجلد الوجهة', source: 'اختر مجلد المصدر', search: 'اختر مجلد البحث'};
        document.getElementById('pathBrowserTitle').innerText = titles[mode] || titles.dest;
        let start = '';
        if (mode === 'search') {
            start = searchScopePath;
        } else if (mode === 'dest') {
            let val = document.getElementById('custom_dest_path').value;
            start = val ? val : '/sdcard';
        } else {
            start = currentSourceRoot || '/sdcard';
        }
        loadDestBrowser(start || '/sdcard');
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
            div.innerHTML = `<div class="item-name">${fileIcon(item.name)} <span></span></div><span class="file-size"></span>${rowActionsHtml(item.path, item.name, false)}`;
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
        } else if (pickerMode === 'search') {
            setSearchScope(p, true);
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
    // The ONLY row renderer for the source browser. Browsing and search share
    // classes, checkbox namespace, actions and folder expansion; only the
    // checkbox handler differs (a search hit is also remembered in history).
    function itemRowHtml(item, opts) {
        opts = opts || {};
        let isChecked = isPathSelected(item.path) ? 'checked' : '';
        let excludedMark = isPathExplicitlyExcluded(item.path) ? '<span class="excluded-badge">مستثنى</span>' : '';
        // An archive found in search is shown with the folders (no size, no
        // expand arrow) but stays a FILE in the selection, because the copy
        // worker unpacks it. Marking it is_dir would break extraction entirely.
        let isZip = !!item.is_zip;
        let nameHint = isZip ? ' title="أرشيف: يُفكّ تلقائياً عند النسخ"' : '';
        let onChange = opts.searchMode
            ? `toggleSearchResultSelect(this, ${!!item.is_dir}, ${isZip})`
            : `toggleSelect(this, ${!!item.is_dir})`;
        let nameAttrs = nameHint + (item.is_dir
            ? ' data-toggle="1"'
            : ' onclick="this.previousElementSibling.click()"');
        return `
            <div class="item-row">
                ${item.is_dir ? `<button class="toggle-btn" data-toggle="1" aria-label="فتح المجلد">${spriteIcon('ico-chevron-right')}</button>` : `<span style="width:26px;display:inline-block;"></span>`}
                <input type="checkbox" class="checkbox" value="${esc(item.path)}" data-name="${esc(item.name)}" ${isChecked} onchange="${onChange}">
                <div class="item-name" ${nameAttrs}>
                    ${item.is_dir ? iconDir : fileIcon(item.name)} <span>${esc(item.name)}</span>${excludedMark}
                </div>
                ${(item.is_dir || isZip) ? '' : `<span class="file-size">${formatBytes(item.size || 0)}</span>`}
                ${rowActionsHtml(item.path, item.name, item.is_dir)}
            </div>
            ${item.is_dir ? '<div class="children"></div>' : ''}
        `;
    }

    function appendItemRows(parentElement, items, opts) {
        parentElement.innerHTML = '';
        if (!items.length) { parentElement.innerHTML = '<div style="padding:10px; color:var(--text-muted);">مجلد فارغ</div>'; return; }
        items.forEach(item => {
            let div = document.createElement('div');
            div.innerHTML = itemRowHtml(item, opts);
            parentElement.appendChild(div);
        });
    }

    function loadSubFolders(path, parentElement) {
        parentElement.innerHTML = '<div style="padding:10px; color:var(--text-muted);">جاري التحميل...</div>';
        parentElement.dataset.dirPath = path;
        fetch('/api/items?path=' + encodeURIComponent(path) + '&sort=' + encodeURIComponent(currentSort)).then(r=>r.json()).then(data=>{
            appendItemRows(parentElement, data.items || []);
        });
    }

    // Bring a modal to the front so a dialog opened over another modal (e.g.
    // rename/input opening over the selected-items window) is never hidden behind it.
    function bringModalToFront(id) {
        let el = document.getElementById(id);
        if (!el) return;
        document.querySelectorAll('.modal').forEach(function(m) { m.classList.remove('modal-front'); });
        el.classList.add('modal-front');
    }

    function showInputModal(title, label, defaultValue, onConfirm, validate) {
        bringModalToFront('inputModal');
        document.getElementById('inputModalTitleText').innerText = title;
        document.getElementById('inputModalLabel').innerText = label;
        let field = document.getElementById('inputModalField');
        field.value = defaultValue || '';
        let err = document.getElementById('inputModalError');
        if (err) err.textContent = '';
        document.getElementById('inputModal').style.display = 'flex';
        window._inputModalCallback = onConfirm;
        window._inputModalValidate = validate || null;
        setTimeout(() => { field.focus(); field.select(); }, 60);
    }
    function hideInputModalError() {
        let err = document.getElementById('inputModalError');
        if (err) err.textContent = '';
    }
    function submitInputModal() {
        let field = document.getElementById('inputModalField');
        let val = field.value.trim();
        // A validator keeps the dialog open with a readable message instead of
        // closing it and firing an alert the user cannot correct in place.
        if (window._inputModalValidate) {
            let err = window._inputModalValidate(val);
            if (err) {
                let errEl = document.getElementById('inputModalError');
                if (errEl) errEl.textContent = err;
                field.focus();
                return;
            }
        }
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

    function remapPathInGroups(oldPath, newPath) {
        // A renamed folder stays in its group and keeps its size/checked state.
        function remap(p) {
            if (p === oldPath) return newPath;
            if (p.startsWith(oldPath + '/')) return newPath + p.slice(oldPath.length);
            return p;
        }
        copyGroups.forEach(function(g) { g.paths = g.paths.map(remap); });
        let nextSizes = {};
        Object.keys(selectedSizes).forEach(function(p) { nextSizes[remap(p)] = selectedSizes[p]; });
        selectedSizes = nextSizes;
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
        remapPathInGroups(oldPath, newPath);
        refreshCheckboxStates();
    }

    function refreshNearestContainer(btnEl) {
        // A mutation inside the search view re-runs the same query, so the
        // view cannot silently fall back to a directory listing.
        if (resumeSearchAfterMutation()) return;
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
                checkTransferStatus();
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
            row.style.cssText = 'background:var(--surface-field); border:1px solid var(--border); border-radius:8px; padding:8px; word-break:break-all;';
            let nameEl = document.createElement('div');
            nameEl.style.cssText = 'color:var(--text); font-weight:500;';
            nameEl.textContent = String(f.path || '').split('/').filter(Boolean).pop() || f.path;
            let reasonEl = document.createElement('div');
            reasonEl.style.cssText = 'color:var(--danger-ink); margin-top:2px;';
            reasonEl.textContent = f.reason || '';
            let pathEl = document.createElement('div');
            pathEl.style.cssText = 'color:var(--text-muted); margin-top:2px; font-size:calc(11px * var(--font-scale));';
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
            if (btn) { btn.innerHTML = spriteIcon('ico-check') + ' تم النسخ'; setTimeout(function(){ btn.textContent = 'نسخ التفاصيل'; }, 1800); }
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
            if (arrow) arrow.innerHTML = spriteIcon('ico-chevron-right');
        } else {
            if (!children.innerHTML) loadSubFolders(path, children);
            children.classList.add('open');
            if (arrow) arrow.innerHTML = spriteIcon('ico-chevron-down');
        }
    }

    function toggleSelect(cb, isDir) {
        if(cb.checked) includePath(cb.value, isDir);
        else excludePath(cb.value);
        refreshCheckboxStates();
    }

    // Search-result checkboxes use this: a committed selection is also saved to
    // the search history so the folder can be reused later without re-searching.
    function toggleSearchResultSelect(cb, isDir, isZip) {
        if (cb.checked) {
            includePath(cb.value, isDir);
            rememberSearchSelection([{
                path: cb.value,
                name: cb.getAttribute('data-name') || '',
                is_dir: isDir,
                is_zip: !!isZip
            }]);
        } else {
            excludePath(cb.value);
        }
        refreshCheckboxStates();
    }

    let searchDebounceTimer = null;
    let searchRequestToken = 0;
    // Search is a temporary view of the source tree, not a second list. These
    // remember enough to restore it after a rename/delete and to leave it.
    let treeInSearchMode = false;
    let lastSearchQuery = '';
    let lastSearchFoldersOnly = true;
    // ---- search scope: the folder the search runs in ---------------------
    // The old search silently walked currentSourceRoot, so there was no way to
    // narrow it and no hint about where it looked. The scope is its own value
    // (its own localStorage key); its sub-folder menu comes from /api/items.
    let searchScopePath = localStorage.getItem('sdmp3_search_scope') || '';

    function setSearchScope(path, remember) {
        let p = String(path || '').trim();
        if (p.length > 1) p = p.replace(/\\/+$/, '');
        if (!p) p = '/sdcard';
        searchScopePath = p;
        if (remember) { try { localStorage.setItem('sdmp3_search_scope', p); } catch (_e) {} }
        loadSearchScope();
        scheduleUiSave();
    }

    function searchScopeUp() {
        let parts = String(searchScopePath || '').split('/').filter(function(x) { return x; });
        parts.pop();
        setSearchScope('/' + parts.join('/'), true);
    }

    // The scope picker is occasional, so it is a dialog instead of a permanent
    // block: the search row and the results tree stay adjacent.
    function openSearchScope() {
        let modal = document.getElementById('searchScopeModal');
        if (!modal) return;
        modal.style.display = 'flex';
        loadSearchScope();
    }

    // Called by the backdrop and by «إغلاق»; a click inside the content must
    // not close it, which is why the target id is checked.
    function closeSearchScope(e) {
        if (e && e.target && e.target.id !== 'searchScopeModal') return;
        let modal = document.getElementById('searchScopeModal');
        if (modal) modal.style.display = 'none';
    }

    // The dialog mirrors the destination picker: one locked path field and a
    // «تحديد مجلد» button that opens the same browser, which writes the chosen
    // folder back through setSearchScope(). No second sub-folder control.
    function loadSearchScope() {
        let path = searchScopePath || '/sdcard';
        let field = document.getElementById('searchScopePath');
        if (field && 'value' in field) { field.value = path; field.title = path; }
        else if (field) { field.textContent = path; field.title = path; }
    }

    // After a search the results must be visible without a manual scroll. The
    // tree is the results panel and sits right under the search row now, so the
    // common case needs no movement; only an off-screen head is scrolled into
    // view. `nearest`-style scrolling used to do nothing when the head was only
    // partly visible, which is why this computes the delta explicitly.
    function revealSearchResults() {
        let tree = document.getElementById('treeContainer');
        if (!tree) return;
        tree.scrollTop = 0;                     // a new result set starts at its first row
        let head = document.getElementById('searchViewHead') || tree;
        let rect = head.getBoundingClientRect();
        // rect.top is layout-viewport based while the visual viewport can be
        // panned (Chrome shifts it to clear the keyboard instead of scrolling),
        // so both the test and the delta are computed in visual coordinates.
        let vv = window.visualViewport;
        let vvTop = (vv && vv.offsetTop) || 0;
        let viewHeight = (vv && vv.height) || window.innerHeight || 0;
        let bar = document.getElementById('actionBar');
        if (bar) {
            let barTop = bar.getBoundingClientRect().top;
            if (barTop > 0) viewHeight = Math.min(viewHeight, barTop - vvTop);
        }
        let top = rect.top - vvTop;
        // Instant, not smooth: the keyboard is still animating its own resize,
        // and a smooth scroll would lose that race and leave the first rows
        // under the keyboard. A 96px margin keeps the head plus one row clear.
        if (top < 0 || top > viewHeight - 96) {
            let delta = top - 12;
            if (window.scrollBy) window.scrollBy({ top: delta, behavior: 'auto' });
            else document.documentElement.scrollTop += delta;
        }
    }

    function searchViewHeaderHtml(q, scope, count, fromCache) {
        return `<div id="searchViewHead" class="search-view-head">
            <button type="button" class="btn btn-outline btn-sm" onclick="exitSearchView()">
                <svg class="icon" style="width:14px;height:14px;" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"></line><polyline points="12 19 5 12 12 5"></polyline></svg>
                العودة للتصفح
            </button>
            <span class="search-view-label">نتائج البحث: <b>${esc(q)}</b> (${count}${fromCache ? ' • فوري' : ''})</span>
            <span class="search-view-path">${esc(scope)}</span>
        </div>`;
    }

    function exitSearchView() {
        // Invalidate a request still in flight: its rows must not repaint the
        // tree after the user has gone back to browsing.
        searchRequestToken++;
        treeInSearchMode = false;
        lastSearchQuery = '';
        let badge = document.getElementById('searchSpeedBadge');
        if (badge) badge.style.display = 'none';
        let tree = document.getElementById('treeContainer');
        if (tree) {
            tree.dataset.searchMode = '0';
            loadSubFolders(currentSourceRoot, tree);
        }
    }

    function resumeSearchAfterMutation() {
        if (!treeInSearchMode || !lastSearchQuery) return false;
        let input = document.getElementById('searchInput');
        if (input) input.value = lastSearchQuery;
        let foldersOnly = document.getElementById('foldersOnlyCheck');
        if (foldersOnly) foldersOnly.checked = lastSearchFoldersOnly;
        searchDeep(false);
        return true;
    }

    function searchDeep(committed) {
        let input = document.getElementById('searchInput');
        let tree = document.getElementById('treeContainer');
        if (!input || !tree) return;
        let q = input.value.trim();
        if (!q) { exitSearchView(); return; }
        let foldersOnly = document.getElementById('foldersOnlyCheck').checked;
        let scope = searchScopePath || currentSourceRoot;
        treeInSearchMode = true;
        lastSearchQuery = q;
        lastSearchFoldersOnly = foldersOnly;
        tree.dataset.dirPath = scope;
        tree.dataset.searchMode = '1';
        let head = searchViewHeaderHtml(q, scope, 0, false);
        tree.innerHTML = head + '<div style="padding:10px;color:var(--text-muted);">جاري البحث السريع...</div>';

        let myToken = ++searchRequestToken;
        fetch('/api/search?q=' + encodeURIComponent(q) + '&folders_only=' + foldersOnly + '&root=' + encodeURIComponent(scope) + (committed ? '&remember=1' : ''))
            .then(r=>r.json()).then(data=>{
            if (myToken !== searchRequestToken) return;
            let results = data.results || [];
            document.getElementById('searchSpeedBadge').style.display = data.from_cache ? 'inline-flex' : 'none';
            head = searchViewHeaderHtml(q, scope, results.length, data.from_cache);
            if (!results.length) {
                tree.innerHTML = head + '<div style="padding:10px;color:var(--text-muted);">لم يتم العثور على نتائج.</div>';
            } else {
                let body = document.createElement('div');
                appendItemRows(body, results, {searchMode: true});
                tree.innerHTML = head + body.innerHTML;
            }
            revealSearchResults();
            // The search that just ran is now the newest history entry.
            if (committed) loadSearchHistory();
        }).catch(() => {
            if (myToken === searchRequestToken) {
                tree.innerHTML = head + '<div style="padding:10px;color:var(--danger-ink);">تعذر إتمام البحث.</div>';
                revealSearchResults();
            }
        });
    }

    document.getElementById('searchInput').addEventListener('input', function() {
        clearTimeout(searchDebounceTimer);
        if (this.value.trim().length === 0) { exitSearchView(); return; }
        searchDebounceTimer = setTimeout(function() { searchDeep(false); }, 400);
    });
    document.getElementById('searchInput').addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { clearTimeout(searchDebounceTimer); searchDeep(true); }
    });
    // On a phone the keyboard covers the lower half of the screen. Put the
    // browser block at the top edge the moment the field gets focus, without an
    // animation: a smooth scroll would fight the keyboard's own resize. The old
    // query is selected so the next keystroke replaces it instead of appending.
    document.getElementById('searchInput').addEventListener('focus', function() {
        try { this.select(); } catch (e) {}
        let section = this.closest('section');
        if (section && section.scrollIntoView) {
            section.scrollIntoView({ block: 'start', inline: 'nearest', behavior: 'auto' });
        }
    });

    // ---- search history (last 25 results + last 25 queries) -------------
    // Kept in memory only; the server owns the file. Every mutation returns the
    // new list, so the two can never drift apart. The block is opened only by
    // the «السجل» button; selecting folders from search results is tracked
    // separately via rememberSearchSelection().
    let searchHistory = { items: [], queries: [] };
    let historyOpen = false;

    function relativeTime(ts) {
        let seconds = Math.max(0, Math.floor(Date.now() / 1000 - (ts || 0)));
        if (seconds < 60) return 'الآن';
        if (seconds < 3600) return 'قبل ' + Math.floor(seconds / 60) + ' د';
        if (seconds < 86400) return 'قبل ' + Math.floor(seconds / 3600) + ' س';
        if (seconds < 604800) return 'قبل ' + Math.floor(seconds / 86400) + ' ي';
        return new Date((ts || 0) * 1000).toLocaleDateString('ar');
    }

    function toggleHistoryBlock() {
        historyOpen = !historyOpen;
        renderSearchHistory();
        let btn = document.getElementById('historyToggleBtn');
        if (btn) btn.classList.toggle('is-open', historyOpen);
    }

    function loadSearchHistory() {
        return fetch('/api/history').then(function(r) { return r.json(); }).then(function(data) {
            searchHistory = { items: data.items || [], queries: data.queries || [] };
            renderSearchHistory();
        }).catch(function() {});
    }

    function renderSearchHistory() {
        let block = document.getElementById('historyBlock');
        let chips = document.getElementById('historyChips');
        let rows = document.getElementById('historyRows');
        if (!block || !chips || !rows) return;
        let queries = searchHistory.queries || [];
        let items = searchHistory.items || [];
        if (!historyOpen) { block.style.display = 'none'; return; }
        block.style.display = 'block';

        chips.innerHTML = queries.map(function(q) {
            return '<button type="button" class="history-chip" data-query="' + esc(q.q) + '" title="ابحث مرة أخرى عن «' + esc(q.q) + '»">' +
                '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:11px;height:11px;"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>' +
                esc(q.q) + '<small>' + esc(relativeTime(q.at)) + '</small></button>';
        }).join('');

        if (!items.length) {
            rows.innerHTML = '<div class="history-empty">لا يوجد سجل بعد — حدّد مجلدات من نتائج البحث أو نفّذ بحثاً، وستظهر هنا.</div>';
            return;
        }
        let folderIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>';
        let fileIconSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>';
        let zipIconSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="11" y1="4" x2="11" y2="6"></line><line x1="13" y1="6" x2="13" y2="8"></line><line x1="11" y1="8" x2="11" y2="10"></line><line x1="13" y1="10" x2="13" y2="12"></line><rect x="10.5" y="13" width="3" height="4" rx=".5"></rect></svg>';
        rows.innerHTML = items.map(function(it, index) {
            let zipLike = !!it.is_zip || String(it.name || it.path || '')
                .toLowerCase().endsWith('.zip');
            let icon = it.is_dir ? folderIcon : (zipLike ? zipIconSvg : fileIconSvg);
            return '<div class="history-row" data-index="' + index + '">' +
                '<div class="history-row-main" data-act="use" role="button" tabindex="0" title="أضف إلى التحديد">' +
                    '<div class="history-row-name">' + icon + '<span class="truncate-text">' + esc(it.name || it.path) + '</span></div>' +
                    '<div class="history-row-path">' + esc(it.path) + '</div>' +
                '</div>' +
                '<span class="history-row-meta">' + esc(relativeTime(it.at)) + '</span>' +
                '<button type="button" class="icon-btn danger" data-act="forget" aria-label="حذف من السجل">' +
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>' +
                '</button>' +
            '</div>';
        }).join('');
    }

    // One delegated listener: no path or query is ever interpolated into an
    // inline onclick, so a name with a quote cannot break the markup.
    document.getElementById('historyChips').addEventListener('click', function(e) {
        let chip = e.target.closest ? e.target.closest('[data-query]') : null;
        if (!chip) return;
        document.getElementById('searchInput').value = chip.getAttribute('data-query');
        searchDeep(true);
    });

    document.getElementById('historyRows').addEventListener('click', function(e) {
        let btn = e.target.closest ? e.target.closest('[data-act]') : null;
        if (!btn) return;
        let row = btn.closest('.history-row');
        if (!row) return;
        let item = searchHistory.items[Number(row.getAttribute('data-index'))];
        if (!item) return;
        if (btn.getAttribute('data-act') === 'forget') { forgetHistoryItem(item.path); return; }
        useHistoryItem(item, btn.closest('.history-row-main'));
    });
    document.getElementById('historyRows').addEventListener('keydown', function(e) {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        let main = e.target.closest ? e.target.closest('.history-row-main') : null;
        if (!main) return;
        e.preventDefault();
        let item = searchHistory.items[Number(main.closest('.history-row').getAttribute('data-index'))];
        if (item) useHistoryItem(item, main);
    });

    function useHistoryItem(item, rowEl) {
        includePath(item.path, !!item.is_dir);
        refreshCheckboxStates();
        // Feedback without a modal: the row flashes, and the cart counter (which
        // is already on screen) shows the new total.
        if (rowEl) {
            rowEl.classList.add('added');
            setTimeout(function() { rowEl.classList.remove('added'); }, 1200);
        }
    }

    function forgetHistoryItem(path) {
        fetch('/api/history/remove', { method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ path: path }) })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                searchHistory = { items: data.items || [], queries: data.queries || [] };
                renderSearchHistory();
            }).catch(function() {});
    }

    function clearSearchHistory() {
        showConfirmModal('سيتم مسح سجل البحث بالكامل. هل تريد المتابعة؟', function() {
            fetch('/api/history/clear', { method: 'POST' })
                .then(function(r) { return r.json(); })
                .then(function() { searchHistory = { items: [], queries: [] }; renderSearchHistory(); })
                .catch(function() {});
        });
    }

    // ---- settings + themes ---------------------------------------------
    let activeThemeId = document.documentElement.getAttribute('data-theme') || '';

    // Server-rendered so the pickers already show the saved choice.
    let activeFontId = '{{ active_font }}';
    let activeFontSizeId = '{{ active_font_size }}';
    // True while الإعدادات › الفهرس is showing a throw-away draft opened by
    // «إنشاء فهرس JOC». Nothing in that mode is written to disk.
    let indexDraftActive = false;

    function openSettings() {
        indexDraftActive = false;
        setIndexDraftUi(false);
        resetIndexForm();
        document.getElementById('settingsModal').style.display = 'flex';
        showSettingsTab('appearance');
    }

    function closeSettingsModal() {
        indexDraftActive = false;
        setIndexDraftUi(false);
        // Unsaved edits are edits of a form, never of the saved defaults: the
        // next open must show what is actually stored.
        resetIndexForm();
        document.getElementById('settingsModal').style.display = 'none';
    }

    function setIndexDraftUi(on) {
        let banner = document.getElementById('indexDraftBanner');
        if (banner) banner.style.display = on ? 'flex' : 'none';
        let save = document.getElementById('indexSaveActions');
        if (save) save.style.display = on ? 'none' : 'flex';
        let close = document.getElementById('settingsCloseBtn');
        if (close) close.style.display = on ? 'none' : 'inline-flex';
        let actions = document.getElementById('indexDraftActions');
        if (actions) actions.style.display = on ? 'flex' : 'none';
    }

    // Re-read the form from index_settings.json and drop any uploaded mask.
    function resetIndexForm() {
        if (savedIndexSettings) {
            applyIndexSettingsToUI(savedIndexSettings);
            let voice = document.getElementById('ttsVoice');
            if (voice && voice.options.length && savedIndexSettings.tts_voice) {
                voice.value = savedIndexSettings.tts_voice;
            }
        }
        customUploaded = null;
        let name = document.getElementById('customAudioName');
        if (name) name.textContent = 'لم يتم اختيار ملف بعد';
        setSettingsState('', '', 'indexSaveState');
    }

    function showSettingsTab(tab) {
        document.querySelectorAll('.settings-tab').forEach(function(btn) {
            btn.classList.toggle('active', btn.getAttribute('data-tab') === tab);
        });
        // One pane per tab, all in the DOM: switching only toggles display, so
        // no form value is ever rebuilt or lost.
        document.querySelectorAll('.settings-pane').forEach(function(pane) {
            let want = (pane.id === 'settingsPane' + tab.charAt(0).toUpperCase() + tab.slice(1));
            pane.style.display = want ? 'flex' : 'none';
        });
    }

    // The JOC button opens the same index pane as a draft: edit freely, then
    // «متابعة» builds the index with those values. Closing throws them away.
    function openIndexDraft(reset) {
        if (reset) resetIndexForm();
        indexDraftActive = true;
        setIndexDraftUi(true);
        document.getElementById('settingsModal').style.display = 'flex';
        showSettingsTab('index');
    }

    // Back from the create dialog: keep the values currently in the form.
    function openIndexSettings() {
        document.getElementById('jocModal').style.display = 'none';
        openIndexDraft(false);
    }

    function continueJocDraft() {
        indexDraftActive = false;
        setIndexDraftUi(false);
        document.getElementById('settingsModal').style.display = 'none';
        openJocCreate();
    }

    function cancelJocDraft() {
        closeSettingsModal();
    }

    // Applying a theme is only token assignment: the whole palette is a set of
    // CSS custom properties, so nothing is re-rendered and nothing flickers.
    function applyTheme(tokens, bg, id, swatches, previewName) {
        if (tokens) {
            Object.keys(tokens).forEach(function(name) {
                document.documentElement.style.setProperty(name, tokens[name]);
            });
        }
        if (bg) {
            let meta = document.querySelector('meta[name="theme-color"]');
            if (meta) meta.setAttribute('content', bg);
        }
        if (id) {
            activeThemeId = id;
            document.documentElement.setAttribute('data-theme', id);
            let select = document.getElementById('themeSelect');
            if (select) select.value = id;
            if (Array.isArray(swatches)) renderThemePreview(swatches, previewName || 'السمة');
            else updateThemePreview(id);
            updatePhotoOverlayColor();
            applySurfaceAlpha();
        }
    }

    function setSettingsState(text, kind, elementId) {
        let el = document.getElementById(elementId || 'settingsSaveState');
        if (!el) return;
        el.className = 'settings-save-state' + (kind ? ' ' + kind : '');
        el.textContent = text || '';
    }

    function renderThemePreview(colors, label) {
        let preview = document.getElementById('themeColorPreview');
        let name = document.getElementById('themePreviewName');
        if (!preview || !name) return;
        colors = (colors || []).filter(function(color) {
            return /^#[0-9a-f]{6}$/i.test(color);
        }).slice(0, 6);
        let labels = ['الخلفية', 'السطح', 'التمييز', 'التخزين الداخلي', 'بطاقة SD', 'النص'];
        while (preview.firstChild) preview.removeChild(preview.firstChild);
        colors.forEach(function(color, index) {
            let chip = document.createElement('span');
            chip.className = 'theme-preview-chip';
            chip.title = labels[index] || 'لون السمة';
            chip.style.backgroundColor = color;
            preview.appendChild(chip);
        });
        name.textContent = label || 'السمة';
        preview.setAttribute('aria-label', 'معاينة ألوان ' + (label || 'السمة'));
    }

    function updateThemePreview(id) {
        let select = document.getElementById('themeSelect');
        if (!select) return;
        let option = Array.from(select.options).find(function(item) { return item.value === id; });
        if (!option) return;
        let colors = (option.getAttribute('data-swatches') || '').split(',');
        renderThemePreview(colors, option.textContent.trim());
    }

    // ---- local background photo -----------------------------------------
    // Background images stay in this browser's IndexedDB. Only the small on/off
    // and opacity preferences use localStorage; image bytes are never posted.
    const PHOTO_DB_NAME = 'sdmp3-local-background';
    const PHOTO_DB_STORE = 'images';
    const PHOTO_DB_KEY = 'page-background';
    const PHOTO_MAX_BYTES = 8 * 1024 * 1024;
    const PHOTO_MAX_PIXELS = 48000000;
    const PHOTO_MAX_EDGE = 12000;
    const PHOTO_TYPES = {
        'image/jpeg': ['.jpg', '.jpeg'],
        'image/png': ['.png'],
        'image/webp': ['.webp']
    };
    let photoBackgroundUrl = '';
    let photoBackgroundRecordExists = false;
    let photoBackgroundEnabled = false;
    let photoOverlayOpacity = 60;
    // 100 keeps today's opaque surfaces; a lower value fades only the UI
    // surfaces, never the text or the image layer behind them.
    let surfaceAlpha = 100;

    function photoLocalGet(key, fallback) {
        try {
            let value = localStorage.getItem(key);
            return value === null ? fallback : value;
        } catch (_error) {
            return fallback;
        }
    }

    function photoLocalSet(key, value) {
        try {
            localStorage.setItem(key, value);
            return true;
        } catch (_error) {
            return false;
        }
    }

    function openPhotoDatabase() {
        return new Promise(function(resolve, reject) {
            if (!window.indexedDB) {
                reject(new Error('تخزين الصور المحلي غير متاح في هذا المتصفح.'));
                return;
            }
            let request;
            try {
                request = window.indexedDB.open(PHOTO_DB_NAME, 1);
            } catch (_error) {
                reject(new Error('تعذر فتح التخزين المحلي للصور.'));
                return;
            }
            request.onupgradeneeded = function() {
                let db = request.result;
                if (!db.objectStoreNames.contains(PHOTO_DB_STORE)) {
                    db.createObjectStore(PHOTO_DB_STORE);
                }
            };
            request.onsuccess = function() { resolve(request.result); };
            request.onerror = function() {
                reject(new Error('تعذر فتح التخزين المحلي للصور.'));
            };
        });
    }

    function photoDatabaseRequest(operation, value) {
        return openPhotoDatabase().then(function(db) {
            return new Promise(function(resolve, reject) {
                let transaction;
                let settled = false;
                let result;
                function fail(event) {
                    if (settled) return;
                    settled = true;
                    try { db.close(); } catch (_error) {}
                    let storageError = event && event.target && event.target.error;
                    if (!storageError && transaction) storageError = transaction.error;
                    reject(new Error(storageError && storageError.name === 'QuotaExceededError'
                        ? 'مساحة التخزين المحلي غير كافية. اختر صورة أصغر.'
                        : 'تعذر حفظ صورة الخلفية محلياً.'));
                }
                try {
                    transaction = db.transaction(PHOTO_DB_STORE,
                        operation === 'get' ? 'readonly' : 'readwrite');
                    let store = transaction.objectStore(PHOTO_DB_STORE);
                    let request;
                    if (operation === 'get') request = store.get(PHOTO_DB_KEY);
                    else if (operation === 'put') request = store.put(value, PHOTO_DB_KEY);
                    else request = store.delete(PHOTO_DB_KEY);
                    request.onsuccess = function() { result = request.result; };
                    request.onerror = fail;
                    transaction.oncomplete = function() {
                        if (settled) return;
                        settled = true;
                        try { db.close(); } catch (_error) {}
                        resolve(result);
                    };
                    transaction.onerror = fail;
                    transaction.onabort = fail;
                } catch (_error) {
                    fail();
                }
            });
        });
    }

    function photoTypeForFile(file, storedType) {
        let type = String(storedType || file.type || '').toLowerCase();
        let extension = String(file.name || '').toLowerCase().match(/\\.[^.]+$/);
        let ext = extension ? extension[0] : '';
        if (!PHOTO_TYPES[type] && !type && ext) {
            Object.keys(PHOTO_TYPES).some(function(candidate) {
                if (PHOTO_TYPES[candidate].indexOf(ext) !== -1) {
                    type = candidate;
                    return true;
                }
                return false;
            });
        }
        if (!PHOTO_TYPES[type]) {
            throw new Error('نوع الصورة غير مدعوم. استخدم PNG أو JPEG أو WebP.');
        }
        return type;
    }

    function inspectPhotoFile(file, storedType) {
        return new Promise(function(resolve, reject) {
            if (!file || !file.size) {
                reject(new Error('ملف الصورة فارغ أو غير صالح.'));
                return;
            }
            if (file.size > PHOTO_MAX_BYTES) {
                reject(new Error('الصورة كبيرة جداً. الحد الأقصى 8 ميجابايت.'));
                return;
            }
            let type;
            try {
                type = photoTypeForFile(file, storedType);
            } catch (error) {
                reject(error);
                return;
            }
            let temporaryUrl;
            try {
                temporaryUrl = URL.createObjectURL(file);
            } catch (_error) {
                reject(new Error('تعذر قراءة ملف الصورة.'));
                return;
            }
            let image = new Image();
            image.onload = function() {
                let width = image.naturalWidth;
                let height = image.naturalHeight;
                URL.revokeObjectURL(temporaryUrl);
                if (!width || !height) {
                    reject(new Error('تعذر قراءة أبعاد الصورة. اختر صورة سليمة.'));
                } else if (width > PHOTO_MAX_EDGE || height > PHOTO_MAX_EDGE
                           || width * height > PHOTO_MAX_PIXELS) {
                    reject(new Error('أبعاد الصورة كبيرة جداً. اختر صورة أصغر.'));
                } else {
                    resolve({type: type, width: width, height: height});
                }
            };
            image.onerror = function() {
                URL.revokeObjectURL(temporaryUrl);
                reject(new Error('تعذر فتح الصورة. استخدم PNG أو JPEG أو WebP سليمة.'));
            };
            image.src = temporaryUrl;
        });
    }

    function setPhotoBackgroundStatus(text, kind) {
        let state = document.getElementById('photoBackgroundState');
        if (!state) return;
        state.className = 'settings-save-state' + (kind ? ' ' + kind : '');
        state.textContent = text || '';
    }

    function updatePhotoBackgroundUi() {
        let toggle = document.getElementById('photoBackgroundEnabled');
        let remove = document.getElementById('removePhotoBackgroundBtn');
        let slider = document.getElementById('photoOverlayRange');
        let value = document.getElementById('photoOverlayValue');
        let preview = document.getElementById('photoBackgroundPreview');
        if (toggle) {
            toggle.checked = photoBackgroundEnabled;
            toggle.disabled = !photoBackgroundUrl;
        }
        if (remove) remove.disabled = !photoBackgroundRecordExists;
        if (slider) {
            slider.value = String(photoOverlayOpacity);
            slider.disabled = !photoBackgroundUrl;
        }
        if (value) value.textContent = photoOverlayOpacity + '%';
        if (preview) {
            preview.hidden = !photoBackgroundUrl;
            if (photoBackgroundUrl) preview.src = photoBackgroundUrl;
            else preview.removeAttribute('src');
        }
        applyPhotoBackground();
    }

    function updatePhotoOverlayColor() {
        if (!document.body) return;
        let color = getComputedStyle(document.documentElement)
            .getPropertyValue('--bg').trim();
        let match = color.match(/^#([0-9a-f]{6})$/i);
        let red = 22, green = 29, blue = 39;
        if (match) {
            red = parseInt(match[1].slice(0, 2), 16);
            green = parseInt(match[1].slice(2, 4), 16);
            blue = parseInt(match[1].slice(4, 6), 16);
        }
        document.body.style.setProperty('--photo-overlay',
            'rgba(' + red + ',' + green + ',' + blue + ',' + (photoOverlayOpacity / 100) + ')');
    }

    function applySurfaceAlpha() {
        document.documentElement.style.setProperty('--surface-alpha',
            String(surfaceAlpha / 100));
        let slider = document.getElementById('surfaceAlphaRange');
        let value = document.getElementById('surfaceAlphaValue');
        if (slider) slider.value = String(surfaceAlpha);
        if (value) value.textContent = surfaceAlpha + '%';
    }

    function setSurfaceAlpha(value) {
        let parsed = Number.parseInt(value, 10);
        if (!Number.isFinite(parsed)) return;
        surfaceAlpha = Math.max(30, Math.min(100, parsed));
        let saved = photoLocalSet('sdmp3_surface_alpha', String(surfaceAlpha));
        applySurfaceAlpha();
        if (!saved) {
            setPhotoBackgroundStatus(
                'تم تغيير شفافية الأسطح مؤقتاً، لكن تعذر حفظها في المتصفح.', 'err');
        }
    }

    function applyPhotoBackground() {
        if (!document.body) return;
        updatePhotoOverlayColor();
        applySurfaceAlpha();
        if (photoBackgroundUrl) {
            document.body.style.setProperty('--photo-image',
                'url("' + photoBackgroundUrl + '")');
        } else {
            document.body.style.removeProperty('--photo-image');
        }
        document.body.classList.toggle('photo-background-enabled',
            !!photoBackgroundUrl && photoBackgroundEnabled);
    }

    function choosePhotoBackground() {
        let input = document.getElementById('photoBackgroundFile');
        if (input) input.click();
    }

    async function handlePhotoBackgroundFile(input) {
        let file = input && input.files && input.files[0];
        if (input) input.value = '';
        if (!file) return;
        setPhotoBackgroundStatus('جارٍ فحص الصورة وحفظها على هذا الجهاز…', '');
        try {
            let info = await inspectPhotoFile(file);
            let nextUrl = URL.createObjectURL(file);
            let record = {
                blob: file, type: info.type, name: String(file.name || 'background'),
                width: info.width, height: info.height
            };
            try {
                await photoDatabaseRequest('put', record);
            } catch (error) {
                URL.revokeObjectURL(nextUrl);
                throw error;
            }
            if (photoBackgroundUrl) URL.revokeObjectURL(photoBackgroundUrl);
            photoBackgroundUrl = nextUrl;
            photoBackgroundRecordExists = true;
            photoBackgroundEnabled = true;
            let enabledSaved = photoLocalSet('sdmp3_photo_background_enabled', '1');
            photoLocalSet('sdmp3_photo_background_opacity', String(photoOverlayOpacity));
            updatePhotoBackgroundUi();
            setPhotoBackgroundStatus(enabledSaved
                ? 'تم حفظ الصورة محلياً وتفعيلها.'
                : 'تم حفظ الصورة، لكن تعذر حفظ حالة التفعيل في المتصفح.', enabledSaved ? 'ok' : 'err');
        } catch (error) {
            setPhotoBackgroundStatus(error && error.message
                ? error.message : 'تعذر حفظ الصورة محلياً.', 'err');
        }
    }

    function setPhotoBackgroundEnabled(enabled) {
        if (enabled && !photoBackgroundUrl) {
            photoBackgroundEnabled = false;
            updatePhotoBackgroundUi();
            setPhotoBackgroundStatus('اختر صورة أولاً لتفعيل الخلفية.', 'err');
            return;
        }
        photoBackgroundEnabled = !!enabled;
        let saved = photoLocalSet('sdmp3_photo_background_enabled',
            photoBackgroundEnabled ? '1' : '0');
        updatePhotoBackgroundUi();
        setPhotoBackgroundStatus(saved
            ? (photoBackgroundEnabled ? 'تم تفعيل صورة الخلفية.' : 'تم إخفاء صورة الخلفية.')
            : 'تم تطبيق الاختيار مؤقتاً، لكن تعذر حفظه في المتصفح.', saved ? 'ok' : 'err');
    }

    function setPhotoOverlayOpacity(value) {
        let parsed = Number.parseInt(value, 10);
        if (!Number.isFinite(parsed)) return;
        photoOverlayOpacity = Math.max(0, Math.min(85, parsed));
        let saved = photoLocalSet('sdmp3_photo_background_opacity',
            String(photoOverlayOpacity));
        updatePhotoBackgroundUi();
        if (!saved) {
            setPhotoBackgroundStatus(
                'تم تغيير التعتيم مؤقتاً، لكن تعذر حفظه في المتصفح.', 'err');
        }
    }

    async function removePhotoBackground() {
        if (!photoBackgroundRecordExists) return;
        setPhotoBackgroundStatus('جارٍ إزالة الصورة من التخزين المحلي…', '');
        try {
            await photoDatabaseRequest('delete');
            if (photoBackgroundUrl) URL.revokeObjectURL(photoBackgroundUrl);
            photoBackgroundUrl = '';
            photoBackgroundRecordExists = false;
            photoBackgroundEnabled = false;
            photoOverlayOpacity = 60;
            surfaceAlpha = 100;
            let enabledSaved = photoLocalSet('sdmp3_photo_background_enabled', '0');
            let opacitySaved = photoLocalSet('sdmp3_photo_background_opacity', '60');
            let alphaSaved = photoLocalSet('sdmp3_surface_alpha', '100');
            updatePhotoBackgroundUi();
            setPhotoBackgroundStatus(
                enabledSaved && opacitySaved && alphaSaved
                    ? 'تمت إزالة الصورة وإيقاف الخلفية، وأُعيدت الأسطح إلى عتامتها الكاملة.'
                    : 'تمت إزالة الصورة، لكن تعذر حفظ بعض الإعدادات في المتصفح.',
                enabledSaved && opacitySaved && alphaSaved ? 'ok' : 'err');
        } catch (error) {
            setPhotoBackgroundStatus(error && error.message
                ? error.message : 'تعذر إزالة الصورة.', 'err');
        }
    }

    async function initializePhotoBackground() {
        let savedEnabled = photoLocalGet('sdmp3_photo_background_enabled', '0') === '1';
        let savedOpacity = Number.parseInt(
            photoLocalGet('sdmp3_photo_background_opacity', '60'), 10);
        photoOverlayOpacity = Number.isFinite(savedOpacity)
            ? Math.max(0, Math.min(85, savedOpacity)) : 60;
        let savedAlpha = Number.parseInt(
            photoLocalGet('sdmp3_surface_alpha', '100'), 10);
        surfaceAlpha = Number.isFinite(savedAlpha)
            ? Math.max(30, Math.min(100, savedAlpha)) : 100;
        applySurfaceAlpha();
        try {
            let record = await photoDatabaseRequest('get');
            if (!record) {
                photoBackgroundEnabled = false;
                updatePhotoBackgroundUi();
                setPhotoBackgroundStatus('اختر صورة PNG أو JPEG أو WebP بحجم لا يتجاوز 8 ميجابايت.', '');
                return;
            }
            photoBackgroundRecordExists = true;
            if (!record.blob || typeof record.blob.size !== 'number') {
                throw new Error('الصورة المحفوظة غير صالحة. أزلها واختر صورة أخرى.');
            }
            await inspectPhotoFile(record.blob, record.type);
            photoBackgroundUrl = URL.createObjectURL(record.blob);
            photoBackgroundEnabled = savedEnabled;
            if (!savedEnabled) photoLocalSet('sdmp3_photo_background_enabled', '0');
            updatePhotoBackgroundUi();
            setPhotoBackgroundStatus(savedEnabled
                ? 'تم استرجاع الصورة من هذا المتصفح.'
                : 'الصورة محفوظة محلياً. فعّل الخيار لإظهارها.', 'ok');
        } catch (error) {
            photoBackgroundEnabled = false;
            photoLocalSet('sdmp3_photo_background_enabled', '0');
            updatePhotoBackgroundUi();
            setPhotoBackgroundStatus(error && error.message
                ? error.message : 'تعذر استرجاع صورة الخلفية.', 'err');
        }
    }

    // Typography is two CSS variables: `--app-font` (a font stack) and
    // `--font-scale` (a multiplier). Applying them is a property write - no
    // re-render, no reload, and the fonts not in use are never downloaded.
    function applyFont(appFont, scale) {
        if (appFont) document.documentElement.style.setProperty('--app-font', appFont);
        if (scale) document.documentElement.style.setProperty('--font-scale', scale);
        let preview = document.getElementById('fontPreview');
        if (preview && appFont) preview.style.fontFamily = appFont;
    }

    // Save one font/size choice. The server validates the id against the
    // registry, so an unknown value can never end up in settings.json.
    function saveTypography(payload, stateId) {
        setSettingsState('جارٍ الحفظ...', '', stateId);
        return fetch('/api/settings', { method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload) })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(res) {
                if (!res.ok) { setSettingsState(res.data.error || 'تعذر الحفظ', 'err', stateId); return; }
                applyFont(res.data.app_font, res.data.font_scale);
                setSettingsState('تم الحفظ.', 'ok', stateId);
                setTimeout(function() { setSettingsState('', '', stateId); }, 2000);
            })
            .catch(function() { setSettingsState('تعذر الاتصال بالخادم', 'err', stateId); });
    }

    function pickFont(id) {
        if (!id || id === activeFontId) return;
        let select = document.getElementById('fontSelect');
        if (select) select.value = id;
        activeFontId = id;
        saveTypography({ font: id }, 'settingsSaveState');
    }

    function pickFontSize(id) {
        if (!id || id === activeFontSizeId) return;
        document.querySelectorAll('#sizeSeg .size-seg-btn').forEach(function(btn) {
            btn.classList.toggle('is-active', btn.getAttribute('data-size-id') === id);
        });
        activeFontSizeId = id;
        saveTypography({ font_size: id }, 'settingsSaveState');
    }

    // The saved switch IS the consent: no per-copy prompt, so it also works
    // when Chrome was closed before the copy finished.
    function pickAutoSpoof(enabled) {
        let cb = document.getElementById('autoSpoofCheck');
        if (cb) cb.checked = !!enabled;
        saveTypography({ auto_spoof: !!enabled }, 'generalSaveState');
    }

    function pickTheme(id) {
        if (!id || id === activeThemeId) return;
        updateThemePreview(id);
        setSettingsState('جارٍ الحفظ...', '');
        fetch('/api/settings', { method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ theme: id }) })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(res) {
                if (!res.ok) {
                    let select = document.getElementById('themeSelect');
                    if (select) select.value = activeThemeId;
                    updateThemePreview(activeThemeId);
                    setSettingsState(res.data.error || 'تعذر حفظ السمة', 'err');
                    return;
                }
                applyTheme(res.data.tokens, res.data.theme_bg, id,
                    res.data.swatches, res.data.theme_name);
                setSettingsState('تم الحفظ — السمة مطبقة.', 'ok');
                setTimeout(function() { setSettingsState('', ''); }, 2500);
            })
            .catch(function() {
                let select = document.getElementById('themeSelect');
                if (select) select.value = activeThemeId;
                updateThemePreview(activeThemeId);
                setSettingsState('تعذر الاتصال بالخادم', 'err');
            });
    }

    // ---- selection tracking for the «السجل» panel ------------------
    // Called when a search-result checkbox is checked: the selected folder is
    // recorded in the history so it can be reused later without re-searching.
    // Only the items list is sent; the query chips are owned by /api/search.
    function rememberSearchSelection(selections) {
        if (!selections || !selections.length) return;
        fetch('/api/history/update', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ selected_folders: selections })
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data.ok) {
                searchHistory = { items: data.items || [], queries: data.queries || [] };
                if (historyOpen) renderSearchHistory();
            }
        }).catch(function() {});
    }

    // The action bar shows the selection size only. A time estimate was tried
    // and removed: it was derived from one remembered throughput and was wrong
    // by a wide margin on every real card, which is worse than no number.
    function updateSizeDisplay(totalBytes) {
        let formattedSize = '';
        let mbThreshold = 1000 * 1024 * 1024; 
        if (totalBytes >= mbThreshold) formattedSize = (totalBytes / (1024*1024*1024)).toFixed(2) + ' GB';
        else formattedSize = (totalBytes / (1024*1024)).toFixed(2) + ' MB';

        // The action bar repeats the selection size: the dialog is closed most
        // of the time, and "how much am I copying" must stay one glance away.
        let barSizeEl = document.getElementById('barSize');
        if (barSizeEl) barSizeEl.textContent = totalBytes > 0 ? formattedSize : '—';

        let sizeHtml = `إجمالي الحجم المحدد: <span class="num" style="color:var(--text); font-size:calc(14px * var(--font-scale)); font-weight:600;">${formattedSize}</span>`;
        if (currentDestFreeBytes > 0) {
            let diff = currentDestFreeBytes - totalBytes;
            if (diff >= 0) {
                let diffFmt = diff >= mbThreshold ? (diff/(1024*1024*1024)).toFixed(2) + ' GB' : (diff/(1024*1024)).toFixed(2) + ' MB';
                sizeHtml += `<br><span style="color:var(--success); font-size:calc(12px * var(--font-scale)); margin-top:4px; display:block;">المساحة كافية (المتبقي: ${diffFmt})</span>`;
            } else {
                let absDiff = Math.abs(diff);
                let diffFmt = absDiff >= mbThreshold ? (absDiff/(1024*1024*1024)).toFixed(2) + ' GB' : (absDiff/(1024*1024)).toFixed(2) + ' MB';
                sizeHtml += `<br><span style="color:var(--danger-ink); font-size:calc(12px * var(--font-scale)); margin-top:4px; display:block;">المساحة لا تكفي (عجز: -${diffFmt})</span>`;
            }
        } else { sizeHtml += `<br><span style="color:var(--text-muted); font-size:calc(12px * var(--font-scale)); margin-top:4px; display:block;">يرجى تحديد الوجهة لمعرفة المساحة.</span>`; }
        document.getElementById('totalSizeSelected').innerHTML = sizeHtml;
    }

    async function openSelectedModal() {
        let modal = document.getElementById('selectedModal');
        modal.style.display = 'flex';

        if (selectedPaths.size === 0) {
            document.getElementById('selectedList').innerHTML = '<div style="text-align:center; padding:40px; color:var(--text-muted);">لا يوجد عناصر محددة</div>';
            document.getElementById('totalSizeSelected').innerHTML = 'لا توجد ملفات محددة';
            renderGroupUI();
            return;
        }

        // The window is drawn immediately from whatever is already known; sizes
        // are requested once for the whole selection and cached for re-renders
        // (checking a box must not refetch anything).
        renderGroupUI();
        updateSelectedTotal(Array.from(selectedPaths.keys()));
        let pathsArray = Array.from(selectedPaths.keys());
        let missing = pathsArray.some(function(p) { return !selectedSizes[p]; });
        if (!missing) { renderSelectedRows(); return; }

        let list = document.getElementById('selectedList');
        if (!list.querySelector('.selected-row')) {
            list.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">جاري حساب المساحة...</div>';
        }
        try {
            // One request for the whole selection; the server walks each tree
            // once and drops nested duplicates (a child is inside its parent).
            let res = await fetch('/api/sizes', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({paths: pathsArray, excludes: Array.from(excludedPaths)})
            });
            let data = await res.json();
            let sizes = data.sizes || {};
            pathsArray.forEach(function(path) {
                selectedSizes[path] = sizes[path] || {bytes: 0, formatted: '0 B', owner: path};
            });
            renderSelectedRows();
            updateSelectedTotal(pathsArray);
            // The dialog just paid for these numbers; the bar need not ask again.
            selectionSizeSig = selectionSignature();
        } catch (e) {
            if (!list.querySelector('.selected-row')) {
                list.innerHTML = '<div style="padding:20px; color:var(--danger-ink);">تعذر حساب الحجم — تحقق من الاتصال.</div>';
            }
            document.getElementById('totalSizeSelected').innerText = 'تعذر حساب الحجم';
        }
    }

    function updateSelectedTotal(pathsArray) {
        let counted = {};
        let totalBytes = 0;
        pathsArray.forEach(function(path) {
            let info = selectedSizes[path] || {bytes: 0, owner: path};
            if (!counted[info.owner]) { counted[info.owner] = true; totalBytes += info.bytes || 0; }
        });
        updateSizeDisplay(totalBytes);
    }

    function updateBulkSelectionControls() {
        let list = document.getElementById('selectedList');
        if (!list) return;
        let total = selectedPaths.size;
        let count = Array.from(bulkSelectedPaths).filter(function(path) {
            return selectedPaths.has(path);
        }).length;
        let countEl = list.querySelector('.bulk-count');
        let all = document.getElementById('bulkSelectAll');
        if (countEl) countEl.textContent = count + ' محدد';
        if (all) {
            all.checked = total > 0 && count === total;
            all.indeterminate = count > 0 && count < total;
        }
        list.querySelectorAll('[data-bulk-act]').forEach(function(button) {
            button.disabled = count === 0;
        });
    }

    // Rows are rebuilt with DOM nodes: a track named `01 "live".mp3` must not be
    // able to break out of an attribute or inject markup.
    function renderSelectedRows() {
        let list = document.getElementById('selectedList');
        if (!list) return;
        let pathsArray = Array.from(selectedPaths.keys());
        let keepScroll = list.scrollTop;
        list.innerHTML = '';
        if (!pathsArray.length) {
            bulkSelectedPaths.clear();
            list.innerHTML = '<div style="text-align:center; padding:40px; color:var(--text-muted);">لا يوجد عناصر محددة</div>';
            return;
        }

        let bulkBar = document.createElement('div');
        bulkBar.className = 'bulk-assign-bar';
        let allLabel = document.createElement('label');
        allLabel.className = 'bulk-select-all';
        let allCheck = document.createElement('input');
        allCheck.type = 'checkbox';
        allCheck.id = 'bulkSelectAll';
        allCheck.setAttribute('aria-label', 'تحديد كل الملفات والمجلدات');
        let allText = document.createElement('span');
        allText.textContent = 'تحديد الكل';
        let count = document.createElement('span');
        count.className = 'bulk-count';
        count.textContent = '0 محدد';
        allLabel.appendChild(allCheck);
        allLabel.appendChild(allText);
        allLabel.appendChild(count);
        let assignButton = document.createElement('button');
        assignButton.type = 'button';
        assignButton.className = 'btn btn-primary btn-sm';
        assignButton.setAttribute('data-bulk-act', 'assign');
        assignButton.textContent = 'إسناد لمجلد';
        let clearButton = document.createElement('button');
        clearButton.type = 'button';
        clearButton.className = 'btn btn-outline btn-sm';
        clearButton.setAttribute('data-bulk-act', 'clear');
        clearButton.textContent = 'إخراج من المجلد';
        bulkBar.appendChild(allLabel);
        bulkBar.appendChild(assignButton);
        bulkBar.appendChild(clearButton);
        list.appendChild(bulkBar);

        pathsArray.forEach(function(path) {
            let isDir = selectedPaths.get(path);
            let itemName = path.split('/').filter(Boolean).pop() || path;

            let row = document.createElement('div');
            row.className = 'selected-row';
            row.setAttribute('data-path', path);
            row.draggable = true;
            row.addEventListener('dragstart', function(e) {
                e.dataTransfer.setData('text/x-sdmp3-path', path);
                e.dataTransfer.effectAllowed = 'move';
                row.classList.add('is-dragging');
            });
            row.addEventListener('dragend', function() {
                row.classList.remove('is-dragging');
                clearGroupDropTargets();
            });

            let selectCheck = document.createElement('input');
            selectCheck.type = 'checkbox';
            selectCheck.className = 'selected-row-check bulk-item-check';
            selectCheck.setAttribute('data-path', path);
            selectCheck.setAttribute('aria-label', 'تحديد ' + itemName);
            selectCheck.checked = bulkSelectedPaths.has(path);
            row.appendChild(selectCheck);

            let main = document.createElement('div');
            main.className = 'selected-row-main';
            let top = document.createElement('div');
            top.className = 'selected-row-top';
            top.innerHTML = isDir ? iconDir : fileIcon(itemName);
            let nameEl = document.createElement('div');
            nameEl.className = 'truncate-text selected-row-name';
            nameEl.textContent = itemName;
            nameEl.title = path;
            top.appendChild(nameEl);
            if (isDir) {
                let expanded = selectedBrowseExpanded.has(path);
                let browseToggle = document.createElement('button');
                browseToggle.type = 'button';
                browseToggle.className = 'selected-browse-toggle root' + (expanded ? ' open' : '');
                browseToggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
                browseToggle.setAttribute('aria-label',
                    (expanded ? 'طي' : 'فتح') + ' محتويات ' + itemName);
                browseToggle.title = expanded ? 'طي محتويات المجلد' : 'فتح محتويات المجلد لاستثناء عناصر';
                browseToggle.innerHTML = spriteIcon('ico-chevron-right');
                browseToggle.onclick = function() { toggleSelectedBrowse(path); };
                top.appendChild(browseToggle);
            }
            main.appendChild(top);

            // The folder chip is the touch-friendly assignment control; drag &
            // drop onto the folder list does the same thing.
            let gname = groupNameForPath(path);
            let chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'group-chip' + (gname ? '' : ' none');
            chip.setAttribute('data-path', path);
            chip.setAttribute('data-act', 'assign');
            chip.title = gname
                ? 'داخل «' + groupPathParts(gname).join(' › ') + '» — اضغط للتغيير'
                : 'بدون مجلد — اضغط للاختيار';
            chip.innerHTML = spriteIcon(gname ? 'ico-folder' : 'ico-close');
            let chipLabel = document.createElement('span');
            chipLabel.textContent = gname ? 'داخل: ' + groupPathParts(gname).join(' › ') : 'بدون مجلد';
            chip.appendChild(chipLabel);
            main.appendChild(chip);

            let right = document.createElement('div');
            right.className = 'selected-row-right';
            let sizeEl = document.createElement('span');
            sizeEl.className = 'selected-row-size';
            let info = selectedSizes[path];
            sizeEl.textContent = info ? info.formatted : '...';
            let removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'btn btn-outline';
            removeBtn.style.cssText = 'padding: 6px 10px; font-size: calc(12px * var(--font-scale)); color:var(--danger-ink); border-color:var(--danger-ink);';
            removeBtn.textContent = 'إزالة';
            removeBtn.onclick = function () { unselectFromModal(path); };
            right.appendChild(sizeEl);
            right.appendChild(removeBtn);

            row.appendChild(main);
            row.appendChild(right);
            list.appendChild(row);
            if (isDir && selectedBrowseExpanded.has(path)) {
                list.appendChild(renderSelectedBrowseBranch(path, 1));
            }
        });

        if (excludedPaths.size > 0) {
            let head = document.createElement('div');
            head.style.cssText = 'margin:12px 0 2px; font-size:calc(13px * var(--font-scale)); color:var(--warning); font-weight:500;';
            head.textContent = 'عناصر مستثناة (' + excludedPaths.size + '):';
            list.appendChild(head);
            Array.from(excludedPaths).forEach(function(path) {
                let itemName = path.split('/').filter(Boolean).pop() || path;
                let row = document.createElement('div');
                row.className = 'selected-row';
                row.style.cssText = 'background: var(--warning-soft); border-style: dashed; border-color: var(--warning-line);';
                let nameEl = document.createElement('div');
                nameEl.className = 'truncate-text';
                nameEl.style.cssText = 'font-size:calc(13px * var(--font-scale)); color:var(--danger-ink); flex:1; min-width:0;';
                nameEl.title = path;
                nameEl.textContent = itemName;
                let addBtn = document.createElement('button');
                addBtn.className = 'btn btn-outline';
                addBtn.style.cssText = 'padding: 6px 10px; font-size: calc(12px * var(--font-scale)); flex-shrink: 0;';
                addBtn.textContent = 'إرجاع';
                addBtn.onclick = function () { reincludeFromModal(path, false); };
                row.appendChild(nameEl);
                row.appendChild(addBtn);
                list.appendChild(row);
            });
        }
        updateBulkSelectionControls();
        list.scrollTop = keepScroll;
    }

    function unselectFromModal(path) {
        excludePath(path);
        // A removed item must leave its group as well, or the copy would still
        // try to place a folder that is no longer in the cart.
        copyGroups.forEach(function(g) {
            g.paths = g.paths.filter(function(p) { return p !== path; });
        });
        refreshCheckboxStates();
        updateSelectedTotal(Array.from(selectedPaths.keys()));
    }

    function reincludeFromModal(path, isDir) {
        includePath(path, isDir);
        refreshCheckboxStates();
        updateSelectedTotal(Array.from(selectedPaths.keys()));
    }

    function selectedBrowsePathIsCovered(path) {
        return Array.from(selectedPaths.keys()).some(function(root) {
            return path === root || (root === '/'
                ? path.indexOf('/') === 0
                : path.indexOf(root + '/') === 0);
        });
    }

    function pruneSelectedBrowseState() {
        let candidates = new Set([
            ...selectedBrowseExpanded,
            ...selectedBrowseCache.keys(),
            ...selectedBrowseLoading,
            ...selectedBrowseErrors.keys()
        ]);
        candidates.forEach(function(path) {
            if (selectedBrowsePathIsCovered(path)) return;
            selectedBrowseExpanded.delete(path);
            selectedBrowseCache.delete(path);
            selectedBrowseLoading.delete(path);
            selectedBrowseErrors.delete(path);
        });
    }

    function toggleSelectedBrowse(path) {
        if (selectedBrowseExpanded.has(path)) {
            selectedBrowseExpanded.delete(path);
            renderSelectedRows();
            return;
        }
        if (!selectedBrowsePathIsCovered(path)) return;
        selectedBrowseExpanded.add(path);
        selectedBrowseErrors.delete(path);
        if (!selectedBrowseCache.has(path) && !selectedBrowseLoading.has(path)) {
            loadSelectedBrowseItems(path);
        } else {
            renderSelectedRows();
        }
    }

    function loadSelectedBrowseItems(path) {
        selectedBrowseLoading.add(path);
        renderSelectedRows();
        fetch('/api/items?path=' + encodeURIComponent(path) +
              '&sort=' + encodeURIComponent(currentSort))
            .then(function(response) {
                if (!response.ok) throw new Error('تعذر قراءة محتويات المجلد');
                return response.json();
            })
            .then(function(data) {
                if (data.error) throw new Error(data.error);
                if (selectedBrowseExpanded.has(path) && selectedBrowsePathIsCovered(path)) {
                    selectedBrowseCache.set(path, Array.isArray(data.items) ? data.items : []);
                }
            })
            .catch(function(error) {
                if (selectedBrowseExpanded.has(path) && selectedBrowsePathIsCovered(path)) {
                    selectedBrowseErrors.set(path, error.message || 'تعذر قراءة المجلد');
                }
            })
            .finally(function() {
                selectedBrowseLoading.delete(path);
                renderSelectedRows();
            });
    }

    function renderSelectedBrowseBranch(path, depth) {
        let branch = document.createElement('div');
        branch.className = 'selected-browse-tree';
        branch.setAttribute('data-browse-parent', path);
        if (selectedBrowseLoading.has(path)) {
            branch.textContent = 'جارٍ قراءة محتويات المجلد...';
            return branch;
        }
        if (selectedBrowseErrors.has(path)) {
            let message = document.createElement('span');
            message.className = 'selected-browse-message error';
            message.textContent = selectedBrowseErrors.get(path);
            let retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'btn btn-outline btn-sm';
            retry.textContent = 'إعادة المحاولة';
            retry.onclick = function() { loadSelectedBrowseItems(path); };
            branch.appendChild(message);
            branch.appendChild(retry);
            return branch;
        }
        let items = selectedBrowseCache.get(path);
        if (!items) {
            branch.textContent = 'جارٍ قراءة محتويات المجلد...';
            return branch;
        }
        if (!items.length) {
            branch.textContent = 'المجلد فارغ';
            branch.classList.add('is-empty');
            return branch;
        }

        items.forEach(function(item) {
            let row = document.createElement('div');
            row.className = 'selected-browse-row';
            row.style.marginInlineStart = Math.min(depth, 8) * 10 + 'px';

            let checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.className = 'checkbox selected-browse-checkbox';
            checkbox.value = item.path;
            checkbox.checked = isPathSelected(item.path);
            checkbox.setAttribute('aria-label',
                (checkbox.checked ? 'استثناء ' : 'إرجاع ') + item.name);
            checkbox.onchange = function() { toggleSelect(checkbox, !!item.is_dir); };
            row.appendChild(checkbox);

            let icon = document.createElement('span');
            icon.className = 'selected-browse-icon';
            icon.innerHTML = item.is_dir ? iconDir : fileIcon(item.name);
            row.appendChild(icon);

            let name = document.createElement('span');
            name.className = 'selected-browse-name';
            name.textContent = item.name;
            name.title = item.path;
            row.appendChild(name);

            if (excludedPaths.has(item.path)) {
                let excluded = document.createElement('span');
                excluded.className = 'excluded-badge';
                excluded.textContent = 'مستثنى';
                row.appendChild(excluded);
            }

            if (item.is_dir) {
                let expanded = selectedBrowseExpanded.has(item.path);
                let toggle = document.createElement('button');
                toggle.type = 'button';
                toggle.className = 'selected-browse-toggle' + (expanded ? ' open' : '');
                toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
                toggle.setAttribute('aria-label',
                    (expanded ? 'طي' : 'فتح') + ' المجلد ' + item.name);
                toggle.innerHTML = spriteIcon('ico-chevron-right');
                toggle.onclick = function() { toggleSelectedBrowse(item.path); };
                row.appendChild(toggle);
            }
            branch.appendChild(row);
            if (item.is_dir && selectedBrowseExpanded.has(item.path)) {
                branch.appendChild(renderSelectedBrowseBranch(item.path, depth + 1));
            }
        });
        return branch;
    }

    function updateSavedOperationControls() {
        let saveButton = document.getElementById('saveOperationBtn');
        let updateButton = document.getElementById('updateSavedOperationBtn');
        let activeLabel = document.getElementById('savedOperationActiveLabel');
        if (saveButton) saveButton.disabled = selectedPaths.size === 0;
        if (updateButton) {
            updateButton.style.display = activeSavedOperationId && selectedPaths.size ? '' : 'none';
            updateButton.disabled = selectedPaths.size === 0;
        }
        if (activeLabel) {
            activeLabel.textContent = activeSavedOperationId
                ? 'العملية الحالية: ' + activeSavedOperationName
                : '';
            activeLabel.style.display = activeSavedOperationId ? 'block' : 'none';
        }
    }

    function currentSavedOperationPayload(name) {
        return {
            name: name || '',
            paths: Array.from(selectedPaths.entries()).map(function(entry) {
                return {path: entry[0], is_dir: !!entry[1]};
            }),
            excludes: Array.from(excludedPaths),
            groups: copyGroups.map(function(group) {
                return {name: group.name, paths: group.paths.slice()};
            })
        };
    }

    function savedOperationConfigKey(payload) {
        return JSON.stringify({
            paths: (payload.paths || []).map(function(item) {
                return {path: item.path, is_dir: !!item.is_dir};
            }).sort(function(a, b) { return a.path.localeCompare(b.path); }),
            excludes: (payload.excludes || []).slice().sort(),
            groups: (payload.groups || []).map(function(group) {
                return {name: group.name, paths: (group.paths || []).slice().sort()};
            }).sort(function(a, b) { return a.name.localeCompare(b.name); })
        });
    }

    async function savedOperationsRequest(url, options) {
        let method = (options && options.method) || 'GET';
        let response;
        try {
            response = await fetch(url, Object.assign({cache: 'no-store'}, options || {}));
        } catch (networkError) {
            throw new Error('فشل الاتصال بالطلب ' + method + ' ' + url +
                ' (الإصدار ' + '{{ app_version }}' + '): ' +
                (networkError.message || 'تعذر الوصول إلى الخادم'));
        }
        let data = await response.json().catch(function() { return {}; });
        if (!response.ok) {
            let version = response.headers.get('X-SDMP3-Version') || '{{ app_version }}';
            throw new Error((data.error || 'تعذر تنفيذ الطلب') +
                ' [' + method + ' ' + url +
                '؛ الإصدار ' + version + ']');
        }
        return data;
    }

    function setSavedOperationsStatus(text, kind) {
        let el = document.getElementById('savedOperationsStatus');
        if (!el) return;
        el.textContent = text || '';
        el.className = 'settings-save-state' + (kind ? ' ' + kind : '');
        el.setAttribute('aria-busy', kind === 'is-loading' ? 'true' : 'false');
    }

    function openSavedOperationsModal() {
        document.getElementById('savedOperationsModal').style.display = 'flex';
        setSavedOperationsStatus('جارٍ تحميل قائمة العمليات المحفوظة...', 'is-loading');
        loadSavedOperationsList();
    }

    function closeSavedOperationsModal(event) {
        if (event && event.target.id !== 'savedOperationsModal') return;
        document.getElementById('savedOperationsModal').style.display = 'none';
    }

    async function loadSavedOperationsList() {
        setSavedOperationsStatus('جارٍ تحميل قائمة العمليات المحفوظة...', 'is-loading');
        let list = document.getElementById('savedOperationsList');
        if (list) list.innerHTML = '';
        try {
            let data = await savedOperationsRequest('/api/saved_operations');
            let operations = Array.isArray(data.operations) ? data.operations : [];
            list.innerHTML = '';
            if (!operations.length) {
                let empty = document.createElement('div');
                empty.className = 'settings-note';
                empty.textContent = 'لا توجد عمليات محفوظة بعد. افتح نافذة الملفات المحددة واضغط «حفظ».';
                list.appendChild(empty);
                setSavedOperationsStatus('', '');
                return;
            }
            setSavedOperationsStatus('عدد العمليات: ' + operations.length, '');
            operations.forEach(function(operation) {
                let row = document.createElement('div');
                row.className = 'saved-operation-row';
                let main = document.createElement('div');
                main.className = 'saved-operation-row-main';
                let name = document.createElement('div');
                name.className = 'saved-operation-row-name';
                name.textContent = operation.name;
                let details = document.createElement('div');
                details.className = 'saved-operation-row-detail';
                details.textContent = (operation.item_count || 0) + ' عنصر · ' +
                    (operation.group_count || 0) + ' مجلد تنظيم · ' +
                    (operation.excluded_count || 0) + ' مستثنى';
                main.appendChild(name);
                main.appendChild(details);
                let actions = document.createElement('div');
                actions.className = 'saved-operation-row-actions';
                [
                    ['load', 'تحميل'],
                    ['rename', 'إعادة تسمية'],
                    ['delete', 'حذف']
                ].forEach(function(action) {
                    let button = document.createElement('button');
                    button.type = 'button';
                    button.className = action[0] === 'load'
                        ? 'btn btn-primary btn-sm' : 'btn btn-outline btn-sm';
                    button.textContent = action[1];
                    if (action[0] === 'load') {
                        button.setAttribute('data-saved-operation-load', operation.id);
                    }
                    button.onclick = function() {
                        if (action[0] === 'load') loadSavedOperation(operation.id, button);
                        else if (action[0] === 'rename') {
                            openSaveOperationDialog('rename', operation.id, operation.name);
                        } else deleteSavedOperation(operation.id, operation.name);
                    };
                    actions.appendChild(button);
                });
                row.appendChild(main);
                row.appendChild(actions);
                list.appendChild(row);
            });
        } catch (error) {
            setSavedOperationsStatus(error.message || 'تعذر تحميل العمليات المحفوظة', 'err');
        }
    }

    function openSaveOperationDialog(mode, operationId, name) {
        saveOperationDialogMode = mode || 'create';
        saveOperationDialogId = operationId || '';
        let title = document.getElementById('saveOperationNameTitle');
        let input = document.getElementById('saveOperationNameInput');
        let submit = document.getElementById('saveOperationNameSubmit');
        document.getElementById('saveOperationNameError').textContent = '';
        title.textContent = saveOperationDialogMode === 'rename'
            ? 'إعادة تسمية العملية' : 'حفظ عملية جديدة';
        submit.textContent = saveOperationDialogMode === 'rename' ? 'حفظ الاسم' : 'حفظ';
        input.value = name || '';
        document.getElementById('saveOperationNameModal').style.display = 'flex';
        setTimeout(function() { input.focus(); input.select(); }, 0);
    }

    function closeSaveOperationDialog(event) {
        if (event && event.target.id !== 'saveOperationNameModal') return;
        document.getElementById('saveOperationNameModal').style.display = 'none';
    }

    async function submitSaveOperationName() {
        let input = document.getElementById('saveOperationNameInput');
        let errorEl = document.getElementById('saveOperationNameError');
        let submit = document.getElementById('saveOperationNameSubmit');
        let name = input.value.trim();
        errorEl.textContent = '';
        if (!name) { errorEl.textContent = 'اكتب اسماً للعملية.'; input.focus(); return; }
        submit.disabled = true;
        try {
            let url = '/api/saved_operations';
            let method = 'POST';
            let payload;
            if (saveOperationDialogMode === 'rename') {
                url += '/' + encodeURIComponent(saveOperationDialogId);
                method = 'PUT';
                payload = {name: name};
            } else {
                payload = currentSavedOperationPayload(name);
            }
            let data = await savedOperationsRequest(url, {
                method: method,
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            document.getElementById('saveOperationNameModal').style.display = 'none';
            if (saveOperationDialogMode === 'rename') {
                if (activeSavedOperationId === saveOperationDialogId) {
                    activeSavedOperationName = data.operation.name;
                }
            } else {
                activeSavedOperationId = data.operation.id;
                activeSavedOperationName = data.operation.name;
                setGroupStatus('تم حفظ العملية. اختر الوجهة يدوياً عند النسخ.');
            }
            updateSavedOperationControls();
            if (document.getElementById('savedOperationsModal').style.display === 'flex') {
                await loadSavedOperationsList();
            }
        } catch (error) {
            errorEl.textContent = error.message || 'تعذر حفظ العملية';
        } finally {
            submit.disabled = false;
        }
    }

    async function updateActiveSavedOperation() {
        if (!activeSavedOperationId || !selectedPaths.size) return;
        let button = document.getElementById('updateSavedOperationBtn');
        button.disabled = true;
        try {
            let payload = currentSavedOperationPayload(activeSavedOperationName);
            let data = await savedOperationsRequest(
                '/api/saved_operations/' + encodeURIComponent(activeSavedOperationId),
                {method: 'PUT', headers: {'Content-Type': 'application/json'},
                 body: JSON.stringify(payload)}
            );
            activeSavedOperationName = data.operation.name;
            setGroupStatus('تم تحديث العملية «' + activeSavedOperationName + '».');
            updateSavedOperationControls();
        } catch (error) {
            setGroupStatus(error.message || 'تعذر تحديث العملية المحفوظة');
        } finally {
            button.disabled = false;
        }
    }

    function deleteSavedOperation(operationId, name) {
        showConfirmModal('حذف العملية المحفوظة «' + name + '»؟ لن تُحذف أي ملفات من المصدر أو الوجهة.', async function() {
            try {
                await savedOperationsRequest('/api/saved_operations/' + encodeURIComponent(operationId),
                    {method: 'DELETE'});
                if (activeSavedOperationId === operationId) {
                    activeSavedOperationId = '';
                    activeSavedOperationName = '';
                    updateSavedOperationControls();
                }
                await loadSavedOperationsList();
            } catch (error) {
                setSavedOperationsStatus(error.message || 'تعذر حذف العملية', 'err');
            }
        });
    }

    function setSavedOperationLoadState(loading, activeButton) {
        let buttons = document.querySelectorAll('[data-saved-operation-load]');
        for (let i = 0; i < buttons.length; i++) {
            let button = buttons[i];
            button.disabled = !!loading;
            if (loading) {
                button.setAttribute('aria-disabled', 'true');
                if (button === activeButton) {
                    button.classList.add('is-loading');
                    button.setAttribute('aria-busy', 'true');
                    button.textContent = 'جارٍ التحميل...';
                }
            } else {
                button.removeAttribute('aria-disabled');
                button.removeAttribute('aria-busy');
                button.classList.remove('is-loading');
                button.textContent = 'تحميل';
            }
        }
        if (!loading) savedOperationLoadingId = '';
    }

    function savedOperationPathLabel(path) {
        // Report the file/folder name, not a directory chain: the warning is
        // about the handful of skipped items, not a repeat of saved paths.
        let parts = String(path || '').split('/').filter(Boolean);
        let label = parts.length ? parts[parts.length - 1] : String(path || '');
        return label.length > 180 ? '…' + label.slice(-179) : label;
    }

    function savedOperationIssueMessage(items) {
        if (!items || !items.length) return '';
        let folders = items.filter(function(item) { return !!item.is_dir; }).length;
        let files = items.length - folders;
        let names = items.map(function(item) {
            return '• ' + savedOperationPathLabel(item.path);
        });
        return 'تحذير: تم تجاهل ' + items.length + ' عنصر غير متاح (' +
            folders + ' مجلد، ' + files + ' ملف). تعذّر الوصول إلى المسار المحفوظ ' +
            '(قد يكون حُذف أو نُقل أو تغيّر نوعه)؛ سيتم نسخ العناصر المتاحة فقط.\\n' +
            names.join('\\n');
    }

    async function checkSavedOperationPaths(operation) {
        let unavailable = new Set();
        function inspectResult(items, result) {
            let missing = new Set(result.missing || []);
            let types = result.types || {};
            items.forEach(function(item) {
                if (missing.has(item.path) ||
                    types[item.path] !== (item.is_dir ? 'directory' : 'file')) {
                    unavailable.add(item.path);
                }
            });
        }
        async function check(items) {
            return savedOperationsRequest('/api/path_status', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({paths: items})
            });
        }
        try {
            inspectResult(operation.paths, await check(operation.paths));
        } catch (batchError) {
            // A failed batch must not reject the operation. Check each item
            // independently so an error naming one source cannot hide the rest.
            // These workers only inspect metadata; copying remains sequential.
            let cursor = 0;
            async function worker() {
                while (cursor < operation.paths.length) {
                    let item = operation.paths[cursor++];
                    try {
                        inspectResult([item], await check([item]));
                    } catch (itemError) {
                        unavailable.add(item.path);
                    }
                }
            }
            await Promise.all(Array.from(
                {length: Math.min(4, operation.paths.length)}, worker
            ));
        }
        return Array.from(unavailable);
    }

    async function loadSavedOperation(operationId, activeButton) {
        if (savedOperationLoadingId) return;
        savedOperationLoadingId = String(operationId);
        let phase = 'قراءة بيانات العملية المحفوظة';
        setSavedOperationLoadState(true, activeButton);
        setSavedOperationsStatus('جارٍ تحميل العملية المحفوظة...', 'is-loading');
        try {
            let data = await savedOperationsRequest(
                '/api/saved_operations/' + encodeURIComponent(operationId)
            );
            let operation = data.operation;
            phase = 'فحص وجود الملفات والمجلدات';
            setSavedOperationsStatus('جارٍ فحص الملفات والمجلدات المحفوظة...', 'is-loading');
            let unavailable = new Set(await checkSavedOperationPaths(operation));
            let sizeWarning = '';

            // Keep size calculation from blocking restore. A saved selection is
            // still useful when a card/filesystem refuses a folder walk; missing
            // paths are checked separately above and never enter the selection.
            let sizeData = {sizes: {}};
            let availableBeforeSizing = operation.paths.filter(function(item) {
                return !unavailable.has(item.path);
            });
            if (availableBeforeSizing.length) {
                try {
                    sizeData = await savedOperationsRequest('/api/sizes', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            paths: availableBeforeSizing.map(function(item) { return item.path; }),
                            excludes: operation.excludes || [],
                            include_types: true
                        })
                    });
                    (Array.isArray(sizeData.missing) ? sizeData.missing : []).forEach(function(path) {
                        unavailable.add(path);
                    });
                    let sizeTypes = sizeData.types || {};
                    availableBeforeSizing.forEach(function(item) {
                        let type = sizeTypes[item.path];
                        if (type && type !== (item.is_dir ? 'directory' : 'file')) {
                            unavailable.add(item.path);
                        }
                    });
                } catch (sizeError) {
                    sizeWarning += (sizeWarning ? '\\n' : '') +
                        'تعذر حساب الأحجام الآن، لكن تم تحميل العناصر المتاحة؛ يمكنك متابعة النسخ.';
                }
            }
            let unavailablePaths = Array.from(unavailable);
            let unavailableItems = operation.paths.filter(function(item) {
                return unavailable.has(item.path);
            });
            setSavedOperationsStatus('', '');
            let different = selectedPaths.size > 0 &&
                savedOperationConfigKey(currentSavedOperationPayload()) !==
                savedOperationConfigKey(operation);
            let proceed = function() {
                try {
                    phase = 'تحديد الملفات السليمة وتطبيق التنظيم';
                    if (applySavedOperation(operation, sizeData, unavailablePaths, sizeWarning)) {
                        closeSavedOperationsModal();
                    }
                } catch (error) {
                    setSavedOperationsStatus(
                        'فشل تحميل العملية في مرحلة «تحديد الملفات وتطبيق التنظيم»: ' +
                        (error.message || 'خطأ غير معروف'), 'err');
                }
            };
            if (different) {
                phase = 'انتظار تأكيد استبدال التحديد الحالي';
                let message = 'سيتم استبدال التحديد الحالي دون بدء النسخ.';
                if (unavailableItems.length) {
                    message += '\\n' + unavailableItems.length +
                        ' عنصر غير متاح سيتم تجاهله، وستُحمّل العناصر السليمة فقط.';
                }
                setSavedOperationsStatus('تم فحص العملية؛ أكّد الاستبدال للمتابعة.', '');
                showConfirmModal(message, proceed);
            } else {
                proceed();
            }
        } catch (error) {
            setSavedOperationsStatus(
                'فشل تحميل العملية في مرحلة «' + phase + '»: ' +
                (error.message || 'خطأ غير معروف'), 'err');
        } finally {
            setSavedOperationLoadState(false);
        }
    }

    function applySavedOperation(operation, sizeData, unavailablePaths, sizeWarning) {
        let missingSet = new Set(unavailablePaths || []);
        let validPaths = operation.paths.filter(function(item) {
            return !missingSet.has(item.path);
        });
        selectedPaths = new Map(validPaths.map(function(item) {
            return [item.path, !!item.is_dir];
        }));
        let validSet = new Set(validPaths.map(function(item) { return item.path; }));
        excludedPaths = new Set((operation.excludes || []).filter(function(path) {
            return validPaths.some(function(item) {
                let root = item.path;
                while (root.length > 1 && root.endsWith('/')) root = root.slice(0, -1);
                return path.startsWith(root === '/' ? '/' : root + '/');
            });
        }));
        copyGroups = (operation.groups || []).map(function(group) {
            return {id: nextGroupId++, name: group.name,
                paths: (group.paths || []).filter(function(path) { return validSet.has(path); })};
        });
        bulkSelectedPaths.clear();
        selectedSizes = {};
        let sizes = (sizeData && sizeData.sizes) || {};
        validPaths.forEach(function(item) {
            selectedSizes[item.path] = sizes[item.path] ||
                {bytes: 0, formatted: sizeWarning ? 'غير محسوب' : '0 B', owner: item.path};
        });
        activeSavedOperationId = operation.id;
        activeSavedOperationName = operation.name;
        selectionSizeSig = selectionSignature();
        refreshCheckboxStates();
        renderSelectedRows();
        updateSelectedTotal(Array.from(selectedPaths.keys()));
        document.getElementById('selectedModal').style.display = 'flex';
        let unavailableItems = operation.paths.filter(function(item) {
            return missingSet.has(item.path);
        });
        let issueMessage = savedOperationIssueMessage(unavailableItems);
        if (sizeWarning) {
            issueMessage += (issueMessage ? '\\n' : '') + sizeWarning;
        }
        if (issueMessage) {
            setGroupStatus(issueMessage, 'warning');
        } else {
            setGroupStatus('تم تحميل العملية؛ راجع التحديد ثم اختر الوجهة وابدأ النسخ يدوياً.');
        }
        updateSavedOperationControls();
        scheduleUiSave();
        return true;
    }

    function closeSelectedModal(e) {
        if (e.target.id !== 'selectedModal') return;
        e.target.style.display = 'none';
        cancelSmartPreview();
    }

    function startTransfer(confirmMerge, confirmExtract) {
        if(selectedPaths.size === 0) return alert('اختر ملفات أولاً.');
        let dest = document.getElementById('custom_dest_path').value;
        if(!dest) return alert('حدد مسار الوجهة.');
        // Both calls need the button click: Chrome only unlocks WebAudio and
        // shows the notification prompt from a real user gesture.
        unlockCompletionSound();
        maybeAskNotificationPermission();
        let verifyEl = document.getElementById('verifyAfterCopy');
        let verifyContentEl = document.getElementById('verifyContentAfterCopy');
        let verifyContent = verifyContentEl ? verifyContentEl.checked : false;

        let reqData = {
            dest: dest,
            paths: Array.from(selectedPaths.keys()),
            excludes: Array.from(excludedPaths),
            groups: copyGroups
                .filter(function(g) { return g.paths.length > 0; })
                .map(function(g) { return { name: g.name, paths: g.paths.slice() }; }),
            verify: (verifyEl ? verifyEl.checked : true) || verifyContent,
            verify_content: verifyContent,
            confirm_merge: !!confirmMerge,
            confirm_extract: !!confirmExtract
        };

        fetch('/transfer/start', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(reqData)
        }).then(r=>r.json()).then(data=>{
            // An existing group folder needs an explicit OK: a merge may replace
            // files inside it, so it must never happen silently.
            if (data.needs_group_confirm) {
                showConfirmModal(data.error, function(){ startTransfer(true, false); });
                return;
            }
            // Unpacking an archive into an existing folder is a merge too, and
            // it can replace files: ask once, before a single byte is written.
            if (data.needs_extract_confirm) {
                showConfirmModal(data.error, function(){ startTransfer(false, true); });
                return;
            }
            // A refused copy (dead card, no permission, self-copy) is shown in
            // the modal banner so the whole reason stays readable, not in alert().
            if(data.error) showInlineError('تعذر بدء النسخ: ' + data.error);
            else { showProgressModal(); checkTransferStatus(); }
        });
    }

    function startSpoofOnly() {
        let dest = document.getElementById('custom_dest_path').value;
        let spoofDate = document.getElementById('spoofDateInput').value;
        if(!spoofDate) return alert('حدد التاريخ والوقت أولاً.');

        // Applying spoof is the button's whole meaning: it is allowed to touch
        // the destination already shown above it. No confirmation dialog — the
        // empty-destination case falls back to the internal storage, exactly as
        // the hint under the field says.
        if (!dest) dest = '/sdcard';

        fetch('/transfer/spoof_only', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({dest: dest, spoof_date: spoofDate})
        }).then(r=>r.json()).then(data=>{ if(data.error) showInlineError('تعذر بدء التمويه: ' + data.error); else { showProgressModal(); checkTransferStatus(); } });
    }

    // Card check. It writes real data (32MB, one end flush — the same shape as
    // a copy), so its result is the only honest answer to «هل الكارت يكتب؟», and
    // the result is what الإعدادات › عام shows. It runs from the destination
    // picker, where the folder to write into is already on screen.
    function runDiagnostic() {
        if (transferRunning) { restoreProgressModal(); return; }
        let pickerView = document.getElementById('currentDestView');
        let pickerOpen = document.getElementById('destBrowserModal').style.display === 'flex';
        let dest = (pickerOpen && pickerView && pickerView.value)
            ? pickerView.value
            : document.getElementById('custom_dest_path').value;
        if (!dest) { alert('افتح نافذة الوجهة واختر مجلداً أولاً.'); return; }

        renderTransferReport([]);
        showProgressModal();
        document.getElementById('modalTitle').innerText = 'جاري فحص الكارت…';
        document.getElementById('cancelProgressBtn').style.display = 'none';
        document.getElementById('countText').innerText = '';
        document.getElementById('speedText').textContent = '';
        document.getElementById('currentFile').innerText = 'كتابة 32 ميجابايت في ' + dest;

        fetch('/api/benchmark', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({dest: dest})
        }).then(r=>r.json()).then(data => {
            document.getElementById('modalTitle').innerText = 'فحص الكارت';
            if (data.error) {
                rememberCardCheck(dest, {error: data.error});
                document.getElementById('currentFile').innerText = dest;
                showInlineError('تعذّرت الكتابة على هذا المجلد: ' + data.error);
                return;
            }
            rememberCardCheck(dest, data);
            let estMin = Math.round((data.est_10gb || 0) / 60);
            let est = estMin >= 60 ? (estMin / 60).toFixed(1) + ' ساعة' : Math.max(1, estMin) + ' دقيقة';
            renderTransferReport([
                reportRow('ico-check', 'الكارت يكتب بنجاح', data.speed_mbps + ' MB/s', 'ok'),
                reportRow('ico-clock', 'نسخ 10 جيجابايت', 'حوالي ' + est),
                reportRow('ico-folder', 'المجلد', dest)
            ]);
            document.getElementById('currentFile').innerText =
                'القياس بنفس طريقة النسخ الحقيقية (' + (data.size_mb || 32) + ' ميجابايت، دفعة واحدة).';
            document.getElementById('currentFile').style.color = 'var(--text-muted)';
            document.getElementById('cancelProgressBtn').style.display = 'none';
            document.getElementById('closeProgressBtn').style.display = 'block';
            document.getElementById('failedText').innerText = '';
        }).catch(function() {
            document.getElementById('modalTitle').innerText = 'فحص الكارت';
            rememberCardCheck(dest, {error: 'تعذر الاتصال بالخادم'});
            showInlineError('تعذر الاتصال بالخادم أثناء فحص الكارت.');
        });
    }

    let maskMode = 'noise';   // 'none' | 'noise' | 'file'
    let customUploaded = null;  // original filename of uploaded custom mask
    // False once /api/tts/voices says edge-tts is not installed: the MP3 switch
    // is then disabled, so nothing silently fails at creation time.
    let ttsAvailable = true;

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
        if(mode === 'none') hint.innerHTML = 'بلا تمويه.';
        else if(mode === 'noise') hint.innerHTML = 'ضوضاء منخفضة لدقيقتين قبل الفهرس.';
        else hint.innerHTML = 'ملف MP3 في بداية الفهرس.';
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
                // Shizuku is optional; it is a quiet row in الإعدادات › عام now.
                var sEl = document.getElementById('generalShizukuStatus');
                if (sEl) {
                    if (!s.installed) { sEl.textContent = 'غير مثبّت'; sEl.className = 'status-value'; }
                    else if (!s.running) { sEl.textContent = 'متوقف'; sEl.className = 'status-value warn'; }
                    else { sEl.textContent = 'مفعّل'; sEl.className = 'status-value ok'; }
                }
                // No card claim from this response: `sd.writable` is a 5-byte
                // probe with a 5s timeout and reports a slow card as dead. The
                // card row is filled by the real write test (renderCardStatus).
                renderCardStatus();

                if (s.checked === false && !isRetry) {
                    setTimeout(function(){ loadSystemInfo(true, true); }, 2000);
                }
            })
            .catch(function(){});
    }

    // ---- card check (the only honest source of «هل الكارت يكتب؟») ----------
    // A measurement, not a probe: /api/benchmark writes 32 MB the same way the
    // copy does. Its result is remembered so the settings row can answer
    // without touching the card again.
    const CARD_CHECK_KEY = 'sdmp3_card_check';

    function renderCardStatus() {
        let el = document.getElementById('generalCardStatus');
        if (!el) return;
        let saved = null;
        try { saved = JSON.parse(localStorage.getItem(CARD_CHECK_KEY) || 'null'); } catch (_e) { saved = null; }
        el.className = 'status-value';
        if (!saved || !saved.at) {
            el.textContent = 'لم يُفحص بعد';
            el.title = 'افحصه من نافذة اختيار الوجهة: زر «فحص الكارت»';
            return;
        }
        el.title = saved.path || '';
        if (saved.error) {
            el.textContent = 'تعذّرت الكتابة · ' + relativeTime(saved.at);
            el.className = 'status-value bad';
            el.title = saved.error;
        } else {
            el.textContent = saved.mbps + ' MB/s · ' + relativeTime(saved.at);
            el.className = 'status-value ok';
        }
    }

    function rememberCardCheck(path, data) {
        let entry = {path: path || '', at: Math.floor(Date.now() / 1000)};
        if (data && data.error) {
            entry.error = String(data.error);
        } else if (data) {
            entry.mbps = data.speed_mbps;
        }
        try { localStorage.setItem(CARD_CHECK_KEY, JSON.stringify(entry)); } catch (_e) {}
        renderCardStatus();
    }

    document.addEventListener('DOMContentLoaded', function() {
        initializePhotoBackground();
        document.querySelectorAll('.seg.option').forEach(el => {
            el.addEventListener('click', function() { setMaskMode(this.getAttribute('data-mode')); });
        });
        const fin = document.getElementById('jocCustomAudio');
        if(fin) fin.addEventListener('change', function(ev) {
            let f = ev.target.files && ev.target.files[0];
            if(f) uploadCustomAudio(f);
        });
        const sourceSort = document.getElementById('sourceSort');
        if (sourceSort) {
            sourceSort.value = currentSort;
            sourceSort.addEventListener('change', function() {
                currentSort = this.value;
                localStorage.setItem('sdmp3_source_sort', currentSort);
                loadSubFolders(currentSourceRoot, document.getElementById('treeContainer'));
            });
        }
        // Load the saved index settings once, then fill the voice list (which
        // needs them to restore the chosen voice).
        fetch('/api/index_settings').then(function(r) { return r.json(); }).then(function(data) {
            if (data.ok && data.settings) {
                savedIndexSettings = data.settings;
                applyIndexSettingsToUI(savedIndexSettings);
            }
            loadTtsVoices();
        }).catch(function() {
            toggleMp3Index();
            setMaskMode(maskMode);
            renderJocSummary();
            loadTtsVoices();
        });
        let soundCb = document.getElementById('completionSoundCheck');
        if (soundCb) soundCb.checked = soundEnabled();
        refreshNotificationControls();
        loadSystemInfo(false, false);
        setInterval(function(){ loadSystemInfo(false, true); }, 120000);
    });

    // ---- keep the most recently opened modal on top of any other open modal ----
    // (e.g. rename/input or settings opening over the selected-items window must
    // never appear behind it). z-index comes from .modal-front in CSS.
    (function() {
        let seq = 0;
        let visibleSeq = {};
        function syncModalFront() {
            let frontId = null;
            let frontSeq = -1;
            document.querySelectorAll('.modal').forEach(function(m) {
                let visible = (m.style.display === 'flex' || m.style.display === 'block');
                if (visible && !(m.id in visibleSeq)) visibleSeq[m.id] = ++seq;
                if (!visible) { delete visibleSeq[m.id]; m.classList.remove('modal-front'); }
                if (visible && visibleSeq[m.id] > frontSeq) { frontSeq = visibleSeq[m.id]; frontId = m.id; }
            });
            document.querySelectorAll('.modal').forEach(function(m) {
                // Toggle (not add): a modal that was front and is still visible
                // must lose the class, otherwise two dialogs share z-index 1001
                // and DOM order decides - which puts the *older* one on top.
                m.classList.toggle('modal-front', m.id === frontId);
            });
        }
        const obs = new MutationObserver(syncModalFront);
        document.querySelectorAll('.modal').forEach(function(m) { obs.observe(m, { attributes: true, attributeFilter: ['style'] }); });
        syncModalFront();
    })();

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
            // Restore the user's saved voice if present.
            if (savedIndexSettings && savedIndexSettings.tts_voice) {
                sel.value = savedIndexSettings.tts_voice;
            }
            if(!data.available) {
                // Do NOT replace #ttsOptions' content: its controls are part of
                // the saved form, and destroying them made «إنشاء» throw when
                // edge-tts was missing. Disable the switch instead and say why.
                ttsAvailable = false;
                sel.disabled = true;
                let notice = document.getElementById('ttsNotice');
                if (notice) notice.innerHTML = '<small style="color:var(--danger-ink); display:block; margin-bottom:8px;">حزمة edge-tts غير مثبتة (يرجى تشغيل: pip install edge-tts)</small>';
                let cb = document.getElementById('jocMp3Index');
                if (cb) { cb.checked = false; cb.disabled = true; }
                toggleMp3Index();
            }
            renderJocSummary();
        }).catch(()=>{});
    }

    function makeRandomName(len) {
        const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
        let s = '';
        for (let i = 0; i < len; i++) s += chars[Math.floor(Math.random() * chars.length)];
        return s;
    }

    // ---- persisted index settings -------------------------------------------
    // These belong to الإعدادات › الفهرس and are written by its save button only.
    // The create dialog reads them; it never writes them (that was the old,
    // confusing behaviour where pressing "إنشاء" silently saved settings).
    function applyIndexSettingsToUI(s) {
        const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
        const setc = (id, val) => { const el = document.getElementById(id); if (el) el.checked = !!val; };
        set('jocStart', s.joc_start);
        set('jocLimit', s.joc_limit);
        set('jocMode', s.joc_mode);
        set('jocTemplate', s.joc_template);
        setc('jocStrip', s.joc_strip);
        setc('jocDownloadTxt', s.joc_download_txt);
        setc('jocReviewTxt', s.joc_review_txt);
        setc('jocMp3Index', s.joc_mp3_index && ttsAvailable);
        setc('jocMp3ToDest', s.mp3_to_dest);
        set('ttsRate', s.tts_rate);
        if (s.mask_mode) setMaskMode(s.mask_mode);
        toggleMp3Index();
        renderJocSummary();
    }

    function gatherIndexSettings() {
        const g = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
        const gc = (id) => { const el = document.getElementById(id); return el ? el.checked : false; };
        return {
            joc_start: g('jocStart'),
            joc_limit: g('jocLimit'),
            joc_mode: g('jocMode'),
            joc_template: g('jocTemplate'),
            joc_strip: gc('jocStrip'),
            joc_download_txt: gc('jocDownloadTxt'),
            joc_review_txt: gc('jocReviewTxt'),
            joc_mp3_index: gc('jocMp3Index'),
            mp3_to_dest: gc('jocMp3ToDest'),
            tts_voice: g('ttsVoice'),
            tts_rate: g('ttsRate'),
            mask_mode: maskMode || 'noise',
        };
    }

    function saveIndexSettingsNow() {
        // In draft mode (opened by «إنشاء فهرس JOC») this form is a scratch pad:
        // writing it here would overwrite the user's saved defaults, which is
        // exactly what the draft exists to prevent.
        if (indexDraftActive) return;
        setSettingsState('جارٍ الحفظ...', '', 'indexSaveState');
        fetch('/api/index_settings', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(gatherIndexSettings())
        })
            .then(function(r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function(res) {
                if (!res.ok) { setSettingsState(res.data.error || 'تعذر حفظ إعدادات الفهرس', 'err', 'indexSaveState'); return; }
                // The server echoes the canonical shape, so the form shows what
                // was actually written (clamped numbers, validated booleans).
                savedIndexSettings = res.data.settings;
                applyIndexSettingsToUI(savedIndexSettings);
                setSettingsState('تم حفظ إعدادات الفهرس.', 'ok', 'indexSaveState');
                setTimeout(function() { setSettingsState('', '', 'indexSaveState'); }, 2500);
            })
            .catch(function() { setSettingsState('تعذر الاتصال بالخادم', 'err', 'indexSaveState'); });
    }

    // A read-out of the saved settings in the create dialog, so the user does
    // not have to open settings to remember what the index will look like.
    const JOC_MODE_LABELS = {'1': 'مجلدات فقط', '2': 'مجلدات وملفات', '3': 'المجلد الحالي فقط'};
    const JOC_TEMPLATE_LABELS = {'4': 'الاسم : الرقم.', '1': 'الرقم : الاسم .', '2': 'الاسم من الرقم', '3': 'الاسم من رقم الرقم .'};
    const MASK_LABELS = {none: 'بدون', noise: 'ضوضاء خفيفة', file: 'ملف مخصص'};

    function renderJocSummary() {
        let el = document.getElementById('jocSummary');
        if (!el) return;
        const g = (id) => { const e = document.getElementById(id); return e ? e.value : ''; };
        const gc = (id) => { const e = document.getElementById(id); return !!(e && e.checked); };
        let rows = [];
        rows.push('الوضع: <b>' + esc(JOC_MODE_LABELS[g('jocMode')] || '—') + '</b>');
        rows.push('التنسيق: <b>' + esc(JOC_TEMPLATE_LABELS[g('jocTemplate')] || '—') + '</b>');
        rows.push('البداية: <b>' + esc(g('jocStart')) + '</b> • الحد الأقصى للحروف: <b>' + esc(g('jocLimit')) + '</b>');
        if (gc('jocStrip')) rows.push('يُحذف الإنجليزي والأرقام والرموز من الأسماء.');
        if (gc('jocDownloadTxt')) rows.push('سيُحمَّل ملف <b>.txt</b> بعد الإنشاء.');
        if (gc('jocReviewTxt')) rows.push('ستُفتح مراجعة النص قبل المتابعة.');
        if (gc('jocMp3Index')) {
            let voiceSel = document.getElementById('ttsVoice');
            let voiceLabel = (voiceSel && voiceSel.selectedIndex >= 0)
                ? voiceSel.options[voiceSel.selectedIndex].textContent.split(' — ')[0]
                : g('ttsVoice');
            let line = 'سيُولَّد <b>MP3</b> بصوت <b>' + esc(voiceLabel) + '</b>';
            line += gc('jocMp3ToDest') ? ' ويُحفظ في <b>الوجهة</b> مباشرة.' : ' ويُحمَّل على الجهاز.';
            rows.push(line);
            if (maskMode && maskMode !== 'none') rows.push('مع تمويه: <b>' + esc(MASK_LABELS[maskMode] || maskMode) + '</b>.');
        } else {
            rows.push('بدون تحويل صوتي.');
        }
        el.innerHTML = '<div class="summary-title">'
            + '<svg class="icon" style="width:15px;height:15px;color:var(--accent);" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>'
            + 'ما سيتم إنشاؤه'
            + '</div>' + rows.map(function(r) { return '<div>' + r + '</div>'; }).join('');
    }

    // «إنشاء فهرس JOC» opens the index pane as a throw-away draft first: the
    // fields are editable, but nothing is written to index_settings.json because
    // of a creation. Closing the pane returns to the saved defaults.
    function openJocModal() {
        let dest = document.getElementById('custom_dest_path').value;
        if(!dest) return alert('يرجى تحديد مسار الوجهة أولاً.');
        openIndexDraft(true);
    }

    // The name/summary step, reached from «متابعة إلى الإنشاء» (and from the
    // create dialog's «الإعدادات» link when the user goes back and forth).
    function openJocCreate() {
        // The name is generated per creation and never remembered. It is the one
        // field this dialog owns, and it stays editable until "إنشاء".
        document.getElementById('jocFileName').value = makeRandomName(5);
        renderJocSummary();
        document.getElementById('jocModal').style.display = 'flex';
    }

    let currentTtsFileName = null;
    let currentJocIndexText = null;
    let pendingJocActions = null;
    // Persisted index settings, loaded once from ~/.local/state/sdmp3/index_settings.json.
    let savedIndexSettings = null;
    // Name of the .txt the current index was created with (the create field may
    // be re-generated before the user copies the text).
    let currentJocTxtName = '';

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
        copyTextToClipboard(currentJocIndexText, 'تم نسخ الفهرس بنجاح <b>' + esc((currentJocTxtName || 'index.txt')) + '</b> — الصقه في أي مكان.');
    }

    function copyEditTxt() {
        let txtArea = document.getElementById('editTxtArea');
        let txt = txtArea ? txtArea.value : '';
        copyTextToClipboard(txt, 'تم نسخ النص من المحرر بنجاح — الصقه في أي مكان.');
    }

    function submitJocIndex() {
        // Settings are NOT saved here: they are saved by the save button in
        // الإعدادات › الفهرس. This dialog only creates.
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
        let saveToDest = document.getElementById('jocMp3ToDest').checked;
        let voice = document.getElementById('ttsVoice').value || 'ar-AE-HamdanNeural';
        let rate = document.getElementById('ttsRate').value || '+0%';
        currentJocTxtName = txtName;
        pendingJocActions = { txtName: txtName, downloadTxt: downloadTxt, doMp3: doMp3, mp3Name: mp3Name,
                              voice: voice, rate: rate, obfuscate: obfuscate,
                              saveToDest: saveToDest, saveDir: dest };
        
        document.getElementById('jocModal').style.display = 'none';
        // Everything this run needs is already captured in pendingJocActions,
        // so the pane can go back to the saved defaults immediately: a draft must
        // never leak into the next creation.
        resetIndexForm();
        
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
        showInlineNotify(spriteIcon('ico-check') + ' تم إنشاء الفهرس النصي <b>' + esc(txtName) + '</b> بنجاح.'
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
                    obfuscate: a.obfuscate,
                    save_to_dest: !!a.saveToDest,
                    save_dir: a.saveDir || ''
                })
            }).then(r=>r.json()).then(r2=>{
                if(r2.error) { showInlineError(r2.error); resetTransfer(); return; }
            }).catch(e=>{ resetTransfer(); showInlineError('حدث خطأ أثناء تشغيل TTS.'); });
        }
    }

    // Success card, shared by both outcomes (saved into the destination or
    // downloaded to the phone) so the buttons never diverge.
    function showTtsSuccessNotify(mp3Name, savedPath) {
        let where = savedPath
            ? 'تم حفظ MP3 الصوتي <b>' + esc(mp3Name) + '</b> في الوجهة:<div class="ltr-input" style="margin-top:6px; font-size:calc(11.5px * var(--font-scale));">' + esc(savedPath) + '</div>'
            : 'تم إنشاء وتحميل MP3 الصوتي <b>' + esc(mp3Name) + '</b>، وكذا الفهرس النصي <b>' + esc(currentJocTxtName || '') + '</b>.';
        showInlineNotify(spriteIcon('ico-check') + ' ' + where
            + '<div style="display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;">'
            + '<button class="btn btn-primary btn-sm" style="flex:1;" onclick="copyJocIndex()">'
            + '<svg class="icon" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>'
            + 'نسخ الفهرس</button>'
            + '<button class="btn btn-outline btn-sm" style="flex:1;" onclick="openEditTxtModal(1)">'
            + '<svg class="icon" viewBox="0 0 24 24"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"></path></svg>'
            + 'تحرير النص</button>'
            + '</div>', 'success');
    }

    // Called once the TTS run reports success. `snapshot` is the poll payload:
    // `tts_saved_path` is set when the server wrote the MP3 into the destination.
    function downloadTtsFile(snapshot) {
        let savedPath = (snapshot && snapshot.tts_saved_path) || '';
        let mp3Name = (snapshot && snapshot.tts_name) || currentTtsFileName;
        if (savedPath) {
            // Nothing to download: refresh the destination listing so the new
            // file appears where the user expects it.
            if (pendingJocActions && pendingJocActions.saveDir === document.getElementById('currentDestView').value) {
                loadDestBrowser(pendingJocActions.saveDir);
            }
            showTtsSuccessNotify(mp3Name, savedPath);
            currentTtsFileName = null;
            return;
        }
        if(!currentTtsFileName) return;
        let d = document.createElement('a');
        d.href = '/api/tts/download?name=' + encodeURIComponent(currentTtsFileName);
        d.download = currentTtsFileName;
        document.body.appendChild(d);
        d.click();
        document.body.removeChild(d);
        showTtsSuccessNotify(currentTtsFileName, '');
        currentTtsFileName = null;
    }

    let progressModalVisible = false;
    let progressMinimized = false;
    let transferRunning = false;
    let terminalTransferData = null;
    const BROWSER_NOTIFY_KEY = 'sdmp3_browser_notify';
    const COMPLETION_SOUND_KEY = 'sdmp3_completion_sound';
    const NOTIFY_PROMPT_KEY = 'sdmp3_notify_prompted';
    const NOTIFIED_RUN_KEY = 'sdmp3_notified_run';
    let notificationSoundContext = null;
    let lastNotifiedTransferKey = '';
    let activeTransferKey = '';
    let appToastTimer = null;
    // Queue hook for the upcoming scheduled copy: the next batch only has to
    // push {label, sizeText, ...} and call renderQueue(). Nothing enqueues yet.
    let transferQueue = [];

    function showProgressModal() {
        progressMinimized = false;
        terminalTransferData = null;
        hideAppToast();
        let copyDetailsBtn = document.getElementById('copyTransferDetailsBtn');
        if (copyDetailsBtn) {
            copyDetailsBtn.style.display = 'none';
            copyDetailsBtn.textContent = 'نسخ التفاصيل';
        }
        document.getElementById('progressModal').style.display = 'flex';
        progressModalVisible = true;
        document.getElementById('cancelProgressBtn').style.display = 'block';
        document.getElementById('closeProgressBtn').style.display = 'none';
        document.getElementById('timeTakenDiv').style.display = 'none';
        document.getElementById('cardWarningBox').style.display = 'none';
        document.getElementById('inlineNotify').style.display = 'none';
        document.getElementById('currentFile').style.color = 'var(--text-muted)';
        document.getElementById('minimizeProgressBtn').style.display = '';
    }

    function copyTransferDetails() {
        if (!terminalTransferData) return;
        let data = terminalTransferData;
        let versionEl = document.getElementById('appVersionPill');
        let lines = [
            'SD Card Coffee — ' + (versionEl ? versionEl.textContent.trim() : ''),
            'الملف: ' + (data.file || '—'),
            'الخطأ: ' + (data.log || '—'),
            'تحذير تثبيت البيانات: ' + (data.flush_warning || '—'),
            'العناصر الفاشلة:'
        ];
        if (data.errors && data.errors.length) {
            data.errors.forEach(function(item) { lines.push('- ' + item); });
        } else {
            lines.push('لا يوجد');
        }
        lines.push('عناصر تعذر التحقق منها:');
        if (data.verify_failed && data.verify_failed.length) {
            data.verify_failed.forEach(function(item) { lines.push('- ' + item); });
        } else {
            lines.push('لا يوجد');
        }
        let text = lines.join('\\n');
        let done = function() {
            let btn = document.getElementById('copyTransferDetailsBtn');
            if (btn) {
                btn.textContent = 'تم النسخ';
                setTimeout(function() {
                    if (btn && btn.style.display !== 'none') btn.textContent = 'نسخ التفاصيل';
                }, 1800);
            }
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done).catch(function() {
                fallbackCopyDeleteErrors(text, done);
            });
        } else {
            fallbackCopyDeleteErrors(text, done);
        }
    }

    // The dialog is an interruption, so it folds into the action bar instead of
    // covering the page: browsing and selecting the next batch stay possible.
    function minimizeProgressModal() {
        let modal = document.getElementById('progressModal');
        if (modal) modal.style.display = 'none';
        progressModalVisible = false;
        progressMinimized = true;
    }

    function restoreProgressModal() {
        let modal = document.getElementById('progressModal');
        if (!modal) return;
        progressMinimized = false;
        modal.style.display = 'flex';
        progressModalVisible = true;
    }

    function barPrimaryAction() {
        if (transferRunning) restoreProgressModal();
        else startTransfer();
    }

    function renderQueue() {
        let block = document.getElementById('queueBlock');
        let rows = document.getElementById('queueRows');
        let count = document.getElementById('queueCount');
        let badge = document.getElementById('barQueueBadge');
        if (!block || !rows) return;
        let n = transferQueue.length;
        block.style.display = n ? 'block' : 'none';
        if (count) count.textContent = n ? String(n) : '';
        if (badge) {
            badge.style.display = n ? '' : 'none';
            badge.textContent = n ? ('+' + n + ' في الانتظار') : '';
        }
        rows.innerHTML = n ? transferQueue.map(function(job, i) {
            return '<div class="queue-row"><span class="num">' + (i + 1) + '</span>'
                + '<span class="queue-row-label">' + esc(job.label || '') + '</span>'
                + '<span class="queue-row-size num">' + esc(job.sizeText || '') + '</span></div>';
        }).join('') : '';
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
            el.style.background = 'var(--ok-soft)';
            el.style.borderColor = 'var(--ok-line)';
            el.style.color = 'var(--success)';
        } else {
            el.style.background = 'var(--danger-soft)';
            el.style.borderColor = 'var(--danger)';
            el.style.color = 'var(--danger-ink)';
        }
        document.getElementById('cancelProgressBtn').style.display = 'none';
        document.getElementById('closeProgressBtn').style.display = 'block';
    }
    // Small inline sprite icons for generated notices: emoji would carry their
    // own colour and metrics into a themed panel.
    function spriteIcon(id) {
        return '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use href="#' + id + '"></use></svg>';
    }

    // ---- completion alerts: page toast, OS notification, short chime -------
    // The terminal snapshot arrives every second until the next run. A run key
    // (action + server start time) makes the alert fire once, even across a
    // refresh in the same tab; a new copying snapshot clears it.
    function getNotifyPref() {
        try { return localStorage.getItem(BROWSER_NOTIFY_KEY); } catch (_e) { return null; }
    }
    function setNotifyPref(enabled) {
        try { localStorage.setItem(BROWSER_NOTIFY_KEY, enabled ? '1' : '0'); } catch (_e) {}
    }
    function soundEnabled() {
        try {
            let value = localStorage.getItem(COMPLETION_SOUND_KEY);
            return value === null || value === '1';
        } catch (_e) { return true; }
    }
    function runAlreadyNotified(key) {
        if (lastNotifiedTransferKey === key) return true;
        try { return sessionStorage.getItem(NOTIFIED_RUN_KEY) === key; } catch (_e) { return false; }
    }
    function markRunNotified(key) {
        lastNotifiedTransferKey = key;
        try { sessionStorage.setItem(NOTIFIED_RUN_KEY, key); } catch (_e) {}
    }
    function clearRunNotification() {
        lastNotifiedTransferKey = '';
        try { sessionStorage.removeItem(NOTIFIED_RUN_KEY); } catch (_e) {}
    }

    function hideAppToast() {
        if (appToastTimer) clearTimeout(appToastTimer);
        let el = document.getElementById('appToast');
        if (el) el.style.display = 'none';
    }

    function showAppToast(message, kind) {
        let el = document.getElementById('appToast');
        if (!el) return;
        if (appToastTimer) clearTimeout(appToastTimer);
        let tone = (kind === 'error') ? 'error' : (kind === 'info' ? 'info' : 'success');
        let icon = (tone === 'error') ? 'ico-alert' : (tone === 'info' ? 'ico-clock' : 'ico-check');
        el.className = 'app-toast is-' + tone;
        el.setAttribute('role', tone === 'error' ? 'alert' : 'status');
        el.innerHTML = spriteIcon(icon) + '<span>' + esc(message) + '</span>';
        el.style.display = 'flex';
        appToastTimer = setTimeout(function() { el.style.display = 'none'; }, 6000);
    }

    // WebAudio has to be created/resumed in a real gesture, so the copy button
    // and the sound switch both call this; a later poll may then play the chime.
    function unlockCompletionSound() {
        if (!soundEnabled()) return null;
        let AudioCtor = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtor) return null;
        try {
            if (!notificationSoundContext) notificationSoundContext = new AudioCtor();
            if (notificationSoundContext.state === 'suspended' && notificationSoundContext.resume) {
                let resumed = notificationSoundContext.resume();
                if (resumed && resumed.catch) resumed.catch(function() {});
            }
            return notificationSoundContext;
        } catch (_e) { return null; }
    }

    function playCompletionSound(kind, test) {
        let ctx = unlockCompletionSound();
        if (!ctx || !ctx.createOscillator || !ctx.createGain) return;
        try {
            let notes = (kind === 'error') ? [523.25, 392, 293.66] : [659.25, 880, 1046.5];
            if (test) notes = [880];
            let base = ctx.currentTime + 0.02;
            notes.forEach(function(freq, i) {
                let at = base + i * 0.14;
                let osc = ctx.createOscillator();
                let gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(freq, at);
                gain.gain.setValueAtTime(0.0001, at);
                gain.gain.exponentialRampToValueAtTime(test ? 0.05 : 0.09, at + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.18);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start(at);
                osc.stop(at + 0.2);
            });
        } catch (_e) {}
    }

    function refreshNotificationControls() {
        let cb = document.getElementById('browserNotifyCheck');
        let supported = ('Notification' in window);
        if (!supported) {
            if (cb) cb.checked = false;
            setSettingsState('هذا المتصفح لا يدعم إشعارات النظام — التنبيه داخل الصفحة يعمل.', '', 'notifySaveState');
            return;
        }
        let pref = getNotifyPref();
        let granted = Notification.permission === 'granted';
        if (granted && pref === null) setNotifyPref(true);
        if (Notification.permission === 'denied') setNotifyPref(false);
        let enabled = granted && pref !== '0';
        if (cb) cb.checked = enabled;
        if (Notification.permission === 'denied') {
            setSettingsState('الإشعارات محجوبة في إعدادات المتصفح.', 'err', 'notifySaveState');
        } else if (enabled) {
            setSettingsState('إشعارات المتصفح مفعّلة.', 'ok', 'notifySaveState');
        } else {
            setSettingsState('', '', 'notifySaveState');
        }
    }

    function pickBrowserNotifications(enabled) {
        let cb = document.getElementById('browserNotifyCheck');
        if (!('Notification' in window)) {
            if (cb) cb.checked = false;
            setSettingsState('هذا المتصفح لا يدعم إشعارات النظام.', 'err', 'notifySaveState');
            return;
        }
        if (!enabled) {
            setNotifyPref(false);
            setSettingsState('تم إيقاف إشعارات المتصفح.', '', 'notifySaveState');
            return;
        }
        let finish = function(permission) {
            let granted = permission === 'granted';
            setNotifyPref(granted);
            if (cb) cb.checked = granted;
            setSettingsState(granted ? 'تم تفعيل إشعارات المتصفح.'
                : 'لم يمنح المتصفح الإذن — التنبيه داخل الصفحة يعمل.', granted ? 'ok' : 'err', 'notifySaveState');
        };
        if (Notification.permission === 'granted') { finish('granted'); return; }
        if (Notification.permission === 'denied') { finish('denied'); return; }
        try {
            let request = Notification.requestPermission();
            if (request && typeof request.then === 'function') {
                request.then(finish).catch(function() { finish('denied'); });
            } else {
                finish(Notification.permission);
            }
        } catch (_e) { finish('denied'); }
    }

    // First copy is a user gesture, so Chrome will actually show its permission
    // prompt there. After a decision we never nag again; the settings switch
    // remains the way to opt in later.
    function maybeAskNotificationPermission() {
        if (!('Notification' in window)) return;
        if (Notification.permission === 'granted') {
            if (getNotifyPref() !== '0') setNotifyPref(true);
            refreshNotificationControls();
            return;
        }
        if (Notification.permission === 'denied') { setNotifyPref(false); return; }
        if (getNotifyPref() !== null) return;
        try {
            if (localStorage.getItem(NOTIFY_PROMPT_KEY) === '1') return;
            localStorage.setItem(NOTIFY_PROMPT_KEY, '1');
        } catch (_e) { return; }
        try {
            let request = Notification.requestPermission();
            let finish = function(permission) { setNotifyPref(permission === 'granted'); refreshNotificationControls(); };
            if (request && typeof request.then === 'function') request.then(finish).catch(function() {});
            else finish(Notification.permission);
        } catch (_e) {}
    }

    function pickCompletionSound(enabled) {
        let cb = document.getElementById('completionSoundCheck');
        if (cb) cb.checked = !!enabled;
        try { localStorage.setItem(COMPLETION_SOUND_KEY, enabled ? '1' : '0'); } catch (_e) {}
        if (enabled) {
            unlockCompletionSound();
            playCompletionSound('success', true);
        }
        setSettingsState(enabled ? 'تم تفعيل صوت الانتهاء.' : 'تم إيقاف صوت الانتهاء.', 'ok', 'notifySaveState');
        setTimeout(function() { setSettingsState('', '', 'notifySaveState'); }, 2200);
    }

    function sendTransferNotification(title, body, key) {
        if (getNotifyPref() !== '1') return;
        if (!('Notification' in window) || Notification.permission !== 'granted') return;
        try {
            new Notification(title, { body: body, tag: 'sdmp3-' + key, renotify: false });
        } catch (_e) {}
    }

    function transferRunKey(data) {
        if (!data) return '';
        return String(data.action || '') + ':' + String(data.start_time || data.time_taken || '');
    }

    function notifyTransferFinished(data) {
        let key = transferRunKey(data);
        if (!key || runAlreadyNotified(key)) return;
        markRunNotified(key);
        if (data.action !== 'copy') return;
        let detail = String(data.log || '').slice(0, 180);
        if (data.status === 'done') {
            showAppToast('اكتمل النسخ بنجاح.', 'success');
            playCompletionSound('success');
            sendTransferNotification('SD Card Coffee', 'اكتمل النسخ بنجاح.', key);
        } else if (data.status === 'error') {
            showAppToast('فشل النسخ' + (detail ? ': ' + detail : '.'), 'error');
            playCompletionSound('error');
            sendTransferNotification('SD Card Coffee', 'فشل النسخ' + (detail ? ': ' + detail : '.'), key);
        } else if (data.status === 'cancelled') {
            showAppToast('تم إيقاف النسخ.', 'info');
        }
    }

    function showInlineDone(msg) { showInlineNotify(spriteIcon('ico-check') + ' ' + msg, 'success'); }
    function showInlineError(msg) { showInlineNotify(spriteIcon('ico-alert') + ' ' + esc(msg), 'error'); }

    // ---- the end-of-run report -------------------------------------------
    // A finished copy used to print one long line of concatenated numbers (total
    // time, copy time, average speed, verify time, spoof count) in a single
    // centred string: nothing in it was scannable. The same facts are rows now,
    // each with an icon, and a row with nothing to say is not rendered at all.
    function reportRow(iconId, label, value, tone) {
        return '<div class="report-row' + (tone ? ' is-' + tone : '') + '">'
            + spriteIcon(iconId)
            + '<span class="report-label">' + esc(label) + '</span>'
            + (value ? '<span class="report-value">' + esc(String(value)) + '</span>' : '')
            + '</div>';
    }

    function renderTransferReport(rows) {
        let box = document.getElementById('timeTakenDiv');
        if (!box) return;
        let html = (rows || []).filter(Boolean).join('');
        if (!html) { box.style.display = 'none'; box.innerHTML = ''; return; }
        box.innerHTML = html;
        box.style.display = 'flex';
    }

    function buildTransferReport(data) {
        let action = data.action || '';
        let isCopy = action === 'copy';
        let isDelete = action === 'delete';
        let isTts = action === 'tts';
        let isSpoof = action === 'spoof';
        let baseHeadline = isTts ? 'تم إنشاء الفهرس الصوتي'
            : isDelete ? 'تم الحذف'
            : isCopy ? 'اكتمل النسخ'
            : isSpoof ? 'اكتمل التمويه'
            : 'اكتملت العملية';
        let hasFailures = Number(data.failed || 0) > 0
            || !!(data.errors && data.errors.length)
            || !!(data.verify_failed && data.verify_failed.length)
            || !!data.spoof_error;
        let hasWarnings = !!data.verify_cancelled || !!data.flush_warning
            || !!(data.skipped && data.skipped.length) || !!data.spoof_cancelled;
        let headline = hasFailures ? 'اكتملت العملية مع إخفاقات'
            : hasWarnings ? 'اكتملت العملية مع ملاحظات' : baseHeadline;
        let headlineTone = hasFailures ? 'bad' : hasWarnings ? 'warn' : 'ok';
        let unit = isCopy ? 'ملف' : 'عنصر';
        // `copied` is chunks for TTS, not files: only the file-moving actions
        // have a count worth printing.
        let count = (isCopy || isDelete || isSpoof) ? (data.copied || 0) : 0;
        let headlineIcon = headlineTone === 'ok'
            ? (isTts ? 'ico-music' : 'ico-check') : 'ico-alert';
        let rows = [reportRow(headlineIcon, headline,
                              count ? count + ' ' + unit : '', headlineTone)];

        if (isCopy) {
            // Copy time and average speed share one row: they are the two halves
            // of the same honest statement ("how fast was the card").
            let speed = data.avg_mbps ? data.avg_mbps + ' MB/s' : '';
            let copyLine = [data.copy_seconds ? data.copy_seconds + ' ث' : '', speed]
                .filter(Boolean).join(' · ');
            rows.push(reportRow('ico-bolt', 'النسخ', copyLine));
        } else if (data.time_taken) {
            rows.push(reportRow('ico-clock', 'الزمن', data.time_taken));
        }

        // An archive is unpacked, not copied: say how many, and how many files
        // came out of them. Only present when a `.zip` was part of the transfer.
        if (data.zip_archives > 0) {
            rows.push(reportRow('ico-zip', 'فك الضغط',
                data.zip_archives + ' أرشيف · ' + (data.zip_files || 0) + ' ملف'));
        }

        if (data.verify_total > 0) {
            let tone = 'ok';
            let value = (data.verified || 0) + '/' + data.verify_total + ' سليم';
            if (data.verify_cancelled) {
                tone = 'warn';
                value = 'تم الإيقاف';
            } else if (data.verify_failed && data.verify_failed.length) {
                tone = 'bad';
                value = (data.verified || 0) + '/' + data.verify_total
                    + ' — ' + data.verify_failed.length + ' مخالف';
            }
            if (data.verify_seconds) value += ' · ' + data.verify_seconds + ' ث';
            let verifyLabel = data.verify_mode === 'content'
                ? 'مقارنة المحتوى (SHA-256)' : 'التحقق من الحجم';
            rows.push(reportRow('ico-shield', verifyLabel, value, tone));
        }
        if (data.flush_warning) {
            rows.push(reportRow('ico-alert', 'تثبيت البيانات',
                'تحذير — راجع التفاصيل', 'warn'));
        }

        if (data.spoof_total) {
            rows.push(reportRow('ico-spark', 'تمويه التواريخ',
                (data.spoof_done || 0) + '/' + data.spoof_total, 'ok'));
        } else if (data.spoof_cancelled) {
            rows.push(reportRow('ico-spark', 'تمويه التواريخ', 'تم الإيقاف', 'warn'));
        } else if (data.spoof_error) {
            rows.push(reportRow('ico-spark', 'تمويه التواريخ', 'تعذر', 'bad'));
        }

        if (isTts && data.tts_saved_path) {
            rows.push(reportRow('ico-folder', 'حُفظ في الوجهة',
                data.tts_saved_path.split('/').pop(), 'ok'));
        }
        return rows;
    }

    function cancelTransfer() {
        if(confirm('متأكد من الإيقاف؟')) {
            document.getElementById('cancelProgressBtn').disabled = true;
            fetch('/transfer/cancel').then(()=>{
                document.getElementById('cancelProgressBtn').style.display = 'none';
                document.getElementById('currentFile').innerText = 'جاري الإيقاف بشكل آمن...';
            }).catch(()=>{ document.getElementById('cancelProgressBtn').disabled = false; });
        }
    }

    // The 3px line in the action bar repeats the running copy, so the progress
    // dialog is optional. A delete has no known total, so it sweeps like the
    // modal's bar instead of pretending to know a percentage.
    function mirrorProgressInBar(data) {
        let wrap = document.getElementById('barProgressWrap');
        let fill = document.getElementById('barProgressFill');
        if (!wrap || !fill) return;
        if (data.status !== 'copying') {
            wrap.classList.remove('on');
            fill.classList.remove('indeterminate');
            fill.style.width = '0%';
            return;
        }
        wrap.classList.add('on');
        if (data.action === 'delete') {
            fill.classList.add('indeterminate');
            fill.style.width = '100%';
            return;
        }
        fill.classList.remove('indeterminate');
        let pct = (data.phase === 'verifying') ? (data.verify_progress || 0) : (data.progress || 0);
        fill.style.width = pct + '%';
    }

    function checkTransferStatus() {
        if (statusTimeout) clearTimeout(statusTimeout);

        let pollDelay = 1000;
        fetch('/transfer/status?_t=' + new Date().getTime(), { cache: 'no-store' })
        .then(r=>r.json())
        .then(data => {
            if(data.status !== 'idle' && !progressModalVisible && !progressMinimized) {
                showProgressModal();
            }
            let copyDetailsBtn = document.getElementById('copyTransferDetailsBtn');
            if (data.status === 'copying') {
                terminalTransferData = null;
                let runningKey = transferRunKey(data);
                if (runningKey && runningKey !== activeTransferKey) {
                    activeTransferKey = runningKey;
                    clearRunNotification();
                }
                if (copyDetailsBtn) {
                    copyDetailsBtn.style.display = 'none';
                    copyDetailsBtn.textContent = 'نسخ التفاصيل';
                }
            } else if (data.status === 'done' || data.status === 'cancelled' || data.status === 'error') {
                terminalTransferData = data;
                if (copyDetailsBtn) copyDetailsBtn.style.display = 'inline-flex';
            }
            if(data.status !== 'idle') {
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
                } else if (data.phase === 'verifying') {
                    fillEl.classList.remove('indeterminate');
                    fillEl.style.width = (data.verify_progress || 0) + '%';
                    document.getElementById('progressText').innerText = 'تحقق';
                    document.getElementById('countText').innerText =
                        (data.verify_done || 0) + ' / ' + (data.verify_total || 0);
                } else if (data.phase === 'spoofing') {
                    // Metadata-only pass, still server-side: no fake card speed.
                    fillEl.classList.remove('indeterminate');
                    let spoofPct = data.spoof_total
                        ? Math.min(99, Math.round(((data.spoof_done || 0) / data.spoof_total) * 100))
                        : 99;
                    fillEl.style.width = spoofPct + '%';
                    document.getElementById('progressText').innerText = 'تمويه';
                    document.getElementById('countText').innerText = 'تم تمويه ' + (data.spoof_done || 0);
                } else {
                    fillEl.classList.remove('indeterminate');
                    document.getElementById('progressText').innerText = data.progress + '%';
                    fillEl.style.width = data.progress + '%';
                    if(data.total > 0) document.getElementById('countText').innerText = data.copied + ' / ' + data.total;
                }
                // A stalled card must say so instead of pretending to work.
                // TTS never touches a card: it is network-bound, so it can be
                // slow without any volume being in trouble.
                let currentEl = document.getElementById('currentFile');
                if (data.phase === 'syncing' && data.action !== 'tts') {
                    // A fast flush (the normal case) keeps the current line as it
                    // is. Only a flush that is genuinely slow earns the suffix,
                    // once, so the dialog stops repeating it every poll.
                    let syncSecs = data.sync_seconds || 0;
                    if (syncSecs >= 3) {
                        currentEl.innerText = (data.file ? data.file + ' — ' : '') +
                            'تثبيت البيانات على الكارت (' + syncSecs + ' ث)';
                    } else {
                        currentEl.innerText = data.file || '';
                    }
                    currentEl.style.color = 'var(--text-muted)';
                } else if (data.phase === 'verifying') {
                    let verifyLabel = data.verify_content
                        ? 'جاري مقارنة المحتوى' : 'جاري التحقق من الحجم';
                    let currentName = (data.verify_content && data.file)
                        ? ' — ' + data.file : '';
                    currentEl.innerText = verifyLabel + currentName + ' · '
                        + (data.verify_done || 0) + ' / ' + (data.verify_total || 0);
                    currentEl.style.color = 'var(--accent-text)';
                } else if (data.phase === 'spoofing') {
                    currentEl.innerText = 'جاري تمويه تواريخ الوجهة... '
                        + (data.spoof_done || 0) + ' / ' + (data.spoof_total || 0);
                    currentEl.style.color = 'var(--accent-text)';
                } else if (data.stalled && data.action !== 'tts') {
                    currentEl.innerText = (data.action === 'delete')
                        ? 'توقف الحذف — لم يُحذف أي عنصر منذ ' + data.stall_seconds + ' ثانية. ' +
                          'إن كان الكارت يعمل فانتظر قليلاً. إذا استمر التوقف: أعد تشغيل التطبيق: sdmp3 restart'
                        : 'توقف النقل — لم يتم نقل أي بيانات منذ ' + data.stall_seconds + ' ثانية. ' +
                          'إن كان الكارت يعمل فانتظر قليلاً. إذا استمر التوقف: أخرج الكارت وأدخله ثم أعد المحاولة';
                    currentEl.style.color = 'var(--danger-ink)';
                } else if (data.slow && data.action !== 'tts') {
                    currentEl.innerText = (data.action === 'delete')
                        ? 'الكارت بطيء — لم يُحذف عنصر جديد منذ ' + data.slow_seconds + ' ثانية. العملية مستمرة، لا تُخرج الكارت.'
                        : 'الكارت بطيء — لم تصل بيانات جديدة منذ ' + data.slow_seconds + ' ثانية. العملية مستمرة، لا تُخرج الكارت.';
                    currentEl.style.color = 'var(--warning)';
                } else if (data.status !== 'copying') {
                    // The run is over. Its result belongs to the icon rows below;
                    // echoing the server's one-sentence summary here printed the
                    // same information twice in the same dialog.
                    currentEl.innerText = '';
                    currentEl.style.color = 'var(--text-muted)';
                } else {
                    currentEl.innerText = data.file;
                    currentEl.style.color = 'var(--text-muted)';
                }
                document.getElementById('failedText').innerText = data.failed ? ('فشل: ' + data.failed) : '';
                mirrorProgressInBar(data);
                // The primary button is the way back into a minimized dialog;
                // it stays «بدء النسخ» while idle.
                transferRunning = (data.status === 'copying');
                let barLabel = document.getElementById('barActionLabel');
                if (barLabel) {
                    if (transferRunning) {
                        let word = (data.action === 'delete') ? 'الحذف'
                            : (data.action === 'tts' ? 'الفهرس' : 'النسخ');
                        let pct = (data.phase === 'verifying') ? (data.verify_progress || 0)
                            : (data.action === 'delete' ? null : (data.progress || 0));
                        barLabel.textContent = 'استعادة ' + word + (pct === null ? '' : ' ' + pct + '٪');
                    } else {
                        barLabel.textContent = 'بدء النسخ';
                    }
                }

                // One figure, not two: the instantaneous rate dips with every
                // small file and only made the average look untrustworthy. The
                // average is the number the report will show too.
                let speedEl = document.getElementById('speedText');
                if (speedEl) {
                    if (data.phase === 'syncing' || data.phase === 'verifying'
                        || data.phase === 'spoofing') {
                        speedEl.textContent = '';
                    } else if (data.avg_mbps > 0) {
                        let eta = '';
                        if (data.eta_seconds > 0) {
                            let m = Math.floor(data.eta_seconds / 60);
                            let s = data.eta_seconds % 60;
                            eta = ' • متبقي ~' + (m > 0 ? m + ' د ' : '') + s + ' ث';
                        }
                        speedEl.textContent = 'المتوسط ' + data.avg_mbps + ' MB/s' + eta;
                    } else {
                        speedEl.textContent = '';
                    }
                }

                // A measured slow card is a warning, not a failure: no red, no
                // "pull it out" instruction while files are still being written.
                let warnEl = document.getElementById('cardWarningBox');
                if (warnEl) {
                    if (data.card_warning) {
                        warnEl.innerText = data.card_warning;
                        warnEl.style.display = 'block';
                    } else {
                        warnEl.style.display = 'none';
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
                        downloadTtsFile(data);
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

                    // A finished run is never silent: the result, verification
                    // and error list only exist inside the dialog.
                    transferRunning = false;
                    // The live rate is meaningless once the run is over; the
                    // report rows carry the final average.
                    let liveSpeed = document.getElementById('speedText');
                    if (liveSpeed) liveSpeed.textContent = '';
                    // Minimizing a finished run would be undone by the next
                    // poll, so the control is gone once there is nothing left
                    // to keep running in the background.
                    let minBtn = document.getElementById('minimizeProgressBtn');
                    if (minBtn) minBtn.style.display = 'none';
                    if (progressMinimized) restoreProgressModal();
                    let barDoneLabel = document.getElementById('barActionLabel');
                    if (barDoneLabel) barDoneLabel.textContent = 'بدء النسخ';
                    document.getElementById('cancelProgressBtn').style.display = 'none';
                    document.getElementById('closeProgressBtn').style.display = 'block';

                    // One alert per run, before any per-status report rendering.
                    // Repeated terminal polls must not re-notify.
                    notifyTransferFinished(data);

                    if(data.status === 'error') {
                        document.getElementById('currentFile').innerText = 'خطأ: ' + data.log;
                        document.getElementById('currentFile').style.color = 'var(--danger-ink)';
                        showInlineError('فشلت العملية: ' + data.log);
                    } else if (data.status === 'done' && data.time_taken) {
                        renderTransferReport(buildTransferReport(data));
                        // The parent folders belonged to the copy that just ran.
                        if (data.action === 'copy' && copyGroups.length) {
                            copyGroups = [];
                            renderGroupUI();
                        }
                    } else if (data.status === 'cancelled' && data.time_taken) {
                        renderTransferReport([
                            reportRow('ico-alert', 'تم الإيقاف', data.time_taken, 'warn')
                        ]);
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
                    if (data.verify_failed && data.verify_failed.length > 0) {
                        lines.push(data.verify_mode === 'content'
                            ? '<b>مقارنة محتوى الملفات (SHA-256):</b>'
                            : '<b>التحقق من الحجم:</b>');
                        data.verify_failed.forEach(function(n){ lines.push('- ' + esc(n)); });
                    }
                    if (data.flush_warning) {
                        lines.push('<b>تثبيت البيانات على الكارت:</b> '
                            + esc(data.flush_warning));
                    }
                    if (data.spoof_error) {
                        lines.push('<b>تمويه التواريخ:</b> ' + esc(data.spoof_error));
                    } else if (data.spoof_cancelled) {
                        lines.push('<b>تمويه التواريخ:</b> تم الإيقاف');
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
            // Idle polling does not need a request every second; during a copy
            // it does, and verification wants a slightly smoother bar.
            pollDelay = (data.status === 'idle') ? 2500 : (data.phase === 'verifying' ? 700 : 1000);
        })
        .catch(err => console.log('Network sync delayed...'))
        .finally(() => {
            statusTimeout = setTimeout(checkTransferStatus, pollDelay);
        });
    }
    
    // Esc closes the modal on top, never the progress dialog: that one owns a
    // running copy and must be closed with its own button after the run ends.
    document.addEventListener('keydown', function(e) {
        if (e.key !== 'Escape') return;
        // The copy dialog is filtered out before the choice: it owns a running
        // copy, and taking it out after picking would turn Esc into a no-op
        // whenever a finished run had just restored it over another dialog.
        let open = Array.from(document.querySelectorAll('.modal')).filter(function(m) {
            return m.style.display === 'flex' && m.id !== 'progressModal';
        });
        if (!open.length) return;
        let front = open.filter(function(m) { return m.classList.contains('modal-front'); })[0]
            || open[open.length - 1];
        if (!front) return;
        // These two own transient state: closing them must also drop the state,
        // not just hide the box (a draft must never survive its dialog).
        if (front.id === 'assignGroupModal') { closeAssignGroup(); return; }
        if (front.id === 'groupParentModal') { closeGroupParentPicker(); return; }
        if (front.id === 'settingsModal') { closeSettingsModal(); return; }
        front.style.display = 'none';
    });

    function resetTransfer() {
        fetch('/transfer/reset').then(() => {
            checkDiskSpace(document.getElementById('custom_dest_path').value);
            document.getElementById('progressModal').style.display = 'none';
            progressModalVisible = false;
            progressMinimized = false;
            transferRunning = false;
            let barLabel = document.getElementById('barActionLabel');
            if (barLabel) barLabel.textContent = 'بدء النسخ';
            document.getElementById('errorsBox').style.display = 'none';
            terminalTransferData = null;
            let copyDetailsBtn = document.getElementById('copyTransferDetailsBtn');
            if (copyDetailsBtn) {
                copyDetailsBtn.style.display = 'none';
                copyDetailsBtn.textContent = 'نسخ التفاصيل';
            }
            document.getElementById('cardWarningBox').style.display = 'none';
            document.getElementById('inlineNotify').style.display = 'none';
            hideAppToast();
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
    """The whole page, painted with the saved theme and font on the first byte.

    The token block, the font stacks and `theme-color` are rendered server-side
    from settings.json: a client-side apply would flash the default theme or
    the wrong typeface on every load, which is exactly what a phone user
    notices.
    """
    theme_id = APP_SETTINGS.get('theme') or DEFAULT_THEME
    font_id = APP_SETTINGS.get('font') or DEFAULT_FONT
    size_id = APP_SETTINGS.get('font_size') or DEFAULT_FONT_SIZE
    active = get_theme(theme_id)
    base_theme_css = theme_css_pairs(theme_id)
    return render_template_string(
        HTML_TEMPLATE,
        storages=get_available_storages(),
        app_version=get_app_version(),
        active_theme=active,
        theme_css=base_theme_css,
        theme_bg=theme_color(theme_id),
        themes=catalog(),
        # Typography: the @font-face rules are inert until a family is used, so
        # the default `system` choice still paints with zero font requests.
        font_css=font_face_css(get_app_version()),
        app_font=font_stack(font_id),
        font_scale=font_scale(size_id),
        fonts=[dict(f, css_stack=font_stack(f['id'])) for f in font_catalog()],
        font_sizes=FONT_SIZES,
        active_font=font_id,
        active_font_size=size_id,
        auto_spoof=bool(APP_SETTINGS.get('auto_spoof')),
        smart_depth=int(APP_SETTINGS.get('smart_depth') or SMART_DEPTH_DEFAULT),
    )

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
    sort_mode = request.args.get('sort', 'name')
    if sort_mode not in ('name', 'date_desc', 'date_asc'):
        sort_mode = 'name'
    items = []
    if os.path.exists(req_path) and os.path.isdir(req_path):
        try:
            # One scandir pass gives name + type + size + mtime without extra stats.
            with os.scandir(req_path) as it:
                entries = list(it)
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                    st = entry.stat()
                    size = 0 if is_dir else st.st_size
                    mtime = st.st_mtime
                except OSError:
                    continue
                items.append({'name': entry.name, 'path': entry.path,
                              'is_dir': is_dir, 'size': size, 'mtime': mtime})
            if sort_mode == 'date_desc':
                items.sort(key=lambda i: (not i['is_dir'], -i.get('mtime', 0)))
            elif sort_mode == 'date_asc':
                items.sort(key=lambda i: (not i['is_dir'], i.get('mtime', 0)))
            else:
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
    """Return the best search hits without sorting every match.

    The UI only displays SEARCH_MAX_RESULTS rows. A cached index can contain
    hundreds of thousands of matches, so selecting the best 40 with a bounded
    heap avoids allocating and sorting the entire result set on every query.
    """
    q_lower = q.lower()
    def rank_key(r):
        name_lower = r['name'].lower()
        if name_lower == q_lower: tier = 0
        elif name_lower.startswith(q_lower): tier = 1
        else: tier = 2
        return (tier, natural_sort_key(r['name']))
    return heapq.nsmallest(SEARCH_MAX_RESULTS, results, key=rank_key)

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
SEARCH_CACHE_FILE = state_file('search_cache.json')
SEARCH_CACHE_MAX_ROOTS = 32
_cache_save_timer = {'handle': None}

# User settings (currently: the theme) and the last searches. Both files live in
# ~/.local/state/sdmp3/ through sdmp3_state.py, and both are loaded in main() so
# the page can be painted with them on the very first request.
# «تنظيم ذكي» keeps this many trailing path segments as the group name; the
# server refuses groups deeper than 3, so the setting shares that ceiling.
SMART_DEPTH_MAX = 3
SMART_DEPTH_DEFAULT = 1

APP_SETTINGS = {'version': SETTINGS_VERSION, 'theme': DEFAULT_THEME,
                'font': DEFAULT_FONT, 'font_size': DEFAULT_FONT_SIZE,
                'auto_spoof': False,
                'smart_depth': SMART_DEPTH_DEFAULT}
APP_INDEX_SETTINGS = dict(DEFAULT_INDEX_SETTINGS)
SEARCH_HISTORY = {'version': 1, 'items': [], 'queries': []}
_settings_lock = threading.RLock()
_history_lock = threading.RLock()
_saved_operations_lock = threading.RLock()


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
                    # A selected archive is unpacked by the copy worker, so in
                    # search it is presented with folders but selected as a file.
                    is_zip = (not is_dir) and looks_like_zip(entry.path)
                    items.append({'name': entry.name, 'name_lower': entry.name.lower(),
                                  'path': entry.path, 'is_dir': is_dir, 'is_zip': is_zip})
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

def _fill_search_result_sizes(results):
    """Add the size a shared tree row needs, for at most 40 file hits."""
    for item in results:
        if item.get('is_dir') or item.get('is_zip'):
            # An archive is shown folder-like; no need to stat a possibly huge
            # zip just for a size the row will not display.
            continue
        try:
            item['size'] = os.path.getsize(item['path'])
        except OSError:
            item['size'] = 0
    return results


@app.route('/api/search')
def deep_search():
    q = request.args.get('q', '').lower().strip()
    folders_only = request.args.get('folders_only', 'true') == 'true'
    root = request.args.get('root', '/sdcard') or '/sdcard'
    if not q: return jsonify({'results': []})
    if not os.path.isdir(root): root = '/sdcard'
    # Only a *committed* search (Enter / the button) is remembered; the 400ms
    # typing debounce would otherwise fill the history with `م`, `مج`, `مجل`...
    remember = request.args.get('remember') == '1'

    with folder_cache_lock:
        cached = FOLDER_CACHE.get(root)

    if cached:
        # Instant path: filter the pre-built in-memory index, no disk access at all.
        matches = (
            {'name': it['name'], 'path': it['path'], 'is_dir': it['is_dir'],
             'is_zip': it.get('is_zip', False)}
            for it in cached['items']
            if (not folders_only or it['is_dir'] or it.get('is_zip'))
            and q in it['name_lower']
        )
        results = _rank_search_results(matches, q)
        if remember:
            remember_search(q, root, results)
        return jsonify({'results': _fill_search_result_sizes(results), 'from_cache': True, 'cache_count': cached['count']})

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
                        is_zip = (not is_dir) and looks_like_zip(entry.path)
                        if q in entry.name.lower():
                            if not folders_only or is_dir or is_zip:
                                results.append({'name': entry.name, 'path': entry.path,
                                                'is_dir': is_dir, 'is_zip': is_zip})
                                if len(results) >= SEARCH_MAX_RESULTS:
                                    break
            except (PermissionError, OSError):
                continue
    except Exception:
        pass

    _rank_search_results(results, q)
    if remember:
        remember_search(q, root, results)
    return jsonify({'results': _fill_search_result_sizes(results), 'from_cache': False})


# ---------------------------------------------------------------------------
# settings (theme) and search history
# ---------------------------------------------------------------------------

# How many results one search may contribute to the history. A single 500-hit
# search would otherwise wipe the 25 slots and the section would stop being a
# history of *searches*.
HISTORY_RESULTS_PER_SEARCH = 10


def remember_search(query, root, results):
    """Save a committed search: the query chip and its top results."""
    global SEARCH_HISTORY
    now = int(time.time())
    top = []
    for item in (results or [])[:HISTORY_RESULTS_PER_SEARCH]:
        path = item.get('path')
        if not path:
            continue
        top.append({
            'path': path,
            'name': item.get('name') or os.path.basename(path),
            'is_dir': bool(item.get('is_dir')),
            'is_zip': bool(item.get('is_zip')),
            'root': root,
            'q': query,
            'at': now,
        })
    with _history_lock:
        items = [i for i in (SEARCH_HISTORY.get('items') or []) if i.get('path')]
        # One row per path: a folder found twice is still one folder.
        new_paths = {i['path'] for i in top}
        items = [i for i in items if i['path'] not in new_paths]
        items = (top + items)[:HISTORY_ITEM_LIMIT]
        queries = upsert_recent(SEARCH_HISTORY.get('queries') or [],
                                {'q': query, 'root': root, 'at': now},
                                'q', HISTORY_QUERY_LIMIT)
        SEARCH_HISTORY = {'version': 1, 'items': items, 'queries': queries}
        snapshot = dict(SEARCH_HISTORY)
    save_search_history(snapshot)


def _coerce_smart_depth(value, default):
    """Depth for «تنظيم ذكي»: an integer in 1..SMART_DEPTH_MAX, else `default`.

    A hand-edited settings.json or an old client must never be able to ask for a
    group the server would refuse, so the value is clamped, not rejected.
    """
    if isinstance(value, bool):
        return default
    try:
        depth = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(SMART_DEPTH_MAX, depth))


def _coerce_bool(value, default=False):
    """Bool/0|1/'true'|'on'|'yes' -> bool; anything else keeps `default`."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ('1', 'true', 'on', 'yes'):
            return True
        if low in ('0', 'false', 'off', 'no'):
            return False
    return default


@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    """Read or update the appearance + typography settings.

    POST accepts any subset of appearance and `auto_spoof` fields: each id is
    checked against its registry (sdmp3_themes.py / sdmp3_fonts.py)
    and an unknown id is refused with a 400, so settings.json can never hold a
    value the page cannot render. A bad boolean is dropped, not written. Only
    the keys present in the body change - the General pane saves a font without
    touching the theme, and vice versa.
    """
    global APP_SETTINGS
    if request.method == 'GET':
        return jsonify({
            'settings': APP_SETTINGS,
            'themes': [{'id': t['id'], 'name': t['name'], 'desc': t['desc'],
                        'dark': t['dark'], 'swatches': t['swatches'],
                        'tokens': t['tokens'] if request.args.get('tokens') == '1' else None}
                       for t in catalog()],
            'fonts': [dict(f, css_stack=font_stack(f['id'])) for f in font_catalog()],
            'font_sizes': FONT_SIZES,
            'app_font': font_stack(APP_SETTINGS.get('font') or DEFAULT_FONT),
            'font_scale': font_scale(APP_SETTINGS.get('font_size') or DEFAULT_FONT_SIZE),
            'default': DEFAULT_THEME,
        })

    body = request.json or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'بيانات غير صالحة'}), 400
    theme_id = body.get('theme', APP_SETTINGS.get('theme') or DEFAULT_THEME)
    font_id = body.get('font', APP_SETTINGS.get('font') or DEFAULT_FONT)
    size_id = body.get('font_size', APP_SETTINGS.get('font_size') or DEFAULT_FONT_SIZE)
    auto_spoof = _coerce_bool(body.get('auto_spoof'),
                              bool(APP_SETTINGS.get('auto_spoof')))
    smart_depth = _coerce_smart_depth(body.get('smart_depth'),
                                      int(APP_SETTINGS.get('smart_depth') or SMART_DEPTH_DEFAULT))
    if theme_id not in THEMES:
        return jsonify({'error': 'سمة غير معروفة'}), 400
    if font_id not in font_ids():
        return jsonify({'error': 'خط غير معروف'}), 400
    if size_id not in font_size_ids():
        return jsonify({'error': 'حجم خط غير معروف'}), 400
    selected_theme = get_theme(theme_id)
    settings = {'version': SETTINGS_VERSION, 'theme': theme_id,
                'font': font_id, 'font_size': size_id,
                'auto_spoof': auto_spoof, 'smart_depth': smart_depth}
    with _settings_lock:
        if not save_settings(settings):
            return jsonify({'error': 'تعذر حفظ الإعدادات على الجهاز'}), 500
        APP_SETTINGS = settings
    return jsonify({
        'ok': True,
        'settings': settings,
        'theme_bg': selected_theme['tokens']['--bg'],
        'tokens': selected_theme['tokens'],
        'swatches': selected_theme['swatches'],
        'theme_name': selected_theme['name'],
        'app_font': font_stack(font_id),
        'font_scale': font_scale(size_id),
    })


def _saved_operation_payload(body):
    """Validate the saved source selection and its copy-group assignment."""
    if not isinstance(body, dict):
        return None, 'بيانات العملية غير صالحة'
    name = body.get('name')
    if not isinstance(name, str) or not name.strip():
        return None, 'اكتب اسماً للعملية'
    name = name.strip()
    if len(name) > 80:
        return None, 'اسم العملية طويل جداً (الحد 80 حرفاً)'

    raw_paths = body.get('paths')
    if not isinstance(raw_paths, list) or not raw_paths:
        return None, 'لا توجد ملفات أو مجلدات لحفظها'
    if len(raw_paths) > SAVED_OPERATION_PATH_LIMIT:
        return None, 'عدد العناصر يتجاوز الحد المسموح لحفظ عملية واحدة'
    paths = []
    seen_paths = set()
    for item in raw_paths:
        if not isinstance(item, dict):
            return None, 'صيغة العناصر المحددة غير صالحة'
        path = item.get('path')
        is_dir = item.get('is_dir')
        if (not isinstance(path, str) or not path.startswith('/')
                or '\x00' in path or len(path) > 4096
                or not isinstance(is_dir, bool)):
            return None, 'مسار عنصر محدد غير صالح'
        path = os.path.normpath(path)
        if path in seen_paths:
            return None, 'العنصر محدد أكثر من مرة'
        for existing in seen_paths:
            if (path.startswith(existing.rstrip('/') + '/')
                    or existing.startswith(path.rstrip('/') + '/')):
                return None, 'لا يمكن حفظ مجلد وعنصر بداخله كتحديدين منفصلين'
        seen_paths.add(path)
        paths.append({'path': path, 'is_dir': is_dir})

    raw_excludes = body.get('excludes', [])
    if not isinstance(raw_excludes, list) or len(raw_excludes) > SAVED_OPERATION_PATH_LIMIT:
        return None, 'صيغة العناصر المستثناة غير صالحة'
    excludes = []
    seen_excludes = set()
    for excluded in raw_excludes:
        if (not isinstance(excluded, str) or not excluded.startswith('/')
                or '\x00' in excluded or len(excluded) > 4096):
            return None, 'مسار عنصر مستثنى غير صالح'
        excluded = os.path.normpath(excluded)
        if excluded in seen_excludes:
            continue
        if not any(excluded.startswith(path['path'].rstrip('/') + '/')
                   for path in paths):
            return None, 'العنصر المستثنى ليس داخل التحديد'
        seen_excludes.add(excluded)
        excludes.append(excluded)

    raw_groups = body.get('groups', [])
    if not isinstance(raw_groups, list) or len(raw_groups) > SAVED_OPERATION_GROUP_LIMIT:
        return None, 'عدد مجلدات النسخ غير صالح'
    selected = {item['path'] for item in paths}
    groups = []
    group_names = set()
    assigned = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            return None, 'صيغة مجلد النسخ غير صالحة'
        name_value, error = validate_group_path(raw_group.get('name') or '')
        if error or not name_value:
            return None, error or 'اسم مجلد النسخ مطلوب'
        group_key = name_value.casefold()
        if group_key in group_names:
            return None, 'اسم مجلد النسخ مستخدم أكثر من مرة'
        group_names.add(group_key)
        group_paths = raw_group.get('paths', [])
        if not isinstance(group_paths, list):
            return None, 'قائمة العناصر داخل المجلد غير صالحة'
        clean_group_paths = []
        for path in group_paths:
            if not isinstance(path, str) or not path.startswith('/') or '\x00' in path:
                return None, 'مسار داخل مجلد النسخ غير صالح'
            path = os.path.normpath(path)
            if path not in selected:
                return None, 'العنصر داخل مجلد النسخ غير موجود في التحديد'
            if path in assigned:
                return None, 'لا يمكن وضع العنصر في أكثر من مجلد نسخ'
            assigned.add(path)
            clean_group_paths.append(path)
        groups.append({'name': name_value, 'paths': clean_group_paths})

    return {
        'name': name,
        'paths': paths,
        'excludes': excludes,
        'groups': groups,
    }, None


def _saved_operation_summary(operation):
    return {
        'id': operation['id'],
        'name': operation['name'],
        'item_count': len(operation['paths']),
        'excluded_count': len(operation['excludes']),
        'group_count': len(operation['groups']),
        'updated': operation['updated'],
    }


@app.route('/api/saved_operations', methods=['GET', 'POST'])
def api_saved_operations():
    """List or save reusable source selections and their copy organization."""
    if request.method == 'GET':
        with _saved_operations_lock:
            operations = load_saved_operations()
        return jsonify({'operations': [_saved_operation_summary(op) for op in operations]})

    payload, error = _saved_operation_payload(request.get_json(silent=True))
    if error:
        return jsonify({'error': error}), 400
    now = int(time.time())
    with _saved_operations_lock:
        operations = load_saved_operations()
        if any(op['name'].casefold() == payload['name'].casefold() for op in operations):
            return jsonify({'error': 'يوجد اسم محفوظ بهذا الوصف بالفعل'}), 409
        if len(operations) >= SAVED_OPERATIONS_LIMIT:
            return jsonify({'error': 'وصلت إلى الحد الأقصى للعمليات المحفوظة'}), 409
        operation = dict(payload, id=uuid.uuid4().hex, created=now, updated=now)
        operations.insert(0, operation)
        if not save_saved_operations(operations):
            return jsonify({'error': 'تعذر حفظ العملية على الجهاز'}), 500
    return jsonify({'ok': True, 'operation': operation}), 201


@app.route('/api/saved_operations/<operation_id>', methods=['GET', 'PUT', 'DELETE'])
def api_saved_operation(operation_id):
    """Read, rename/update, or delete one saved copy operation."""
    with _saved_operations_lock:
        operations = load_saved_operations()
        index = next((i for i, op in enumerate(operations)
                      if op['id'] == operation_id), None)
        if index is None:
            return jsonify({'error': 'العملية المحفوظة غير موجودة'}), 404
        operation = operations[index]

        if request.method == 'GET':
            return jsonify({'operation': operation})

        if request.method == 'DELETE':
            operations.pop(index)
            if not save_saved_operations(operations):
                return jsonify({'error': 'تعذر حذف العملية من الجهاز'}), 500
            return jsonify({'ok': True})

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({'error': 'بيانات التعديل غير صالحة'}), 400
        name = body.get('name', operation['name'])
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            return jsonify({'error': 'اكتب اسماً صالحاً للعملية (80 حرفاً كحد أقصى)'}), 400
        name = name.strip()
        if any(i != index and op['name'].casefold() == name.casefold()
               for i, op in enumerate(operations)):
            return jsonify({'error': 'يوجد اسم محفوظ بهذا الوصف بالفعل'}), 409

        configuration_keys = ('paths', 'excludes', 'groups')
        if any(key in body for key in configuration_keys):
            if not all(key in body for key in configuration_keys):
                return jsonify({'error': 'بيانات التحديد والتنظيم غير مكتملة'}), 400
            payload, error = _saved_operation_payload(dict(body, name=name))
            if error:
                return jsonify({'error': error}), 400
            operation.update(payload)
        operation['name'] = name
        operation['updated'] = int(time.time())
        operations.pop(index)
        operations.insert(0, operation)
        if not save_saved_operations(operations):
            return jsonify({'error': 'تعذر تحديث العملية على الجهاز'}), 500
        return jsonify({'ok': True, 'operation': operation})


def _coerce_index_settings(body):
    """Validate an index-settings payload into the canonical shape.

    Values that do not fit are dropped (the previous value is kept): the file is
    user-editable, and one bad number must not make the whole form unreadable.
    """
    out = {}

    def as_int(key, lo, hi):
        try:
            out[key] = max(lo, min(hi, int(body.get(key))))
        except (TypeError, ValueError):
            pass

    def as_choice(key, allowed):
        val = str(body.get(key))
        if val in allowed:
            out[key] = val

    def as_bool(key):
        val = body.get(key)
        if isinstance(val, bool):
            out[key] = val
        elif isinstance(val, (int, float)):
            out[key] = bool(val)
        elif isinstance(val, str) and val.lower() in ('1', 'true', 'on', 'yes'):
            out[key] = True
        elif isinstance(val, str) and val.lower() in ('0', 'false', 'off', 'no'):
            out[key] = False

    as_int('joc_start', 1, 999999)
    as_int('joc_limit', 10, 5000)
    as_choice('joc_mode', ('1', '2', '3'))
    as_choice('joc_template', ('1', '2', '3', '4'))
    as_choice('tts_rate', TTS_RATES)
    as_choice('mask_mode', ('none', 'noise', 'file'))
    voice = body.get('tts_voice')
    if isinstance(voice, str) and 0 < len(voice) <= 64:
        out['tts_voice'] = voice
    for key in ('joc_strip', 'joc_download_txt', 'joc_review_txt',
                'joc_mp3_index', 'mp3_to_dest'):
        as_bool(key)
    return out


@app.route('/api/index_settings', methods=['GET', 'POST'])
def api_index_settings():
    """Read or persist the JOC index settings.

    Stored in ~/.local/state/sdmp3/index_settings.json so the user does not
    have to reconfigure the index template on every run. The *output name* is
    deliberately not part of this shape: it is generated in the create dialog
    for each run and never remembered.
    """
    global APP_INDEX_SETTINGS
    if request.method == 'GET':
        return jsonify({'ok': True, 'settings': dict(APP_INDEX_SETTINGS)})

    body = request.json or {}
    if not isinstance(body, dict):
        return jsonify({'error': 'بيانات غير صالحة'}), 400
    updates = _coerce_index_settings(body)
    with _settings_lock:
        APP_INDEX_SETTINGS = dict(APP_INDEX_SETTINGS)
        APP_INDEX_SETTINGS.update(updates)
        snapshot = dict(APP_INDEX_SETTINGS)
    if not save_index_settings(snapshot):
        return jsonify({'error': 'تعذر حفظ إعدادات الفهرس على الجهاز'}), 500
    return jsonify({'ok': True, 'settings': snapshot})


@app.route('/api/history')
def api_history():
    with _history_lock:
        return jsonify({
            'items': list(SEARCH_HISTORY.get('items') or []),
            'queries': list(SEARCH_HISTORY.get('queries') or []),
        })


@app.route('/api/history/clear', methods=['POST'])
def api_history_clear():
    global SEARCH_HISTORY
    with _history_lock:
        SEARCH_HISTORY = {'version': 1, 'items': [], 'queries': []}
        snapshot = dict(SEARCH_HISTORY)
    save_search_history(snapshot)
    return jsonify({'ok': True})


@app.route('/api/history/remove', methods=['POST'])
def api_history_remove():
    """Forget one result row (`path`) or one query chip (`q`)."""
    global SEARCH_HISTORY
    body = request.json or {}
    path = body.get('path')
    query = body.get('q')
    if not path and not query:
        return jsonify({'error': 'لا يوجد عنصر للحذف'}), 400
    with _history_lock:
        items = SEARCH_HISTORY.get('items') or []
        queries = SEARCH_HISTORY.get('queries') or []
        if path:
            items = [i for i in items if i.get('path') != path]
        if query:
            queries = [i for i in queries if i.get('q') != query]
        SEARCH_HISTORY = {'version': 1, 'items': items, 'queries': queries}
        snapshot = dict(SEARCH_HISTORY)
    save_search_history(snapshot)
    return jsonify({'ok': True, 'items': list(snapshot['items']),
                    'queries': list(snapshot['queries'])})

@app.route('/api/history/update', methods=['POST'])
def api_history_update():
    """Record explicitly selected items (e.g. search results) in the history.

    Unlike a committed search this touches ONLY the items list: the user did
    not type a query, so pushing a fake empty chip into the queries list would
    corrupt it. Every path appears once (newest first), capped as everywhere.
    """
    global SEARCH_HISTORY
    body = request.json or {}
    selected_folders = body.get('selected_folders') or []
    now = int(time.time())
    top = []
    for item in selected_folders:
        if not isinstance(item, dict):
            continue
        path = item.get('path')
        if not path:
            continue
        top.append({
            'path': path,
            'name': item.get('name') or os.path.basename(path),
            'is_dir': bool(item.get('is_dir')),
            'is_zip': bool(item.get('is_zip')),
            'root': item.get('root') or '/sdcard',
            'q': item.get('q') or '',
            'at': now,
        })
    with _history_lock:
        items = [i for i in (SEARCH_HISTORY.get('items') or []) if i.get('path')]
        if top:
            # One row per path: a folder selected twice is still one folder.
            new_paths = {i['path'] for i in top}
            items = [i for i in items if i['path'] not in new_paths]
            items = (top + items)[:HISTORY_ITEM_LIMIT]
        SEARCH_HISTORY = {'version': 1,
                          'items': items,
                          'queries': SEARCH_HISTORY.get('queries') or []}
        snapshot = dict(SEARCH_HISTORY)
    save_search_history(snapshot)
    return jsonify({'ok': True,
                    'items': list(snapshot['items']),
                    'queries': list(snapshot['queries'])})

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

@app.route('/api/path_status', methods=['POST'])
def get_paths_status():
    """Check saved source roots without walking folders or calculating sizes."""
    body = request.get_json(silent=True) or {}
    raw_paths = body.get('paths', [])
    if not isinstance(raw_paths, list):
        return jsonify({'error': 'قائمة المسارات غير صالحة'}), 400

    types = {}
    missing = []
    for entry in raw_paths:
        path = entry.get('path') if isinstance(entry, dict) else entry
        if (not isinstance(path, str) or not path.startswith('/')
                or len(path) > 4096 or '\x00' in path):
            return jsonify({'error': 'يوجد مسار محفوظ غير صالح'}), 400
        try:
            path_stat = os.stat(path)
        except (OSError, ValueError):
            # Access restrictions and removed/moved paths are both unavailable
            # to this copy. Report them as data instead of failing the request.
            missing.append(path)
            types[path] = 'missing'
            continue
        except Exception:
            app.logger.exception('Source inspection failed for %r (v%s)', path, APP_VERSION)
            missing.append(path)
            types[path] = 'missing'
            continue

        if stat.S_ISDIR(path_stat.st_mode):
            types[path] = 'directory'
        elif stat.S_ISREG(path_stat.st_mode):
            types[path] = 'file'
        else:
            types[path] = 'other'

    return jsonify({'missing': missing, 'types': types})


@app.route('/api/saved_operations/<operation_id>/restore', methods=['GET'])
def api_restore_saved_operation(operation_id):
    """Load a saved operation and isolate filesystem failures per source path."""
    with _saved_operations_lock:
        operation = next(
            (item for item in load_saved_operations() if item.get('id') == operation_id),
            None,
        )
    if operation is None:
        return jsonify({'error': 'العملية المحفوظة غير موجودة'}), 404

    unavailable = []
    for item in operation.get('paths') or []:
        path = item.get('path')
        try:
            path_stat = os.stat(path)
            if item.get('is_dir') and not stat.S_ISDIR(path_stat.st_mode):
                unavailable.append(path)
            elif not item.get('is_dir') and not stat.S_ISREG(path_stat.st_mode):
                unavailable.append(path)
        except Exception:
            # A bad, removed, inaccessible, or otherwise failing path is skipped
            # independently; other saved items remain available for restoration.
            unavailable.append(path)

    return jsonify({'operation': operation, 'unavailable_paths': unavailable})


@app.route('/api/sizes', methods=['POST'])
def get_items_sizes():
    """Sizes for a whole selection in one request.

    The modal used to fire one /api/size per selected item, sequentially, so a
    dozen folders meant a dozen full walks and a long row of "يحسب..".
    """
    body = request.get_json(silent=True) or {}
    raw_paths = body.get('paths', [])
    if not isinstance(raw_paths, list):
        return jsonify({'error': 'قائمة المسارات غير صالحة'}), 400
    paths = []
    for path in raw_paths:
        if not isinstance(path, str) or not path.startswith('/') or '\x00' in path:
            return jsonify({'error': 'يوجد مسار غير صالح في طلب حساب الأحجام'}), 400
        paths.append(path)
    raw_excludes = body.get('excludes', [])
    if not isinstance(raw_excludes, list):
        return jsonify({'error': 'قائمة الاستثناءات غير صالحة'}), 400
    excludes = set(p for p in raw_excludes if isinstance(p, str) and p)
    include_types = body.get('include_types') is True
    # A child's bytes are already inside its parent: walk each tree once.
    roots = []
    for path in sorted(paths, key=len):
        if not any(path == r or path.startswith(r.rstrip('/') + '/') for r in roots):
            roots.append(path)
    root_exists = {}
    root_bytes = {}
    root_types = {}
    for root in roots:
        try:
            # Inspect each root independently. A single exceptional path must
            # not turn a multi-item size request into HTTP 500.
            root_stat = os.stat(root)
            root_exists[root] = True
            if stat.S_ISDIR(root_stat.st_mode):
                root_types[root] = 'directory'
            elif stat.S_ISREG(root_stat.st_mode):
                root_types[root] = 'file'
            else:
                root_types[root] = 'other'
            try:
                root_bytes[root] = get_dir_size(root, excludes)
            except Exception as size_error:
                log_event('size scan skipped {}: {}: {}'.format(
                    root, type(size_error).__name__, size_error
                ))
                root_bytes[root] = 0
        except Exception as path_error:
            root_exists[root] = False
            root_types[root] = 'missing'
            root_bytes[root] = 0
            log_event('size path skipped {}: {}: {}'.format(
                root, type(path_error).__name__, path_error
            ))
    sizes = {}
    missing = []
    types = {}
    for path in paths:
        owner = next(
            (r for r in roots if path == r or path.startswith(r.rstrip('/') + '/')),
            None,
        )
        if owner is None:
            sizes[path] = {'bytes': 0, 'formatted': '0 B', 'owner': path}
            missing.append(path)
            if include_types:
                types[path] = 'missing'
            continue
        total = root_bytes.get(owner, 0)
        sizes[path] = {'bytes': total, 'formatted': format_size(total), 'owner': owner}
        if not root_exists.get(owner, False):
            missing.append(path)
        if include_types:
            # A missing or inaccessible saved path is still a valid response.
            # Never let an absent type entry turn a partial restore into HTTP 500.
            types[path] = root_types.get(owner, 'missing')
    result = {'sizes': sizes, 'missing': missing}
    if include_types:
        result['types'] = types
    return jsonify(result)


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

    # The check writes 32MB. Never do that while the copy worker owns the card:
    # two writers interleave FAT directory entries and clusters, and the
    # benchmark would measure the worker's queue instead of the card.
    with transfer_lock:
        busy = busy_worker_reason('هناك عملية قيد التشغيل بالفعل، انتظر انتهاءها',
                                  'benchmark')
    if busy: return jsonify({'error': busy})

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

def tts_thread_task(index_text, voice, rate, out_name, chunk_size, obfuscate=False,
                    dest_dir=None):
    """Synthesise the index MP3 and merge it.

    `dest_dir` writes the final MP3 straight into the destination folder (no
    copy, no download); ffmpeg still builds it sequentially, so the playback
    order is untouched. Chunks always live in the TTS temp cache.
    """
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

        # The final file goes straight to its home: the destination when the
        # user asked for that, the temp cache otherwise.
        final_path = os.path.join(dest_dir, out_name) if dest_dir else os.path.join(TTS_CACHE_DIR, out_name)
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
        finish_transfer('done', 'تم إنشاء MP3 الصوتي بنجاح.',
                        extra={'progress': 100, 'tts_name': out_name,
                               'tts_saved_path': final_path if dest_dir else ''})
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
    out_name = os.path.basename(out_name) or 'JOC_Index.mp3'
    chunk_size = int(data.get('chunk_size', 3000))
    obf = data.get('obfuscate', False)
    if obf is True or obf is False:
        obfuscate = bool(obf)
    elif isinstance(obf, str) and obf.lower() in ('file', 'custom'):
        obfuscate = 'file'
    else:
        obfuscate = bool(obf)

    # Busy before probing: the destination probe writes a file, and the single
    # worker must not be joined by another writer.
    with transfer_lock:
        busy = busy_worker_reason('هناك عملية قيد التشغيل بالفعل', 'tts')
    if busy: return jsonify({'error': busy})

    # Optional: write the finished MP3 straight into the destination instead of
    # the temp cache (the UI then offers no download). Probe the folder first -
    # the same 5-byte check a copy does - so a dead card fails before ffmpeg
    # starts, and never overwrite an existing file silently.
    dest_dir = ''
    if data.get('save_to_dest'):
        dest_dir = (data.get('save_dir') or '').strip()
        if not dest_dir or not os.path.isdir(dest_dir):
            return jsonify({'error': 'مسار الوجهة غير صالح لحفظ MP3'})
        if os.path.exists(os.path.join(dest_dir, out_name)):
            return jsonify({'error': 'يوجد ملف بالاسم نفسه في الوجهة — اختر اسماً آخر.'})
        problem = check_destination(dest_dir)
        if problem:
            return jsonify({'error': problem})

    global ACTIVE_THREAD
    with transfer_lock:
        busy = busy_worker_reason('هناك عملية قيد التشغيل بالفعل', 'tts')
        if busy: return jsonify({'error': busy})
        reset_state(status='copying', action='tts', start_time=time.time(),
                    file='جاري تحضير الصوت…')
        ACTIVE_THREAD = threading.Thread(
            target=tts_thread_task,
            args=(index_text, voice, rate, out_name, chunk_size, obfuscate,
                  dest_dir or None),
            daemon=True)
        ACTIVE_THREAD.start()
    return jsonify({'success': True, 'saved_to_dest': bool(dest_dir)})

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

    # Busy first, probe second. The destination check writes a tiny file, and a
    # worker already writing this card must not be joined by another writer.
    with transfer_lock:
        busy = busy_worker_reason('هناك عملية قيد التشغيل بالفعل', 'copy')
    if busy: return jsonify({'error': busy})

    # One request may carry several named parent folders ("groups"). The old
    # single `group` field is still accepted so an old page cannot break.
    raw_groups = data.get('groups')
    if raw_groups is None and data.get('group'):
        raw_groups = [{'name': data.get('group'), 'paths': paths}]
    groups, groups_error = prepare_copy_groups(raw_groups or [], paths)
    if groups_error: return jsonify({'error': groups_error})

    dest = os.path.normpath(dest)
    group_names = [name for name, _ in groups]
    assigned_norm = {os.path.normpath(p) for _name, gpaths in groups for p in gpaths}
    ungrouped = [p for p in paths if os.path.normpath(p) not in assigned_norm]

    # Selected archives are unpacked, never copied. Decided once here (suffix +
    # is-file, so a folder named `x.zip` is still treated as a folder) and used
    # by every guard below.
    archives = selected_archives(paths)
    archive_set = set(archives)

    # A card can be mounted but dead. Check it now (with a timeout) so the user
    # gets "الكارت لا يستجيب" instead of a transfer that freezes at 0%.
    problem = check_destination(dest)
    if problem: return jsonify({'error': problem})

    # A group's TOP-LEVEL folder that matches an ungrouped root would put that
    # root inside the group folder instead of at the destination: silent mixing,
    # so refuse it. ("A" and "A/B" groups are fine: that is nesting.)
    # A selected archive writes into a folder named after itself, so that name
    # counts as its top-level name too.
    ungrouped_names = set()
    for p in ungrouped:
        ungrouped_names.add(os.path.basename(os.path.normpath(p)).casefold())
        if p in archive_set:
            ungrouped_names.add(zip_folder_name(p).casefold())
    for name in group_names:
        top = group_segments(name)[0]
        if top.casefold() in ungrouped_names:
            return jsonify({'error': ('اسم المجلد «{}» يتعارض مع عنصر غير مجمَّع بنفس الاسم — '
                                      'ضمّه إلى مجموعة أو غيّر الاسم'.format(top))})

    # A group folder that already exists is a merge: files with the same name
    # would be replaced. Ask once, before anything is written, for all of them.
    # Every level of a nested path is checked: a *file* where a folder must be
    # would otherwise fail halfway through the copy.
    existing = []
    group_chain = {}
    for name, _gpaths in groups:
        chain = group_dir_chain(dest, name)
        group_chain[name] = chain
        for dir_path in chain:
            if os.path.exists(dir_path) and not os.path.isdir(dir_path):
                return jsonify({'error': 'يوجد ملف بنفس اسم المجلد «{}» — اختر اسماً آخر'.format(
                    os.path.basename(dir_path))})
        if chain and os.path.exists(chain[-1]):
            existing.append(name)
    if existing and not data.get('confirm_merge'):
        if len(existing) == 1:
            detail = 'مجلد «{}» موجود بالفعل'.format(existing[0])
        else:
            detail = 'المجلدات التالية موجودة بالفعل: {}'.format(
                '، '.join('«{}»'.format(n) for n in existing))
        return jsonify({
            'needs_group_confirm': True,
            'groups': existing,
            'error': (detail + '. سيتم دمج العناصر داخلها، واستبدال أي ملف بنفس '
                      'الاسم. هل تريد المتابعة؟')
        })

    # ----- Selected archives: they are unpacked, not copied -----
    # Nothing is written here. A `.zip` becomes a folder named after the archive
    # inside its destination root (or inside its group folder), and the copy
    # worker unpacks it there in a single write pass: one card write per byte,
    # progress/cancel/verify/report for free, and no temporary staging folder.
    group_of = {os.path.normpath(p): name
                for name, gpaths in groups for p in gpaths}
    zip_targets = {}
    existing_zips = []
    for p in archives:
        owner = group_of.get(os.path.normpath(p), '')
        chain = group_chain.get(owner) or []
        base = (chain[-1] if owner and chain else dest)
        target = os.path.join(base, zip_folder_name(p))
        key = os.path.normpath(target).casefold()
        other = zip_targets.get(key)
        if other is not None:
            return jsonify({'error': 'أرشيفان بنفس الاسم «{}» سيفكّان في مجلد واحد — '
                                     'أعد تسمية أحدهما'.format(os.path.basename(target))})
        zip_targets[key] = p
        if os.path.exists(target) and not os.path.isdir(target):
            return jsonify({'error': 'يوجد ملف باسم «{}» في الوجهة — '
                                     'أعد تسمية الأرشيف أو أخرجه'.format(
                                         os.path.basename(target))})
        if os.path.isdir(target):
            existing_zips.append(os.path.basename(target))
    if existing_zips and not data.get('confirm_extract'):
        unique_zips = list(dict.fromkeys(existing_zips))
        detail = ('مجلد «{}» موجود بالفعل'.format(unique_zips[0]) if len(unique_zips) == 1
                  else 'المجلدات التالية موجودة بالفعل: {}'.format(
                      '، '.join('«{}»'.format(n) for n in unique_zips)))
        return jsonify({
            'needs_extract_confirm': True,
            'folders': unique_zips,
            'error': (detail + '. سيتم فك الأرشيف داخله ودمج ملفاته، واستبدال أي '
                      'ملف بنفس الاسم. هل تريد المتابعة؟')
        })

    # Same self-copy guards as before, once per group target and once for the
    # ungrouped roots that go straight into the destination. An archive is
    # exempt from the mirror check: its target is a *folder* named after it,
    # never the archive file itself.
    for name, gpaths in groups:
        chain = group_chain.get(name) or group_dir_chain(dest, name)
        target = chain[-1] if chain else dest
        if dest_inside_source(gpaths, target):
            return jsonify({'error': 'لا يمكن النسخ إلى مجلد داخل المصدر: ' + target})
        plain = [p for p in gpaths if p not in archive_set]
        if plain and copy_targets_source(plain, target):
            return jsonify({'error': 'الوجهة هي مجلد المصدر نفسه — اختر مجلداً جديداً داخلها بدل اختيارها هي'})
    if ungrouped:
        if dest_inside_source(ungrouped, dest):
            return jsonify({'error': 'لا يمكن النسخ إلى مجلد داخل المصدر: ' + dest})
        plain_ungrouped = [p for p in ungrouped if p not in archive_set]
        if plain_ungrouped and copy_targets_source(plain_ungrouped, dest):
            return jsonify({'error': 'الوجهة هي مجلد المصدر نفسه — اختر مجلداً جديداً داخلها بدل اختيارها هي'})

    verify_content = bool(data.get('verify_content', False))
    verify = bool(data.get('verify', True)) or verify_content
    verify_mode = 'content' if verify_content else ('size' if verify else 'off')
    global ACTIVE_THREAD
    with transfer_lock:
        busy = busy_worker_reason('هناك عملية قيد التشغيل بالفعل', 'copy')
        if busy: return jsonify({'error': busy})
        # Clear the previous run here, not in the worker: otherwise a poll that
        # lands between this request and the thread start still sees the old
        # 'done'/'error' state and the UI reports the wrong result.
        reset_state(status='copying', action='copy', start_time=time.time(),
                    phase='planning', verify_mode=verify_mode,
                    verify_content=verify_content, flush_warning='')
        ACTIVE_THREAD = threading.Thread(
            target=copy_thread_task,
            args=(paths, dest, set(p for p in data.get('excludes', []) if p),
                  groups, verify, verify_content),
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

    # Busy before probing: see start_transfer().
    with transfer_lock:
        busy = busy_worker_reason('النظام مشغول', 'spoof')
    if busy: return jsonify({'error': busy})

    problem = check_destination(dest)
    if problem: return jsonify({'error': problem})

    global ACTIVE_THREAD
    with transfer_lock:
        busy = busy_worker_reason('النظام مشغول', 'spoof')
        if busy: return jsonify({'error': busy})
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
    app.logger.exception(
        'Unhandled request error: %s %s (v%s)',
        request.method, request.path, APP_VERSION,
    )
    return jsonify({'error': f'خطأ داخلي: {e}'}), 500


@app.after_request
def identify_running_version(response):
    response.headers['X-SDMP3-Version'] = APP_VERSION
    if request.path == '/' or request.path.startswith('/api/saved_operations'):
        response.headers['Cache-Control'] = 'no-store'
    return response

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
    # Settings + search history + index settings: read once, here, so the first
    # request is already painted with the saved theme and previous searches.
    try:
        global APP_SETTINGS, SEARCH_HISTORY, APP_INDEX_SETTINGS
        APP_SETTINGS = load_settings(theme_ids(), DEFAULT_THEME,
                                     font_ids(), DEFAULT_FONT,
                                     font_size_ids(), DEFAULT_FONT_SIZE)
        SEARCH_HISTORY = load_search_history()
        APP_INDEX_SETTINGS = load_index_settings()
        # A pulled-out card leaves stale results behind. Prune them once at
        # startup instead of stat()-ing every saved path on each request.
        SEARCH_HISTORY = prune_history_to_roots(SEARCH_HISTORY, os.path.isdir)
        save_search_history(SEARCH_HISTORY)
    except Exception as exc:
        print(f"  settings: using defaults ({exc})", flush=True)
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

