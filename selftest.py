"""MAI2API self-test: proxy health + one minimal live completion.
Usage: python selftest.py   (exit code 0 = all good)"""
import json
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = "18787"
BASE = f"http://127.0.0.1:{PORT}"


def main():
    with urllib.request.urlopen(BASE + "/health", timeout=10) as r:
        h = json.loads(r.read().decode())
    print("health:", h)
    tok = h.get("token") or {}
    if not tok.get("loaded"):
        print("FAIL: no token stored yet - complete the one-time "
              "sign-in (python harvest_now.py --setup)")
        return 1
    if (tok.get("seconds_left") or 0) <= 0:
        print("FAIL: token expired - the keeper should land a fresh "
              "one within a few minutes")
        return 1
    body = {"model": "mai-thinking-1-latest",
            "messages": [{"role": "user",
                          "content": "Reply with exactly: SELFTEST-OK"}]}
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 method="POST",
                                 data=json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=180) as r:
        out = json.loads(r.read().decode())
    text = out["choices"][0]["message"]["content"]
    print("completion:", text.strip()[:60])
    ok = "SELFTEST-OK" in text
    print("SELFTEST", "OK" if ok else "UNEXPECTED OUTPUT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
