# self_mcp
MCP architecture to make sure project is the priority.

## Description

Aim us to provide the effortless environment to focus on the project rather than making sure the AI gets right information about the project.

The project allows to keep the notes about the project while notifying the tokens limit and making sure you are not hitting the dumb zone for the LLMs.

## Features

Create a standaloen MCP for your project and let the LLM take care of it when you discuss it with the agents.
Make sure you will have all the things at the same place for example your own doocuments and links for example documentations, github implementation and papers.

## How to use it?

Create an environment with Python 3.12,
then:

```
pip install -r requirements.txt
python start.py
```

## Provider Setup

Open **Settings → Providers** in the sidebar, or visit `/providers`.
First-time users without configured credentials see a setup dialog. Choose OpenAI
or Anthropic, enter a key, test the connection, choose a default model identifier,
and save. New keys must pass the GUI connection test before saving. Choose **Set up
later** to dismiss onboarding and continue using project features; Providers
settings remain available at any time. Closing first-time onboarding also records
that choice. Existing environment or app-managed credentials skip onboarding.

Environment credentials are detected only in the running API process, not in
another terminal's environment or an external AI client's login session. Startup
does not automatically test keys or call a model. Provider status distinguishes
credential presence from a successful connection test. OAuth/provider sign-in,
automatic task-to-model selection, and agent execution are not part of this feature.

The dialog supports keyboard dismissal, focus isolation, and scrolling on short
screens. Keys are cleared when it closes and are never stored in browser storage.
Markdown preview is sanitized with pinned DOMPurify; if the CDN is unavailable,
preview falls back to plain text rather than rendering unsanitized HTML.

Provider onboarding requires Python 3.12 and is available under `/api/providers`.
It does not change existing MCP or orchestration behavior or project tabs.
The normal requirements and project dependencies include `keyring>=25,<26` for
OS credential storage as well as HTTP model listing support. Installing keyring
does not guarantee that the operating system has an available, unlocked backend.
Missing, locked, unsupported, or unavailable keyrings fail gracefully; there is no
plaintext credential fallback. Alternate file keyrings and chained backends are
not used. `keyring_available` is a best-effort backend/read availability check;
an OS prompt or write failure can still occur when saving.

- `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` take precedence over app credentials.
- Saving an input key while the corresponding environment variable is set returns
  HTTP 409. Omit `api_key` to change settings without changing credentials.
- Session storage lasts for the **API process lifetime**, is shared by all local
  clients, and is not browser-session isolation. Reloading/restarting loses it.
- Saving an input session key removes the older app-managed keyring credential
  when keyring is accessible. If no backend is available, session saves remain
  usable but return an explicit warning: this is **not a confirmed session-only
  switch**. An older persisted key cannot be inspected or removed and may become
  active again after restart when keyring access returns. Restore keyring access
  and save the session key again to remove it. A backend that exists but fails to
  read or delete its credential causes HTTP 503 instead of reporting success.
- Keyring storage uses service `self_mcp.providers`, with the provider ID as the
  account. Settings-only saves do not move or replace existing credentials.
- Non-secret model/default/onboarding preferences live separately in
  `~/.self_mcp/provider_settings.json`, using locking and atomic replacement.
- All endpoints require a loopback peer and Host. Browser Origin must exactly
  equal the request's scheme and Host (including port). CLI may omit Origin.
  Forwarding headers are rejected. This API is not designed for reverse proxies.
- Every write, **including bodyless DELETE**, requires
  `Content-Type: application/json`. Validation and upstream errors never echo keys.

API shapes (all successful responses use HTTP 200):

```text
GET /api/providers
-> {providers: [{id: "openai"|"anthropic", name: string, configured: boolean,
                source: "environment"|"keyring"|"session"|null,
                default_model: string|null}],
    keyring_available: boolean, default_provider: string|null,
    onboarding_dismissed: boolean}

POST /api/providers/{provider}/test
   {api_key?: string, model?: string}
-> {models: [{id: string, name: string}], message: string}

PUT /api/providers/{provider}
   {api_key?: string, storage: "session"|"keyring", model: string,
    make_default?: boolean}  // defaults to true
-> {message: string}

DELETE /api/providers/{provider}
-> {message: string}

POST /api/providers/onboarding
   {dismissed: boolean}
-> {onboarding_dismissed: boolean}
```

Testing uses the supplied key, if present, otherwise the resolved credential; it
never saves it. Testing authenticates to the provider's fixed `/v1/models`
endpoint and **does not verify inference**. The optional `model` is validated but
does not select or invoke a model. Anthropic listing stops after five pages of
100 models; responses indicate truncation. Listing has a 30-second overall deadline,
a 5-second connect timeout, and 10-second HTTP operation timeouts. Requests
do not follow redirects or use environment proxies.

