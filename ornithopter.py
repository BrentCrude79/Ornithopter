#!/usr/bin/env python3
"""ornithopter: local Anthropic-compatible front for OpenCode Zen.

Exposes Zen models on 127.0.0.1 under official Anthropic model names so
Claude Code (and any Anthropic SDK client) accepts it as a local
inference point. Stdlib only — runs on Windows, macOS, Linux.

Examples:
  python ornithopter.py --key sk-zen-... --port 8646
  python ornithopter.py --key sk-zen-... --override claude-opus-4-5
  python ornithopter.py --key sk-zen-... --override claude-sonnet-4-5 --upstream claude-sonnet-4-5

Then point Claude Code at it:
  set ANTHROPIC_BASE_URL=http://127.0.0.1:8646
  set ANTHROPIC_AUTH_TOKEN=dummy
  (model name on the client must match --override)

Flow: client sends model=<override> -> proxy rewrites to the real Zen
model id -> forwards to <upstream-base>/messages with your Zen key ->
streams the upstream response back untouched.
"""
import argparse
import configparser
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

__version__ = "1.7.6"

# Model names Claude Code accepts today (client-facing --override
# namespace). These are official Anthropic API IDs, independent of what
# any gateway serves — the proxy rewrites them to real Zen IDs.
CLAUDE_CODE_MODELS = [
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-sonnet-4-5",
    "claude-sonnet-4-0",
    "claude-haiku-4-5",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
]
# Models that look free (-free suffix) but are keyed server-side and can
# never ride the free tier (mirrors Hermes's own exclusion list).
FREE_KEYED_TWINS = frozenset({"ox-alpha-free"})


def is_free_id(mid):
    """Definitively free-tier marker: -free (Zen) or :free (OpenRouter)
    suffix, minus keyed twins."""
    m = (mid or "").lower()
    return (m.endswith("-free") or m.endswith(":free")) \
        and m not in FREE_KEYED_TWINS


def retry_after_seconds(exc, cap=60):
    """Upstream Retry-After hint in seconds (capped), or None when the
    header is absent/unparseable — in which case retrying blind just
    re-hits the limit, so the caller should fail over instead."""
    try:
        headers = getattr(exc, "headers", None)
        raw = (headers.get("Retry-After") or "").strip() if headers else ""
        if not raw:
            return None
        return max(0, min(cap, int(float(raw))))
    except Exception:
        return None


def _upstream_line_iter(upstream):
    """Yield response body lines, transparently gunzipping."""
    enc = (upstream.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        for line in gzip.decompress(upstream.read()).split(b"\n"):
            yield line
        return
    buf = b""
    while True:
        chunk = upstream.read(8192)
        if not chunk:
            if buf:
                yield buf
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line


def _read_upstream_text(upstream):
    """Full body as text, gunzipping when the upstream encoded it."""
    data = upstream.read()
    if "gzip" in (upstream.headers.get("Content-Encoding") or "").lower():
        data = gzip.decompress(data)
    return data.decode("utf-8")


def fetch_live_ids(base, timeout=15):
    """Live Zen catalog IDs, or None when unreachable."""
    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/models",
            headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {m["id"] for m in json.loads(r.read())["data"]}
    except Exception:
        return None


def validate_body(upstream_path, payload):
    """Reject malformed inference bodies locally with a specific 400.

    Zen answers bad shapes with an opaque 500, which looks like the
    proxy (or gateway) is broken. Catch the common cases first so the
    client sees what's actually missing.
    """
    if not isinstance(payload, dict):
        return None  # non-JSON: let upstream reject it
    if not isinstance(payload.get("model"), str):
        return "missing string 'model'"
    if upstream_path.endswith("/responses"):
        if "input" not in payload:
            return "missing 'input'"
        return None
    # /messages and /chat/completions share the messages array shape.
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return "missing non-empty 'messages' array"
    if upstream_path.endswith("/messages") and not isinstance(
            payload.get("max_tokens"), int):
        return "missing integer 'max_tokens' (required by the Messages API)"
    return None


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="ornithopter",
        description="Local Anthropic-compatible proxy for OpenCode Zen "
                    "with spoofable model names for Claude Code.",
        epilog="override choices (names Claude Code accepts): "
               + ", ".join(CLAUDE_CODE_MODELS)
               + ". --upstream takes any Zen model ID "
                 "(e.g. claude-sonnet-4-5, muse-spark-1.3-contributor-free); "
                 "see https://opencode.ai/zen/v1/models or GET /v1/models "
                 "while running. --list-models prints the live catalog. "
                 "Options load from ornithopter.ini next to the script; "
                 "CLI flags win; --save writes them.",
    )
    p.add_argument("--key", default="",
                   help="Upstream API key (or OPENROUTER_API_KEY / "
                        "ZEN_API_KEY env). Local clients may use any "
                        "placeholder bearer.")
    p.add_argument("--override", default="claude-sonnet-4-5",
                   help="Model name advertised locally and accepted from "
                        "clients. Must be an official Anthropic catalog name "
                        "so Claude Code accepts it. "
                        "(default: %(default)s)")
    p.add_argument("--upstream", default="poolside/laguna-s-2.1:free",
                   help="Real model id to forward to. Free-tier only "
                        "unless --allow-paid. "
                        "(default: %(default)s)")
    p.add_argument("--upstream-base", default="https://openrouter.ai/api/v1",
                   help="Upstream base URL (OpenRouter, Zen, or compatible). "
                        "(default: %(default)s)")
    p.add_argument("--fallback", default="",
                   help="Comma-separated fallback model IDs, first-last "
                        "priority (e.g. --fallback "
                        "nvidia/nemotron-3-ultra-550b-a55b:free). On "
                        "rate-limit (429), 5xx, timeout, or connection "
                        "error the request is retried with the next ID. "
                        "Free-tier only unless --allow-paid.")
    p.add_argument("--list-models", action="store_true",
                   help="Print the live upstream model catalog (one ID per "
                        "line) and exit.")
    p.add_argument("--launch", action="store_true",
                   help="Launch the Claude app once the proxy is healthy. "
                        "Target comes from --launch-target (or the ini).")
    p.add_argument("--launch-target", default="",
                   help="What to launch: an exe / Store alias / protocol "
                        "(e.g. claude) or a UWP AppID containing '!' "
                        "(launched via shell:AppsFolder). Setting this "
                        "implies --launch. (default: claude)")
    p.add_argument("--save", action="store_true",
                   help="Save the effective options to ornithopter.ini "
                        "next to the script, then keep running.")
    p.add_argument("--no-config", action="store_true",
                   help="Ignore ornithopter.ini even if present.")
    p.add_argument("--allow-paid", action="store_true",
                   help="Disable the free-tier-only guard (students: leave "
                        "it off — paid model IDs can spend real credits).")
    p.add_argument("--direct", action="store_true",
                   help="Disable translation: always rename-and-forward "
                        "untouched (for models that speak /messages "
                        "natively, or debugging).")
    p.add_argument("--transport", default="auto",
                   choices=["auto", "messages", "chat", "responses"],
                   help="Force the upstream API shape instead of detecting "
                        "it (useful for other OpenAI-compatible bases). "
                        "(default: %(default)s)")
    p.add_argument("--retries", type=int, default=1,
                   help="Retries of the SAME model on HTTP 429 when the "
                        "upstream gives a Retry-After hint (waits up to "
                        "60s), before failing over to the next fallback. "
                        "Hintless 429s fail over immediately. 0 disables "
                        "retries. (default: %(default)s)")
    p.add_argument("--probe", action="store_true",
                   help="Test every free-tier model with a minimal request "
                        "using your key and report what actually serves "
                        "(status per model). Probes free IDs only — never "
                        "spends. Then exit.")
    p.add_argument("--verbose", action="store_true",
                   help="Log every request (method, path, model, upstream "
                        "attempts and statuses) to stderr. Failures are "
                        "always logged.")
    p.add_argument("--version", action="version", version="ornithopter "
                   + __version__)
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address. (default: %(default)s)")
    p.add_argument("--port", type=int, default=8646,
                   help="Bind port. (default: %(default)s)")
    return p.parse_args(argv)


