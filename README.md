# MAI2API

Use Microsoft's **MAI-Thinking-1** as a local API. MAI2API runs a small
proxy on your PC that speaks the OpenAI (`/v1/chat/completions`),
Anthropic (`/v1/messages`) and OpenAI Responses (`/v1/responses`)
APIs and translates them to the Microsoft Playground web app, using
**your own signed-in session**. Point Claude Code (or any
OpenAI/Anthropic client) at it and the model answers —
tools included, via emulation (see *Limits*).

> Unofficial and unsupported. It drives a consumer web UI with your
> own account; if the Playground changes, this can break. Prefer the
> official API (Microsoft Foundry, model `MAI-Thinking-1`) if you
> need a contract.

## How it works

```
Claude Code ──► mai_proxy.py (127.0.0.1:18787)
                  │  Bearer = your Playground session token (~65 min)
                  ▼
            playground.microsoft.ai  (tRPC + SSE)
                  ▲
   token supply   │  rpa_keeper.py: resident headless Edge on a
                  │  dedicated profile (CDP), renews the token every
                  │  few minutes, clicks through account-picker /
                  │  "stay signed in" screens when they appear
   supervision    │  guardian.py: restarts the proxy if it dies
   alerts         │  notify.py: Windows toast + optional ntfy push
                  │  when a human sign-in is needed again
```

## Requirements

- Windows 10/11, Python 3.10+ (`pip install -r requirements.txt`)
- Microsoft Edge (preinstalled on Windows) for the dedicated profile
- A Microsoft account that can open https://playground.microsoft.ai
- Optional: Claude Code for the coding-agent setup

## Setup (once)

```powershell
git clone https://github.com/MasayukiTa/MAI2API.git MAI2API
cd MAI2API
powershell -ExecutionPolicy Bypass -File .\setup.ps1
python harvest_now.py --setup   # sign in once in the window that opens
python selftest.py              # expect: SELFTEST OK
```

`setup.ps1` creates a dedicated virtual environment, registers
logon auto-start for the guardian and the keeper, and starts both.

**Runtime state lives outside this folder**: the token, the
dedicated browser profile, sessions, logs and pid files are kept in
`%LOCALAPPDATA%\MAI2API` (override with the `MAI2API_STATE_DIR`
environment variable), so the source tree never holds live
credentials. The one-time sign-in creates the browser profile
there; after that, everything is unattended.

## Daily use

Nothing to start — the pieces auto-start at logon. For Claude Code:

```bat
mai-claude.cmd
```

(It sets `ANTHROPIC_BASE_URL=http://127.0.0.1:18787`,
`ANTHROPIC_API_KEY=dummy`, `ANTHROPIC_MODEL=sonnet`.) Model names
follow an explicit alias policy: any client-supplied name maps to
the single upstream model, and responses always report the real
upstream id (`mai-thinking-1-latest`). Set `MAI_PROXY_MODEL_STRICT=1`
to reject names outside the known alias families with a 400 instead.
Any other client can use `http://127.0.0.1:18787/v1/chat/completions`
directly.

Feeding a token from somewhere else also works: POST a raw Easy Auth
`id_token` to `http://127.0.0.1:18787/__harvest` with the
`X-MAI-Harvest-Key` header (the key is generated on first run in the
state directory, `harvest_key.txt`), or write the token to
`token.txt` in the state directory.

## Optional: phone alerts

If the session ever truly dies (password change, policy
re-evaluation), the keeper cannot fix it alone — it can at least tell
you. Create `notify_config.json`:

```json
{ "ntfy_topic": "your-random-topic-name" }
```

and subscribe to that topic in the [ntfy](https://ntfy.sh) app. The
keeper alerts on sign-in loss, repeated failures, and recovery. A
Windows toast is always attempted as well.

## Files

| file | role |
| --- | --- |
| `mai_proxy.py` | the API proxy (OpenAI + Anthropic surfaces) |
| `rpa_keeper.py` | token keeper: headless Edge + RPA clicks, 4-min passes |
| `headless_harvest.py` | one-shot harvest the proxy spawns on a stale token |
| `guardian.py` | crash watchdog for the proxy (PID-exact restarts) |
| `harvest_now.py` | one-time visible sign-in / manual harvest |
| `notify.py` | toast + ntfy notifications used by the keeper |
| `selftest.py` | health + one live completion |
| `setup.ps1` | one-time setup (auto-start, dependency, first launch) |
| `mai-claude.cmd` | Claude Code launcher |

Stop switches: create `guardian_stop.txt` / `rpa_stop.txt` /
`harvest_stop.txt` in the **state directory** to stand the
respective piece down.

Endpoints: `/v1/chat/completions`, `/v1/messages` (plus
`count_tokens`, an approximation), `/v1/responses`, `/v1/models`,
and the operational trio `/health` (token validity), `/health/live`
(process liveness), `/health/ready` (503 until the token is valid),
plus `/metrics` (request/upstream/auth counters as JSON). All
responses stream truly: upstream deltas are flushed as they arrive.

## Limits (measured, honest)

- **Throughput**: the Playground enforces an account-level rate
  limit — bursts of ~20–25 streams are fine, sustained throughput is
  ≈10 completions/min. The proxy paces and backs off on 429s, so
  clients see waits, not errors.
- **Tools are emulated**: the Playground offers no native tool
  calling. The proxy teaches the model a text protocol and converts
  its replies into `tool_calls`/`tool_use`; execution happens in your
  client (e.g. Claude Code runs Read/Write/Bash/WebSearch itself).
  Calls are validated against the declared tools (name allowlist +
  argument schema): an invalid call is fed back for one repair
  attempt, then dropped — never executed blindly.
- **Usage numbers are estimates**: the Playground reports no token
  counts, so `usage` is estimated from character counts and marked
  (`usage_estimated` / `"estimated": true`). It is never silently 0.
- **Not supported**: image input, `previous_response_id` (rejected
  with a 400 — send full input each time), exact token counting.
  The legacy browser-extension harvester is superseded by the
  headless keeper and is not needed.
- **System prompts**: the Playground accepts only a short system
  field and largely ignores it, so the proxy folds system text and
  tool definitions into the conversation (cap via
  `MAI_PROXY_STUFF_CAP`, default 280k chars, oldest turns dropped).
- **One machine, one account, localhost only.** The token and the
  browser profile in the state directory are credentials — they are
  git-ignored and physically separated from the source; keep it
  that way. `requests.log` records request lines only (method, path,
  status) — never bodies or prompts.

## Troubleshooting

- `selftest.py` says the token expired and nothing heals: the
  session needs a human again — run `python harvest_now.py --setup`.
- Proxy down: the guardian restarts it within about a minute
  (check `proxy.log` in the state directory). A process that stays
  alive but stops answering is terminated and restarted after ~5
  minutes of failed liveness checks, capped at 3 per hour
  (`guardian_state.json` records every restart).
- Keeper quiet: check `rpa_loop.log` in the state directory; make
  sure no `rpa_stop.txt` exists there.

## Development

```powershell
python -m pytest tests -q   # 40+ unit tests, no network needed
```

Release checklist: pytest green · CI green · `selftest.py` OK on a
real install · a streamed and a buffered completion through each
surface (`/v1/chat/completions`, `/v1/messages`, `/v1/responses`) ·
`/health/ready` returns 503 with an expired token and 200 with a
fresh one.
