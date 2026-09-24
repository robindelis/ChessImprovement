"""Shared helpers for FIXER-U probes. Evidence-only; never imported by the app."""
import os
import sys


def _repo():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ensure_repo_on_path():
    repo = _repo()
    if repo not in sys.path:
        sys.path.insert(0, repo)
    return repo


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(port, method, path, body=None, headers=None, timeout=30):
    """Minimal raw-socket HTTP/1.1 request. No third-party client and no
    TestClient machinery: this is what actually goes over the wire."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, data, dict(resp.getheaders())
    finally:
        conn.close()


def post_routes(app):
    """(path, methods) for every /api route that can accept a POST."""
    out = []
    for route in app.router.routes:
        methods = getattr(route, "methods", None) or set()
        if "POST" not in methods:
            continue
        path = getattr(route, "path", None)
        if not path or not path.startswith("/api/"):
            continue
        if path.startswith("/api/{unmatched"):
            continue
        out.append((path, sorted(methods)))
    return sorted(out)
