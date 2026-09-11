# zen-claude-proxy

**Use OpenCode Zen models inside Claude Code — as a local inference point, under official Anthropic model names.**

Claude Code only talks to the Anthropic API and only accepts official Anthropic model names. OpenCode [Zen](https://opencode.ai/docs/zen/) serves great models (including free ones like `muse-spark-1.3-contributor-free`) behind an OpenAI-style gateway with Zen-specific model IDs. `zen-claude-proxy` bridges the gap: a zero-dependency local proxy on `127.0.0.1` that advertises an official Anthropic name, silently rewrites it to the real Zen model ID, and streams the response back untouched.

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
# 2. Run (Windows: zen-claude-proxy.bat, same flags)
python zen-claude-proxy.py --key sk-zen-...

# 3. In another terminal, check it
curl http://127.0.0.1:8646/v1/models
```

## Claude Code setup

There is no "developer mode" switch in Claude Code — pointing it at a
custom `ANTHROPIC_BASE_URL` **is** the local-inference override
mechanism. (If you meant a different app's GUI toggle, tell me which
app and I'll add the exact clicks — I'm not going to guess menu paths.)

The model name Claude Code sends must match `--override`
(default: `claude-sonnet-4-5`).

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

Then start the proxy before launching `claude` (or run it as a
scheduled task / service so it's always there).

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

If Claude Code rejects the model, the name it sends doesn't equal
`--override` — set `ANTHROPIC_MODEL` explicitly (Option A/B) rather
than relying on its default.

## Examples

```bash
# Advertise Opus locally, actually run Sonnet on Zen
python zen-claude-proxy.py --key sk-zen-... \
  --override claude-opus-4-5 --upstream claude-sonnet-4-5

# Run a free Zen model behind a Sonnet name
python zen-claude-proxy.py --key sk-zen-... \
  --override claude-sonnet-4-5 --upstream muse-spark-1.3-contributor-free

# Custom port / key from environment
ZEN_API_KEY=sk-zen-... python zen-claude-proxy.py --port 9000
```

## CLI reference

| Flag | Default | Description |
|---|---|---|
| `--key` | `$ZEN_API_KEY` | Zen API key, attached upstream as `x-api-key` + bearer. Local clients may send any placeholder. |
| `--override` | `claude-sonnet-4-5` | Model name advertised in `/v1/models` and accepted from clients. Warns if not in the known Anthropic catalog. |
| `--upstream` | same as `--override` | Real Zen model ID forwarded to (`claude-sonnet-4-5`, `muse-spark-1.3-contributor-free`, …). |
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
2. Proxy rewrites `model` to the real Zen ID (`--upstream`).
3. Request is forwarded to `<upstream-base>/messages` with your Zen key (`x-api-key` + `Authorization: Bearer`).
4. Upstream response — including SSE event streams — is relayed byte-for-byte. Non-streaming bodies keep their `Content-Length`; streams are connection-close delimited.
5. Upstream errors (e.g. 401 on a bad key) pass through with their original status and body.

## Verified behavior

- `POST /v1/messages` with `model: claude-opus-4-5` reaches upstream as `model: claude-sonnet-4-5` (rewrite confirmed against a recording dummy server).
- `GET /v1/models` against live Zen returns the override first + full catalog (70 entries).
- SSE relay is chunk-forwarded, not buffered, so `stream: true` clients work.

## Limitations

- Inference requires a real Zen key; `/v1/models` is public and works without one.
- The proxy is inference pass-through only — no agent loop, no tools, no prompt caching.
- Binds loopback only. If you expose it (`--host 0.0.0.0`), put it behind auth — anyone with network access can spend your Zen credits.

## Requirements

- Python 3.8+. No packages. No build step.

## License

MIT. Not affiliated with OpenCode or Anthropic.
