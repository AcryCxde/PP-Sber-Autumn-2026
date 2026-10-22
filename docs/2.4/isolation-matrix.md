# PPS-132 — негативная матрица изоляции (T03)

- **Версия документа:** `PPS-132-T03 v0.2-draft`
- **Дата проверки:** 2026-10-22
- **Ветка:** `isolation-matrix-storage`
- **Runner baseline:** `AcryCxde/isolation-matrix@8be086c`
- **Storage 2.3 source snapshot:** `task-infra@8ba443b` (реализация из `86cf6d0`, проверки надёжности из `ff75f1e`, точный поиск `Operation-Id` из `8ba443b`)
- **Текущий HEAD до коммита матрицы Storage:** `8be086c`; изменения Storage и этого документа находятся в рабочем дереве
- **Локальный Run ID:** `PPS-132-storage-20261022-local-02`

Строгий `XFAIL` означает открытую находку, а не успешную проверку. `XPASS` для таких тестов
считается падением и требует снять метку после подтверждённого исправления.

## 1. Runner — база Ильи сохранена

Автоматизированная матрица: `runner/tests/test_isolation_matrix.py`.
Канонический результат ветки `isolation-matrix@8be086c`: **124 проверки — 115 PASS / 9 strict XFAIL**.
Она проверяет, что граница проекта на стороне runner отклоняет чужие пути, ID и секреты, а отказ
не оставляет побочных эффектов: файл не создан, inbox пуст, журнал и события v0 не содержат утечек.

### 1.1. Что считается границей runner

1. Том `proj-<id>` монтируется только в контейнер `ct-<id>` (`sessions.run_args`), секреты — `:ro`.
2. Внутри контейнера `guard.check` ограничивает файловые инструменты корнем
   `/workspace/project` с раскрытием симлинков. Для Bash это только эвристика, не граница.
3. Команды проходят через `accept_command` и `Inbox` (ID `[0-9A-Za-z_-]{1,64}`, реестр развилок).
4. Наружу данные проходят через `Redactor`, затем `Translator` v0 публикует только разрешённые поля.

### 1.2. Матрица runner

| Вектор | Слой | Статус baseline |
|---|---|---|
| `../`, глубокий traversal, абсолютный путь в соседний том | `guard.check` | PASS |
| `/etc/passwd`, `/proc/self/environ`, `/run/secrets/*` через Read/Grep/Glob/Bash | `guard.check` | PASS |
| `.runner/events.jsonl`, `.runner/inbox`, `claude/` | `guard.check` | PASS |
| Двойник корня, `~`, `/usr/../etc`, `/dev/null/../..` | `guard.check` | PASS |
| Glob с `..`, NUL-байт, симлинки и цепочки симлинков | `guard.check` + `realpath` | PASS |
| Симлинк из seed-каталога | `seed_project` + `guard.check` | PASS |
| `..\x`, `%2e%2e/x`, unicode-точки | буквальные имена внутри корня | PASS |
| Bash с явными `/`, substitution и redirect | эвристика guard | PASS |
| Bash через `$HOME`, base64 и путь, собранный интерпретатором | эвристика guard | strict XFAIL |
| Hardlink на внешний файл | `guard.check` | strict XFAIL |
| Невалидные command/fork ID, чужие/закрытые fork, неверные ответы | `accept_command` / `Inbox` | PASS |
| Повтор command ID с другим телом не перезаписывает команду | `accept_command` | PASS |
| Чужой `project_id` не попадает в inbox | `accept_command` | PASS |
| Кадр команды с чужим `project_id` отклоняется | `accept_command` | strict XFAIL |
| Невалидный `project_id` в config/spec/CLI | config / CLI | PASS |
| Только свой том и секреты; нет privileged/network/docker.sock/cap-add | `sessions.run_args` | PASS |
| Секреты и чужие ключи отсутствуют в env контейнера | `sessions.container_env` | PASS |
| Секреты, системные пути и чужой project ID не выходят в draft v0 | `EventLog` + `Redactor` + `Translator` | PASS |
| Артефакты вне проекта не публикуются | `Translator` | PASS |
| Внутренний путь в тексте `message.created` | `Translator` | strict XFAIL |
| Токен в base64/hex | `Redactor` | strict XFAIL |

### 1.3. Пять находок runner baseline (без изменения результатов Ильи)

