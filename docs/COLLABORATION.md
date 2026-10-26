# Shared projects: proposed next step

This is a design, not an available hosted feature.

## What works now

- One local app holds documents, task conversations, results, and project search.
- The GUI and MCP use that same project knowledge.
- Knowledge updates are serialized across threads/processes and written atomically.
  Concurrent additions are retained; the last save still wins for edits to the same document.
- Each independent installation has its own data. There are no user accounts,
  invitations, per-user credentials, or remote project permissions.

## Smallest useful shared version

Run one service on one machine. Both people sign in and join the same project.
Each can work on a separate task while both retrieve the project's saved results.

1. **Project invitations:** owner, editor, and reader access. Check membership on
   every API/MCP read and write, including search and task history. A project URL
   or source ID is not permission to access it.
2. **Separate keys:** each person connects their own provider account. Keep secrets
   in protected credential storage, separate from shared project data. Attribute
   task usage and charges to the person who starts the work.
3. **Shared results:** save documents, answers, and source references under the project.
   Mark model drafts and review judgments clearly; sharing does not verify them.
4. **Safe edits:** give documents a version. Reject an outdated edit and show the newer
   version instead of silently overwriting it. Keep existing same-task message checks.
5. **Live updates:** notify other members when work or knowledge changes. Show who
   produced each result and when, with the source version used by each agent.
6. **A hosted access boundary:** add authenticated HTTPS access for the web app and
   MCP. Current provider/task/MCP routes deliberately reject non-local requests;
   changing the bind address alone cannot enable collaboration safely.

For an initial two-person pilot, one writer service with locked JSON storage can
remain the starting point. Do not let clients write the files directly, run multiple
task-runner owners, or sync live JSON files through Dropbox/Drive. Add a database
when the storage size or deployment needs it; file locks do not provide distributed storage.

## Checks before inviting a collaborator

- Two members can create separate tasks and both retrieve their saved results.
- A non-member cannot list, search, guess a source ID, or read a task in the project.
- Removing a member revokes both web and MCP access, including existing connections.
- Keys are never shared with another member or returned in project exports.
- Concurrent edits produce a clear conflict; duplicate messages do not start duplicate work.
- A failed model call preserves drafts and reports usage without blaming the other member.
- Restart and restore tests preserve membership, task history, and knowledge references.
