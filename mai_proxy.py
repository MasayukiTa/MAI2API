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
PORT = int(os.environ.get("MAI_PROXY_PORT", "18787"))
_HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.environ.get("MAI_TOKEN_FILE", os.path.join(_HERE, "token.txt"))
REQUESTS_LOG = os.path.join(_HERE, "requests.log")
PACE_SECONDS = float(os.environ.get("MAI_PROXY_PACE", "1.2"))

_state_lock = threading.Lock()
_last_call = [0.0]
_sessions = {}  # key -> {"cid","last","turns":[(role, text), ...]}

# --- hardening knobs (all optional, env-overridable) ---
SESSIONS_FILE = os.path.join(_HERE, "sessions.json")
MAX_STREAMS = int(os.environ.get("MAI_PROXY_MAX_STREAMS", "3"))
_stream_sem = threading.Semaphore(MAX_STREAMS)
STUFF_CHAR_CAP = int(os.environ.get("MAI_PROXY_STUFF_CAP", "280000"))
TOKEN_WAIT_SECONDS = int(os.environ.get("MAI_PROXY_TOKEN_WAIT", "150"))


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
        return {"loaded": False}
    try:
        import base64
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return {"loaded": True, "expires_at": exp,
                "seconds_left": int(exp - time.time()) if exp else None}
    except Exception:  # noqa: BLE001
        return {"loaded": True}


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
            and os.path.isdir(os.path.join(_HERE, "edge-profile"))):
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
                print(f"[proxy] 429 on {path}; retry {attempt429 + 1} "
                      f"in {backoff[attempt429]}s", flush=True)
                time.sleep(backoff[attempt429])
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
    data = trpc("conversations.addPromptAndInitResponse", {
        "conversationId": cid, "text": text, "modelId": MODEL,
        "lastMessageId": last_mid})
    mid = data["modelMessage"]["id"]
    parts = []
    for delta in stream_upstream(cid, mid, extra):
        parts.append(delta)
    return "".join(parts), mid


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
    if not info.get("loaded") or (info.get("seconds_left") or 0) <= 0:
        wait_for_fresh_token()
    tok_before = get_token()
    with _stream_sem:
        try:
            return _complete_inner(system_text, turns, tools, extra)
        except UpstreamError as e:
            if e.status not in (401, 403):
                raise
            print("[proxy] upstream auth error; waiting for a fresh "
                  "token, then retrying once", flush=True)
            if not wait_for_fresh_token(old_value=tok_before, timeout=120):
                raise
            return _complete_inner(system_text, turns, tools, extra)


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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        try:
            with open(REQUESTS_LOG, "a", encoding="utf-8") as f:
                f.write(fmt % args + "\n")
        except OSError:
            pass

    def _send(self, status, obj, ctype="application/json"):
        body = obj if isinstance(obj, (bytes, str)) else json.dumps(obj)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"ok": True, "token": token_info()})
        elif path == "/v1/models":
            self._send(200, {"object": "list", "data": [
                {"id": MODEL, "object": "model", "owned_by": "microsoft-mai"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/__harvest":
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
            if path == "/v1/chat/completions":
                self._openai(body)
            elif path == "/v1/messages":
                self._anthropic(body)
            elif path == "/v1/messages/count_tokens":
                approx = len(json.dumps(body.get("messages") or [])) // 4
                self._send(200, {"input_tokens": approx})
            else:
                self._send(404, {"error": "not found"})
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

    def _send_sse(self, events):
        body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n"
                       for name, data in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _openai(self, body):
        system_text, turns = turns_from_openai(body.get("messages") or [])
        text = complete(system_text, turns, body.get("tools"), self._extra(body))
        before, call = parse_tool_call(text)
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
        if body.get("stream"):
            # Buffered-then-chunked SSE (upstream text is fully collected
            # because tool-call blocks must be parsed before responding).
            delta = {"role": "assistant"}
            if tool_calls:
                delta["tool_calls"] = [dict(tc, index=0) for tc in tool_calls]
            elif before:
                delta["content"] = before
            chunks = [
                {"id": cid_, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": MODEL,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": cid_, "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": MODEL,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            ]
            body_out = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
            body_out += "data: [DONE]\n\n"
            self._send(200, body_out, "text/event-stream")
            return
        self._send(200, {
            "id": cid_,
            "object": "chat.completion", "created": int(time.time()),
            "model": MODEL,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                      "total_tokens": 0}})

    def _anthropic(self, body):
        system_text, turns = turns_from_anthropic(body.get("system"),
                                                  body.get("messages") or [])
        tools = anthropic_tools(body.get("tools"))
        text = complete(system_text, turns, tools, self._extra(body))
        before, call = parse_tool_call(text)
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
        if body.get("stream"):
            events = [("message_start", {
                "type": "message_start",
                "message": {"id": msg_id, "type": "message", "role": "assistant",
                            "model": MODEL, "content": [],
                            "stop_reason": None, "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}}})]
            for i, block in enumerate(content):
                if block["type"] == "text":
                    events.append(("content_block_start", {
                        "type": "content_block_start", "index": i,
                        "content_block": {"type": "text", "text": ""}}))
                    if block["text"]:
                        events.append(("content_block_delta", {
                            "type": "content_block_delta", "index": i,
                            "delta": {"type": "text_delta",
                                      "text": block["text"]}}))
                else:
                    events.append(("content_block_start", {
                        "type": "content_block_start", "index": i,
                        "content_block": {"type": "tool_use", "id": block["id"],
                                          "name": block["name"], "input": {}}}))
                    events.append(("content_block_delta", {
                        "type": "content_block_delta", "index": i,
                        "delta": {"type": "input_json_delta",
                                  "partial_json": json.dumps(block["input"])}}))
                events.append(("content_block_stop", {
                    "type": "content_block_stop", "index": i}))
            events.append(("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 0}}))
            events.append(("message_stop", {"type": "message_stop"}))
            self._send_sse(events)
            return
        self._send(200, {
            "id": msg_id, "type": "message",
            "role": "assistant", "model": MODEL, "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}})


if __name__ == "__main__":
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    _load_sessions()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"mai_proxy on http://127.0.0.1:{PORT}  token_file={TOKEN_FILE} "
          f"token_loaded={bool(get_token())}", flush=True)
    srv.serve_forever()
