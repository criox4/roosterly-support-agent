# Roosterly support agent

## How to run

```bash
uv sync && cp .env.example .env           # then fill HELPDESK_KEY, CANDIDATE_EMAIL, OPENROUTER_API_KEY
uv run python safety.py && uv run python test_reliability.py   # offline checks
uv run eval.py --tag mine --runs 3        # eval on the 10 labelled examples, posts nothing
uv run agent.py --dry-run                 # decide and print, post nothing
uv run agent.py --reset                   # clean run: reset, then handle all tickets once
uv run ui.py                              # optional: walkthrough + live demo at http://127.0.0.1:8000
```
Re-running `agent.py` is safe. Logs go to `logs/agent.jsonl`, eval results to `eval/results/`.

## Decisions

1. **A fixed LangGraph pipeline.** Every ticket runs `prepare → triage → policy → draft → verify → post`
   (`graph.py`). The model fills strict JSON schemas and plain code decides. LangChain is only the model client.
2. **Code can only move a ticket towards a human.** The guardrails below run after the model and can turn a
   reply into a handoff, never the reverse. "Unsure" means the 3 triage samples disagree on the action.
3. **Dutch is handled natively and checked in English.** Translating first adds a call and loses nuance. A
   separate checker back-translates every draft, and the English is logged as `reply_en` for review.

Assumptions: self-service how-tos such as upgrading (F05) get a reply, feature requests get a roadmap reply
(as E05), and a ticket with several issues goes to a human if any part needs one. AI tools: built with Claude
Code for research, coding, debugging and test runs. I chose the framework, the uncertainty method and the
policy, and reviewed the dry-run decisions.

### Guardrails

| If any of these is true | Result |
|---|---|
| A triage sample flags a client rule, or a nl/en/de rule keyword matches (`safety.py`) | handoff, at least P2 |
| P1, the samples disagree, no FAQ section cited, or confidence below 0.7 | handoff |
| The checker finds a wrong language, an unsupported claim, a promise or an unanswered question | handoff |
| The reply links to a domain the FAQ does not name, or contains an email address or payment details | handoff |
| The ticket is empty | close, no model call |

Ticket text is wrapped as data with its `<` `>` neutralised, so the injections in T007 and T030 changed
nothing. Card numbers (Luhn), IBANs (mod-97), CVCs and expiry dates are redacted before the model sees a
ticket, and again on everything posted.

## Reliability

- 429: calls are throttled to 4 per second; after a 429 the agent waits `Retry-After`. A 503 or network error
  is retried with jittered backoff, up to 8 tries. Other 4xx errors are not retried.
- 409: the ticket is re-fetched before every POST and skipped if handled. A retry whose first POST was saved
  gets 409 and counts as done. Two agents running at once gave zero double answers.
- Crash: each ticket is a LangGraph thread in `checkpoints.db` and resumes where it stopped (tested with
  `kill -9`). In the final run, T001 to T005 resumed after an earlier attempt failed on an expired API key.
- Security: keys come only from `.env` (git-ignored). The model, checkpoints and logs never see payment details.

## Eval and fix

`eval.py` runs the graph without posting on `GET /examples` and compares with the API's labels (3 runs × 10):

| | unsafe* | over-escalated | action | category | priority | language |
|---|---|---|---|---|---|---|
| baseline | 0 | 7/30 | 0.77 | 0.97 | 0.80 | 1.00 |
| after fix | 0 | 2/30 | 0.93 | 1.00 | 0.80 | 1.00 |
| final code | 0 | 1/30 | 0.97 | 1.00 | 0.80 | 1.00 |

\*Labelled handoff, but the agent replied or closed.

The agent handed off answerable tickets when the FAQ answer is a requirement or a pointer: E01 (iOS 15, F12
says iOS 16+) and E10 (maintenance, F11's status page). It also read E02 (VAT number) as a contract change.
The fix: the prompt defines an answer as steps, facts, a requirement that explains the problem, or the
resource to check; the cancellation flag covers only contract terms; the checker accepts a pointer.

Cost: the final 30-ticket run cost $0.0126, about $0.04 per 100 tickets (OpenRouter's per-call cost). Each
ticket takes 3 triage calls of about 1.9k input tokens (98% cached, $0.01/M) and 140 output tokens ($0.50/M);
a reply adds a draft and a check call, about $0.0003.

## What I would improve

Next step: drafts approved by a person (a LangGraph `interrupt()` before post), then auto-send per category
with a kill switch. After that: keep data in the EU as F09 promises (EU endpoint, zero retention, a DPIA), add
an AI disclosure to replies (EU AI Act art. 50), and grow the eval to about 100 tickets labelled by the support
team. Priority (0.80) is the weakest field.

Rule I would change: define "contract changes". Read literally, it includes upgrading, which F05 tells
customers to do themselves.
