# Roosterly support agent

## How to run

```bash
git clone <repo> && cd nordavo_takehome
uv sync                                  # Python 3.12+, installs LangGraph + langchain-openrouter
cp .env.example .env                     # fill HELPDESK_KEY, CANDIDATE_EMAIL, OPENROUTER_API_KEY
uv run python safety.py && uv run python test_reliability.py   # offline checks, no API calls
uv run eval.py --tag mine --runs 3       # eval on the 10 labelled examples, posts nothing
uv run agent.py --dry-run                # decide on all open tickets, print, post nothing
uv run agent.py --reset                  # clean run: reset practice tickets, handle all once
```
Re-running `uv run agent.py` is always safe. Per-ticket log: `logs/agent.jsonl`. Eval results: `eval/results/`.
(Some ISPs block `supabase.co` via DNS; a VPN or 1.1.1.1/WARP fixes it.)

## Decisions

1. **A fixed LangGraph pipeline, not an autonomous agent.** `prepare → triage → policy → draft → verify → post`
   (`graph.py`). Research into how support agents are built today (Anthropic's *Building effective agents*,
   OpenAI's CS demo, tau-bench consistency results) pointed the same way: a ticket always takes the same steps, so
   a tool-calling loop only adds unpredictability. The model makes narrow, schema-constrained judgements; plain
   code decides. LangGraph gives per-ticket checkpoints (crash resume) and node retries for free.
2. **Safety is code, and it only moves tickets towards a human.** The model sets one flag per client rule
   (refund, cancellation, personal data, security, legal, churn); a nl/en/de keyword list is OR-ed in as a
   backstop. Any hit, any P1, a reply without a cited FAQ section, low confidence, or **disagreement between 3
   triage samples** forces a handoff. "Unsure" is measured by self-consistency rather than trusting a
   self-reported confidence. Ticket text is wrapped in tags as data; injected instructions (T007, T030) cannot
   change the action, and the reply is drafted from the cited FAQ sections only.
3. **Dutch is processed natively and checked in English.** Dutch is a high-resource language, so translating
   first would add cost and lose nuance. Instead every draft goes to a separate checker call that back-translates
   it to English, detects the reply language, and lists unsupported claims and promises against the FAQ; any
   problem turns the reply into a handoff. The back-translation is in the log, so a reviewer who reads no Dutch
   can check every reply (`reply_en`).

**Assumptions** (where the brief was open): how-to questions about self-service features the FAQ describes
(upgrading, F05) get a reply, not a "contract change" handoff; feature requests get a roadmap reply (as example
E05); a ticket with several issues goes to a human if any part needs one; P1 always goes to a human.
**AI tools:** built with Claude Code: two research sub-agents (framework landscape, support-agent practices),
then code, debugging and test runs in the session. I chose the framework, the uncertainty method and the
policy above, and reviewed every decision in the dry runs.

## Reliability

- **429:** client-side throttle at 4 req/s; on a 429 wait `Retry-After`, then retry. **503 / network errors:**
  exponential backoff with jitter, up to 8 tries (the API saves nothing on a 503). Other 4xx are not retried.
- **409 / never two answers:** before every POST the ticket is fetched again and skipped if handled. A POST that
  was saved but whose response was lost comes back as 409 on retry and counts as done. Tested: two agents running
  at once on the same tickets produced zero double answers.
- **Crash:** each ticket is a LangGraph thread in `checkpoints.db`; a re-run resumes an unfinished ticket at the
  node where it stopped, and handled tickets are no longer listed as open. Tested with `kill -9` mid-run.
- **Security:** keys only from `.env` (git-ignored). Card numbers (Luhn) and IBANs (mod-97), CVCs and expiry
  dates are redacted *before* text enters the graph, so the model, checkpoints and logs never see them, and again
  on everything posted. A model failure leaves that ticket open and logs it; the run continues.

## Eval and fix

`eval.py` runs the graph (without `post`) on the 10 examples from `GET /examples`; labels come from the API.
3 runs × 10 examples, `openai/gpt-6-luna`:

| | unsafe (label handoff, agent didn't) | over-escalated | action | category | priority | language |
|---|---|---|---|---|---|---|
| baseline | 0 | 7 / 30 | 0.77 | 0.97 | 0.80 | 1.00 |
| after fix | 0 | **2 / 30** | **0.93** | **1.00** | 0.80 | 1.00 |

**Wrong:** answerable tickets were handed off when the FAQ answer is a pointer or a requirement: E01 (crash on
iOS 15 → F12 needs iOS 16) and E10 (maintenance tonight → F11 status page); E02 (change VAT number) was sometimes
read as a "contract change". **Fix:** define "answers" in the triage prompt (steps, facts, a requirement that
explains the problem, or the resource to check), narrow the cancellation flag to contract terms, and let the
checker accept a pointer as an answer. A 30-ticket dry run confirmed no client-rule ticket started getting replies.

**Cost per 100 tickets: ≈ $0.04** (measured, OpenRouter cost per call summed in the logs: $0.0123 for 30 tickets).
Per ticket: 3 triage calls × (~1.9k input tokens, 98% cache hits at $0.01/M, ~140 output at $0.50/M) ≈ $0.00027;
replies add a draft and a check call ≈ $0.0003. 30 tickets ≈ 17 handoffs + 11 replies + 2 closes → $0.041/100.

## What I would improve

**Next step:** priority. It is stuck at 0.80: the model reads "money involved" too broadly (a VAT how-to as P2)
and calls a single-user crash P2. I would get ~20 practice tickets labelled by the support team, add a priority
rubric, and measure on that wider set instead of tuning on 10 examples. After that: log the triage samples'
disagreement rate in production as a drift signal, and process tickets concurrently.
**One change to the client's rules:** define "contract changes". Read literally it includes upgrading, which
F05 tells customers to do themselves, so the rules and the help centre contradict each other.
