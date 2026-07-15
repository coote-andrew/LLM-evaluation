# Database Connection Lifecycle Proposal

## Problem

Under load — especially when several test runs are in flight with
`ModelConfig.max_concurrency` raised above 1 — Postgres reports too many
connections. The app opens a Django connection per worker **thread**, and for
test runs those threads keep the connection open for the entire LLM HTTP wait
(often up to `default_timeout`, default **120s**). There is no connection
pooling in front of Postgres, and no explicit close around long-running work.

This is not primarily about “forgetting” to load input data then disconnect;
input rows are loaded correctly on the main task thread before fan-out. The
pressure comes from **how concurrent workers touch the ORM during and around
LLM calls**, multiplied by Celery/Gunicorn process count.

## Current architecture (summary)

```
Browser → Gunicorn (web, 2 workers)
              │
              ├─ test run  → Celery worker (or fallback daemon thread in web)
              │                 └─ ThreadPoolExecutor(max_concurrency)
              │                       └─ per-row: ORM cancel check → LLM wait
              │
              └─ eval run  → daemon thread in web process
                                └─ (AI judge / field match) ThreadPoolExecutor
```

| Path | Where it runs | DB during LLM wait? |
|------|---------------|---------------------|
| Test run (`execute_test_run`) | Celery task, or web thread if Redis down | **Yes** — each pool thread queries then waits on HTTP |
| AI judge / field-match eval | Daemon thread inside Gunicorn | Pool threads usually avoid ORM; host thread holds a connection for the whole eval |
| Keyword / Python eval | Daemon thread inside Gunicorn | Sequential ORM; lower pressure |

Relevant code: `core/tasks.py`, `core/views/runs.py`, `core/views/evaluations.py`,
`config/settings.py`, `entrypoint.sh`.

## How runs use the database today

### 1. Input data is loaded up front (this part is fine)

In `execute_test_run`:

1. Load `TestRun` + related objects on the **main task thread**.
2. Query `TestCaseRow` rows (optional `row_limit` / skip from parent).
3. Materialise with `rows = list(rows_qs)` before any LLM work.
4. Prompt building uses in-memory `row.input_fields` — no per-row DB fetch for inputs.

So we are **not** holding a connection open specifically to “keep reading” input
data while waiting for the model. The problematic connections are elsewhere.

### 2. Test-run pool threads: ORM first, then LLM on the same thread

```python
# core/tasks.py — _call_row (simplified)
check = TestRun.objects.only("status").get(id=run_id)  # opens thread-local DB conn
if check.status == RunStatus.CANCELLED:
    return row, None
limiter.wait_if_needed()          # may sleep for RPM
result = call_llm(...)            # blocking HTTP, often tens of seconds
```

Django connections are **thread-local**. The cancel check primes a connection on
that pool thread. Nothing closes it before `call_llm`. The executor reuses
threads across rows, so the connection typically stays for the life of the pool
(and can leak after shutdown if never closed).

**Yes — we keep the connection open until the response is back.** For a busy
thread that is usually many seconds to minutes. From Postgres’s point of view
those sessions sit idle while the process waits on the LLM API.

That is **not wise** for a long-lived HTTP call. A DB session should not be
held across unbounded external I/O.

### 3. Result writes stay on the main thread (good pattern)

Completed futures are consumed on the main task thread. Each result is persisted
inside a short `transaction.atomic()` around `update_or_create` + progress
counters. There is **no** outer transaction wrapping the whole run, which is
correct: we do not hold row locks across LLM calls.

### 4. Eval runs add web-process pressure

Evaluation starts always use `threading.Thread(..., daemon=True)` inside the
**Gunicorn worker** (not Celery). AI judge / field-match still fan out with
`ThreadPoolExecutor`. Even when pool workers skip the ORM, the hosting thread
holds a Django connection for the duration of the eval, competing with normal
request traffic on the same two web workers.

### 5. Settings: no longevity / pooling strategy

`DATABASES` in `config/settings.py` has no `CONN_MAX_AGE`, no `OPTIONS` pool,
and there is no pgBouncer (or similar) in the deployment path. With
`CONN_MAX_AGE` unset, Django’s default is `0` (close at end of each **HTTP**
request). That does **not** apply cleanly to Celery tasks or background threads:
connections remain usable for the life of that thread until closed explicitly.

Celery’s Django fixup closes connections around the **main** task thread
before/after a task. It does **not** close connections created by threads the
task spawned.

## Why connection counts explode

Rough upper bound when things hurt:

```
Gunicorn workers (2)
  + long-lived eval threads inside those workers
  + Celery prefork processes (--concurrency ≈ CPU count, × replicas)
      × concurrent execute_test_run tasks
          × (1 main thread + up to max_concurrency pool threads with ORM)
  + leaked thread-local connections after ThreadPoolExecutor shutdown
  + normal request connections
```

Docs (`TECHNICAL.md`) encourage raising Celery `--concurrency` and replicas
without a connection budget. Each increase of `max_concurrency` on a
`ModelConfig` linearly multiplies DB sessions held during LLM waits.

Example: 4 Celery processes, 2 concurrent runs, `max_concurrency=8` → on the
order of `4 × 2 × (1+8) ≈ 72` Postgres sessions from test-run workers alone,
before web/eval traffic — many idle for ~120s at a time.

## Answers to the direct questions

