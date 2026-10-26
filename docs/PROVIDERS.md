# Local, Hosted, and Automatic Model Routing

Provider connections are saved once in **Settings → Providers**. Add more later
without changing the workflow code, provided they speak a supported protocol.

## Supported adapters

| Adapter | Typical use | Base URL example | Authentication |
| --- | --- | --- | --- |
| OpenAI | OpenAI API | Built in | API key |
| Anthropic | Anthropic API | Built in | API key |
| Ollama | Native local Ollama | `http://localhost:11434` | Optional for a keyless local server |
| OpenAI-compatible | LM Studio, vLLM, compatible hosted services | `http://localhost:1234/v1` | Optional; depends on the endpoint |

Compatible profiles use `/models` and `/chat/completions`, not the OpenAI Responses
API. Ollama uses `/api/tags` and `/api/chat`. Another protocol needs another adapter;
not every product advertised as an LLM supports these interfaces. Provider OAuth,
Copilot login/session reuse, and automated account registration are not supported.

## Local setup

1. Install and start Ollama, or start LM Studio's local server. Install/load an
   instruction-following model through that application first. This app never
   downloads models, pulls Ollama models, or launches an inference server.
2. Open **Providers → Add Ollama** or **Add compatible endpoint**.
3. Enter a connection name and the base URL. For native Ollama do not append `/api`;
   for an OpenAI-compatible server include its documented API prefix (often `/v1`).
4. Leave the API key empty when the endpoint intentionally needs no authentication.
5. Leave the model blank for automatic selection, or test to pick a preferred model. Adjust
   context/output budgets to fit the actual model and your hardware.
6. Save the connection; new endpoints and keys are checked automatically first.
   A local keyless connection stays available after restart
   without credential prompts; a session-only authenticated connection needs its
   key again after restart and will not silently switch to unauthenticated use.

HTTP is accepted only for literal loopback addresses or `localhost`. Remote/LAN
endpoints require HTTPS. URLs cannot contain credentials, query strings, fragments,
or link-local/metadata IP addresses. Existing profile endpoints are immutable:
create a new connection instead of redirecting an old stored key to a new server.

Models must follow structured JSON planning/review instructions. Very small models
may fail those instructions. Start with a simple goal, 1–2 subtasks, and one revision.
Model-name/parameter-count heuristics are not capability benchmarks. Different
local models may compete for memory when two worker calls run concurrently.

## Usage preferences and one-time permissions

In Providers settings, select the connections under **Usage preferences** and save:

- **Local only:** excludes non-loopback endpoints before discovery, and excludes
  reported/recognized cloud-backed models. Native Ollama generation also checks
  `/api/show` metadata to reject cloud-backed aliases. A custom local proxy can
  still forward remotely; configure only servers you trust. This is not a network
  sandbox around external inference software.
- **Prefer local:** favors local models but may offer remote worker models from
  approved connections. This mode is not a guarantee that no data leaves the machine.
- **Balanced:** favors discovered configured defaults and lighter worker choices.
- **Prefer stronger models:** uses model-family/size heuristics; it may cost more.

The default automatic allowlist is empty. Adding a connection never automatically
authorizes it for multi-provider runs. Changing preferences does not start calls.
Newly allowed providers are available to future runs, not added to a running run.
Removing permission, restricting to local-only, or changing a selected connection's
limits/model restrictions stops subsequent calls rather than rerouting silently.

Choose **Automatic routing** in the project Tasks tab. When the saved allowlist is
usable, it is the default on a fresh visit. Load models to inspect the pool or start
directly: discovery happens again when the run starts. The lead can suggest worker
provider/model pairs from a bounded offered list; unsupported or unapproved pairs
are rejected. A deterministic task policy selects coding specialists for implementation,
compact models for extraction/writing, and stronger models for reasoning. General
tasks keep the validated planner route or compact default. Each task displays its
actual provider and model and the selection reason. The lead plans,
reviews, and synthesizes; workers receive task-specific context independently.

Selecting an explicit provider instead keeps work on that provider and is separate
from automatic-routing permissions. Task-specific model selection works within that
provider too. An explicit lead/worker model override pins that role, including on
failure. Leave both model menus on automatic for recovery without manual intervention.

Automatically selected models can switch after recognized model-unavailable,
unsupported-chat, context-length, out-of-memory, rate-limit, or overload errors.
Each call tries at most three distinct models, counting toward the existing 37-call
run cap. Remote alternatives can incur charges and receive the same task context;
only submission-time approved providers/models are eligible, with authorization
rechecked before dispatch. Authentication, billing, unknown errors, refusals, and
network timeouts do not trigger model switching. No models are downloaded or installed.

Known incompatible chat IDs (including `gpt-3.5-turbo-instruct`, legacy completion
models, and embeddings) are excluded; local Qwen/Llama instruct models remain eligible.
Explicit `chat_capable: false` discovery metadata is honored. Unknown aliases remain
eligible because names alone cannot prove capability. Failed availability is remembered
in memory for five minutes for unsupported chat, or 30 seconds for transient failures,
scoped to connection configuration; context errors are not cached. Restart clears this
cache. Recovery is recorded in task details and the activity log, never applied to
historical failed runs automatically.

## Hosted free tiers

Add a compatible endpoint using its documented base URL and API key, if required.
The service may require API credits or impose limits even when some models are
free. We cannot infer price reliably from model names or guarantee a free tier.

