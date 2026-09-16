# Dify Agent runtime

Dify Agent hosts Agenton-composed Pydantic AI runs behind a FastAPI API. Its
source code stays under `src/dify_agent`, while framework-neutral Agenton code
stays under `src/agenton` and `src/agenton_collections`.

See the [operations guide](guide/index.md) for local server behavior.

Workbench Office, browser, and environment usage instructions are maintained in
the Agent's published config files and loaded through the config layer. The
workbench environment layer enforces shared dependency updates and conversation
file placement. Workbench Shell prompts omit native sandbox installation and
Home-layout assumptions; administrators own environment usage guidance in
published instructions or config files. CLI declarations use already-provisioned
commands; workbench runs skip their installation scripts and request missing
dependencies through the shared-environment owner. Environment declarations keep
normal variables and account-host secret references, excluding the publisher's
inline secret values.

Workbench knowledge selection requires an access attempt before a final answer,
while preparation tools and deferred human/environment requests remain available.
Answer text is withheld until the required knowledge attempts have been made,
so output rejected by the knowledge validator never appears as a streamed draft.
Access failures are explicit observations; they neither count as successful
searches nor force repeated calls. Retrieval failures within a dataset propagate
to this boundary instead of silently becoming empty or partial search results.

Workbench search observations are paginated JSON. Search failures return JSON
with `status=error`, the attempted `search_id`, and a public `message`, so the API
can order the retrieval record with its tool return. `knowledge_base_read_results`
continues the same result using `search_id` and `next_offset`; the current run
retains its latest five results across suspension. `knowledge_base_list_documents`
and `knowledge_base_read_document` use the authenticated inner document API to
page through selected datasets. Each page rechecks the active run's owner,
selection, current retrieval permission and full-content read permission. Document
filters come from the frozen run configuration; query-dependent automatic filters
require using search instead of full enumeration. `complete`, `unavailable_count` and
`scope` describe indexed-content coverage; Top-K search and external providers
without enumeration cannot establish complete document coverage. Native Agent
tool availability, preview limits and error propagation remain unchanged.

Workbench executions can include `dify.workbench_followups`, which depends on
`dify.execution_context`. This layer polls the authenticated inner API for
supplements to the current workbench run and persists delivered message IDs in
its session snapshot. The runtime injects each message at the next model boundary;
an executing tool finishes normally. A supplement arriving during the final model
response keeps the same native run open via Pydantic AI's pending-message drain.
The final seal and user steering are serialized under the owning chat lock, so
messages arriving after the seal remain in the conversation's three-message FIFO.
Steering retains the active run's model and resources; separately queued messages
carry their own frozen selections. Inner API credentials are server-injected.
Steering requests include the task identity shown when the user clicks; a changed
target leaves the message queued. Transient inner API transport errors, timeouts,
429 and server failures retry at the same model/final boundary with cancellable
backoff. The runtime cannot finish before it confirms the final seal, and retrying
this control request does not repeat a business tool.
Regeneration and edited-message regeneration create new follow-up-capable runs;
this capability does not permit regeneration to bypass the active-task guard.
Recovery selects each conversation's actual FIFO head before applying its batch
limit. Only that head's predecessor can hold the queue paused; a cancelled run on
another historical branch does not block recovery after a lost completion notice.

Workbench Pause stops the native execution and holds the conversation's waiting
messages. Its terminal fence returns captured model history and delivered steering
IDs before the worker releases the execution lease. A blank Continue submits the
internal query `继续` with `continue_run_id`; its response is grouped with the original
turn in the UI, without an extra user bubble. New text instead creates a visible
turn using the paused task's context. Either continuation precedes the existing
three waiting messages; those resume in FIFO order after it ends. Continue keeps
the original model and resources, while new text uses the current selection.
Knowledge selection persists across conversations and does not count as unsent
message content. Attachment-only messages remain valid with selected knowledge;
the Agent can read the attachment before forming a knowledge search query.
Blank Continue uses the original owned revision without saving or compiling the
current draft, including when its save is pending or its version has conflicted.
The caller and published Agent are still authorized, and execution retains its
knowledge-access checks. New draft choices remain available after Continue or a
lost-response retry. Skill and plugin mentions clear when a new message sends.

