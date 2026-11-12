# PR36: безопасное копирование Run ID и изоляция build-теста

Дата: 2026-11-12. Только локальные исправления; commit/push/deploy не выполнялись.

## Подтверждённый дефект Clipboard API

`RunDetailContent` напрямую обращался к `navigator.clipboard.writeText`: при отсутствии Clipboard API доступ к методу бросал синхронный TypeError, до `.catch`. Такое возможно на небезопасном HTTP origin.

Локальный helper `adminClipboard.copyRunId` проверяет доступность API и перехватывает синхронные/асинхронные ошибки. Успешная надпись появляется только после выполненного write. При отказе/отсутствии API показываются `role=status`, инструкция и подписанное read-only поле Run ID; фокус выделяет ID для ручного копирования. `execCommand`, разрешения браузера и сторонние зависимости не добавлялись.

Проверки:
- RED: новый test suite не загружался из-за отсутствия helper; затем GREEN — 4 случая: undefined API, rejected write, successful exact ID, synchronous throw.
- Настоящий Chrome и frontend на 127.0.0.1:5198 с synthetic API: Clipboard API отсутствует / write запрещён / write успешен — все три сценария PASS. Не было pageerror, ложного copied, поле ручного копирования выделяло точный ID.
- Screenshot `/tmp/coreteams-admin-clipboard-fallback.png` просмотрен; это тестовые данные, не production.

## Исправление CI build-test boundary

Read-only проверка GitHub CI на опубликованном `02024af` обнаружила frontend failure: `adminBuild.test.ts` читает репозиторный Compose вне frontend image (`/docker-compose.yml`), что даёт ENOENT. Остальные python/schema-postgres/build/compose/web-product checks были green, gate зависимо red.

Воспроизведён RED на отдельной копии только frontend Dockerfile и теста **без Compose**. После исправления этот frontend-only context проходит: 1 passed. Frontend test по-прежнему проверяет Dockerfile ARG/ENV перед Vite build.

Обязательная Compose-проверка не отключена и не пропущена: она перенесена в `backend/tests/test_admin_build_config.py`, который исполняется существующей root/backend CI-командой. Path детерминирован относительно test-файла; regex ограничен именно frontend service/build block. Backend test — 1 passed без PYTHONPATH; дополнительная проба подменённого default `true` действительно отклоняется assertion.

## Итоговые проверки

- `npm exec --yes --package=node@22 -- npm --prefix frontend test` — **76 passed**.
- Та же Node22: frontend build и lint — PASS.
- `env -u PYTHONPATH /tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests/test_admin_build_config.py -q` — **1 passed**.
- Ruff backend test с CI selectors E4/E7/E9/F — PASS.
- Scoped diff-check — PASS. Общий diff-check во время параллельной работы обнаружил чужую пустую строку EOF в `backend/harness_diagnostics.py`; координатор уведомлён, этот файл не менялся.
- Собственный Vite PID/команда проверены, процесс остановлен; порт 5198 свободен. Браузер закрыт.

Notes: existing-code классификация Local/Module, contract и actual diff routing проверены. Python routing: test-only, testing/developer references прочитаны. `typescript-rules`, `security-review`, `reliability-review` недоступны: checklist gates не выполнялись. Конкретные clipboard failure/recovery/browser и build-context проверки перечислены выше. Для уже существующего browser smoke пришлось использовать имеющийся Playwright driver вместо отсутствующего chromium-cli; воспроизводимый launch/browser workflow можно позднее оформить `/run-skill-generator`.
