"""On-demand token harvest for MAI2API (no resident Edge).

Launches a DEDICATED Edge profile (the edge-profile folder next to
this script) that holds only the Playground sign-in and the MAI2API
Harvester extension, waits until the proxy reports a fresh token, then
closes exactly the Edge process tree this script started (by its root
PID).

Nothing here is tied to a specific user, account, or tenant: paths and
the proxy port can be overridden with --profile / --port or the
MAI2API_PROFILE_DIR / MAI2API_PORT environment variables.

One-time setup (manual, in the window this script opens):
  1. Run this script once with --setup: Edge opens on the Playground.
  2. Sign in with the Microsoft account you use for the Playground
     (as the only account in this profile, later renewals normally
     complete without an account picker).
  3. In that window: edge://extensions -> Developer mode ->
     Load unpacked -> the harvester-extension folder of this repo.
  4. Close the window. From then on, plain runs are unattended.

Usage:
  pythonw harvest_now.py            # harvest only if token is stale
  pythonw harvest_now.py --force    # harvest regardless
  pythonw harvest_now.py --setup    # first-time interactive setup
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _arg(name, env, default):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return os.environ.get(env, default)


PROFILE = _arg("--profile", "MAI2API_PROFILE_DIR",
               os.path.join(HERE, "edge-profile"))
PORT = _arg("--port", "MAI2API_PORT", "18787")
EXT_DIR = os.path.join(HERE, "harvester-extension")
PIDFILE = os.path.join(HERE, "harvest_edge.pid")
HEALTH = f"http://127.0.0.1:{PORT}/health"
PLAYGROUND = "https://playground.microsoft.ai/chat?model=mai-thinking-1-latest"
STALE_UNDER = 600        # harvest when fewer seconds remain than this
FRESH_OVER = 3000        # a harvest counts as fresh above this


def health():
    try:
        with urllib.request.urlopen(HEALTH, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {"ok": False, "token": {"loaded": False}}


def edge_exe():
    found = shutil.which("msedge") or shutil.which("msedge.exe")
    if found:
        return found
    for cand in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(cand):
            return cand
    return "msedge.exe"


def close_tree_gracefully(root_pid):
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _lp):
        pid = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == root_pid and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    user32.EnumWindows(proc(cb), 0)
    for _ in range(16):
        time.sleep(0.5)
        r = subprocess.run(["tasklist", "/FI", f"PID eq {root_pid}", "/NH"],
                           capture_output=True, text=True)
        if str(root_pid) not in (r.stdout or ""):
            return
    subprocess.run(["taskkill", "/PID", str(root_pid), "/T", "/F"],
                   capture_output=True)


def harvest_count():
    try:
        with open(os.path.join(HERE, "requests.log"),
                  encoding="utf-8", errors="replace") as f:
            return f.read().count("/__harvest")
    except OSError:
        return 0


def main():
    setup = "--setup" in sys.argv
    force = "--force" in sys.argv
    h = health()
    left = (h.get("token") or {}).get("seconds_left")
    if not setup and not force and left and left > STALE_UNDER:
        print(f"token still fresh ({left}s left); nothing to do")
        return 0
    before = harvest_count()

    args = [edge_exe(), f"--user-data-dir={PROFILE}",
            "--no-first-run", "--start-minimized", PLAYGROUND]
    if setup:
        args.remove("--start-minimized")
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    print("harvest Edge root pid:", p.pid)

    if setup:
        print("setup mode: sign in + load the extension, then close the window")
        return 0

    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(4)
        left = (health().get("token") or {}).get("seconds_left")
        # Success = a NEW harvest actually landed during this run and
        # the stored token is fresh (not merely "was already fresh").
        if left and left > FRESH_OVER and harvest_count() > before:
            print(f"fresh token harvested ({left}s left)")
            break
    else:
        print("timed out waiting for a fresh token")

    # Close gracefully first (WM_CLOSE to the windows owned by the
    # process started above) so Edge shuts down cleanly and does not
    # show a "restore pages" prompt on the next launch; escalate to a
    # tree kill only if it does not exit.
    close_tree_gracefully(p.pid)
    print("harvest Edge closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
