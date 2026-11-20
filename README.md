# Shift-Change Request Agent

A multi-tenant chat agent that handles employee cab shift-change requests: it reads only that
employee's roster, checks cab options, applies **each company's own rules in code**, escalates to a
transport manager when a rule is broken (pausing until a decision arrives, even across a restart), and
tells the employee plainly what happened.

> **The LLM proposes, code disposes.** The model understands messy language and picks a tool. Code owns
> identity, tenant isolation, business rules, seat booking, approvals, and every sentence that states a
> fact about a trip.

## Run it (one command)

```bash
docker compose up --build        # API + demo UI on http://localhost:8000
```

Open <http://localhost:8000>: left pane is the employee chat (pick a seeded person, *Sign in* mints a dev
JWT and shows its claims; your own trips are shown above the chat), right pane is the transport-manager console
(pending queue with approve/reject, history, per-request audit trail). The header has the demo controls: the agent
clock (presets 3:00 / 5:30 / 6:45 PM to show the cut-off rules), a tool-timeout switch and *reset demo data*.
Swagger is at `/docs`. No API key is needed to start: without one the agent uses a deterministic
rule-based interpreter for canonical phrasings (e.g. *"change my drop today to 8:30 pm"*).
For the full natural-language experience, copy `.env.example` to `.env` and set `GROQ_API_KEY` +
`GROQ_MODEL` (and optionally the Gemini pair as fallback). **Never commit `.env`.**

| Task | Command |
|---|---|
| Tests (fake LLM, real Postgres) | `docker compose run --rm tests` |
| Evaluation set (offline, fake LLM) | `docker compose run --rm eval` → `eval/results.fake.jsonl` |
| Evaluation set with a real model | `make eval-live` → `eval/results.live.jsonl` (36/36 with Groq `gpt-oss-20b`) |
| Latency benchmark (real model) | `make bench` → `eval/latency.json` |
| Restart proof against docker | `./scripts/e2e_restart.sh` |
| Mint a token | `make token COMPANY=acme EMPLOYEE=A001` |

Seeded people: Acme Tech `A001..A005`, manager `AM01`; Globex Finance `G001..G003`, manager `GM01`.
The demo clock (`/dev/clock`, or the UI box) lets you show cut-off behaviour at any time of day.

## Architecture

```
text ─► [LLM: understand + pick a tool, copy verbatim spans] ─► [Gate: allowlist, schema, identity,
span grounding] ─► [Tools ─► rules engine (pure) ─► atomic SQL booking] ─► [Templates render the reply]
                         └─ rule broken + employee agrees ─► interrupt() ─ Postgres checkpoint ─► manager
                            decision (outbox) ─► resume ─► re-validate ─► apply ─► notify employee
```

Stack: Python 3.12, FastAPI, LangGraph 1.x (Postgres checkpointer), Postgres 16, PyJWT, Pydantic v2,
psycopg 3, langchain-openai (OpenAI-compatible client for Groq/Gemini). One process, one database; no Redis.
Full design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), behaviour contract: [docs/SPEC.md](docs/SPEC.md).

## Where the LLM earns its place, and where plain code is better

| Job | Owner | Why |
|---|---|---|
| Understanding "can I go a bit late?", "make it 9 instead", "the 8:30 one", basic Hinglish | **LLM** | Real language variety; this is the model's job |
| Choosing the tool | **LLM**, validated by the gate | Intent routing over fuzzy language |
| Turning "8", "half past 8", "30 min later" into a time | Code (`time_resolver.py`) | Deterministic, testable, asks when ambiguous |
| Who the user is, which company | Code (JWT only) | Must never come from user text or model output |
| Cut-off, capacity, change window, daily limit | Code (`domain/rules/`) | Exact, per company, unit-tested at the boundaries |
| Nearest free cab | Code (`alternatives.py`) | A sort by time distance; an LLM adds nothing |
| Booking the seat | Code (one SQL transaction) | Atomic under concurrency |
| Every sentence that states an outcome | Code (`templates.py`) | A made-up confirmation is worse than none |
| Small talk ("thanks!") | LLM, through a guard that blocks outcome claims | Harmless if it can't claim anything |

## How we stay under 4 LLM calls per message

A counter in code (`TurnBudget`, `app/llm/base.py`) wraps the model for each turn and raises at the cap
(`MAX_LLM_CALLS_PER_TURN`, default 4). Every request that reaches a provider counts, including failed ones and a
fallback retry (Groq fails → Gemini = 2 calls), so the cap holds under the strictest reading. Typical turn = **1** call, because:
the roster snapshot is already in the prompt (no read-tool round trip); after a mutating tool the model is
**not** called again (a template states the outcome); clarifying questions are templates too. The only
extra call is one *repair* attempt when the model emits an invalid tool call. Measured over the eval set:
max 2 calls in any turn, mean 1.01 (scripted); with a real model: always 1.

