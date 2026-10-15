"""A tiny local web server that pretends to be an outside API (Groq, Pixabay, Wikimedia).

    server = FakeAPI(lambda method, path, query, body: (200, {"hits": []}))
    ... call server.url ...
    server.calls  -> every request seen: {"method", "path", "query", "headers", "body"}
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeAPI:
    def __init__(self, answer, delay: float = 0.0):
        """answer(method, path, query dict, body bytes) -> (status, JSON-able body, or bytes)."""
        self.calls: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self, method):
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
                fake.calls.append({"method": method, "path": parts.path, "query": query,
                                   "headers": dict(self.headers), "body": body})
                if delay:
                    time.sleep(delay)
                status, payload = answer(method, parts.path, query, body)
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
