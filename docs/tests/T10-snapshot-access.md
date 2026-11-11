# T10 — доступ только к выбранному неизменяемому снимку

- Задача: `PM-SPRINT-2026-10-16-T10` (`PPS-209`, S4 T10)
- Канон: Wiki `PPS-161`, §3, T10
- Implementation parents: `PPS-167`, `PPS-136`, `PPS-129`
- Дата локальной проверки: 2026-11-08
- Ветка: `test/t10-snapshot-access`
- `source_sha`: `e51cc62fe4acddf0acb862b9a44055d62556df69`
- `harness_sha`: `4bba990a977457f2f2e40a9b323b6f2d288b1c4c` (закреплён входным заданием; этот объект отсутствует в project Git object database и не использовался как исходный код теста)

## Итог

**Внешняя публикация: `NOT_IMPLEMENTED`.** В текущем приложении нет сущностей snapshot grant/publication, API выдачи выбранного снимка отдельной identity, push/publish-потока либо общей подсистемы backup/restore. Поля `Artifact.commit_sha`, `Artifact.git_tree_url` и `Project.git_repo_url` существуют как nullable metadata, но не образуют контракт внешней публикации.

**Вердикт T10: `inconclusive` для продукта, `supported` для проверенного storage-level решения.** Существующие механизмы подтверждают неизменяемость Git-снимка, owner isolation, terminal guard, idempotent operation ID и durable artifact receipt. Добавленный изолированный синтетический SQLite/filesystem probe подтверждает предлагаемую модель snapshot ACL, отзыв/удаление и восстановление прежней ACL. Он не доказывает отсутствующий пользовательский publication flow и не заменяет PostgreSQL RLS-проверку.

Инженерное решение: при реализации публикации хранить отдельный immutable snapshot, привязанный к конкретным `project_id` и terminal `run_id`, и проверять отдельную snapshot ACL. Никогда не разрешать snapshot identity читать строки live `Project`, `Run`, `Conversation` или mutable workspace. Слияние не выполнялось и не предлагается автоматически.

## Конфигурация и границы

| Параметр | Значение |
|---|---|
| Платформа | Windows 11, локальный checkout |
| Python теста | CPython 3.12.15 через `uv` |
| Проверяемое хранилище probe | локальный SQLite + локальная файловая система во временном каталоге pytest |
| Данные | только synthetic IDs и synthetic content |
| Внешняя сеть / публикация / deploy | не использовались |
| Клиентские данные, сырые секреты, контакты | отсутствуют |
| Письма, приглашения, публичные ссылки | не создавались |
| Пользовательский E2E | не запускался |
| PostgreSQL / RLS | `NOT_RUN`; production policy должна дублироваться FK/ACL/RLS и проверяться отдельно |

Первый прямой запуск `python -m pytest backend/tests/test_snapshot_access.py -q -s` завершился до collection со статусом `FAIL`: в системном Python отсутствовал `sqlalchemy`, который импортирует общий `backend/tests/conftest.py`. После этого зависимости установлены из закреплённого проекта командой `uv run --extra test`; сам probe и выбранная backend-матрица прошли.

## Источник фактов в коде

- Project↔Run: `backend/models.py` (`Project.conversation_id`, `ProjectRun`) и `backend/projects.py` (`stage_runs`, версии результата).
- Авторизация: `backend/auth.py`, owner-filtered `backend/api/projects.py` и `backend/api/history.py`; negative matrix — `backend/tests/test_ownership_isolation.py` и `backend/tests/test_projects_api.py`.
- Operation ID / replay: unique `Run.client_request_id` и idempotent acceptance в `backend/repository.py`.
- Terminal guard: `_append_event_in_transaction` в `backend/repository.py` и terminal checks в `backend/runner_transport.py`.
- Immutable selected bytes: Git commit SHA и чтение фактов из commit object в `runner/src/ctrunner/checkpoint.py`; working-tree mutation test — `runner/tests/test_checkpoint_facts.py`.
- Durable receipt: artifact receipt validation и запрет completion до receipts в `backend/runner_transport.py`; persisted artifact download snapshot — `backend/api/history.py`.
- Backup/restore: общей реализации нет. `backend/transfer_sqlite.py` — только однонаправленный offline transfer SQLite→empty PostgreSQL, не snapshot backup contract.
- Новый reference probe: `backend/tests/test_snapshot_access.py`.

## Синтетическое доказательство

Probe создаёт synthetic project и completed Run, сериализует выбранные данные в отдельный файл, записывает hash/size/ACL/receipt в SQLite, закрывает соединение и перечитывает receipt через новое соединение. После этого он меняет live Project и Run, проверяет прежние bytes/hash снимка, выполняет negative access checks, отзывает grant и удаляет снимок. Backup, снятый до отзыва, восстанавливается в отдельные пути; восстановленная ACL разрешает только прежней test identity доступ к снимку и по-прежнему запрещает ей live Project/Run.

Реальные значения завершённого прогона:

