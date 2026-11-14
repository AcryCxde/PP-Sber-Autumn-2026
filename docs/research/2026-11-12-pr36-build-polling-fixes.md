# PR #36: build-time флаг и deadline polling

## Исправления

- `frontend/Dockerfile`: build stage принимает `ARG VITE_ADMIN_SHELL_ENABLED=false` и передаёт его Vite через `ENV` до `npm run build`.
- `docker-compose.yml`: `frontend.build.args.VITE_ADMIN_SHELL_ENABLED` получает `${VITE_ADMIN_SHELL_ENABLED:-false}`. По умолчанию функция остаётся выключенной; backend-флаги не менялись.
- `adminPolling.ts`: одна попытка имеет deadline 30 секунд. По deadline запрос отменяется и попытка завершается ошибкой, освобождая controller для ручного повтора и автоматического backoff. `Promise.race` обеспечивает завершение даже loader, который игнорирует AbortSignal. Поздний результат завершённой попытки не восстанавливает данные. Dispose/revoke/replace отменяют попытку и очищают deadline.

## Команды сборки (не выполнялись как deploy)

Из корня репозитория при подготовленных обычных `.env` / `.env.gateway`:

```sh
VITE_ADMIN_SHELL_ENABLED=true docker compose build frontend
# Только после отдельного разрешения на выпуск:
# docker compose up -d --no-deps frontend
```

Пересборка выключенного интерфейса для отката:

```sh
VITE_ADMIN_SHELL_ENABLED=false docker compose build frontend
```

Прямой build без Compose: `docker build --build-arg VITE_ADMIN_SHELL_ENABLED=true -f frontend/Dockerfile .`.
Runtime environment уже собранного контейнера не изменяет Vite bundle. Эти команды не включают backend management/diagnostics и не означают одобрение deploy.

## Доказательства

RED: два polling-regression и build-contract regression — **3 failed, 9 passed**. Наблюдались отсутствие abort/error после 30 секунд и отсутствие Docker ARG.

GREEN:

```sh
npm exec --yes --package=node@22 -- npm --prefix frontend test
npm exec --yes --package=node@22 -- npm --prefix frontend run build
npm exec --yes --package=node@22 -- npm --prefix frontend run lint
```

**65 passed**, build/lint PASS. Включены hanging initial request → автоматический retry → auth cleanup; hanging refresh → timeout → ручной retry; abort-ignoring late reply не меняет свежий snapshot; dispose не оставляет таймеры. Прежние generation/auth/stale regressions также проходят.

Compose config проверен с synthetic `ci-only` interpolation values для обоих flag `false`/`true`: frontend build args совпали. Исходный config требует отсутствующий `.env.gateway`; для проверки через stdin override `services.backend.env_file: !reset []` исключено чтение этого файла. Compose/Docker ничего не запускали и не строили, project-файлы окружения не создавали.

`git diff --check` PASS. Existing-code routing выполнен до правок и по итоговому diff; изменение классифицировано Boundary (build/runtime и асинхронный request lifecycle), сохранены opt-in и прежние auth/generation контракты.

## Отдельный статус CI опубликованного коммита

`gh pr checks 36`: Python/gate fail, остальные проверки pass (build, classify, compose, frontend, schema-postgres, web-product). В Python job текущего опубликованного `14a851c` backend-only команда без дополнительного PYTHONPATH получает **4 failed, 466 passed, 18 skipped**: все четыре — `ModuleNotFoundError: evals` в `backend/tests/test_harness_wire.py`. Это передано координатору, исправление не входит в данный frontend/Docker scope.

## Ограничения

- Docker image build не выполнялся: проверены Dockerfile/Compose wiring, реальное Compose config interpolation и Vite build, но не полная контейнерная сборка.
- Loader, не поддерживающий отмену, может физически продолжать свою работу после deadline; отменённая попытка больше не блокирует UI и её поздний результат игнорируется. Для fetch используется AbortSignal.
- `typescript-rules`, `security-review`, `reliability-review` недоступны; соответствующие checklist gates не выполнялись. Security/reliability notes: backend guards не ослаблены, default-off сохранён, поздние ответы и отмена покрыты тестами.
- Lockfiles, backend, runner, пользовательские данные и feature defaults не изменены. Commit/push/deploy и модельных вызовов не было.
