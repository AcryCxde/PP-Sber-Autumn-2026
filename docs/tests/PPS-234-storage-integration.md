# PPS-234 — storage integration probe

## Область и ограничения

Probe восстановлен из #22 (`origin/test/pps-234-storage-integration@69b678a`) и адаптирован к базе `133e8db` после #26. Проверяется legacy-связь `Project → ProjectRun → Run → Event/Artifact` реальными функциями `backend.projects` и `backend.repository`, без mock и изменений runtime.

`ProjectRun` — связь отдельного запуска Claude с этапом проекта. Это не `ProductRun`: последний представляет V2-цепочку исходного запуска и продолжений. Probe создаёт legacy-связь напрямую, с `product_run_id=None`; V2 API, маршрутизация и создание связи через HTTP здесь не проверяются.

| Сценарий | Проверяемый контракт |
|---|---|
| Связь и running | Один stage-run, совпадение project/run/conversation, persisted `running` → `working` |
| Пауза | `completed` + `question.asked` → `waiting`, с публичной проекцией вопроса; отдельный persisted статус `waiting_for_input` не утверждается |
| Ошибка | persisted `failed`, `error_code`, событие `run.failed` → `failed` |
| Результат | persisted `completed`, текст `Done`, отсутствие вопроса → `review` |
| Идемпотентность | Повтор `accept_run` с тем же `client_request_id` возвращает тот же Run, строка Run остаётся одна |
| Replay | Повторное чтение сравнивает `(seq, type, payload)`, seq упорядочены и уникальны; `after_seq` возвращает строгий суффикс |
| Terminal guard | Поздний append отвергается, повторный running не выполняется; статус и поток событий не изменяются |
| Полный текстовый snapshot | В новой сессии проверяются path, content, UTF-8 size, truncated=False и **сохранённый `Artifact.sha256`**, совпадающий с пересчитанным hash |

Локальный `append_artifact` сохраняет snapshot и `artifact.saved`. Это **не** доказательство durable runner confirmation, записи файла runner или подтверждённой доставки. Confirmation lifecycle отдельно покрывают `test_artifact_confirmations.py`, `test_repository_confirmation_recovery.py` и `test_pr20_delivery_lifecycle.py`; здесь он не дублируется. Конкурентные accept/replay и браузерный reconnect также вне области.

## Изоляция

По умолчанию используется SQLite в `tmp_path`. При `TEST_POSTGRES_URL` helper `backend/tests/databases.py` создаёт отдельную случайно именованную тестовую БД; `conftest.py` удаляет созданные БД при завершении pytest. Fixture освобождает engine в `finally`, включая сбой создания схемы. Все данные синтетические, email в `example.invalid`; тест не печатает credentials, содержимое окружения или сгенерированные evidence IDs и не вызывает runner, object storage или production.

## Проверка 2026-11-11

Команды выполнены из корня worktree на Python 3.12 (`/tmp/pps-pr20-venv/bin/python`). Ниже результаты новых запусков, не исторические результаты #22.

SQLite:

```sh
env -u TEST_POSTGRES_URL /tmp/pps-pr20-venv/bin/python -m pytest backend/tests/test_pps234_storage_integration.py -q -o asyncio_default_fixture_loop_scope=function
# 1 passed
env -u TEST_POSTGRES_URL /tmp/pps-pr20-venv/bin/python -m pytest backend/tests/test_repository.py backend/tests/test_projects_api.py backend/tests/test_pps234_storage_integration.py -q -o asyncio_default_fixture_loop_scope=function
# 21 passed
```

PostgreSQL: только выделенный disposable контейнер `pps-pr25-product-pg2`, PostgreSQL 16.15, `127.0.0.1:56946`, tmpfs 1 GiB. Перед запуском проверены порт, состояние и список БД; существовала только `postgres` (кроме templates). URL ниже не содержит пароля и относится только к этому локальному контейнеру.

```sh
TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:56946/postgres /tmp/pps-pr20-venv/bin/python -m pytest backend/tests/test_pps234_storage_integration.py -q -o asyncio_default_fixture_loop_scope=function
# 1 passed
TEST_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:56946/postgres /tmp/pps-pr20-venv/bin/python -m pytest backend/tests/test_repository.py backend/tests/test_projects_api.py backend/tests/test_pps234_storage_integration.py -q -o asyncio_default_fixture_loop_scope=function
# 21 passed
```

Запуски последовательные; после PostgreSQL-проверок количество БД с именами `test_%` равно 0.

```sh
/tmp/pps-pr20-venv/bin/ruff check backend/tests/test_pps234_storage_integration.py
/tmp/pps-pr20-venv/bin/ruff format --check backend/tests/test_pps234_storage_integration.py
/tmp/pps-pr20-venv/bin/mypy --follow-imports=silent backend/tests/test_pps234_storage_integration.py
```

Статические проверки относятся только к probe, не ко всему backend.

## Review notes

`security-review` и `reliability-review` skills недоступны; формальные gates не запускались. Вручную проверены изоляция и cleanup тестовых БД, реальные production-вызовы, идемпотентность, terminal guards и отсутствие вывода приватных данных. Проверка не является аудитом безопасности всего приложения или доказательством конкурентной корректности.
