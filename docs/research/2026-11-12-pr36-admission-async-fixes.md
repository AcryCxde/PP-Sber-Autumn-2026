# PR36: отмена admission и неблокирующее чтение диагностики

Дата: 2026-11-12. Исправления шестого раунда ревью; не разрешение на rollout.

## Подтверждённые проблемы

1. Отмена во время admission оставляла принятый Run в pending либо running без исполнителя. RED: два сценария cancellation возвращали эти состояния вместо терминального исхода.
2. `diagnostic_snapshots()` вызывался прямо из event loop в detail и runner finalize. RED проверка обнаружила выполнение SQLite-чтения в потоке приложения.

Первый focused RED: **3 failed, 1 passed**.

## Решение

- Admission выполняется в явно удерживаемой задаче с cooperative deadline 30 секунд. Отмена владельца не отделяет эту задачу: сначала разрешается результат допуска, затем отмена распространяется.
- Результат успешного commit фиксируется в локальной переменной до закрытия session context. Поэтому deadline при cleanup после commit не теряет доказательство владения Run.
- Только результат `ADMITTED` позволяет финализировать принадлежащий этой попытке запуск как `outcome_unknown`; `ALREADY_ADMITTED` другого исполнителя не затрагивается.
- Ошибка admission до получения допуска использует прежний атомарный pending-only guard. Финализация сохраняет bounded owned-task поведение при повторной отмене.
- Оба optional diagnostic boundary вызывают `await asyncio.to_thread(transport.diagnostic_snapshots, run_id)`. SQLite-соединение создаётся и закрывается внутри worker; SQLAlchemy session и async projection не передаются в thread.
- Прежняя изоляция `sqlite3.Error` сохраняется: основная detail/terminal доставка не зависит от доступности необязательной диагностики.

## Проверки

Новый `backend/tests/test_pr36_admission_async.py`: **8 passed**. Покрыты отмена до admission, во время commit и после commit; сохранность запуска другого владельца; deadline после настоящего commit при задержанном session cleanup; heartbeat при реальном `BEGIN EXCLUSIVE` на SQLite; отдельный поток чтения диагностики перед выдачей terminal через FakeRunner.

Смежный scoped набор до добавления двух post-commit timeout случаев: **59 passed**. Независимая перепроверка нового модуля: координатор подтвердил **8 passed**, замечаний нет.

Точная команда общего backend gate:

```sh
env -u PYTHONPATH -u TEST_POSTGRES_URL \
  /tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests -q --tb=short
```

Полный gate: **542 passed, 20 skipped, 83.14 s**, exit 0. Пропуски относятся к существующим отдельным PostgreSQL/test-environment проверкам; они не выдаются за выполненные этим offline-прогоном. Ruff 0.17.4: backend `--select E4,E7,E9,F` и строгий runner `src tests evals` — PASS; compileall и `git diff --check` — PASS.

## Ограничения

- Cooperative timeout не является жёсткой wall-clock гарантией при некооперативном драйвере.
- Реально неизвестный результат COMMIT внутри драйвера нельзя признать владением по одному running-статусу: новый код не финализирует чужой запуск предположением. Восстановление после такой неопределённости остаётся задачей существующего reconciliation.
- Отмена `to_thread` не останавливает начатое SQLite-чтение; оно ограничено существующим sqlite timeout и не блокирует asyncio loop.
- PostgreSQL отдельно в этом пакете не запускался: SQL/DDL/порядок блокировок не менялись. Проверки ownership и блокировки чтения выполнены на SQLite.
- `existing-code`, `python-rules`, `postgres-rules` и fired references применены. Skills `security-review` и `reliability-review` недоступны: полные checklist-аудиты не выполнялись. Это конкретные security/reliability notes, не аудит.
- Реальные модели, production, контейнеры, commit и push не использовались.
