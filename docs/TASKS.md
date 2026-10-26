# Text-Only Multiagent Tasks

Explicitly submitting a task run starts real model calls automatically in the API process. Hosted calls may be billable; local models use your hardware. There are no stubs, tools, subprocesses, code execution, or simulation execution. Automatic model selection supports bounded recovery through approved alternatives. Existing provider onboarding supplies access through `provider_api.settings`, including session-only credentials and keyless local profiles. Task endpoints do not accept API keys. OpenAI, Anthropic, native Ollama, and OpenAI-compatible profiles are supported; see [PROVIDERS.md](PROVIDERS.md).

## API Contract

All paths below start with `/api/stores/{store_id}/task-runs`. Store and run IDs must be UUIDs. Stores are checked through `storage.get_store`; cross-store run access returns 404.

| Method | Suffix | Request | Response |
| --- | --- | --- | --- |
| GET | (none) | None | 200 `{"runs": [full_run, ...]}`, newest 50 |
| GET | `/{run_id}` | None | 200 full run |
| POST | (none) | Run input below | 202 full run with `status: "queued"`; automatic execution |
| POST | `/{run_id}/cancel` | `{}` | 200 full run; terminal runs remain unchanged |
| POST | `/{run_id}/messages` | Run input + `message_id` + `expected_turn_id` | 202 same task ID with a new turn; idempotent retries |
| POST | `/model-options` | `{"provider": "openai"}` or `{"provider": "anthropic"}` | 200 `{"models": [{"id": "...", "name": "..."}], "lead_model": "..." or null, "worker_model": "..." or null, "reason": "..."}` |

```json
{
  "goal": "Draft and review a solution",
  "mode": "delegate",
  "provider": "openai",
  "lead_model": null,
  "worker_model": null,
  "include_project_context": false,
  "schema_retries": 1,
  "max_tasks": 6,
  "max_revisions": 2
}
```

`mode` is `delegate` (default, including older runs) or `direct`. Direct mode
discovers approved models and asks the selected model for an answer without any
planner, task DAG, schema corrections, worker calls, or reviewer. It normally uses
one inference call; an automatically selected model may use the existing bounded
recovery policy. `lead_model` selects/pins the direct model. `worker_model` must
be absent/null. `provider: auto` uses only approved connections, with no explicit
model overrides, just as in delegate mode. Context attachment, cancellation,
usage accounting, history, redaction, and the overall run deadline are shared.
Direct answers are saved in `final_output`, with empty `tasks` and `reviews`, and
an explicit notice that code has not been executed or independently verified.

Optional per-run inference settings: `timeout_seconds` (strict integer 30–600),
`output_tokens` (strict integer 256–32768), and `reasoning_effort` (`auto`, `low`,
`medium`, `high`; default `auto`). Null timeout/output values use model defaults.
Custom connections continue to cap output by their saved output budget. Reasoning
overrides are accepted only for a model with an explicit capability profile;
unknown aliases keep their adapter's default and receive no guessed parameters.

`goal` is nonblank and at most 12,000 characters. Model overrides are optional/null or model IDs up to 200 characters. `max_tasks` is a strict integer from 1 to 8 (default 6); `max_revisions` is a strict integer from 0 to 3 (default 2). Unknown fields, providers, and invalid schemas return 422 with no reflected input. All routes use the existing `ProviderRoute` guard before body parsing: loopback peer and Host, exact same Origin when present, no forwarding headers, and JSON Content-Type on POST. Requests have a 16 KiB body limit (413), including JSON encoding overhead; this can be stricter than the goal character limit. Successful responses use `Cache-Control: no-store`. Guard errors retain the existing provider-route wording.

`provider` may also be a saved `profile_<uuidhex>` ID or `auto`. Auto runs require an
explicit configured routing allowlist; model overrides must be absent/null. Auto
model options additionally return `lead_provider`, `worker_provider`, `mode`,
`candidates`, `worker_candidates`, and `warnings`. `models` contains the lead
provider's models only; provider-qualified candidates remove ambiguity between
identical model names. Public runs add `lead_provider`, `worker_provider`,
`routing_mode`, `worker_candidates`, `model_candidates`, `lead_model_pinned`, and
`worker_model_pinned`. Each task records its actual `provider`, `model`, and
`routing_reason`; recovered tasks also record prior pairs and error categories in
`routing_history`. Runs with recovery record `fallback_count`. Historical runs
without these fields remain readable.

