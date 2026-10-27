"""RPA session keeper for MAI2API.

The OAuth shortcuts are closed on strict tenants (Conditional Access
denies the device-code flow, the app registers no localhost redirect,
/.auth/refresh is 403). What remains sanctioned is the browser
session itself -- so this keeper babysits the session with simple,
dumb, periodic RPA:

  * a resident headless Edge on a dedicated profile (CDP 127.0.0.1),
  * every pass reads /.auth/me and checks the JWT exp (a present
    token string is not a fresh token: /.auth/me keeps returning the
    expired token object); if the token is gone or no longer fresh
    it drives the Easy Auth login flow and handles the screens a raw
    navigation cannot pass, with REAL mouse clicks over CDP:
      - account picker: click the tile matching the learned account
        (name/domain stored in rpa_hints.json after a success),
      - "stay signed in" (KMSI): click Yes,
      - Next / Sign-in buttons when a flow stalls on them,
  * if the token is fine it still performs one harmless keepalive
    click in the app UI, so the session sees regular activity,
  * a fresh token is POSTed to the proxy /__harvest.

No credentials are ever typed: clicks only select/confirm what the
signed-in session itself offers. Token values are never printed.
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "edge-profile")
CDP_PORT = 9231
PROXY_PORT = int(os.environ.get("MAI2API_PORT", "18787"))
PIDFILE = os.path.join(HERE, "rpa_edge.pid")
HINTS = os.path.join(HERE, "rpa_hints.json")
STOP = os.path.join(HERE, "rpa_stop.txt")
PLAYGROUND = "https://playground.microsoft.ai/chat?model=mai-thinking-1-latest"
LOGIN = ("https://playground.microsoft.ai/.auth/login/aad"
         "?post_login_redirect_uri=%2Fchat%3Fmodel%3Dmai-thinking-1-latest")

EDGE_FLAGS = [
    "--headless=new", "--window-position=-32000,-32000",
    "--window-size=1400,1000", "--remote-debugging-address=127.0.0.1",
    "--no-first-run", "--no-default-browser-check",
    "--hide-crash-restore-bubble", "--disable-session-crashed-bubble",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-extensions",
    "--disable-sync", "--disable-component-update",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--process-per-site", "--disk-cache-size=2097152",
    "--disable-gpu-shader-disk-cache", "--disable-gpu", "--noerrdialogs",
]


REFRESH_THRESHOLD = int(os.environ.get("MAI2API_REFRESH_THRESHOLD", "600"))


def jwt_exp(tok):
    """Expiry (epoch seconds) of a JWT, or 0 when undecodable.
    Presence of a token string is NOT freshness: /.auth/me keeps
    returning the session's token object after it has expired, so
    every acceptance decision must be made on exp."""
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p))["exp"])
    except Exception:
        return 0


def seconds_left(tok):
    return jwt_exp(tok) - int(time.time()) if tok else 0


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


def ensure_edge():
    if cdp_up():
        return
    import shutil
    exe = shutil.which("msedge") or shutil.which("msedge.exe")
    if not exe:
        for cand in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                     r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
            if os.path.exists(cand):
                exe = cand
                break
    args = [exe or "msedge.exe", f"--user-data-dir={PROFILE}",
            f"--remote-debugging-port={CDP_PORT}"] + EDGE_FLAGS + ["about:blank"]
    log = open(os.path.join(HERE, "rpa_edge.log"), "ab")
    p = subprocess.Popen(args, creationflags=0x00000008 | 0x00000200,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    with open(PIDFILE, "w") as f:
        f.write(str(p.pid))
    for _ in range(40):
        if cdp_up():
            return
        time.sleep(1)
    raise RuntimeError("CDP did not come up")


def load_hints():
    try:
        return json.load(open(HINTS, encoding="utf-8"))
    except Exception:
        return {}


def save_hints(h):
    with open(HINTS, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False)


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.mid = 0

    async def _send(self, method, params=None):
        self.mid += 1
        await self.ws.send(json.dumps(
            {"id": self.mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.mid:
                return msg.get("result", {})

    async def eval(self, expr):
        r = await self._send("Runtime.evaluate", {
            "expression": expr, "awaitPromise": True, "returnByValue": True})
        return (r.get("result") or {}).get("value")

    async def click_xy(self, x, y):
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await self._send("Input.dispatchMouseEvent", {
                "type": kind, "x": float(x), "y": float(y),
                "button": "left", "clickCount": 1})
        return True

    async def click_text(self, needles, kinds=("button", "[role=button]",
                                               "a", "input[type=submit]",
                                               "[data-testid]")):
        """Find a visible element whose text contains any needle and
        click its centre with a real mouse event. Returns the matched
        text or None."""
        js = """
        (needles => {
          const sels = %s;
          const els = new Set();
          sels.forEach(s => document.querySelectorAll(s)
            .forEach(e => els.add(e)));
          for (const e of els) {
            const t = (e.innerText || e.value || '').trim();
            if (!t) continue;
            const low = t.toLowerCase();
            if (!needles.some(n => low.includes(n))) continue;
            const r = e.getBoundingClientRect();
            if (r.width < 4 || r.height < 4) continue;
            e.scrollIntoView({block: 'center'});
            const r2 = e.getBoundingClientRect();
            return {x: r2.x + r2.width / 2, y: r2.y + r2.height / 2,
                    text: t.slice(0, 60)};
          }
          return null;
        })(%s)""" % (json.dumps(list(kinds)), json.dumps(
            [n.lower() for n in needles]))
        hit = await self.eval(js)
        if hit:
            await self.click_xy(hit["x"], hit["y"])
            return hit["text"]
        return None

    async def click_tile(self, hint_name, hint_domain):
        """Account-picker tiles are usually plain divs with the account
        name + address as text. Match on the learned name/domain."""
        needles = [n for n in (hint_name, hint_domain) if n]
        if not needles:
            return None
        js = """
        (needles => {
          const all = document.querySelectorAll('div, li, button, a');
          for (const e of all) {
            const t = (e.innerText || '').trim();
            if (!t || t.length > 200) continue;
            if (!t.includes('@') && !needles.some(n => t.includes(n)))
              continue;
            if (!needles.some(n => t.toLowerCase().includes(n.toLowerCase())))
              continue;
            const r = e.getBoundingClientRect();
            if (r.width < 40 || r.height < 10) continue;
            e.scrollIntoView({block: 'center'});
            const r2 = e.getBoundingClientRect();
            return {x: r2.x + r2.width / 2, y: r2.y + r2.height / 2,
                    text: t.slice(0, 60)};
          }
          return null;
        })(%s)""" % json.dumps(needles)
        hit = await self.eval(js)
        if hit:
            await self.click_xy(hit["x"], hit["y"])
            return hit["text"]
        return None

    async def me(self):
        out = await self.eval(
            "fetch('/.auth/me',{credentials:'include'})"
            ".then(r=>r.text().then(t=>({s:r.status,t})))"
            ".catch(e=>({s:0,t:String(e)}))")
        if not out or out.get("s") != 200:
            return None, None
        try:
            data = json.loads(out["t"])
            entry = data[0]
            return entry.get("id_token"), entry
        except Exception:
            return None, None


async def keeper_pass(force_login=False):
    import websockets
    ensure_edge()
    targets = http_json(f"http://127.0.0.1:{CDP_PORT}/json/list")
    page = next((t for t in targets if t.get("type") == "page"
                 and "playground.microsoft.ai" in t.get("url", "")), None)
    if page is None:
        page = http_json(
            f"http://127.0.0.1:{CDP_PORT}/json/new?"
            + urllib.parse.quote(PLAYGROUND, safe=""), method="PUT")
    async with websockets.connect(page["webSocketDebuggerUrl"],
                                  max_size=8 * 1024 * 1024) as ws:
        pg = Page(ws)
        await asyncio.sleep(2)
        tok, entry = await pg.me()
        old_exp = jwt_exp(tok) if tok else 0
        if tok and not force_login and \
                old_exp - int(time.time()) > REFRESH_THRESHOLD:
            clicked = await pg.click_text(
                ["新しいチャット", "new chat", "new conversation"])
            if not clicked:
                await pg.click_xy(60, 60)  # harmless corner activity
            print(f"pass: token fresh "
                  f"({old_exp - int(time.time())}s left); keepalive click "
                  f"({clicked or 'corner'})", flush=True)
            return tok, entry
        print(f"pass: token stale/absent "
              f"({old_exp - int(time.time()) if tok else 'none'}s left); "
              f"driving login flow", flush=True)
        await pg.eval(f"location.href = {json.dumps(LOGIN)}; 'nav'")
        hints = load_hints()
        for i in range(30):
            await asyncio.sleep(3)
            tok, entry = await pg.me()
            # Success requires a genuinely NEW token: the stale
            # session object keeps answering /.auth/me with the old
            # JWT, so accept only an exp that actually advanced.
            if tok and jwt_exp(tok) > old_exp + 60:
                break
            url = await pg.eval("location.href") or ""
            host = urllib.parse.urlparse(url).netloc
            if "login" in host or "adfs" in host or i % 3 == 0:
                tile = await pg.click_tile(hints.get("name"),
                                           hints.get("domain"))
                if tile:
                    print(f"  clicked account tile: {tile}", flush=True)
                    continue
                yes = await pg.click_text(
                    ["はい", "yes", "ログインしたまま", "stay signed in"])
                if yes:
                    print(f"  clicked stay-signed-in: {yes}", flush=True)
                    continue
                nxt = await pg.click_text(
                    ["次へ", "next", "サインイン", "sign in"])
                if nxt:
                    print(f"  clicked flow button: {nxt}", flush=True)
                    continue
        return tok, entry


def deliver(tok, entry):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PROXY_PORT}/__harvest", method="POST",
        data=tok.encode())
    urllib.request.urlopen(req, timeout=15).read()
    h = http_json(f"http://127.0.0.1:{PROXY_PORT}/health")
    left = (h.get("token") or {}).get("seconds_left")
    if entry:
        claims = {c.get("typ"): c.get("val")
                  for c in entry.get("user_claims", [])}
        name = claims.get("name") or ""
        upn = claims.get(
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
            "") or claims.get("preferred_username", "")
        dom = upn.split("@")[-1] if "@" in upn else ""
        if name or dom:
            save_hints({"name": name, "domain": dom})
    print(f"harvest delivered; seconds_left={left}", flush=True)
    return left


try:
    from notify import notify
except Exception:  # notification is an add-on; never block the keeper
    def notify(event, detail="", priority="default"):
        print(f"notify unavailable [{event}] {detail}", flush=True)
        return False

STATE = os.path.join(HERE, "keeper_state.json")
ALERT_EVERY = 6 * 3600  # re-alert at most every 6h while a state holds


def _load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"alert": None, "last_notify": 0, "consec": 0}


def _save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def handle_result(st, rc, err=None):
    """State-transition notifications, emitted by the script itself."""
    now = time.time()
    if rc == 2:  # NEEDS_SIGNIN
        if st.get("alert") != "needs_signin" or \
                now - st.get("last_notify", 0) > ALERT_EVERY:
            notify("要サインイン",
                   "Playgroundのセッションが切れています。"
                   "専用プロファイルで一度サインインしてください",
                   priority="high")
            st["alert"] = "needs_signin"
            st["last_notify"] = now
        st["consec"] = 0
    elif rc == 1:  # stale token; refresh attempted and failed
        st["consec"] = st.get("consec", 0) + 1
        if st["consec"] >= 3 and (
                st.get("alert") != "stale"
                or now - st.get("last_notify", 0) > ALERT_EVERY):
            notify("トークン更新失敗",
                   f"期限切れトークンの更新が{st['consec']}回連続で"
                   f"成功していません", priority="high")
            st["alert"] = "stale"
            st["last_notify"] = now
    elif err is not None:
        st["consec"] = st.get("consec", 0) + 1
        if st["consec"] >= 3 and (
                st.get("alert") != "failures"
                or now - st.get("last_notify", 0) > ALERT_EVERY):
            notify("巡回失敗",
                   f"keeper pass が{st['consec']}回連続で失敗しています"
                   f"（{err}）", priority="high")
            st["alert"] = "failures"
            st["last_notify"] = now
    else:  # rc == 0: a fresh token was delivered
        if st.get("alert") in ("needs_signin", "stale", "failures"):
            notify("復旧", "トークン収穫が再開しました")
        st["alert"] = None
        st["consec"] = 0
    _save_state(st)


def one_pass(force_login=False):
    tok, entry = asyncio.run(keeper_pass(force_login))
    if not tok:
        print("pass: NEEDS_SIGNIN (flow did not complete)", flush=True)
        return 2
    if seconds_left(tok) <= REFRESH_THRESHOLD:
        # Never deliver a stale token as if it were a harvest: that
        # was the loop that kept an expired JWT in circulation.
        print(f"pass: refresh failed; token still stale "
              f"({seconds_left(tok)}s left)", flush=True)
        return 1
    left = deliver(tok, entry)
    return 0 if left and left > REFRESH_THRESHOLD else 1


def main():
    if "--loop" in sys.argv:
        # Singleton: hold a localhost port for the loop's lifetime so
        # an autostart entry and a manual launch cannot both run.
        import socket
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            lock.bind(("127.0.0.1", 18789))
            lock.listen(1)
        except OSError:
            print("another keeper loop already holds :18789; exiting",
                  flush=True)
            return 0
        interval = 240
        st = _load_state()
        while not os.path.exists(STOP):
            try:
                rc = one_pass()
                handle_result(st, rc)
            except Exception as e:  # noqa: BLE001
                print("keeper error:", repr(e), flush=True)
                handle_result(st, None, err=repr(e)[:120])
            for _ in range(interval // 10):
                if os.path.exists(STOP):
                    break
                time.sleep(10)
        print("keeper loop stopped", flush=True)
        return 0
    return one_pass(force_login="--force-login" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
