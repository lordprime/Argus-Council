"""Static test site for the real-browser integration test (scripts/tests/browser-integration.sh).

Serves the same pages over http (port 80) and https (port 443, with the test CA's certificate)
and appends every request it receives, whatever the method, to $SITE_LOG_DIR/requests.jsonl, so
the test can tell whether a form POST ever left the browser. Standard library only.
"""

from __future__ import annotations

import json
import os
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = os.environ.get("SITE_NAME", "site")
LOG_DIR = Path(os.environ.get("SITE_LOG_DIR", "/log"))
TLS_DIR = Path(os.environ.get("SITE_TLS_DIR", "/tls"))
# The download test looks for these on the browser's disk: the file name and a body-only token.
DOWNLOAD = {"filename": f"argus-dl-{secrets.token_hex(8)}.bin", "body_token": secrets.token_hex(16)}
_lock = threading.Lock()

PAGES = {
    "/": f"""<!doctype html><title>Argus test site ({NAME})</title>
<h1>Argus test site</h1>
<p>{"CANARY-REACHED: the browser loaded a private address" if NAME == "canary" else "Welcome."}</p>
<p><a href="/next.html">Next page</a></p>""",
    "/next.html": """<!doctype html><title>Second page</title>
<h1>Second page</h1><p><a href="/">Back home</a></p>""",
    "/form.html": """<!doctype html><title>Prefilled form</title>
<h1>Prefilled form</h1>
<form method="post" action="/submit">
  <input type="hidden" name="secret" value="prefilled-secret">
  <input name="q" value="prefilled text" aria-label="Query">
  <button type="submit">Send form</button>
</form>
<p><button id="control" onclick="fetch('/control-post', {method: 'POST', body: 'x'})
  .then(() => { document.title = 'control sent'; })">Script POST</button></p>""",
    "/sw.html": """<!doctype html><title>Service worker form</title>
<h1>Service worker form</h1>
<p id="status">sw: starting</p>
<form method="post" action="/sw-submit">
  <input type="hidden" name="secret" value="prefilled-secret">
  <button type="submit">Send via worker</button>
</form>
<p><button onclick="fetch('/sw-control-post', {method: 'POST', body: 'x'})">
  Script POST via worker</button></p>
<script>
const status = document.getElementById('status');
if (!('serviceWorker' in navigator)) {
  status.textContent = 'sw: unsupported';
} else {
  navigator.serviceWorker.register('/sw.js', {scope: '/'})
    .then(() => navigator.serviceWorker.ready).then(() => {
    if (!navigator.serviceWorker.controller && !sessionStorage.getItem('reloaded')) {
      sessionStorage.setItem('reloaded', '1');
      location.reload();
      return;
    }
    status.textContent = 'sw: active; controlled: ' + (navigator.serviceWorker.controller ? 'yes' : 'no');
  }).catch((err) => { status.textContent = 'sw: failed ' + err; });
}
</script>""",
    "/download.html": """<!doctype html><title>Download</title>
<h1>Download</h1><p><a href="/files/report.bin">Download report</a></p>""",
}

SERVICE_WORKER = """self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
// Pass every request through, POSTs included: a navigation it answers would skip the page's
// own request interception unless the browser bypasses service workers.
self.addEventListener('fetch', (e) => e.respondWith(fetch(e.request)));
"""


def record(handler: BaseHTTPRequestHandler, body_len: int) -> None:
    entry = {
        "t": time.time(),
        "site": NAME,
        "method": handler.command,
        "path": handler.path,
        "host": handler.headers.get("Host", ""),
        "body_len": body_len,
        "tls": isinstance(handler.connection, ssl.SSLSocket),
    }
    with _lock, open(LOG_DIR / "requests.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "argus-test-site"
    sys_version = ""

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self, body_len: int) -> None:
        record(self, body_len)
        path = self.path.split("?", 1)[0]
        if self.command not in ("GET", "HEAD"):
            # Answer, so that a POST that did get through is plainly visible in the page too.
            self._send(200, b"<!doctype html><title>received</title><p>POST RECEIVED</p>", "text/html")
        elif path in PAGES:
            self._send(200, PAGES[path].encode(), "text/html; charset=utf-8")
        elif path == "/sw.js":
            self._send(
                200, SERVICE_WORKER.encode(), "application/javascript", {"Service-Worker-Allowed": "/"}
            )
        elif path == "/files/report.bin":
            body = (DOWNLOAD["body_token"] + "\n").encode() * 64
            disposition = f'attachment; filename="{DOWNLOAD["filename"]}"'
            self._send(200, body, "application/octet-stream", {"Content-Disposition": disposition})
        else:
            self._send(404, b"not found", "text/plain")

    def do_GET(self) -> None:
        self._route(0)

    def do_HEAD(self) -> None:
        self._route(0)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(min(length, 1 << 20))
        self._route(length)

    do_PUT = do_PATCH = do_DELETE = do_POST

    def log_message(self, format: str, *args: object) -> None:
        print(f"{NAME}: {self.address_string()} {format % args}", flush=True)


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "download.json").write_text(json.dumps(DOWNLOAD))
    http_port = int(os.environ.get("SITE_HTTP_PORT", "80"))
    https_port = int(os.environ.get("SITE_HTTPS_PORT", "443"))
    plain = ThreadingHTTPServer(("0.0.0.0", http_port), Handler)  # noqa: S104 - test network only
    tls = ThreadingHTTPServer(("0.0.0.0", https_port), Handler)  # noqa: S104
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(TLS_DIR / "site.pem", TLS_DIR / "site-key.pem")
    tls.socket = ctx.wrap_socket(tls.socket, server_side=True)
    threading.Thread(target=plain.serve_forever, daemon=True).start()
    print(f"{NAME}: serving on :{http_port} (http) and :{https_port} (https)", flush=True)
    tls.serve_forever()


if __name__ == "__main__":
    main()
