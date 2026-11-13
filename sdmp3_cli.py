#!/usr/bin/env python3
"""sdmp3 - one command for SD-Copy-MP3-Manager on Termux.

Usage:
  sdmp3                          start the web UI in the foreground
  sdmp3 --port 8080              same, with flags passed to the app
  sdmp3 start | stop | restart   manage a detached background instance
  sdmp3 status                   show version, pid, port and URLs
  sdmp3 update [--force] [--restart]
                                 pull the latest version from GitHub
  sdmp3 doctor                   check python / ffmpeg / edge-tts / storage / port
  sdmp3 check-card [path]        find folders Android can no longer read (FAT damage)
  sdmp3 unlock [--revert]        grant Termux all-files access via Shizuku (optional)
  sdmp3 version                  print version and git commit
  sdmp3 open                     open the UI in the Android browser
"""

import argparse
import importlib.metadata
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_PY = os.path.join(APP_DIR, "project.py")
VERSION_FILE = os.path.join(APP_DIR, "VERSION")
REQUIREMENTS = os.path.join(APP_DIR, "requirements.txt")

STATE_DIR = os.path.join(os.path.expanduser("~"), ".local", "state", "sdmp3")
STATE_FILE = os.path.join(STATE_DIR, "instance.json")
LOG_FILE = os.path.join(STATE_DIR, "sdmp3.log")
# One previous copy is enough for support; the log is append-only per run.
LOG_MAX_BYTES = 2 * 1024 * 1024


def rotate_log(path=LOG_FILE, max_bytes=LOG_MAX_BYTES):
    """Keep sdmp3.log from growing without bound across restarts."""
    try:
        if os.path.getsize(path) > max_bytes:
            try:
                os.replace(path, path + ".1")
            except OSError:
                pass
    except OSError:
        pass

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5050
START_TIMEOUT = 20

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #
def paint(code, text):
    return "\033[{}m{}\033[0m".format(code, text) if COLOR else str(text)


def info(msg):
    print(paint("1;36", "==>"), msg, flush=True)


def ok(msg):
    print(paint("1;32", "  ok"), msg, flush=True)


def warn(msg):
    print(paint("1;33", "  !"), msg, flush=True)


def fail(msg):
    print(paint("1;31", "  x"), msg, file=sys.stderr, flush=True)


def die(msg, code=1):
    fail(msg)
    raise SystemExit(code)


# --------------------------------------------------------------------------- #
# project metadata
# --------------------------------------------------------------------------- #
def app_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as fh:
            return fh.read().strip() or "unknown"
    except OSError:
        return "unknown"


