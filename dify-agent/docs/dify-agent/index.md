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
published instructions or config files. The published CLI and environment
declarations retain their existing runtime behavior and sandbox restrictions.

Workbench knowledge selection requires an access attempt before a final answer,
while preparation tools and deferred human/environment requests remain available.
Access failures are explicit observations; they neither count as successful
searches nor force repeated calls. Retrieval failures within a dataset propagate
to this boundary instead of silently becoming empty or partial search results.

Workbench search observations are paginated JSON. `knowledge_base_read_results`
continues the same result using `search_id` and `next_offset`; the current run
retains its latest five results across suspension. `knowledge_base_list_documents`
and `knowledge_base_read_document` use the authenticated inner document API to
page through selected datasets. Each page rechecks the active run's owner,
selection and current dataset permission. `complete`, `unavailable_count` and
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
