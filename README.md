# Roosterly support agent

## How to run

```bash
uv sync && cp .env.example .env   # then fill in the keys
uv run python safety.py && uv run python test_reliability.py   # offline
uv run eval.py --tag mine --runs 3   # eval on the examples, posts nothing
uv run agent.py --reset              # final run (--dry-run posts nothing)
uv run ui.py                         # optional demo UI, 127.0.0.1:8000
```

## Decisions

1. **A fixed LangGraph pipeline** (`graph.py`): the model fills strict JSON schemas and plain code decides.
   LangChain is only the model client.
2. **Code can only move a ticket towards a human.** A client-rule flag or nl/en/de keyword, P1, disagreement
   between the 3 triage samples ("unsure"), no cited FAQ section, low confidence or a failed reply check all
   force a handoff.
3. **Dutch is handled natively, checked in English.** Translating first adds a call and loses nuance; a
   separate checker back-translates each draft, logged as `reply_en`.

Assumed: how-tos like upgrading (F05) get a reply; mixed tickets go to a human. Built with Claude Code
(research, coding, tests); I chose the framework, uncertainty method and policy.

## Reliability

429: throttle at 4 calls/s, then wait `Retry-After`. 503 or network error: jittered backoff, 8 tries. 409:
re-fetch before each POST and skip if handled; a 409 on retry counts as done (two agents in parallel gave
zero double answers). Crash: each ticket is a LangGraph thread in `checkpoints.db` that resumes where it
stopped (`kill -9` tested; T001 to T005 resumed in the final run). Security: keys only from `.env`; ticket
text is wrapped as data; payment details are redacted before the model, checkpoints and logs.

## Eval and fix

`eval.py` runs the graph on `GET /examples` without posting, against the API's labels (3 runs × 10):

| | unsafe* | over-escalated | action | category | priority | language |
|---|---|---|---|---|---|---|
| baseline | 0 | 7/30 | 0.77 | 0.97 | 0.80 | 1.00 |
| after fix | 0 | 2/30 | 0.93 | 1.00 | 0.80 | 1.00 |
| final code | 0 | 1/30 | 0.97 | 1.00 | 0.80 | 1.00 |

\*Labelled handoff, agent replied or closed. Wrong: tickets answered by a requirement or a pointer (E01,
E10) were handed off, and E02 (VAT number) was read as a contract change. Fix: the
prompt defines an answer, the cancellation flag covers only contract terms, the checker accepts a pointer.
Cost: $0.0126 for the final 30 ≈ $0.04 per 100 tickets (3 triage calls of ~1.9k input, 98% cached at $0.01/M,
and ~140 output at $0.50/M; a reply adds ~$0.0003).

## What I would improve

Next: person-approved drafts (LangGraph `interrupt()`), then auto-send per category; EU data residency (F09).
Rule change: define "contract changes"; read literally it includes upgrading, which F05 leaves to customers.
