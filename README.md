# Roosterly support agent

## How to run

```bash
uv sync && cp .env.example .env           # then fill HELPDESK_KEY, CANDIDATE_EMAIL, OPENROUTER_API_KEY
uv run python safety.py && uv run python test_reliability.py   # offline checks
uv run eval.py --tag mine --runs 3        # eval on the 10 labelled examples, posts nothing
uv run agent.py --dry-run                 # decide and print, post nothing
uv run agent.py --reset                   # clean run: reset, then handle all tickets once
uv run ui.py                              # optional demo UI at http://127.0.0.1:8000 (step-by-step trace)
```
Re-running `agent.py` is safe. Logs: `logs/agent.jsonl`. Eval results: `eval/results/`.

## Decisions

1. **A fixed LangGraph pipeline, not an autonomous agent:** `prepare → triage → policy → draft → verify →
   post` (`graph.py`). Every ticket takes the same steps, so a tool loop would only add unpredictability.
   The model makes narrow, schema-constrained judgements and code decides. LangGraph gives checkpoints and
   retries. LangChain is only the OpenRouter client.
2. **Safety is code that only moves tickets towards a human.** Client-rule flags from the model OR a nl/en/de
   keyword list, any P1, a reply without a cited FAQ section, low confidence, or **disagreement between 3
   triage samples** force a handoff. "Unsure" is measured by self-consistency, not a self-reported score.
   Ticket text is wrapped as data, and replies are drafted only from the cited FAQ sections, so injections
   (T007, T030) change nothing.
3. **Dutch is handled natively and checked in English.** Translating first costs more and loses nuance.
   A separate checker back-translates each draft, checks its language, and lists unsupported claims and
   promises. Code blocks links to domains the FAQ does not name, email addresses and payment details. Any
   problem turns the reply into a handoff. The English version is logged (`reply_en`) for review.

**Assumptions:** self-service how-tos (upgrading, F05) get a reply, not a contract-change handoff. Feature
requests get a roadmap reply (as E05). A ticket with several issues goes to a human if any part needs one.
**AI tools:** built with Claude Code: research sub-agents (frameworks, support-agent practice), coding,
debugging, test runs. I chose the framework, the uncertainty method and the policy, and reviewed the
dry-run decisions.

## Reliability

- **429:** throttle at 4 req/s, then wait `Retry-After`. **503 or network error:** backoff with jitter, up to
  8 tries. Other 4xx are not retried.
- **409, never two answers:** the ticket is re-fetched before every POST and skipped if handled. A saved POST
  whose response was lost gets 409 on retry and counts as done. Two agents run at once: zero double answers.
- **Crash:** each ticket is a LangGraph thread in `checkpoints.db`, resumed at the node where it stopped
  (tested with `kill -9`). A failed model call leaves that ticket open and the run continues.
- **Security:** keys only from `.env` (git-ignored). Cards (Luhn), IBANs (mod-97), CVCs and expiry dates are
  redacted before text enters the graph, so the model, checkpoints and logs never see them.

## Eval and fix

`eval.py` runs the graph (no posting) on `GET /examples` against the API's labels. 3 runs × 10 examples:

| | unsafe* | over-escalated | action | category | priority | language |
|---|---|---|---|---|---|---|
| baseline | 0 | 7/30 | 0.77 | 0.97 | 0.80 | 1.00 |
| after fix | 0 | 2/30 | 0.93 | 1.00 | 0.80 | 1.00 |
| final code | 0 | 1/30 | 0.97 | 1.00 | 0.80 | 1.00 |

\*labelled handoff, agent replied or closed. ±1 ticket between runs is sampling noise.

**Wrong:** answerable tickets were handed off when the FAQ answer is a pointer or a requirement: E01 (crash on
iOS 15, F12 says iOS 16+) and E10 (maintenance tonight, F11's status page). E02 (VAT number) was read as a
contract change. **Fix:** the prompt defines "answers" (steps, facts, an explaining requirement, or the
resource to check), the cancellation flag is narrowed to contract terms, and the checker accepts a pointer.
A 30-ticket dry run showed no client-rule ticket getting a reply.

**Cost: ≈ $0.04 per 100 tickets**, measured from OpenRouter's per-call cost ($0.0123 for 30 tickets).
Triage ≈ 1.9k input tokens (98% cache hits, $0.01/M) + 140 output tokens ($0.50/M) ≈ $0.00009 × 3 samples.
A reply adds a draft and a check call ≈ $0.0003.

## What I would improve

Next steps before real customers, from how production agents are run (Intercom Fin, Sierra, Dutch AP/ACM,
OWASP LLM Top 10):
- **Human-approved drafts first** (LangGraph `interrupt()`), then auto-send per category, with a kill switch.
- **Keep data in the EU** as F09 promises: OpenRouter's EU endpoint with zero retention, pseudonymised names,
  log retention, a DPIA.
- **An AI disclosure in replies** (EU AI Act Art. 50).
- **About 100 tickets labelled by the support team**, a red-team set, and the eval in CI. Priority (0.80) is
  the weakest field.

**One change to the client's rules:** define "contract changes". Read literally it includes upgrading, which
F05 tells customers to do themselves.
