# MAI2API

Use Microsoft's **MAI-Thinking-1** as a local API. MAI2API runs a small
proxy on your PC that speaks the OpenAI (`/v1/chat/completions`) and
Anthropic (`/v1/messages`) APIs and translates them to the Microsoft
Playground web app, using **your own signed-in session**. Point Claude
Code (or any OpenAI/Anthropic client) at it and the model answers —
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
git clone <this repo> MAI2API
cd MAI2API
powershell -ExecutionPolicy Bypass -File .\setup.ps1
python harvest_now.py --setup   # sign in once in the window that opens
python selftest.py              # expect: SELFTEST OK
```

`setup.ps1` registers logon auto-start for the guardian and the
keeper and starts both. The one-time sign-in creates a dedicated
browser profile in `edge-profile\`; after that, everything is
unattended.

## Daily use

Nothing to start — the pieces auto-start at logon. For Claude Code:

```bat
mai-claude.cmd
```

(It sets `ANTHROPIC_BASE_URL=http://127.0.0.1:18787`,
`ANTHROPIC_API_KEY=dummy`, `ANTHROPIC_MODEL=sonnet` — the model name
must be a Claude-catalog name; the proxy ignores it and always calls
MAI-Thinking-1.) Any other client can use
`http://127.0.0.1:18787/v1/chat/completions` directly.

Feeding a token from somewhere else also works: POST a raw Easy Auth
`id_token` to `http://127.0.0.1:18787/__harvest`, or write it to
`token.txt`.

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
`harvest_stop.txt` in this folder to stand the respective piece down.

## Limits (measured, honest)

- **Throughput**: the Playground enforces an account-level rate
  limit — bursts of ~20–25 streams are fine, sustained throughput is
  ≈10 completions/min. The proxy paces and backs off on 429s, so
  clients see waits, not errors.
- **Tools are emulated**: the Playground offers no native tool
  calling. The proxy teaches the model a text protocol and converts
  its replies into `tool_calls`/`tool_use`; execution happens in your
  client (e.g. Claude Code runs Read/Write/Bash/WebSearch itself).
  Occasionally the model produces a malformed call — the cost of
  emulation.
- **System prompts**: the Playground accepts only a short system
  field and largely ignores it, so the proxy folds system text and
  tool definitions into the conversation (cap via
  `MAI_PROXY_STUFF_CAP`, default 280k chars, oldest turns dropped).
- **One machine, one account, localhost only.** The token and the
  browser profile in this folder are credentials — they are
  git-ignored; keep it that way.

## Troubleshooting

- `selftest.py` says the token expired and nothing heals: the
  session needs a human again — run `python harvest_now.py --setup`.
- Proxy down: the guardian restarts it within about a minute
  (check `proxy.log`).
- Keeper quiet: check `rpa_loop.log`; make sure no `rpa_stop.txt`
  exists.
