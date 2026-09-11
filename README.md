# Ornithopter

![Ornithopter — Antiquities, art by Amy Weber](https://cards.scryfall.io/normal/front/5/9/59cc9bdb-7cf2-4795-bac7-ffff605c9eb0.jpg?1783948360)

*Flying. A cheap and flimsy way to fly — but it works.. mostly.*
*Card: [Ornithopter (Antiquities) on Scryfall](https://scryfall.com/card/atq/60/ornithopter?utm_source=api) · Art by Amy Weber · Card art © Wizards of the Coast, image via Scryfall.*

**Use OpenCode Zen models inside Claude Code — as a local inference point, under official Anthropic model names.**

Claude Code only talks to the Anthropic API and only accepts official Anthropic model names. OpenCode [Zen](https://opencode.ai/docs/zen/) serves great models (including free ones like `muse-spark-1.3-contributor-free`) behind an OpenAI-style gateway with Zen-specific model IDs. Ornithopter bridges the gap: a zero-dependency local proxy on `127.0.0.1` that advertises an official Anthropic name, silently rewrites it to the real Zen model ID, and streams the response back untouched. Zero mana cost, zero dependencies — just a 0/2 flier held together with Thran spare parts.

```
Claude Code  --->  http://127.0.0.1:8646  --->  https://opencode.ai/zen/v1
  model:                 rewrite:                 real inference:
  claude-sonnet-4-5  ->  claude-sonnet-4-5  ->  Zen (your API key)
```

## Why

- **Claude Code compatibility.** Point `ANTHROPIC_BASE_URL` at localhost and keep using your normal model names.
- **Name spoofing.** `--override` advertises any official Anthropic catalog name; the real Zen model is selected separately with `--upstream`.
- **No key sprawl.** Your Zen key lives in one place (the proxy). Local apps use a dummy token.
- **Zero dependencies.** Python 3 stdlib only. Runs on Windows, macOS, Linux. No `pip install`, no venv, no Docker.

## Quick start

```bash
# 1. Get a Zen API key: opencode.ai dashboard -> /connect (or `opencode auth login`)
# 2. Run (Windows: ornithopter.bat, same flags)
python ornithopter.py --key sk-zen-...

# 3. In another terminal, check it
curl http://127.0.0.1:8646/v1/models
```

## Claude Code setup

Two surfaces, same proxy — pick yours:

**Claude Desktop app (GUI):**

1. Open the main menu (hamburger, ☰) → **Help → Troubleshooting → Enable Developer Mode**.
2. This uncovers developer settings under **File → Developer**.
3. In the developer settings, configure local inference: base URL `http://127.0.0.1:8646` with any placeholder token.
4. Ornithopter defaults to advertising **Sonnet 4.5** (`claude-sonnet-4-5`) — so select that model in the app, or restart the proxy with `--override <name>` to advertise a different official Anthropic name and select that instead.

**Claude Code CLI (terminal):** pointing it at a custom `ANTHROPIC_BASE_URL` is the local-inference override mechanism.

The model name Claude Code sends must match `--override` (default: `claude-sonnet-4-5` — change it with the `--override` flag, e.g. `--override claude-opus-4-5`).

**Option A — current terminal only:**

```bash
# Windows (cmd)
set ANTHROPIC_BASE_URL=http://127.0.0.1:8646
set ANTHROPIC_AUTH_TOKEN=dummy
set ANTHROPIC_MODEL=claude-sonnet-4-5

# macOS / Linux
export ANTHROPIC_BASE_URL=http://127.0.0.1:8646
export ANTHROPIC_AUTH_TOKEN=dummy
export ANTHROPIC_MODEL=claude-sonnet-4-5
```

**Option B — persistent on Windows (survives reboots):**

```bat
setx ANTHROPIC_BASE_URL "http://127.0.0.1:8646"
setx ANTHROPIC_AUTH_TOKEN "dummy"
setx ANTHROPIC_MODEL "claude-sonnet-4-5"
```

Then start Ornithopter before launching `claude` (or run it as a scheduled task / service so it's always there).

**Option C — per-project via `.claude/settings.json`:**

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8646",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_MODEL": "claude-sonnet-4-5"
  }
}
```

**Verify before launching `claude`:**

```bash
curl http://127.0.0.1:8646/health
curl http://127.0.0.1:8646/v1/models  # override name must be first entry
```

If Claude Code rejects the model, the name it sends doesn't equal `--override` — set `ANTHROPIC_MODEL` explicitly (Option A/B) rather than relying on its default.

**Port must match:** Claude's base URL port and `--port` must be the same number. If you start the proxy with `--port 8080`, the base URL is `http://127.0.0.1:8080` — a mismatch just looks like silence.

## Examples

```bash
# Advertise Opus locally, actually run a free Muse Spark on Zen
python ornithopter.py --key sk-zen-... \
  --override claude-opus-4-5 --upstream muse-spark-1.3-contributor-free

# Run a free Zen model behind a Sonnet name
python ornithopter.py --key sk-zen-... \
  --override claude-sonnet-4-5 --upstream muse-spark-1.3-contributor-free

# Muse Spark first, Sonnet if it's rate-limited or down
python ornithopter.py --key sk-zen-... \
  --override claude-sonnet-4-5 --upstream muse-spark-1.3-contributor-free \
  --fallback claude-sonnet-4-5

# See what's actually on Zen right now (catalog rotates)
python ornithopter.py --list-models

# Save your setup and auto-launch the Claude app on future runs
python ornithopter.py --key sk-zen-... \
  --override claude-sonnet-4-5 --upstream muse-spark-1.3-contributor-free \
  --fallback claude-sonnet-4-5 --launch --save
# next time: just `python ornithopter.py` — everything (key included) loads from the ini

# Custom port / key from environment
ZEN_API_KEY=sk-zen-... python ornithopter.py --port 9000
```

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--key` | `$ZEN_API_KEY` | Zen API key, attached upstream as `x-api-key` + bearer. Local clients may send any placeholder. |
| `--override` | `claude-sonnet-4-5` | Model name advertised in `/v1/models` and accepted from clients. Warns if not in the known Anthropic catalog. |
| `--upstream` | `muse-spark-1.3-contributor-free` | Real Zen model id to forward to. Free-tier only unless `--allow-paid`. |
| `--allow-paid` | off | Disable the free-tier-only guard. Students: leave it off — paid IDs can spend real credits. |
| `--direct` | off | Disable Messages→Responses translation (rename-and-forward only). For models speaking `/messages` natively, or debugging. |
| `--fallback` | _(none)_ | Comma-separated fallback Zen model IDs, first-last priority. On 429, 5xx, timeout, or connection error the request is retried with the next ID. |
| `--list-models` | | Print the live upstream catalog (one ID per line) and exit. |
| `--launch` | off | Launch the Claude app once the proxy is healthy. Target from `--launch-target` (or ini). |
| `--launch-target` | `claude` | What to launch: exe / Store alias / protocol, or a UWP AppID containing `!` (via `shell:AppsFolder`). Setting it implies `--launch`. |
| `--save` | | Save effective options to `ornithopter.ini` next to the script, then keep running. |
| `--no-config` | | Ignore `ornithopter.ini` even if present. |
| `--verbose` | off | Log every request (method, path, model, redacted headers, first 500 body chars, upstream attempts) to stderr. Upstream failures always log. |
| `--upstream-base` | `https://opencode.ai/zen/v1` | Upstream base URL (override for testing). |
| `--host` | `127.0.0.1` | Bind address. Loopback by default; nothing is exposed to the LAN. |
| `--port` | `8646` | Bind port. |
| `-h, --help` | | Full help. |

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/messages` | Anthropic Messages API (what Claude Code uses). Model rewritten, SSE streamed through. |
| `POST /v1/chat/completions` | OpenAI chat format, for other local tools. |
| `POST /v1/responses` | OpenAI responses format. |
| `GET /v1/models` | Override name first, then the live Zen catalog (70 models at time of writing). |
| `GET /health` | `{"status": "ok", "override": …, "upstream": …}` |

## How it works

1. Client sends an Anthropic request with `model: <override>`.
2. Ornithopter rewrites `model` to the real Zen ID (`--upstream`).
3. Request is forwarded to `<upstream-base>/messages` with your Zen key (`x-api-key` + `Authorization: Bearer`).
4. Upstream response — including SSE event streams — is relayed byte-for-byte. Non-streaming bodies keep their `Content-Length`; streams are connection-close delimited.
5. Upstream errors (e.g. 401 on a bad key) pass through with their original status and body.

## Fallbacks

`--fallback` takes Zen IDs in first-last priority order, and each one
is a real link in the chain: `--upstream A --fallback B,C,D` makes up
to **four** attempts per spoofed request — A, then B, then C, then D.
A link is skipped past when it hits a rate-limit (429), a 5xx, a
timeout, or a connection error, so mixed failures (A 429s, B 500s, C
answers) still land on C. Other errors (401, 400, 404) pass straight
through — retrying those is pointless. Failover happens per request,
before any response bytes flow; once upstream returns 200 the proxy
commits to it. If the **last** link fails with an HTTP error, that
error passes through with its original status and body; you only get
`502 {"error": "all upstreams failed"}` when every link died without
an HTTP response at all (timeouts, refused connections). `/health`
reports the active chain.

## Student safety: free-tier-only by default

Ornithopter is built for classrooms: by default it can **never** route
to a paid model, so it can never run up costs.

- `--upstream` / `--fallback` must be free-tier IDs: verified against
  the **live** Zen catalog at startup (must be listed *and* `-free`
  suffixed, minus server-side keyed twins like `ox-alpha-free` —
  Hermes's own exclusion, mirrored here). Violations refuse to start
  (`exit 2`). Offline, it falls back to suffix checking with a warning.
- The guard also covers **direct** model names: in free-only mode *every*
  requested model — dated IDs, aliases, anything Claude actually sends —
  is mapped onto the free chain, so the guard can't be bypassed from
  Claude Code's config and Claude never gets a rejection for the name
  it prefers.
- `/v1/models` only advertises the override alias plus free-tier IDs —
  paid models aren't even discoverable through the proxy.
- `--allow-paid` disables all of this. Don't set it on student machines.

Why not just copy Hermes's keyless trick? Hermes rides Zen's free tier
with empty-bearer + attribution headers *as a registered aggregator* —
and even that only works with OpenCode session context: direct
anonymous calls fail (`OpenCode's free tier can only be used in
OpenCode`). A standalone localhost proxy can't mint that context, so a
personal Zen key (free tier) remains required — the guard makes sure
that key can only ever touch free models.

## Messages↔Responses translation

Some Zen models (Muse Spark, GPT-, Grok- families — the same split
Hermes uses internally) are served via the **Responses API**, not
`/messages`. Pointing Claude's Messages calls at them returns Zen's
opaque 500, so Ornithopter translates per attempt:

- **Request:** `system` → `instructions`, messages → `input`
  (text/tool_use/tool_result/image blocks mapped), `max_tokens` →
  `max_output_tokens`, tools + `tool_choice` converted, `stream`
  passed through. The call goes to `/responses` with the real model ID.
- **Response:** converted back to an Anthropic message (or Anthropic
  SSE event stream: `message_start` → deltas → `message_delta` →
  `message_stop` → `[DONE]`), with the *requested* model name echoed
  so picky clients accept it.
- Translation is per fallback link: a translated attempt that 500s
  falls over to the next link, translated or direct, by the normal
  chain rules. `--direct` disables it entirely.

Verified against a scripted Responses-shaped dummy: text+tools
round-trip (tool IDs preserved both ways), full streaming event
sequence terminating cleanly, and a mixed chain (translated 500 →
direct 200). Honest limit: the Zen Responses dialect itself is
untested here (no Zen key in this environment) — first real run
against Spark confirms the last mile.

## Config file (`ornithopter.ini`)

Options persist in `ornithopter.ini`, stored next to the script
(wherever `ornithopter.py` lives, not the working directory).
Precedence: **CLI flags > ini > built-in defaults**. `--save` writes
the effective options; `--no-config` ignores the file.

```ini
[ornithopter]
override = claude-sonnet-4-5
upstream = muse-spark-1.3-contributor-free
upstream_base = https://opencode.ai/zen/v1
fallback = claude-sonnet-4-5
launch = True
launch_target = claude
host = 127.0.0.1
port = 8646
key = sk-zen-...
```

The API key **is** saved (your explicit choice for double-click-to-fly
convenience) — in plaintext, so treat the ini like a password file and
don't commit it to a shared repo. Precedence every run:
`--key` flag > `ZEN_API_KEY` env > ini.

## Auto-launch (`--launch`)

With `--launch`, Ornithopter serves in the background, polls its own
`/health` until the proxy answers, then opens the Claude app and keeps
running until you Ctrl+C it.

- Default target is `claude` (exe / Store execution alias / protocol —
  whatever `start` resolves on your machine).
- For the UWP Store app specifically, find its AppID in PowerShell:
  `Get-StartApps | Where-Object {$_.Name -like '*Claude*'}` — then set
  `launch_target` to that AppID (it contains a `!`, e.g.
  `Anthropic.Claude_xxxx!App`) and Ornithopter launches it via
  `shell:AppsFolder`.
- Setting `--launch-target` implies `--launch`; both are ini-saveable,
  so `--save` gives you double-click-to-fly behavior paired with the
  `.bat` launcher.

## Troubleshooting a rejected test request

Run with `--verbose` and retry. The console then shows each request
plus every upstream attempt (`upstream <id> -> HTTP <status>`), which
tells you which side rejected it:

- `upstream <id> -> HTTP 401` — the Zen key is missing/wrong. Check
  `--key` / `ZEN_API_KEY` / ini.
- `upstream <id> -> HTTP 429/5xx` with no fallback left — add
  `--fallback` entries.
- `502 {"error": "all upstreams failed"}` — every link died without an
  HTTP response (network/timeout). The `detail` field has the last error.
- HTTP `500` with Zen's `Internal server error` body — Zen rejected the
  request shape itself (seen with missing auth and wrong endpoints);
  the proxy passes it through untouched, so the body is Zen's, not ours.
- `500` on a *well-formed* request with a *working* key (bad keys give
  `401`, so a 500 means auth passed) points at a model/endpoint
  mismatch: e.g. Muse Spark is served via the Responses API, not
  `/messages` — Hermes routes it that way internally. Confirm with
  `--allow-paid --upstream claude-haiku-4-5` and the same request: a
  200 there means the model needs a different endpoint, which is a
  translation feature, not a config fix.
- Proxy-side crashes now return `400/502` JSON instead of bare 500s;
  malformed bodies, broken pipes, and headerless posts are all handled.
- Malformed inference bodies are rejected locally with `400
  invalid_request` naming the missing field (`messages`, `max_tokens`,
  `input`) — Zen answers these with an opaque 500, so the proxy
  intercepts them first. Only well-formed requests reach upstream.

## Verified behavior

- `POST /v1/messages` with `model: claude-opus-4-5` reaches upstream as `model: claude-sonnet-4-5` (rewrite confirmed against a recording dummy server).
- `GET /v1/models` against live Zen returns the override first + full catalog (70 entries).
- SSE relay is chunk-forwarded, not buffered, so `stream: true` clients work.
- Three-link chain verified against a scripted dummy: primary 429 →
  fallback 500 → second fallback 200, client got the third link's
  response; all-bad chain walked every link and returned the last
  link's error with its original status.
- `--save` writes `ornithopter.ini`; a bare rerun loads override/upstream/fallback/port from it; CLI flags beat ini; `--no-config` ignores it.
- `--launch` waits for `/health` 200 before opening the target (verified with a harmless binary; the UWP `shell:AppsFolder` path is Windows-only and code-reviewed, not live-tested here).
- Free-tier guard verified live: paid `--upstream` refuses with `exit 2`; `--allow-paid` starts clean; `/v1/models` shows override + 7 free IDs, zero paid; direct paid model names are rewritten onto the free chain (no bypass, no rejection).
- Dated/aliased model IDs (`claude-opus-4-1-20250805`, arbitrary strings) all rewrite to the chain primary; `stream:true` SSE responses terminate cleanly (~0.9s for a 3-chunk stream) via `Connection: close`.

## Limitations

- Inference requires a real Zen key; `/v1/models` is public and works without one.
- Ornithopter is inference pass-through only — no agent loop, no tools, no prompt caching. Like the card: it flies, it doesn't fight. Mostly.
- Binds loopback only. If you expose it (`--host 0.0.0.0`), put it behind auth — anyone with network access can spend your Zen credits.

## Requirements

- Python 3.8+. No packages. No build step.

## License

MIT. Not affiliated with OpenCode, Anthropic, or Wizards of the Coast.
