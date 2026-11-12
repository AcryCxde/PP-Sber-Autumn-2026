# PR36: HTTPS Origin, согласованная пагинация и UTC

Дата: 2026-11-12. Три замечания четвёртого ревью воспроизведены и исправлены. Это не деплой и не разрешение включения функций.

## Решения и польза

- **HTTPS за HTTP upstream:** backend получил необязательный `ADMIN_PUBLIC_ORIGIN`. Это точный внешний browser origin, задаваемый оператором, а не произвольные `Forwarded`/`X-Forwarded-*`. При отсутствии настройки сохраняется сравнение с непосредственным request origin. Compose передаёт настройку в backend; пустое значение означает отсутствие. За HTTPS оператор задаёт, например, `ADMIN_PUBLIC_ORIGIN=https://admin.example.test` (либо явный нестандартный порт, например `:8443`). Нужен перезапуск backend с настройкой; флаги функций остаются выключенными по умолчанию.
- **Конфигурация fail-fast:** принимается только каноническая HTTP(S) origin-строка: lowercase ASCII hostname, без path/query/fragment/userinfo, default-port опущен; нестандартный порт — обычное десятичное число без начальных нулей. IPv6 — сжатый адрес в скобках без zone ID, IPv4 — полный обычный адрес. Пробелы, controls, Unicode hostname (использовать явный punycode), пустой порт и неоднозначные числовые/hex/octal IPv4 формы отклоняются при старте. Это защита от ошибки конфигурации и ложных 403, не утверждение нового CSRF bypass.
- **Users pagination:** COUNT и ограниченная страница выбираются одним SQL statement (CTE + outer join). PostgreSQL READ COMMITTED использует один snapshot. Пустая и выходящая за диапазон страница всё равно возвращает правильный total. Сортировка `created_at DESC, id DESC` сохранена во внешнем SELECT.
- **UTC:** operational `_stamp` делегирует единому `utc_iso`; naive значения считаются UTC, aware offsets переводятся в UTC, `None` остаётся `None`.

## Доказательства

Начальные регрессии: **3 failed** (отсутствующая настройка origin, два SELECT вместо одного, aware offset не переводился в UTC). Дополнительные некорректные конфигурации: **12 failed / 6 passed** до строгой проверки.

```sh
/tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests/test_pr36_origin_pagination.py -q --tb=short
```

Итог: **33 passed, 1 skipped**; пропуск — PostgreSQL-only concurrent-registration test.

Изолированный PostgreSQL16 (собственный disposable container, отдельный loopback port):

```sh
TEST_POSTGRES_URL=<temporary-isolated-test-url> /tmp/coreteams-admin-plan-venv/bin/python -m pytest \
  backend/tests/test_pr36_origin_pagination.py backend/tests/test_admin_api.py \
  backend/tests/test_admin_operations.py -q --tb=short
```

**46 passed**. Этот прогон выполнен до последних шести дополнительных parser-only конфигурационных случаев; изменения SQL после него отсутствовали. Deterministic regression коммитит регистрацию независимым соединением после начала statement: первый ответ остаётся согласованным, следующий видит нового пользователя.

Реальная TLS-проверка: собственный nginx из уже имеющегося образа `pps-ci:frontend` терминировал HTTPS и передавал запрос в настоящий локальный HTTP backend, запущенный с `--no-proxy-headers`. Временный self-signed сертификат, только синтетические аккаунты. `Origin=https://localhost:<external-port>` дал **200**, чужой Origin — **403**. Безопасность не зависит от принятия forwarded scheme Uvicorn. Сначала временный backend не стартовал из-за пересечения workspace/data; исправлена только изолированная тестовая конфигурация, после чего проверка прошла.

Ruff **0.17.4** с CI selectors `E4,E7,E9,F`, compileall и `git diff --check` — PASS. Это scoped-проверки, не новый полный CI-прогон.

## Очистка и ограничения

Собственные PostgreSQL и nginx проверены по полному ID/ownership label и удалены; PostgreSQL test DB count=0. Собственный backend остановлен; пользовательские контейнеры/БД/модели не затрагивались. Коммитов/push/deploy нет.

Применены existing-code/python-rules/postgres-rules и сработавшие reference routing до изменений и по фактическому diff. Навыки security-review/reliability-review недоступны; формальные checklist-аудиты не выполнялись. Проверены конкретные hostile-Origin, неявное недоверие forwarded headers, pagination snapshot и UTC регрессии. Реальная производственная TLS-конфигурация не менялась и не проверялась.