The lead suggests subtask routes only from the offered worker pairs. Task-specific
rules select among those pairs unless a worker model was explicitly pinned. Custom model
allowlists and local-only restrictions apply before selection and before dispatch.
Automatic catalogs are capped at 120 candidates and 12 offered worker routes; a
catalog is not a claim to exhaust every possible model. No new provider permission
can expand an active run's submission-time trust snapshot.

Missing credentials return 422 before a run is created; unavailable keyring/persistence/runner returns 503. Queue saturation returns 429. Discovery errors on `/model-options` return the provider's sanitized error status; listing does not perform inference. A submitted run reports subsequent discovery, override, planning, inference, and review errors through `status: "failed"` and `error`, not a later HTTP response. No extra frontend start/next/plan endpoint is required.

## Run Representation

A full run contains `id`, `store_id`, `goal`, `provider`, `lead_model`, `worker_model`, `status`, `tasks`, `reviews`, `events`, `final_output`, `error`, `created_at`, `updated_at`, `max_tasks`, `max_revisions`, `revision_count`, and `usage`. Timestamps are UTC ISO 8601. Usage contains integer `input_tokens`, `output_tokens`, `model_calls`, and `unknown_calls`; discovery is not an inference call. Token usage is accounted when returned, including provider error responses when available. Calls cancelled before usage arrives can still be billed upstream. A separate idempotent accounting operation records known late usage without changing terminal workflow status or artifacts. `unknown_calls` counts attempts without complete token usage and must not be interpreted as free calls.

Runs and individual worker records may include `inference_settings` with the actual
model, provider, timeout, output budget, and reasoning setting used for their latest
call. Failed runs add `error_action`, controlled advice suitable for the main UI.
HTTP errors retain status and operation with sanitized error categories, never raw
provider messages. Token-budget exhaustion is distinguished from timeouts and model
access errors; reported usage is retained even for incomplete output.

Statuses are `queued`, `discovering`, `planning`, `running`, `reviewing`, `completed`, `failed`, `cancelled`, and `interrupted`. Each task contains `id`, `title`, `instructions`, `depends_on` (task IDs), `acceptance_criteria` (strings), `status` (`pending`, `running`, `completed`, `failed`), `model`, `attempts`, `output`, and `error`. Reviews contain `approved`, `feedback`, `revision_tasks`, and `final_output`. Events contain `time` and `message`; only the newest 100 events are retained.

The planner must return strict JSON with unique IDs and a valid DAG (no duplicate/missing/self/cyclic dependencies). Ready workers use the goal, instructions, criteria, and completed dependency outputs. The lead reviewer receives all task criteria and outputs, and synthesizes a nonempty final answer when approving. Rejection reruns the specified tasks and every transitive dependent; independent outputs are retained. Previous outputs remain available as revision context and are retained in `output_versions` after replacement. Invalid schemas can request up to `schema_retries` correction calls per planning/review stage (0–2; API default 0, GUI default 1). Corrections use the current lead route and include the invalid response with controlled correction instructions. Model recovery and schema corrections share the existing 37-call cap; they do not expand it. When revisions are exhausted, the run completes with a delivered draft and
`review_approved: false`, `completion_reason: "revision_limit"`, and the unresolved
review notes in `final_output`. The final review receives `final_review: true` and
is asked to synthesize the best available answer even without approval. If it
returns null final text, the server assembles the latest full worker drafts without
another inference call. `final_output_source` distinguishes `reviewer` from
`worker_drafts`. Earlier rejection rounds still require null final text. Approved
runs set `review_approved: true` and `completion_reason: "approved"`. API/model
failures, invalid review schemas, cancellation and other safety limits retain
their existing outcomes; delivery is not a claim of correctness.

Completed delegated `final_output` ends with this server-controlled limitation:

> This is model-based text/code drafting and review only. Code and simulations have not been executed; correctness has not been verified.

The reviewer is explicitly forbidden from claiming executed VQE experiments or verified simulation results. Outputs remain untrusted model drafts, not proof of correctness.

## Models And Bounds