Workbench executions additionally emit `context_status` events. Their `data.phase`
is `usage`, `compacting`, `compacted`, or `failed`. Token counts describe the current
request context, never cumulative billed usage. `estimated` distinguishes native
pre-request estimates from provider response usage; an unknown model window stays
null. Compaction phases share `compaction_id` and include `before_tokens`.
The existing tiered compactor remains responsible for rewriting history. These
events are non-terminal; consumers that do not display context can ignore them.

Workbench automatic and manual compaction share an incremental summarizer. Old
tool results reach the summary model in full, including facts after the first
500 characters; they are not cleared before summarization. Oversized source text
is processed in bounded segments with the previous summary anchored in each
request. The summary preserves requirements, corrections, authorization, exact
artifact references, verified outcomes, uncertain side effects and next actions.
Native tool-pair boundaries and recent history are retained. Reused provider call
IDs are normalized on a copy. If one recent tool pair exceeds the input target,
its complete text is summarized too. Model summaries remain lossy and may require
re-reading source files or the original transcript, especially for binary media.
Non-Workbench compaction retains its clamp, clear and summarize tiers.

Keeping a fixed number of recent messages alone cannot bound a short history
containing large tool arguments. The input target uses 80 percent of the
reported model window and reserves the configured output allowance. When a
Workbench model reports no valid window, an explicit 8,000-token history budget
still enables compaction; it is a policy threshold, not a claimed model capacity.
Context events keep `window_tokens=null` and report estimated current usage.
Non-Workbench callers retain the existing unknown-window behavior.
Post-compaction counts subtract reclaimed text from the pre-compaction estimate;
they do not reuse a retained response's old provider count as the new size. A new
provider response replaces the estimate. No reclaimed text is reported as a
failed compaction attempt rather than a completed reduction. Manual compaction
saves a durable checkpoint before publishing success; errors retain the previous
history. Incremental summaries add model requests and consume model usage.

### Workbench planning and goals

Plan mode guides information gathering, analysis, requirements clarification,
discussion of material choices, a complete reviewable plan, and revisions based
on feedback. `exit_plan_mode` pauses for an explicit review action; keep-planning
feedback leaves the mode active and requires another complete plan. Approval
exits planning and resumes execution. Complete candidates have monotonically
increasing versions; approval is tied to the exact content/version displayed in
the pending card. The approved plan persists independently of history summaries.
Clarification answers, skipped questions and stale review cards cannot approve a
plan. Feedback requires a revised candidate before execution can be approved.

While planning, the runtime exposes only trusted built-in investigation and
collaboration tools. Validation covers deferred/external tools as well as normal
functions, with API admission and environment-dispatch checks as a second boundary.
Text is held until the response passes plan and file checks; rejected final text
is never published as an answer. Neither text nor structured output can replace
a completed plan review. `plan_inspect` runs
bounded shell investigations in a disposable container: owner volumes are mounted
read-only, the image is read-only, network is disabled, no runtime credentials are
injected, and writable scratch is confined to `/tmp`. Scratch survives subsequent
inspections within the same conversation and can hold parsing scripts/previews;
up to four temporary PNG/JPEG previews can be returned to the model. Normal shell,
file-write, environment-update and external plugin tools become available after
approval. Sandbox-manager and Agent/API must be released together for this path.
The manager issues a durable binding-generation ticket before the API rechecks
the active execution. Stop rotates this ticket, removes preparing/running
inspection containers, and serializes cleanup with startup and request teardown.
Requests delayed beyond the stop cannot launch; manager restart preserves the fence.

Goals are persisted control state independent of an assistant's final response.
While active, the server schedules another round after the previous run and
queued user messages finish. Client disconnection or a worker restart does not
complete the goal. Default goals have no fixed round cap. The former internal
256-round default is lifted when loading existing goals; paused and blocked
goals still need explicit resumption. Finite non-default limits remain readable.
Human edits, pause/stop, plan review, recovery and completed-goal fencing retain
their existing behavior. Completion requires the current goal revision and a
finished execution list when one is used; the model audits every original requirement
against actual verification and delivery. The semantic truth of completion still
depends on model judgment and the evidence available to it.

