# Evaluation report

> A formatted version of this report is in [`EVAL_REPORT.pdf`](EVAL_REPORT.pdf) (built from `docs/report/eval_report.tex` by `python -m scripts.build_report`, with the numbers read from the results files).

## 1. Summary

| Layer | Result |
|---|---|
| pytest suite (fake LLM, real Postgres, frozen clock) | **321 passed** |
| Evaluation set, fake mode (`eval/results.fake.jsonl`) | **60 / 60 passed** (9 categories) |
| LLM calls per message (all eval turns) | mean **1.01**, max **2** (cap 4) |
| In-process reply latency, fake model (84 turns) | p50 58 ms, p95 100 ms, max 1,152 ms (max = deliberate tool timeout/retry cases) |
| Evaluation set, live mode (Groq `openai/gpt-oss-20b`, Gemini `gemini-3.1-flash-lite` fallback) | **36 / 36 passed** (the 36 cases that make sense with a real model; the other 24 need scripted bad output, restarts or fault control) |
| Reply latency with the real LLM | final live run (51 turns): p50 **0.7 s**, p95 **1.6 s**, max 2.6 s · earlier 76-message benchmark under free-tier throttling (`eval/latency.json`): p50 1.9 s, p95 4.1 s · always **1** LLM call per message |

p95 < 5 s holds with a real model, but with a thin margin: the tail is the free tier. When Groq answers 429 the
agent retries on the Gemini fallback, which takes 3-4 s. A paid tier or a second fast provider would remove that tail.

## 2. Method

- **pytest** proves code-level guarantees: rules (boundaries per company), time resolver (≥ 60 cases), atomic booking (60 real races), tool gate/executor, graph conversations, approval flow with **real restarts** (the whole app is shut down and a new one built on the same database), failure matrix, hostile inputs. Tests never call a real LLM.
- **Evaluation set** (`eval/cases.jsonl`, 60 cases): end-to-end through the real app, in-process. Fake mode scripts the model output per step (including wrong/malicious/garbage output); live mode ignores the scripts and uses the real model. Cases can restart the app, move the clock, arm tool faults, send concurrent requests, record a decision without resuming, run the sweeper, and call the API directly.
- **Judging is deterministic**: outcome status, tools called, DB state (roster, seats, request rows), LLM-call budget, HTTP status and substrings of *templated* text. No LLM-as-judge: it would add quota dependence and noise. The harness was validated against a deliberately wrong expectation (it fails it).
- Results file format: one line per case with exactly `id, input, expected, actual, pass, notes`.

## 3. Results by category (fake mode)

| Category | Pass / total |
|---|---|
| isolation | 9 / 9 |
| cutoff | 7 / 7 |
| resume_after_restart | 8 / 8 |
| tool_selection | 10 / 10 |
| tool_failure | 8 / 8 |
| clarification | 7 / 7 |
| capacity | 5 / 5 |
| concurrency_bonus | 2 / 2 |
| input_safety | 4 / 4 |

## 4. Problems found while building and testing (what failed and why)

1. **Deadlock in the last-seat race.** Inserting a request takes a KEY SHARE lock on the trip (FK check); a later `SELECT … FOR UPDATE` conflicted with the other transaction's lock. Fixed by locking both trip rows in id order with `FOR NO KEY UPDATE` before any insert. Found only because the test uses separate real connections, 20 repetitions, no sleeps.
2. **A NUL character in a message returned 500.** Postgres JSONB (the checkpoint) cannot store `\u0000`. Fixed by stripping control characters at the API boundary; regression test added.
3. **Spec vs provided eval case.** Strict span grounding made the agent ask "which day?" when the model assumed "today" (case ISO-02). Resolved by keeping strict grounding for times and accepting an assumed date only for the soonest trip of that type.
4. **Follow-up lost its context** ("the 8:30 one" after a rejection): the draft was cleared too early. Fixed to keep trip type + date after rejections.
5. **"the second one" picked option 1** (matched the bare word "one"). Fixed matching order.
6. **Seed-data default would wipe pending approvals on restart** (`SEED_ON_START=true`). Changed to seed only an empty database.
7. A second session double-submitting would have left that thread paused forever waiting for another thread's request; a duplicate no longer pauses its thread.

