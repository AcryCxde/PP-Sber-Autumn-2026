# PR36: исправления backend по ревью

## Область

Исправлены три замечания ревью; остальные изменения PR выполняются отдельными исполнителями. Коммиты, push, деплой и обращения к моделям не выполнялись.

1. **Внешний authority nginx.** HTTP и WebSocket proxy передают `$http_host`, сохраняя внешний порт. Проверка Origin backend не ослаблена.
2. **Ошибка допуска повторного исполнителя.** `fail_unadmitted_run` блокирует контекст и Run, перечитывает состояние и завершает только `pending` без `admitted_at`. Уже допущенная работа не получает ложный terminal event. На SQLite блокировка записи берётся до чтения (`BEGIN IMMEDIATE`); на PostgreSQL используются существующие блокировки строк в порядке Conversation → Run.
3. **Необязательная диагностическая проекция.** Ошибки DBAPI и известные ошибки lineage/snapshot не лишают администратора основной информации о запуске: оценка возвращается как unknown с причиной `projection_unavailable`. Проекция использует отдельную сессию, которая закрывает неуспешную транзакцию. Ошибки самого чтения запуска и неизвестные ValueError не подавляются.

Дополнительно удалён неиспользуемый импорт `sqlalchemy.select` из `test_harness_projection.py`, отмеченный CI.

## RED → GREEN

Новый `backend/tests/test_pr36_backend_regressions.py` до исправления: **4 failed, 1 passed**. Падения непосредственно воспроизвели ложное завершение running Run, обе ошибки проекции и отсутствие сохранения порта в nginx. Проверка Host/Origin на ASGI-границе уже проходила: дефект был в proxy, не в серверной защите.

После исправлений адресные и соседние тесты: **45 passed, 1 skipped**. Расширенный backend-набор до добавления отрицательного контроля неизвестного ValueError: **173 passed, 5 skipped**. Пропуски зависят от PostgreSQL test URL; результаты SQLite не выдаются за PostgreSQL.

Текущий набор на отдельном PostgreSQL 16:

```sh
TEST_POSTGRES_URL=<isolated disposable PostgreSQL URL> \
/tmp/coreteams-admin-plan-venv/bin/python -m pytest \
  backend/tests/test_pr36_backend_regressions.py \
  backend/tests/test_ai_access_ingress.py \
  backend/tests/test_harness_projection.py -q --tb=short
```

**29 passed**. После pytest количество собственных `test_*` БД равно 0. Собственный tmpfs-контейнер удалён после проверки полного ID и ownership label. Чужие контейнеры и тома не затронуты.

## Настоящий nginx → backend

Временный скрипт `/tmp/pr36_nginx_integration.py` поднял настоящий локальный backend с новой SQLite и nginx из уже существующего образа `pps-ci:frontend`. В контейнер адресно смонтирована актуальная конфигурация проекта; backend доступен через `host.docker.internal`, внешний порт nginx назначен Docker случайно на loopback. Использовались только синтетические аккаунты.

Результат настоящих HTTP-запросов через nginx:

- `Origin` совпадает с внешним authority **включая порт** → freeze **200**;
- посторонний `Origin` при тех же cookie/Host → **403**;
- конфигурация обоих proxy-путей `/api/` и `/ws` сохраняет `$http_host`.

Первый технический запуск не дождался nginx из-за startup/entrypoint; контейнер был удалён, запуск исправлен указанием nginx entrypoint и обработкой временного disconnect. Успешный повтор проверил реальный HTTP путь; WebSocket handshake отдельно не выполнялся. Собственный nginx-контейнер и локальный backend остановлены.

## Проверки и ограничения

Текущие шесть адресных regressions: **6 passed**. Попытка полного backend-прогона без канонического `PYTHONPATH=runner/src:runner` на параллельно изменявшемся diagnostic-коде: **3 failed, 463 passed, 18 skipped**. Два failure — недоступный импорт `evals.fake_gateway`, один — timeout wire lost-ACK. Эти чужие файлы не исправлялись; полный green gate этим запуском не заявляется. После завершения параллельных изменений нужен общий канонический прогон.

Scoped Ruff `--select F`, compileall и `git diff --check`: PASS. Использован `uv tool run --from ruff`, поскольку Ruff не установлен в тестовом venv; зависимости проекта не менялись.

Применены existing-code, python-rules, postgres-rules и сработавшие references до правок и по итоговому diff. Изменение относится к Boundary: proxy/API, состояние исполнения и SQL-транзакции.

Плагины `security-review` и `reliability-review` отсутствуют: соответствующие checklist gates не запускались, полного аудита не заявляем. Конкретные проверки Origin, изоляции optional failure, конкурентной работы и PostgreSQL выполнены выше. Стенд/production не проверялись; feature flags не менялись.