Each submitted run discovers accessible model IDs again. Built-in adapters filter recognized generation families; compatible profiles permit unknown aliases but exclude known completion-only IDs, embeddings, other non-chat families, and explicit `chat_capable: false` metadata. A saved incompatible default is ignored. Empty compatible catalogs fail before inference. Overrides must appear in the filtered discovery list.

`model_policy.py` uses a deterministic lookup/ranking policy without extra model calls: coding specialists for implementation/setup/debugging, compact models for extraction/writing, and stronger models for planning/reasoning. General tasks retain a validated planner assignment or compact default. Locality preferences apply before suitability. Recommendations are hints, not benchmarks or price guarantees. No model ID is invented.

An unpinned model can recover from recognized unavailable/unsupported-model, context-length, out-of-memory, rate-limit, or overload errors using at most three distinct model candidates per call. OOM favors a compact alternative. Single-provider runs stay on that provider; automatic runs remain inside their submission-time approved pool and model restrictions. Permission/configuration changes stop execution. Errors requiring credentials or credits, unknown errors, refusals, incomplete outputs, and network timeouts do not trigger recovery. Recovery retains completed task outputs, updates the actual route, logs every switch, and accounts usage from failed calls too. Manually selected roles never switch. See [PROVIDERS.md](PROVIDERS.md) for availability cooldowns.

- At most 20 active runs are admitted, across all stores; model-options requests have a separate 20-request admission bound.
- A shared semaphore permits at most two upstream calls, including discovery, across workflows and model-options requests.
- Each run schedules at most two workers concurrently.
- Total run time is 900 seconds, including semaphore waits. Inference defaults to 90 seconds, or a model-specific bounded timeout (Astra: 300 seconds); per-run overrides allow 30–600 seconds. Discovery retains its separate timeout.
- At most eight tasks, four rounds (initial drafting plus up to three revisions), and 37 inference attempts total, including model alternatives and schema corrections. These are application cost/time safeguards, not limits imposed by a model. The default is two revisions. Exhausting the revision budget delivers an explicitly unapproved draft instead of failing.
- Model input is capped at 200,000 UTF-8 bytes and 60,000 `cl100k_base` tokens. Oversized contexts fail explicitly rather than silently dropping dependencies.
- Built-in output defaults to 6,000 tokens or a model capability default (Astra: 16,384). Custom profiles use their configured context/output budgets (defaults 8192/2048), with a 32,768-token output ceiling. Per-run overrides remain bounded. Both use a 128,000-character text cap and 1 MiB response cap. Review synthesis is capped at 120,000 characters plus the fixed limitation. Token estimates use a generic tokenizer and may differ from a local model's tokenizer. Reasoning consumes output budget too; incomplete responses fail with actionable guidance, without an automatic budget increase.
- Provider URLs are fixed HTTPS endpoints; redirects and environment proxies are disabled. OpenAI Responses requests specify `store: false`.

## Persistence And Lifecycle

All saved final answers, worker output versions, historical message results, and
review notes are projected into project-scoped MCP knowledge (`mcp_knowledge.py`).
`list_task_results`, `search_task_results`, and `get_task_result` retain their
existing contracts and use file-invalidated snapshots without owning the runner,
invoking inference, or modifying the source.
The index covers all project runs, not just the recent-run list's first page.
Search matches case-insensitive substrings across requests, titles, and full
content. Lists/searches return bounded previews; reads use explicit character
paging with `next_offset` so long code is never silently truncated.

Result IDs are deterministic content-and-source hashes. Sources include task,
message, worker/version, and review status. Worker review labels only apply when
the review identifies that exact output version; older drafts are not promoted
to approved because a later draft passed. Every response identifies generated
content as reference material, never instructions or verified facts. MCP resources
expose a live project index and a result URI template. These outputs are not
registered as instruction prompts or duplicated into ordinary saved documents.
Access is restricted to the connected project's existing storage scope, including
reads by guessed IDs/URIs. Legacy saved results are available without migration.

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
  A bounded numeric `Retry-After` value is shown if supplied. Automatic model routing
  may try another approved model; it does not repeatedly retry the same model.
- Unknown or malformed 429 responses: report that the cause is ambiguous and check
  both billing/quota and rate limits, rather than assuming it is temporary.

The same diagnostics are used for model discovery and task inference. Discovery
never invokes a model. Inference recovery shares existing application limits;
additional attempts on hosted models may be billable.

