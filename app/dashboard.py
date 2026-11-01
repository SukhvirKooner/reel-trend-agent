"""Live dashboard with zero extra dependencies — the evidence report, rebuilt from the database on every
request; the page refreshes its content in place every 30 s (keeps your tab and scroll position).

    python -m app.dashboard                  # http://localhost:8080   (?db=street_food | ?db=concert)
    python -m app.dashboard --port 8090
"""
import argparse
import http.server
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import db, report, settings


def sources():
    """(key, label, path) for live niche databases only (no demo / frozen backups).

    The active niche uses settings.DB_PATH so tests / DB_PATH=… overrides still work;
    other registered niches are listed from config/niches.yaml.
    """
    out = []
    niches = settings.list_niches()
    active = settings.ACTIVE_NICHE if settings.ACTIVE_NICHE in niches else next(iter(niches))
    out.append((active, f"{niches[active]['label']} · live", Path(settings.DB_PATH)))
    seen = {Path(settings.DB_PATH).resolve()}
    for name, spec in niches.items():
        p = Path(spec["db"]).resolve()
        if name == active or p in seen:
            continue
        out.append((name, f"{spec['label']} · live", Path(spec["db"])))
        seen.add(p)
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        srcs = sources()
        want = (parse_qs(u.query).get("db") or [settings.default_niche_name()])[0]
        key, _, path = next((s for s in srcs if s[0] == want), srcs[0])
        try:
            conn = db.connect(Path(path))
            _, niche_cfg = settings.niche_for_db(path)
            page = report.build(conn, Path(path).name, live=True,
                                sources=[(k, lab) for k, lab, _ in srcs] if len(srcs) > 1 else None,
                                current=key, niche_cfg=niche_cfg, db_path=path)
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
    settings.add_niche_arg(ap)
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    if a.niche:
        settings.apply_niche(a.niche)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Dashboard: http://localhost:{a.port}   sources: {', '.join(k for k, _, _ in sources())}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