INI_NAME = "ornithopter.ini"
INI_SECTION = "ornithopter"
# Option keys persisted to the ini, including the API key (plaintext —
# the user's explicit choice for double-click-to-fly convenience).
INI_KEYS = ("override", "upstream", "upstream_base", "fallback", "launch",
            "launch_target", "host", "port", "key", "transport", "retries")


def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def ini_path():
    return os.path.join(script_dir(), INI_NAME)


def probe_once(base, path, body, key, timeout=60):
    """One probe request. Returns (status, detail)."""
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("User-Agent", "curl/8.0")
    req.add_header("Content-Type", "application/json")
    if "openrouter" in (base or "").lower():
        req.add_header("HTTP-Referer",
                       "https://github.com/BrentCrude79/Ornithopter")
        req.add_header("X-Title", "Ornithopter")
    if path == "/messages":
        req.add_header("anthropic-version", "2023-06-01")
    if key:
        req.add_header("x-api-key", key)
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:160].decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        return e.code, detail
    except Exception as e:
        return -1, str(e)[:120]


def probe_models(cfg):
    """Try every free-tier model on every endpoint shape with the
    configured key and report what serves where. Free IDs only."""
    live = fetch_live_ids(cfg["upstream_base"])
    if live is None:
        print("error: could not fetch the live catalog", file=sys.stderr)
        return 1
    free = sorted(m for m in live if is_free_id(m))
    if not free:
        print("no free-tier models in the live catalog")
        return 1
    print("%d free-tier models; probing messages/chat/responses..."
          % len(free))
    mini_messages = {"model": "probe", "max_tokens": 1,
                     "messages": [{"role": "user", "content": "."}]}
    auth_failures = 0
    for mid in free:
        attempts = []
        translated = anthropic_to_responses(dict(mini_messages, model=mid),
                                            mid)
        chat = {"model": mid, "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}]}
        bodies = {"/responses": translated,
                  "/messages": dict(mini_messages, model=mid),
                  "/chat/completions": chat}
        order = {"/responses": ["/responses", "/messages",
                                "/chat/completions"],
                 "chat": ["/chat/completions", "/messages", "/responses"],
                 "messages": ["/messages", "/chat/completions",
                              "/responses"]}[
                                  transport_for(cfg["upstream_base"], mid,
                                                cfg.get("transport"))]
        attempts = [(p, bodies[p]) for p in order]
        served = None
        notes = []
        first_detail = ""
        for path, body in attempts:
            status, detail = probe_once(cfg["upstream_base"], path, body,
                                        cfg["key"])
            if status == 200:
                served = path
                break
            notes.append("%s=%s" % (path, status
                                    if status != -1 else "ERR"))
            if detail and not first_detail:
                first_detail = detail
            if status == 401:
                auth_failures += 1
                if auth_failures >= 3:
                    print("key rejected repeatedly; stopping.",
                          file=sys.stderr)
                    return 1
            time.sleep(0.5)
        if served:
            print("%-42s serves via %-18s" % (mid, served), flush=True)
        else:
            print("%-42s no path (%s)" % (mid, " ".join(notes)), flush=True)
            if first_detail:
                print("    e.g. %s" % first_detail[:160], flush=True)
            time.sleep(0.5)
    return 0


def overlay_ini(args, argv=None):
    """Fill options from ornithopter.ini when the flag wasn't passed
    on the CLI (CLI flags win over ini over built-in defaults)."""
    if args.no_config:
        return args
    cp = configparser.ConfigParser()
    if not os.path.exists(ini_path()) or not cp.read(ini_path()):
        return args
    if not cp.has_section(INI_SECTION):
        return args
    ini = cp[INI_SECTION]
    tokens = argv if argv is not None else sys.argv[1:]

    def given(key):
        flag = "--" + key.replace("_", "-")
        return any(t == flag or t.startswith(flag + "=") for t in tokens)

    for key in ("override", "upstream", "upstream_base", "fallback",
                "launch_target", "host", "transport"):
        if not given(key) and ini.get(key):
            setattr(args, key, ini.get(key))
    if not given("retries") and ini.get("retries"):
        try:
            args.retries = max(0, int(ini.get("retries") or 0))
        except ValueError:
            pass
    # Key precedence: --key flag > OPENROUTER_API_KEY > ZEN_API_KEY > ini.
    # (Env beats ini: only fill from ini when no env key exists.)
    if (not args.key and not given("key") and ini.get("key")
            and not os.environ.get("OPENROUTER_API_KEY")
            and not os.environ.get("ZEN_API_KEY")):
        args.key = ini.get("key")
    if not given("port") and ini.get("port"):
        try:
            args.port = int(ini.get("port") or 8646)
        except ValueError:
            pass
    if not given("launch") and ini.getboolean("launch", fallback=False):
        args.launch = True
    return args


def save_ini(args, cfg):
    """Persist the effective options (including the key) next to the script."""
    cp = configparser.ConfigParser()
    cp[INI_SECTION] = {
        "override": cfg["override"],
        "upstream": args.upstream,
        "upstream_base": args.upstream_base,
        "fallback": args.fallback,
        "launch": str(bool(args.launch or args.launch_target)),
        "launch_target": args.launch_target,
        "host": args.host,
        "port": str(args.port),
        "transport": args.transport,
        "retries": str(args.retries),
        "key": args.key,
    }
    try:
        with open(ini_path(), "w") as f:
            cp.write(f)
        print("saved %s" % ini_path(), flush=True)
    except OSError as e:
        print("could not save %s: %s" % (ini_path(), e), file=sys.stderr)


def wait_healthy(host, port, timeout=15):
    deadline = time.time() + timeout
    url = "http://%s:%d/health" % (host, port)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def launch_app(target):
    """Open the Claude app. UWP AppIDs (containing '!') go through
    shell:AppsFolder; anything else via the shell (exe, Store alias,
    protocol, URL). Non-Windows: exec if found on PATH."""
    target = target or "claude"
    print("launching: %s" % target, flush=True)
    try:
        if os.name == "nt":
            if "!" in target and "://" not in target:
                if not target.lower().startswith("shell:"):
                    target = "shell:AppsFolder\\" + target
                subprocess.Popen(["explorer.exe", target])
            else:
                subprocess.Popen(["cmd", "/c", "start", "", target])
        elif shutil.which(target):
            subprocess.Popen([target])
        else:
            subprocess.Popen(target, shell=True)
    except Exception as e:
        print("launch failed: %s" % e, file=sys.stderr)


# Model families Zen serves via the Responses API (Hermes routes these
# with codex_responses internally): anything else goes direct.
RESPONSES_PREFIXES = ("gpt-", "grok-", "muse-spark")


def needs_responses_transport(mid):
    return (mid or "").lower().startswith(RESPONSES_PREFIXES)


def _text_of(block):
    if isinstance(block, str):
        return block
    if isinstance(block, dict) and block.get("type") in (
            "text", "input_text", "output_text"):
        return block.get("text", "") or ""
    return ""


def anthropic_to_responses(payload, model_id):
    """Anthropic Messages request -> OpenAI Responses request."""
    out = {"model": model_id}
    system = payload.get("system")
    if isinstance(system, str) and system:
        out["instructions"] = system
    elif isinstance(system, list):
        out["instructions"] = "".join(_text_of(b) for b in system)
    req_input = []
    for msg in payload.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        blocks = content if isinstance(content, list) else [content]
        user_parts, asst_parts, calls, outputs = [], [], [], []
        for b in blocks:
            if isinstance(b, str):
                (asst_parts if role == "assistant" else user_parts).append(
                    {"type": "output_text" if role == "assistant"
                     else "input_text", "text": b})
            elif isinstance(b, dict):
                t = b.get("type")
                if t == "text":
                    (asst_parts if role == "assistant" else user_parts).append(
                        {"type": "output_text" if role == "assistant"
                         else "input_text", "text": b.get("text", "") or ""})
                elif t == "image":
                    src = b.get("source", {}) or {}
                    if src.get("type") == "base64":
                        user_parts.append({
                            "type": "input_image",
                            "image_url": "data:%s;base64,%s" % (
                                src.get("media_type", "image/png"),
                                src.get("data", ""))})
                    elif src.get("type") == "url":
                        user_parts.append({"type": "input_image",
                                           "image_url": src.get("url", "")})
                elif t == "tool_use":
                    calls.append({"type": "function_call",
                                  "id": "fc_" + str(b.get("id", "")),
                                  "call_id": str(b.get("id", "")),
                                  "name": str(b.get("name", "")),
                                  "arguments": json.dumps(
                                      b.get("input", {}))})
                elif t == "tool_result":
                    inner = b.get("content")
                    inner = inner if isinstance(inner, list) else [inner]
                    outputs.append({
                        "type": "function_call_output",
                        "call_id": str(b.get("tool_use_id", "")),
                        "output": "".join(_text_of(c) for c in inner)})
        if role == "user" and (user_parts or not outputs):
            req_input.append({"role": "user", "content": user_parts or [
                {"type": "input_text", "text": ""}]})
        if role == "assistant" and asst_parts:
            req_input.append({"role": "assistant", "content": asst_parts})
        req_input.extend(calls)
        req_input.extend(outputs)
    out["input"] = req_input
    if isinstance(payload.get("max_tokens"), int):
        out["max_output_tokens"] = payload["max_tokens"]
    for k in ("temperature", "top_p"):
        if payload.get(k) is not None:
            out[k] = payload[k]
    if payload.get("stream") is True:
        out["stream"] = True
    tools = []
    for t in payload.get("tools", []) or []:
        if isinstance(t, dict) and t.get("name"):
            tools.append({"type": "function", "name": t["name"],
                          "description": t.get("description", "") or "",
                          "parameters": t.get("input_schema",
                                              {"type": "object"})})
    if tools:
        out["tools"] = tools
    tc = payload.get("tool_choice")
    if isinstance(tc, dict):
        if tc.get("type") == "auto":
            out["tool_choice"] = "auto"
        elif tc.get("type") == "any":
            out["tool_choice"] = "required"
        elif tc.get("type") == "tool":
            out["tool_choice"] = {"type": "function",
                                  "name": tc.get("name", "")}
    return out


def responses_to_anthropic(resp, req_model):
    """OpenAI Responses object -> Anthropic Messages object."""
    rid = str(resp.get("id", ""))
    content, stop = [], "end_turn"
    for item in resp.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for c in item.get("content", []) or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "output_text":
                    content.append({"type": "text",
                                    "text": c.get("text", "") or ""})
                elif c.get("type") == "refusal":
                    content.append({"type": "text",
                                    "text": c.get("refusal", "") or ""})
        elif item.get("type") == "function_call":
            try:
                fargs = json.loads(item.get("arguments") or "{}")
            except Exception:
                fargs = {}
            content.append({"type": "tool_use",
                            "id": str(item.get("call_id")
                                      or item.get("id") or ""),
                            "name": str(item.get("name", "")),
                            "input": fargs})
    if str(resp.get("status") or "completed") == "incomplete":
        stop = "max_tokens"
    usage = resp.get("usage") or {}
    return {"id": "msg_" + rid.replace("resp_", ""), "type": "message",
            "role": "assistant", "model": req_model, "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": _usage((usage.get("input_tokens") or 0),
                            (usage.get("output_tokens") or 0))}


class ResponsesStreamToAnthropic:
    """Feeds OpenAI Responses SSE data payloads (dicts), yields Anthropic
    SSE byte chunks."""

    def __init__(self, req_model):
        self.req_model = req_model
        self.msg_id = None
        self.item_index = {}
        self.next_tool_index = 1
        self.done = False

    def _ev(self, obj):
        # Faithful Anthropic SSE: typed event line + data line. No [DONE]
        # sentinel — the real API ends with message_stop and hangs up.
        return ("event: %s\ndata: %s\n\n" % (
            obj.get("type", "message"),
            json.dumps(obj, separators=(",", ":")))).encode()

    def feed(self, data):
        out = []
        t = data.get("type") if isinstance(data, dict) else None
        if t == "response.created":
            r = data.get("response", {}) or {}
            self.msg_id = "msg_" + str(r.get("id", "")).replace("resp_", "")
            out.append(self._ev({
                "type": "message_start",
                "message": {"id": self.msg_id, "type": "message",
                            "role": "assistant", "model": self.req_model,
                            "content": [], "stop_reason": None,
                            "stop_sequence": None,
                            "usage": _usage(0, 0)}}))
        elif t == "response.output_item.added":
            item = data.get("item", {}) or {}
            iid = str(item.get("id", ""))
            if item.get("type") == "message":
                self.item_index[iid] = 0
                out.append(self._ev({"type": "content_block_start",
                                     "index": 0, "content_block": {
                                         "type": "text", "text": ""}}))
            elif item.get("type") == "function_call":
                idx = self.next_tool_index
                self.next_tool_index += 1
                self.item_index[iid] = idx
                out.append(self._ev({"type": "content_block_start",
                                     "index": idx, "content_block": {
                                         "type": "tool_use",
                                         "id": str(item.get("call_id")
                                                   or iid),
                                         "name": str(item.get("name", "")),
                                         "input": {}}}))
        elif t == "response.output_text.delta":
            idx = self.item_index.get(str(data.get("item_id")), 0)
            out.append(self._ev({"type": "content_block_delta",
                                 "index": idx, "delta": {
                                     "type": "text_delta",
                                     "text": data.get("delta", "") or ""}}))
        elif t == "response.function_call_arguments.delta":
            idx = self.item_index.get(str(data.get("item_id")), 1)
            out.append(self._ev({"type": "content_block_delta",
                                 "index": idx, "delta": {
                                     "type": "input_json_delta",
                                     "partial_json": data.get("delta",
                                                              "") or ""}}))
        elif t in ("response.output_text.done", "response.output_item.done"):
            item = data.get("item", {}) or {}
            key = str(item.get("id", "") or data.get("item_id", ""))
            out.append(self._ev({"type": "content_block_stop",
                                 "index": self.item_index.get(key, 0)}))
        elif t == "response.completed":
            r = data.get("response", {}) or {}
            u = r.get("usage") or {}
            stop = ("max_tokens"
                    if str(r.get("status") or "") == "incomplete"
                    else "end_turn")
            out.append(self._ev({"type": "message_delta",
                                 "delta": {"stop_reason": stop,
                                           "stop_sequence": None},
                                 "usage": _usage(
                                     (u.get("input_tokens") or 0),
                                     (u.get("output_tokens") or 0))}))
            out.append(self._ev({"type": "message_stop"}))
            self.done = True
        elif t in ("response.failed", "response.incomplete"):
            out.append(self._ev({"type": "message_delta",
                                 "delta": {"stop_reason": "end_turn",
                                           "stop_sequence": None},
                                 "usage": _usage(0, 0)}))
            out.append(self._ev({"type": "message_stop"}))
            self.done = True
        return out

    def finish(self):
        if self.done:
            return []
        self.done = True
        return [self._ev({"type": "message_delta",
                          "delta": {"stop_reason": "end_turn",
                                    "stop_sequence": None},
                          "usage": _usage(0, 0)}),
                self._ev({"type": "message_stop"})]


def transport_for(base, mid, force="auto"):
    """Which upstream API shape serves this model: responses (spark/gpt/
    grok families), chat (OpenAI-compatible bases such as OpenRouter, or
    forced), or messages (native Anthropic surface)."""
    if force in ("responses", "chat", "messages"):
        return force
    if needs_responses_transport(mid):
        return "responses"
    if "openrouter" in (base or "").lower():
        return "chat"
    return "messages"


def anthropic_to_chat(payload, model_id):
    """Anthropic Messages request -> OpenAI chat/completions request."""
    out = {"model": model_id}
    msgs = []
    system = payload.get("system")
    if isinstance(system, str) and system:
        msgs.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "".join(_text_of(b) for b in system)
        if text:
            msgs.append({"role": "system", "content": text})
    for msg in payload.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        blocks = content if isinstance(content, list) else [content]
        if role == "assistant":
            text_parts, calls = [], []
            for b in blocks:
                if isinstance(b, str):
                    text_parts.append(b)
                elif isinstance(b, dict):
                    if b.get("type") == "text":
                        text_parts.append(b.get("text", "") or "")
                    elif b.get("type") == "tool_use":
                        calls.append({
                            "id": str(b.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": str(b.get("name", "")),
                                "arguments": json.dumps(
                                    b.get("input", {}))}})
            m = {"role": "assistant"}
            if text_parts:
                m["content"] = "".join(text_parts)
            elif not calls:
                m["content"] = ""
            else:
                m["content"] = None
            if calls:
                m["tool_calls"] = calls
            msgs.append(m)
        else:
            for b in blocks:
                if isinstance(b, str):
                    msgs.append({"role": "user", "content": b})
                elif isinstance(b, dict):
                    t = b.get("type")
                    if t == "text":
                        msgs.append({"role": "user",
                                     "content": b.get("text", "") or ""})
                    elif t == "image":
                        src = b.get("source", {}) or {}
                        if src.get("type") == "base64":
                            url = "data:%s;base64,%s" % (
                                src.get("media_type", "image/png"),
                                src.get("data", ""))
                        else:
                            url = src.get("url", "") or ""
                        msgs.append({"role": "user", "content": [{
                            "type": "image_url", "image_url": {"url": url}}]})
                    elif t == "tool_result":
                        inner = b.get("content")
                        inner = inner if isinstance(inner, list) else [inner]
                        msgs.append({
                            "role": "tool",
                            "tool_call_id": str(b.get("tool_use_id", "")),
                            "content": "".join(_text_of(c)
                                               for c in inner)})
    out["messages"] = msgs
    if isinstance(payload.get("max_tokens"), int):
        out["max_tokens"] = payload["max_tokens"]
    for k in ("temperature", "top_p"):
        if payload.get(k) is not None:
            out[k] = payload[k]
    if isinstance(payload.get("stop_sequences"), list):
        out["stop"] = payload["stop_sequences"]
    if payload.get("stream") is True:
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    tools = []
    for t in payload.get("tools", []) or []:
        if isinstance(t, dict) and t.get("name"):
            tools.append({"type": "function", "function": {
                "name": t["name"],
                "description": t.get("description", "") or "",
                "parameters": t.get("input_schema", {"type": "object"})}})
    if tools:
        out["tools"] = tools
    tc = payload.get("tool_choice")
    if isinstance(tc, dict):
        if tc.get("type") == "auto":
            out["tool_choice"] = "auto"
        elif tc.get("type") == "any":
            out["tool_choice"] = "required"
        elif tc.get("type") == "tool":
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": tc.get("name", "")}}
    return out


def chat_to_anthropic(resp, req_model):
    """OpenAI chat/completions object -> Anthropic Messages object."""
    rid = str(resp.get("id", ""))
    choices = resp.get("choices", []) or [{}]
    choice = choices[0] if isinstance(choices[0], dict) else {}
    msg = choice.get("message", {}) or {}
    finish = str(choice.get("finish_reason") or "stop")
    content = []
    text = msg.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    for call in msg.get("tool_calls", []) or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function", {}) or {}
        try:
            fargs = json.loads(fn.get("arguments") or "{}")
        except Exception:
            fargs = {}
        content.append({"type": "tool_use",
                        "id": str(call.get("id", "")),
                        "name": str(fn.get("name", "")),
                        "input": fargs})
    stop = {"stop": "end_turn", "length": "max_tokens",
            "tool_calls": "tool_use", "content_filter": "end_turn",
            "function_call": "tool_use"}.get(finish, "end_turn")
    usage = resp.get("usage", {}) or {}
    return {"id": "msg_" + rid.replace("chatcmpl-", ""), "type": "message",
            "role": "assistant", "model": req_model, "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": _usage((usage.get("prompt_tokens") or 0),
                            (usage.get("completion_tokens") or 0))}


class ChatStreamToAnthropic:
    """Feeds OpenAI chat SSE chunk dicts, yields Anthropic SSE bytes."""

    def __init__(self, req_model):
        self.req_model = req_model
        self.msg_id = None
        self.text_open = False
        self.tool_index = {}  # openai tool index -> anthropic block index
        self.tool_meta = {}   # anthropic block index -> {id, name, started}
        self.next_tool_block = 1
        self.usage = _usage(0, 0)
        self.stop = "end_turn"
        self.done = False

    def _ev(self, obj):
        # Faithful Anthropic SSE: typed event line + data line. No [DONE]
        # sentinel — the real API ends with message_stop and hangs up.
        return ("event: %s\ndata: %s\n\n" % (
            obj.get("type", "message"),
            json.dumps(obj, separators=(",", ":")))).encode()

    def feed(self, data):
        out = []
        if not isinstance(data, dict):
            return out
        if self.msg_id is None:
            self.msg_id = "msg_" + str(data.get("id", "")).replace(
                "chatcmpl-", "")
            out.append(self._ev({
                "type": "message_start",
                "message": {"id": self.msg_id, "type": "message",
                            "role": "assistant", "model": self.req_model,
                            "content": [], "stop_reason": None,
                            "stop_sequence": None,
                            "usage": _usage(0, 0)}}))
        if isinstance(data.get("usage"), dict):
            u = data["usage"]
            self.usage = _usage((u.get("prompt_tokens") or 0),
                                (u.get("completion_tokens") or 0))
        choices = data.get("choices", []) or []
        choice = choices[0] if choices and isinstance(choices[0],
                                                      dict) else {}
        finish = choice.get("finish_reason")
        if finish:
            self.stop = {"stop": "end_turn", "length": "max_tokens",
                         "tool_calls": "tool_use",
                         "content_filter": "end_turn",
                         "function_call": "tool_use"}.get(str(finish),
                                                          "end_turn")
        delta = choice.get("delta", {}) or {}
        text = delta.get("content")
        if isinstance(text, str) and text:
            if not self.text_open:
                self.text_open = True
                out.append(self._ev({"type": "content_block_start",
                                     "index": 0, "content_block": {
                                         "type": "text", "text": ""}}))
            out.append(self._ev({"type": "content_block_delta",
                                 "index": 0, "delta": {
                                     "type": "text_delta", "text": text}}))
        for call in delta.get("tool_calls", []) or []:
            if not isinstance(call, dict):
                continue
            oidx = call.get("index", 0)
            if oidx not in self.tool_index:
                self.tool_index[oidx] = self.next_tool_block
                self.next_tool_block += 1
            bidx = self.tool_index[oidx]
            meta = self.tool_meta.setdefault(bidx, {"started": False})
            fn = call.get("function", {}) or {}
            if call.get("id"):
                meta["id"] = str(call["id"])
            if fn.get("name"):
                meta["name"] = str(fn["name"])
            if not meta["started"] and meta.get("name"):
                meta["started"] = True
                out.append(self._ev({"type": "content_block_start",
                                     "index": bidx, "content_block": {
                                         "type": "tool_use",
                                         "id": meta.get("id",
                                                        "call_%d" % bidx),
                                         "name": meta["name"],
                                         "input": {}}}))
            frag = fn.get("arguments") or ""
            if frag and meta.get("started"):
                out.append(self._ev({"type": "content_block_delta",
                                     "index": bidx, "delta": {
                                         "type": "input_json_delta",
                                         "partial_json": frag}}))
        return out

    def _close_all(self):
        out = []
        if self.text_open:
            out.append(self._ev({"type": "content_block_stop", "index": 0}))
            self.text_open = False
        for bidx, meta in sorted(self.tool_meta.items()):
            if meta.get("started"):
                out.append(self._ev({"type": "content_block_stop",
                                     "index": bidx}))
                meta["started"] = False
        return out

    def finish(self):
        if self.done:
            return []
        self.done = True
        return self._close_all() + [self._ev({
            "type": "message_delta",
            "delta": {"stop_reason": self.stop, "stop_sequence": None},
            "usage": self.usage}),
            self._ev({"type": "message_stop"})]


def _usage(inp, outp):
    """Full Anthropic usage shape — strict clients read the cache keys."""
    return {"input_tokens": inp or 0, "output_tokens": outp or 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def make_handler(cfg):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "ornithopter/1.0"
        protocol_version = "HTTP/1.1"
        # Idle keep-alive sockets die instead of leaking threads.
        timeout = 30

        def log_message(self, *a):
            pass

        def end_headers(self):
            # Browser/Electron fetch contexts need CORS headers to read
            # responses at all; harmless everywhere else.
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, Authorization, x-api-key, "
                             "anthropic-version")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_json(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _catalog_payload(self):
            # OpenAI-shaped "data" plus an Ollama-shaped "models" array,
            # like llama.cpp serves — whichever shape the client reads,
            # the override alias is first with completion capability.
            models = [{"id": cfg["override"], "object": "model",
                       "created": 0, "owned_by": "anthropic",
                       "aliases": [cfg["override"]]}]
            try:
                req = urllib.request.Request(
                    cfg["upstream_base"] + "/models",
                    headers={"User-Agent": "curl/8.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    for m in json.loads(r.read())["data"]:
                        if m["id"] == cfg["override"]:
                            continue
                        if cfg["free_only"] and not is_free_id(m["id"]):
                            continue
                        m.setdefault("aliases", [m["id"]])
                        models.append(m)
            except Exception:
                pass
            ollama = [{"name": m["id"], "model": m["id"], "modified_at": "",
                       "size": 0, "digest": "", "details": {},
                       "capabilities": ["completion"]} for m in models]
            return {"object": "list", "data": models, "models": ollama}

        def _models_payload(self):
            return self._catalog_payload()

        def do_GET(self):
            if self.path in ("/v1/models", "/models"):
                self._send_json(self._models_payload())
            elif self.path in ("/api/tags", "/api/ps"):
                # Ollama-style catalog for clients that read it.
                self._send_json({"models": self._catalog_payload()[
                    "models"]})
            elif self.path in ("/", "/health", "/v1/health"):
                self._send_json({"status": "ok", "override": cfg["override"],
                                 "upstream": cfg["upstream"],
                                 "fallbacks": cfg["fallbacks"]})
            else:
                self._send_json({"error": "not found"}, 404)

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (ValueError, TypeError):
                length = 0
            return self.rfile.read(length) if length else b""

        def _make_request(self, upstream_path, body):
            req = urllib.request.Request(
                cfg["upstream_base"] + upstream_path, data=body, method="POST")
            req.add_header("User-Agent", "curl/8.0")
            req.add_header("Content-Type", "application/json")
            if "openrouter" in (cfg.get("upstream_base") or "").lower():
                req.add_header(
                    "HTTP-Referer",
                    "https://github.com/BrentCrude79/Ornithopter")
                req.add_header("X-Title", "Ornithopter")
            if self.headers.get("Accept"):
                req.add_header("Accept", self.headers.get("Accept"))
            # Anthropic-shaped auth upstream + vanilla bearer fallback.
            if cfg["key"]:
                req.add_header("x-api-key", cfg["key"])
                req.add_header("Authorization", "Bearer " + cfg["key"])
            elif self.headers.get("Authorization"):
                req.add_header("Authorization",
                               self.headers.get("Authorization"))
            if self.headers.get("anthropic-version"):
                req.add_header("anthropic-version",
                               self.headers.get("anthropic-version"))
            elif upstream_path.endswith("/messages"):
                # Anthropic APIs require a version; some gateways omit it
                # and Zen 500s instead of 400ing. Default, don't override.
                req.add_header("anthropic-version", "2023-06-01")
            return req

        def _relay(self, upstream):
            self.send_response(upstream.status)
            # No chunked encoding implemented: terminate with connection
            # close so streaming clients (SSE) see end-of-body instead of
            # hanging on a keep-alive socket.
            self.send_header("Connection", "close")
            ctype = upstream.headers.get("Content-Type", "application/json")
            self.send_header("Content-Type", ctype)
            cenc = upstream.headers.get("Content-Encoding")
            if cenc:
                # Never re-label encoded bytes as plain JSON.
                self.send_header("Content-Encoding", cenc)
            if "text/event-stream" in ctype:
                # Stream SSE chunks through; connection-close delimited.
                self.end_headers()
                try:
                    while True:
                        chunk = upstream.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                data = upstream.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                if cfg.get("verbose"):
                    try:
                        print("reply: %r" % (data[:500].decode(
                            "utf-8", errors="replace"),), flush=True,
                            file=sys.stderr)
                    except Exception:
                        pass

        def _passthrough_error(self, e):
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            cenc = e.headers.get("Content-Encoding") if getattr(
                e, "headers", None) else None
            if cenc:
                self.send_header("Content-Encoding", cenc)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _relay_converted(self, req, req_model, stream, obj_fn,
                             translator_cls):
            """Relay a converted upstream (responses/chat), returning
            Anthropic Messages shape (object or SSE stream)."""
            upstream = urllib.request.urlopen(req, timeout=300)
            if not stream:
                try:
                    resp = json.loads(_read_upstream_text(upstream))
                    body = json.dumps(obj_fn(resp, req_model)).encode()
                except Exception as e:
                    print("relay failed: unreadable upstream body: %s" % e,
                          flush=True, file=sys.stderr)
                    self._send_json(
                        {"error": "bad_upstream_body",
                         "detail": "upstream returned 200 with an empty, "
                                   "non-JSON, or undecodable body"}, 502)
                    return
                self.send_response(200)
                self.send_header("Connection", "close")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                if cfg.get("verbose"):
                    try:
                        print("reply: %r" % (body[:500].decode(
                            "utf-8", errors="replace"),), flush=True,
                            file=sys.stderr)
                    except Exception:
                        pass
                return
            self.send_response(200)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            tr = translator_cls(req_model)
            try:
                for line in _upstream_line_iter(upstream):
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload_s = line[5:].strip()
                    if payload_s == b"[DONE]":
                        continue
                    try:
                        data = json.loads(payload_s.decode())
                    except Exception:
                        continue
                    for piece in tr.feed(data):
                        self.wfile.write(piece)
                    self.wfile.flush()
                for piece in tr.finish():
                    self.wfile.write(piece)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                print("relay failed mid-stream: %s" % e, flush=True,
                      file=sys.stderr)

        def _relay_translated(self, req, req_model, stream):
            self._relay_converted(req, req_model, stream,
                                  responses_to_anthropic,
                                  ResponsesStreamToAnthropic)

        def _relay_chat(self, req, req_model, stream):
            self._relay_converted(req, req_model, stream,
                                  chat_to_anthropic,
                                  ChatStreamToAnthropic)

        def _forward(self, upstream_path):
            raw = self._read_body()
            try:
                payload = json.loads(raw.decode() or "{}")
                incoming = (payload.get("model") if isinstance(payload, dict)
                            else None)
                if not isinstance(payload, dict):
                    payload = None
            except Exception:
                payload, incoming = None, None  # non-JSON: forward untouched
            if cfg.get("verbose"):
                print("POST %s model=%r bytes=%d" % (
                    upstream_path, incoming, len(raw)), flush=True,
                    file=sys.stderr)
                try:
                    hdrs = {k: ("<redacted>" if "key" in k.lower()
                                or k.lower() == "authorization" else v)
                            for k, v in self.headers.items()}
                    print("client headers: %r" % (hdrs,), flush=True,
                          file=sys.stderr)
                    try:
                        print("body: %r" % (raw[:500].decode("utf-8",
                              errors="replace"),), flush=True,
                              file=sys.stderr)
                    except Exception:
                        pass
                except Exception:
                    pass
            problem = (validate_body(upstream_path, payload)
                       if payload is not None else None)
            if problem:
                print("rejecting malformed body: %s" % problem, flush=True,
                      file=sys.stderr)
                self._send_json({"error": "invalid_request",
                                 "detail": problem}, 400)
                return
            # Free-only mode maps EVERY model name onto the free chain, so
            # no client can reach (or bill) a paid model whatever it asks
            # for — Claude sends dated/aliased IDs, not just --override.
            # With --allow-paid only the override alias is rewritten and
            # anything else passes through untouched.
            spoofed = True if cfg["free_only"] else incoming == cfg["override"]
            attempts = ([cfg["upstream"]] + cfg["fallbacks"]
                        if spoofed else [None])
            last_error = "no attempts made"
            req_name = (incoming if isinstance(incoming, str)
                        else cfg["override"])
            for i, attempt in enumerate(attempts):
                use_path = upstream_path
                translate = None
                try:
                    body = raw
                    if payload is not None and attempt is not None:
                        if (upstream_path.endswith("/messages")
                                and not cfg.get("direct")):
                            mode = transport_for(cfg.get("upstream_base"),
                                                 attempt,
                                                 cfg.get("transport"))
                            if mode != "messages":
                                # Model lives on another API shape: translate
                                # the whole request instead of renaming it.
                                conv = (anthropic_to_responses
                                        if mode == "responses" else
                                        anthropic_to_chat)
                                rbody = conv(payload, attempt)
                                body = json.dumps(rbody).encode()
                                use_path = ("/responses"
                                            if mode == "responses" else
                                            "/chat/completions")
                                translate = mode
                            else:
                                payload["model"] = attempt
                                body = json.dumps(payload).encode()
                        else:
                            # Rewrite the spoofed name to this attempt's Zen id.
                            payload["model"] = attempt
                            body = json.dumps(payload).encode()
                except Exception as e:
                    self._send_json({"error": "bad_request",
                                     "detail": str(e)}, 400)
                    return
                tag = attempt if attempt is not None else "passthrough"
                if translate and cfg.get("verbose"):
                    print("translated messages->%s for %s" % (translate,
                                                              tag),
                          flush=True, file=sys.stderr)
                max_retries = max(0, cfg.get("retries", 0) or 0)
                attempt_no = 0
                while True:
                    try:
                        req = self._make_request(use_path, body)
                        stream_out = (bool(payload.get("stream"))
                                      if isinstance(payload, dict) else False)
                        if translate == "responses":
                            self._relay_translated(req, req_name, stream_out)
                        elif translate == "chat":
                            self._relay_chat(req, req_name, stream_out)
                        else:
                            self._relay(urllib.request.urlopen(
                                req, timeout=300))
                        if cfg.get("verbose"):
                            print("upstream %s -> 200" % tag, flush=True,
                                  file=sys.stderr)
                        return
                    except urllib.error.HTTPError as e:
                        print("upstream %s -> HTTP %s" % (tag, e.code),
                              flush=True, file=sys.stderr)
                        if e.code == 429 and attempt_no < max_retries:
                            wait = retry_after_seconds(e)
                            if wait is not None:
                                attempt_no += 1
                                print("429, retrying %s in %ss (%d/%d)" % (
                                    tag, wait, attempt_no, max_retries),
                                    flush=True, file=sys.stderr)
                                time.sleep(wait)
                                continue
                            print("429 with no Retry-After hint on %s; "
                                  "failing over instead of retrying blind"
                                  % tag, flush=True, file=sys.stderr)
                        if ((e.code == 429 or e.code >= 500)
                                and i < len(attempts) - 1):
                            try:
                                last_error = "%s: %s" % (e.code,
                                                         e.read()[:200])
                            except Exception:
                                last_error = "HTTP %s" % e.code
                            break  # rate-limited / sick: try next candidate
                        self._passthrough_error(e)
                        return
                    except Exception as e:
                        print("upstream %s -> error: %s" % (tag, e),
                              flush=True, file=sys.stderr)
                        last_error = str(e)  # timeout/refused: try next
                        break
            self._send_json({"error": "all upstreams failed",
                             "detail": str(last_error)}, 502)

        def do_POST(self):
            if self.path in ("/v1/messages", "/messages"):
                self._forward("/messages")
            elif self.path in ("/v1/chat/completions",
                               "/chat/completions"):
                self._forward("/chat/completions")
            elif self.path in ("/v1/responses", "/responses"):
                self._forward("/responses")
            else:
                self._send_json({"error": "not found"}, 404)

    return Handler


def main(argv=None):
    args = parse_args(argv)
    args = overlay_ini(args, argv)
    if args.override not in CLAUDE_CODE_MODELS:
        print("warning: --override '%s' is not a model name Claude Code "
              "accepts; it may reject it. Accepts: %s"
              % (args.override, ", ".join(CLAUDE_CODE_MODELS)),
              file=sys.stderr)
    key = (args.key or os.environ.get("OPENROUTER_API_KEY", "") or
           os.environ.get("ZEN_API_KEY", ""))
    cfg = {"override": args.override,
           "upstream": args.upstream or args.override,
           "upstream_base": args.upstream_base.rstrip("/"),
           "fallbacks": [f.strip() for f in args.fallback.split(",")
                         if f.strip()],
           "free_only": not args.allow_paid,
           "direct": args.direct,
           "transport": args.transport,
           "retries": args.retries,
           "verbose": args.verbose,
           "key": key.strip()}
    if args.probe:
        return probe_models(cfg)
    if cfg["free_only"]:
        live = fetch_live_ids(cfg["upstream_base"])
        if live is None:
            print("warning: could not verify the live catalog; checking "
                  "-free suffix only.", file=sys.stderr)
        problems = []
        for m in [cfg["upstream"]] + cfg["fallbacks"]:
            if live is not None and m not in live:
                problems.append("%s: not in the live catalog" % m)
            elif not is_free_id(m):
                problems.append("%s: not a free-tier model ID "
                                "(students guard)" % m)
        if problems:
            print("free-tier guard: refusing to start "
                  "(use --allow-paid to override):", file=sys.stderr)
            for p in problems:
                print("  - " + p, file=sys.stderr)
            return 2
    if args.list_models:
        req = urllib.request.Request(
            cfg["upstream_base"] + "/models",
            headers={"User-Agent": "curl/8.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                for m in json.loads(r.read())["data"]:
                    print(m["id"])
        except Exception as e:
            print("error fetching catalog: %s" % e, file=sys.stderr)
            return 1
        return 0
    if not cfg["key"]:
        print("warning: no Zen key given (--key or ZEN_API_KEY); "
              "/v1/models will work but inference will 401 upstream.",
              file=sys.stderr)
    from http.server import ThreadingHTTPServer
    handler = make_handler(cfg)
    if args.save:
        save_ini(args, cfg)
    srv = ThreadingHTTPServer((args.host, args.port), handler)
    srv.daemon_threads = True
    print("ornithopter v%s on http://%s:%d  override=%s  upstream=%s  "
          "key=%s" % (__version__, args.host, args.port, cfg["override"],
                      cfg["upstream"],
                      "set" if cfg["key"] else "missing"), flush=True)
    launch_active = bool(args.launch or args.launch_target)
    if launch_active:
        serving = threading.Thread(target=srv.serve_forever, daemon=True)
        serving.start()
        if wait_healthy(args.host, args.port):
            launch_app(args.launch_target or "claude")
        else:
            print("proxy did not become healthy; not launching.",
                  file=sys.stderr)
    try:
        if launch_active:
            threading.Event().wait()
        else:
            srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main() or 0)
