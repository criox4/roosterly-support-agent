# Roosterly support agent

## How to run

```bash
uv sync                                   # Python 3.12+: LangGraph, langchain-openrouter
cp .env.example .env                      # fill HELPDESK_KEY, CANDIDATE_EMAIL, OPENROUTER_API_KEY
uv run python safety.py && uv run python test_reliability.py   # offline checks
uv run eval.py --tag mine --runs 3        # eval on the 10 labelled examples, posts nothing
uv run agent.py --dry-run                 # decide on open tickets, print, post nothing
uv run agent.py --reset                   # clean run: reset practice tickets, handle all once
```
Re-running `agent.py` is always safe. Logs: `logs/agent.jsonl`. Eval results: `eval/results/`.

## Decisions

1. **A fixed LangGraph pipeline, not an autonomous agent:** `prepare → triage → policy → draft → verify → post`
   (`graph.py`). Every ticket takes the same steps, so a tool-calling loop would only add unpredictability.
   The model makes narrow, schema-constrained judgements. Plain code decides what happens. LangGraph adds
   per-ticket checkpoints and node retries. LangChain is only used to call the model through OpenRouter.
2. **Safety is code, and it only moves tickets towards a human.** The model sets a flag per client rule. A
   nl/en/de keyword list is a backstop. Any rule hit, any P1, a reply without a cited FAQ section, low confidence,
   or **disagreement between 3 triage samples** forces a handoff. I measure "unsure" by self-consistency
   instead of trusting a self-reported score. Ticket text is wrapped as data. Replies are drafted only from the
   cited FAQ sections, so injected instructions (T007, T030) change nothing.
3. **Dutch is handled natively and checked in English.** Translating first costs more and loses nuance.
   Instead, a separate checker call back-translates each draft to English, checks the reply language, and lists
   unsupported claims and promises. Code also blocks links to domains the FAQ does not name, email addresses
   and payment details. Any problem turns the reply into a handoff. The English version is logged (`reply_en`),
   so someone who reads no Dutch can review every reply.

**Assumptions:**
- How-to questions about self-service features (upgrading, F05) get a reply, not a "contract change" handoff.
- Feature requests get a roadmap reply (as in E05).
- A ticket with several issues goes to a human if any part needs one.

**AI tools:** built with Claude Code. Research sub-agents surveyed the frameworks and current support-agent
practice; then code, debugging and test runs. I chose the framework, the uncertainty method and the policy,
and reviewed every dry-run decision.

## Reliability

- **429:** a client-side throttle (4 req/s). On a 429, wait `Retry-After`. **503 / network errors:**
  exponential backoff with jitter, up to 8 tries. Other 4xx errors are not retried.
- **409, never two answers:** the ticket is re-fetched before every POST and skipped if handled. A POST that
  was saved but whose response got lost returns 409 on retry, and that counts as done. Tested: two agents
  running on the same tickets at once gave zero double answers.
- **Crash:** each ticket is a LangGraph thread in `checkpoints.db`. A re-run resumes an unfinished ticket at
  the node where it stopped. Tested with `kill -9` mid-ticket. If a model call fails, that ticket stays open
  and the run continues.
- **Security:** keys come only from `.env` (git-ignored). Card numbers (Luhn), IBANs (mod-97), CVCs and expiry
  dates are redacted before ticket text enters the graph, so the model, checkpoints and logs never see them.
  Everything posted is redacted again.

## Eval and fix

`eval.py` runs the graph without `post` on the 10 examples from `GET /examples`, using the API's labels.
3 runs × 10 examples:

| | unsafe | over-escalated | action | category | priority | language |
|---|---|---|---|---|---|---|
| baseline | 0 | 7 / 30 | 0.77 | 0.97 | 0.80 | 1.00 |
| after fix | 0 | 2 / 30 | 0.93 | 1.00 | 0.80 | 1.00 |
| final code | 0 | 1 / 30 | 0.97 | 1.00 | 0.80 | 1.00 |

*Unsafe* = labelled handoff, but the agent replied or closed.

**Wrong:** answerable tickets were handed off when the FAQ answer is a pointer or a requirement:
- E01: crash on iOS 15, while F12 says the app needs iOS 16.
- E10: maintenance tonight, which F11 answers with the status page.
- E02 (change the VAT number) was sometimes read as a contract change.

**Fix:**
- The triage prompt now defines "answers": steps, facts, a requirement that explains the problem, or the
  resource to check.
- The cancellation flag is narrowed to contract terms.
- The checker accepts a pointer as an answer.

A 30-ticket dry run confirmed that no client-rule ticket started getting replies. After the fix I also added
"P1 always human" and the link/PII filter. With 3 samples at temperature 0.8, ±1 ticket between runs is noise.

**Cost: ≈ $0.04 per 100 tickets**, measured from OpenRouter's per-call cost summed in the logs
($0.0123 for 30 tickets). A triage call is ~1.9k input tokens, 98% of them cache hits at $0.01/M, plus ~140
output tokens at $0.50/M, ≈ $0.00009. Three calls per ticket ≈ $0.00027. Replies add a draft and a check
call ≈ $0.0003. The practice run (17 handoffs, 11 replies, 2 closes) works out to $0.041 per 100 tickets.

## What I would improve

**Next step, before real customers.** Compared with how production support agents are run (Intercom Fin,
Sierra, the Dutch AP/ACM chatbot opinion, OWASP LLM Top 10):
1. **Human approval first.** Start with drafts approved by an agent (LangGraph `interrupt()`), then allow
   automatic sending per category, with a kill switch.
2. **Data residency.** Ticket text currently reaches US model providers, which contradicts Roosterly's promise
   to keep data in the EU (F09). Use OpenRouter's EU endpoint with zero data retention, pseudonymise names
   before the model call, set log/checkpoint retention, and write a DPIA.
3. **AI disclosure in every reply.** EU AI Act Art. 50, which applies from Aug 2026.
4. **A bigger eval.** ~100 tickets labelled by the support team, a red-team set, the checker validated
   against human judgement, run in CI. Priority (0.80) is the weakest field: the model reads "money involved"
   too broadly.
5. **Tracing and metrics** (Langfuse): escalation rate, reopen rate, the model and prompt version on every
   decision.

**One change to the client's rules:** define "contract changes". Read literally, the term includes
upgrading, which F05 tells customers to do themselves, so the rules and the help centre contradict each other.
