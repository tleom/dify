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
composition supplies the trusted logical `workbench_run_id`. Its sequential
`report_activity` tool lets the same task model describe an action and purpose,
update the stage, and close an activity after results return. The runtime assigns
activity IDs and revisions, binds each business call at execution start, and
restores the same identity when human input or environment installation resumes
in a different native run. Parallel business calls remain parallel. Tool retries
become error records only when an execution binding exists; argument-validation
failures do not start tool work, while actual execution failures remain visible.
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
still emit tool and text records with the reporting tool disabled. Legacy runs
retain their previous reader and composition contract. Do not drop the journal
when merely disabling reporting.
