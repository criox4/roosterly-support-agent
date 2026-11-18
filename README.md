# Roosterly support agent

The agent reads every open ticket from the Roosterly helpdesk and posts one decision per ticket: a reply
drafted from the help centre (`faq.md`), a handoff to the support team with a note in English, or a close
for spam and empty tickets.

| Final run (30 tickets) | Eval (10 examples × 3 runs) | Cost |
|---|---|---|
| 30 posted once, 0 errors: 12 reply, 16 handoff, 2 close | 0 unsafe, over-escalated 7/30 → 1/30, action 0.97 | about $0.04 per 100 tickets |

## How to run

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync && cp .env.example .env      # then fill in the keys
uv run python safety.py && uv run python test_reliability.py   # offline checks, no network
uv run eval.py --tag mine --runs 3   # eval on the 10 labelled examples, posts nothing
uv run agent.py --dry-run            # decide every open ticket and print, post nothing
uv run agent.py --reset              # final run: reset the practice set, then handle all 30 once
uv run ui.py                         # optional walkthrough + live demo at http://127.0.0.1:8000
```

Re-running `agent.py` is safe: handled tickets are skipped. `--only T005 T007` handles specific tickets. Logs
go to `logs/agent.jsonl` (one line per ticket), eval results to `eval/results/`.

## Decisions

![One ticket through the pipeline](docs/pipeline.png)

1. **A fixed LangGraph pipeline instead of a tool-calling agent.** Every ticket takes the same steps
   (`graph.py`), so a loop where the model picks tools would only add ways to go wrong. The model fills strict
   JSON schemas (language, category, priority, cited FAQ sections, client-rule flags) and plain Python
   decides. LangGraph adds checkpoints and retries; LangChain is only the model client.
2. **Code can only move a ticket towards a human.** "Unsure" means the three triage samples (temperature
   0.8) disagree on the action. Ticket text is treated as data, so the injections in T007 and T030 changed
   nothing.

   | When | Then |
   |---|---|
   | A triage sample flags a client rule (refund, cancellation or contract change, GDPR/AVG, security, legal or churn threat), or a nl/en/de keyword for one matches | handoff, at least P2 |
   | The ticket is P1, the samples disagree, no FAQ section is cited, or confidence is below 0.7 | handoff |
   | The reply check fails (see 3) | handoff |
   | The ticket is empty | close, no model call |
3. **Dutch is handled directly and checked in English.** Translating first would cost a call per ticket and
   lose tone and the Dutch menu names. A second model call back-translates each draft, checks its language
   and lists unsupported claims and promises; code checks links, email addresses and payment details. Any
   problem means a handoff. The English version is logged as `reply_en`.

Assumptions: self-service how-tos such as upgrading (F05) get a reply, feature requests get a roadmap reply
(as E05), and a ticket with several issues goes to a human if any part needs one. I built this with Claude
Code (research, coding, debugging, tests); I chose the framework, uncertainty method and policy, and
reviewed the dry-run decisions.

## Reliability

| Situation | What the agent does |
|---|---|
| 429 rate limit | Throttles itself to 4 calls a second, under the limit of 5; after a 429 it waits `Retry-After` and retries |
| 503 or network error | Retries with exponential backoff plus jitter, up to 8 tries |
| Other 4xx (400, 401, 404, 422) | No retry: the request itself is wrong, so the error is logged and the ticket stays open |
| 409, or already handled | Re-fetches the ticket before every POST and skips it if handled; a 409 on a retry counts as done (two agents at once: zero double answers) |
| Crash or failed model call | Each ticket is a checkpointed LangGraph thread that resumes where it stopped (tested with `kill -9`; T001 to T005 resumed in the final run). A failed ticket stays open; the run continues |

Keys come only from `.env` (git-ignored). Card numbers, IBANs, CVCs and expiry dates are redacted before the
model, checkpoints and logs see a ticket, and again on everything posted. `test_reliability.py` and
`safety.py` test this offline.

## Eval and fix

`eval.py` runs the graph on the 10 examples from `GET /examples` without posting and compares with Nordavo's
labels, 3 runs each because sampling is noisy.

| | unsafe* | over-escalated | action | category | priority | language |
|---|---|---|---|---|---|---|
| baseline | 0 | 7/30 | 0.77 | 0.97 | 0.80 | 1.00 |
| after fix | 0 | 2/30 | 0.93 | 1.00 | 0.80 | 1.00 |
| final code | 0 | 1/30 | 0.97 | 1.00 | 0.80 | 1.00 |

\*Unsafe means the label says handoff but the agent replied or closed. Over-escalated means the label says
reply but the agent handed off.

What went wrong: the agent handed off tickets the FAQ answers with a requirement or a pointer (E01, a crash
on iOS 15, where F12 says iOS 16+; E10, maintenance, where F11 gives the status page), and read E02 (a VAT
number change) as a contract change. The fix: the prompt defines what counts as an answer, the cancellation
flag covers only contract terms, and the checker accepts a pointer. Priority stays at 0.80: the agent tends
to say P2 where the label says P3.

Cost: $0.0126 for the final 30 tickets, about $0.04 per 100 (OpenRouter's per-call cost). Each ticket makes 3
triage calls of ~1.9k input tokens (98% cached, $0.01/M) and ~140 output ($0.50/M), about $0.00009 each; a
reply adds ~$0.0003 for the draft and check.

## What I would improve

Next: drafts approved by a person (a LangGraph `interrupt()` before posting), then auto-send one category at
a time. After that, EU data residency as F09 promises, an AI disclosure (EU AI Act art. 50), and about 100
tickets labelled by the support team, with the eval in CI.

Rule I would change: define "contract changes". Read literally it includes upgrading, which F05 tells
customers to do themselves.
