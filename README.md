# Roosterly support agent

## How to run

```bash
uv sync && cp .env.example .env           # then fill HELPDESK_KEY, CANDIDATE_EMAIL, OPENROUTER_API_KEY
uv run python safety.py && uv run python test_reliability.py   # offline checks
uv run eval.py --tag mine --runs 3        # eval on the 10 labelled examples, posts nothing
uv run agent.py --dry-run                 # decide and print, post nothing
uv run agent.py --reset                   # clean run: reset, then handle all tickets once
uv run ui.py                              # optional: UI + API in one process, opens http://127.0.0.1:8000
```
Re-running `agent.py` is safe. Logs go to `logs/agent.jsonl` and eval results to `eval/results/`.

## Decisions

1. **A fixed LangGraph pipeline.** Every ticket goes through `prepare → triage → policy → draft → verify → post`
   (`graph.py`). The steps are the same for every ticket, so the model gets no tools and no loop. It makes
   narrow judgements into a strict schema, and plain code makes the decision. LangGraph provides checkpoints
   and retries; LangChain is only the OpenRouter client.
2. **Safety rules live in code, and code can only move a ticket towards a human.** A ticket goes to a human
   when the model flags a client rule or a nl/en/de keyword list matches, when it is P1, when a reply cites
   no FAQ section, when confidence is low, or when the 3 triage samples disagree on the action. That
   disagreement is the main signal for "unsure"; the model's own confidence score is a second check. Ticket
   text is wrapped as data and replies are drafted only from the cited FAQ sections, so the injection
   attempts in T007 and T030 had no effect.
3. **Dutch is handled natively and checked in English.** Translating first would add a call per ticket and
   lose nuance. A separate checker back-translates each draft, confirms its language, and lists unsupported
   claims and promises. Code then blocks links to domains the FAQ does not name, email addresses and payment
   details. Any problem turns the reply into a handoff. The English version is logged as `reply_en`.

Assumptions: self-service how-tos such as upgrading (F05) get a reply, not a contract-change handoff. Feature
requests get a roadmap reply, as in E05. A ticket with several issues goes to a human if any part needs one.
AI tools: I built this with Claude Code, using it for research sub-agents (frameworks, support-agent
practice), coding, debugging and test runs. I chose the framework, the uncertainty method and the policy,
and reviewed the dry-run decisions.

## Reliability

- 429: calls are throttled to 4 per second, and after a 429 the agent waits `Retry-After`. A 503 or network
  error is retried with jittered backoff, up to 8 tries. Other 4xx errors are not retried.
- 409: the agent re-fetches the ticket before every POST and skips it if it is already handled. When a POST
  was saved but its response was lost, the retry gets 409 and counts as done. Two agents running at once
  produced zero double answers.
- Crash: each ticket is a LangGraph thread in `checkpoints.db` and resumes at the node where it stopped
  (tested with `kill -9`). A failed model call leaves that ticket open and the run continues.
- Security: keys come only from `.env`, which is git-ignored. Card numbers (Luhn), IBANs (mod-97), CVCs and
  expiry dates are redacted before text enters the graph, so the model, checkpoints and logs never see them.

## Eval and fix

`eval.py` runs the graph without posting on `GET /examples` and compares with the API's labels, 3 runs × 10
examples:

| | unsafe* | over-escalated | action | category | priority | language |
|---|---|---|---|---|---|---|
| baseline | 0 | 7/30 | 0.77 | 0.97 | 0.80 | 1.00 |
| after fix | 0 | 2/30 | 0.93 | 1.00 | 0.80 | 1.00 |
| final code | 0 | 1/30 | 0.97 | 1.00 | 0.80 | 1.00 |

\*Labelled handoff, but the agent replied or closed. A difference of one ticket between runs is sampling noise.

The agent handed off answerable tickets when the FAQ answers with a requirement or a pointer: E01 (crash on
iOS 15, and F12 says iOS 16+) and E10 (maintenance tonight, answered by F11's status page). It also read E02
(VAT number) as a contract change. The fix: the prompt defines an answer as steps, facts, a requirement that
explains the problem, or the resource to check; the cancellation flag covers only contract terms; and the
checker accepts a pointer. A 30-ticket dry run afterwards gave no client-rule ticket a reply.

Cost is about $0.04 per 100 tickets, from OpenRouter's per-call cost ($0.0123 for 30 tickets). Triage uses
about 1.9k input tokens (98% cache hits at $0.01/M) and 140 output tokens ($0.50/M), about $0.00009 per
sample, times 3 samples. A reply adds a draft and a check call, about $0.0003.

## What I would improve

Before real customers, following how production support agents are run (Intercom Fin, Sierra, the Dutch AP
and ACM, OWASP LLM Top 10):
- Start with human-approved drafts (a LangGraph `interrupt()`), then auto-send per category, with a kill switch.
- Keep data in the EU, as F09 promises: OpenRouter's EU endpoint with zero retention, pseudonymised names,
  log retention limits and a DPIA.
- Add an AI disclosure to replies (EU AI Act art. 50).
- Have the support team label about 100 tickets, add a red-team set, and run the eval in CI. Priority (0.80)
  is the weakest field.

One change to the client's rules: define "contract changes". Read literally, the term includes upgrading,
which F05 tells customers to do themselves.
