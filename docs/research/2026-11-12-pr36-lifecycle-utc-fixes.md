# PR #36: исход запуска при сбое публикации и UTC timestamps

Дата: 2026-11-12. Исправления третьего ревью, без включения функций, commit/push и deployment.

## Подтверждённые проблемы

1. После committed admission исключение или отмена в доставке `context.reset` / `run.started` оставляли Run в `running`, хотя executor ещё не был вызван.
2. SQLite возвращает naive datetime; users/audit и mutation receipt сериализовали их без зоны, поэтому браузер мог интерпретировать UTC как локальное время.

RED: новый `backend/tests/test_pr36_lifecycle_utc.py` первоначально дал **5 failed**: четыре сценария оставили `running`, timestamps имели `utcoffset() is None`.

## Исправления

- `backend/main.py`: publication admission-событий защищена отдельно до любого CLI/runner вызова. Владелец только что допущенного Run сохраняет `failed/claude_request_failed` при ошибке либо `outcome_unknown/heartbeat_lost` при отмене, затем исходная ошибка/отмена распространяется наружу. Повторный исполнитель без нового допуска не финализирует чужую работу.
- Финализация не зависит от успешной повторной доставки подписчику: terminal сохранён в БД и доступен replay. Потерявший соединение подписчик не получает ложного подтверждения доставки.
- Повторная отмена task не отделяет незавершённую финализацию: явный task остаётся под владением вызывающей операции до результата ограниченной попытки. Внутренняя попытка имеет **cooperative timeout 10 секунд**, а не строгую гарантию wall-clock: отмена зависит от поведения DB-драйвера и cleanup. Timeout/ошибка БД не скрываются; traceback сохраняет исходную ошибку публикации в `__context__`. Если БД не позволяет сохранить terminal, не утверждаем, что durable outcome гарантирован; прежний recovery остаётся необходим.
- `backend/timestamps.py`: `utc_iso()` трактует naive persistence datetime как UTC, aware значения приводит к UTC. Users, audit и новый mutation receipt используют этот helper. Идемпотентный replay возвращает прежний сохранённый receipt побайтово по смыслу JSON; исторические receipts не переписываются.

## Проверки

Без `PYTHONPATH`:

```sh
env -u PYTHONPATH /tmp/coreteams-admin-plan-venv/bin/python -m pytest \
 backend/tests/test_pr36_lifecycle_utc.py backend/tests/test_reset_admission.py \
 backend/tests/test_admin_api.py backend/tests/test_history_api.py \
 backend/tests/test_ai_access_ingress.py backend/tests/test_runner_executor.py -q --tb=short
```

**91 passed**, 28.58 s. Проверены оба executor mode до model I/O, исключения и отмены на обоих admission-событиях, настоящий `task.cancel()`, повторная отмена при заблокированной финализации, deadline и ошибка БД с сохранённой причиной, exact terminal count/replay, отсутствие активного Run-блокера, UTC users/audit/receipt и UTC+3 отображение.

Дополнительный дефект повторной отмены был подтверждён отдельным RED: родительский task завершался до terminal commit. После исправления barrier-регрессия проходит. Промежуточные GREEN-ошибки вызова `finalize_run` и неверного ожидания resume после reset исправлены; финальные результаты относятся к актуальному коду.

На собственном изолированном `postgres:16-alpine`:

```sh
TEST_POSTGRES_URL=<temporary isolated URL> env -u PYTHONPATH \
 /tmp/coreteams-admin-plan-venv/bin/python -m pytest \
 backend/tests/test_pr36_lifecycle_utc.py backend/tests/test_reset_admission.py -q --tb=short
```

**19 passed**, 5.15 s. После pytest `count(test_* databases) = 0`; контейнер удалён только после проверки полного ID и собственной label. Чужие контейнеры и volumes не затрагивались; credentials в файлы репозитория не записывались.

Ruff **0.17.4**, `--select E4,E7,E9,F` для затронутых Python-файлов, compileall и `git diff --check` — PASS. Общий backend CI-прогон не запускался поверх параллельных изменений runner.

## Правила и границы доказательств

Изменение класса Boundary: API timestamps + durable lifecycle/event delivery. Прочитаны определения и соседние пути `main.execute_accepted_run`, `ai_access.change_ai_access`, admin users/audit, repository finalization через тесты, существующие reset/admin/history/runner fixtures. Выполнены applicable routing references existing-code, Python main/developer/idioms/errors-common/legacy-errors/testing/sqlalchemy, PostgreSQL deployment; production SQL/DDL и порядок блокировок не изменялись, поэтому migrations/locking reference не срабатывают.

`security-review` и `reliability-review` skills отсутствуют: соответствующие checklist gates не выполнялись, полный аудит не заявляется. Конкретные privacy/auth и lifecycle/cancellation/UTC проверки выполнены указанными тестами; реальные модели, production и аварийное выключение процесса не использовались.
