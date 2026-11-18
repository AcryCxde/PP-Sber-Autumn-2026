# PR36: необязательная диагностика и стоимость повторного чтения

## Подтверждённые дефекты

1. `diagnostic_snapshots()` использует стандартный `sqlite3`, поэтому его ошибки не входили в обработку SQLAlchemy `DBAPIError`. Ошибка чтения private transport могла скрыть уже сохранённое terminal-событие или превратить доступные детали Run в HTTP 500.
2. Polling деталей повторно материализовал все принятые diagnostic receipts и заново брал write-lock даже без изменения оценки.
3. Тесты wire импортировали repository-only `evals.fake_gateway` через добавочный `PYTHONPATH`, отсутствующий в точной команде backend CI.

## Исправление и границы

- На двух optional-границах (`runner_executor.finalize`, admin detail) отдельно перехватывается `sqlite3.Error`. Основной результат/статус не теряется; API помечает оценку `unknown/projection_unavailable`, не нулём. Посторонние ошибки программирования не получают нового широкого перехвата.
- Transport возвращает только последнее **принятое** наблюдение в единственном immutable segment данного backend Run. Между разными Run/answer segments данные не объединяются; используется прежняя source-order/lineage семантика.
- Индекс SQLite `(command_id, source_public_seq DESC)` добавляется в существующем additive bootstrap; повторное открытие старой БД сохраняет данные. PostgreSQL migrations не меняются.
- `LIMIT 1` предотвращает материализацию всей истории receipts. Это не обещание O(1): если последние source ещё не имеют receipt, запрос может просмотреть соответствующий хвост индекса.
- Read-only preflight отбрасывает неизменившиеся/более старые наблюдения без write-lock. При необходимой записи остаётся fresh-read под прежней блокировкой Run и monotonic comparison; одинаковый order с иным телом остаётся конфликтом. Конкурентный запрос может успеть взять lock, но повторную неизменившуюся запись не выполняет.
- Wire tests явно загружают fake gateway по пути файла, не меняют глобальный `sys.path`, production imports или workflow.

## Доказательства

- RED: четыре ограниченные регрессии упали — два raw SQLite detail failures, лишний write-lock неизменившейся проекции, материализация трёх receipts вместо одного. Первый общий RED с websocket ожидал потерянный terminal и был остановлен; его не считаем завершённым прогоном.
- Focused GREEN без `PYTHONPATH`: `python -m pytest backend/tests/test_pr36_projection_regressions.py backend/tests/test_harness_projection.py backend/tests/test_harness_wire.py -q --tb=short` → **19 passed**.
- Тесты включают `sqlite3.OperationalError/DatabaseError` на detail и terminal boundary, отсутствие write-lock/UPDATE при повторном/старом snapshot, конфликт тела на том же order, существующую БД до индекса и `EXPLAIN QUERY PLAN` на 500 synthetic sources без temporary sort.
- Изолированный PostgreSQL 16: `test_harness_projection.py` + `test_pr36_projection_regressions.py` → **12 passed**; monotonic delayed/concurrent writers и отсутствие повторных write-lock проверены на PostgreSQL. Transport при этом по своему контракту SQLite.
- Точный backend CI без `PYTHONPATH`: `python -m pytest backend/tests -q --tb=short` → **482 passed, 18 skipped**, 72.52 s. Пропуски — existing PostgreSQL-dependent проверки без URL.
- Точный runner CI без `PYTHONPATH`: `python -m pytest runner/tests -q --tb=short` → **445 passed**, 7.60 s.
- Перед точными CI-командами установлен актуальный `./runner` в отдельное `/tmp/coreteams-admin-plan-venv`: прежняя установленная копия была устаревшей. Lockfiles/зависимости репозитория не менялись.
- Ruff **0.17.4**, backend `--select E4,E7,E9,F`, compileall и `git diff --check` — PASS.

## Безопасность и очистка

Один собственный `postgres:16-alpine` контейнер с loopback port, ограниченными tmpfs/memory и временными credentials. После PostgreSQL-прогона test DB count = 0; полный ID/ownership label проверены перед удалением, ownership listing пуст, временные credentials удалены. Чужие контейнеры/тома не затрагивались. Production, реальные модели, секреты проекта, commit/push/deploy не использовались.

Применены existing-code, python-rules, postgres-rules и сработавшие references до изменений и по фактическому diff. `security-review`/`reliability-review` skills отсутствуют: полные checklist-аудиты не выполнены; выше перечислены конкретные проверки. SQLite additive bootstrap соответствует существующему transport, не является PostgreSQL migration.
