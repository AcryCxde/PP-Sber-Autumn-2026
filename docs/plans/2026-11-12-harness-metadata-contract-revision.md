# Task6: harness metadata contract revision

**TECHNICALLY APPROVED for implementation — 2026-11-12; не разрешение rollout.**
Независимое повторное ревью закрыло оба контрактных blockers: versioned storage wrapper и atomic monotonic projection. Runtime acceptance остаётся обязательным.
2026-11-12. Направление принято координатором; ниже source-inspected proposal, без runtime-доказательств.

## Решение и границы
- Рекомендация: private diagnostic envelope на существующем WS с независимыми identity/version/receipt.
- Optional enrichment usage.reported отвергнут: canonical body того же deterministic ID меняется при upgrade/rollback.
- Existing public event store недостаточен **без расширения**: `_append` назначает public seq, `ingest` отвергает новые terminal events; нет private receipt/source journal seq/backend binding.
- Это не доказательство невозможности любого reuse SQLite: предлагается расширить тот же transport file, не строить broker/новый WS/универсальную шину.
- V1 только session_total, unknown != 0; run_delta требует отдельно доказанной continuity/baseline/attribution. Без monetary KPI/лимитов, без изменения Run.cost_usd.

## Проверенные anchors (статическое чтение, не выполненные тесты)
- `runner/src/ctrunner/gateway.py:45–51` `parse_welcome` игнорирует additions; `JournalFeed.poll:101–122` читает с начала, выдаёт `event.seq >= after_seq`.
- `_connection:249–255`, `_send_events:266–271`: отдельные задачи, прежний порядок drafts; `_receive:290–304` сейчас обрабатывает только command, diagnostic ACK будет проигнорирован без расширения.
- `runner/src/ctrunner/v0.py:_Out.emit:337–343`: UUID5 key содержит project/run/source.seq/draft ordinal; insertion draft меняет последующие IDs. `_opaque:326–327` — precedent UUID5, не privacy proof.
- `backend/runner_app.py:RunnerBridge.handle:92` welcome без capability; `_receive:114–134` closed dispatch, неизвестный frame → unexpected_frame; terminal_run → skip cursor.
- `backend/runner_transport.py:RunnerTransport.__init__:185–203`: events хранит event_id/project/run/public seq/body/source; journal_seq в event row отсутствует.
- `_append:224–230` назначает public seq; `ingest:337–380` canonical comparison до terminal checks, затем terminal/completion guards и общий project cursor.
- `backend/runner_executor.py:run_via_runner:172–186`: answer использует старый transport Run, новый backend run_id становится command_id; `after_seq` — только локальная переменная.
- `run_via_runner:191–208` usage присваивается локальному payload; question завершает текущий backend Run. Persisted source→backend Run mapping здесь нет.
- `backend/harness_estimate.py:snapshot:7–23` принимает decimal-string, UUID, timestamp; `project_snapshot:26–36` сравнивает seq только при одинаковом session_ref, persisted lineage не содержит.
- Документы `docs/2.4/protocol.md:50` говорят replay `>`, но прочитанный sender использует `>=`; legacy реализацию не менять ради документа.

## Неизменяемые legacy-инварианты
- Usage bytes/8 полей/event_id, draft order, source seq, public seq, ACK/cursor/replay/dedup и terminal policy неизменны.
- Diagnostic не events.v0, не двигает cursor/public seq, не открывает Run и не обновляет accounting.
- Не strip/re-enrich legacy events и не требовать нового skip-кода от настоящего старого sender.

## Предлагаемый wire v1 (ещё не существующие symbols/API)
- Optional `welcome.capabilities.harness_diagnostic_v1=true`; нет capability/версия неизвестна → только legacy.
- Frame `{v:1,type:'harness.diagnostic',schema:'harness.diagnostic.v1',diagnostic_id,project_id,transport_run_id,source_event_id,source_journal_seq,payload}`.
- Payload ровно `{session_ref,observed_at,value_usd}`; scope session_total задаёт schema; continuity unverified, baseline null.
- Diagnostic ID: UUID5 отдельного фиксированного namespace от compact UTF-8 JSON array `[schema,project_id,transport_run_id,source_event_id]`; namespace фиксируется golden fixture до GREEN.
- Session ref: UUID5 другого фиксированного namespace от compact UTF-8 JSON array `[project_id,actual_result_session_id]`.
- Это deterministic project-scoped pseudonym, **без секрета и без обещания необратимости**: low-entropy ID можно перебрать; не использовать как auth/access token.
- Не включать connection time/hello runner_version в canonical body. observed_at берётся из source row; отсутствующая/невалидная session/value → unknown, не synthetic session_marker.
- Diagnostic pass читает raw row JSON с parse_float=Decimal, отдельно от legacy parse; finite nonnegative value сериализуется fixed-point без exponent/лишних trailing zeros, negative zero → '0'; bool запрещён, максимум 64 символа.
- Значения длиннее/невалидные → unknown; не округлять. JSON wire хранит decimal-string; legacy float parsing и bytes не меняются.
- Exact canonical body сохраняется до parsing/DTO normalization; duplicate с другим body → conflict, не «нормализовать до совпадения».
- V1 не передаёт raw result/model_usage/session_id/content. Это allowlist v1, не запрет будущего scope: проверенные derived facts допускаются отдельной schema/capability, не расширением принятого тела.

