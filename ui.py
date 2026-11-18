#!/usr/bin/env python3
"""Local demo UI for the support agent: uv run ui.py, then open http://127.0.0.1:8000

Runs the same LangGraph pipeline as agent.py. "Decide" is a dry run (nothing posted);
"Decide & post" goes through the same checkpointed, re-check-before-post path as agent.py.
Standard library only; binds to localhost.
"""
import inspect
import json
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

import agent
import graph
import helpdesk
import safety
from helpdesk import ApiError, Helpdesk
from safety import redact, redact_ticket

ROOT = Path(__file__).parent
agent.load_env()
HD = Helpdesk()
DRY = graph.build()
SAVER = SqliteSaver(sqlite3.connect(ROOT / "checkpoints.db", check_same_thread=False))
LIVE = graph.build(helpdesk=HD, checkpointer=SAVER)
POST_LOCK = threading.Lock()  # one posting run at a time, like the CLI
RUN = {"running": False, "mode": None, "lines": [], "exit": None, "proc": None}  # the "run all" job (one at a time)


def start_run(mode):
    """Run agent.py itself as a child process (same code path as the CLI) and collect its output."""
    args = {"dry": ["--dry-run"], "open": [], "reset": ["--reset"]}[mode]
    with POST_LOCK:
        if RUN["running"]:
            return False
        RUN.update(running=True, mode=mode, lines=[], exit=None)
    proc = RUN["proc"] = subprocess.Popen([sys.executable, "-u", "agent.py", *args], cwd=ROOT, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def pump():
        for line in proc.stdout:
            RUN["lines"].append(line.rstrip("\n"))  # agent.py only prints redacted text

        RUN.update(exit=proc.wait(), running=False)
    threading.Thread(target=pump, daemon=True).start()
    return True


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


def evals(rows=False):
    files = sorted((ROOT / "eval" / "results").glob("*.json"), key=lambda p: p.stat().st_mtime)
    data = [json.loads(p.read_text()) for p in files]
    return data if rows else [d["summary"] for d in data]


# Code the walkthrough may show, by name (a whitelist: the UI never reads arbitrary files).
CODE = {"prepare": graph.prepare, "triage": graph.triage, "vote": graph.vote, "policy": graph.policy,
        "draft": graph.draft, "verify": graph.verify, "build": graph.build, "payload": graph.payload,
        "call": helpdesk.Helpdesk.call, "decide": helpdesk.Helpdesk.decide, "run_ticket": agent.run_ticket,
        "thread_for": agent.thread_for, "redact": safety.redact, "rule_hits": safety.rule_hits,
        "reply_problems": safety.reply_problems, "Triage": graph.Triage, "Check": graph.Check}
PROMPTS = ("TRIAGE_SYSTEM", "DRAFT_SYSTEM", "CHECK_SYSTEM", "UNTRUSTED")


def code(name):
    if name in PROMPTS:
        src = (ROOT / "graph.py").read_text().splitlines()
        line = next(i for i, l in enumerate(src, 1) if l.startswith(name + " ="))
        return {"name": name, "file": "graph.py", "line": line, "source": getattr(graph, name)}
    obj = CODE[name]
    lines, line = inspect.getsourcelines(obj)
    return {"name": name, "file": Path(inspect.getsourcefile(obj)).name, "line": line, "source": "".join(lines)}


def run_tests():
    """Run the two offline self-checks (no network, nothing posted) and list what they cover."""
    out = []
    for script in ("test_reliability.py", "safety.py"):
        started = time.monotonic()
        p = subprocess.run([sys.executable, script], cwd=ROOT, capture_output=True, text=True, timeout=60)
        src = (ROOT / script).read_text()
        if script == "test_reliability.py":
            checks = [l[2:] for l in src.splitlines() if l.startswith("# ")]
        else:
            checks = [f"{src.split('__main__')[1].count('assert ')} assertions: card numbers (Luhn), IBANs (mod-97), "
                      "CVC and expiry redaction; rule keywords; reply egress filter"]
        out.append({"script": script, "ok": p.returncode == 0, "checks": checks,
                    "output": redact((p.stdout + p.stderr).strip())[-1500:], "secs": round(time.monotonic() - started, 2)})
    return out


def run_log():
    """Latest decision per ticket from logs/agent.jsonl, plus one line per run."""
    path = ROOT / "logs" / "agent.jsonl"
    recs = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []
    latest, runs = {}, {}
    for r in recs:
        run = runs.setdefault(r["run"], {"run": r["run"], "tickets": 0, "errors": 0, "cost": 0.0})
        run["tickets"] += 1
        run["errors"] += r["outcome"] == "error"
        run["cost"] = round(run["cost"] + (r.get("cost_usd") or 0), 6)
        if r["outcome"] != "error" or r["ticket"] not in latest:
            latest[r["ticket"]] = r
    return {"tickets": [latest[k] for k in sorted(latest)], "runs": list(runs.values()), "lines": len(recs)}


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
                self._json({**HD.call("GET", "/status"), "evals": evals(),
                            "model": graph.model(0).model_name})
            elif self.path == "/api/graph":
                self._json({"mermaid": DRY.get_graph().draw_mermaid()})
            elif self.path == "/api/evals":
                self._json(evals(rows=True))
            elif self.path == "/api/run":
                self._json({k: v for k, v in RUN.items() if k != "proc"})
            elif self.path == "/api/log":
                self._json(run_log())
            elif self.path == "/api/config":
                self._json({"rules": safety.RULE_KEYWORDS, "allowed_domains": sorted(graph.ALLOWED_DOMAINS),
                            "samples": graph.SAMPLES, "confidence_min": graph.CONFIDENCE_MIN,
                            "faq": {k: v.splitlines()[0][3:] for k, v in graph.FAQ_SECTIONS.items()},
                            "throttle_per_s": round(1 / Helpdesk.MIN_INTERVAL), "max_tries": Helpdesk.MAX_TRIES})
            elif self.path.startswith("/api/code/"):
                name = self.path.rsplit("/", 1)[1]
                self._json(code(name) if name in CODE or name in PROMPTS else {"error": "not_found"},
                           200 if name in CODE or name in PROMPTS else 404)
            else:
                self._json({"error": "not_found"}, 404)
        except ApiError as e:
            self._json({"error": f"helpdesk {e.status}: {e.payload}"}, 502)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/run":
                mode = body.get("mode")
                if mode not in ("dry", "open", "reset"):
                    return self._json({"error": "mode must be dry, open or reset"}, 400)
                return self._json({"running": True, "mode": mode, "lines": [], "exit": None}) if start_run(mode) else self._json({"error": "a run is already in progress"}, 409)
            if self.path == "/api/post" and RUN["running"]:
                return self._json({"error": "a run-all is in progress"}, 409)
            if self.path == "/api/tests":
                return self._json(run_tests())
            if self.path == "/api/redact":  # safety playground: what the deterministic layer does to some text
                text = str(body.get("text", ""))[:5000]
                return self._json({"redacted": redact(text), "rule_hits": safety.rule_hits({"body": text}),
                                   "reply_problems": safety.reply_problems(text, graph.ALLOWED_DOMAINS)})
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
    import argparse
    import webbrowser
    ap = argparse.ArgumentParser(description="Serve the walkthrough UI and its API (one process).")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = ap.parse_args()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        sys.exit(f"Port {args.port} is in use (is the UI already running?). Try: uv run ui.py --port {args.port + 1}")
    url = f"http://127.0.0.1:{args.port}"
    print(f"Support agent UI + API: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:  # do not leave a run-all child behind (it is safe to stop: unfinished tickets resume next run)
        if RUN["running"] and RUN["proc"]:
            RUN["proc"].terminate()