## Collaboration and shared memory

The delegated flow is plan → worker drafts → review of every task → targeted
revisions → combined answer. `CollaborativeReview` requires exactly one
`task_reviews` item per task: `task_id`, `verdict` (`keep`/`revise`), `reason`,
`preserve` (parts worth retaining), and `change` (required for revise, null for
keep). Every revision also requires `criterion_index` (1-based in that task's
original acceptance criteria) and `evidence` identifying the material defect or
missing content and its impact. Nonexistent criteria and missing evidence fail
validation before workers are rerun. Revise verdicts must exactly match `revision_tasks`.
The planner is instructed to make criteria observable, essential, and feasible
for text/code drafting, without requiring execution or adding unrequested scope.
The reviewer puts optional polish in `suggestions`, which does not block approval,
and must not raise the acceptance bar between rounds. The lead is instructed
to evaluate acceptance criteria rather than stylistic preferences, and to combine
worker contributions rather than silently rewrite them. These are model judgments,
not independent correctness verification. Missing/invalid assessments use the
existing bounded schema-repair budget and otherwise stop with saved drafts intact.

Each successful worker draft gets an `output_versions` entry with version, round,
provider, model, and full output. Reviews record the versions they assessed. Only
requested tasks and their dependents run again: the former receive their own repair
instructions; the latter get integration feedback and updated dependency outputs.
Independent work is retained. All versions survive follow-ups and API restarts.
Earlier drafts, per-task reviews and shared context are in the details drawer,
including worker drafts from earlier messages even when a final answer exists.
When work stops without a final answer, the main answer column shows all available
worker drafts, including the last saved version if a revision failed. This also
works for older saved failures. The UI labels these as partial, unreviewed results;
the API retains its actual failed/cancelled/interrupted status. Error details and
recovery guidance appear beside the input. No extra model calls or automatic
retries are used to recover or display those drafts.

Every model call receives deterministic `collaboration_memory`: the current task
map, latest review, up to eight distinct recent design decisions, and six earlier
worker excerpts of at most 400 characters with source turn/task/version references.
Older review decisions are not erased when a newer review omits them. Newer notes
are prioritized; conflicting notes are not automatically resolved as facts.
Omitted counts and truncated-excerpt flags are explicit; full drafts stay saved.
Memory is scoped to this task and marked untrusted, with the latest user request
having precedence. Known credentials are scrubbed before excerpt truncation and
before sending/persistence. The existing conversation context and request budgets
still apply, including fail-before-inference when essential context cannot fit.
`shared_memory` records the most recently submitted context for inspection.

This adds no summarizer calls, vector database, extra review agents, or automatic
cross-project sharing. It is bounded reference memory, not unlimited recall: short
excerpts cannot replace full source code. The model can still make poor judgments;
protocol tests verify retention and routing, not a live model's answer quality.

## Offline Verification

Run `.venv/bin/python -m unittest discover -s tests -v` with Python 3.12. Task tests use temporary persistence, mocked credential environment/keyring/model discovery/inference, in-process ASGI requests, and HTTP mock transports. The cl100k_base tokenizer data is bundled and hash-checked; token counting needs no network or external cache. Some budget tests mock token counts to exercise boundary cases. No live keys, personal data, upstream calls, listeners, or startup port-killing commands are used.

The project Tasks tab uses `src/static/tasks.js` independently of provider onboarding.
It resumes one task per project, preferring the last task used in this browser tab
and otherwise the most recently updated saved task. A compact Saved task selector
identifies the task by title, date, and ID, with pagination to load older separate
tasks. Switching preserves the selected task on reload and refuses to move unsent
text to another task. There is no new-task control or history sidebar. Active task
status is polled every 2.5 seconds, only while visible.
**Refresh status** updates provider availability and checks the saved task without
starting model work. Failed initial/status reads disable submission until recovery,
so a disconnected workspace cannot accidentally start a fresh task.