1. **Hardlink:** `realpath` не раскрывает hardlink; фактическая граница зависит от раздельных томов,
   `EXDEV` и системной политики hardlink.
2. **Bash-эвристика:** переменные, подстановки и интерпретаторы обходят статический разбор пути;
   границей должно оставаться монтирование, а не эвристика.
3. **Чужой `project_id` в команде:** `accept_command` не знает ID своего runner и принимает чужое поле;
   нужна проверка project binding в gateway/runner-контракте.
4. **Путь в `message.created`:** авторский текст модели публикуется без маскировки внутренних путей.
5. **Закодированный токен:** `Redactor.text` маскирует дословное значение, но не base64/hex варианты.

### 1.4. Локальная перепроверка runner на Windows 11

Команда:

```text
uv --directory runner run pytest tests/test_isolation_matrix.py -q -rxX
```

Фактический локальный результат 2026-10-22: **115 PASS / 9 strict XFAIL / 9 FAIL**.
Девять дополнительных FAIL не заменяют канонический результат Ильи: запуск выполнен на Windows,
где создание symlink завершилось `WinError 1314`, а POSIX-пути и ожидаемые Docker mount-строки
нормализовались иначе. Для принятия runner baseline используется результат Linux-ветки
`8be086c`; локальный Windows-запуск зафиксирован как непереносимый, а не замаскирован под успех.

## 2. Storage 2.3 — матрица Арины

### 2.1. Тестовый контур

Тесты: `backend/tests/test_storage_isolation.py`.

- `project_alpha`: фактический ID `demo-main`, субъект `demo-user`;
- `project_beta`: фактический ID `demo-isolated`, субъект `demo-other`;
- внутренний Storage API запускается на случайном loopback-порту;
- `DATA_DIR` и `STORAGE_ROOT_PATH` для каждого теста создаются в отдельном `tmp_path`;
- сервисный ключ генерируется на тест и не печатается в отчёт или публичные события;
- физическое хранилище — реальный локальный Git-репозиторий, а не mock.

Использованные файлы Storage 2.3:

- `web-demo/server.js` — public/internal routes, ownership checks, события;
- `web-demo/git-storage.js` — проектный Git-prefix, запись и восстановление blob;
- `web-demo/metadata.js` — metadata/confirmation boundary;
- `backend/tests/test_storage_isolation.py` — негативная матрица.

### 2.2. Фактический запуск

```text
python -m pytest backend/tests/test_storage_isolation.py -q -rxX
```

Run ID `PPS-132-storage-20261022-local-02`: **34 проверки — 16 PASS / 18 strict XFAIL / 0 FAIL / 0 NOT_RUN**.

Полный backend regression run:

```text
python -m pytest backend/tests -q
```

Фактический результат: **75 PASS / 18 strict XFAIL / 1 SKIP / 0 FAIL**. Единственный SKIP находится
в общем backend-наборе; целевая Storage-матрица выполнена полностью без SKIP.

### 2.3. Покрытые векторы Storage

| Вектор | Проверка | Результат |
|---|---|---:|
| Foreign `projectId` | alpha видит только alpha; list/create runs beta отклонены | 3 PASS |
| Foreign `artifactId`: direct read/download | чужой ID под alpha-prefix и beta-prefix с alpha-subject скрыт | 2 PASS |
| Foreign run artifact download | alpha не скачивает artifact beta через run route | 1 PASS |
| Artifact metadata | сначала положительный owner-control, затем foreign request | 1 strict XFAIL |
| Artifact versions/history | сначала положительный owner-control, затем foreign request | 1 strict XFAIL |
| Foreign `materialId` | read, bind, modify, delete с owner positive control | 4 strict XFAIL |
| Approval bypass | approve и reject чужой версии с owner positive control | 2 strict XFAIL |
| Traversal в `projectId` | `../`, `%2e%2e%2f`, POSIX/Windows absolute; без записи | 4 PASS |
| Traversal в `runId` | те же классы плюс separator; отказ без state mutation | 5 strict XFAIL |
| Traversal в `operationId` | `../`, encoded и absolute; отказ без state mutation | 4 strict XFAIL |
| Traversal в download URL | encoded slash/dot-dot/absolute | 3 PASS |
| Symlink/junction на project-prefix | запись не должна выйти за `STORAGE_ROOT_PATH` | 1 strict XFAIL |
| Утечки в rejection body | нет content, git path, stack, token, internal filesystem path | 1 PASS |
| Утечки в public events после rejected path | attack marker/path/token не публикуются | 1 PASS |
| Утечки в artifact events | content, git path, storage root и token не публикуются | 1 PASS |

