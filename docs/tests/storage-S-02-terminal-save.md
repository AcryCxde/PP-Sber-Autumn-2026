# S-02 — terminal-last guard для позднего сохранения

## Source SHA

- Карточка: `MISMATCH-FOLLOWUP-2026-10-16-S-02`.
- Базовый SHA реализации: `265d7034a4347130872b05c05eb1b541e5568e53` (`task-infra`, PR #9).
- Коммит с воспроизводящими тестами: `67a114f`.
- SHA текущей рабочей базы до S-02: `8ba443b3c53a78d30ab5aadf0423feba4fb493a7`.
- Итоговый SHA исправления: **NOT_CREATED** — изменения оставлены незакоммиченными для финального ревью.

## Reproduction & Commands

Оригинальные два теста S-02 из `67a114f` были запущены на точном содержимом базового коммита во временном каталоге:

```bash
tmp_dir=$(mktemp -d) && \
  git archive 265d7034a4347130872b05c05eb1b541e5568e53 web-demo | tar -x -C "$tmp_dir" && \
  mkdir -p "$tmp_dir/web-demo/contract-tests" && \
  git show 67a114f:web-demo/contract-tests/storage-http.js \
    > "$tmp_dir/web-demo/contract-tests/storage-http.js" && \
  node_path=$(node -p "require('path').resolve('web-demo/node_modules')") && \
  NODE_PATH="$node_path" node --test --test-reporter=spec \
    --test-name-pattern="late artifact save must not reopen" \
    "$tmp_dir/web-demo/contract-tests/storage-http.js"
```

Результат воспроизведения: **2 FAIL / 0 PASS**. Для `unknown` и `failed` поздний save был принят, Run переходил в `completed`, а в tape добавлялись `artifact_saved` и `completed`.

Проверка S-02 после исправления:

```bash
node --test --test-reporter=spec \
  --test-name-pattern="late artifact save must not reopen" \
  web-demo/contract-tests/storage-http.js
```

Полный локальный HTTP-контракт:

```bash
node --test --test-reporter=spec web-demo/contract-tests/storage-http.js
```

Baseline-набор из 12 тестов:

```bash
DATABASE_URL='' npm --prefix web-demo test
PYTHONUTF8=1 PYTHONPATH=runner/src \
  python -m pytest -c runner/pyproject.toml \
  runner/tests/test_storage.py -q -p no:cacheprovider
```

## Изменение

### `web-demo/server.js`

- Общий набор терминальных статусов импортируется из `metadata.js`, чтобы HTTP и PostgreSQL guards использовали один перечень.
- Существующий ранний guard `/internal/artifacts` отклоняет новую операцию для терминального Run до мутаций состояния и вызова storage.
- В `persistArtifact` добавлен второй guard после обработки конфликта и подтверждённого идемпотентного retry, но до:
  - создания operation intent;
  - `save(state)`;
  - `metadata.createIntent(...)`;
  - Git commit;
  - подготовки или публикации событий.
- `TerminalRunError` нормализуется в безопасный HTTP `409`.
- Уже подтверждённая точная операция возвращается до terminal guard, поэтому корректный retry остаётся идемпотентным.

### `web-demo/metadata.js`

- Добавлены общие `terminalRunStatuses` и `TerminalRunError`.
- `confirmArtifact` внутри транзакции блокирует строку Run через `SELECT ... FOR UPDATE`.
- Если сохранённый в PostgreSQL статус уже терминальный, транзакция отклоняется до обновления operation, artifact, events и Run.
- Безусловный перевод в `completed` остаётся только для Run, который после блокировки подтверждён как активный.

### `web-demo/contract-tests/storage-http.js`

Два S-02 теста проверяют `failed` и канонический текущий вариант unknown-состояния (`outcome_unknown` / событие `run.outcome_unknown`). Для каждого сценария подтверждено:

- HTTP `409` на late save;
- исходный терминальный статус не меняется;
- tape полностью неизменна;
- рекурсивный SHA-256 снимок всех файлов тестового `DATA_DIR` до и после отказа совпадает;
- ранее подтверждённая операция после отказа повторяется с HTTP `200` и возвращает исходные `artifactId` и `commitSha`.

## Test Results

| Проверка | Статус | Результат |
|---|---|---|
| S-02 на `265d7034...` — terminal `unknown` | **FAIL** (воспроизведение) | Late save принят; Run стал `completed`; добавлены два события |
| S-02 на `265d7034...` — terminal `failed` | **FAIL** (воспроизведение) | Late save принят; Run стал `completed`; добавлены два события |
| S-02 после исправления | **PASS** | 2 passed, 0 failed |
| Полный локальный `storage-http.js` | **PASS** | 6 passed, 0 failed |
| Baseline `web-demo/test/*.test.js` | **PASS** | 9 passed, 0 failed |
| Baseline `runner/tests/test_storage.py` | **PASS** | 3 passed, 0 failed |
| Baseline всего | **PASS** | 12 passed, 0 failed |
| Реальный PostgreSQL / `confirmArtifact` integration | **NOT_RUN** | Disposable PostgreSQL-стенд и безопасный `DATABASE_URL` не предоставлены; результат не симулировался |
| Общий стенд / deployment | **NOT_RUN** | Вне скоупа и запрещено условиями карточки |
| Пользовательский E2E | **NOT_RUN** | Не доказан локальными контрактными тестами |
| Остальные группы `67a114f` | **NOT_RUN** | S-01/S-03/S-04 не являются целью S-02 |

## Terminal-last Verification

Для обоих терминальных сценариев тест сохраняет status, полную tape и SHA-256 снимок каждого файла временного `DATA_DIR`. После late save проверяется точное равенство:

- status остаётся `failed` или `outcome_unknown`;
- список событий не получает `artifact_saved` или `completed`;
- `state.json`, Git repository и остальные локальные байты не изменяются;
- ответ — HTTP `409`, новый receipt не создаётся.

После отказа выполняется retry ранее подтверждённой операции. Он возвращает HTTP `200` и исходный receipt, что подтверждает отсутствие повреждения подтверждённой версии и сохранение идемпотентности.

## Known Limitations & Next Steps

- PostgreSQL guard проверен ревью кода, но не интеграционным прогоном: **NOT_RUN** из-за отсутствия disposable PostgreSQL-среды.
- Не проверялись реальные транзакционные гонки между несколькими процессами, failover PostgreSQL и восстановление после потери соединения.
- Локальный HTTP-набор использует настоящий временный Git repository, но `DATABASE_URL=''` и no-op metadata backend.
- ZIP, формат артефакта, миграции, gateway и storage implementation не переписывались.
- PR #11 не изменялся; merge, push и deploy не выполнялись.
