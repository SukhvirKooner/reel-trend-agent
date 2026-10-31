"""Live dashboard with zero extra dependencies — the evidence report, rebuilt from the database on every
request; the page refreshes its content in place every 30 s (keeps your tab and scroll position).

    python -m app.dashboard                  # http://localhost:8080   (?db=live | ?db=demo | ?db=<backup>)
    python -m app.dashboard --port 8090
"""
import argparse
import http.server
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import db, report, settings


def sources():
    """(key, label, path) for every database the dashboard can show."""
    out = [("live", "Live data", settings.DB_PATH)]
    demo = settings.ROOT / "data" / "demo.db"
    if demo.exists() and demo.resolve() != Path(settings.DB_PATH).resolve():
        out.append(("demo", "Demo data", demo))
    for b in sorted((settings.ROOT / "backups").glob("*.db"))[-3:]:
        out.append((b.stem, f"Frozen {b.stem.replace('app-', '')}", b))
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        srcs = sources()
        want = (parse_qs(u.query).get("db") or ["live"])[0]
        key, _, path = next((s for s in srcs if s[0] == want), srcs[0])
        try:
            conn = db.connect(Path(path))
            page = report.build(conn, Path(path).name, live=True,
                                sources=[(k, lab) for k, lab, _ in srcs] if len(srcs) > 1 else None, current=key)
            conn.close()
            body, code = page.encode("utf-8"), 200
        except Exception:
            body, code = f"<pre>{traceback.format_exc()}</pre>".encode(), 500
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Dashboard: http://localhost:{a.port}   sources: {', '.join(k for k, _, _ in sources())}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
