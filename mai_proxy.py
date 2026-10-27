#!/usr/bin/env python3
"""
mai_proxy.py -- OpenAI/Anthropic-compatible proxy for the MAI Playground
(playground.microsoft.ai, model mai-thinking-1-latest).

Upstream facts this is built on (all live-verified 2026-10-26):
  * Auth: Authorization: Bearer <Easy Auth id_token> (from /.auth/me).
    No cookie needed. Token lifetime ~65 min, /.auth/refresh -> 403,
    so the token must be re-harvested from a logged-in browser page and
    dropped into the token file (see /__harvest below).
  * Flow: POST /api/trpc conversations.create {title, modelId}
          POST /api/trpc conversations.addPromptAndInitResponse
               {conversationId, text, modelId, lastMessageId (null ok)}
          POST /api/chat/stream {conversationId, modelId,
               pendingModelMessageId, isFirstMessage?, systemPrompt?,
               temperature?, topP?, maxTokens?, enableWebSearch?,
               responseFormat?, userLocation:{timezone}}
          -> SSE of OpenAI chat.completion.chunk objects, [DONE] at end.
  * systemPrompt server cap: 8000 chars. Native tools are IGNORED by the
    upstream, so tools are emulated by prompt protocol (see below).
  * Upstream 429s above roughly 10 streams/min: this proxy paces calls.

Tool emulation protocol: when the client sends tools, the proxy appends
instructions asking the model to emit
  <mai_tool_call>{"name": "...", "input": {...}}</mai_tool_call>
and parses that block out of the reply into a real tool_calls/tool_use
response. This is emulation, not native tool calling.

Conversation bridging: Claude-style clients resend full history every
request. The proxy keeps sessions keyed by a hash of the system text +
first user text, and by exact turn-prefix matching sends only the new
user turn upstream (native multi-turn). On any mismatch it falls back
to a fresh conversation with the whole transcript stuffed into one
user message.

Token harvest: while a logged-in Playground tab is open, running this in
its DevTools console pushes a fresh token to the proxy:
  fetch('/.auth/me').then(r=>r.json()).then(j=>fetch(
    'http://127.0.0.1:18787/__harvest',{method:'POST',body:j[0].id_token}))
(The browser asks for local-network permission once per origin.)

Stdlib only. Binds 127.0.0.1 only.
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://playground.microsoft.ai"
MODEL = "mai-thinking-1-latest"
__version__ = "1.0.0"
PORT = int(os.environ.get("MAI_PROXY_PORT", "18787"))
_HERE = os.path.dirname(os.path.abspath(__file__))


def _state_dir():
    """Runtime state (token, sessions, logs, keys) lives OUTSIDE the
    source tree: %LOCALAPPDATA%\\MAI2API by default, or the directory
    named by MAI2API_STATE_DIR. Credentials and the source tree stay
    physically separate, so zipping or copying the repo can never
    carry a live token with it."""
    env = os.environ.get("MAI2API_STATE_DIR")
    if env:
        return env
    lad = os.environ.get("LOCALAPPDATA")
    if lad:
        return os.path.join(lad, "MAI2API")
    return _HERE


STATE_DIR = _state_dir()
TOKEN_FILE = os.environ.get("MAI_TOKEN_FILE",
                            os.path.join(STATE_DIR, "token.txt"))
REQUESTS_LOG = os.path.join(STATE_DIR, "requests.log")
HARVEST_KEY_FILE = os.path.join(STATE_DIR, "harvest_key.txt")
PACE_SECONDS = float(os.environ.get("MAI_PROXY_PACE", "1.2"))

_state_lock = threading.Lock()
_last_call = [0.0]
_sessions = {}  # key -> {"cid","last","turns":[(role, text), ...]}

# --- hardening knobs (all optional, env-overridable) ---
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")
MAX_STREAMS = int(os.environ.get("MAI_PROXY_MAX_STREAMS", "3"))
_stream_sem = threading.Semaphore(MAX_STREAMS)
MAX_INFLIGHT = int(os.environ.get("MAI_PROXY_MAX_INFLIGHT", "8"))
_inflight = threading.BoundedSemaphore(MAX_INFLIGHT)


class _ClientGone(Exception):
    """The client disconnected mid-response. Unwinds quietly: a
    cancelled request is normal operation, not a proxy error."""
STUFF_CHAR_CAP = int(os.environ.get("MAI_PROXY_STUFF_CAP", "280000"))
TOKEN_WAIT_SECONDS = int(os.environ.get("MAI_PROXY_TOKEN_WAIT", "90"))


_log_check_at = [0.0]


def _maybe_rotate_requests_log():
    """Size-cap requests.log (checked at most hourly; the file is
    opened per request, so rotation between writes is race-light).
    The guardian also rotates it whenever it starts the proxy."""
    now = time.time()
    if now - _log_check_at[0] < 3600:
        return
    _log_check_at[0] = now
    try:
        if os.path.getsize(REQUESTS_LOG) > 5 * 1024 * 1024:
            for src, dst in ((REQUESTS_LOG + ".1", REQUESTS_LOG + ".2"),
                             (REQUESTS_LOG, REQUESTS_LOG + ".1")):
                if os.path.exists(src):
                    os.replace(src, dst)
    except OSError:
        pass


MODEL_STRICT = os.environ.get("MAI_PROXY_MODEL_STRICT") == "1"
# Alias policy: by default any client-supplied model name is accepted
# as an alias for the single upstream model (clients such as Claude
# Code pin their own catalogue names), and every response reports
# the REAL upstream model id. With MAI_PROXY_MODEL_STRICT=1, names
# outside the known alias families are rejected with a 400 instead
# of being silently mapped.
MODEL_ALIAS_HINTS = ("mai-thinking-1", "sonnet", "opus", "haiku",
                     "claude", "gpt-", "o1", "o3", "o4")


def _model_rejected(name):
    if not MODEL_STRICT or not name:
        return False
    n = str(name).lower()
    return not any(h in n for h in MODEL_ALIAS_HINTS)


def _harvest_key():
    try:
        with open(HARVEST_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _ensure_harvest_key():
    """Create the local harvest secret on first run. /__harvest is
    localhost-only, but any same-user process could otherwise inject
    a bogus token; the keeper and harvester present this key."""
    k = _harvest_key()
    if not k:
        import secrets
        k = secrets.token_hex(32)
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = HARVEST_KEY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(k)
        os.replace(tmp, HARVEST_KEY_FILE)
    return k


def get_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def token_info():
    """Expiry info from the stored JWT payload (not signature-verified;
    only used for status display and renewal timing)."""
    tok = get_token()
    if not tok:
        return {"loaded": False, "valid": False}
    try:
        import base64
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        left = int(exp - time.time()) if exp else None
        return {"loaded": True, "expires_at": exp, "seconds_left": left,
                "valid": bool(left and left > 30)}
    except Exception:  # noqa: BLE001
        return {"loaded": True, "valid": False}


_last_harvest_spawn = [0.0]


def _maybe_spawn_harvest():
    """If a dedicated Edge profile has been set up next to this proxy
    (edge-profile/ + headless_harvest.py), launch an on-demand harvest
    so a stale token can heal with no browser open. The harvester uses
    the resident-browser pattern: a RESIDENT headless Edge (no window at
    all) driven over CDP -- the old minimized-GUI-window harvester got
    its renderer throttled/discarded and grew popups needing a human.
    Purely file-presence triggered — nothing user-specific.
    10-minute cooldown."""
    now = time.time()
    if now - _last_harvest_spawn[0] < 600:
        return
    script = os.path.join(_HERE, "headless_harvest.py")
    if not (os.path.exists(script)
            and os.path.isdir(os.path.join(STATE_DIR, "edge-profile"))):
        return
    _last_harvest_spawn[0] = now
    try:
        DETACHED = 0x00000008 | 0x00000200
        subprocess.Popen([sys.executable, script], cwd=_HERE,
                         creationflags=DETACHED, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[proxy] stale token: launched on-demand harvest", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] harvest launch failed: {e!r}", flush=True)


def wait_for_fresh_token(old_value=None, timeout=TOKEN_WAIT_SECONDS):
    """Wait until a usable token (>10 min left) is stored; when
    old_value is given, it must also differ from that failed token.
    Returns True when a fresh token is in place. This turns the brief
    window around token expiry into latency instead of an error:
    the harvester (or an on-demand harvest) usually lands within it."""
    _maybe_spawn_harvest()
    deadline = time.time() + timeout
    while time.time() < deadline:
        tok = get_token()
        left = token_info().get("seconds_left") or 0
        if tok and left > 600 and (old_value is None or tok != old_value):
            return True
        time.sleep(3)
    return False


def _save_sessions():
    """Persist conversation bridges so a proxy restart does not force
    every live session back onto the full-transcript path."""
    try:
        items = list(_sessions.items())[-24:]
        data = {k: {"cid": v["cid"], "last": v["last"],
                    "turns": [list(t) for t in v["turns"]]}
                for k, v in items}
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, SESSIONS_FILE)
    except Exception:  # noqa: BLE001
        pass


def _load_sessions():
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            _sessions[k] = {"cid": v["cid"], "last": v["last"],
                            "turns": [tuple(t) for t in v["turns"]]}
        print(f"[proxy] restored {len(_sessions)} session bridge(s)",
              flush=True)
    except Exception:  # noqa: BLE001
        pass


def pace():
    with _state_lock:
        now = time.time()
        wait = _last_call[0] + PACE_SECONDS - now
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


_metrics = {"requests_total": 0, "by_route": {}, "by_status": {},
            "upstream_429_retries": 0, "upstream_errors": 0,
            "auth_waits": 0, "auth_unavailable": 0,
            "client_cancellations": 0, "tool_repairs": 0,
            "tool_calls_rejected": 0,
            "started_at": int(time.time())}
_mlock = threading.Lock()


def _metric(key, n=1):
    with _mlock:
        _metrics[key] = _metrics.get(key, 0) + n


def _metric_map(kind, name):
    with _mlock:
        _metrics[kind][name] = _metrics[kind].get(name, 0) + 1


def log_event(event, **fields):
    """One structured JSON line per notable event (request
    completions, auth waits, rejected tool calls). Older free-text
    prints remain for the rest; new events land here."""
    rec = {"ts": int(time.time()), "event": event}
    rec.update(fields)
    print("[proxy] " + json.dumps(rec, ensure_ascii=False), flush=True)


def _retry_after_seconds(headers):
    """Parse a Retry-After header (delta-seconds or HTTP-date)."""
    try:
        v = headers.get("Retry-After")
    except Exception:  # noqa: BLE001
        return None
    if not v:
        return None
    v = v.strip()
    if v.isdigit():
        return int(v)
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(v)
        if dt is not None:
            return max(0, int(dt.timestamp() - time.time()))
    except Exception:  # noqa: BLE001
        pass
    return None


def up(method, path, payload=None, stream=False, idempotent=False):
    pace()
    data = json.dumps(payload).encode() if payload is not None else None
    # The Playground rate limit is account-wide with a fast-refilling
    # bucket (measured 2026-10-26: bursts of ~25 streams trip it, a
    # retry ~20 s later succeeds). Absorb 429s here with backoff so
    # agent-style bursts surface as latency, not errors. 429 means the
    # request was rejected before processing, so retrying is safe for
    # every endpoint. 5xx retries are restricted to idempotent calls
    # (conversation creation, reads): for prompt/stream calls the
    # execution state after a 5xx is unknown, and those recover one
    # level up by dropping the session bridge instead.
    backoff = [5, 10, 20, 30, 45]
    transient = [3, 8]
    attempt429 = 0
    attempt5xx = 0
    while True:
        req = urllib.request.Request(UPSTREAM + path, method=method, data=data)
        req.add_header("Authorization", "Bearer " + get_token())
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            return urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:400]
            if e.code == 429 and attempt429 < len(backoff):
                _metric("upstream_429_retries")
                wait = backoff[attempt429]
                ra = _retry_after_seconds(e.headers)
                if ra is not None:
                    wait = min(max(wait, ra), 120)
                print(f"[proxy] 429 on {path}; retry {attempt429 + 1} "
                      f"in {wait}s", flush=True)
                time.sleep(wait)
                attempt429 += 1
                continue
            if (idempotent and e.code in (502, 503, 504)
                    and attempt5xx < len(transient)):
                print(f"[proxy] {e.code} on {path}; retry "
                      f"{attempt5xx + 1} in {transient[attempt5xx]}s",
                      flush=True)
                time.sleep(transient[attempt5xx])
                attempt5xx += 1
                continue
            raise UpstreamError(e.code, body)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if idempotent and attempt5xx < len(transient):
                print(f"[proxy] network error on {path} ({e!r}); retry "
                      f"{attempt5xx + 1} in {transient[attempt5xx]}s",
                      flush=True)
                time.sleep(transient[attempt5xx])
                attempt5xx += 1
                continue
            raise UpstreamError(0, repr(e))


class UpstreamError(Exception):
    def __init__(self, status, body):
        super().__init__(f"upstream {status}: {body}")
        self.status = status
        self.body = body


class AuthUnavailable(Exception):
    """No usable Playground token and the bounded refresh wait did
    not land one. Mapped to a 503 so a dead session fails fast and
    machine-readably instead of hanging on a doomed upstream call."""


def trpc(name, payload, idempotent=False):
    with up("POST", "/api/trpc/" + name, payload,
            idempotent=idempotent) as r:
        return json.loads(r.read().decode())["result"]["data"]


def stream_upstream(cid, mid, extra):
    """Generator of assistant text deltas from /api/chat/stream."""
    body = {
        "conversationId": cid,
        "modelId": MODEL,
        "pendingModelMessageId": mid,
        "userLocation": {"timezone": "Asia/Tokyo"},
    }
    if extra:
        body.update(extra)
    with up("POST", "/api/chat/stream", body) as r:
        buf = ""
        for raw in r:
            buf += raw.decode(errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    j = json.loads(payload)
                except ValueError:
                    continue
                choices = j.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        yield delta["content"]


def run_turn(cid, last_mid, text, extra):
    """One full user turn; returns (assistant_text, model_message_id)."""
    sink = {}
    for _ in run_turn_iter(cid, last_mid, text, extra, sink):
        pass
    return sink["text"], sink["mid"]


def run_turn_iter(cid, last_mid, text, extra, sink):
    """Streaming variant of run_turn: yields assistant text deltas as
    the upstream produces them; on completion sink holds the full
    text and the model message id (for session-bridge bookkeeping)."""
    data = trpc("conversations.addPromptAndInitResponse", {
        "conversationId": cid, "text": text, "modelId": MODEL,
        "lastMessageId": last_mid})
    mid = data["modelMessage"]["id"]
    parts = []
    for delta in stream_upstream(cid, mid, extra):
        parts.append(delta)
        yield delta
    sink["text"] = "".join(parts)
    sink["mid"] = mid


TOOL_TAG = "<mai_tool_call>"


class ToolBlockFilter:
    """Incremental splitter for streamed replies. Text that is
    certainly reply text is emitted immediately; a trailing suffix
    that could still grow into a <mai_tool_call> block (or the block
    itself and anything after it) is held back for the final parse,
    so a tool call is never half-shown as prose."""

    def __init__(self):
        self.buf = ""
        self.emitted = ""

    def feed(self, delta):
        self.buf += delta
        idx = self.buf.find(TOOL_TAG)
        if idx >= 0:
            safe = self.buf[:idx]
            self.buf = self.buf[idx:]
            self.emitted += safe
            return safe
        hold = 0
        for k in range(min(len(self.buf), len(TOOL_TAG) - 1), 0, -1):
            if self.buf.endswith(TOOL_TAG[:k]):
                hold = k
                break
        safe = self.buf[:len(self.buf) - hold] if hold else self.buf
        self.buf = self.buf[len(safe):]
        self.emitted += safe
        return safe

    @property
    def full_text(self):
        return self.emitted + self.buf


TOOL_BLOCK_RE = re.compile(r"<mai_tool_call>\s*(\{.*?\})\s*</mai_tool_call>", re.S)

TOOL_INSTRUCTIONS = """

