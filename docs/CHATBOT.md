# ConScoutAI backend

`POST /chat` now uses an Ollama tool-calling agent by default. The model interprets
the question, requests read-only project data, and generates a cited answer from
the tool results. The legacy keyword router and template answers have been removed.
Changing a suggested question does not require a new routing branch.

## Runtime setup

Set these variables in the **main API process/container**:

```dotenv
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.2:3b
CHAT_TIMEOUT_SECONDS=45
CHAT_MAX_TOOL_CALLS=6
CHAT_CONTEXT_TOKENS=16384
```

Use the reachable Ollama service hostname when API and Ollama run in separate
containers. The configured model must already be pulled into that Ollama runtime
and support native tool calling plus structured JSON output. The existing model
name is a compatibility default, not a quality recommendation: evaluate it on
real project questions and the deployment hardware before production rollout.

Ollama must share the API's Docker network. For the existing VPS containers:

```sh
docker network connect conscout-main conscout-ollama
```

Run this only if Ollama is not already attached. The existing 4 GB VPS needs
swap to load the 3B model alongside the other services and uses
`CHAT_CONTEXT_TOKENS=8192`. Evidence/history budgets shrink with this context
setting. Swap prevents out-of-memory kills; measure response latency on the host.

The old `CHAT_INTENT_PROVIDER`, `CHAT_ANSWER_STYLE_PROVIDER` and
`CHAT_ANSWER_FORMATTER` switches do not control the new agent. It does not silently
fall back to keyword templates if Ollama is unavailable. Restart the API after
deploying code and changing environment settings. API startup creates conversation
indexes; Mongo credentials must permit index creation as with the other services.

Check the configured runtime from the API host with `GET /api/tags` and ensure
the configured model is listed. Use `POST /api/show` with `{"model":"llama3.2:3b"}`
to inspect its capabilities. Ollama API reference:
[tool calling](https://docs.ollama.com/capabilities/tool-calling) and
[chat](https://docs.ollama.com/api/chat).

## Tools and data

* `list_projects`: accessible projects read from the server's sites collection.
* `get_project_details`: selected project metadata and team membership.
* `read_project_records`: tours, comments (including panorama comments),
  inspections, recipient-scoped notifications, and the material ledger. Supports
  literal text, exact status, UTC date ranges, specific tours, and pagination.
* `get_project_progress`: existing server schedule calculations and recorded
  latest-tour data. Activities support text/status filters and pagination.

The model can combine tools to answer explanations and comparisons. A frontend
tour ID is a context hint; it does not force every project question into that tour.
Project names supplied by a client cannot grant access. Every data query enforces
the authenticated user's ownership/shared floorplan scope and the selected
project. Notifications additionally require the user's recipient identity.

Only allowlisted fields are sent to the model. Database queries are server-built;
the model cannot submit arbitrary Mongo operators, run code, or modify data.
Record text and conversation history are explicitly treated as untrusted data.
The agent validates tool arguments and source IDs, but these checks do not prove
that every generated interpretation is correct; live answer evaluation is required.

Record searches return exact `total_matching` counts for their filters before
pagination. Result pages contain at most 20 records and expose `has_more` and
truncation. Field text is capped at 1,200 characters and nested lists at 30 items;
the result is a bounded evidence excerpt, not a full document. A request permits
at most 6 tools by default, four tool rounds and up to 24,000 characters of tool evidence
(8,384 at an 8,192-token context). History is also capped relative to the context.
Database aggregation queries use a three-second server execution limit. Existing
schedule calculations retain their own execution behavior. Model calls share a
45-second request budget; hardware must serve within the frontend's 60-second
timeout. Configure `CHAT_CONTEXT_TOKENS` for model/hardware capacity.

## API contract and follow-ups

Existing clients can continue sending:

```json
{"message":"What needs attention in Fozan?","site_name":"Fozan"}
```

The response still includes `answer`, `intent`, `sources` (dataset names), and
`timestamp`. New fields include:

* `citations`: source ID, tool, project, dataset, retrieval time, filters, page
  counts, and available record IDs. `[S1]` references in the answer correspond to
  these entries. A source represents a query result or calculated summary; a
  citation is not automatically a clickable frontend URL.
* `conversation_id`, `conversation_saved`: server conversation memory status.
* `request_id`, `model`, `intent_source: "ollama_tools"`, `degraded`.

For the next turn, send the returned conversation ID:

```json
{"message":"Who owns those issues?","conversation_id":"<returned 32-character ID>"}
```

Clients may alternatively supply up to 12 `history` messages with only `user` or
`assistant` roles. Do not send both `history` and `conversation_id`. Tool/system
messages from clients are rejected. History is bounded and never substitutes for
fresh evidence. A new request without a conversation ID creates a new conversation.

The `chat_conversations` Mongo collection stores only recent user/assistant
messages, owner, revision and timestamps. It retains at most 12 messages, expiring
30 days after the last saved turn. TTL cleanup is asynchronous; reads immediately
reject expired conversations. Cross-account IDs are indistinguishable from missing
IDs. Concurrent turns using the same revision return HTTP 409 so a client can retry
against fresh context. If saving history fails after an answer, the answer is still
returned with `conversation_saved: false`.

The current web proxy/widget must forward and retain `conversation_id` (or history)
to use follow-ups, and render `citations` to provide clickable source UI. Those
frontend changes are separate from this backend update. Existing single questions
continue to use the same `answer` field.

HTTP 503 means the model/data service is unavailable, 504 means model timeout,
502 means an invalid or unsupported model answer, and 404 means an unavailable
conversation. The assistant distinguishes retrieval failure from a successful
empty search. Logs contain request IDs, model, tool count, errors and elapsed time,
not complete prompts, retrieved records or secrets.

## Verification and rollout

Run offline tests:

```sh
python -m pytest tests/test_chat_agent.py -q
```

These use synthetic Mongo results and model responses, covering question
forwarding, tool loops, scoping, pagination, source validation, failures,
conversation ownership, and endpoint compatibility. They do not verify actual
Mongo aggregation execution or model quality. On the deployed services, verify:

1. Each of the three UI suggestions returns the requested information and cites
   records from the selected project.
2. Paraphrases and a combined question such as "Why are we behind, and which open
   inspections might be related?" use appropriate tools and qualify interpretations.
3. Follow-ups reuse context while fetching fresh facts. Date comparisons specify
   their date windows and do not invent historical progress snapshots.
4. A second account cannot retrieve the first account's project, notifications,
   or conversations, including via a forged project/tour ID.
5. Empty data, unavailable Ollama, missing models and malformed model outputs
   produce honest answers or service errors within the client timeout.

Semantic/vector search, document ingestion, streaming, and a frontend source-link
experience are not part of this update. Current text retrieval is literal search.
Add semantic retrieval if evaluation shows comments/reports cannot be found
reliably with these tools. No training on the Mongo database is required.