Saving requires a credential and a nonempty model ID (at most 200 ASCII letters,
digits, dots, underscores, colons, slashes, or hyphens; first character alphanumeric).
Input keys must be 1-4096 printable ASCII characters without spaces. Bodies are
limited to 16 KiB. Saving does not call the provider. Removing clears app-managed
credentials, model, and its default-provider selection, but cannot unset environment
variables. If keyring is unavailable, the response explicitly warns that any
existing keyring credential could not be removed. An environment-backed provider
therefore remains configured after deletion. A default/model preference can also
remain after a restart has discarded its session credential; use `configured`
to determine current credential availability.

Preference writes are atomic, and session changes are committed only after those
writes succeed. Keyring changes are rolled back on handled write/update failures;
the rollback credential exists only in memory, never in JSON or a disk journal.
If rollback also fails, HTTP 503 explicitly reports uncertain keyring state and
asks you to inspect the OS credential store and retry. JSON preferences and
session credentials remain unchanged in that case. These operations are not a
crash-atomic transaction across the OS keyring and filesystem: a process crash
between their commits can still leave them inconsistent. Other processes directly
editing the OS keyring are outside the preferences lock.

Errors use `{detail: string}` with sanitized messages: 401 authentication/access,
403 local/origin guard, 404 unknown provider, 409 environment override, 413 body
limit, 415 non-JSON write, 422 validation/missing credential, 429 rate limit,
502 upstream/network, 503 keyring/preferences, or 504 upstream timeout.

Run isolated tests with `.venv/bin/python -m unittest discover -s tests -v`.
Tests use temporary preferences, fake environment values, mocked keyrings, and
mocked upstream HTTP; no server or real credential is needed.

For browser checks, install Playwright separately, then run
`node tests/provider_ui.cjs`. If it is not installed in the project, set
`PLAYWRIGHT_MODULE` to the package path. Set `CHROME_PATH` to an installed Chrome
executable, or install Playwright's Chromium. These checks use an ephemeral local
static server and mock all APIs; they do not start `start.py`, use your keys, or
touch project data. They cover onboarding, skip/revisit, failed/successful tests,
save/remove, environment detection, mobile dialog scrolling, and existing tabs.

## Multiagent Tasks

Open a project and select **Tasks**. This is separate from Documents, Links, Issues,
and provider settings; it does not restore the old Plans tab.

1. Enter a goal, relevant context, and acceptance criteria. Project documents are
   not automatically included, so include the necessary information in your goal.
2. Select a configured OpenAI or Anthropic provider. Automatic selection discovers
   accessible generation models when the run starts. Use **Load accessible models**
   to inspect recommendations and choose lead/worker overrides if desired.
3. Set the maximum subtask count and revision rounds, acknowledge provider charges,
   and click **Start task**. This immediately starts real model calls.
4. Watch the lead's breakdown, dependencies, worker outputs, review feedback,
   revisions, usage, and execution timeline. A rejected draft reruns selected tasks
   and their dependent tasks, up to the configured limit.
5. Copy the final reviewed draft or cancel an active run. Leaving the tab does not
   cancel work; restarting/reloading the API interrupts it without automatic replay.

Model recommendations use transparent family/tier/version heuristics, not a claim
that model listing proves task suitability, price, or inference access. Copilot
authentication is not supported yet. No silent provider/model fallback is used.
All worker agents in a run use its selected smaller-worker model with separate
task context; the lead performs planning, review, and final synthesis.

This version generates **text and code drafts only**. It does not execute code,
run scientific simulations, browse, or change project files. In particular, a VQE
answer must not be treated as an experimentally verified result. The final output
always includes a server-controlled notice that code/simulations were not executed.

Run data remains in JSON at `~/.self_mcp/task_runs.json`, separate from project
content and provider credentials. Updates are locked and atomically replaced.
Use one API worker; a process lease prevents conflicting workflow owners. There
are at most two simultaneous upstream calls, 20 admitted runs, a 15-minute deadline
per run, and finite call/token/revision limits. These are not a dollar-cost cap.

See [docs/TASKS.md](docs/TASKS.md) for the API and execution contract. Run
`node tests/tasks_ui.cjs` with the same Playwright setup described above for the
mocked browser regression suite; backend tests remain under unittest discovery.

## Preview

![Dark Preview](./preview_dark.png)
![Light Preview](./preview_light.png)