7. **Live run #1 passed only 18/36.** Found by running the real model, which the scripted tests could not:
   - *Asked "which day?" for "8 pm drop"* (today and tomorrow both have a drop). A desk executive assumes the next one. Now: known trip type + no date → the **next** trip of that type, and the reply names the day. Unknown trip type still asks (CLR-05).
   - *Grounding was too literal*: the model wrote `9:00 pm` where the user said `9 pm`, so it was treated as invented. Now spelling variants and the same explicit clock time count as grounded, but a bare `8` can never become `20:00` behind the user's back (unit-tested).
   - *`suggested_time` confused the model* into putting explicit times there; it is now internal-only (the gate creates it when a time is invented).
   - *The model sometimes answered in prose* instead of calling a tool; the system prompt now forbids it and a blocked/ungrounded prose reply gets a second, deterministic chance via the rule-based interpreter.
   - *Tool choice*: "what time is my pickup tomorrow?" went to `check_cab_options`; tool descriptions were sharpened (now 10/10 tool-selection cases).
   - *Provider issues*: the Gemini fallback model I first chose (`gemini-2.5-flash-lite`) is retired for new accounts (404) and `gemini-3.5-flash-lite` was too slow (>3 s); `gemini-3.1-flash-lite` works. The default per-call timeout is now 4 s. Free-tier Groq returns 429 under load, which the chain handles (fallback provider, then rule-based, then a plain message).
   - Some of my own cases said "8 pm" without saying drop/pickup; asking was correct, so the wording was fixed (and 3 cases accept a safe alternative in live mode, e.g. a polite prose refusal, recorded in each case's notes).

8. **Code review before submission found five more bugs, each now with a regression test that fails on the old code:**
   - asking again after a manager rejection was reported as a *duplicate* (escalation key didn't vary per offer);
   - in a paused chat, *status* / *withdraw it* could target another chat's request;
   - a chat whose request was withdrawn elsewhere stayed in "waiting for manager" mode;
   - the follow-up "what time instead?" always said *today*;
   - **with the demo clock set ahead of real time every JWT was rejected** (PyJWT checked `iat` against the wall clock). Found by walking through the demo, not by the suite, because test clocks are in the past.
9. **Final live run** on the current code with a fresh provider quota: **36/36**, no provider errors, p95 1.6 s (`eval/results.live.jsonl`).
10. **Free-tier quota.** A later live rerun hit Groq's daily token limit (200,000 tokens/day; one call ≈ 1,250 tokens ≈ 160 messages/day). The affected turns ended in the plain "nothing was changed, try again" reply, which is the designed behaviour. That run was repeated with a fresh quota (item 9).

11. **Fallback-path live run** (Groq out of daily quota, so after one 429 every call went to Gemini `gemini-3.1-flash-lite`): **32/36**, p95 4.07 s, 1 call per message. 2 failures were Gemini timeouts (11 of ~51 calls exceeded the 4 s per-call limit; the turns ended in the plain "nothing was changed" reply), 2 were Gemini listing cab options where a clarifying question is expected (safe, nothing changed, but a different behaviour; expectations were not loosened). Conclusion: Groq stays primary (0.75 s typical, steady); Gemini is a working but slower fallback. Added a 60 s cooldown so a rate-limited provider is skipped instead of costing every message an extra call.

## 5. Known failures and limitations

- Live pass rate depends on provider health: on free tiers a run can hit 429/503 and a case then fails with a plain "nothing was changed, try again" reply (this happened once for INP-03 during development and passed on rerun). The final committed live run is 36/36.
- Free-tier models still occasionally emit a resolved time (`19:30`) instead of the user's words (`30 mins later`); the gate turns that into a confirmation question instead of acting (CLR-07 accepts either outcome in live mode).
- Occasional malformed tool calls get one repair attempt, then a plain fallback.
- Rule-based degraded mode only understands canonical phrasings by design.
- A turn that exceeds the 8 s deadline answers honestly that it could not finish and asks the employee to check their schedule, because a write may have committed.
- No real push notifications; the UI polls. Single timezone per company. No seat holds during approval.

## 6. Trade-offs (with reasons)

- **Templates over LLM prose**: fewer hallucinated confirmations and lower latency, at the cost of warmth.
- **No seat hold while waiting for a manager**: better seat utilisation, at the cost of the honest `APPROVED_BUT_UNAVAILABLE` path.
- **Thread locked while pending**: simpler state machine; the employee starts a new chat for unrelated changes.

## 7. Reproduce

```bash
docker compose run --rm tests        # 321 tests
docker compose run --rm eval         # writes eval/results.fake.jsonl (60/60)
make eval-live                       # needs GROQ_API_KEY + GROQ_MODEL in .env
make bench                           # live p50/p95 -> eval/latency.json
./scripts/e2e_restart.sh             # real `docker compose restart api` between escalation and approval
```
