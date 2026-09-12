# Text-Only Multiagent Tasks

Explicitly submitting a task run starts real, potentially billable model calls automatically in the API process. There are no stubs, inference fallbacks, tools, subprocesses, code execution, or simulation execution. Existing provider onboarding supplies credentials, including session-only credentials, through `provider_api.settings`. Task endpoints do not accept API keys. Only OpenAI and Anthropic are supported.

## API Contract

All paths below start with `/api/stores/{store_id}/task-runs`. Store and run IDs must be UUIDs. Stores are checked through `storage.get_store`; cross-store run access returns 404.

| Method | Suffix | Request | Response |
| --- | --- | --- | --- |
| GET | (none) | None | 200 `{"runs": [full_run, ...]}`, newest 50 |
| GET | `/{run_id}` | None | 200 full run |
| POST | (none) | Run input below | 202 full run with `status: "queued"`; automatic execution |
| POST | `/{run_id}/cancel` | `{}` | 200 full run; terminal runs remain unchanged |
| POST | `/model-options` | `{"provider": "openai"}` or `{"provider": "anthropic"}` | 200 `{"models": [{"id": "...", "name": "..."}], "lead_model": "..." or null, "worker_model": "..." or null, "reason": "..."}` |

```json
{
  "goal": "Draft and review a solution",
  "provider": "openai",
  "lead_model": null,
  "worker_model": null,
  "max_tasks": 6,
  "max_revisions": 2
}
```

`goal` is nonblank and at most 12,000 characters. Model overrides are optional/null or model IDs up to 200 characters. `max_tasks` is a strict integer from 1 to 8 (default 6); `max_revisions` is a strict integer from 0 to 3 (default 2). Unknown fields, providers, and invalid schemas return 422 with no reflected input. All routes use the existing `ProviderRoute` guard before body parsing: loopback peer and Host, exact same Origin when present, no forwarding headers, and JSON Content-Type on POST. Requests have a 16 KiB body limit (413), including JSON encoding overhead; this can be stricter than the goal character limit. Successful responses use `Cache-Control: no-store`. Guard errors retain the existing provider-route wording.

Missing credentials return 422 before a run is created; unavailable keyring/persistence/runner returns 503. Queue saturation returns 429. Discovery errors on `/model-options` return the provider's sanitized error status; listing does not perform inference. A submitted run reports subsequent discovery, override, planning, inference, and review errors through `status: "failed"` and `error`, not a later HTTP response. No extra frontend start/next/plan endpoint is required.

## Run Representation

A full run contains `id`, `store_id`, `goal`, `provider`, `lead_model`, `worker_model`, `status`, `tasks`, `reviews`, `events`, `final_output`, `error`, `created_at`, `updated_at`, `max_tasks`, `max_revisions`, `revision_count`, and `usage`. Timestamps are UTC ISO 8601. Usage contains integer `input_tokens`, `output_tokens`, `model_calls`, and `unknown_calls`; discovery is not an inference call. Token usage is accounted when returned, including provider error responses when available. Calls cancelled before usage arrives can still be billed upstream. A separate idempotent accounting operation records known late usage without changing terminal workflow status or artifacts. `unknown_calls` counts attempts without complete token usage and must not be interpreted as free calls.

Statuses are `queued`, `discovering`, `planning`, `running`, `reviewing`, `completed`, `failed`, `cancelled`, and `interrupted`. Each task contains `id`, `title`, `instructions`, `depends_on` (task IDs), `acceptance_criteria` (strings), `status` (`pending`, `running`, `completed`, `failed`), `model`, `attempts`, `output`, and `error`. Reviews contain `approved`, `feedback`, `revision_tasks`, and `final_output`. Events contain `time` and `message`; only the newest 100 events are retained.

The planner must return strict JSON with unique IDs and a valid DAG (no duplicate/missing/self/cyclic dependencies). Ready workers use the goal, instructions, criteria, and completed dependency outputs. The lead reviewer receives all task criteria and outputs, and synthesizes a nonempty final answer when approving. Rejection reruns the specified tasks and every transitive dependent; independent outputs are retained. Previous invalidated outputs remain available as revision context until replaced. Invalid schemas fail once rather than triggering unbounded repair calls. Exhausted revisions fail while retaining task and review artifacts.

Completed `final_output` always ends with this server-controlled limitation:

> This is model-based text/code drafting and review only. Code and simulations have not been executed; correctness has not been verified.

The reviewer is explicitly forbidden from claiming executed VQE experiments or verified simulation results. Outputs remain untrusted model drafts, not proof of correctness.

## Models And Bounds

Each submitted run discovers accessible model IDs again. Options filter recognized text-generation families and exclude embeddings, audio, image, search, Codex, and tool-specific families. Lead selection heuristically ranks known family/tier/version; worker selection prefers mini/nano or Haiku when available. Recommendations are not claims about price, actual inference access, or the objectively best model. Overrides must appear in the recognized discovery list. Empty/legacy-only lists may have null recommendations; explicit compatible overrides are required in that case. No model ID is invented and an inference failure never silently selects another model.

