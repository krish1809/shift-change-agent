# Evaluation report

## 1. Summary

| Layer | Result |
|---|---|
| pytest suite (fake LLM, real Postgres, frozen clock) | **299 passed** |
| Evaluation set, fake mode (`eval/results.fake.jsonl`) | **60 / 60 passed** (9 categories) |
| LLM calls per message (all eval turns) | mean **1.01**, max **2** (cap 4) |
| In-process reply latency, fake model (84 turns) | p50 58 ms, p95 100 ms, max 1,152 ms (max = deliberate tool timeout/retry cases) |
| Evaluation set, live mode | **not run yet**: needs a Groq/Gemini key (`make eval-live`) |
| p95 latency with a real LLM | **not measured yet** (`make bench` → `eval/latency.json`) |

The p95 < 5 s requirement is therefore only *designed for* and *measured against the fake model* so far;
the live number must be filled in after running the bench with a real key.

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

## 5. Known failures and limitations

- Live-model behaviour (tool choice, span extraction, Hinglish) is **unmeasured** until a key is supplied; expected weak spots are free-tier models emitting `20:30` instead of the user's words (handled by span grounding, but it turns into an extra question) and occasional malformed tool calls (one repair attempt, then a plain fallback).
- Rule-based degraded mode only understands canonical phrasings by design.
- A turn that exceeds the 8 s deadline answers honestly that it could not finish and asks the employee to check their schedule, because a write may have committed.
- No real push notifications; the UI polls. Single timezone per company. No seat holds during approval.

## 6. Trade-offs (with reasons)

- **Templates over LLM prose**: fewer hallucinated confirmations and lower latency, at the cost of warmth.
- **No seat hold while waiting for a manager**: better seat utilisation, at the cost of the honest `APPROVED_BUT_UNAVAILABLE` path.
- **Thread locked while pending**: simpler state machine; the employee starts a new chat for unrelated changes.

## 7. Reproduce

```bash
docker compose run --rm tests        # 299 tests
docker compose run --rm eval         # writes eval/results.fake.jsonl (60/60)
make eval-live                       # needs GROQ_API_KEY + GROQ_MODEL in .env
make bench                           # live p50/p95 -> eval/latency.json
./scripts/e2e_restart.sh             # real `docker compose restart api` between escalation and approval
```
