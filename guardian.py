"""Optional guardian for the MAI2API proxy (opt-in).

Polls the proxy every 30 s, keyed on LIVENESS (/health/live), not
on token readiness -- a stale token is the keeper's problem and
restarting the proxy would not fix it.

Recovery policy:
  * proxy process (the PID recorded in proxy.pid) gone AND the
    live check fails twice in a row: start the proxy.
  * process alive but the live check fails WEDGE_POLLS polls in a
    row (the process is wedged, not merely token-stale): terminate
    the recorded PID tree (graceful first, force after 15 s), then
    start the proxy. Wedge restarts are capped at 3 per rolling
    hour; beyond that the guardian keeps polling but stops
    restarting until a healthy poll resets the window. Every
    restart is recorded in guardian_state.json.
  * it never touches any process other than the recorded PID tree,
    and it stands down when guardian_stop.txt exists.

It also rotates proxy.log / requests.log (5 MB, two backups) each
time it starts the proxy -- at that moment the proxy is down, so
no writer holds the files.

Run at logon INSTEAD of mai_proxy.py if you want crash recovery:
point your auto-start entry at this script. It starts the proxy on
first run if needed.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _state_dir():
    """Runtime state lives outside the source tree:
    %LOCALAPPDATA%\\MAI2API by default, or MAI2API_STATE_DIR."""
    env = os.environ.get("MAI2API_STATE_DIR")
    if env:
        return env
    lad = os.environ.get("LOCALAPPDATA")
    if lad:
        return os.path.join(lad, "MAI2API")
    return HERE


STATE_DIR = _state_dir()
PROXY = os.path.join(HERE, "mai_proxy.py")
PIDFILE = os.path.join(STATE_DIR, "proxy.pid")
STOPFILE = os.path.join(STATE_DIR, "guardian_stop.txt")
STATE = os.path.join(STATE_DIR, "guardian_state.json")
PORT = os.environ.get("MAI_PROXY_PORT", "18787")
LIVE = f"http://127.0.0.1:{PORT}/health/live"
WEDGE_POLLS = int(os.environ.get("MAI2API_GUARDIAN_WEDGE_POLLS", "10"))
WEDGE_CAP = 3
WEDGE_WINDOW = 3600
LOG_MAX = 5 * 1024 * 1024


def live_ok():
    try:
        with urllib.request.urlopen(LIVE, timeout=5) as r:
            return json.loads(r.read().decode()).get("ok") is True
    except Exception:
        return False


def pid_alive(pid):
    if not pid:
        return False
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                       capture_output=True, text=True)
    return str(pid) in (r.stdout or "")


def _load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"wedge_restarts": [], "total_restarts": 0,
                "last_restart": None, "last_reason": None}


def _save_state(st):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def rotate_log(path):
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
            for src, dst in ((path + ".1", path + ".2"), (path, path + ".1")):
                if os.path.exists(src):
                    os.replace(src, dst)
    except OSError:
        pass


def start_proxy(reason):
    rotate_log(os.path.join(STATE_DIR, "proxy.log"))
    rotate_log(os.path.join(STATE_DIR, "requests.log"))
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    DETACHED = 0x00000008 | 0x00000200
    log = open(os.path.join(STATE_DIR, "proxy.log"), "ab")
    p = subprocess.Popen([pyw, PROXY], creationflags=DETACHED,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    st = _load_state()
    st["total_restarts"] = st.get("total_restarts", 0) + 1
    st["last_restart"] = int(time.time())
    st["last_reason"] = reason
    _save_state(st)
    print(f"[guardian] started proxy pid {p.pid} ({reason})", flush=True)
    return p.pid


def terminate_tree(pid):
    """Terminate ONLY the recorded PID's tree: graceful first,
    force after a grace period if it is still alive."""
    subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                   capture_output=True, text=True)
    for _ in range(15):
        if not pid_alive(pid):
            return True
        time.sleep(1)
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, text=True)
    for _ in range(5):
        if not pid_alive(pid):
            return True
        time.sleep(1)
    return not pid_alive(pid)


def main():
    # Singleton: hold a localhost port for the guardian's lifetime
    # so an autostart entry and a manual launch cannot both run.
    lock_port = int(os.environ.get("MAI2API_GUARDIAN_LOCK_PORT", "18790"))
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", lock_port))
        lock.listen(1)
    except OSError:
        print(f"another guardian already holds :{lock_port}; exiting",
              flush=True)
        return
    fails = 0
    wedge = 0
    capped = False
    while not os.path.exists(STOPFILE):
        pid = None
        try:
            pid = int(open(PIDFILE).read().strip())
        except Exception:
            pass
        if live_ok():
            fails = 0
            wedge = 0
            capped = False
        elif not pid_alive(pid):
            wedge = 0
            fails += 1
            if fails >= 2:
                start_proxy("process gone")
                fails = 0
                time.sleep(10)
        else:
            # Alive but not live: possibly wedged. Token staleness
            # does NOT land here -- /health/live answers regardless
            # of the token -- so a long failure streak means the
            # process itself is stuck.
            fails = 0
            wedge += 1
            if wedge >= WEDGE_POLLS:
                st = _load_state()
                now = time.time()
                recent = [t for t in st.get("wedge_restarts", [])
                          if now - t < WEDGE_WINDOW]
                if len(recent) < WEDGE_CAP:
                    print(f"[guardian] proxy pid {pid} wedged "
                          f"({wedge} failed live checks); restarting",
                          flush=True)
                    if terminate_tree(pid):
                        recent.append(int(now))
                        st["wedge_restarts"] = recent
                        _save_state(st)
                        start_proxy("wedged")
                    wedge = 0
                    time.sleep(10)
                else:
                    if not capped:
                        print("[guardian] wedge restart cap reached; "
                              "leaving the process alone", flush=True)
                        capped = True
                    wedge = 0
        time.sleep(30)


if __name__ == "__main__":
    main()