--- TOOL USE ---
You have access to tools. To call a tool, end your reply with a block in
exactly this format (valid JSON, no markdown fences):
<mai_tool_call>{"name": "TOOL_NAME", "input": { ...arguments... }}</mai_tool_call>
Available tools:
{tool_list}
Call at most one tool per reply. If no tool is needed, answer normally
in plain text and do not emit the block.
"""


def format_tools_openai(tools):
    lines = []
    for t in tools or []:
        fn = t.get("function") or t
        name = fn.get("name", "?")
        desc = fn.get("description", "")
        params = fn.get("parameters") or fn.get("input_schema") or {}
        lines.append(f"- {name}: {desc} params={json.dumps(params)}")
    return "\n".join(lines)


def parse_tool_call(text):
    m = TOOL_BLOCK_RE.search(text)
    if not m:
        return text, None
    try:
        call = json.loads(m.group(1))
    except ValueError:
        return text, None
    before = text[:m.start()].strip()
    return before, call


def _schema_errors(schema, value, path="$"):
    """A small JSON-Schema subset validator (types, object
    properties/required, array items, enum) -- enough to catch the
    argument shapes a model actually gets wrong, with no deps."""
    errs = []
    if not isinstance(schema, dict):
        return errs
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        errs.append(f"{path}: {value!r} is not one of {enum}")
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return errs + [f"{path}: expected object"]
        for req in schema.get("required") or []:
            if req not in value:
                errs.append(f"{path}.{req}: missing required property")
        props = schema.get("properties") or {}
        for k, sub in props.items():
            if k in value:
                errs.extend(_schema_errors(sub, value[k], f"{path}.{k}"))
    elif t == "string":
        if not isinstance(value, str):
            errs.append(f"{path}: expected string")
    elif t in ("number", "integer"):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errs.append(f"{path}: expected {t}")
        elif t == "integer" and float(value) != int(value):
            errs.append(f"{path}: expected integer")
    elif t == "boolean":
        if not isinstance(value, bool):
            errs.append(f"{path}: expected boolean")
    elif t == "array":
        if not isinstance(value, list):
            errs.append(f"{path}: expected array")
        elif isinstance(schema.get("items"), dict):
            for i, item in enumerate(value):
                errs.extend(_schema_errors(schema["items"], item,
                                           f"{path}[{i}]"))
    return errs


def _validate_tool_call(call, tools):
    """None when the call is acceptable, else a human-readable
    rejection reason. With no declared tools there is nothing to
    validate against, so calls pass (documented behaviour)."""
    if not call:
        return None
    declared = {}
    for t in tools or []:
        fn = t.get("function") or t
        if fn.get("name"):
            declared[fn["name"]] = fn.get("parameters") or \
                fn.get("input_schema") or {}
    if not declared:
        return None
    name = call.get("name")
    if name not in declared:
        return f"undeclared tool {name!r} (declared: {sorted(declared)})"
    args = call.get("input")
    if not isinstance(args, dict):
        return f"arguments for {name} must be a JSON object"
    errs = _schema_errors(declared[name], args)
    if errs:
        return f"invalid arguments for {name}: " + "; ".join(errs[:4])
    return None


def _repair_call(system_text, turns, tools, extra, bad_text, reason):
    """One repair attempt after a rejected tool call: feed the
    rejection back and let the model emit a valid call or a plain
    answer. Returns (before, call) of the repaired reply, or None
    when the repair itself failed upstream."""
    feedback = ("[proxy] Your tool call was rejected: " + reason +
                ". Reply with exactly one valid <mai_tool_call> block "
                "for a declared tool whose arguments match its schema, "
                "or answer in plain text without a tool call.")
    try:
        text = complete(system_text,
                        list(turns) + [("assistant", bad_text),
                                       ("user", feedback)],
                        tools, extra)
    except Exception:  # noqa: BLE001
        return None
    return parse_tool_call(text)


def _est_tokens(text):
    return max(1, len(text) // 4) if text else 0


def _estimate_input(system_text, turns, tools):
    total = len(system_text or "")
    for _, t in turns:
        total += len(t or "")
    if tools:
        total += len(json.dumps(tools))
    return _est_tokens("x" * total)


def turns_from_openai(messages):
    system_parts, turns = [], []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict))
        content = content or ""
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            turns.append(("user", content))
        elif role == "assistant":
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function") or {}
                    turns.append(("assistant_tool",
                                  f'<mai_tool_call>{{"name": "{fn.get("name")}", '
                                  f'"input": {fn.get("arguments") or "{}"}}}</mai_tool_call>'))
            if content:
                turns.append(("assistant", content))
        elif role == "tool":
            turns.append(("tool_result", content))
    return "\n\n".join(system_parts), turns


def turns_from_anthropic(system, messages):
    system_text = system if isinstance(system, str) else ""
    if isinstance(system, list):
        system_text = "".join(b.get("text", "") for b in system
                              if isinstance(b, dict))
    turns = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            turns.append(("user" if role == "user" else "assistant", content))
            continue
        for block in content or []:
            btype = block.get("type")
            if btype == "text":
                turns.append(("user" if role == "user" else "assistant",
                              block.get("text", "")))
            elif btype == "tool_use":
                turns.append(("assistant_tool",
                              f'<mai_tool_call>{{"name": "{block.get("name")}", '
                              f'"input": {json.dumps(block.get("input") or {})}}}</mai_tool_call>'))
            elif btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    inner = "".join(b.get("text", "") for b in inner
                                    if isinstance(b, dict))
                turns.append(("tool_result", inner or ""))
    return system_text, turns


def render_turn_text(role, text):
    if role == "tool_result":
        return f"[Tool result]\n{text}"
    return text


def session_key(system_text, turns, tools_block):
    first_user = next((t for r, t in turns if r == "user"), "")
    h = hashlib.sha256()
    h.update(system_text.encode())
    h.update(b"\x00")
    h.update(first_user.encode())
    h.update(b"\x00")
    h.update(tools_block.encode())
    return h.hexdigest()[:24]


def flatten_for_transcript(system_text, turns, tools_block):
    parts = []
    header = system_text
    if tools_block:
        header = (header + TOOL_INSTRUCTIONS.replace(
            "{tool_list}", tools_block)).strip()
    if header:
        parts.append(header)
    for role, text in turns[:-1]:
        label = {"user": "User", "assistant": "Assistant",
                 "assistant_tool": "Assistant", "tool_result": "Tool result"}[role]
        parts.append(f"{label}: {text}")
    last_role, last_text = turns[-1]
    parts.append(f"User: {render_turn_text(last_role, last_text)}")
    parts.append("\nRespond as the Assistant's next message only.")
    text = "\n\n".join(parts)
    if len(text) > STUFF_CHAR_CAP:
        # Live probes verified 300k-char user messages are accepted;
        # stay under that with margin. Drop the OLDEST history turns —
        # the system header and the newest turns always survive — so a
        # very long session degrades gracefully instead of hitting an
        # unknown upstream wall mid-run.
        head = parts[:1] if header else []
        tail = parts[-2:]
        middle = parts[len(head):-2]
        dropped = 0
        while middle and len("\n\n".join(head + middle + tail)) > STUFF_CHAR_CAP:
            middle.pop(0)
            dropped += 1
        if dropped:
            middle.insert(0, f"[{dropped} earlier turn(s) omitted by the "
                             "proxy to fit the upstream message limit]")
            text = "\n\n".join(head + middle + tail)
            print(f"[proxy] transcript trimmed: dropped {dropped} turn(s), "
                  f"{len(text)} chars", flush=True)
    return text


def complete(system_text, turns, tools, extra):
    """Run one completion; returns assistant text (may contain a tool block).

    Stability behaviour:
    * a dead/missing token first waits for the harvester to land a
      fresh one (up to TOKEN_WAIT_SECONDS) instead of failing at once;
    * an upstream 401/403 waits for a DIFFERENT token, then retries
      the whole completion once;
    * concurrent streams are capped (MAI_PROXY_MAX_STREAMS) so
      parallel agent fan-out queues here instead of colliding with
      the account-wide rate limit upstream.
    """
    if not turns:
        raise ValueError("no messages")
    info = token_info()
    if not info.get("valid"):
        # Missing, expired or nearly expired token: wait ONE bounded
        # window for the keeper / an on-demand harvest to land a
        # fresh token. If none lands, fail fast with a 503 instead
        # of feeding a dead token to the upstream and hanging.
        _metric("auth_waits")
        if not wait_for_fresh_token():
            _metric("auth_unavailable")
            raise AuthUnavailable(
                "Playground session token could not be refreshed")
    tok_before = get_token()
    with _stream_sem:
        try:
            return _complete_inner(system_text, turns, tools, extra)
        except UpstreamError as e:
            if e.status not in (401, 403):
                raise
            print("[proxy] upstream auth error; waiting for a fresh "
                  "token, then retrying once", flush=True)
            if not wait_for_fresh_token(old_value=tok_before, timeout=90):
                raise
            return _complete_inner(system_text, turns, tools, extra)


def complete_iter(system_text, turns, tools, extra):
    """Streaming variant of complete(): yields reply-text deltas as
    they arrive. Token gating matches complete(); the single
    auth retry can only substitute a fresh stream before the first
    delta has been yielded (after that the client already has bytes
    and a restart would corrupt the response)."""
    if not turns:
        raise ValueError("no messages")
    info = token_info()
    if not info.get("valid"):
        _metric("auth_waits")
        if not wait_for_fresh_token():
            _metric("auth_unavailable")
            raise AuthUnavailable(
                "Playground session token could not be refreshed")
    tok_before = get_token()
    with _stream_sem:
        yielded = False
        retried = False
        while True:
            try:
                for d in _complete_inner_iter(system_text, turns,
                                              tools, extra):
                    yielded = True
                    yield d
                return
            except UpstreamError as e:
                if e.status in (401, 403) and not yielded and not retried:
                    retried = True
                    print("[proxy] upstream auth error before first "
                          "delta; waiting for a fresh token, then "
                          "retrying once", flush=True)
                    if not wait_for_fresh_token(old_value=tok_before,
                                                timeout=90):
                        raise
                    continue
                raise


def _complete_inner_iter(system_text, turns, tools, extra):
    """Yielding twin of _complete_inner (same session-bridge rules)."""
    tools_block = format_tools_openai(tools) if tools else ""
    key = session_key(system_text, turns, tools_block)
    sess = _sessions.get(key)
    new_text = None
    if sess:
        known = sess["turns"]
        if len(turns) > len(known) and turns[:len(known)] == known:
            delta = turns[len(known):]
            user_delta = [t for t in delta if t[0] in ("user", "tool_result")]
            asst_known = [t for t in delta if t[0] in ("assistant", "assistant_tool")]
            if len(user_delta) == 1 and not asst_known:
                new_text = render_turn_text(*user_delta[0])
    if sess and new_text is not None:
        sink = {}
        try:
            for d in run_turn_iter(sess["cid"], sess["last"],
                                   new_text, extra, sink):
                yield d
        except (UpstreamError, GeneratorExit):
            # Same rule as the buffered path (plus client disconnect):
            # the upstream conversation may now hold an unanswered
            # user turn, so drop the bridge and let the next attempt
            # restuff into a fresh conversation.
            print("[proxy] delta turn failed/closed; dropping session "
                  "bridge", flush=True)
            _sessions.pop(key, None)
            _save_sessions()
            raise
        sess["turns"] = turns + [("assistant", sink["text"])]
        sess["last"] = sink["mid"]
        _save_sessions()
        return
    data = trpc("conversations.create", {"title": "mai-proxy", "modelId": MODEL},
                idempotent=True)
    cid = data["id"]
    first_extra = dict(extra or {})
    body_text = flatten_for_transcript(system_text, turns, tools_block)
    turn_extra = dict(first_extra)
    turn_extra["isFirstMessage"] = True
    sink = {}
    for d in run_turn_iter(cid, None, body_text, turn_extra, sink):
        yield d
    _sessions[key] = {"cid": cid, "last": sink["mid"],
                      "turns": turns + [("assistant", sink["text"])]}
    _save_sessions()


def _complete_inner(system_text, turns, tools, extra):
    tools_block = format_tools_openai(tools) if tools else ""
    key = session_key(system_text, turns, tools_block)
    sess = _sessions.get(key)
    new_text = None
    if sess:
        known = sess["turns"]
        if len(turns) > len(known) and turns[:len(known)] == known:
            delta = turns[len(known):]
            user_delta = [t for t in delta if t[0] in ("user", "tool_result")]
            asst_known = [t for t in delta if t[0] in ("assistant", "assistant_tool")]
            if len(user_delta) == 1 and not asst_known:
                new_text = render_turn_text(*user_delta[0])
    if sess and new_text is not None:
        try:
            text_out, mid = run_turn(sess["cid"], sess["last"], new_text, extra)
        except UpstreamError:
            # The upstream conversation may now hold a user turn with
            # no reply (or a stale message chain). Drop the bridge so
            # the next attempt restuffs into a fresh conversation
            # instead of compounding the mismatch.
            print("[proxy] delta turn failed; dropping session bridge",
                  flush=True)
            _sessions.pop(key, None)
            _save_sessions()
            raise
        sess["turns"] = turns + [("assistant", text_out)]
        sess["last"] = mid
        _save_sessions()
        return text_out
    # fallback: fresh conversation, transcript stuffed into one message
    data = trpc("conversations.create", {"title": "mai-proxy", "modelId": MODEL},
                idempotent=True)
    cid = data["id"]
    first_extra = dict(extra or {})
    # NOTE: the upstream systemPrompt field proved unreliable in live
    # probes (instructions ignored), so the system text is ALWAYS stuffed
    # into the first user message as transcript header, where the model
    # demonstrably follows it.
    body_text = flatten_for_transcript(system_text, turns, tools_block)
    turn_extra = dict(first_extra)
    turn_extra["isFirstMessage"] = True
    text_out, mid = run_turn(cid, None, body_text, turn_extra)
    _sessions[key] = {"cid": cid, "last": mid,
                      "turns": turns + [("assistant", text_out)]}
    _save_sessions()
    return text_out


def anthropic_tools(tools):
    out = []
    for t in tools or []:
        out.append({"type": "function",
                    "function": {"name": t.get("name"),
                                 "description": t.get("description", ""),
                                 "parameters": t.get("input_schema") or {}}})
    return out


def turns_from_responses(body):
    """Convert a Responses-API request into (system_text, turns,
    tools). previous_response_id is deliberately unsupported: this
    proxy is stateless per request, so the caller sends full input."""
    system_parts = []
    if body.get("instructions"):
        system_parts.append(str(body["instructions"]))
    turns = []
    inp = body.get("input")
    items = []
    if isinstance(inp, str):
        items = [{"type": "message", "role": "user", "content": inp}]
    elif isinstance(inp, list):
        items = inp
    for item in items:
        if not isinstance(item, dict):
            continue
        t = item.get("type", "message")
        if t == "message":
            role = item.get("role", "user")
            content = item.get("content")
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict)
                    and p.get("type") in ("input_text", "output_text",
                                          "text"))
            content = content or ""
            if role in ("system", "developer"):
                system_parts.append(content)
            elif role == "assistant":
                if content:
                    turns.append(("assistant", content))
            else:
                turns.append(("user", content))
        elif t == "function_call":
            args = item.get("arguments") or "{}"
            turns.append(("assistant_tool",
                          f'<mai_tool_call>{{"name": '
                          f'{json.dumps(item.get("name"))}, '
                          f'"input": {args}}}</mai_tool_call>'))
        elif t == "function_call_output":
            out = item.get("output")
            turns.append(("tool_result",
                          out if isinstance(out, str)
                          else json.dumps(out or "")))
    tools = []
    for t in body.get("tools") or []:
        if t.get("type") == "function" and t.get("name"):
            tools.append({"type": "function", "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("parameters") or {}}})
    return "\n\n".join(system_parts), turns, tools


def _responses_object(resp_id, output, in_est, out_est, status="completed"):
    return {"id": resp_id, "object": "response",
            "created_at": int(time.time()), "model": MODEL,
            "status": status, "output": output,
            "usage": {"input_tokens": in_est, "output_tokens": out_est,
                      "total_tokens": in_est + out_est,
                      "estimated": True},
            "parallel_tool_calls": True, "tool_choice": "auto",
            "temperature": None, "metadata": {}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        try:
            with open(REQUESTS_LOG, "a", encoding="utf-8") as f:
                f.write(fmt % args + "\n")
            _maybe_rotate_requests_log()
        except OSError:
            pass

    def _send(self, status, obj, ctype="application/json"):
        body = obj if isinstance(obj, (bytes, str)) else json.dumps(obj)
        if isinstance(body, str):
            body = body.encode()
        if not getattr(self, "_counted", False):
            self._counted = True
            _metric_map("by_status", str(status))
            log_event("request", id=getattr(self, "_req_id", "-"),
                      method=self.command,
                      path=self.path.split("?", 1)[0], status=status,
                      ms=int((time.time() - getattr(self, "_t0",
                                                    time.time())) * 1000))
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            raise _ClientGone()

    def _sse_open(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            raise _ClientGone()

    def _sse_write(self, text):
        try:
            self.wfile.write(text.encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise _ClientGone()

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._req_id = uuid.uuid4().hex[:12]
        self._t0 = time.time()
        _metric("requests_total")
        _metric_map("by_route", "GET " + self.path.split("?", 1)[0])
        if not _inflight.acquire(blocking=False):
            self._send(503, {"error": {"message": "proxy overloaded",
                                       "type": "proxy_overloaded"}})
            return
        try:
            self._do_GET()
        except _ClientGone:
            _metric("client_cancellations")
        finally:
            _inflight.release()

    def _do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/health/live", "/health/ready"):
            info = token_info()
            summary = {"ok": bool(info.get("valid")),
                       "process": "ok", "version": __version__,
                       "token": info}
            if path == "/health/live":
                self._send(200, {"ok": True, "process": "ok"})
            elif path == "/health/ready":
                self._send(200 if info.get("valid") else 503, summary)
            else:
                self._send(200, summary)
        elif path == "/v1/models":
            self._send(200, {"object": "list", "data": [
                {"id": MODEL, "object": "model", "owned_by": "microsoft-mai"}]})
        elif path == "/metrics":
            with _mlock:
                snap = json.loads(json.dumps(_metrics))
            snap["uptime_seconds"] = int(time.time()) - snap["started_at"]
            snap["token_valid"] = bool(token_info().get("valid"))
            self._send(200, snap)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        self._req_id = uuid.uuid4().hex[:12]
        self._t0 = time.time()
        _metric("requests_total")
        _metric_map("by_route", "POST " + self.path.split("?", 1)[0])
        if not _inflight.acquire(blocking=False):
            self._send(503, {"error": {"message": "proxy overloaded",
                                       "type": "proxy_overloaded"}})
            return
        try:
            self._do_POST()
        finally:
            _inflight.release()

    def _do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/__harvest":
                sent = self.headers.get("X-MAI-Harvest-Key", "")
                if not hmac.compare_digest(sent, _harvest_key()):
                    self._send(403, {"error": "bad or missing "
                                             "harvest key"})
                    return
                n = int(self.headers.get("Content-Length", 0))
                tok = self.rfile.read(n).decode().strip()
                if tok.count(".") != 2 or len(tok) < 100:
                    self._send(400, {"error": "does not look like a JWT"})
                    return
                os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
                tmp = TOKEN_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(tok)
                os.replace(tmp, TOKEN_FILE)  # atomic: readers never
                # see a half-written token
                self._send(200, {"ok": True, "token_len": len(tok)})
                return
            body = self._read_json()
            if path in ("/v1/chat/completions", "/v1/messages",
                        "/v1/responses") and \
                    _model_rejected(body.get("model")):
                self._send(400, {"error": {
                    "message": f"model {body.get('model')!r} is not "
                               f"served by this proxy (upstream: "
                               f"{MODEL}); see the model alias policy",
                    "type": "model_not_supported"}})
                return
            if path == "/v1/chat/completions":
                self._openai(body)
            elif path == "/v1/messages":
                self._anthropic(body)
            elif path == "/v1/responses":
                self._responses(body)
            elif path == "/v1/messages/count_tokens":
                approx = len(json.dumps(body.get("messages") or [])) // 4
                self._send(200, {"input_tokens": approx})
            else:
                self._send(404, {"error": "not found"})
        except _ClientGone:
            _metric("client_cancellations")
            print("[proxy] client disconnected; request unwound",
                  flush=True)
        except AuthUnavailable as e:
            self._send(503, {"error": {"message": str(e),
                                       "type": "mai_auth_unavailable"}})
        except UpstreamError as e:
            self._send(502, {"error": {"message":
                f"upstream {e.status}: {e.body}. If 401/403, the Playground "
                "token has expired; re-harvest it (see mai_proxy.py header).",
                "type": "upstream_error"}})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": {"message": repr(e), "type": "proxy_error"}})

    def _extra(self, body):
        extra = {}
        for k in ("temperature", "topP", "maxTokens"):
            if body.get(k) is not None:
                extra[k] = body[k]
        if body.get("max_tokens") is not None:
            extra["maxTokens"] = body["max_tokens"]
        return extra

    def _prime_stream(self, gen, filt):
        """Pull deltas until the first safe text chunk exists (or the
        stream ends). Errors raised here happen BEFORE any SSE bytes
        are written, so they still surface as normal JSON errors."""
        pending = []
        exhausted = False
        try:
            while True:
                d = next(gen)
                safe = filt.feed(d)
                if safe:
                    pending.append(safe)
                    break
        except StopIteration:
            exhausted = True
        return pending, exhausted

    def _openai_stream(self, body, system_text, turns, extra):
        gen = complete_iter(system_text, turns, body.get("tools"), extra)
        filt = ToolBlockFilter()
        pending, exhausted = self._prime_stream(gen, filt)
        cid_ = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        def chunk(delta, finish=None):
            return {"id": cid_, "object": "chat.completion.chunk",
                    "created": created, "model": MODEL,
                    "choices": [{"index": 0, "delta": delta,
                                 "finish_reason": finish}]}

        self._sse_open()
        try:
            self._sse_write("data: " + json.dumps(
                chunk({"role": "assistant"})) + "\n\n")
            for safe in pending:
                self._sse_write("data: " + json.dumps(
                    chunk({"content": safe})) + "\n\n")
            if not exhausted:
                for d in gen:
                    safe = filt.feed(d)
                    if safe:
                        self._sse_write("data: " + json.dumps(
                            chunk({"content": safe})) + "\n\n")
            full = filt.full_text
            before, call = parse_tool_call(full)
            rest = before[len(filt.emitted):] if call \
                else full[len(filt.emitted):]
            if rest:
                self._sse_write("data: " + json.dumps(
                    chunk({"content": rest})) + "\n\n")
            repair_text = ""
            err = _validate_tool_call(call, body.get("tools"))
            if err:
                _metric("tool_calls_rejected")
                log_event("tool_call_rejected", reason=err,
                          tool=(call or {}).get("name"))
                _metric("tool_repairs")
                fixed = _repair_call(system_text, turns,
                                     body.get("tools"), extra, full, err)
                if fixed is not None:
                    b2, c2 = fixed
                    if c2 and not _validate_tool_call(c2,
                                                      body.get("tools")):
                        repair_text, call = b2, c2
                    elif not c2:
                        repair_text, call = b2, None
                    else:
                        call = None
                else:
                    call = None
            if repair_text:
                self._sse_write("data: " + json.dumps(
                    chunk({"content": repair_text})) + "\n\n")
            finish = "stop"
            if call:
                tc = [{"index": 0,
                       "id": "call_" + uuid.uuid4().hex[:24],
                       "type": "function",
                       "function": {
                           "name": call.get("name"),
                           "arguments": json.dumps(call.get("input") or {})}}]
                self._sse_write("data: " + json.dumps(
                    chunk({"tool_calls": tc})) + "\n\n")
                finish = "tool_calls"
            self._sse_write("data: " + json.dumps(chunk({}, finish)) + "\n\n")
            if (body.get("stream_options") or {}).get("include_usage"):
                in_est = _estimate_input(system_text, turns,
                                         body.get("tools"))
                out_est = _est_tokens(full + repair_text)
                usage_chunk = {"id": cid_, "object": "chat.completion.chunk",
                               "created": created, "model": MODEL,
                               "choices": [],
                               "usage": {"prompt_tokens": in_est,
                                         "completion_tokens": out_est,
                                         "total_tokens": in_est + out_est},
                               "usage_estimated": True}
                self._sse_write("data: " + json.dumps(usage_chunk) + "\n\n")
            self._sse_write("data: [DONE]\n\n")
        except _ClientGone:
            gen.close()
            raise
        except UpstreamError as e:
            print(f"[proxy] upstream error mid-stream: {e.status}",
                  flush=True)
            try:
                self._sse_write("data: " + json.dumps(
                    {"error": {"message": f"upstream {e.status}",
                               "type": "upstream_error"}}) + "\n\n")
                self._sse_write("data: [DONE]\n\n")
            except _ClientGone:
                gen.close()
                raise

    def _anthropic_stream(self, body, system_text, turns, tools, extra):
        gen = complete_iter(system_text, turns, tools, extra)
        filt = ToolBlockFilter()
        pending, exhausted = self._prime_stream(gen, filt)
        msg_id = "msg_" + uuid.uuid4().hex[:24]

        def ev(name, data):
            self._sse_write(f"event: {name}\ndata: {json.dumps(data)}\n\n")

        self._sse_open()
        try:
            ev("message_start", {
                "type": "message_start",
                "message": {"id": msg_id, "type": "message",
                            "role": "assistant", "model": MODEL,
                            "content": [], "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": _estimate_input(
                                          system_text, turns, tools),
                                      "output_tokens": 0,
                                      "estimated": True}}})
            idx = 0
            text_open = False

            def text_delta(t):
                nonlocal text_open
                if not text_open:
                    ev("content_block_start", {
                        "type": "content_block_start", "index": idx,
                        "content_block": {"type": "text", "text": ""}})
                    text_open = True
                ev("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": t}})

            for safe in pending:
                text_delta(safe)
            if not exhausted:
                for d in gen:
                    safe = filt.feed(d)
                    if safe:
                        text_delta(safe)
            full = filt.full_text
            before, call = parse_tool_call(full)
            rest = before[len(filt.emitted):] if call \
                else full[len(filt.emitted):]
            if rest:
                text_delta(rest)
            repair_text = ""
            err = _validate_tool_call(call, tools)
            if err:
                _metric("tool_calls_rejected")
                log_event("tool_call_rejected", reason=err,
                          tool=(call or {}).get("name"))
                _metric("tool_repairs")
                fixed = _repair_call(system_text, turns, tools, extra,
                                     full, err)
                if fixed is not None:
                    b2, c2 = fixed
                    if c2 and not _validate_tool_call(c2, tools):
                        repair_text, call = b2, c2
                    elif not c2:
                        repair_text, call = b2, None
                    else:
                        call = None
                else:
                    call = None
            if repair_text:
                text_delta(repair_text)
            if text_open:
                ev("content_block_stop", {
                    "type": "content_block_stop", "index": idx})
                idx += 1
            stop = "end_turn"
            if call:
                tid = "toolu_" + uuid.uuid4().hex[:24]
                ev("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "tool_use", "id": tid,
                                      "name": call.get("name"),
                                      "input": {}}})
                ev("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "input_json_delta",
                              "partial_json": json.dumps(
                                  call.get("input") or {})}})
                ev("content_block_stop", {
                    "type": "content_block_stop", "index": idx})
                stop = "tool_use"
            elif not text_open:
                ev("content_block_start", {
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "text", "text": ""}})
                ev("content_block_stop", {
                    "type": "content_block_stop", "index": idx})
            ev("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": _est_tokens(full + repair_text),
                          "estimated": True}})
            ev("message_stop", {"type": "message_stop"})
        except _ClientGone:
            gen.close()
            raise
        except UpstreamError as e:
            print(f"[proxy] upstream error mid-stream: {e.status}",
                  flush=True)
            try:
                ev("error", {"type": "error", "error": {
                    "type": "upstream_error",
                    "message": f"upstream {e.status}"}})
            except _ClientGone:
                gen.close()
                raise

    def _openai(self, body):
        system_text, turns = turns_from_openai(body.get("messages") or [])
        extra = self._extra(body)
        if body.get("stream"):
            self._openai_stream(body, system_text, turns, extra)
            return
        text = complete(system_text, turns, body.get("tools"), extra)
        before, call = parse_tool_call(text)
        err = _validate_tool_call(call, body.get("tools"))
        if err:
            _metric("tool_calls_rejected")
            log_event("tool_call_rejected", reason=err,
                      tool=(call or {}).get("name"))
            _metric("tool_repairs")
            fixed = _repair_call(system_text, turns, body.get("tools"),
                                 extra, text, err)
            if fixed is not None:
                b2, c2 = fixed
                if c2 and not _validate_tool_call(c2, body.get("tools")):
                    before, call = b2, c2
                elif not c2:
                    before, call = b2, None
                else:
                    call = None  # still invalid: drop the call
            else:
                call = None
        in_est = _estimate_input(system_text, turns, body.get("tools"))
        out_est = _est_tokens(text)
        msg = {"role": "assistant", "content": before or None}
        finish = "stop"
        tool_calls = None
        if call:
            tool_calls = [{
                "id": "call_" + uuid.uuid4().hex[:24],
                "type": "function",
                "function": {"name": call.get("name"),
                             "arguments": json.dumps(call.get("input") or {})}}]
            msg["tool_calls"] = tool_calls
            finish = "tool_calls"
        cid_ = "chatcmpl-" + uuid.uuid4().hex[:24]
        self._send(200, {
            "id": cid_,
            "object": "chat.completion", "created": int(time.time()),
            "model": MODEL,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": in_est,
                      "completion_tokens": out_est,
                      "total_tokens": in_est + out_est},
            "usage_estimated": True})

    def _responses(self, body):
        if body.get("previous_response_id"):
            self._send(400, {"error": {
                "message": "previous_response_id is not supported by "
                           "this proxy; send the full input on every "
                           "request",
                "type": "unsupported_parameter"}})
            return
        system_text, turns, tools = turns_from_responses(body)
        extra = {}
        if body.get("max_output_tokens") is not None:
            extra["maxTokens"] = body["max_output_tokens"]
        if body.get("stream"):
            self._responses_stream(body, system_text, turns, tools, extra)
            return
        text = complete(system_text, turns, tools, extra)
        before, call = parse_tool_call(text)
        err = _validate_tool_call(call, tools)
        if err:
            _metric("tool_calls_rejected")
            log_event("tool_call_rejected", reason=err,
                      tool=(call or {}).get("name"))
            _metric("tool_repairs")
            fixed = _repair_call(system_text, turns, tools, extra,
                                 text, err)
            if fixed is not None:
                b2, c2 = fixed
                if c2 and not _validate_tool_call(c2, tools):
                    before, call = b2, c2
                elif not c2:
                    before, call = b2, None
                else:
                    call = None
            else:
                call = None
        output = []
        resp_id = "resp_" + uuid.uuid4().hex[:24]
        if before or not call:
            output.append({
                "type": "message", "id": "msg_" + uuid.uuid4().hex[:24],
                "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": before,
                             "annotations": []}]})
        if call:
            output.append({
                "type": "function_call",
                "id": "fc_" + uuid.uuid4().hex[:24],
                "call_id": "call_" + uuid.uuid4().hex[:24],
                "name": call.get("name"),
                "arguments": json.dumps(call.get("input") or {}),
                "status": "completed"})
        self._send(200, _responses_object(
            resp_id, output,
            _estimate_input(system_text, turns, tools),
            _est_tokens(text)))

    def _responses_stream(self, body, system_text, turns, tools, extra):
        gen = complete_iter(system_text, turns, tools, extra)
        filt = ToolBlockFilter()
        pending, exhausted = self._prime_stream(gen, filt)
        resp_id = "resp_" + uuid.uuid4().hex[:24]
        msg_id = "msg_" + uuid.uuid4().hex[:24]
        in_est = _estimate_input(system_text, turns, tools)

        def ev(name, data):
            payload = dict(data)
            payload["type"] = name
            self._sse_write(f"event: {name}\n"
                            f"data: {json.dumps(payload)}\n\n")

        self._sse_open()
        try:
            ev("response.created", {"response": _responses_object(
                resp_id, [], in_est, 0, status="in_progress")})
            msg_item = {"type": "message", "id": msg_id,
                        "role": "assistant", "status": "in_progress",
                        "content": []}
            ev("response.output_item.added",
               {"output_index": 0, "item": msg_item})
            ev("response.content_part.added", {
                "item_id": msg_id, "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": "",
                         "annotations": []}})
            for safe in pending:
                ev("response.output_text.delta", {
                    "item_id": msg_id, "output_index": 0,
                    "content_index": 0, "delta": safe})
            if not exhausted:
                for d in gen:
                    safe = filt.feed(d)
                    if safe:
                        ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": 0,
                            "content_index": 0, "delta": safe})
            full = filt.full_text
            before, call = parse_tool_call(full)
            rest = before[len(filt.emitted):] if call \
                else full[len(filt.emitted):]
            if rest:
                ev("response.output_text.delta", {
                    "item_id": msg_id, "output_index": 0,
                    "content_index": 0, "delta": rest})
            repair_text = ""
            err = _validate_tool_call(call, tools)
            if err:
                _metric("tool_calls_rejected")
                log_event("tool_call_rejected", reason=err,
                          tool=(call or {}).get("name"))
                _metric("tool_repairs")
                fixed = _repair_call(system_text, turns, tools, extra,
                                     full, err)
                if fixed is not None:
                    b2, c2 = fixed
                    if c2 and not _validate_tool_call(c2, tools):
                        repair_text, call = b2, c2
                    elif not c2:
                        repair_text, call = b2, None
                    else:
                        call = None
                else:
                    call = None
            final_text = before + repair_text
            if repair_text:
                ev("response.output_text.delta", {
                    "item_id": msg_id, "output_index": 0,
                    "content_index": 0, "delta": repair_text})
            ev("response.output_text.done", {
                "item_id": msg_id, "output_index": 0,
                "content_index": 0, "text": final_text})
            ev("response.content_part.done", {
                "item_id": msg_id, "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": final_text,
                         "annotations": []}})
            msg_item = {"type": "message", "id": msg_id,
                        "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text",
                                     "text": final_text,
                                     "annotations": []}]}
            ev("response.output_item.done",
               {"output_index": 0, "item": msg_item})
            output = [msg_item]
            if call:
                fc = {"type": "function_call",
                      "id": "fc_" + uuid.uuid4().hex[:24],
                      "call_id": "call_" + uuid.uuid4().hex[:24],
                      "name": call.get("name"),
                      "arguments": json.dumps(call.get("input") or {}),
                      "status": "completed"}
                ev("response.output_item.added",
                   {"output_index": 1, "item": dict(fc, status="in_progress",
                                                   arguments="")})
                ev("response.function_call_arguments.delta", {
                    "item_id": fc["id"], "output_index": 1,
                    "delta": fc["arguments"]})
                ev("response.function_call_arguments.done", {
                    "item_id": fc["id"], "output_index": 1,
                    "arguments": fc["arguments"]})
                ev("response.output_item.done",
                   {"output_index": 1, "item": fc})
                output.append(fc)
            ev("response.completed", {"response": _responses_object(
                resp_id, output, in_est,
                _est_tokens(full + repair_text))})
        except _ClientGone:
            gen.close()
            raise
        except UpstreamError as e:
            print(f"[proxy] upstream error mid-stream: {e.status}",
                  flush=True)
            try:
                ev("response.failed", {"response": _responses_object(
                    resp_id, [], in_est, 0, status="failed")})
            except _ClientGone:
                gen.close()
                raise

    def _anthropic(self, body):
        system_text, turns = turns_from_anthropic(body.get("system"),
                                                  body.get("messages") or [])
        tools = anthropic_tools(body.get("tools"))
        extra = self._extra(body)
        if body.get("stream"):
            self._anthropic_stream(body, system_text, turns, tools, extra)
            return
        text = complete(system_text, turns, tools, extra)
        before, call = parse_tool_call(text)
        err = _validate_tool_call(call, tools)
        if err:
            _metric("tool_calls_rejected")
            log_event("tool_call_rejected", reason=err,
                      tool=(call or {}).get("name"))
            _metric("tool_repairs")
            fixed = _repair_call(system_text, turns, tools, extra,
                                 text, err)
            if fixed is not None:
                b2, c2 = fixed
                if c2 and not _validate_tool_call(c2, tools):
                    before, call = b2, c2
                elif not c2:
                    before, call = b2, None
                else:
                    call = None
            else:
                call = None
        content = []
        if before:
            content.append({"type": "text", "text": before})
        stop = "end_turn"
        if call:
            content.append({"type": "tool_use",
                            "id": "toolu_" + uuid.uuid4().hex[:24],
                            "name": call.get("name"),
                            "input": call.get("input") or {}})
            stop = "tool_use"
        if not content:
            content.append({"type": "text", "text": ""})
        msg_id = "msg_" + uuid.uuid4().hex[:24]
        self._send(200, {
            "id": msg_id, "type": "message",
            "role": "assistant", "model": MODEL, "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": _estimate_input(system_text, turns,
                                                     tools),
                      "output_tokens": _est_tokens(text),
                      "estimated": True}})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            _ClientGone)):
            return  # a client hangup is not a server error
        super().handle_error(request, client_address)

if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    _ensure_harvest_key()
    _load_sessions()
    srv = Server(("127.0.0.1", PORT), Handler)
    print(f"mai_proxy on http://127.0.0.1:{PORT}  token_file={TOKEN_FILE} "
          f"token_loaded={bool(get_token())}", flush=True)
    srv.serve_forever()
