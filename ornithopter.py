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
# NOTE: --upstream is deliberately NOT validated. Any Zen model ID works
# here — including muse-spark and other non-Claude models — the proxy
# just rewrites the name and forwards. If the ID doesn't exist upstream
# Zen itself returns the error.


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
    p.add_argument("--upstream", default="",
                   help="Real Zen model id to forward to. "
                        "Default: same as --override.")
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
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address. (default: %(default)s)")
    p.add_argument("--port", type=int, default=8646,
                   help="Bind port. (default: %(default)s)")
    return p.parse_args(argv)


INI_NAME = "ornithopter.ini"
INI_SECTION = "ornithopter"
# Option keys persisted to the ini (never the API key).
INI_KEYS = ("override", "upstream", "fallback", "launch", "launch_target",
            "host", "port")


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

    for key in ("override", "upstream", "fallback", "launch_target", "host"):
        if not given(key) and ini.get(key):
            setattr(args, key, ini.get(key))
    if not given("port") and ini.get("port"):
        try:
            args.port = int(ini.get("port") or 8646)
        except ValueError:
            pass
    if not given("launch") and ini.getboolean("launch", fallback=False):
        args.launch = True
    return args


def save_ini(args, cfg):
    """Persist the effective options (never the key) next to the script."""
    cp = configparser.ConfigParser()
    cp[INI_SECTION] = {
        "override": cfg["override"],
        "upstream": args.upstream,
        "fallback": args.fallback,
        "launch": str(bool(args.launch or args.launch_target)),
        "launch_target": args.launch_target,
        "host": args.host,
        "port": str(args.port),
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
            models = [{"id": cfg["override"], "object": "model",
                       "created": 0, "owned_by": "anthropic"}]
            try:
                req = urllib.request.Request(
                    cfg["upstream_base"] + "/models",
                    headers={"User-Agent": "curl/8.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    for m in json.loads(r.read())["data"]:
                        if m["id"] != cfg["override"]:
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
            except ValueError:
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
            return req

        def _relay(self, upstream):
            self.send_response(upstream.status)
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
                self.wfile.write(data)

        def _passthrough_error(self, e):
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _forward(self, upstream_path):
            raw = self._read_body()
            try:
                payload = json.loads(raw.decode() or "{}")
                spoofed = payload.get("model") == cfg["override"]
            except Exception:
                payload, spoofed = None, False  # non-JSON: forward untouched
            # Spoofed requests walk primary + fallbacks in order; anything
            # else goes through once, untouched. Failover only happens
            # before a response starts — once upstream returns 200 we commit.
            attempts = ([cfg["upstream"]] + cfg["fallbacks"]
                        if spoofed else [None])
            last_error = "no attempts made"
            for i, attempt in enumerate(attempts):
                body = raw
                if payload is not None and attempt is not None:
                    # Rewrite the spoofed name to this attempt's Zen id.
                    payload["model"] = attempt
                    body = json.dumps(payload).encode()
                try:
                    self._relay(urllib.request.urlopen(
                        self._make_request(upstream_path, body), timeout=300))
                    return
                except urllib.error.HTTPError as e:
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
           "key": args.key.strip()}
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
    print("ornithopter on http://%s:%d  override=%s  upstream=%s  "
          "key=%s" % (args.host, args.port, cfg["override"],
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
    main()
