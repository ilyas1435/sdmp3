import os
import shutil
import subprocess
import threading
import time
import datetime
import re
from collections import deque
from werkzeug.exceptions import HTTPException
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

# Cache optimization: 512KB to ensure stable flow
COPY_BUFFER_SIZE = 512 * 1024  
# Force hardware sync every 16MB to prevent RAM bloat and freezing
SYNC_THRESHOLD = 16 * 1024 * 1024  

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

def acquire_wakelock():
    try:
        subprocess.run(["termux-wake-lock"], timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def release_wakelock():
    try:
        subprocess.run(["termux-wake-unlock"], timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

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

def try_remove_entry(path):
    try:
        is_dir = os.path.isdir(path) and not os.path.islink(path)
    except Exception:
        is_dir = False
    try:
        if is_dir: os.rmdir(path)
        else: os.remove(path)
        return True
    except Exception:
        try: os.chmod(path, 0o777)
        except Exception: pass
        try:
            if is_dir: os.rmdir(path)
            else: os.remove(path)
            return True
        except Exception:
            return False

def force_delete_tree(target, cancel_check=None, progress_cb=None):
    deleted = 0
    failed = 0
    if not os.path.lexists(target):
        return deleted, failed
    try:
        if os.path.isfile(target) or os.path.islink(target):
            if try_remove_entry(target): return 1, 0
            return 0, 1
        for root, dirs, files in os.walk(target, topdown=False):
            if cancel_check is not None and cancel_check():
                return deleted, failed
            for name in files:
                fp = os.path.join(root, name)
                if try_remove_entry(fp): deleted += 1
                else: failed += 1
                if progress_cb and (deleted + failed) % 20 == 0:
                    progress_cb(deleted, failed)
            for name in dirs:
                try_remove_entry(os.path.join(root, name))
        try_remove_entry(target)
    except Exception: pass
    if progress_cb: progress_cb(deleted, failed)
    return deleted, failed

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
        def build_dfs_queue(current_src, current_dest):
            if current_src in excludes: return
            name = os.path.basename(current_src)
            dst_path = os.path.join(current_dest, name)
            
            if os.path.isfile(current_src):
                ordered_tasks.append((current_src, dst_path, 'file'))
            elif os.path.isdir(current_src):
                ordered_tasks.append((current_src, dst_path, 'dir'))
                try:
                    with os.scandir(current_src) as it:
                        children = sorted(it, key=lambda e: natural_sort_key(e.name))
                        for entry in children:
                            build_dfs_queue(entry.path, dst_path)
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

# ------------------ HTML & JS Template ---------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html dir="rtl" lang="ar">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>JOC Transfer Pro Max</title>
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
            --accent: #6366f1;
            --accent-hover: #4f46e5;
            --accent-soft: rgba(99, 102, 241, 0.14);
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

        .card { background: var(--card); padding: 18px; border-radius: var(--radius); border: 1px solid var(--border); margin-bottom: 16px; }
        .card-title { display:flex; align-items:center; gap:8px; font-size: 14px; font-weight:600; color:#fff; margin: 0 0 14px; }
        .card-title svg { width:17px; height:17px; stroke: var(--accent); }

        label { font-weight: 500; margin-bottom: 8px; display: block; color: var(--text-muted); font-size: 13px; }
        select, input[type="text"], input[type="number"], input[type="datetime-local"] { width: 100%; padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; font-size: 14px; background-color: var(--bg); color: #fff; outline: none; margin-bottom: 12px; transition: border-color 0.15s, box-shadow 0.15s; min-height: 46px; }
        select:focus, input[type="text"]:focus, input[type="number"]:focus, input[type="datetime-local"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }

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

        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(6, 9, 18, 0.85); z-index: 1000; justify-content: center; align-items: center; padding: 16px; }
        .modal-content { background: var(--card); padding: 22px; border-radius: 18px; width: 100%; max-width: 600px; border: 1px solid var(--border); max-height: 90vh; display: flex; flex-direction: column; box-shadow: 0 24px 48px -12px rgba(0,0,0,0.6);}

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

        svg.icon { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    </style>
</head>
<body>

<div class="container">
    <div class="app-header">
        <div class="badge">
            <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
        </div>
        <div>
            <h1>JOC Transfer Pro Max</h1>
            <p>نقل، تنظيم، وتمويه الملفات باحترافية</p>
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
            <button class="btn btn-outline" style="color: var(--warning); border-color: var(--warning);" onclick="createFolderInDest()">
                <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path><line x1="12" y1="11" x2="12" y2="17"></line><line x1="9" y1="14" x2="15" y2="14"></line></svg>
                مجلد جديد بالوجهة
            </button>
        </div>
    </div>

    <div class="card">
        <div class="card-title">
            <svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
            بحث عن مجلد أو ملف
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
        <h3 style="margin-top:0; border-bottom: 1px solid var(--border); padding-bottom: 15px; display:flex; align-items:center; gap:8px;">
            <svg class="icon" style="color:var(--accent);" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path></svg>
            إعدادات فهرس JOC
        </h3>
        
        <div style="display: flex; gap: 10px;">
            <div style="flex: 1;">
                <label>رقم البداية (يبدأ من):</label>
                <input type="number" id="jocStart" value="1" min="1">
            </div>
            <div style="flex: 1;">
                <label>الحد الأقصى للحروف:</label>
                <input type="number" id="jocLimit" value="53" min="10">
            </div>
        </div>
        
        <label>الوضع (Mode):</label>
        <select id="jocMode">
            <option value="2">مجلدات وملفات (الوضع الشامل)</option>
            <option value="1">مجلدات فقط (فهرس رئيسي)</option>
            <option value="3">المجلد الحالي فقط (ملفات بدون تفرع)</option>
        </select>
        
        <label>تنسيق الإخراج (Template):</label>
        <select id="jocTemplate">
            <option value="4">الاسم : الرقم . (مثال: سورة البقرة : 1 .)</option>
            <option value="1">الرقم : الاسم . (مثال: 1 : سورة البقرة .)</option>
            <option value="2">الاسم من الرقم (مثال: سورة البقرة من 1)</option>
            <option value="3">الاسم من رقم الرقم . (مثال: سورة البقرة من رقم 1 .)</option>
        </select>
        
        <label>اسم ملف الناتج:</label>
        <input type="text" id="jocFileName" class="ltr-input" value="JOC_Index.txt">
        
        <label style="display:flex; align-items:center; gap:8px; cursor:pointer; color: var(--text); margin-top: 10px; margin-bottom: 20px;">
            <input type="checkbox" id="jocStrip" class="checkbox" checked style="margin:0;">
            حذف الحروف الإنجليزية والأرقام والرموز
        </label>
        
        <div style="display:flex; gap:12px; margin-top:10px;">
            <button class="btn btn-outline" style="flex:1;" onclick="document.getElementById('jocModal').style.display='none'">إلغاء</button>
            <button class="btn btn-primary" style="flex:2;" onclick="submitJocIndex()">إنشاء وتحميل</button>
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
        document.querySelectorAll('.checkbox').forEach(cb => { cb.checked = isPathSelected(cb.value); });
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
    };

    function updateSourceRoot(path) {
        if (!path) return;
        currentSourceRoot = path;
        localStorage.setItem('joc_source_root', path);
        document.getElementById('custom_source_path').value = path;
        loadSubFolders(path, document.getElementById('treeContainer'));
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
    function createFolderInDest() {
        let dest = document.getElementById('custom_dest_path').value;
        if(!dest) return alert('يرجى تحديد مسار الوجهة أولاً.');
        showInputModal('مجلد جديد', 'اسم المجلد:', '', (name) => {
            if(!name) return;
            fetch('/api/mkdir', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({path: dest, name: name})
            }).then(r=>r.json()).then(res=>{
                if(res.success) alert('تم إنشاء المجلد بنجاح.');
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

    function openJocModal() {
        let dest = document.getElementById('custom_dest_path').value;
        if(!dest) return alert('يرجى تحديد مسار الوجهة أولاً.');
        document.getElementById('jocModal').style.display = 'flex';
    }

    function submitJocIndex() {
        let dest = document.getElementById('custom_dest_path').value;
        let startX = document.getElementById('jocStart').value;
        let limit = document.getElementById('jocLimit').value;
        let mode = document.getElementById('jocMode').value;
        let template = document.getElementById('jocTemplate').value;
        let fileName = document.getElementById('jocFileName').value || 'JOC_Index.txt';
        let strip = document.getElementById('jocStrip').checked;
        
        document.getElementById('jocModal').style.display = 'none';
        
        document.getElementById('modalTitle').innerText = 'جاري إنشاء فهرس JOC...';
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
            resetTransfer();
            if(res.error) {
                alert(res.error);
            } else {
                let blob = new Blob(["\\uFEFF" + res.output], {type: "text/plain;charset=utf-8"});
                let url = URL.createObjectURL(blob);
                let a = document.createElement("a");
                a.href = url;
                a.download = fileName;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                alert("تم إنشاء وتحميل الفهرس بنجاح باسم " + fileName);
            }
        }).catch(e=>{
            resetTransfer();
            alert('حدث خطأ أثناء الإنشاء.');
        });
    }

    let progressModalVisible = false;
    function showProgressModal() {
        document.getElementById('progressModal').style.display = 'flex';
        progressModalVisible = true;
        document.getElementById('cancelProgressBtn').style.display = 'block';
        document.getElementById('closeProgressBtn').style.display = 'none';
        document.getElementById('timeTakenDiv').style.display = 'none';
        document.getElementById('currentFile').style.color = 'var(--text-muted)';
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

                    document.getElementById('cancelProgressBtn').style.display = 'none';
                    document.getElementById('closeProgressBtn').style.display = 'block';
                    
                    if(data.status === 'error') {
                        document.getElementById('currentFile').innerText = 'خطأ: ' + data.log;
                        document.getElementById('currentFile').style.color = 'var(--danger)';
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
    return render_template_string(HTML_TEMPLATE, storages=get_available_storages())

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
        return jsonify({'success': True, 'old_path': old_path, 'new_path': new_path})
    except Exception as e: return jsonify({'success': False, 'error': str(e)})

SEARCH_SKIP_NAMES = {'.thumbnails', '.trashed', 'lost.dir', '.trash'}
SEARCH_MAX_RESULTS = 40
SEARCH_MAX_DIRS_SCANNED = 15000  

def _search_should_skip(entry_name, parent_name):
    if entry_name.lower() in SEARCH_SKIP_NAMES: return True
    if parent_name == 'Android' and entry_name in ('data', 'obb'): return True
    return False

@app.route('/api/search')
def deep_search():
    q = request.args.get('q', '').lower().strip()
    folders_only = request.args.get('folders_only', 'true') == 'true'
    root = request.args.get('root', '/sdcard') or '/sdcard'
    results = []
    if not q: return jsonify({'results': results})
    if not os.path.isdir(root): root = '/sdcard'

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

    return jsonify({'results': results})

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
            deleted, failed = force_delete_tree(path)
            if os.path.lexists(path):
                return jsonify({'success': False, 'error': f'تعذر حذف بعض العناصر المحمية ({failed}), تم حذف {deleted} فقط'})
            return jsonify({'success': True})
        else:
            if try_remove_entry(path): return jsonify({'success': True})
            return jsonify({'success': False, 'error': 'تعذر الحذف (قد يكون الملف محمياً)'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/joc_index', methods=['POST'])
def generate_joc_index():
    data = request.json or {}
    target_dir = data.get('path', '')
    mode = int(data.get('mode', 2))
    start_x = int(data.get('start_x', 1))
    char_limit = int(data.get('char_limit', 53))
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
                children = sorted(it, key=lambda e: natural_sort_key(e.name))
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
                for f in sorted(it, key=lambda e: natural_sort_key(e.name)):
                    if f.is_file() and f.name.lower().endswith('.mp3'):
                        f_name = clean_and_cut(os.path.splitext(f.name)[0], char_limit, strip_symbols)
                        output_lines.append(format_entry(f_name, current_x))
                        current_x += 1
        except: pass

    return jsonify({'success': True, 'output': '\n'.join(output_lines)})

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=False, threaded=True)
