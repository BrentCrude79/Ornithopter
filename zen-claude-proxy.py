#!/usr/bin/env python3
"""zen-claude-proxy: local Anthropic-compatible front for OpenCode Zen.

Exposes Zen models on 127.0.0.1 under official Anthropic model names so
Claude Code (and any Anthropic SDK client) accepts it as a local
inference point. Stdlib only — runs on Windows, macOS, Linux.

Examples:
  python zen-claude-proxy.py --key sk-zen-... --port 8646
  python zen-claude-proxy.py --key sk-zen-... --override claude-opus-4-5
  python zen-claude-proxy.py --key sk-zen-... --override claude-sonnet-4-5 --upstream claude-sonnet-4-5

Then point Claude Code at it:
  set ANTHROPIC_BASE_URL=http://127.0.0.1:8646
  set ANTHROPIC_AUTH_TOKEN=dummy
  (model name on the client must match --override)

Flow: client sends model=<override> -> proxy rewrites to the real Zen
model id -> forwards to <upstream-base>/messages with your Zen key ->
streams the upstream response back untouched.
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

ANTHROPIC_CATALOG = [
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-sonnet-4-5",
    "claude-sonnet-4-0",
    "claude-haiku-4-5",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="zen-claude-proxy",
        description="Local Anthropic-compatible proxy for OpenCode Zen "
                    "with spoofable model names for Claude Code.",
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
    p.add_argument("--host", default="127.0.0.1",
                   help="Bind address. (default: %(default)s)")
    p.add_argument("--port", type=int, default=8646,
                   help="Bind port. (default: %(default)s)")
    return p.parse_args(argv)


def make_handler(cfg):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        server_version = "zen-claude-proxy/1.0"
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
                                 "upstream": cfg["upstream"]})
            else:
                self._send_json({"error": "not found"}, 404)

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            return self.rfile.read(length) if length else b""

        def _forward(self, upstream_path):
            raw = self._read_body()
            # Rewrite the spoofed name to the real Zen model id.
            try:
                payload = json.loads(raw.decode() or "{}")
                if payload.get("model") == cfg["override"]:
                    payload["model"] = cfg["upstream"]
                raw = json.dumps(payload).encode()
            except Exception:
                pass  # non-JSON (shouldn't happen) -> forward untouched
            req = urllib.request.Request(
                cfg["upstream_base"] + upstream_path, data=raw, method="POST")
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
            try:
                upstream = urllib.request.urlopen(req, timeout=300)
                self.send_response(upstream.status)
                ctype = upstream.headers.get("Content-Type",
                                             "application/json")
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
            except urllib.error.HTTPError as e:
                data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send_json({"error": "upstream unreachable",
                                 "detail": str(e)}, 502)

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
    if args.override not in ANTHROPIC_CATALOG:
        print("warning: --override '%s' is not in the known Anthropic "
              "catalog; Claude Code may reject it. Known: %s"
              % (args.override, ", ".join(ANTHROPIC_CATALOG)),
              file=sys.stderr)
    cfg = {"override": args.override,
           "upstream": args.upstream or args.override,
           "upstream_base": args.upstream_base.rstrip("/"),
           "key": args.key.strip()}
    if not cfg["key"]:
        print("warning: no Zen key given (--key or ZEN_API_KEY); "
              "/v1/models will work but inference will 401 upstream.",
              file=sys.stderr)
    from http.server import HTTPServer
    handler = make_handler(cfg)
    srv = HTTPServer((args.host, args.port), handler)
    print("zen-claude-proxy on http://%s:%d  override=%s  upstream=%s  "
          "key=%s" % (args.host, args.port, cfg["override"],
                      cfg["upstream"],
                      "set" if cfg["key"] else "missing"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
