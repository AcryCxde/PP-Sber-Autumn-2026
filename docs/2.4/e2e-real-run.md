# Runbook: проверка реального продуктового пути

Срез: canonical master `b76f360fe77519c8e1687a9cfc2a09ed112d68c6` (PR #27).
Это инструкция для будущего разрешённого прогона, **не отчёт E2E PASS**.
Полный прогон с живой моделью и прогон в k3s при подготовке документа не выполнялись.
Локальные тесты/fixture/smoke не подтверждают production readiness.

## 1. Границы и подготовка

- Запуск backend/frontend: [RUN_APPLICATION](../RUN_APPLICATION.md); продуктовый React workspace и его dev-режим: [web-product](../../web-product/README.md). Проверять именно V2 workspace, не статический пример и не свободный чат. Для отдельно запущенного web-product используется его dev-server (4191); `/product/` — путь собранного интерфейса в Docker stack.
- Текущая реализация: [V2 router](../../backend/api/product.py), [проекция ProductRun](../../backend/product.py). Старый [V2 proposal](../../web-product/docs/ctf-v2-contract.md) содержит исторические ограничения и предложения; он не заменяет проверку текущего router.
- Зафиксировать фактический SHA каждого компонента, image digest и безопасную версию конфигурации. Название ветки, локальный тег образа или SHA этого документа не доказывают, что именно они работают на стенде.
- Использовать отдельные синтетические аккаунты A/B и проекты, несекретные файлы (например, `brief.md` и `excluded.md` с разными маркерами). Получить разрешение на записи, модельный бюджет, скачивание и отдельно — fault injection. Нет разрешения/предусловия — `NOT_RUN`, а не обход ограничения.
- Разрешение на production read-only **не разрешает** export/download данных, migration bridge, restart, deployment или остановку writers. Этот runbook сам по себе ничего из этого не разрешает. Здесь нет команд SSH, deploy или разрушительных fault injections.
- В canonical compose runner создаётся через Docker API: backend монтирует `/var/run/docker.sock`, задаёт `RUNNER_IMAGE`, `RUNNER_NETWORK`, `RUNNER_GATEWAY_URL`. Доступ к socket эквивалентен root на хосте; это не доказательство совместимости с k3s/containerd. Отдельно подтвердить runtime, сеть, volumes и сохранность данных; не «лечить» разрыв выдачей привилегий.
- Миграция deployed fork — **отдельное maintenance**, по [CANONICAL_MIGRATION](../CANONICAL_MIGRATION.md): проверенный восстанавливаемый backup, репетиция restore, quiescence всех writers/schedulers, проверка схемы. Не использовать `alembic stamp` как shortcut и не запускать bridge внутри E2E. Обычный startup canonical БД и bridge несовместимой истории — разные процедуры.

## 2. Какие идентификаторы и подтверждения не смешивать

| Сущность | Текущий смысл |
|---|---|
| ProductRun V2 (`snapshot.id`) | Публичный запуск `/api/v2`; цепочка initial/rework + ответов. Ответ сохраняет **тот же публичный ID**. |
| Legacy/internal `Run` | Исполнение в conversation; ответ может создать следующий внутренний Run, связанный через `ProjectRun.product_run_id`. Не подставлять публичный ID в legacy `/api/runs/...`. |
| Rework | Новый ProductRun того же проекта, с `parentRunId` и `sourceArtifactId`; не ответ и не повтор старта. |
| Artifact/version | `artifact.id`, `documentId`, `stageId`, `version`; `file` содержит ссылки/метаданные полного файла, если доступен. История версий не должна переписываться. |
| Подтверждения | Принятие HTTP/transport ack ≠ сохранённые bytes ≠ отдельный пользовательский approval receipt. `completed` без проверенного результата не доказывает скачиваемость всех файлов. |

V2 получает snapshots через GET/polling, не через browser WS events.v0.
Статусы текущей проекции: `queued`, `running`, `waiting_for_input`, `completed`, `failed`, `outcome_unknown`.
Внутренний Run может быть `completed` с вопросом, пока публичный V2 ждёт ответа.
Не требовать от V2 последовательности legacy событий как от HTTP-контракта.

## 3. Основной путь (на разрешённом тестовом стенде)

Работать через UI; в Network сверять запросы и redacted ответы, не публиковать HAR целиком.
Все пути ниже относительно **`/api/v2`**, требуют сессии владельца.
Сохранять исходные тела мутаций и operation UUID для ручного retry, не менять их при неясном исходе.

| Шаг | Действие / реальный HTTP-контракт | Что проверить и сохранить в протоколе |
|---|---|---|
| T1 Проект и роли | `POST /projects`: `clientProjectId`, `title`, `goal`, `context`, `scenario`, три preparation `answers`, `team`, `teamNote`, `stages[{id,title,goal}]`. | Серверный project ID. Роли из текущего реестра: `researcher`, `analyst`, `specialist`, `editor`; неизвестная/повторяющаяся роль → 422 `unknown_role`. Выбор роли/текст prompt не доказывают отдельное исполнение роли: `roleEvents` сейчас не выдаются. |
| T2 Материалы | `POST /projects/{project_id}/files`, multipart `file`, `clientFileId`, `sha256`. Дождаться receipt `{id,sha256}`. | Hash фактических исходных bytes и размер. Для нового clientFileId неправильный hash → 422 `sha256_mismatch`. Повтор upload определяется clientFileId+заявленным hash; receipt повтора не является независимым повторным измерением bytes. Не считать upload доказательством PDF/Office extraction или malware scanning. |
| T3 Старт и selection | `POST /projects/{project_id}/runs`, исходный plan + `stageId`, `fileIds` только выбранных server file IDs; `Idempotency-Key` обязателен. | Новый публичный ID; этап входит в plan. Начальный запуск обрабатывает весь план, а не только один выбранный этап. Выбранный материал доступен исполнителю, снятый с выбора отсутствует в prompt/workspace. HTTP receipt загрузки сам по себе последнее не доказывает: нужны разрешённые наблюдения либо тест. |
| T4 Вопрос | `GET /projects/{project_id}/runs/{run_id}` до реального `waiting_for_input`. | Текущие `question.id`, все `question.questions[]`, revision. Если модель не задала вопрос — вопросный путь `NOT_RUN`, не подделывать событие на живом стенде. |
| T5 Ответ | `POST /projects/{project_id}/runs/{run_id}/answers`: `{questionId,answers:{"точный текст вопроса":"ответ"}}`, один ответ на каждый вопрос; стабильный `Idempotency-Key`. | Тот же публичный `snapshot.id`, новые сообщения/revision, продолжение исполнения. Для нескольких выбранных вариантов — текстовые labels через запятую и пробел. Не заменять ответ новым `/runs`. Устаревший вопрос → 409 `unknown_fork`, неполная карта → 422 `answers_mismatch`. |
| T6 Результат | GET того же Run до terminal; проверить `artifacts`, `saved`, `savedAt`, versions и сообщения. | Прочитать фактический файл, не только preview. Agent сохраняет документы этапов в `coreteams/<stageId>.md`; прочие файлы привязываются к этапу запуска. `saved=true` — проекция сохранённой записи, не гарантия работоспособности S3 или полных bytes старого/усечённого снимка. |
| T7 Bytes и версия | В snapshot взять точный `artifact.file.downloadUrl` (когда есть); ZIP: `GET /projects/{project_id}/runs/{producing_run_id}/artifacts/{artifact_id}/zip`. | Размер скачанного файла против `file.sizeBytes`, hash — против известного синтетического оригинала или отдельно разрешённого storage evidence (V2 `file` сейчас не содержит SHA-256). ZIP: content-type `application/zip`, корректный архив, документ нужной версии + `manifest.json` с document/version/artifact/producing run. Не угадывать legacy ID из ProductRun ID. Наличие `PK` — только format smoke, не integrity proof. |
| T8 Одобрение | `POST /projects/{project_id}/runs/{producing_run_id}/artifacts/{artifact_id}/approve`: `{artifactId,version}`, стабильный `Idempotency-Key`. | Отдельный `{receiptId,artifactId,version,approved,approvedAt}` для точной версии; повтор возвращает тот же receipt. Ошибочная версия → 409 `version_conflict`. GET Run не меняется от approval; receipt не обязан появляться в snapshot. ZIP manifest после approval содержит receipt; approval не обязателен для скачивания ZIP. |
| T9 Доработка | После `completed` последнего ProductRun: `POST /projects/{project_id}/runs`, plan + `text`, `stageId`, `parentRunId`, `sourceArtifactId`, `documentId`, `sourceVersion`, выбранные `fileIds`, **новый** operation key. | Новый публичный Run, точные parent/source links, тот же documentId и следующая версия при сохранении изменённого документа. Старый Run/bytes остаются прежними. Неверная версия → `version_conflict`; незавершённый/не последний parent → `project_state_conflict`. Полный источник проверяется по hash/размеру; недоступный → `source_unavailable`, не пустая подмена. Rework поддерживает UTF-8 текст, бинарный источник → `source_not_text`. |
| T10 История | Выбрать старую версию после rework; читать/скачивать её через **производящий её Run**, а не последний. | Старые bytes/hash неизменны; approval относится к старой точной версии, а не автоматически к новой. Архив целиком может измениться после approval из-за manifest: сравнивать отдельно bytes документа. |

Продуктовый ZIP — пакет **одной версии** и manifest; это не обязательный формат отдельного pilot/research result package. Согласие, baseline, время/качество и обезличенные исследовательские данные проверяются отдельно по [journey guide](../../web-product/docs/journey-metrics.md). Неизмеренные usage/время — unknown, не ноль; токены и исполнение каждой роли не выводить из отсутствующих полей.

## 4. Retry, reconnect, isolation и сбои

Каждый сценарий — на отдельном синтетическом проекте. Управляемые сбои только при отдельном разрешении; иначе выполнить существующий focused test и оставить live-сценарий `NOT_RUN`.

| Сценарий | Безопасная процедура и ожидаемое наблюдение |
|---|---|
| S1 Неясный ответ POST / retry | После обрыва сначала GET текущего Run, если его ID известен; повторять вручную **тот же endpoint, key и payload**, не создавать новый UUID «чтобы прошло». Start/answer/rework replay возвращает текущий snapshot прежнего ProductRun, не обязательно старую revision. Изменённое тело start/answer с прежним key → 409 `idempotency_conflict`. Новую операцию создавать только для действительно нового действия. |
| S2 Разные правила dedup | Project create распознаётся по owner+clientProjectId и возвращает прежний ID, не проверяет изменённый plan на равенство. Upload — по project+clientFileId/hash. Start/answer/approve требуют header key, но нельзя обещать универсальный actor-scoped key namespace: используйте уникальные UUID. Не переносить обещание changed-body conflict на все endpoint без проверки. |
| S3 Reconnect браузера | Кратко отключить сеть браузера на тестовом стенде, восстановить, вручную GET/refresh. Timeout/5xx — неопределённость транспорта, не `completed` и не серверный `outcome_unknown`. При `unknown_fork` обновить snapshot и заполнить актуальный вопрос; старую операцию не переотправлять как новый ответ. Проверить отсутствие дублированных сообщений/исполнений/версий, а не только зелёный экран. |
| S4 Повторное открытие | Обновить страницу того же аккаунта/браузера без очистки storage; проверить сохранённые server IDs и чтение прежних Run/версий. Cross-device server list/restore для V2 пока не реализован; `/api/projects` — legacy список и V2 проекты скрывает. Не обещать восстановление из пустого browser storage. |
| S5 Restart/reconciliation | Локально тестируется перевод прерванного linked Run в `outcome_unknown` (`backend_restarted`) без ложного успеха. Реальный restart backend/runner и transport replay — отдельная разрешённая проверка, не результат refresh браузера. Не утверждать автоматическое продолжение любого потерянного исполнения. |
| S6 Isolation | A создаёт синтетический проект, B пробует его GET, upload/start/answer/approve/ZIP и подстановку чужого artifact/source. Чужое чтение/запись должны быть отклонены без данных/receipt/нового исполнения; анонимный GET → 401, чужой project GET → 404. Проверка одного GET не закрывает всю матрицу мутаций. |
| S7 Storage / failure | Недоступные original bytes или storage не должны выдаваться за проверенное скачивание. Проверять HTTP-код, полный размер/hash, terminal status отдельно. Текст может сохраниться inline и оставаться скачиваемым даже при отказе object storage; бинарный файл без storage может быть недоступен (legacy download 413). Не требовать универсального падения Run при любом отказе S3 и не объявлять полный storage E2E по текстовому fallback. |

## 5. Локальные проверки без живой модели

Из корня репозитория, в установленном test-окружении (см. RUN_APPLICATION):

```sh
python -m pytest -q backend/tests/test_product_api.py backend/tests/test_product_api_reliability.py backend/tests/test_artifact_storage.py
```

Эти тесты используют fake model/runner/Docker и MemoryStorage; по умолчанию SQLite.
PG concurrency cases без opt-in URL пропускаются: их skip не означает PASS PostgreSQL.
`test_answers_continue_the_same_run` инжектирует вопрос: это contract test, не доказательство вопроса живой модели.
Полезные привязки: [V2 tests](../../backend/tests/test_product_api.py), [retry/material/source/reconciliation](../../backend/tests/test_product_api_reliability.py), [original bytes/ZIP/storage failures](../../backend/tests/test_artifact_storage.py).
Не запускать широкую suite, Docker builds или production-проверки ради документационного изменения.

## 6. Компактный протокол доказательств

Заполнять отдельно для **реального** прогона. Пустое/неизвестное оставлять `NOT_PROVIDED`/`NOT_RUN`, не переносить старые PASS.

```text
UTC / исполнитель / разрешённый scope:
Тип: source review | local fake tests | local smoke | live technical E2E | consented user study
Environment: local/staging/production; runtime Docker/k3s; DB/storage type:
Actual backend/frontend/runner SHA (полные): NOT_PROVIDED
Image digests / config version (без значений секретов): NOT_PROVIDED
Provenance: откуда подтверждены версии; если неизвестно — NOT_VERIFIED
Synthetic account/project/ProductRun IDs; отдельно internal Run IDs, если наблюдались:
Operation ID / question ID / artifact / document / stage / version / approval receipt ID:
Step T*/S* | expected | redacted actual | PASS/FAIL/NOT_RUN | evidence reference
Original/download bytes: size + SHA-256; ZIP document hash отдельно от archive hash:
Commands/tests actually run + exit/result/skips; mocks/fixtures and other limits:
NOT_RUN reason: нет реализации | нет предусловия | не запускалось по решению
Defect/blocker / owner / next action:
Overall: PARTIAL/FAIL/NOT_RUN; live E2E proof outstanding where applicable
```

Сохранять только разрешённые redacted outcomes, коды ошибок, счётчики и hash синтетических файлов. Не включать cookie, Authorization headers, `.env`, credentials/DSN, сырой HAR, модельные промпты с личными данными, содержимое пользовательских документов или закрытые object keys. Даже read-only export требует отдельного разрешения. Публичная публикация evidence не предполагается.

Полный E2E требует фактического React V2 → gateway → реального runner/model → durable storage → точного download/rework/approval и recovery/isolation evidence на закреплённой сборке. Он остаётся открытым до такого прогона; user study и production readiness — дополнительные, не взаимозаменяемые решения.

## Источник восстановленных сценариев

- `origin/e2e-runbook-pr11-review` @ `4aaa3cd`, `docs/2.4/e2e-real-run.md`: start/question/answer, результат, disconnect/retry/reopen/failure, synthetic evidence и разделение technical E2E/user study. Исторические «нет реализации», команды `docker kill/exec`, protocol assumptions и старые PASS сюда не перенесены.
- PR #12, `origin/codex/team-contracts-docs-2026-10-16` @ `450ec66`, `docs/contracts/{README,integration,verification,review-2026-10-16}.md`: закреплённый manifest, same public Run vs linked rework, три разных подтверждения, immutable versions/approval, isolation и ограничения fixture proof. Это provenance идей, не manifest текущего стенда и не восстановление четырёх устаревших контрактов.
