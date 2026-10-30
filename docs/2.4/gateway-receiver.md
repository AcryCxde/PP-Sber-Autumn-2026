# Gateway-side runner connection — review candidate

Base: `runner-v0-input-usage-sha@8bfe889928c8b2755eb3545eee4758162e7bde0e`.
This implements the previously missing **receiver**, not a new runner protocol.
Canonical wire format remains [protocol.md](protocol.md), `v: 1`; public events
remain [events-v0.md](../contracts/events-v0.md), `schema: events.v0`.
Review by Dima is required; this document does not record contract acceptance.

## What is executable

- `backend/runner_app.py`: mountable FastAPI WebSocket router `/v1/runner`.
  Runner opens the connection; no inbound runner container port is needed.
- `backend/runner_transport.py`: durable transport outbox, acknowledgements,
  project journal cursor, event-id deduplication and public run event replay.
- The actual `ctrunner.gateway.GatewayClient`, not a rewritten fake client,
  is exercised by `backend/tests/test_runner_app.py` against a local Uvicorn server.
- Existing `backend/main.py`, browser `/ws`, migrations and React A/B are unchanged.

The SQLite transport file is **not** Arina's artifact storage. It contains no
artifact file bytes and is not a second artifact API. It does contain prompts,
answers and safe public text: protect it like other project data, with access
controls, backup/retention and a persistent disk outside runner project volumes.

## Dima: embed in the real gateway

1. Pin this candidate and the runner SHA above (plus separately reviewed fixes),
   rather than searching floating branches. Review the existing wire contract.
2. Create `RunnerTransport(path)` once; use one process/worker per transport file.
   This candidate uses short synchronous SQLite transactions, default durable
   commits, a unique active-run index, event-id and `(run,seq)` uniqueness. It is
   not a multi-worker distributed gateway implementation or a load-test result.
3. Instantiate `RunnerBridge(transport, {project_id: project_token})`. Each project
   must have a distinct non-empty token. Never place tokens in source/control/UI.
4. Mount `create_runner_router(bridge)` in the existing FastAPI app. Start and stop
   `bridge.start()` / `bridge.stop()` in its lifespan. The standalone factory below
   already does this; the legacy gateway lifespan is deliberately not rewritten.
5. Before dispatch, authenticate the **browser user** in the real gateway and
   check their project/conversation/run ownership. Map the real project's identity
   explicitly; do not silently equate a legacy conversation UUID to a project ID.
   Internal Python methods are trusted gateway calls, NOT anonymous HTTP endpoints.
6. On a real, accepted browser action call:

   ```python
   command = transport.message(
       project_id, run_id, client_request_id, text,
       roles=["researcher"], stage="analysis", input_files=["inputs/context.md"],
   )
   # gateway must put actual authorized input-file bytes into the runner's project
   # volume BEFORE enqueueing; passing a filename alone does not upload anything.
   ```

   Gateway chooses `run_id`; it becomes `command.id` and the runner's `turn_id`.
   Repeating the exact request returns the same command; changing its payload or
   project is an idempotency conflict. One active Run **per project** is the bounded
   candidate's deliberate restriction (stricter than legacy per-conversation).
7. For a stored `facilitator.question`, keep its `run_id` and exact question texts:

   ```python
   transport.answer(project_id, run_id, answer_command_id, question_id,
                    {"Exact question text?": "Chosen label or custom answer"})
   ```

   `answer_command_id` is new, but no new Run is created. Keys must match every
   question; multi-select is joined with `", "` as in the runner contract.
8. Poll/use `transport.events(project_id, run_id, after_seq=N)` from the authenticated
   gateway's existing REST/WS/SSE projection. Only then connect React B to that real
   gateway. No public replay/command endpoint is added here; no second browser API
   is invented. Do not merge these events with a separately generated legacy
   `run.accepted` stream without choosing one durable owner/sequence.

## Delivery, questions and failures

`hello.project_id` must match the Bearer credential. A second live connection for
the same project, unsupported wire version, foreign run/event, malformed payload,
runner-supplied `run.accepted` / `artifact.saved`, and gateway-only unknown codes
are rejected. SDK/raw/tool fields are not projected. Text still relies on runner
redaction; this is not proof of complete secret/path-content filtering or isolation.