`get_goal` exposes the goal's `goal_id` and `revision` directly, avoiding ambiguity
with the collaboration state's separate revision. A rejected update returns the
fresh goal for reevaluation; it never silently substitutes a newer revision.
Execution lists are optional for complex work or an explicit user request. They
are unavailable during planning and never gate ordinary business tools by call
count or by requiring an active item. Updates describe actual milestones,
changed next actions or blockers; identical list writes produce no new progress
event. Simple goals can complete without creating a list. Goal input messages
retain their original text and attachments; automatic subsequent rounds remain
internal continuations. Legacy placeholder queries are recovered from their own
immutable command record when reading history, never from an edited current goal.
First-input recovery uses a non-recursive, idempotent enqueue path and the current
authorized chat configuration until a run is committed. Once committed, its
configuration remains frozen. Removing the first queued goal input pauses that
goal; steering it binds the goal to the receiving run and counts that as its first
round. A removed input is never replayed as a missing enqueue.

Workbench runs with a conversation shell also apply the SDK's `ToolOutputLimits`.
Oversized tool results are stored under that conversation's
`.cache/workbench-tool-results`, and the model receives a handle and bounded
preview. `read_tool_result` reads selected lines, literal matches, or character
slices of a long line without replaying the original operation. Commit verification
hashes fixed-size chunks; line counts, literal filtering and character slices also
stream chunks, including within a single oversized line. Stored output
survives a new native run while the conversation sandbox remains available and
does not appear as a generated deliverable. Failed storage returns an explicitly
truncated result without inventing a handle. Reduction preserves business-error
metadata so large failures still count toward the five-failure budget.

The LLM layer accepts an optional `credential_ref` with `type` (`provider` or
`model`), `id`, and optional `provider`. The API resolves it for the caller's
tenant and selected provider/model on every invocation, including runtime
credential policy checks. Saved configuration references remain executable by
published apps even when the invoking user cannot see them in credential lists.
An explicit reference pins the call to
that credential and uses the custom-provider billing path; load balancing cannot
replace it. Invalid references fail explicitly. Context-window and vision
capabilities use the same reference. Secrets remain inside the API runtime.
New or changed model references are authorized against the editing account before
drafts, snapshots, or copied Agents persist them. A reference preserved from the
same Agent's stored model can remain unchanged; another Agent's payload cannot
supply that trust. Published invocations continue using the saved reference.

Workbench mentions are loaded from the current run's frozen payload. Mentioned
Skills are eagerly read by the config layer before the model runs. The optional
`dify.workbench_mentions` layer stores `workbench_run_id` and `tool_groups`
(`name`, `tool_names`). At least one appropriate tool in each mentioned group
must return an observation before a final answer. Argument-validation retries
do not count; explicit tool error observations count as attempts and must be
reported accurately. Preparation and deferred human/environment requests remain
available. Rejected answer text is withheld from streaming, and output validation
has two retries when mentions are present. Completion state survives suspension
of that run; a new workbench turn starts with fresh mention requirements.

Workbench activity reporting is opt-in through `dify.workbench_activity`. The
tool name `report_activity` is reserved in composer saves and prepared plugin/core
tool declarations, including model-facing name overrides and expanded providers.
The composition supplies the trusted logical `workbench_run_id`. Its sequential
`report_activity` tool lets the same task model describe an action and purpose,
update the stage, and close an activity after results return. The runtime assigns
activity IDs and revisions, binds each business call at execution start, and
restores the same identity when human input or environment installation resumes
in a different native run. Parallel business calls remain parallel. Tool retries
become error records only when an execution binding exists; argument-validation
failures do not start tool work, while actual execution failures remain visible.
Plugin and core-tool error observations carry application-only SDK failure metadata;
their original model-facing text is preserved and the activity records an error.
Reports do not appear as business tool rows; malformed or repeated reports become
no-ops without using the task's retry budget. Four reports without business work hide
the report tool until work resumes. Reporting still uses the normal model token
and request budget; no separate summarization model is invoked.

`workbench_activity` public events contain a discriminated `data.kind`: `activity`
for public titles, `tool` for call state, and `text`/`reasoning` for visible model
output. Tool `call_id` includes its originating native run and remains unchanged
across a deferred continuation. Shell `done=false` means the background job is
still pending even when output has arrived. Activity close checks pending calls
and jobs; it is not independent proof that a user's business goal was achieved.