The workspace has one answer column and a composer with **Ask a model** / **Delegate
a task** modes. The latest successful answer remains visible during a follow-up and
after failure or cancellation, with its source message and date explicitly labelled.
Earlier exchanges appear under Previous messages in this task in the main column,
including dated requests, saved answers, partial drafts, errors, and message usage.
Work breakdown, reviews, usage, and activity are expandable inside a keyboard-accessible details drawer. Model
choices and budgets live in **Settings**; the direct model picker is beside the
composer. Live status, step progress, the details button, and cancellation sit above
the message input, so long answers do not separate progress from the composer.
Polling preserves keyboard focus on these controls. Reloading the model catalog
preserves explicit model choices.

The composer shows whole-task input/output tokens, call attempts, and USD cost
estimates. Tokens & cost in the details drawer separates this message, the whole
task, and all saved tasks in this project, with model and individual-call breakdowns.
Calls include planner, worker, reviewer, correction, and fallback attempts. Missing
provider token reports remain unknown, even after failure or cancellation.
Per-call model/stage metadata is recorded for new calls; older calls without this
metadata are labelled as unattributed rather than assigned to the current model.

Cost estimates use editable input/output prices per million tokens for each
connection/model, stored per project. Prices apply retrospectively to recorded
calls with known models and counts. No rates are guessed; absent rates/counts
produce unavailable or partial estimates, and zero rates must be explicitly saved.
These are estimates, not invoice charges: discounts, fees, caching rates, and
provider billing adjustments are not included. No billing API is contacted.
`GET /task-runs/{run_id}/usage` returns accounting; `PUT /task-runs/pricing` saves
USD prices. Task listing accepts `offset` and `limit` and returns `next_offset`.

Unsent messages and settings are saved per project in browser `sessionStorage` for
this tab, with an in-memory fallback if storage is unavailable. Sending consent is
never stored. Pending submissions are recorded before the HTTP request, including
the exact follow-up message ID/body, so reloads do not lose duplicate protection.
Accepted messages clear the draft, including replies that arrive after navigation.
A status check reconciles accepted messages; unconfirmed follow-ups can be explicitly
retried with the same ID. Initial task creation remains non-idempotent: if it cannot
be confirmed, another initial submission requires explicit acknowledgement.
No submission or inference is automatically retried by the UI.

**Send follow-up** or **⌘/Ctrl + Enter** sends changes to the same task with earlier
messages and results as context. Generated outputs use sanitized Markdown (plain
text fallback); model HTML is never executed. Sending acknowledgement remains
required, and code is saved as a draft rather than executed.

## Continuing a task

The task `id` remains stable across messages. A new `turn_id` identifies each
exchange; `root_goal` is the task title and `goal` is the latest user message.
`turns` holds finished prior turns, including their prompts, outputs, partial drafts,
errors, settings, usage, and review/task details. The top-level execution fields
describe the current turn. `usage` is cumulative across the task; archived turn
usage is per turn, and `turn_start_usage` is the current turn's baseline. The 37-call
cap, revision limit, and 900-second deadline restart for each explicitly submitted
message. Model-call IDs remain monotonic so token accounting never reuses a ledger
entry from a previous turn. Task listing is ordered by latest activity.

To continue, POST the same settings schema used for a new run, with `goal` set to
the follow-up, plus two UUID strings:

```json
{
  "goal": "Keep the existing API and add pagination",
  "provider": "openai",
  "mode": "direct",
  "message_id": "7bd449f1-6448-44a8-9fa4-6385065f608c",
  "expected_turn_id": "3e2c147f-6ecb-48c7-86d8-95c719aeb127"
}
```

Use the task's current `turn_id` as `expected_turn_id`; legacy tasks without that
field use their `id`. Generate one new `message_id` for the follow-up and reuse
the exact body/ID when retrying an uncertain response. An accepted ID returns the
current task without starting more inference, even after later turns. Reusing an
ID with different content returns 409. Active/settling tasks and stale turn IDs also
return 409. Cross-project access returns 404. Each follow-up rechecks current
provider permissions and credentials, then archives and admits the new turn under
one storage transaction. Cancellation settles before another turn can start.

All inference stages receive a `conversation` payload containing ordered earlier
user/assistant exchanges. Planning, workers, review, and direct answers receive
the same continuation instructions to build on prior work. Complete history stays
on disk. For bounded model inputs, the original goal, latest exchange, and last
successful answer are always retained; older full exchanges are added newest first
when they fit. `history_context` reports included/omitted counts, shown in the UI
when necessary. Partial drafts from failed turns are included as drafts. The budget
accounts for the current request and project context, with adapter framing reserved.
If the essential history cannot fit, execution stops before inference with guidance
to shorten the prompt or select a larger context window; it never silently discards
the latest result. Previous answers remain untrusted text, not executed code.

