# PR36: acceptance delivery и запись диагностики

Дата: 2026-11-12. Исправления относительно `f945365`; не разрешение deployment или включения функций.

## Подтверждённые проблемы

1. `handle_run_create` публиковал `run.accepted` после commit, но до `try/finally`, защищающего исполнение. `OSError`/`TimeoutError` отправки оставляли pending Run без executor и callback в `live_run_deliveries`.
2. `RunnerBridge._receive` непосредственно вызывал синхронный `transport.diagnostic`. Его SQLite write-lock мог блокировать общий asyncio loop до истечения SQLite timeout.

Первый RED запуск показал два целевых socket-failure и один целевой thread-boundary failure; четвёртое падение было ошибкой тестовой записи Conversation без обязательного title. Ошибка fixture исправлена отдельно, не выдана за дефект продукта.

## Решение

- Доставка в WebSocket после durable acceptance факультативна: `OSError`, включая `TimeoutError`, помечает подписчика недоступным, но работа выполняется. Сохранённые события доступны через replay.
- Публикация acceptance/reset и handoff executor входят в общий `try/finally`: зарегистрированный callback удаляется при всех исходах.
- Отмена или неожиданная ошибка до handoff завершает только ещё pending, не допущенный Run через существующий атомарный guard. Cleanup — собственная задача с cooperative timeout 10 секунд; повторная отмена не отделяет её от родителя. Idempotent replay не получает право завершать чужой Run.
- Диагностическая запись исполняется `asyncio.to_thread`; SQLite connection создаётся/закрывается самим transport в worker. Один WS по-прежнему обрабатывает кадры последовательно. ACK отправляется только после возврата durable receipt.
- При отмене ожидания worker может завершить уже начатую транзакцию; ACK не отправляется. Повтор того же immutable ID получает сохранённый receipt. Ошибка записи не создаёт успешного ACK.

## Проверки

```sh
env -u PYTHONPATH -u TEST_POSTGRES_URL \
  /tmp/coreteams-admin-plan-venv/bin/python -m pytest \
  backend/tests/test_pr36_acceptance_ingest.py \
  backend/tests/test_pr20_delivery_lifecycle.py \
  backend/tests/test_pr36_admission_async.py \
  backend/tests/test_pr36_resume_preservation.py \
  backend/tests/test_runner_app.py backend/tests/test_harness_wire.py -q --tb=short
```

**49 passed, 8.49 s.** Новый файл содержит 9 cases: socket OSError/Timeout; однократная/повторная отмена acceptance с сохранением resume; отмена replay чужого admitted Run; unexpected publication failure; настоящая SQLite write-lock с продолжающимся loop heartbeat и отсутствием ACK до commit; отмена ожидания записи с последующим replay receipt; SQLite failure без ACK.

Ruff **0.17.4**, `ruff check backend --select E4,E7,E9,F` и `git diff --check` — PASS.

Финальный общий backend CI после свежего `pip install ./runner` из checkout:

```sh
env -u PYTHONPATH -u TEST_POSTGRES_URL \
  /tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests -q --tb=short
```

**561 passed, 20 skipped, 81.53 s; exit 0.** Пропуски включают PostgreSQL-only проверки без test URL; данный прогон не является PostgreSQL-проверкой. Runner suite здесь повторно не запускался. При промежуточной строгой проверке runner найдены два I001 порядка новых импортов; координатор уведомлён, исправление выполнено автором runner. Финальная строгая проверка `ruff check runner/src runner/tests runner/evals` — PASS; чужие файлы этим исполнителем не менялись.

## Ограничения и безопасность

- Не заявляется глобальная неблокируемость всех transport методов: соседние `expire`, публичный ingest и прочие существующие пути не менялись.
- Cooperative timeout не является жёстким wall-clock пределом. Недоступная БД/неопределённый COMMIT по-прежнему требуют предусмотренного reconciliation; код не выводит владение из статуса Run.
- Тесты SQLite, не доказательство всех PostgreSQL interleavings. Репозиторный pending-only guard не изменялся.
- Реальные модели, production, секреты, контейнеры, commit/push не использовались.
- Применены existing-code, Python и PostgreSQL rules и сработавшие references. Skills `security-review`/`reliability-review` недоступны: формальные checklist-аудиты не выполнялись; перечисленные конкретные проверки выполнены.