The API freezes `activity_protocol=1` in new workbench runs only when
`WORKBENCH_ACTIVITY_ENABLED=true`. Its `workbench_run_events` journal is the shared
authority for history and live SSE, with an increasing sequence per logical run;
Redis is a wake-up channel. Install the migration and upgrade all API/Agent
readers before enabling the producer. To roll back reporting, disable the flag
while retaining the new readers and journal. Existing protocol-1 continuations
still emit tool and text records with both the reporting tool and its prompt disabled. Legacy runs
retain their previous reader and composition contract. Do not drop the journal
when merely disabling reporting.

### Workbench file delivery

Workbench compositions include `dify.workbench_files`, which binds the trusted execution context and exposes `workbench_files(path=".")`. Results confirm current-chat files and include the exact `preview_url` and `download_url` returned by the file-space UI. URLs remain stable for the owned path, use signed capabilities, and are invalid after the chat/workspace or file is removed. HTML previews run with an opaque sandbox origin. The model must query files before delivering those URLs.

The config layer selects file-delivery guidance from the trusted `workbench_run_id` on every invocation, including resumed sessions. Workbench replies use the file-space URLs; ordinary Agent replies retain the upload CLI's `public_download_url` guidance. The upload, public-url, and download CLI commands remain available for structured ToolFile references and incoming files.

Pending generated paths persist in the session snapshot across deferred continuations of the same workbench run. URL verification resets on resume, and a new logical run clears the pending paths. Entries with `downloadable=false` remain visible but receive no URLs; the model must split unsupported artifacts or explain that delivery is blocked. File downloads are limited to 20 MiB, and directory archives to 50 MiB of supported file contents.

Workbench file-change inventories cover the owned `/workspace`, including writes
outside the current conversation directory. Personal memory, skills and resource
staging paths are excluded from generated-file delivery. Inventories remain bounded
by file count, output size and execution time. Acquiring a conversation binding
prepares its current directory from `/workspace` and verifies the persistent home;
it does not require the obsolete account-named working directory to exist.

Personal memory is refreshed before each model request. The control layer exposes
`read_memory` and `update_memory(content, version)` for proactive consolidation of
stable user preferences, explicit corrections, verified reusable lessons and durable
project context. Temporary task progress and unverified inferences are excluded;
explicit forget/do-not-retain requests take precedence. Updates use the exact version
read from the owner's workspace, share the editor's account lock and file CAS, and
require the current app/run/execution identity. On conflict the model receives the
latest memory and merges again. Transport retries retain the original payload; an
already-applied identical value is a no-op. External file IO runs outside database
transactions. Unreadable memory cannot be silently replaced by the Agent.

For complex execution, `todo_write` is optional: update an existing list when the
actual next action, progress or blocker changes, and mark steps completed only after
verification. The current execution list is injected outside plan mode. Simple work
does not need a list; unchanged lists do not create progress events or revisions.

The resource catalog lists published global skills first and enabled, valid personal
skills second. Personal skill IDs use `personal:<name>`; they belong to explicit
resource mentions, not the global archive selection. The API resolves them against
the authenticated owner's workspace at submission and dispatch. The model reads
the chosen content through `read_skill(scope="personal", name=...)`.

The command endpoint advertises `command_resources_protocol=1` in the catalog and
accepts `resource_mentions` with `/goal`, `/plan` and `/compact`. Goal state preserves
these mentions for subsequent automatic rounds. `/plan <direction>` starts planning
that direction. `/compact <instruction>` durably compacts history and then executes
the instruction in the same run; bare `/compact` only compacts history. Completion
notification retries cannot turn a committed summary into an "original retained"
failure. Human-input submissions carry the current pending `request_id`; replies to
a superseded request are rejected even if the logical run ID has not changed.

Sandbox manager helpers use `/usr/local/bin/python -I -S`, while normal user commands
retain their personal-environment-first PATH. The office image makes `/usr/local`
root-owned and removes group/other write permissions. A running container on a
previous image is rejected before management helpers execute. Deployments drain
active runs, stop old containers, and recreate them with the configured hardened
image while preserving home, workspace and personal-environment volumes.

Final delivery requires a download URL for at least one changed path or a directory archive containing it. Each query refreshes verification for its requested path; file changes invalidate previous query results and failures. Unrelated files do not establish delivery or explain a failure to deliver the current artifacts.

Workbench shell sessions also expose `file_create(path, content)` and `file_edit(path, old_text, new_text)` for bounded UTF-8 file operations inside the current workspace. Creation refuses overwrite; editing requires exactly one match and preserves unchanged content. Shell argument envelopes are only unwrapped when complete JSON parses, independently of activity reporting; repeated malformed calls produce bounded, explicit observations.

