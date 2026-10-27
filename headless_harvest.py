"""Headless-Edge token harvest for MAI2API (resident-browser pattern).

The companion/bridge in companion tooling keeps its dedicated Edge FULLY
headless (--headless=new: no window, no taskbar, nothing to dismiss),
parked off-screen, with renderer/timer throttling disabled, and drives
it over CDP on 127.0.0.1. This script applies the same pattern to the
MAI2API dedicated profile instead of the old approach (a minimized GUI
window relying on an extension timer -- minimized windows get their
renderers throttled/discarded and grow popups that need a human).

What it does:
  1. Ensures a headless Edge is running for the dedicated profile
     (default: edge-profile/ next to this script) with CDP on
     127.0.0.1:<port> (default 9231). Idempotent: if the CDP endpoint
     the bridge, 9229 was a one-off probe). Idempotent: if the CDP
     endpoint already answers, the running instance is reused and left
     alone. The browser is RESIDENT -- it is not closed afterwards.
  2. Opens (or reuses) a Playground tab over CDP and evaluates
     fetch('/.auth/me') in the page, taking the Easy Auth id_token
     from the response exactly as the harvester extension did.
  3. POSTs the raw token to the proxy's /__harvest endpoint (the same
     delivery the extension's background worker made) and reports the
     proxy /health token seconds_left. The token is never printed.

If the profile session is signed out, the harvest lands on a login
redirect and no token is found: the script exits with rc 2 and prints
NEEDS_SIGNIN. The fix then is ONE visible sign-in launch with the same
profile (the bridge's -Foreground swap), after which headless runs are
unattended again. Nothing here types credentials or clicks pickers.

Usage:
  python headless_harvest.py            # ensure Edge + harvest once
  python headless_harvest.py --port N   # custom CDP port
"""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "edge-profile")
CDP_PORT = 9231
PROXY_PORT = int(os.environ.get("MAI2API_PORT", "18787"))
PIDFILE = os.path.join(HERE, "headless_edge.pid")
PLAYGROUND = "https://playground.microsoft.ai/chat?model=mai-thinking-1-latest"
LOGIN = ("https://playground.microsoft.ai/.auth/login/aad"
         "?post_login_redirect_uri=%2Fchat%3Fmodel%3Dmai-thinking-1-latest")

EDGE_FLAGS = [
    "--headless=new",
    "--window-position=-32000,-32000",
    "--window-size=1400,1000",
    "--remote-debugging-address=127.0.0.1",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-extensions",
    "--disable-sync",
    "--disable-component-update",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--process-per-site",
    "--disk-cache-size=2097152",
    "--disable-gpu-shader-disk-cache",
    "--disable-gpu",
    "--noerrdialogs",
]


def http_json(url, method="GET", timeout=10):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def cdp_up():
    try:
        http_json(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=4)
        return True
    except Exception:
        return False


def edge_exe():
    import shutil
    found = shutil.which("msedge") or shutil.which("msedge.exe")
    if found:
        return found
    for cand in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.exists(cand):
            return cand
    return "msedge.exe"


