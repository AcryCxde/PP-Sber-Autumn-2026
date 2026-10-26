# S-01 — принадлежность Run входному проекту

## Source SHA

- Карточка: `MISMATCH-FOLLOWUP-2026-10-16-S-01`.
- Базовый SHA реализации: `265d7034a4347130872b05c05eb1b541e5568e53`.
- Коммит с контрактными проверками: `67a114f` (`test(storage): add opt-in contract probes and evidence`).
- SHA текущей рабочей базы до изменения: `8ba443b3c53a78d30ab5aadf0423feba4fb493a7`.
- SHA коммита с исправлением: **NOT_CREATED** — исправление оставлено незакоммиченным в рабочей директории.

## Reproduction & Commands

Тест `storage-http.js` из `67a114f` был запущен на точном содержимом базового SHA во временном каталоге:

```bash
tmp_dir=$(mktemp -d) && \
  git archive 265d7034a4347130872b05c05eb1b541e5568e53 web-demo | tar -x -C "$tmp_dir" && \
  mkdir -p "$tmp_dir/web-demo/contract-tests" && \
  git show 67a114f:web-demo/contract-tests/storage-http.js > "$tmp_dir/web-demo/contract-tests/storage-http.js" && \
  node_path=$(node -p "require('path').resolve('web-demo/node_modules')") && \
  NODE_PATH="$node_path" node --test --test-reporter=spec \
    --test-name-pattern="a request project must match the existing run" \
    "$tmp_dir/web-demo/contract-tests/storage-http.js"
```

Воспроизведение на базе: **FAIL**, несовпадающий `projectId` был принят с HTTP `200`.

Проверка S-01 после исправления:

```bash
node --test --test-reporter=spec \
  --test-name-pattern="a request project must match the existing run" \
  web-demo/contract-tests/storage-http.js
```

Baseline-набор:

```bash
DATABASE_URL='' npm --prefix web-demo test
PYTHONUTF8=1 PYTHONPATH=runner/src \
  python -m pytest -c runner/pyproject.toml \
  runner/tests/test_storage.py -q -p no:cacheprovider
```

## Изменение

В текущем `web-demo/server.js` ранняя проверка `existingRun.projectId !== body.projectId` уже находилась до вычисления replay и до любых вызовов `save`, `metadata.ensureRun` и `persistArtifact`. Однако она возвращала HTTP `403`, который не входит в согласованный контракт теста из `67a114f` (`400`, `404`, `409` или `422`).

Внесена минимальная правка: ранний отказ теперь возвращает HTTP `409` с безопасным JSON:

```json
{"error":"Запуск не принадлежит указанному проекту."}
```

Порядок исполнения не изменён: отказ остаётся до replay/save и до первой мутации состояния.

Контрактная проверка S-01 дополнена проверками, что:

- same-project replay возвращает HTTP `200`, тот же `artifactId` и тот же `commitSha`;
- запрос чужого проекта возвращает ровно HTTP `409` и только безопасную ошибку;
- снимок всех байтов файлов в изолированном `DATA_DIR`, включая `state.json` и Git repository, полностью совпадает до и после отказа.

## Test Results

| Набор / сценарий | Результат | Детали |
|---|---|---|
| S-01 на `265d7034...` с тестом из `67a114f` | **FAIL** (ожидаемое воспроизведение) | HTTP `200` для `demo-isolated` при Run проекта `demo-main` |
| S-01 после исправления | **PASS** | 1 passed, 0 failed; отказ HTTP `409` |
| Same-project save/replay | **PASS** | Первый save — `201`; replay — `200`, receipt с исходными `artifactId` и `commitSha` |
| Неизменность состояния при отказе | **PASS** | Полный побайтовый снимок файлов `DATA_DIR` не изменился |
| Baseline `web-demo/test/*.test.js` | **PASS** | 9 passed, 0 failed |
| Baseline `runner/tests/test_storage.py` | **PASS** | 3 passed, 0 failed |
| Baseline всего | **PASS** | 12 passed, 0 failed |
| Остальные opt-in проверки из `67a114f` | **NOT_RUN** | S-02/S-03/S-04 вне скоупа S-01; их статус этой карточкой не переопределяется |
| Реальный PostgreSQL | **NOT_RUN** | В этой карточке не поднимался |
| Общий стенд / deployment | **NOT_RUN** | Деплой запрещён и не выполнялся |
| Пользовательский E2E | **NOT_RUN** | Не входит в локальную S-01-проверку |

## State Immutability Verification

Перед запросом с `projectId=demo-isolated` тест завершает штатный save и same-project replay, затем рекурсивно строит снимок всех файлов тестового `DATA_DIR`: для каждого относительного пути вычисляется SHA-256 фактических байтов.

После ответа HTTP `409` строится повторный снимок и сравнивается с исходным через `assert.deepEqual`. Проверкой подтверждено отсутствие:

- изменений `state.json`;
- новых или изменённых operations, artifacts, runs и events;
- новых Git commits/blobs и изменений других файлов Git repository;
- нового receipt — тело ответа содержит только поле `error`.

Это возможно потому, что проверка принадлежности Run выполняется до присваивания `state.runs`, инициализации `state.events`, `save(state)`, `metadata.ensureRun(...)` и `persistArtifact(...)`.

## Known Limitations & Next Steps

- Проверка выполнена на локальном HTTP-сервере и реальном временном Git repository, но с `DATABASE_URL=''`.
- Реальная PostgreSQL-транзакция, rollback, RLS и конкурентные запросы не проверялись: **NOT_RUN**.
- Общий стенд, gateway-интеграция, deployment и пользовательский E2E не проверялись: **NOT_RUN**.
- S-02, S-03 и S-04 намеренно не исправлялись и не переоценивались этой карточкой.
- Стендовые и PostgreSQL-доказательства остаются в рамках родительских задач `PPS-167`, `PPS-169/170`, `PPS-132`, `PPS-214/213`.
- ZIP-модули, схема хранения, миграции и сервисная архитектура не изменялись.
- `git push`, `git merge` и deployment не выполнялись; PR #11 не изменялся и остаётся draft.