The runtime inventories regular files inside the conversation directory before execution,
after tool results, and before publishing a model response. New and changed paths emit
ordered `file_create` / `file_edit` tool records with `output.source=workspace_change`,
including binary files produced by shell scripts or other tools. Explicit file tools
retain their original row without a duplicate observation. Inventories do not follow
symlinks or include internal configuration, dependency/cache directories, browser profiles,
Office work profiles, logs, or edit temporary files. These incidental
files neither emit inventory rows nor count as pending file deliverables. Documents,
images, data files, generation scripts and user configuration files (including
`.env`, `.gitignore`, `.npmrc`, `.github/workflows`, and dependency lockfiles) remain
observable. Explicit file-tool calls
retain their original records, including failures. The workbench frontend applies
the same exclusions to historical inventory rows without modifying stored events.
Inventories are limited to
10,000 regular files and a 15-second scan. A scan failure is reported rather than
claiming that file changes were checked. Changes are identified by filesystem size,
modification/change timestamps and inode; file contents are not copied into the journal.

Workbench answer text is held until the complete model response passes file-delivery
validation. Markdown image targets must match verified preview URLs, and file/download
links must match URLs obtained by the current file-space tool. Reference-style links
are checked too, while fenced examples remain examples. Invalid responses request a
bounded model correction before either native text events or workbench narrative
events are published. The verified set is cleared when files change or a native run
resumes; a failed lookup may be reported as a blocked delivery without inventing a link.
Tool and reasoning events remain live while answer text is being validated.

An older deferred workbench snapshot may gain only the new, empty file-reader layer
when every other ordered layer name still matches. Existing suspended state and
deferred calls are preserved; other composition changes remain incompatible.

The frontend keeps context compaction in the current execution group. A normal
assistant text reply still separates successive groups.

### Workbench failure recovery and human input

Workbench executions allow five consecutive failed business tool calls. Argument
validation errors return field-specific observations to the model so it can correct
the next call. A successful business call resets the count, including success from
a different tool; activity reports do not consume or reset it. Execution failures
and unknown tool calls use the same budget. The fifth failure is captured in history
before the attempt ends. These rules are scoped to Workbench execution contexts.
Once the fifth failure is observed, not-yet-started calls in the same batch are
recorded explicitly as unexecuted; already-running parallel calls finish and
retain their outcomes. Skipped calls do not reset or consume the failure count.

New workbench runs persist an automatic continuation budget in their payload.
A failed or interrupted attempt may create one child turn whose query is `继续`,
up to three consecutive automatic turns. Each child retains the frozen configuration,
original goal, attachments and history branch. The old native execution must be
confirmed stopped before its successor is queued. Pausing cancels pending recovery
and any automatic successor; a manual new turn or submitted human answer starts a
fresh budget. Historical failures without recovery metadata are not restarted.
Timers and successor IDs are persisted so worker restarts do not duplicate turns.
The automatic `继续` query is issued by the backend and remains internal to the
execution chain. The frontend groups server-linked successors under the original
question and assistant reply, preserves each error inline, and appends subsequent
output without a new user bubble or outline entry. The reply stays active while
recovery is pending; final response actions appear only once the chain has stopped.
An explicitly submitted user message, including a manual `继续`, remains visible.

With `followup_protocol=1`, a chat can hold up to three waiting messages. Automatic
recovery continues the current task before starting these messages and moves the
queue head onto the successor's history branch. Waiting or removed messages do
not cancel recovery. A resumed ancestor may have been created after its waiting
descendants; recovery follows their branch relationship instead of treating that
ancestor's newer creation time as a replacement task.
Pausing holds the queue until an explicit continuation;
waiting messages use the queue-removal endpoint and cannot be paused as independent
executions. Steering requires the same frozen selection and effective configuration
as the active task; a message with a different model or resources remains queued.
Blank continuation retains the original configuration and question, while typed
continuation remains a visible user message. A supplement accepted during the
recovery delay is included once in the successor's context. The native follow-up
hook runs before history checkpointing, and a stopped executor's fence response
preserves both history and delivered supplement IDs. Checkpoints save those IDs
atomically with history, including after compaction, so a killed process does not
cause an already consumed supplement to be inserted again into its successor.