Run `node tests/tasks_ui.cjs` and `node tests/provider_ui.cjs` using an external
Playwright installation via `PLAYWRIGHT_MODULE` if needed. `CHROME_PATH` can select
an installed Chrome executable. Browser tests use temporary profiles, an ephemeral
local static server, mocked APIs, and no real credentials or model calls.

## Shared project retrieval

`include_project_context` is a strict boolean, default false. When enabled, each
planner, worker, reviewer or direct-answer call queries the same local retrieval
service exposed through MCP. `project_context` records the project identity;
`project_knowledge` contains ranked excerpts. Documents, saved link descriptions,
all historical answers, worker output versions and reviews are indexed. No URLs
are fetched, no embeddings or model calls are made, and no SQL service is needed.

`project_knowledge.py` builds an in-memory inverted index over bounded character
sections, with BM25-style term scoring, query coverage, identifier parts and
duplicate-excerpt removal. This is lexical retrieval, not semantic search or RL.
Warm reads stat the JSON files and reuse the index; changed files are reloaded and
unchanged sources reuse their chunks. API and standalone MCP processes each have
their own cache and observe atomic task-file replacements. Cache eviction uses an
eight-project / 32-million-source-character limit per service (a source-size
bound, not a precise RAM limit). An oversized project is served without retaining
its index. The cache is disposable; JSON remains authoritative. Cold indexing,
changed-file parsing, ranking and token counting have measurable local overhead.

The preferred MCP tools are `search_project_knowledge(query, token_budget=1500,
limit=6)` and `get_project_knowledge(source_id, offset=0, limit=12000)`. Search
accepts budgets from 256 to 6000 tokens, at most 20 sources, an optional run filter
and `include_older_versions`. Project identity comes only from the connected MCP
endpoint; no tool accepts another project ID. Direct reads and resource URIs also
enforce this scope, including cached and guessed IDs. This is local project
isolation, not multi-user authentication or authorization for a hosted service.

Excerpts include exact character offsets, source IDs, task/message/version and
review status. Older worker versions are excluded by default; earlier messages
remain searchable and are labeled separately. Review approval does not establish
truth or code execution. Retrieval never silently substitutes an older cached
source after deletion or a storage error. Missing evidence is reported explicitly.

Agent retrieval uses a maximum 1,500-token evidence budget, reduced to fit the
selected model after mandatory conversation, instructions and dependency data.
Token accounting uses `cl100k_base`, an estimate for other model families. Full
current-turn dependency outputs and targeted review feedback still pass directly
between models, with producer/model/version references in `dependency_sources`.
Current-turn outputs are excluded from additional search to avoid duplication.
There are no extra agent discussion rounds or model calls for this retrieval.
Small budgets can omit optional evidence; operational retrieval failures are
recorded as unavailable and do not erase work. Project deletion still stops calls.

Each call's supplied excerpts, source metadata, retrieval time and context tokens
are redacted and saved in `knowledge_retrievals`. **Work details → Knowledge used**
shows that record, including historical messages. This records what was supplied,
not a claim that a model actually used a source. Included evidence contributes to
normal provider input usage. Disabling Project knowledge sends no additional
project retrieval; same-task history and required agent handoffs still apply.

Run `.venv/bin/python tests/benchmark_project_knowledge.py` for a temporary-data
benchmark of 1,500 synthetic documents and 40 exact-identifier queries, reporting
cold and warm latency, source selection and context size. It uses no live model,
listening port or personal data. Real task questions are still needed to measure
semantic recall and answer quality; these numbers are not a production SLA.

## MCP gateway

`task_providers`, `task_model_options`, `start_task_run`, `continue_task_run`, `list_task_runs`,
`get_task_run`, and `cancel_task_run` proxy to the same API on localhost port 8000.
Project scope comes from the MCP endpoint, never a tool-supplied store ID. Tool
arguments are validated before forwarding. Timeout errors warn that a submission
may have succeeded and direct clients to history; no submission is automatically
retried. The legacy external CLI spawner is retired. Manual plan tools remain a
separate client-managed workflow.
