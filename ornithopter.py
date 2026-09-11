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

__version__ = "1.5.0"

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
    """Definitively free-tier marker: -free suffix, minus keyed twins."""
    m = (mid or "").lower()
    return m.endswith("-free") and m not in FREE_KEYED_TWINS


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
    p.add_argument("--key", default=os.environ.get("ZEN_API_KEY", ""),
                   help="Zen API key (or set ZEN_API_KEY env var). "
                        "Local clients may use any placeholder bearer.")
    p.add_argument("--override", default="claude-sonnet-4-5",
                   help="Model name advertised locally and accepted from "
                        "clients. Must be an official Anthropic catalog name "
                        "so Claude Code accepts it. "
                        "(default: %(default)s)")
    p.add_argument("--upstream", default="muse-spark-1.3-contributor-free",
                   help="Real Zen model id to forward to. Free-tier only "
                        "unless --allow-paid. "
                        "(default: %(default)s)")
    p.add_argument("--upstream-base", default="https://opencode.ai/zen/v1",
                   help="Upstream base URL. (default: %(default)s)")
    p.add_argument("--fallback", default="",
                   help="Comma-separated fallback Zen model IDs, first-last "
                        "priority (e.g. --fallback claude-sonnet-4-5,"
                        "muse-spark-1.3-contributor-free). On rate-limit "
                        "(429), 5xx, timeout, or connection error the "
                        "request is retried with the next ID. Any Zen ID "
                        "is allowed.")
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
                   help="Disable Messages->Responses translation: always "
                        "rename-and-forward untouched (for models that "
                        "speak /messages natively, or debugging).")
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
            "launch_target", "host", "port", "key")


def script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def ini_path():
    return os.path.join(script_dir(), INI_NAME)


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
                "launch_target", "host"):
        if not given(key) and ini.get(key):
            setattr(args, key, ini.get(key))
    # Key precedence: --key flag > ZEN_API_KEY env > ini.
    if not args.key and not given("key") and ini.get("key"):
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
            "stop_reason": stop,
            "usage": {"input_tokens": usage.get("input_tokens", 0) or 0,
                      "output_tokens": usage.get("output_tokens", 0) or 0}}


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
        return ("data: %s\n\n" % json.dumps(obj, separators=(",", ":"))).encode()

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
                            "usage": {"input_tokens": 0,
                                      "output_tokens": 0}}}))
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
                                 "usage": {
                                     "input_tokens": u.get("input_tokens",
                                                           0) or 0,
                                     "output_tokens": u.get("output_tokens",
                                                            0) or 0}}))
            out.append(self._ev({"type": "message_stop"}))
            out.append(b"data: [DONE]\n\n")
            self.done = True
        elif t in ("response.failed", "response.incomplete"):
            out.append(self._ev({"type": "message_delta",
                                 "delta": {"stop_reason": "end_turn",
                                           "stop_sequence": None},
                                 "usage": {"input_tokens": 0,
                                           "output_tokens": 0}}))
            out.append(self._ev({"type": "message_stop"}))
            out.append(b"data: [DONE]\n\n")
            self.done = True
        return out

    def finish(self):
        if self.done:
            return []
        self.done = True
        return [self._ev({"type": "message_delta",
                          "delta": {"stop_reason": "end_turn",
                                    "stop_sequence": None},
                          "usage": {"input_tokens": 0, "output_tokens": 0}}),
                self._ev({"type": "message_stop"}),
                b"data: [DONE]\n\n"]


def make_handler(cfg):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "ornithopter/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send_json(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _models_payload(self):
            # Advertise the override name; merge the live upstream catalog
            # (public endpoint) so OpenAI-style clients see everything too.
            # Free-only mode filters the merged list to free-tier IDs so
            # clients never discover (or bill) a paid model.
            models = [{"id": cfg["override"], "object": "model",
                       "created": 0, "owned_by": "anthropic"}]
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
                        models.append(m)
            except Exception:
                pass
            return {"object": "list", "data": models}

        def do_GET(self):
            if self.path in ("/v1/models", "/models"):
                self._send_json(self._models_payload())
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

        def _passthrough_error(self, e):
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _relay_translated(self, req, req_model, stream):
            """Relay a Responses-API upstream, converting to Anthropic
            Messages shape (object or SSE stream)."""
            upstream = urllib.request.urlopen(req, timeout=300)
            if not stream:
                resp = json.loads(upstream.read().decode())
                body = json.dumps(responses_to_anthropic(resp,
                                                         req_model)).encode()
                self.send_response(200)
                self.send_header("Connection", "close")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_response(200)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            tr = ResponsesStreamToAnthropic(req_model)
            try:
                buf = b""
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
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
                translate = False
                try:
                    body = raw
                    if payload is not None and attempt is not None:
                        if (upstream_path.endswith("/messages")
                                and not cfg.get("direct")
                                and needs_responses_transport(attempt)):
                            # Model lives on the Responses API: translate the
                            # whole request instead of just renaming it.
                            rbody = anthropic_to_responses(payload, attempt)
                            body = json.dumps(rbody).encode()
                            use_path = "/responses"
                            translate = True
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
                    print("translated messages->responses for %s" % tag,
                          flush=True, file=sys.stderr)
                try:
                    req = self._make_request(use_path, body)
                    if translate:
                        self._relay_translated(
                            req, req_name,
                            bool(payload.get("stream"))
                            if isinstance(payload, dict) else False)
                    else:
                        self._relay(urllib.request.urlopen(req, timeout=300))
                    if cfg.get("verbose"):
                        print("upstream %s -> 200" % tag, flush=True,
                              file=sys.stderr)
                    return
                except urllib.error.HTTPError as e:
                    print("upstream %s -> HTTP %s" % (tag, e.code),
                          flush=True, file=sys.stderr)
                    if ((e.code == 429 or e.code >= 500)
                            and i < len(attempts) - 1):
                        try:
                            last_error = "%s: %s" % (e.code, e.read()[:200])
                        except Exception:
                            last_error = "HTTP %s" % e.code
                        continue  # rate-limited / sick: try next candidate
                    self._passthrough_error(e)
                    return
                except Exception as e:
                    print("upstream %s -> error: %s" % (tag, e),
                          flush=True, file=sys.stderr)
                    last_error = str(e)  # timeout/refused: try next
                    continue
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
    cfg = {"override": args.override,
           "upstream": args.upstream or args.override,
           "upstream_base": args.upstream_base.rstrip("/"),
           "fallbacks": [f.strip() for f in args.fallback.split(",")
                         if f.strip()],
           "free_only": not args.allow_paid,
           "direct": args.direct,
           "verbose": args.verbose,
           "key": args.key.strip()}
    if cfg["free_only"]:
        live = fetch_live_ids(cfg["upstream_base"])
        if live is None:
            print("warning: could not verify the live catalog; checking "
                  "-free suffix only.", file=sys.stderr)
        problems = []
        for m in [cfg["upstream"]] + cfg["fallbacks"]:
            if live is not None and m not in live:
                problems.append("%s: not in the live Zen catalog" % m)
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
    from http.server import HTTPServer
    handler = make_handler(cfg)
    if args.save:
        save_ini(args, cfg)
    srv = HTTPServer((args.host, args.port), handler)
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