## How isolation is enforced (and the test that proves each layer)

| Layer | Mechanism | Proof |
|---|---|---|
| Identity | Only the verified JWT (HS256, `alg` allowlist, iss/aud/exp, role re-checked in the DB) | `test_auth.py` (tampered, expired, `alg=none`, wrong role) |
| API bodies | No identity fields; `extra="forbid"` → 422 | `test_body_identity_fields_are_rejected`, ISO-07 |
| Model output | Tool schemas have no identity fields; gate rejects extras | `test_gate_rejects_identity_fields`, ISO-09 |
| Repositories | Every function takes `company_id` first and filters by it | `test_every_repository_function_requires_company_id` |
| Database | Composite FKs `(company_id, …)`: a cross-tenant reference can't be stored | `test_cross_tenant_fk` |
| Threads | `thread_id = company:employee:session`, built server-side | `test_session_id_of_another_user_is_an_empty_separate_thread`, ISO-05 |
| Wording | Templates only get the caller's company/policy | `test_acme_cutoff_offers_escalation_and_acme_numbers_only`, ISO-08 |
| Manager | Same company or **404** (not 403, so existence isn't leaked); also for the audit trail | `test_iso06_other_company_manager_gets_404…`, `test_manager_queue_is_readable_and_audit_trail_shows_who_decided` |

## Design decisions

- **Rules in code, not prompts** (`app/domain/rules/`): pure functions, one per rule, all violations collected; escalatable only if *every* violation is escalatable for that company. Boundary is inclusive (exactly at the deadline is allowed). Cut-off is measured from the *earlier* of current/target trip.
- **The model never resolves times.** It copies the user's words (`"8"`, `"half past 8"`, `"30 min later"`); `time_resolver.py` turns them into times or returns *ambiguous* (a bare "9" with no context → question, never a guess). A time the model "extracted" that the user never said is demoted to a confirmation question (span grounding).
- **Atomic seats**: conditional `UPDATE … WHERE seats_taken < effective_capacity` in one transaction with the roster change, request row and audit row; DB `CHECK` as backstop. This is the bonus: last-seat race and double submit are tested 20× each (`test_concurrency.py`) and in eval (CONC-01/02).
- **Approval pause** uses LangGraph `interrupt()` with the Postgres checkpointer. The manager's decision is written to the DB first (outbox), then the thread is resumed; a **sweeper** delivers missed decisions, expires silent requests, and repairs threads whose pause was never saved. Identity on resume is rebuilt from the stored request row, never from the manager's token.
- **Failures are truthful**: every failure message says what happened, whether anything changed (verified, e.g. a write that committed after a timeout is found by its idempotency key and reported as done), and what to do next. No stack traces ever reach a client (a test client fails any response that leaks one).
- **Provider flexibility**: one OpenAI-compatible client for Groq and Gemini; model ids come from env vars; fallback provider, then the rule-based interpreter, then a plain message.

## Trade-offs

- **Templated replies instead of LLM-written ones**: less warmth, but zero hallucinated confirmations and ~1 s faster turns.
- **No seat hold during approval**: a hold would block a scarce seat for hours. We re-validate when approved and may answer "approved, but the cab filled up" (`APPROVED_BUT_UNAVAILABLE`) with fresh options.
- **Thread paused while pending**: simpler state machine; while waiting, the employee can ask for status or withdraw but must open a new chat for an unrelated change.
- **Polling instead of push** for the approval message in the UI (every 2 s).
- **An assumed date** (the model says "today" though the user didn't) is accepted only when it is the soonest upcoming trip of that type; strict grounding applies to *times*, where a silent guess would change the booking.
- **Postgres advisory locks** serialise a thread across workers instead of introducing Redis.
- **Audit log in the same database** as the bookings, written in the same transaction, so "who asked for what and who decided" can never disagree with what was applied (`GET /manager/requests/{id}/audit`).

## Known limitations

Free-tier capacity: one agent call costs about 1,250 tokens and Groq's free tier allows 200,000 tokens/day for the
model used, i.e. roughly 160 messages a day before the Gemini fallback and then the rule-based mode take over.
A cancelled trip can't be re-booked through the chat. Single timezone per company (seed uses Asia/Kolkata); English plus basic Hinglish only; no real notifications
(messages appear in the chat thread); manager auth is a dev JWT issuer; no seat holds. See
[docs/EVAL_REPORT.md](docs/EVAL_REPORT.md) for test results and known failures.

## Layout

```
app/        api/ auth/ db/ domain/ (rules, time resolver, booking) agent/ (graph, templates, guard, sweeper) tools/ llm/
data/seed.json   two companies with different cut-offs/capacity     eval/   cases.jsonl, run_eval.py, results.*.jsonl
tests/      unit + integration (real Postgres, FakeLLM, FrozenClock)  scripts/ tokens, restart proof, live smoke, bench
```