## Минимальное persisted расширение transport SQLite
- Новые additive tables в `RunnerTransport.__init__`, не PostgreSQL/Alembic: старый код использует явные existing tables и не обязан знать новые.
- `diagnostic_segments(project,transport_run,command_id PRIMARY KEY,backend_run,start_after_public_seq,end_public_seq NULL)`; уникальный открытый segment на transport Run.
- Зарегистрировать pending segment атомарно с transport.message/answer enqueue: command_id=backend run_id; backend_run передаётся trusted executor, не sender. Для answer активировать binding только после accepted public user.answer.accepted; usage в gap enqueue→accepted навсегда ambiguous/unknown, не привязывать ни к предыдущему, ни к новому segment.
- При ingest usage в том же SQLite commit: `diagnostic_sources(source_event_id PRIMARY KEY,project,transport_run,command_id,source_journal_seq,source_public_seq)`; question/terminal закрывает segment.
- Сегменты определяются persisted command/public boundaries, не arrival diagnostic и не latest active backend Run. Без зарегистрированного segment исторический source не получает guessed binding.
- Legacy source duplicate не переписывает binding; неизвестный старый source без seq/binding → unknown. Новый private pass не может сам заверить исторический source seq.
- `diagnostic_receipts(diagnostic_id PRIMARY KEY,schema,canonical_body,source_event_id UNIQUE,accepted_at)`; UNIQUE source_event_id для v1, будущая версия требует явного supersession policy, не двойной проекции.
- Receipt связывается с immutable diagnostic_sources; lineage/order = `(project,transport_run,command_id,source_public_seq,source_journal_seq)`, не timestamp/session_ref alone.
- Не сравнивать journal_seq между transport Runs/пересозданными lineage; latest внутри target segment по source_public_seq, не max(value)/arrival order. Разные session_ref не доказывают continuity.
- Предложенные tables/constraints ещё не реализованы; crash/locking behavior и пригодность границ segment требуют independent review и тестов.

## Delivery и backend projection: два persistence domain
- Независимый metadata pass retained journal воспроизводит candidates ниже legacy cursor; IDs берёт из неизменённого Translator replay, не вставляет новые drafts.
- Receiver проверяет authenticated project, accepted usage/source row и binding. Нет source/binding → retryable reason, не accepted receipt/guess.
- `harness.diagnostic_ack {v:1,schema,diagnostic_id,status:'accepted'}` означает durable SQLite receipt; **не** atomic commit Run.harness_estimate в другой backend DB.
- Exact duplicate после lost ACK → тот же receipt; другой body → canonical conflict. До commit повтор — первая запись. Per-ID bounded backoff и fair round-robin: source_not_accepted/terminal absent-source не блокирует другие IDs; постоянный missing source остаётся unknown без accepted ACK, reconnect не превращает его в success.
- Backend detail catch-up читает receipts по immutable backend_run и идемпотентно обновляет Run.harness_estimate в backend transaction; запускается также executor при finalize. Detail после terminal восстанавливает пропущенную projection без worker.
- Versioned Run JSON: `{storage_schema:"harness_estimate.v1",estimate:{...DTO},diagnostic_id,lineage:{project,transport_run,command_id},source_order:{public_seq,journal_seq}}`; **нет верхнеуровневых scope/state/value_usd/source/session_ref**. Новый mapper распаковывает только известный wrapper; pinned старый safe_estimate обязан вернуть None → API unknown. Extra lineage fields у старого flat DTO НЕ обеспечивают rollback safety: old mapper их отбрасывает.
- Atomic monotonic projection: backend transaction блокирует target Run SELECT FOR UPDATE (если требует общий порядок — User→Conversation→Run), затем fresh SELECT persisted JSON под защитой, сравнение same-lineage public_seq и запись wrapper внутри той же transaction; не использовать stale ORM. Для SQLite — тот же comparison/write в BEGIN IMMEDIATE transaction (не FOR UPDATE no-op); lock/busy → bounded retry всей операции. Не держать transport SQLite transaction при backend lock. Equal order + differing identity/body → conflict; incomparable lineage → unknown/no overwrite.
- Даже если A заранее прочитал receipt S1, после B(S2) fresh persisted order защищённо отсекает S1; individual idempotency недостаточна. Crash/retry безопасен только с этим atomic guard; ACK от projection не зависит.
- Retained journal потерян → недоставленное unknown; уже принятый receipt остаётся восстанавливаемым. Новый retention service не нужен.

## Обязательная compatibility/failure matrix
- Lost ACK до/после receipt commit; source accepted/unacked replay; diagnostic ниже cursor; replay older source после newer; restart между receipt и backend projection.
- Actual old sender rollback из pinned pre-change revision, не новый sender с feature=false: usage bytes/IDs неизменны, receipts остаются; недоставленное ждёт upgrade/journal retention.
- Old receiver: capability отсутствует, private frame не посылается; downgrade receiver не удаляет additive tables. Rollback safety доказывает pinned old safe_estimate fixture на versioned wrapper, а не обещание об extra fields.
- Multi-draft row: disconnect между legacy drafts; diagnostic ACK не подтверждает соседей и не двигает journal cursor.
- Late diagnostic после terminal разрешён только для уже accepted usage с persisted source/binding; late result без accepted usage не диагностируется, terminal skip не обходится.
- Answer/new backend Run, delayed diagnostic предыдущего segment; enqueue→user.answer.accepted gap usage и prefeature mapping → unknown. Deterministic two-session barrier: A читает S1, B commits S2, A продолжает → persisted S2. Session reset/change, invalid Decimal/boolean/NaN, no session, privacy, exact conflict и no double count.

## Исполнение после independent review
- Конкретные revised RED/GREEN steps, test paths/commands и additive migration/rollback перечислены в Task6 implementation plan; старые Steps сохранены как superseded history.
- Tasks1–5 и прочие Tasks не меняются. Числа недоступны, пока receipt/binding/projection не доказаны; harness estimate требование сохраняется.
- Проверки этой ревизии: только статическое чтение указанных Python anchors; тесты/модели/PG не запускались, credentials не читались. Boundary scope; code diff отсутствует, runtime/gate PASS не заявляется.