| Факт | Значение |
|---|---|
| `project_id` | `project-t10-synthetic-001` |
| `run_id` | `run-t10-synthetic-001` |
| `snapshot_id` | `snapshot-t10-synthetic-001` |
| owner identity | `identity-t10-owner-001` |
| granted identity | `identity-t10-viewer-001` |
| ungranted identity | `identity-t10-other-001` |
| operation ID | `op-t10-save-001` |
| snapshot path | `snapshots/snapshot-t10-synthetic-001/selected.json` (внутри pytest `tmp_path`; временный абсолютный путь намеренно не считается durable evidence) |
| size | `149` bytes |
| SHA-256 | `844b66f68282738682738b7d6ef2608d926426f1250986e50e45d83b9546409d` |
| terminal guard | `runs.status == completed` до snapshot save |
| durable receipt | `state=committed`; `operation_id`, hash и size перечитаны через новое SQLite connection |
| backup policy | ACL и snapshot metadata сохранены в backup до revoke/delete и восстановлены без расширения доступа |

Negative access checks:

| Проверка | Результат |
|---|---|
| ungranted identity → snapshot | `DENY` |
| granted snapshot identity → live Project | `DENY` |
| granted snapshot identity → live Run | `DENY` |
| granted identity после revoke/delete → snapshot | `DENY` |
| restored ungranted identity → snapshot | `DENY` |
| restored granted snapshot identity → live Project | `DENY` |
| restored granted snapshot identity → live Run | `DENY` |
| working Project/Run изменены после save → snapshot bytes/hash | `UNCHANGED` |

## Точные команды и статусы

```powershell
# Первичная попытка, системный Python без test dependencies
python -m pytest backend/tests/test_snapshot_access.py -q -s
# FAIL: ModuleNotFoundError: No module named 'sqlalchemy'

# Сам synthetic probe с выводом evidence
uv run --extra test python -m pytest backend/tests/test_snapshot_access.py -q -s
# PASS: 1 passed in 0.95s

# Backend: project↔Run, save/replay, authorization, transport receipts, history snapshot и T10 probe
uv run --extra test python -m pytest backend/tests/test_projects_api.py backend/tests/test_repository.py backend/tests/test_ownership_isolation.py backend/tests/test_runner_transport.py backend/tests/test_history_api.py backend/tests/test_snapshot_access.py -q
# PASS: 77 passed, 1 warning in 41.36s

# Переносимый набор Git checkpoint + immutable commit facts
uv run --project runner python -m pytest runner/tests/test_checkpoint.py runner/tests/test_checkpoint_facts.py::test_facts_are_taken_from_commit_not_from_working_tree -q
# PASS: 8 passed in 1.53s

# Более широкая runner-матрица была запущена на Windows
uv run --project runner python -m pytest runner/tests/test_guard.py runner/tests/test_checkpoint.py runner/tests/test_checkpoint_facts.py runner/tests/test_recovery.py runner/tests/test_main_recovery.py runner/tests/test_v0.py -q
# FAIL: 23 failed, 73 passed; platform/environment failures:
# POSIX /workspace path assertions executed with WindowsPath separators,
# Windows forbids a filename containing LF used by one test,
# default cp1251 decoding failed on UTF-8 journal fixtures.
# Это не противоречит portable checkpoint subset, но полный runner suite требует Linux/UTF-8.

# PostgreSQL transfer/RLS boundary
$env:TEST_POSTGRES_URL = 'postgresql+asyncpg://<redacted-local-test-dsn>'
uv run --extra test python -m pytest backend/tests/test_transfer_sqlite.py -q
# NOT_RUN: тестовый PostgreSQL не предоставлен; строка выше — шаблон, не настоящий секрет.
```

Warnings успешного backend-прогона: pytest-asyncio сообщает о будущем изменении fixture loop scope; Starlette сообщает о deprecated `httpx` TestClient compatibility path. На результат T10 probe они не влияют.

## H-A-D-I

### H — Hypothesis

T10: публикация открывает только выбранный неизменяемый снимок, а не живой Project, его Conversation/Run или mutable workspace.

### A — Action

Создан и запущен `backend/tests/test_snapshot_access.py`: synthetic Project + terminal Run → канонический JSON snapshot → SHA-256/size → grant одной identity → durable receipt после commit/reopen → mutation live state → отрицательные проверки → revoke/delete → controlled restore прежнего backup с прежней ACL.

### D — Data

Наблюдались:

- committed receipt `op-t10-save-001` для `snapshot-t10-synthetic-001`;
- snapshot `149` bytes, SHA-256 `844b66f68282738682738b7d6ef2608d926426f1250986e50e45d83b9546409d`;
- неизменные bytes/hash после изменения live Project/Run;
- семь явных `DENY` для ungranted/live/revoked/restored-negative путей;
- `PRESERVED` для backup restore ACL;
- существующие Git checkpoint facts читаются из commit, а не working tree;
- product publication API/event: **явный пропуск, `NOT_IMPLEMENTED`**;
- PostgreSQL/RLS: **явный пропуск, `NOT_RUN`**.

### I — Interpretation

До прогона: `NOT_RUN`.

После прогона:

- Storage-level reference contract: **`supported`**.
- Полная продуктовая гипотеза T10: **`inconclusive`**, потому что publication/grant/revoke/restore API и production enforcement отсутствуют.
- Противоречий модели «доступ только к immutable selected snapshot» не обнаружено.
- Рекомендуемая реализация: content-addressed or commit-addressed snapshot object + отдельная snapshot ACL + terminal-run precondition + unique operation ID + durable receipt after transactional commit + deny-by-default live Project/Run paths + PostgreSQL FK/RLS tests + backup restore policy verification.
- Решение о merge: только инженерное ревью и отдельный явный OK merge-owner; автоматический merge запрещён.
