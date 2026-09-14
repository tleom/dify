# Dify Agent runtime

Dify Agent hosts Agenton-composed Pydantic AI runs behind a FastAPI API. Its
source code stays under `src/dify_agent`, while framework-neutral Agenton code
stays under `src/agenton` and `src/agenton_collections`.

See the [operations guide](guide/index.md) for local server behavior.

Workbench Office, browser, and environment usage instructions are maintained in
the Agent's published config files and loaded through the config layer. The
workbench environment layer describes shared dependency updates and conversation
file placement.

Workbench executions additionally emit `context_status` events. Their `data.phase`
is `usage`, `compacting`, `compacted`, or `failed`. Token counts describe the current
request context, never cumulative billed usage. `estimated` distinguishes native
pre-request estimates from provider response usage; an unknown model window stays
null. Compaction phases share `compaction_id` and include `before_tokens`.
The existing tiered compactor remains responsible for rewriting history. These
events are non-terminal; consumers that do not display context can ignore them.