Use **Approved model IDs** to restrict that connection to specific IDs, one per
line, such as the exact free-tier model IDs published by the service. Discovery,
automatic assignment, and explicit generation all enforce a nonempty restriction.
An empty list permits all compatible discovered models. Check the service's
pricing regularly; providers can change pricing without changing model IDs.

## Persistence and interfaces

Non-secret metadata and routing preferences remain in
`~/.self_mcp/provider_settings.json`, with locking and atomic replacement. API keys
stay in environment variables (built-ins), the OS credential store, or API-process
memory. Keys are never returned to the GUI or stored in workflow JSON.

Additional endpoints (loopback/Origin guards apply):

- `POST /api/providers/profiles` creates a named custom connection.
- `POST /api/providers/profiles/test` tests an unsaved connection without storing it.
- `PUT /api/providers/profiles/{id}` updates a connection without changing its endpoint.
- `POST /api/providers/{id}/test` tests saved access; `DELETE /api/providers/{id}` removes it.
- `GET/PUT /api/providers/routing` reads/writes `{mode, providers: [profile_ids]}`.

Profile input includes `name`, `kind` (`ollama` or `openai_compatible`), `base_url`,
`storage` (`session` or `keyring`), optional `api_key`, `model` (empty allowed),
`make_default`, `requires_key`, `context_tokens` (default 8192), `output_tokens`
(default 2048), and `allowed_models` (up to 50 unique IDs). Output must be smaller
than context. An input key implies authentication is required. Updating with an
omitted key keeps it; `clear_api_key: true` explicitly clears it. Omitted
`allowed_models` on update preserves the saved restriction; `[]` explicitly clears it.

`settings.catalog()` is metadata-only; routing filters eligibility before resolving
credentials. Connection snapshots never persist keys. Before sending inference,
authorization is checked again after token preparation and any native metadata
inspection. Redirects and environment proxies are disabled for model requests.

Protocol extension points: `src/provider_transport.py` (discovery/inference),
`src/providers.py` (profiles/access), and `src/task_routing.py` (selection policy).
No execution tools are added: results remain text/code drafts; delegated runs include model review.

## Model capabilities and direct inference

Choose **Ask a model** in the workspace to use a model independently. Select a
connection, load models, and choose an ID, or leave selection automatic. This mode
skips team planning and review; it saves the answer, actual model, and usage using
the same run history and cancellation controls. It does not execute generated code.
Subsequent messages continue the selected task and include earlier requests and
results. Each follow-up rechecks its chosen connection and permissions. Use **New
task** to start a separate conversation. A saved task can switch mode or provider
for its next message; its conversation remains attached, subject to context limits.

`src/model_capabilities.json` is a repository-owned registry for model-family
patterns, supported adapter kinds, default timeouts, output budgets, and reasoning
settings. `src/inference_policy.py` resolves those rules separately from task routing.
Add profiles for newer models on supported protocols without changing the workflow.
New protocols still require an adapter. Restart the API after changing the JSON
registry. Unknown model aliases use adapter defaults; names alone do not prove
access or parameter compatibility.

GPT-6 Astra uses `gpt-6-astra`, a 300-second call timeout, and `low` reasoning by
default. Built-in OpenAI uses Responses with a 16,384-token output budget. Compatible
endpoints use Chat Completions, `max_completion_tokens`, and `reasoning_effort`, while
honoring the connection's configured output budget. Astra support follows the
[official model guidance](https://developers.openai.com/api/docs/guides/latest-model).
No unsupported sampling parameters are added. Other model families retain their
existing defaults unless explicitly configured.

The workspace's Settings can override timeout, output budget, and supported reasoning
effort per run. Defaults apply automatically, so most requests need no tuning. A
connection with a 2,048-token output limit still needs that limit raised in Providers
before a larger run budget can be used. Larger reasoning/output budgets may incur
more cost. The 15-minute total run deadline and response-size limits still apply.

Failed runs show a controlled next action. Timeouts name the time limit; output
exhaustion names the budget, including reasoning. HTTP failures show status and API
operation, with no raw provider messages or keys. Incomplete output still contributes
reported token usage. Selecting a model does not prove your account can run it.

## When a Lead Succeeds but a Worker Fails

Discovery and planning do not prove that the selected worker model supports chat,
is loaded/available, or has sufficient context, memory, or hosted credits. Check
the actual worker provider/model in the task card; it can differ from the lead.

Custom adapter failures now retain the actual HTTP status and fixed operation path
(for example `HTTP 404 (POST /api/chat)`) with controlled error categories. Raw
provider error messages, headers, and URLs are not exposed. The failing task and
its provider/model are recorded separately from siblings stopped due to that
failure. Automatically selected models first try bounded approved alternatives for
the recognized recoverable categories above; the final error means recovery was
unavailable or exhausted, or the error requires user action.

Older generic errors cannot be decoded retrospectively. For an existing local
failure, inspect the inference server's log at the failure timestamp before
starting another run. For hosted services, check the selected model's access and
credits in the provider dashboard.

## Verification

Backend: `.venv/bin/python -m unittest discover -s tests`.

Browser: `node tests/provider_ui.cjs`, `node tests/profiles_ui.cjs`, and
`node tests/tasks_ui.cjs`. Use `PLAYWRIGHT_MODULE`/`CHROME_PATH` as described in the
README. Tests use temporary state, fake credentials, mocked local/hosted protocols,
and isolated browser profiles—not your live servers or API credits.