The outbox is committed before sending; `command_ack` is committed before it stops
being retried on reconnect. Accepted means **durable runner inbox**, not execution,
completion or saving. Rejected receipts are exposed by `ack_receipt()` and
`status().delivery_blocked/rejected_commands`; they are not automatically converted
to invented v0 failure codes. Dima must surface them and choose a reviewed recovery
policy. `unknown_fork` means wait for the new question after runner restart, not
blindly resend an old answer. A rejected original message remains a blocked queued
Run in this candidate; operator cancellation/replacement policy is not implemented.

The server's persisted `acked_seq` is a **journal** cursor, not public `seq`.
The real client resends `journal_seq >= acked_seq` (inclusive). Multiple public events
from the last journal row are safe: `event_id` deduplicates, but another event from
the same row is accepted. Reused event IDs with different bodies are rejected.
A journal behind the gateway's saved cursor is rejected, not silently reset.

Healthy `awaiting_answer` is normal. After an actual registered connection has not
sent heartbeat for more than 30 seconds, one `run.outcome_unknown{heartbeat_lost}`
is persisted for an active non-finished Run. Wall-clock behavior is tested using an
injected `now`, not 30-second blocking sleeps. Terminal events are last; late new
events do not reopen a terminal Run. Fresh work must use a new linked Run in the
embedding gateway, not mutate an old terminal launch.

## Arina + Dima: storage boundary remains required

`artifact.created` stores metadata only. A runner `run.completed` is held if any
artifact lacks confirmation; `status().awaiting_storage` is explicit. Missing
storage configuration does not fabricate `artifact.saved`, `completed`, failure
or unknown. Completion-pending storage timeout/reconciliation policy remains open;
these Runs can remain pending until a genuine receipt arrives.

Only the trusted storage adapter, **after** reading exact committed bytes, checking
SHA/size and durable storage readback, may call:

```python
transport.confirm_saved(project_id, run_id, artifact_id, {
    "artifact_id": artifact_id, "version_id": immutable_version_id,
    "size_bytes": actual_size, "download": authorized_download_reference,
})
```

The adapter must authorize downloads and verify `sha256`; this transport checks
receipt shape and metadata size, but cannot independently prove byte/hash storage.
It emits `artifact.saved` before releasing the held `run.completed`. Repeated
identical receipts are no-ops; conflicting receipts and writes after terminal
state are rejected. Separate approval/version operations, ZIP bytes, storage errors,
ambiguous save outcome and recovery are **not** implemented by this receiver.

## Local standalone launch (no model call)

Use the declared backend dependencies, Python 3.12 for tests importing the pinned
runner client, and the runner package/dependencies installed as usual. The receiver
itself does not import Claude SDK or provision paid credentials.

Set `RUNNER_TRANSPORT_DB` to a persistent transport SQLite file and
`RUNNER_PROJECT_TOKENS_FILE` to a protected JSON file mapping known project IDs to
unique gateway tokens. No default credentials exist. Then run:

```powershell
python -m uvicorn backend.runner_app:app_from_env --factory --host 127.0.0.1 --port 8011
```

Configure the runner's `GATEWAY_URL=ws://127.0.0.1:8011/v1/runner` only for a
same-host development test; in containers use a routable gateway service name and
the separately reviewed runner WSS policy. Production TLS, proxy WebSocket upgrade,
secret files, network isolation and persistence configuration belong to Filipp's
deployment review. Do not expose this development endpoint to the internet.

## Checks and outstanding acceptance

```powershell
$env:PYTHONPATH = 'runner/src'
python -m pytest backend/tests/test_runner_transport.py backend/tests/test_runner_app.py -q
python -m pytest backend/tests -q
git diff --check
```

The real-client contract test uses controlled journals/inbox, no Claude invocation,
no real user, no real artifact storage and no product/server deployment. Passing
it is **transport compatibility evidence only**, not the complete user E2E.

Before integration acceptance: Dima reviews mapping into actual authenticated
gateway commands/public replay and legacy DB ownership; Arina provides executable
byte-storage adapter; Filipp configures persistent deploy and runs the real
question → answer → result → save → ZIP → restart/reopen chain. Katya then checks
React B. PR #11 remains draft; no merge is authorized by this candidate.