| Question | Answer |
|----------|--------|
| Are we connect/disconnect appropriately for input data? | **Mostly yes** for loading inputs — bulk load on the main thread before fan-out. |
| Do we keep the connection open until the LLM response is back? | **Yes**, on test-run pool threads, because of the cancel-status ORM query before `call_llm`. |
| Is that wise? | **No.** External HTTP waits should not hold Postgres sessions. |
| Root cause of “too many connections”? | Thread-local ORM use during concurrent LLM work + process fan-out + no pooler / no explicit close — not the input materialisation step. |

## Proposed improvements (implementation status)

Phases 1–2 and hygiene from this proposal are implemented:

- Test-run pool threads use a shared `threading.Event` for cancel (no ORM).
- Eval runs dispatch via Celery (`execute_*_eval` tasks) with the same broker
  fallback helper as test runs.
- `MAX_MODEL_CONCURRENCY` caps pool size; `CONN_MAX_AGE` / `DB_APPLICATION_NAME`
  are configurable via env.

Phase 3 (pgBouncer) remains a deployment concern when scaling further.

Ordered by impact vs effort. These can be sequenced; the first two alone should
remove most of the idle-conn spike during runs.

### Phase 1 — Stop holding DB connections across LLM calls (high impact, low risk)

**1a. Remove ORM from LLM worker threads (preferred)**

- Stop calling `TestRun.objects...` inside `_call_row`.
- Pass a shared cancel flag (e.g. `threading.Event`) updated by the main thread,
  or only check cancel on the main thread when scheduling / consuming futures.
- Goal: pool threads never touch Django ORM → no per-thread Postgres session
  for the duration of the LLM wait.

**1b. If a thread must query the DB, close before waiting**

```python
from django.db import connection, close_old_connections

close_old_connections()
# short ORM use
...
connection.close()   # or close_old_connections() again
# THEN call_llm / sleep
```

Apply the same hygiene at ThreadPoolExecutor shutdown (`connections.close_all()`
in a `finally` on worker entry/exit if threads still touch the DB).

**1c. Cap concurrency against a connection budget**

- Document / enforce an upper bound on `ModelConfig.max_concurrency` relative to
  Postgres `max_connections` and Celery process count.
- Treat Celery `--concurrency` and worker replicas as part of the same budget
  (TECHNICAL.md currently encourages increases without that framing).

### Phase 2 — Move evals off the web process (medium impact)

- Run keyword / AI judge / field-match / Python evals via Celery tasks, same as
  test runs, instead of `threading.Thread` inside Gunicorn.
- Keeps long-lived work out of web workers and makes connection lifecycle easier
  to reason about (one task process model).

### Phase 3 — Connection pooling at the edge (high impact for scale)

- Put **pgBouncer** (transaction pooling mode) in front of Postgres for web +
  workers, so many Django “connections” multiplex onto fewer real backends.
- Decide `CONN_MAX_AGE` intentionally:
  - With transaction pooling: usually keep app-side connections short-lived
    (`CONN_MAX_AGE=0` or small) and let pgBouncer do the pooling.
  - Without a pooler: a modest `CONN_MAX_AGE` only reduces connect churn; it does
    **not** fix threads holding sessions open during LLM waits.

### Phase 4 — Hygiene and observability (lower / ongoing)

- Align runtime settings docs with code (`DB_*` vs documented `DATABASE_URL`).
- Set Postgres `application_name` (or distinct users) for web vs worker to
  inspect `pg_stat_activity` when limits are hit.
- Optionally process rows in chunks rather than submitting every future at once
  (helps memory and cancel responsiveness; secondary for connection count once
  Phase 1 is done).
- Prefer Celery over the Redis-down web-thread fallback in production; document
  that fallback as emergency-only because it nests the same pool pattern inside
  Gunicorn.

## Recommended sequence

1. **Phase 1a** (no ORM in `_call_row`) + explicit connection close on pool
   shutdown — largest reduction in idle Postgres sessions during runs.
2. Confirm with `pg_stat_activity` during a concurrent high-`max_concurrency` run.
3. **Phase 2** (evals on Celery) if web workers still spike or feel sticky under
   eval load.
4. **Phase 3** (pgBouncer) when process/replica count must grow further, or when
   platform Postgres `max_connections` stays low.

## Out of scope / non-goals

- Changing how `TestCaseRow` input JSON is stored or queried for prompts.
- Holding a single transaction open for an entire run (that would be worse).
- Switching away from threads for LLM HTTP (async clients could help process
  efficiency later; they are not required to fix the connection holding bug).

## Success criteria

- Concurrent test runs with elevated `max_concurrency` no longer approach
  Postgres `max_connections` under normal Celery/Gunicorn sizing.
- Pool threads show **no** open Django/Postgres sessions while blocked on
  `call_llm` (verify via `pg_stat_activity` / `application_name`).
- Existing behaviour preserved: cancel still works, results still persist
  per-row on completion, rate limits still respected.
- `make test` covers cancel-without-ORM-in-workers and connection-close hygiene
  where practical with mocks.

## File map

| Concern | Location |
|---------|----------|
| Test-run task + thread pool | `core/tasks.py` |
| Run dispatch / Redis fallback | `core/views/runs.py` |
| Eval threads + judge pools | `core/views/evaluations.py` |
| LLM HTTP client | `core/services/llm_client.py` |
| `max_concurrency` | `core/models.py` (`ModelConfig`) |
| `DATABASES` | `config/settings.py` |
| Gunicorn workers | `entrypoint.sh` |
| Celery concurrency notes | `TECHNICAL.md` §6 |
