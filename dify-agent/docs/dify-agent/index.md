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

Workbench executions additionally emit `context_status` events. Their `data.phase`
is `usage`, `compacting`, `compacted`, or `failed`. Token counts describe the current
request context, never cumulative billed usage. `estimated` distinguishes native
pre-request estimates from provider response usage; an unknown model window stays
null. Compaction phases share `compaction_id` and include `before_tokens`.
The existing tiered compactor remains responsible for rewriting history. These
events are non-terminal; consumers that do not display context can ignore them.

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

Pending generated paths persist in the session snapshot across deferred continuations of the same workbench run. URL verification resets on resume, and a new logical run clears the pending paths. Entries with `downloadable=false` remain visible but receive no URLs; the model must split unsupported artifacts or explain that delivery is blocked. File downloads are limited to 20 MiB, and directory archives to 50 MiB of supported file contents.

Final delivery requires a download URL for at least one changed path or a directory archive containing it. Each query refreshes verification for its requested path; file changes invalidate previous query results and failures. Unrelated files do not establish delivery or explain a failure to deliver the current artifacts.

Workbench shell sessions also expose `file_create(path, content)` and `file_edit(path, old_text, new_text)` for bounded UTF-8 file operations inside the current workspace. Creation refuses overwrite; editing requires exactly one match and preserves unchanged content. Shell argument envelopes are only unwrapped when complete JSON parses, independently of activity reporting; repeated malformed calls produce bounded, explicit observations.

The runtime inventories regular files inside the conversation directory before execution,
after tool results, and before publishing a model response. New and changed paths emit
ordered `file_create` / `file_edit` tool records with `output.source=workspace_change`,
including binary files produced by shell scripts or other tools. Explicit file tools
retain their original row without a duplicate observation. Inventories do not follow
symlinks or include hidden files, dependency/cache directories, browser profiles,
Office work profiles, logs, lock files, or edit temporary files. These incidental
files neither emit inventory rows nor count as pending file deliverables. Documents,
images, data files and generation scripts remain observable. Explicit file-tool calls
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
