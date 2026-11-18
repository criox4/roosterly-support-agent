#!/usr/bin/env python3
"""Local demo UI for the support agent: uv run ui.py, then open http://127.0.0.1:8000

Runs the same LangGraph pipeline as agent.py. "Decide" is a dry run (nothing posted);
"Decide & post" goes through the same checkpointed, re-check-before-post path as agent.py.
Standard library only; binds to localhost.
"""
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

import agent
import graph
from helpdesk import ApiError, Helpdesk
from safety import redact, redact_ticket

ROOT = Path(__file__).parent
agent.load_env()
HD = Helpdesk()
DRY = graph.build()
SAVER = SqliteSaver(sqlite3.connect(ROOT / "checkpoints.db", check_same_thread=False))
LIVE = graph.build(helpdesk=HD, checkpointer=SAVER)
POST_LOCK = threading.Lock()  # one posting run at a time, like the CLI


def trace(state, nodes, secs, resumed=False):
    body = graph.payload(state) if state.get("action") else {}
    return {"nodes": nodes, "action": state.get("action"), "payload": body,
            "overrides": state.get("overrides") or [], "samples": state.get("samples") or [],
            "triage": state.get("triage") or {}, "draft_reply": state.get("draft_reply"),
            "check": state.get("check"), "outcome": state.get("outcome", "dry_run"),
            "thread": [t["id"] for t in state.get("thread") or []],
            "cost": round(state.get("cost") or 0, 6), "secs": secs, "resumed": resumed}


def decide(ticket, post):
    tickets = [redact_ticket(t) for t in HD.all_tickets()] if (post or ticket.get("id", "").startswith("T")) else []
    t = redact_ticket(ticket)
    started = time.monotonic()
    if not post:
        state, nodes = agent.execute(DRY, {"ticket": t, "thread": agent.thread_for(t, tickets)})
        return trace(state, nodes, round(time.monotonic() - started, 1))
    with POST_LOCK:
        state, resumed, nodes = agent.run_ticket(LIVE, SAVER, t, agent.thread_for(t, tickets))
        secs = round(time.monotonic() - started, 1)
        rec = agent.log_record("ui", state, state.get("outcome"), secs, resumed)
        (ROOT / "logs").mkdir(exist_ok=True)
        with (ROOT / "logs" / "agent.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return trace(state, nodes, secs, resumed)


def latest_eval():
    files = sorted((ROOT / "eval" / "results").glob("*.json"), key=lambda p: p.stat().st_mtime)
    return [json.loads(p.read_text())["summary"] for p in files]


class Handler(BaseHTTPRequestHandler):
    def _json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        try:
            if self.path == "/":
                raw = (ROOT / "ui.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/api/tickets":
                self._json([redact_ticket(t) for t in HD.all_tickets()])
            elif self.path == "/api/examples":
                self._json([redact_ticket(e) for e in HD.examples()])
            elif self.path == "/api/status":
                self._json({**HD.call("GET", "/status"), "evals": latest_eval(),
                            "model": graph.model(0).model_name})
            elif self.path == "/api/graph":
                self._json({"mermaid": DRY.get_graph().draw_mermaid()})
            else:
                self._json({"error": "not_found"}, 404)
        except ApiError as e:
            self._json({"error": f"helpdesk {e.status}: {e.payload}"}, 502)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path not in ("/api/decide", "/api/post"):
                return self._json({"error": "not_found"}, 404)
            ticket = body.get("ticket") or {}
            if self.path == "/api/post" and not str(ticket.get("id", "")).startswith("T"):
                return self._json({"error": "only helpdesk tickets can be posted"}, 400)
            ticket.setdefault("id", "CUSTOM")
            self._json(decide(ticket, post=self.path == "/api/post"))
        except Exception as e:  # show the error in the UI, never a stack trace with ticket text
            self._json({"error": redact(f"{type(e).__name__}: {e}")[:400]}, 500)

    def log_message(self, fmt, *args):  # keep the console quiet (no ticket ids in access logs)
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("Support agent demo UI: http://127.0.0.1:8000  (Ctrl+C to stop)")
    server.serve_forever()
