"""Optional guardian for the MAI2API proxy (opt-in).

Polls the proxy /health every 30 s. If the proxy process recorded in
proxy.pid is gone AND health checks fail twice in a row, it restarts
the proxy (pythonw mai_proxy.py in this folder) and records the new
PID. It never touches any other process, and it stands down when a
file named guardian_stop.txt exists in this folder (create it to stop
both the guardian and auto-restarts; the proxy itself keeps running
until you stop it).

Run at logon INSTEAD of mai_proxy.py if you want crash recovery:
point your auto-start entry at this script. It starts the proxy on
first run if needed.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "mai_proxy.py")
PIDFILE = os.path.join(HERE, "proxy.pid")
STOPFILE = os.path.join(HERE, "guardian_stop.txt")
PORT = os.environ.get("MAI_PROXY_PORT", "18787")
HEALTH = f"http://127.0.0.1:{PORT}/health"


def healthy():
    try:
        with urllib.request.urlopen(HEALTH, timeout=5) as r:
            return json.loads(r.read().decode()).get("ok") is True
    except Exception:
        return False


def pid_alive(pid):
    if not pid:
        return False
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                       capture_output=True, text=True)
    return str(pid) in (r.stdout or "")


def start_proxy():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    DETACHED = 0x00000008 | 0x00000200
    log = open(os.path.join(HERE, "proxy.log"), "ab")
    p = subprocess.Popen([pyw, PROXY], creationflags=DETACHED,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    return p.pid


def main():
    fails = 0
    while not os.path.exists(STOPFILE):
        pid = None
        try:
            pid = int(open(PIDFILE).read().strip())
        except Exception:
            pass
        if healthy():
            fails = 0
        elif not pid_alive(pid):
            fails += 1
            if fails >= 2:
                start_proxy()
                fails = 0
                time.sleep(10)
        else:
            # Process alive but unhealthy (wedged): leave it alone —
            # restarting a live process is riskier than waiting.
            fails = 0
        time.sleep(30)


if __name__ == "__main__":
    main()
