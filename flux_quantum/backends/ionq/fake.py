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

It listens on the loopback. On an instance of more than one node, jobs run
where the scheduler puts them, so --bind 0.0.0.0 listens on every interface
and the URL printed is the address other nodes reach this one at.

FAKE_IONQ_NO_SESSIONS=1 refuses POST /sessions with 404, which is what an
account without the beta sees. FAKE_IONQ_STEPS is how many GETs a job spends
in each state before moving on, default 1. FAKE_IONQ_SECONDS is the least
time a job spends in each state, default 0, for timings that look like a
device's.

FAKE_IONQ_QUEUE is the device's public queue: the seconds a job sits in
submitted before it is served, a number or a range like 30-90 drawn per job.
A job in a session that has started waits FAKE_IONQ_SESSION_QUEUE instead,
the session queue, 0 by default as the fake campaign ran. IonQ's sessions
do not skip the queue: their jobs go into a separate queue with about half
the device's capacity and wait a minute or two where the public queue is
hours, so a faithful run sets both, say FAKE_IONQ_QUEUE=300 and
FAKE_IONQ_SESSION_QUEUE=1-2, at whatever scale fits the time available. /backends reports the mean as average_queue_time. The queue
can be changed while running, POST /fake/config {"queue": "30-90"}, so an
experiment can sweep it.

