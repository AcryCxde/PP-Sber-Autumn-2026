# PR #36: конечные исходы несвязываемой диагностики

## Проверенный дефект

Источники до регистрации backend binding, usage между enqueue answer и его acceptance и поздние непринятые источники terminal Run возвращали `None` без конечного исхода. Sender повторял их каждые 30 секунд и снова после reconnect. Результат: накопление pending metadata и бесполезные транзакции SQLite.

## Исправление

- Transport сохраняет отдельный immutable negative receipt, **не accepted snapshot**. Подтверждение отказа не меняет public event/cursor/ACK и не создаёт оценку расхода.
- Отказ допустим только из durable evidence: наблюдённый source с неизменяемым `command_id=NULL`; исторический уже принятый event без diagnostic source (duplicate ingestion его не связывает); отсутствующий source у terminal transport Run, куда новый event уже не может быть принят.
- Отсутствующий source живого Run остаётся retryable: `None`, без ложного окончательного отказа.
- `rejected` ACK отправляется только после opt-in `hello.capabilities.harness_diagnostic_rejection_v1=true`. Receiver также объявляет эту capability в welcome. Старый v1 sender не получает новый статус; старый receiver работает как раньше.
- Новый sender сохраняет только terminal accepted/rejected IDs/status в отдельном SQLite-sidecar рядом с journal. Ни payload, ни стоимость, ни session ID туда не записываются. Atomic SQLite transactions; journal не изменяется. Reconnect читает IDs точечными запросами, не загружает всю историю в память.
- Feed сканируется страницами; hot pending window — не более 256 записей. При неполном последнем journal row offset не продвигается.
- ACK mailbox принимает только IDs действительно отправленных незавершённых сообщений. Late duplicate/unsolicited ACK не может заполнить mailbox и вытеснить ACK следующей диагностики.
- Ошибка открытия/повреждение optional sidecar выключает private sender на этом соединении с безопасной диагностикой, **не останавливает legacy delivery**, не трактуется как принятие.

## Границы гарантии

Definitively rejected история удаляется из горячего окна и не пересылается после reconnect, поэтому backlog более 256 таких сообщений не мешает позднему валидному snapshot. Временно отсутствующие sources не теряются и продолжают retry. **256 одновременно неразрешённых transient sources могут задержать более поздние diagnostics**: здесь не реализована новая durable retry queue или гарантия полной fairness. Сброс sidecar оператором приведёт к повторному чтению journal и идемпотентным receiver receipts, не к повторному учёту стоимости.

## Проверки

- RED исходных трёх сценариев definitive rejection: 3 failed, 10 passed до исправления.
- Регрессии: gap/prebinding/historical source; terminal vs живой source; durable rejection после reopen и canonical conflict; backlog 300 rejection + поздний accepted; reconnect; actual pinned old sender/receiver; capability opt-in; потеря ACK; corrupt sidecar при успешной legacy delivery; unsolicited/256 late duplicate ACK и следующий валидный ACK.
- Проверки CI PR #36 до исправлений: `python` упал на F401 в `test_harness_diagnostics.py` (`json`) и `test_harness_projection.py` (`select`), зависимый `gate` тоже failed; build/frontend/web-product/compose/schema-postgres/classify прошли.
- Текущая локальная ветка содержит прежний CI workflow, а remote master/PR merge workflow уже строже. Прочитаны реальные remote шаги: `ruff check backend --select E4,E7,E9,F` и из runner `ruff check src tests evals`. Они выявили также три неиспользуемых импорта; с разрешения координатора адресно удалены `select` в `backend/api/projects.py`, `MATERIALS_DIR` в `backend/runner_executor.py`, `RunnerBridge` в `backend/tests/test_runner_app.py`. Workflow не изменялся. Оба lint шага прошли также на **точной CI-версии Ruff 0.17.4**, установленной только в disposable `/tmp` test venv; lockfiles не менялись.
- Targeted diagnostic/gateway suite: **46 passed**. Первый полный canonical offline suite: **914 passed, 18 skipped, 1 failed** — существующий exact hello fixture не ожидал новую opt-in capability; исправлено только ожидаемое тело handshake, не production logic. Финальный canonical повтор `PYTHONPATH=runner/src:runner /tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests runner/tests -m 'not e2e' -q --tb=short` прошёл: **915 passed, 18 skipped за 80.65 s**. Пропуски — существующие PostgreSQL-URL gates; реальные модельные e2e исключены. Результаты разных прогонов не суммируются.
- Тест opt-in сначала зависал из-за ошибки самой фикстуры: первый connection ACK удалял команду, которую второй connection затем ждал. Исправлена фикстура, live WebSocket regression отдельно прошёл. Прерванные прогоны не объявлены успешными.

## Ограничения

`security-review` и `reliability-review` skills отсутствуют, полные checklist gates не выполнялись. Применены existing-code/python-rules/postgres-rules и сработавшие reference routing; transport/sidecar используют SQLite, production PostgreSQL DDL не менялся. Модели, production, контейнеры, commit/push и внешняя публикация логов не использовались.
