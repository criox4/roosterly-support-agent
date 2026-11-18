# Roosterly support agent

## How to run

```bash
uv sync && cp .env.example .env    # then fill HELPDESK_KEY, CANDIDATE_EMAIL, OPENROUTER_API_KEY
uv run python safety.py && uv run python test_reliability.py   # offline checks
uv run eval.py --tag mine --runs 3 # eval on the 10 labelled examples, posts nothing
uv run agent.py --reset            # clean run: reset, then handle every ticket once (--dry-run posts nothing)
uv run ui.py                       # optional walkthrough + live demo at http://127.0.0.1:8000
```

## Decisions

1. **A fixed LangGraph pipeline:** `prepare → triage → policy → draft → verify → post` (`graph.py`). The model
   fills strict JSON schemas, plain code decides, and LangChain is only the model client.
2. **Code can only move a ticket towards a human.** "Unsure" means the 3 triage samples disagree on the action.
3. **Dutch is handled natively, checked in English.** Translating first adds a call and loses nuance; a
   separate checker back-translates each draft, logged as `reply_en`.

Assumed: self-service how-tos such as upgrading (F05) get a reply; a ticket with several issues goes to a
human if any part needs one. Built with Claude Code (research, coding, tests); I chose the framework,
uncertainty method and policy, and reviewed the dry-run decisions.

### Guardrails

| If any of these is true | Result |
|---|---|
| A sample flags a client rule, or a nl/en/de rule keyword matches (`safety.py`) | handoff, at least P2 |
| P1; samples disagree; no FAQ section cited; confidence < 0.7; the reply check finds a wrong language, unsupported claim, promise, link outside the FAQ's domains, email address or payment detail | handoff |
| Empty ticket | close, no model call |

Ticket text is wrapped as data (T007 and T030's injections changed nothing). Cards (Luhn), IBANs (mod-97),
CVCs and expiry dates are redacted before the model, checkpoints and logs see a ticket.

## Reliability

429: throttle at 4 calls/s, then wait `Retry-After`. 503 or network error: jittered backoff, 8 tries. 409:
each ticket is re-fetched before its POST and skipped if handled; a retry that gets 409 counts as done (two
agents at once gave zero double answers). Crash: each ticket is a LangGraph thread in `checkpoints.db` that
resumes where it stopped (`kill -9` tested; in the final run T001 to T005 resumed after an expired API key).
Keys come only from `.env`.

## Eval and fix

`eval.py` runs the graph on `GET /examples` without posting, against the API's labels (3 runs × 10):

| | unsafe* | over-escalated | action | category | priority | language |
|---|---|---|---|---|---|---|
| baseline | 0 | 7/30 | 0.77 | 0.97 | 0.80 | 1.00 |
| after fix | 0 | 2/30 | 0.93 | 1.00 | 0.80 | 1.00 |
| final code | 0 | 1/30 | 0.97 | 1.00 | 0.80 | 1.00 |

\*Labelled handoff, but the agent replied or closed. Wrong: tickets answered by a requirement or a pointer
were handed off (E01: F12's iOS 16+; E10: F11's status page) and E02 (VAT number) was read as a contract
change. Fix: the prompt defines an answer, the cancellation flag covers only contract terms, and the checker
accepts a pointer. Cost: $0.0126 for the 30-ticket final run, ≈ $0.04 per 100 tickets: 3 triage calls of
~1.9k input tokens (98% cached, $0.01/M) and ~140 output ($0.50/M), plus ~$0.0003 for a reply's two calls.

## What I would improve

Next: drafts approved by a person (LangGraph `interrupt()`), then auto-send per category; EU data residency
as F09 promises; ~100 tickets labelled by the support team (priority, 0.80, is weakest). Rule change: define
"contract changes"; read literally it includes upgrading, which F05 tells customers to do themselves.
