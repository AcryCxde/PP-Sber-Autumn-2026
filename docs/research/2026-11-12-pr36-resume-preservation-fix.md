# PR36: сохранение resume-контекста до executor

Дата: 2026-11-12. Исправление P2 повторного ревью коммита `6cd6658`.

## Подтверждённый дефект

Ошибка/отмена доставки admission-событий происходила до вызова executor, однако `store_publication_failure` вызывал общий `finalize_run` с `claude_request_failed` либо `outcome_unknown`. Общая финализация очищала исправную `Conversation.claude_session_id` и переводила контекст в `unavailable`, хотя модель не запускалась.

Начальный regression-прогон: **5 failed, 2 passed**. Четыре отказа воспроизвели потери resume-сессии при exception/cancellation для local/runner; пятый — перезапись уже изменённого контекста.

## Исправление и границы

- `backend/repository.py`: введён внутренний `ExecutionPhase`; существующие вызовы по умолчанию остаются `EXECUTOR_STARTED` и сохраняют прежнюю консервативную инвалидизацию контекста.
- Только `backend/main.py::store_publication_failure`, выполняемый до передачи управления local/runner executor, задаёт `BEFORE_EXECUTOR`.
- Под существующими блокировками conversation → Run финализация такого **resume** оставляет текущий контекст нетронутым. Старое захваченное значение не восстанавливается поверх нового состояния.
- Терминальный статус, error code, событие и снятие блокировки активного запуска сохраняются. Неопределённость доставки результата не превращается в успех запуска.
- Reset обрабатывается отдельно: уже закоммиченный сброс не откатывается, прежняя сессия не возвращается.
- Ошибка после реального executor handoff использует прежнюю финализацию и инвалидирует потенциально изменённый контекст.
- Фаза — аргумент внутреннего repository API, не поле HTTP/WS-запроса; внешние DTO и протокол не менялись. Admission ownership и защита от повторного исполнителя не менялись.

## Проверки

Новые регрессии: `backend/tests/test_pr36_resume_preservation.py`, **10 случаев**:

1. Exception/cancellation публикации до local/runner executor сохраняют сессию, один terminal и возможность нового resume.
2. Сбой публикации reset не восстанавливает старую сессию.
3. После передачи управления executor обычная AI-ошибка по-прежнему инвалидирует контекст.
4. Предварительно изменённый текущий контекст не перезаписывается прежним session ID.
5. Отмена после commit admission и timeout post-commit cleanup сохраняют нетронутую resume-сессию.
6. Duplicate не финализирует запуск другого владельца.

SQLite / смежные контракты:

```sh
env -u PYTHONPATH -u TEST_POSTGRES_URL /tmp/coreteams-admin-plan-venv/bin/python -m pytest \
  backend/tests/test_pr36_resume_preservation.py \
  backend/tests/test_pr36_lifecycle_utc.py backend/tests/test_pr36_admission_async.py \
  backend/tests/test_reset_admission.py backend/tests/test_repository.py \
  backend/tests/test_repository_confirmation_recovery.py -q --tb=short
```

Результат: **54 passed, 3 skipped**. Пропуски — существующие PostgreSQL-only сценарии без `TEST_POSTGRES_URL`.

PostgreSQL 16, отдельный одноразовый контейнер, временные credentials, loopback-only порт:

```sh
# TEST_POSTGRES_URL передан только subprocess, credentials не сохраняются в документе.
env -u PYTHONPATH /tmp/coreteams-admin-plan-venv/bin/python -m pytest \
  backend/tests/test_pr36_resume_preservation.py -q --tb=short
```

Результат: **10 passed**, 2.41 s. После pytest `test_*` databases = **0**. Контейнер удалён после проверки полного ID и ownership label; чужие ресурсы не затрагивались.

Ruff backend `--select E4,E7,E9,F`, compileall изменённых Python-файлов и scoped `git diff --check` — PASS.

## Ограничения

После стабилизации runner выполнен общий backend gate: свежий `pip install ./runner`, затем `env -u PYTHONPATH -u TEST_POSTGRES_URL /tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests -q --tb=short`. Результат: **552 passed, 20 skipped**, 85.75 s, exit 0. Пропуски относятся к существующим optional PostgreSQL/bridge проверкам без их test URL; новая resume-матрица отдельно прошла PostgreSQL16 10/10. Независимый reviewer подтвердил исправление статически; его окружение не содержало SQLAlchemy, поэтому runtime-evidence автора не выдаётся за проверку reviewer. Реальные модели, deployment, commit/push не выполнялись. Неопределённый исход самого DB COMMIT не превращается в доказанное владение; существующий reconciliation-контракт сохранён.

Применены existing-code, Python и PostgreSQL rules с соответствующими references до изменений и по фактическому diff. Навыки `security-review` / `reliability-review` недоступны: формальные checklist-аудиты не выполнялись. Security/reliability notes: внешний клиент не управляет execution phase; изменения касаются только pre-executor контекста и проверены на SQLite и PostgreSQL.