- At most 20 active runs are admitted, across all stores; model-options requests have a separate 20-request admission bound.
- A shared semaphore permits at most two upstream calls, including discovery, across workflows and model-options requests.
- Each run schedules at most two workers concurrently.
- Total run time is 900 seconds, including semaphore waits. Inference calls are limited to 90 seconds; discovery has the provider's existing 30-second timeout.
- At most eight tasks, four rounds, and 37 inference attempts (one plan, 32 worker attempts, four reviews).
- Model input is capped at 200,000 UTF-8 bytes and 60,000 `cl100k_base` tokens. Oversized contexts fail explicitly rather than silently dropping dependencies.
- Provider output is capped at 6,000 tokens, 24,000 text characters, and 1 MiB response bytes. Review synthesis is capped at 16,000 characters plus the fixed limitation. Reasoning can consume output budget; incomplete responses fail without an automatic retry.
- Provider URLs are fixed HTTPS endpoints; redirects and environment proxies are disabled. OpenAI Responses requests specify `store: false`.

## Persistence And Lifecycle

Runs are stored separately in `~/.self_mcp/task_runs.json` using thread/process locks, atomic replacement, and private files. Listing shows the newest 50 per store, but never deletes historical runs. File and keyring work runs in a threadpool, not on the event loop. Configured credentials and common key patterns are redacted before persistence; errors never include raw provider response bodies or validation input.

Run exactly one API worker. A nonblocking `flock` on `task_runs.runner.lock` is held for the API lifespan; a second process fails startup **before** touching active runs. The lock file may remain after exit; ownership is the live OS lock, not file existence. This works with the existing single-worker uvicorn reload setup without changing startup arguments.

Startup marks persisted active runs `interrupted` and never replays paid work. Shutdown stops admission, persists `interrupted` for active runs before cancelling tracked HTTP/workflow tasks (preventing late completion), and releases ownership. User cancellation sets an in-memory stop fence, persists `cancelled`, then cancels the workflow and its worker HTTP calls; late responses cannot complete it. If the cancellation write fails, local execution is still stopped and the API reports a 503 with potentially stale persisted status, rather than pretending cancellation was saved. Critical startup and submission operations settle before propagating caller cancellation, so ownership cannot be stranded and a committed run cannot be left without a supervising job. Cancellation cannot undo work already accepted by a provider. Store existence is checked before stages and model calls, including after semaphore waits; deletion stops subsequent calls, but cannot retract an already dispatched request.

## Diagnosing Provider Limit Errors

A failure immediately after `Lead is planning` with the older message `Provider rate
limit or quota reached` means the provider returned HTTP 429 on the planning call.
It does not indicate a local task-count or revision limit. Successful model discovery
does not establish inference quota. Old logs cannot distinguish the upstream subtype
because raw error bodies were intentionally not stored.

New failures classify known provider error codes without exposing upstream messages:

- OpenAI `insufficient_quota` / billing-limit codes: check API credits, billing and
  key-specific project/organization limits. A ChatGPT subscription is separate.
- OpenAI `rate_limit_exceeded` or Anthropic `rate_limit_error`: wait, then check
  model/account request and token limits, including traffic from other clients.
  A bounded numeric `Retry-After` value is shown if supplied; no retry is automatic.
- Unknown or malformed 429 responses: report that the cause is ambiguous and check
  both billing/quota and rate limits, rather than assuming it is temporary.

The same diagnostics are used for model discovery and task inference. This does
not raise application limits, alter model choices, or retry paid requests.

## Offline Verification

Run `.venv/bin/python -m unittest discover -s tests -v` with Python 3.12. Task tests use temporary persistence, mocked credential environment/keyring/model discovery/inference, in-process ASGI requests, and HTTP mock transports. Token counting is mocked to avoid tokenizer cache downloads. No live keys, personal data, upstream calls, listeners, or startup port-killing commands are used.

The project Tasks tab uses `src/static/tasks.js` independently of provider onboarding.
It polls status only while visible, ignores stale responses after changing projects,
and never retries submissions or restarts a failed run automatically. History lists
the latest 50 full runs; active selected-run status is polled every 2.5 seconds.
Manual refresh updates history and provider availability. Generated outputs are
rendered as text, not executable HTML. All limits and unexecuted-code limitations
are visible before submission, and billable-call acknowledgement is required.
Unconfirmed submissions remain blocked until a matching new run is observed during
history refresh or the user explicitly authorizes another attempt and acknowledges
charges again. This is a UI safeguard, not backend idempotency: deliberately repeated
POST requests still create separate runs.

Run `node tests/tasks_ui.cjs` and `node tests/provider_ui.cjs` using an external
Playwright installation via `PLAYWRIGHT_MODULE` if needed. `CHROME_PATH` can select
an installed Chrome executable. Browser tests use temporary profiles, an ephemeral
local static server, mocked APIs, and no real credentials or model calls.