def ensure_edge():
    if cdp_up():
        print(f"headless Edge already up on :{CDP_PORT} (reused)")
        return None
    args = [edge_exe(), f"--user-data-dir={PROFILE}",
            f"--remote-debugging-port={CDP_PORT}"] + EDGE_FLAGS + ["about:blank"]
    DETACHED = 0x00000008 | 0x00000200
    log = open(os.path.join(HERE, "headless_edge.log"), "ab")
    p = subprocess.Popen(args, creationflags=DETACHED,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    print(f"headless Edge launched, root pid {p.pid}")
    for _ in range(40):
        if cdp_up():
            print(f"CDP :{CDP_PORT} is answering")
            return p.pid
        time.sleep(1)
    print("ERROR: CDP did not come up in 40s")
    return p.pid


async def evaluate(ws, expr, msg_id):
    await ws.send(json.dumps({
        "id": msg_id, "method": "Runtime.evaluate",
        "params": {"expression": expr, "awaitPromise": True,
                   "returnByValue": True}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == msg_id:
            return msg.get("result", {}).get("result", {})


async def harvest_once():
    import websockets
    targets = http_json(f"http://127.0.0.1:{CDP_PORT}/json/list")
    page = None
    for t in targets:
        if t.get("type") == "page" and "playground.microsoft.ai" in t.get("url", ""):
            page = t
            break
    if page is None:
        page = http_json(
            f"http://127.0.0.1:{CDP_PORT}/json/new?{urllib.parse.quote(PLAYGROUND, safe='')}",
            method="PUT")
        print("opened Playground tab:", page.get("id"))
    ws_url = page["webSocketDebuggerUrl"]
    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        mid = 0

        async def ev(expr):
            nonlocal mid
            mid += 1
            return await evaluate(ws, expr, mid)

        fetch_me = ("fetch('/.auth/me',{credentials:'include'})"
                    ".then(r=>r.text().then(t=>({status:r.status,body:t})))"
                    ".catch(e=>({status:0,body:String(e)}))")

        async def read_token():
            res = await ev(fetch_me)
            val = res.get("value") or {}
            body = val.get("body") or ""
            try:
                data = json.loads(body)
                tok = data[0].get("id_token")
                return tok
            except Exception:
                return None

        # Give a fresh tab time to load, then read the token. A stale
        # Easy Auth session does NOT redirect the page away -- the SPA
        # shell loads fine and /.auth/me simply fails/401s in place
        # (verified live 2026-10-27) -- so after a few failed reads,
        # ALWAYS drive the login flow explicitly. In this profile SSO
        # completes unattended (no picker, no click) and /.auth/me
        # then returns a fresh id_token.
        tok = None
        kicked = False
        for i in range(24):
            await asyncio.sleep(3)
            tok = await read_token()
            if tok:
                break
            if i == 3 and not kicked:
                kicked = True
                await ev(f"location.href = {json.dumps(LOGIN)}; 'nav'")
        if not tok:
            url_res = await ev("location.href")
            print("no token; page at:", url_res.get("value"))
            return None
        return tok


def main():
    if "--port" in sys.argv:
        global CDP_PORT
        CDP_PORT = int(sys.argv[sys.argv.index("--port") + 1])
    if "--loop" in sys.argv:
        # Periodic mode: harvest every 4 minutes (the cadence the
        # extension used), standing down when harvest_stop.txt exists.
        # The Edge stays resident; each pass is idempotent.
        stop = os.path.join(HERE, "harvest_stop.txt")
        while not os.path.exists(stop):
            try:
                ensure_edge()
                tok = asyncio.run(harvest_once())
                if tok:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{PROXY_PORT}/__harvest",
                        method="POST", data=tok.encode())
                    urllib.request.urlopen(req, timeout=15).read()
                    h = http_json(f"http://127.0.0.1:{PROXY_PORT}/health")
                    print("loop harvest ok, seconds_left:",
                          (h.get("token") or {}).get("seconds_left"),
                          flush=True)
                else:
                    print("loop harvest: NEEDS_SIGNIN", flush=True)
            except Exception as e:  # noqa: BLE001
                print("loop harvest error:", repr(e), flush=True)
            for _ in range(24):
                if os.path.exists(stop):
                    break
                time.sleep(10)
        print("harvest loop stopped (harvest_stop.txt present)")
        return 0
    ensure_edge()
    tok = asyncio.run(harvest_once())
    if not tok:
        print("NEEDS_SIGNIN: no id_token from /.auth/me (profile session "
              "is signed out or waiting on an interactive prompt)")
        return 2
    req = urllib.request.Request(
        f"http://127.0.0.1:{PROXY_PORT}/__harvest", method="POST",
        data=tok.encode())
    with urllib.request.urlopen(req, timeout=15) as r:
        print("harvest POST ->", r.status)
    h = http_json(f"http://127.0.0.1:{PROXY_PORT}/health")
    left = (h.get("token") or {}).get("seconds_left")
    print(f"proxy token seconds_left: {left}")
    print("HARVEST OK" if left and left > 600 else "HARVEST SUSPECT")
    return 0 if left and left > 600 else 1


if __name__ == "__main__":
    sys.exit(main())
