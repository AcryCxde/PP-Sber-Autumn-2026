# PR36: единый снимок страницы аудита AI-политики

Дата: 2026-11-12. Локальное исправление замечания P2; commit/push/deploy не выполнялись.

## Зачем

Администратор должен видеть согласованные число записей и страницу истории. Два SELECT на PostgreSQL READ COMMITTED могли вернуть `total=1` и две строки после конкурентного freeze/unfreeze; на границе страницы интерфейс ошибочно отключал переход дальше.

## Подтверждение дефекта

- `backend/api/admin.py::policy_audit` отдельно выполнял COUNT и SELECT страницы.
- SQLite RED: новый тест фиксировал два SQL-запроса к `ai_policy_audits` вместо одного: **1 failed, 2 passed, 1 skipped**.
- PostgreSQL16 RED: после выполнения первого чтения отдельный поток с отдельной Database/session атомарно выполнил настоящий `change_ai_access` (unfreeze). Текущий ответ вернул `total=1`, `len(items)=2`: **1 failed**. Использован SQLAlchemy `after_cursor_execute`, без sleep.

## Решение

Один statement: CTE с COUNT, CTE с ограниченной страницей ID, затем LEFT JOIN и явный внешний ORDER BY `created_at DESC, id DESC`. Оба CTE фильтруют один `target_id`. COUNT сохраняется даже при пустой странице и выходе за диапазон. Проверка существования User и прежний 404 остаются; DTO, приватность reason, AdminUser и `Cache-Control: no-store` не менялись.

Тесты проверяют одну audit-reading SQL-команду, страницы 1/2/3, tie-break ID при одинаковом времени, отсутствие чужого target reason, пустую историю, 404, HTTP 401/403/admin-only reason, точный DTO. PostgreSQL-регрессия подтверждает: конкурентный commit после snapshot не меняет уже выбранные count/items; следующий запрос видит новую запись.

## Проверки

Из корня репозитория:

```sh
env -u TEST_POSTGRES_URL -u PYTHONPATH /tmp/coreteams-admin-plan-venv/bin/python \
  -m pytest backend/tests/test_pr36_audit_snapshot.py backend/tests/test_admin_api.py -q --tb=short
```

**12 passed, 1 skipped**. Пропущен только тест конкурентного PostgreSQL commit.

Та же команда с отдельным временным `TEST_POSTGRES_URL` (без PYTHONPATH): **13 passed**, PostgreSQL16. После pytest `count(test_* databases)=0`.

```sh
/tmp/coreteams-admin-plan-venv/bin/ruff check \
  backend/api/admin.py backend/tests/test_pr36_audit_snapshot.py --select E4,E7,E9,F
/tmp/coreteams-admin-plan-venv/bin/python -m compileall -q \
  backend/api/admin.py backend/tests/test_pr36_audit_snapshot.py
git diff --check
```

Ruff **0.17.4**, compileall и diff-check — PASS. Собственный PostgreSQL-контейнер проверен по полному ID/label и удалён; ownership listing пуст. Временные credentials удалены. Чужие контейнеры/БД не затрагивались.

## Границы

Только `policy_audit`, новый тестовый файл и этот отчёт. Полный backend CI, браузер и production не запускались. Навыки existing-code/python-rules/postgres-rules применены; reference-routing проверен до edit и по фактическому diff. Изменение — Boundary/API без изменения контракта. Навыки security-review/reliability-review недоступны: формальные checklist-гейты не выполнялись; приведены конкретные проверки авторизации, приватности и конкурентного снимка.
