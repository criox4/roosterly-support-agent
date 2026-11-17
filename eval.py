#!/usr/bin/env python3
"""Eval: run the agent's graph (without posting) on the 10 labelled examples from GET /examples
and compare with Nordavo's labels. Labels come from the API, never from the model under test.

    uv run eval.py --tag baseline          # one pass
    uv run eval.py --tag baseline --runs 3 # repeat to see consistency

Writes eval/results/<tag>.json. Metrics, in the order the brief scores them:
- unsafe: a ticket labelled handoff got a reply or close (must be 0)
- over_escalated: labelled reply, agent handed off (costs the support team time)
- per-field accuracy: action, language, category, priority
"""
import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import graph
from agent import load_env
from helpdesk import Helpdesk
from safety import redact_ticket

ROOT = Path(__file__).parent
FIELDS = ("action", "language", "category", "priority")


def predict(app, example):
    ticket = redact_ticket({k: v for k, v in example.items() if k != "label"})
    state = app.invoke({"ticket": ticket, "thread": []})
    body = graph.payload(state)
    return {"action": state["action"], "language": body["language"], "category": body["category"],
            "priority": body["priority"], "overrides": state.get("overrides"),
            "sample_actions": [s["action"] for s in state.get("samples") or []],
            "reply_en": (state.get("check") or {}).get("back_translation_en"),
            "reason": body["reason"], "cost": state.get("cost") or 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="name for this result, e.g. baseline / after-fix")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()

    load_env()
    examples = Helpdesk().examples()
    app = graph.build()  # no helpdesk: nothing is posted

    rows = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for run in range(args.runs):
            for ex, pred in zip(examples, pool.map(lambda e: predict(app, e), examples)):
                label = ex["label"]
                rows.append({"run": run, "id": ex["id"], "label": {f: label[f] for f in FIELDS},
                             "label_reason": label.get("reason"), "pred": pred,
                             "correct": {f: pred[f] == label[f] for f in FIELDS}})

    n = len(rows)
    unsafe = [r for r in rows if r["label"]["action"] == "handoff" and r["pred"]["action"] != "handoff"]
    over = [r for r in rows if r["label"]["action"] == "reply" and r["pred"]["action"] == "handoff"]
    summary = {
        "tag": args.tag, "model": graph.model(0).model_name, "examples": len(examples), "runs": args.runs,
        "unsafe": len(unsafe), "over_escalated": len(over),
        "accuracy": {f: round(sum(r["correct"][f] for r in rows) / n, 2) for f in FIELDS},
        "all_fields_correct": round(sum(all(r["correct"].values()) for r in rows) / n, 2),
        "cost_usd": round(sum(r["pred"]["cost"] for r in rows), 5),
        "cost_per_100_tickets_usd": round(sum(r["pred"]["cost"] for r in rows) / n * 100, 4),
    }
    if args.runs > 1:  # same action in every run?
        by_id = Counter()
        for ex in examples:
            by_id[ex["id"]] = len({r["pred"]["action"] for r in rows if r["id"] == ex["id"]}) == 1
        summary["action_consistent_across_runs"] = round(sum(by_id.values()) / len(examples), 2)

    out = ROOT / "eval" / "results" / f"{args.tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2))

    print(f"{'id':<5} {'field':<9} {'label':<16} {'agent':<16}")
    for r in rows:
        wrong = [f for f in FIELDS if not r["correct"][f]]
        mark = "ok" if not wrong else "MISS " + ",".join(wrong)
        print(f"{r['id']:<5} run{r['run']} {mark}")
        for f in wrong:
            print(f"      {f:<9} {r['label'][f]:<16} {r['pred'][f]:<16}")
        if wrong:
            print(f"      label reason: {r['label_reason']}")
            print(f"      agent reason: {r['pred']['reason']}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
