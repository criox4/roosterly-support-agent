#!/usr/bin/env python3
"""Roosterly support agent: handle every open helpdesk ticket once.

    uv run agent.py             handle all open tickets
    uv run agent.py --dry-run   decide and print, post nothing
    uv run agent.py --reset     POST /practice/reset first (clean run)
    uv run agent.py --only T005 T007

Safe to re-run. Each ticket is a LangGraph thread checkpointed in checkpoints.db:
- a run that crashed mid-ticket resumes at the node where it stopped (post re-checks the
  helpdesk before sending, so a decision that was already saved is never sent twice);
- tickets already handled in the helpdesk are not listed as open, so they are skipped.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

import graph
from helpdesk import Helpdesk
from safety import redact, redact_ticket

ROOT = Path(__file__).parent


def load_env(path=ROOT / ".env"):
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def thread_for(ticket, tickets):
    """Earlier tickets this one answers ('Re: <subject>'), so a follow-up is read in context."""
    m = re.match(r"^\s*(?:re|aw|antw)\s*:\s*(.+)$", ticket.get("subject") or "", re.I)
    if not m:
        return []
    subject = m.group(1).strip().lower()
    return [t for t in tickets if t["id"] != ticket["id"] and (t.get("subject") or "").strip().lower() == subject]


def execute(app, inp, config=None):
    """Run the graph, returning (final state, names of the nodes that ran, in order)."""
    state, nodes = {}, []
    for mode, data in app.stream(inp, config, stream_mode=["updates", "values"]):
        if mode == "updates":
            nodes += list(data)
        else:
            state = data
    return state, nodes


def run_ticket(app, saver, ticket, thread):
    """Run (or resume) one ticket's thread. Returns (final state, resumed?, nodes run)."""
    config = {"configurable": {"thread_id": ticket["id"]}}
    snapshot = app.get_state(config)
    if snapshot.next:  # unfinished from a crashed run: continue where it stopped
        state, nodes = execute(app, None, config)
        return state, True, nodes
    if snapshot.values:  # finished earlier, but the helpdesk says it is open again (practice reset)
        saver.delete_thread(ticket["id"])
    state, nodes = execute(app, {"ticket": ticket, "thread": thread}, config)
    return state, False, nodes


def log_record(run_id, state, outcome, secs, resumed):
    tri, check = state.get("triage") or {}, state.get("check") or {}
    body = graph.payload(state)
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run": run_id,
        "ticket": state["ticket"]["id"], "outcome": outcome, "action": state["action"],
        "language": body["language"], "category": body["category"], "priority": body["priority"],
        "confidence": body["confidence"], "faq": tri.get("faq_ids"), "overrides": state.get("overrides"),
        "sample_actions": [s["action"] for s in state.get("samples") or []],
        "reason": body["reason"], "reply": body.get("draft_reply"),
        "reply_en": check.get("back_translation_en"), "handoff_note": body.get("handoff_note"),
        "cost_usd": round(state.get("cost") or 0, 6), "secs": secs, "resumed": resumed,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="decide and print, post nothing")
    ap.add_argument("--reset", action="store_true", help="POST /practice/reset before running")
    ap.add_argument("--only", nargs="+", metavar="ID", help="only these ticket ids")
    args = ap.parse_args()

    load_env()
    hd = Helpdesk()
    if args.reset:
        print("reset:", hd.reset())

    tickets = [redact_ticket(t) for t in hd.all_tickets()]  # payment details never enter the graph
    todo = [t for t in tickets if not t.get("handled") and (not args.only or t["id"] in args.only)]
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    print(f"run {run_id}: {len(todo)} open of {len(tickets)} tickets, model {os.environ.get('MODEL')}"
          f"{' (dry run)' if args.dry_run else ''}")

    (ROOT / "logs").mkdir(exist_ok=True)
    log_path = ROOT / "logs" / ("dry-run.jsonl" if args.dry_run else "agent.jsonl")
    db = sqlite3.connect(ROOT / ("checkpoints-dry.db" if args.dry_run else "checkpoints.db"), check_same_thread=False)
    saver = SqliteSaver(db)
    app = graph.build(helpdesk=None if args.dry_run else hd, checkpointer=saver)

    counts, errors, total_cost = {}, 0, 0.0
    with log_path.open("a", encoding="utf-8") as log:
        for t in todo:
            started = time.monotonic()
            try:
                state, resumed, _ = run_ticket(app, saver, t, thread_for(t, tickets))
            except Exception as e:  # one bad ticket must not stop the run; it stays open for the next one
                errors += 1
                msg = redact(f"{type(e).__name__}: {e}")[:300]
                log.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                      "run": run_id, "ticket": t["id"], "outcome": "error", "error": msg}) + "\n")
                log.flush()
                print(f"{t['id']}  ERROR  {msg}", file=sys.stderr)
                continue
            outcome = state.get("outcome", "dry_run")
            rec = log_record(run_id, state, outcome, round(time.monotonic() - started, 1), resumed)
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log.flush()
            counts[rec["action"]] = counts.get(rec["action"], 0) + 1
            total_cost += rec["cost_usd"]
            flag = f"  [{', '.join(rec['overrides'])}]" if rec["overrides"] else ""
            print(f"{rec['ticket']}  {rec['action']:<7} {rec['language']} {rec['category']:<15} "
                  f"{rec['priority']}  {outcome}{flag}")
            if args.dry_run:
                print("      ", (rec["reply"] or rec["handoff_note"] or rec["reason"]).replace("\n", " ")[:400])
                if rec["reply_en"]:
                    print("   EN:", rec["reply_en"].replace("\n", " ")[:400])

    n = sum(counts.values())
    print(f"done: {counts}, errors={errors}, helpdesk retries={hd.retries}, "
          f"cost=${total_cost:.4f} (${total_cost / max(n, 1) * 100:.3f} per 100 tickets), log={log_path.name}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