### 2.4. Storage findings — strict XFAIL

1. **STO-1 — Metadata/Versions не реализованы (2 XFAIL).**
   Owner positive control получает `404`, поэтому нельзя считать foreign `404` доказательством
   ownership isolation. Нужны реальные owner routes и затем отрицательные проверки.
2. **STO-2 — Material routes не реализованы (4 XFAIL).**
   Не проверяемы read/bind/modify/delete для существующего material ID. До реализации маршрут нельзя
   считать защищённым только потому, что общий router возвращает `404`.
3. **STO-3 — Approval routes не реализованы (2 XFAIL).**
   Approve/reject для владельца отсутствуют; cross-project approval bypass остаётся непроверенным.
4. **STO-4 — Невалидный `runId` оставляет состояние (5 XFAIL).**
   Path validation происходит после записи attacker-controlled run в `state.json`. Физический выход
   из Git-prefix отклонён, но требование «отказ без побочных эффектов» нарушено.
5. **STO-5 — Невалидный `operationId` остаётся в operations (4 XFAIL).**
   Запрос отклоняется безопасным телом, однако intent с невалидным ID уже записан в состояние.
6. **STO-6 — Symlink/junction позволяет запись вне storage root (1 XFAIL).**
   Если `projects/{projectId}` заранее является directory link, временный/result-файл создаётся во
   внешнем каталоге до того, как Git отклоняет path. Нужна проверка realpath каждого parent/target
   относительно корня репозитория до любой записи.

Ни одна из этих находок не скрыта условным PASS. Исправление должно превратить соответствующий тест
в `XPASS`/падение, после чего метка снимается и документ обновляется.

## 3. Сводка ответственности, 16–26 октября 2026

Для координации используется отдельная нумерация межкомандных находок:
C-1 — project binding; C-2 — paths in `message.created`; C-3 — encoded secrets;
C-4 — real Docker mount isolation; C-5 — hardlink/EXDEV.

| Область | Ответственный | Автоматизированный статус | Статус находок / следующий шаг |
|---|---|---|---|
| Runner baseline | Илья | 115 PASS / 9 strict XFAIL (`8be086c`) | База сохранена; локальные Windows-only FAIL не переопределяют Linux evidence |
| Storage 2.3 | Арина | 16 PASS / 18 strict XFAIL, 0 FAIL | OPEN: STO-1…STO-6; закрыть routes, pre-validation и link boundary |
| Command / Gateway | Дима | NOT_RUN в Storage-ветке | C-1 OPEN: `accept_command`/gateway должны сверять `project_id`; binding токена к проекту ожидает отдельного исправления и проверки |
| Redaction | Филипп | baseline strict XFAIL | C-2 OPEN: убрать внутренние пути из `message.created`; C-3 OPEN: base64/hex в `Redactor` |
| Container boundary | Филипп | NOT_RUN (реальный Docker) | C-4 NOT_RUN: mount isolation; C-5 NOT_RUN: hardlink/EXDEV и реальная политика контейнера |

## 4. Границы T03 и решение

- Секреты T08 (`PPS-211`) проверяются отдельно.
- Враждебные документы T09 (`PPS-210`) проверяются отдельно.
- Строгие XFAIL **не считаются успешными проверками**.
- Карточка PPS-132/T03 остаётся **IN PROGRESS** как минимум до закрытия C-1…C-3 и Storage
  findings STO-1…STO-6; C-4/C-5 требуют отдельного реального Docker evidence.
- Готовность к пользовательскому E2E, включая статус `DEPLOYMENT_READY`, **не заявляется**.
- Документ не содержит реальных секретов, токенов или персональных контактов. Все идентификаторы и
  маркеры тестов синтетические.

## 5. Команды воспроизведения

Из корня репозитория:

```text
# Storage 2.3 — целевая матрица
python -m pytest backend/tests/test_storage_isolation.py -q -rxX

# Backend regression
python -m pytest backend/tests -q

# Runner baseline (предпочтительно Linux)
uv --directory runner run pytest tests/test_isolation_matrix.py -q -rxX
```