FAKE_IONQ_SESSION_START says how a session reaches started. job, the default
and what the fake campaign ran with, starts it when its first job starts.
submit is what IonQ does: the session is started the moment its first job
is submitted, and the job then waits its turn, so a scout that trusts the
session's status releases too early. queue starts the session by itself,
one queue wait after it is created, with no job in it, which IonQ's do not;
it models a service whose sessions queue alone.
"""

import argparse
import datetime
import json
import os
import random
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

ORDER = ["submitted", "ready", "started", "completed"]


def _stamp(t=None):
    return datetime.datetime.fromtimestamp(
        time.time() if t is None else t, datetime.timezone.utc
    ).isoformat()


def parse_queue(spec):
    """A queue spec as (low, high) seconds: "30" is 30 to 30, "30-90" a range,
    nothing is no queue."""
    if spec is None:
        return (0.0, 0.0)
    spec = str(spec).strip()
    if not spec:
        return (0.0, 0.0)
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        lo, hi = float(lo), float(hi)
        return (min(lo, hi), max(lo, hi))
    return (float(spec), float(spec))


class State:
    def __init__(
        self,
        no_sessions=False,
        steps=1,
        seconds=0.0,
        queue=None,
        session_start="job",
        session_queue=None,
    ):
        self.jobs = {}
        self.sessions = {}
        self.no_sessions = no_sessions
        self.steps = max(1, int(steps))
        self.seconds = float(seconds)
        self.queue = parse_queue(queue)
        self.session_queue = parse_queue(session_queue)
        self.session_start = session_start or "job"
        if self.session_start not in ("job", "submit", "queue"):
            raise ValueError(
                "session_start is job, submit or queue, not %r" % self.session_start
            )
        self.requests = []
        self.lock = threading.Lock()

    def queue_wait(self, which=None):
        lo, hi = which if which is not None else self.queue
        return random.uniform(lo, hi) if hi > lo else lo

    def config(self):
        return {
            "queue": "%g-%g" % self.queue,
            "session_queue": "%g-%g" % self.session_queue,
            "seconds": self.seconds,
            "steps": self.steps,
            "no_sessions": self.no_sessions,
            "session_start": self.session_start,
        }

    def configure(self, body):
        """Change the service while it runs, for a sweep. Jobs already
        submitted keep the wait they were given."""
        if "queue" in body:
            self.queue = parse_queue(body["queue"])
        if "session_queue" in body:
            self.session_queue = parse_queue(body["session_queue"])
        if "seconds" in body:
            self.seconds = float(body["seconds"])
        if "steps" in body:
            self.steps = max(1, int(body["steps"]))
        if "no_sessions" in body:
            self.no_sessions = bool(body["no_sessions"])
        if "session_start" in body:
            if body["session_start"] not in ("job", "submit", "queue"):
                return 400, {"error": "session_start is job, submit or queue"}
            self.session_start = body["session_start"]
        return 200, self.config()

    def create_job(self, body):
        jid = str(uuid.uuid4())
        session = body.get("session_id")
        if session and session not in self.sessions:
            return 404, {"error": "session not found"}
        if session:
            self._expire(session)
            self._start(session)
            if self.sessions[session]["status"] == "ended":
                return 400, {"error": "session has ended"}
        # the device's queue, unless a started session already has the device
        served = bool(session and self.sessions[session]["active"])
        # IonQ marks the session started on its first submission. The job
        # still waits its turn, so this does not make it served
        if session and self.session_start == "submit":
            sess = self.sessions[session]
            if sess["status"] == "created":
                sess["status"], sess["started_at"] = "started", _stamp()
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
            "queue": (
                self.queue_wait(self.session_queue) if served else self.queue_wait()
            ),
        }
        return 200, {"id": jid, "status": "submitted", "session_id": session}

    def get_job(self, jid):
        job = self.jobs.get(jid)
        if job is None:
            return 404, {"error": "job not found"}
        if job["status"] in ORDER and job["status"] != "completed":
            job["polls"] += 1
            least = self.seconds
            if job["status"] == "submitted":
                least = max(least, job["queue"])
            if job["polls"] >= self.steps and time.time() - job["since"] >= least:
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
        return 200, {
            k: v for k, v in job.items() if k not in ("polls", "since", "queue")
        }

    def job_cost(self, jid):
        """GET /jobs/{id}/cost, shaped like the service's: the estimate and,
        once the job ran, what was charged. Dry runs charge nothing."""
        job = self.jobs.get(jid)
        if job is None:
            return 404, {"error": "job not found"}
        # the service answers with only dry_run for a job that cost nothing
        if job.get("backend") == "simulator":
            return 200, {"dry_run": False}
        est = self.estimate({"shots": job.get("shots") or 1})[1]["estimated_total_cost"]
        charged = est if job["status"] == "completed" else 0.0
        return 200, {
            "dry_run": False,
            "estimated_cost": {"value": est, "unit": "usd"},
            "cost": {"value": charged, "unit": "usd"},
        }

    def list_jobs(self, params):
        """GET /jobs, newest first, with the service's paging keys."""
        limit = int(params.get("limit", 25))
        jobs = sorted(self.jobs.values(), key=lambda j: j["submitted_at"], reverse=True)
        view = [
            {k: v for k, v in j.items() if k not in ("polls", "since", "queue")}
            for j in jobs[:limit]
        ]
        return 200, {"jobs": view, "next": None}

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
        settings = body.get("settings") or {}
        minutes = settings.get("duration_limit_min")
        expires = time.time() + 60 * float(minutes) if minutes else None
        self.sessions[sid] = {
            "id": sid,
            "backend": body.get("backend"),
            "settings": settings,
            "status": "created",
            "active": False,
            "created_at": _stamp(),
            "started_at": None,
            "ended_at": None,
            "expires_at": _stamp(expires) if expires else None,
            "expires": expires,
            # when the session starts by itself, under session_start=queue
            "starts": (
                time.time() + self.queue_wait()
                if self.session_start == "queue"
                else None
            ),
        }
        return 200, self._session_view(sid)

    def _session_view(self, sid):
        return {
            k: v
            for k, v in self.sessions[sid].items()
            if k not in ("expires", "starts")
        }

    def _start(self, sid):
        """A session queued for the device starts once its wait is up."""
        s = self.sessions.get(sid)
        if (
            s
            and s["status"] == "created"
            and s.get("starts") is not None
            and time.time() >= s["starts"]
        ):
            s["active"], s["status"], s["started_at"] = True, "started", _stamp()

    def _expire(self, sid):
        """A session past its duration limit ends, as the service's does."""
        s = self.sessions.get(sid)
        if (
            s
            and s["expires"]
            and s["status"] != "ended"
            and time.time() >= s["expires"]
        ):
            self.end_session(sid)

    def get_session(self, sid):
        if sid not in self.sessions:
            return 404, {"error": "session not found"}
        self._expire(sid)
        self._start(sid)
        return 200, self._session_view(sid)

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
        return 200, self._session_view(sid)

    # rates for GET /jobs/estimate, shaped like the service's answer. The
    # minimum is what a direct account was quoted for a small circuit, and
    # a small circuit never gets above it, so that is the price of a job
    RATE = {"cost_model": "2QGE_operations", "job_cost_minimum": 25.79, "fake": True}

    def estimate(self, params):
        shots = int(params.get("shots", 1))
        # the simulator is free; a small circuit on a QPU is the minimum
        cost = (
            0.0
            if params.get("backend") == "simulator"
            else self.RATE["job_cost_minimum"]
        )
        return 200, {
            "input_values": {
                "backend": params.get("backend"),
                "type": "ionq.circuit.v1",
                "qubits": int(params.get("qubits", 1)),
                "shots": shots,
                "1q_gates": int(params.get("1q_gates", 0)),
                "2q_gates": int(params.get("2q_gates", 0)),
                "error_mitigation": params.get("error_mitigation", "false"),
            },
            "estimated_at": _stamp(),
            "estimated_total_cost": round(cost, 4),
            "cost_unit": "usd",
            "rate_information": dict(self.RATE),
        }

    def backend(self, name):
        lo, hi = self.queue
        if hi > 0:
            queue_time = (lo + hi) / 2
        else:
            queue_time = 0 if name == "simulator" else 600
        return 200, {
            "backend": name,
            "status": "available",
            "degraded": False,
            "qubits": 36,
            "average_queue_time": queue_time,
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
            path, _, query = self.path.partition("?")
            parts = [p for p in path.split("/") if p]
            params = {k: v[0] for k, v in parse_qs(query).items()}
            body = self._body() if method in ("POST", "PUT") else {}
            if parts in (["jobs", "estimate"], ["jobs"]) and method == "GET":
                body = params
            with state.lock:
                state.requests.append((method, "/" + "/".join(parts), body))
                code, payload = self._dispatch(method, parts, body)
            self._reply(code, payload)

        def _dispatch(self, method, parts, body):
            if parts[:1] == ["backends"] and len(parts) == 2 and method == "GET":
                return state.backend(parts[1])
            if parts == ["jobs"] and method == "POST":
                return state.create_job(body)
            if parts == ["jobs", "estimate"] and method == "GET":
                return state.estimate(body)
            if parts == ["jobs"] and method == "GET":
                return state.list_jobs(body)
            if parts[:1] == ["jobs"] and parts[2:] == ["cost"] and method == "GET":
                return state.job_cost(parts[1])
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
            # not IonQ's: the fake's own knobs, for a sweep
            if parts == ["fake", "config"] and method == "GET":
                return 200, state.config()
            if parts == ["fake", "config"] and method == "POST":
                return state.configure(body)
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


def serve(port=0, state=None, bind="127.0.0.1"):
    """Start serving in a thread. Returns (server, state). The port is
    server.server_address[1], which matters when 0 was asked for."""
    state = state or State(
        no_sessions=bool(os.environ.get("FAKE_IONQ_NO_SESSIONS")),
        steps=os.environ.get("FAKE_IONQ_STEPS", 1),
        seconds=os.environ.get("FAKE_IONQ_SECONDS", 0),
        queue=os.environ.get("FAKE_IONQ_QUEUE"),
        session_queue=os.environ.get("FAKE_IONQ_SESSION_QUEUE"),
        session_start=os.environ.get("FAKE_IONQ_SESSION_START") or "job",
    )
    server = HTTPServer((bind, port), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


def advertised(bind):
    """The address to publish for a bind address. The loopback and a real
    address are themselves. 0.0.0.0 is every interface, so publish the one
    the default route leaves by, which is what the other nodes reach."""
    if bind not in ("0.0.0.0", ""):
        return bind
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # no packet is sent, this only picks the interface for the route
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--bind",
        default="127.0.0.1",
        help="address to listen on. 0.0.0.0 for every interface, so other "
        "nodes of the instance reach it (default: the loopback)",
    )
    args = ap.parse_args()
    server, state = serve(args.port, bind=args.bind)
    print(
        "fake ionq listening on http://%s:%d"
        % (advertised(args.bind), server.server_address[1]),
        flush=True,
    )
    print(
        "fake ionq: queue %ss, session queue %ss, %gs per state, %d poll(s) per state, sessions start on %s%s"
        % (
            state.config()["queue"],
            state.config()["session_queue"],
            state.seconds,
            state.steps,
            state.session_start,
            ", no sessions" if state.no_sessions else "",
        ),
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
