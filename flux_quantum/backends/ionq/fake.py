#!/usr/bin/env python3
"""A stand-in for api.ionq.co, so the IonQ backend runs with no key.

Implements the handful of v0.4 calls the backend makes: backends, jobs,
cancel, sessions and end. Jobs advance one state per GET, submitted to ready
to started to completed, so a poll loop sees every state and nothing has to
sleep, and they carry the service's timestamps, submitted_at, started_at and
completed_at. A session goes created, started, ended, as the service's does,
started when a job inside it starts, and ending it cancels whatever is still
queued.

    python3 -m flux_quantum.backends.ionq.fake --port 8765
    IONQ_API_KEY=anything IONQ_API_URL=http://127.0.0.1:8765 ...

FAKE_IONQ_NO_SESSIONS=1 refuses POST /sessions with 404, which is what an
account without the beta sees. FAKE_IONQ_STEPS is how many GETs a job spends
in each state before moving on, default 1. FAKE_IONQ_SECONDS is the least
time a job spends in each state, default 0, for timings that look like a
device's.
"""

import argparse
import datetime
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

ORDER = ["submitted", "ready", "started", "completed"]


def _stamp(t=None):
    return datetime.datetime.fromtimestamp(
        time.time() if t is None else t, datetime.timezone.utc
    ).isoformat()


class State:
    def __init__(self, no_sessions=False, steps=1, seconds=0.0):
        self.jobs = {}
        self.sessions = {}
        self.no_sessions = no_sessions
        self.steps = max(1, int(steps))
        self.seconds = float(seconds)
        self.requests = []
        self.lock = threading.Lock()

    def create_job(self, body):
        jid = str(uuid.uuid4())
        session = body.get("session_id")
        if session and session not in self.sessions:
            return 404, {"error": "session not found"}
        self.jobs[jid] = {
            "id": jid,
            "status": "submitted",
            "backend": body.get("backend"),
            "shots": body.get("shots"),
            "session_id": session,
            "noise": body.get("noise"),
            "name": body.get("name"),
            "submitted_at": _stamp(),
            "started_at": None,
            "completed_at": None,
            "execution_duration_ms": None,
            "polls": 0,
            "since": time.time(),
        }
        return 200, {"id": jid, "status": "submitted", "session_id": session}

    def get_job(self, jid):
        job = self.jobs.get(jid)
        if job is None:
            return 404, {"error": "job not found"}
        if job["status"] in ORDER and job["status"] != "completed":
            job["polls"] += 1
            if (
                job["polls"] >= self.steps
                and time.time() - job["since"] >= self.seconds
            ):
                job["polls"] = 0
                job["since"] = time.time()
                job["status"] = ORDER[ORDER.index(job["status"]) + 1]
                if job["status"] == "started":
                    job["started_at"] = _stamp()
                    sess = self.sessions.get(job["session_id"])
                    if sess and not sess["active"]:
                        sess["active"] = True
                        sess["status"] = "started"
                        sess["started_at"] = _stamp()
                elif job["status"] == "completed":
                    job["completed_at"] = _stamp()
                    job["execution_duration_ms"] = int(max(1.0, self.seconds) * 1000)
        return 200, {k: v for k, v in job.items() if k not in ("polls", "since")}

    def cancel_job(self, jid):
        job = self.jobs.get(jid)
        if job is None:
            return 404, {"error": "job not found"}
        if job["status"] not in ("completed", "failed"):
            job["status"] = "canceled"
        return 200, {"id": jid, "status": "canceled"}

    def create_session(self, body):
        if self.no_sessions:
            return 404, {"error": "not found"}
        sid = str(uuid.uuid4())
        self.sessions[sid] = {
            "id": sid,
            "backend": body.get("backend"),
            "settings": body.get("settings") or {},
            "status": "created",
            "active": False,
            "created_at": _stamp(),
            "started_at": None,
            "ended_at": None,
        }
        return 200, dict(self.sessions[sid])

    def get_session(self, sid):
        s = self.sessions.get(sid)
        return (404, {"error": "session not found"}) if s is None else (200, dict(s))

    def end_session(self, sid):
        s = self.sessions.get(sid)
        if s is None:
            return 404, {"error": "session not found"}
        s["status"], s["active"], s["ended_at"] = "ended", False, _stamp()
        for job in self.jobs.values():
            if job["session_id"] == sid and job["status"] not in (
                "completed",
                "failed",
            ):
                job["status"] = "canceled"
        return 200, dict(s)

    def backend(self, name):
        return 200, {
            "backend": name,
            "status": "available",
            "degraded": False,
            "qubits": 36,
            "average_queue_time": 0 if name == "simulator" else 600,
        }


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw) if raw else {}

        def _reply(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, method):
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("apiKey ") or len(auth) <= len("apiKey "):
                return self._reply(401, {"error": "unauthorized"})
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            body = self._body() if method in ("POST", "PUT") else {}
            with state.lock:
                state.requests.append((method, "/" + "/".join(parts), body))
                code, payload = self._dispatch(method, parts, body)
            self._reply(code, payload)

        def _dispatch(self, method, parts, body):
            if parts[:1] == ["backends"] and len(parts) == 2 and method == "GET":
                return state.backend(parts[1])
            if parts == ["jobs"] and method == "POST":
                return state.create_job(body)
            if parts[:1] == ["jobs"] and len(parts) == 2 and method == "GET":
                return state.get_job(parts[1])
            if parts[:1] == ["jobs"] and parts[2:] == ["results"] and method == "GET":
                return 200, {"0": 0.5, "1": 0.5}
            if parts[:1] == ["jobs"] and parts[2:] == ["status", "cancel"]:
                return state.cancel_job(parts[1])
            if parts == ["sessions"] and method == "POST":
                return state.create_session(body)
            if parts[:1] == ["sessions"] and len(parts) == 2 and method == "GET":
                return state.get_session(parts[1])
            if parts[:1] == ["sessions"] and parts[2:] == ["end"] and method == "POST":
                return state.end_session(parts[1])
            return 404, {
                "error": "no such route: {} {}".format(method, "/".join(parts))
            }

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

    return Handler


def serve(port=0, state=None):
    """Start serving in a thread. Returns (server, state). The port is
    server.server_address[1], which matters when 0 was asked for."""
    state = state or State(
        no_sessions=bool(os.environ.get("FAKE_IONQ_NO_SESSIONS")),
        steps=os.environ.get("FAKE_IONQ_STEPS", 1),
        seconds=os.environ.get("FAKE_IONQ_SECONDS", 0),
    )
    server = HTTPServer(("127.0.0.1", port), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    server, _ = serve(args.port)
    print(
        "fake ionq listening on http://127.0.0.1:%d" % server.server_address[1],
        flush=True,
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