def git(*args, timeout=120):
    try:
        return subprocess.run(
            ["git", "-C", APP_DIR] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def git_head(short=True):
    args = ("rev-parse", "--short", "HEAD") if short else ("rev-parse", "HEAD")
    res = git(*args)
    return res.stdout.strip() if res.returncode == 0 else "unknown"


def git_branch():
    res = git("rev-parse", "--abbrev-ref", "HEAD")
    branch = res.stdout.strip()
    return branch if res.returncode == 0 and branch != "HEAD" else "main"


def git_dirty():
    res = git("status", "--porcelain")
    return res.returncode == 0 and bool(res.stdout.strip())


def is_git_checkout():
    return os.path.isdir(os.path.join(APP_DIR, ".git"))


# --------------------------------------------------------------------------- #
# network / process helpers
# --------------------------------------------------------------------------- #
def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def port_open(port, timeout=0.4):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def read_instance():
    """Return {"pid", "port", "host", "started"} for a live instance, else None."""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        pid = int(data.get("pid") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if pid <= 0 or not pid_alive(pid):
        return None
    data["pid"] = pid
    return data


def write_instance(pid, host, port):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(
            {"pid": pid, "host": host, "port": port, "started": time.time()},
            fh,
        )
    os.replace(tmp, STATE_FILE)


def clear_instance():
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def tail_log(lines=20):
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).rstrip()
    except OSError:
        return "(no log yet)"


def format_duration(seconds):
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "{}h {:02d}m".format(hours, minutes)
    if minutes:
        return "{}m {:02d}s".format(minutes, secs)
    return "{}s".format(secs)


def external_storage():
    """SD card names, detected the same way the app does (df -h /storage/...)."""
    cards = []
    for mount in external_mounts():
        name = os.path.basename(mount)
        if name and name not in cards:
            cards.append(name)
    return cards


def external_mounts():
    """Full mount paths of the external SD cards (/storage/XXXX-XXXX)."""
    mounts = []
    try:
        out = subprocess.run(["df", "-h"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return mounts
    for line in out.splitlines():
        if "/storage/" not in line or "emulated" in line:
            continue
        mount = line.split()[-1]
        if mount and mount not in mounts:
            mounts.append(mount)
    return mounts


def rish(command, timeout=10):
    """Run a command through Shizuku's rish shell, or None if unavailable.

    rish sends the remote command's output to stderr, so the streams are merged.
    """
    path = shutil.which("rish")
    if not path:
        return None
    try:
        return subprocess.run([path, "-c", command], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def shizuku_shell():
    res = rish("id")
    return bool(res and res.returncode == 0 and "uid=2000" in (res.stdout or ""))


def all_files_granted():
    res = rish("appops get com.termux MANAGE_EXTERNAL_STORAGE")
    return bool(res and "allow" in (res.stdout or "").lower())


def print_manual_unlock():
    print("     manual way: Settings -> Apps -> Termux -> Special app access", flush=True)
    print("                  -> All files access -> Allow", flush=True)
    print("     the app keeps working without it, but SD card writes may be blocked.", flush=True)


def module_version(dist_name):
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def print_urls(host, port):
    ok("local:   http://localhost:{}".format(port))
    ip = lan_ip()
    if ip and host in ("0.0.0.0", "", "::"):
        ok("network: http://{}:{}".format(ip, port))


# --------------------------------------------------------------------------- #
# server flags (shared by run / start / restart)
# --------------------------------------------------------------------------- #
def parse_server_flags(argv):
    parser = argparse.ArgumentParser(prog="sdmp3", add_help=False)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-wakelock", action="store_true")
    return parser.parse_known_args(argv)


def server_argv(ns, extra):
    args = ["--host", ns.host, "--port", str(ns.port)]
    if ns.no_wakelock:
        args.append("--no-wakelock")
    return args + list(extra)


def ensure_app():
    if not os.path.isfile(APP_PY):
        die("app not found at {} - reinstall with install.sh".format(APP_PY))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_run(argv):
    """Replace this process with the Flask app (foreground, Ctrl+C to stop)."""
    ensure_app()
    os.execv(sys.executable, [sys.executable, APP_PY] + list(argv))


def cmd_start(ns, extra):
    ensure_app()
    inst = read_instance()
    if inst:
        die(
            "already running (pid {}) on port {} - use 'sdmp3 restart' or 'sdmp3 stop'".format(
                inst["pid"], inst["port"]
            )
        )
    if port_open(ns.port):
        die("port {} is already in use by another program (try --port)".format(ns.port))

    os.makedirs(STATE_DIR, exist_ok=True)
    rotate_log()
    with open(LOG_FILE, "ab", buffering=0) as logfile:
        logfile.write(
            "\n=== sdmp3 start {} ===\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S")
            ).encode()
        )
        proc = subprocess.Popen(
            [sys.executable, APP_PY] + server_argv(ns, extra),
            cwd=APP_DIR,
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    write_instance(proc.pid, ns.host, ns.port)
    info("starting sdmp3 (pid {})...".format(proc.pid))

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if port_open(ns.port):
            ok("running on port {}".format(ns.port))
            print_urls(ns.host, ns.port)
            ok("log: {}".format(LOG_FILE))
            return 0
        time.sleep(0.2)

    if proc.poll() is None:
        proc.terminate()
    clear_instance()
    fail("server did not start")
    print(tail_log(), file=sys.stderr)
    return 1


def cmd_stop(quiet=False):
    inst = read_instance()
    if not inst:
        clear_instance()
        if port_open(DEFAULT_PORT) and not quiet:
            warn(
                "port {} is busy but no instance file was found "
                "(started outside sdmp3?)".format(DEFAULT_PORT)
            )
        elif not quiet:
            info("not running")
        return 0

    pid, port = inst["pid"], inst["port"]
    info("stopping sdmp3 (pid {})...".format(pid))
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    for _ in range(100):
        if not pid_alive(pid):
            break
        time.sleep(0.1)

    if pid_alive(pid):
        warn("graceful stop timed out - sending SIGKILL")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        time.sleep(0.3)

    clear_instance()
    if not quiet:
        ok("stopped (port {})".format(port))
    return 0


def cmd_status():
    inst = read_instance()
    print(
        "{} {} ({})".format(
            paint("1;37", "sdmp3"), app_version(), git_head()
        ),
        flush=True,
    )
    ok("install: {}".format(APP_DIR))
    if not inst:
        warn("not running  (start with: sdmp3 start)")
        return 1
    ok("running: pid {} on port {}".format(inst["pid"], inst["port"]))
    print_urls(inst.get("host", DEFAULT_HOST), inst["port"])
    if inst.get("started"):
        ok("uptime:  {}".format(format_duration(time.time() - inst["started"])))
    ok("log:     {}".format(LOG_FILE))
    return 0


def cmd_update(force=False, restart=False):
    if not is_git_checkout():
        die("{} is not a git checkout - reinstall with install.sh".format(APP_DIR))

    dirty = git_dirty()
    if dirty and not force:
        die(
            "local changes in {} - commit/stash them or run 'sdmp3 update --force'".format(
                APP_DIR
            )
        )

    branch = git_branch()
    old_head = git_head(short=False)
    old_version = app_version()
    inst = read_instance()

    info("fetching origin/{}...".format(branch))
    fetch = git("fetch", "--depth", "1", "origin", branch)
    if fetch.returncode != 0:
        die("git fetch failed:\n{}".format((fetch.stderr or fetch.stdout).strip()))

    res = git("rev-parse", "FETCH_HEAD")
    new_head = res.stdout.strip() if res.returncode == 0 else ""
    if not new_head:
        die("could not resolve FETCH_HEAD")

    changed = []
    if new_head != old_head:
        changed = git("diff", "--name-only", old_head, new_head).stdout.split()
        reset = git("reset", "--hard", new_head)
        if reset.returncode != 0:
            die("git reset failed:\n{}".format(reset.stderr.strip()))
        ok(
            "updated {} -> {} ({} -> {})".format(
                old_version, app_version(), old_head[:7], new_head[:7]
            )
        )
        if changed:
            shown = ", ".join(changed[:12])
            if len(changed) > 12:
                shown += " ..."
            print("     changed: {}".format(shown), flush=True)
    elif dirty:
        # --force on an unchanged checkout: throw away the local edits
        git("reset", "--hard", "HEAD")
        ok("already up to date ({} @ {}) - local changes discarded".format(old_version, old_head[:7]))
    else:
        ok("already up to date ({} @ {})".format(old_version, old_head[:7]))

    if "requirements.txt" in changed:
        info("installing updated Python dependencies...")
        pip = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "-r", REQUIREMENTS],
            capture_output=True,
            text=True,
        )
        if pip.returncode == 0:
            ok("dependencies updated")
        else:
            warn("dependency install failed - run: pip install -r {}".format(REQUIREMENTS))

    if inst:
        if restart:
            ns = argparse.Namespace(
                host=inst.get("host", DEFAULT_HOST),
                port=inst["port"],
                no_wakelock=False,
            )
            cmd_stop(quiet=True)
            return cmd_start(ns, [])
        warn("a server is running - restart it to load the new version: sdmp3 restart")

    return 0


def scan_card(root):
    """Walk a card and return (damaged, restricted, dirs, files).

    damaged    = folders that exist but cannot be read (EIO / EACCES outside the
                 Android private area) - the FAT entry is broken.
    restricted = Android/data + Android/obb, which every app is denied by design.
    """
    damaged, restricted = [], []
    dirs = files = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as exc:
            code = getattr(exc, "errno", None)
            private = "/Android/data/" in current or "/Android/obb/" in current
            if private or code in (2, 13):
                restricted.append((current, code, exc.strerror))
            else:
                damaged.append((current, code, exc.strerror))
            continue
        dirs += 1
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    files += 1
            except OSError:
                pass
    return damaged, restricted, dirs, files


def cmd_check_card(argv):
    """Scan the SD cards for folders Android can no longer read (FAT damage)."""
    targets = [a for a in argv if not a.startswith("-")]
    if not targets:
        targets = external_mounts()
    if not targets:
        fail("no external sd card found")
        return 1

    total_damaged = 0
    for root in targets:
        info("scanning {} ...".format(root))
        damaged, restricted, dirs, files = scan_card(root)
        print(
            "     {} folders, {} files readable".format(dirs, files), flush=True
        )
        if restricted:
            print(
                "     {} Android/data or Android/obb folders skipped "
                "(protected by Android on purpose)".format(len(restricted)),
                flush=True,
            )
        if not damaged:
            ok("no damaged folder found on {}".format(root))
            continue
        total_damaged += len(damaged)
        fail("{} damaged folder(s) on {}".format(len(damaged), root))
        for path, code, message in damaged:
            print("       errno {} {} -> {}".format(code, message, path), flush=True)
        print(
            "     these cannot be read or deleted on the phone, not even with\n"
            "     all-files access. repair the card on a computer:\n"
            "       windows : chkdsk X: /f\n"
            "       linux   : sudo fsck.vfat -a /dev/sdX\n"
            "     then delete the folders normally.",
            flush=True,
        )

    return 1 if total_damaged else 0


def probe_card(path, timeout=8):
    """Return (ok, message) for a mounted card.

    Catches the failure mode that makes the app look frozen: a card that is
    still listed as mounted but no longer answers, so even a 5-byte write blocks
    forever inside the kernel. Runs with a hard timeout.
    """
    result = []

    def worker():
        probe = os.path.join(path, ".sdmp3_probe")
        try:
            with open(probe, "wb") as fh:
                fh.write(b"sdmp3")
                fh.flush()
                os.fsync(fh.fileno())
            os.remove(probe)
            result.append(None)
        except OSError as exc:
            result.append(exc)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, "NOT RESPONDING (>{}s) - remove and reinsert the card".format(timeout)
    if result and result[0] is not None:
        return False, "write failed: {}".format(result[0])
    return True, "writable"


def cmd_doctor():
    failures = []
    warnings = []

    def check(ok_, label, detail="", hard=True):
        line = label if not detail else "{}: {}".format(label, detail)
        if ok_:
            ok(line)
        elif hard:
            fail(line)
            failures.append(label)
        else:
            warn(line)
            warnings.append(label)

    version = sys.version_info
    check(
        version >= (3, 8),
        "python",
        "{}.{}.{}".format(version.major, version.minor, version.micro),
    )

    for dist, label, hard in (
        ("flask", "flask", True),
        ("edge-tts", "edge-tts", False),
    ):
        ver = module_version(dist)
        check(ver is not None, label, ver or "not installed", hard=hard)

    for binary, label, hard in (
        ("ffmpeg", "ffmpeg", False),
        ("termux-wake-lock", "termux-wake-lock", False),
        ("git", "git", True),
    ):
        path = shutil.which(binary)
        check(path is not None, label, path or "not found (pkg install " + binary + ")", hard=hard)

    check(os.path.isdir("/sdcard"), "storage /sdcard", "present" if os.path.isdir("/sdcard") else "missing")
    writable = os.path.isdir("/sdcard") and os.access("/sdcard", os.W_OK)
    check(writable, "/sdcard writable", "yes" if writable else "no - run: termux-setup-storage", hard=False)

    cards = external_storage()
    check(bool(cards), "external sd cards", ", ".join(cards) if cards else "none mounted", hard=False)
    for mount in external_mounts():
        healthy, message = probe_card(mount)
        check(healthy, "card {}".format(mount), message, hard=False)

    if shutil.which("rish"):
        running = shizuku_shell()
        check(running, "shizuku", "running" if running else "installed but not running", hard=False)
        if running:
            granted = all_files_granted()
            check(
                granted,
                "sd card all-files access",
                "granted" if granted else "not granted - run: sdmp3 unlock",
                hard=False,
            )
    else:
        warn("shizuku: not set up (optional - 'sdmp3 unlock' helps SD writes)")

    inst = read_instance()
    if inst:
        check(True, "port {}".format(inst["port"]), "in use by sdmp3 (pid {})".format(inst["pid"]))
    else:
        busy = port_open(DEFAULT_PORT)
        check(not busy, "port {}".format(DEFAULT_PORT), "busy - another program is using it" if busy else "free")

    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
        free_gb = usage.free / (1024 ** 3)
        check(free_gb >= 0.2, "free space (~)", "{:.1f} GB".format(free_gb), hard=False)
    except OSError:
        pass

    if is_git_checkout():
        dirty = git_dirty()
        check(not dirty, "git checkout", "clean" if not dirty else "local changes present", hard=False)
        ok("version: {} ({})".format(app_version(), git_head()))
    else:
        check(False, "git checkout", "missing - reinstall with install.sh", hard=False)

    print("", flush=True)
    if failures:
        fail("{} problem(s) found: {}".format(len(failures), ", ".join(failures)))
        return 1
    if warnings:
        warn("{} warning(s) - optional features may not work".format(len(warnings)))
    ok("environment looks good")
    return 0


def cmd_unlock(argv):
    """Grant Termux all-files access through Shizuku. Purely optional."""
    revert = "--revert" in argv
    action = "default" if revert else "allow"

    if not shutil.which("rish"):
        fail("rish not found - Shizuku is not set up for Termux")
        print_manual_unlock()
        return 1

    info("asking Shizuku for a shell...")
    if not shizuku_shell():
        fail("Shizuku is installed but not running")
        print("     open the Shizuku app, start it, then retry.", flush=True)
        print_manual_unlock()
        return 1
    ok("Shizuku shell available (uid=2000)")

    info("{} all-files access for com.termux...".format("revoking" if revert else "granting"))
    res = rish("appops set com.termux MANAGE_EXTERNAL_STORAGE {}".format(action))
    if res is None or res.returncode != 0:
        fail("could not change the permission")
        print_manual_unlock()
        return 1

    if revert:
        ok("all-files access revoked")
        return 0

    if all_files_granted():
        ok("granted - the app can now write to the SD card")
        info("undo later with: sdmp3 unlock --revert")
        return 0

    warn("command sent, but the permission still reads as not granted")
    print_manual_unlock()
    return 1


def cmd_open(argv):
    inst = read_instance()
    default = inst["port"] if inst else DEFAULT_PORT
    port = int(argv[0]) if argv and argv[0].isdigit() else default
    url = "http://localhost:{}".format(port)
    opener = shutil.which("termux-open-url")
    if not opener:
        info("open this in a browser: {}".format(url))
        return 0
    subprocess.run([opener, url], check=False)
    return 0


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return cmd_run([])

    cmd, rest = argv[0], argv[1:]

    if cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if cmd in ("-v", "--version", "version"):
        print("sdmp3 {} ({})".format(app_version(), git_head()))
        return 0
    if cmd == "run":
        return cmd_run(rest)
    if cmd in ("start", "restart"):
        ns, extra = parse_server_flags(rest)
        if cmd == "restart":
            cmd_stop(quiet=True)
        return cmd_start(ns, extra)
    if cmd == "stop":
        return cmd_stop()
    if cmd in ("status", "ps"):
        return cmd_status()
    if cmd == "update":
        return cmd_update(force="--force" in rest, restart="--restart" in rest)
    if cmd == "doctor":
        return cmd_doctor()
    if cmd in ("check-card", "checkcard", "fsck"):
        return cmd_check_card(rest)
    if cmd == "unlock":
        return cmd_unlock(rest)
    if cmd == "open":
        return cmd_open(rest)
    if cmd.startswith("-"):
        # flags only, e.g. `sdmp3 --port 8080` -> foreground run
        return cmd_run(argv)

    die("unknown command: {}\nrun 'sdmp3 --help' for usage".format(cmd))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