An `ask_human` request receives a unique `human_input.request_id`, a server deadline
60 seconds after the question enters the waiting state, and `server_now` for display
clock correction. The frontend shows the remaining seconds inside the `跳过` button
beside `发送`. Any interaction with the form cancels its countdown and persists that
decision through `input-interaction`. `input-timeout` resumes only an untouched,
expired request; `input-skip` explicitly skips the matching question. Both return
the current run state and resume the same logical run without submitting defaults
or partially entered answers. A missing answer does not grant new permissions or
establish facts. All three endpoints enforce account, tenant and question identity.

The API stops consuming immediately after a terminal error frame, even if the
upstream connection remains open or continues sending keepalives. Workbench
model requests have a 180-second idle deadline that resets on actual text,
reasoning or tool-argument output. Tool execution and deferred human input do
not consume that idle deadline. A silent model ends the attempt through the
existing checkpoint, remote fencing and bounded automatic recovery path;
manual cancellation remains cancellation and is never converted into recovery.

File delivery proof is invalidated only when that file changes or is removed.
A directory archive also loses proof when one of its members changes. Writes in
another conversation or unrelated build/preview files retain verified links,
so simultaneous work in one personal workspace does not force repeated delivery
retries for unchanged artifacts.

The API treats a stream without a terminal frame as a failure and tolerates brief
Redis heartbeat outages until the last confirmed execution lease expires. Recovery
reconciles lost leases and rechecks renewed ones before interrupting a run. Continued
execution remains bounded by the runtime's existing model, sandbox and step limits;
persistent external failures can still exhaust the three-turn recovery budget.
Remote cleanup calls allow 120 seconds for a response, covering the sandbox
manager's 90-second cleanup window while connection setup remains limited to
10 seconds. Reconciliation reads Redis leases before opening its write transaction
and only updates an unchanged, unlocked execution row. A delayed lease reply or
a concurrent pause or replacement execution cannot overwrite the newer state.

Remote cleanup acknowledgement is persisted against the current execution ticket
with its fenced history. Manual continuations and promoted queue entries wait for
that acknowledgement even if their predecessor's Redis reservation disappears.
Cancelled, failed and interrupted tickets without acknowledgement are included in
a rotating reconciliation scan. Older terminal records with a remote ticket and
no proof are fenced before further work; a missing ticket before dispatch does not
imply a remote execution. Replies for another ticket or an unknown status cannot
confirm cleanup. A successful terminal stream records proof in its SQL status
transaction before capacity is released.

Workbench checkpoints persist history before each model request, before tool
validation can dispatch an effect, and after successful completion. Request
instructions are excluded, as in the normal history snapshot. Repeated identical
history is not rewritten. The native `POST /runs/{run_id}/fence` response uses
`FenceRunResponse` and returns history only after the executor is confirmed
stopped; it prefers the terminal history snapshot and otherwise returns the last
checkpoint. The API saves that history against the matching execution ticket
before creating a continuation. Interrupted tool calls retain their uncertain
outcome so the next attempt can inspect files or external state before retrying.
Checkpoints share the configured Redis run retention; Redis data loss or expiry
can still lose progress which has not yet reached the application database.

Client SSE reconnection budgets count consecutive reconnects without a new event
cursor. Receiving new progress resets the budget, allowing a long run to survive
multiple separated disconnects while still bounding an unproductive reconnect
loop. Redis checkpoint writes and cancellation observation tolerate connection
and timeout errors for up to 60 seconds; persistent errors still terminate through
the normal recovery path. Event appends are not blindly replayed after an
ambiguous write.

Personal sandbox keep-alive retries after a failed touch instead of permanently
ending its periodic loop. Stop notifications are best effort and cannot prevent
remote fencing or later recovery dispatch. Bounded database timer scans rotate
past previously attempted rows, so unavailable executors or revoked accounts do
not monopolize the recovery batch. These are local recovery mechanisms; they do
not remove model context, per-attempt time/step, sandbox resource or external
service limits.

The integration checks in
`tests/integration/dify_agent/runtime/test_workbench_fault_recovery.py` require an
isolated Redis and an external restart supervisor. They exercise a killed native
process after a completed file operation, restoration through the actual API
history bridge, and a Redis restart during checkpoint writes and cancellation
observation. They do not establish arbitrary external side-effect idempotency or
replace target-environment model, worker and sandbox-manager acceptance tests.
