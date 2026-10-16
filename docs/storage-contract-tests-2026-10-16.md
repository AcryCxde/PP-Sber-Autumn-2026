# Storage: сверка готовой реализации и недостающие проверки

Дата: 16 октября 2026. База: PR #9, `KolbasnyaAr/PP-Sber-Autumn-2026`,
`task-infra@265d7034a4347130872b05c05eb1b541e5568e53`.
Тестовая ветка: `codex/storage-contract-tests-2026-10-16`.

## Границы пакета

Использованы без изменения готовые `GitArtifactStorage`, HTTP-сервер, `MetadataStore`
и `ArtifactStorageClient`. Добавлены только три opt-in тестовых файла и этот документ.
Все production-файлы, прежние тесты, package.json и lock-файл сохранены.
Нет новой реализации хранилища, формата ZIP, сервиса одобрения или миграции.
Нет merge, изменений PR #11/PPS-2 или применения к общему стенду.

Это тестирование существующей ветки хранения, не принятие архитектуры подключения.
В этой базе Runner обращается к `/internal/artifacts` сам. Решение владельца о записи
через gateway требует отдельно согласовать/подключить этот готовый код к gateway;
успешные локальные тесты не доказывают готовность такого соединения.

## Фактический результат

| Набор | PASS | FAIL | Область доказательства |
|---|---:|---:|---|
| Существующий `web-demo/test/*.test.js` | 9 | 0 | 3 API/storage + 6 UI-логика |
| Существующий `runner/tests/test_storage.py` | 3 | 0 | Клиент хранения |
| Новый `storage-git.js` | 4 | 1 | Реальные временные Git-репозитории |
| Новый `storage-http.js` | 3 | 3 | Реальный локальный сервер + Git, без PostgreSQL |
| Новый `test_storage_receipts.py` | 17 | 9 | Реальный клиент, подмена только HTTP-ответа/ошибки |
| Всего новых проверок | 24 | 13 | 37 случаев; 13 проваленных assertions, не 13 независимых причин |

Провалы не скрыты через skip/xfail. Новые файлы расположены вне существующих
default test globs: старые зелёные проверки не заменены и не маскируют новые дефекты.
Оба новых opt-in запуска возвращают exit code 1 на указанной базе — это ожидаемая
фиксация реальных несовместимостей, а не свидетельство готовности к слиянию.

### Что уже подтверждено и используется повторно

- Точное сохранение UTF-8, SHA-256 и размера в байтах, в том числе кириллицы,
  emoji и CRLF; в каждом временном Git repo задан **локальный** `core.autocrlf=false`.
  Глобальная Git-конфигурация не менялась. Иные настройки нормализации в deployment
  этим тестом не проверены: их нужно явно зафиксировать отдельно.
- Старый Git commit читается без изменения после новой версии и удаления рабочего
  файла. Это проверка blob/commit, не готового API пользовательских версий/одобрения.
- Точный retry существующей операции возвращает тот же commit; изменение содержимого
  с тем же operation ID отвергается. На HTTP-границе отвергается перенос operation ID
  на другой Run.
- Traversal в Run ID и неверный digest отвергаются Git boundary.
- Отсутствующий/неверный сервисный Bearer token даёт HTTP 401, операция не появляется.
- Download проверяет соответствие artifact/project и демо-владельца.
  `x-demo-user` — имитируемая identity: это **не доказательство настоящей авторизации**.
- Клиент сохраняет исходные UTF-8 bytes в payload, стабильно считает operation ID
  для retry и отвергает отсутствующие поля, неверные type/operation/hash, не-JSON,
  неверный UTF-8/shape и HTTP 401/409/503, URLError/timeout.
- Существующий тест сохранения/повтора/конфликта, удаления workspace и перезапуска
  сервера сохранён и проходит; PostgreSQL в нём не включён.

## Подтверждённые пробелы для ревью

| ID / владелец ревью | Проваленные случаи | Наблюдение на базе | Где проверить / минимальное направление исправления |
|---|---:|---|---|
| S-01 / Арина + Дима | 1 | Save/replay с `projectId=demo-isolated` для существующего Run `demo-main` получает HTTP 200 и receipt исходного проекта | `web-demo/server.js:215`: до записи/replay сверить входной project с принадлежностью Run. Сервисный токен сам по себе не подтверждает эту связь |
| S-02 / Арина + Дима | 2 | После terminal `unknown` или `failed` поздний save получает HTTP 201, меняет Run на `completed`, дописывает `artifact_saved` и `completed` | `web-demo/server.js:103-145`, `/internal/artifacts`; также `metadata.js:121`. Не открывать закрытый Run повторно; позднюю версию/одобрение согласовать отдельно, не подменять текущий Run |
| S-03 / Арина | 1 | `findOperation("operation-prefix")` возвращает commit с trailer `Operation-Id: operation-prefix-long` | `web-demo/git-storage.js:54`: поиск должен сравнивать полный trailer, а не подстроку. Helper допускает произвольный безопасный segment; UUID production-клиента снижает частоту случая, но не исправляет helper |
| S-04 / Арина + Дима | 9 | Receipt принимает null/пустой artifact ID, произвольный commit, чужой gitPath, отрицательный/bool/string/неверный byte count; null size выбрасывает сырой TypeError вместо ArtifactStorageError | `runner/src/ctrunner/storage.py:89-105`: сейчас проверяется наличие 4 полей и затем coercion str/int. Нужны согласованные типы, привязка пути и точный byte count, нормализованная ошибка отказа; возвращения Ильи для этой сверки не ждём |

Для S-04 тесты задают целевой fail-closed критерий ревью: непустой строковый ID,
40 hex commit, путь текущего project/Run, целый (не bool/string) byte count,
совпадающий с отправленными байтами. Это предложенные строгие проверки имеющихся
полей, не объявление ещё не согласованного нового wire schema.

## Воспроизведение (PowerShell, из корня тестового worktree)

Среда фактического запуска: Node.js 24.19.0, Python 3.12, готовый соседний test venv.
Для запуска `server.js` установлена только объявленная зависимость `pg` с её
транзитивными зависимостями в изолированный `web-demo/node_modules`:

```powershell
Push-Location web-demo
pnpm install --ignore-scripts --lockfile=false --config.update-notifier=false
Pop-Location
```

Разрешённый package.json диапазон `pg ^8.16.3` разрешился в **8.23.1**.
Это не frozen-lock установка и не подтверждение точной deployment-версии.
Ни package.json, ни package-lock.json не изменены, глобальных установок нет.

Существующие проверки:

```powershell
$env:DATABASE_URL = ''
Push-Location web-demo
node --test test/*.test.js
Pop-Location
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'runner/src'
python -m pytest -c runner/pyproject.toml runner/tests/test_storage.py -q -p no:cacheprovider
```

Новые проверки (на базе выше **FAIL**, перечень и причины указаны в таблице):

```powershell
Push-Location web-demo
node --test --test-reporter=spec contract-tests/storage-git.js contract-tests/storage-http.js
Pop-Location
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'runner/src'
python -m pytest -c runner/pyproject.toml runner/contract-tests/test_storage_receipts.py -q -p no:cacheprovider --tb=short
git diff --check
```

Вне этого workspace можно использовать эквивалентный Python 3.12 venv с объявленными
pytest/pytest-asyncio. Новые тесты не импортируют/не вызывают Claude SDK.
На этом Windows sandbox запуск потребовал reviewed escalation из-за запрета temp
каталогов; это ограничение окружения, не дефект storage. Один созданный неудачным
pytest запуском пустой temp-каталог проверен и удалён. Temp Git/HTTP fixtures
удаляются самими тестами; данные владельца/общего стенда не затрагиваются.

HTTP-пакет выбирает свободный локальный порт, поднимает настоящий `server.js`,
принудительно задаёт пустой `DATABASE_URL` и отдельные DATA_DIR/ARTIFACT_REPO_PATH,
по завершении останавливает только свой child process. Не запускать прежние
baseline-тесты параллельно с другим процессом на их фиксированном порту 4181.

## NOT_RUN / чего не заявляем

- Реальный PostgreSQL: Docker CLI есть, но daemon недоступен, psql/postgres runtime
  не найден. Нет миграций, SQL transaction/rollback/race/fault injection, проверки
  durable PG после перезапуска и восстановления метаданных из PG. No-DB методы
  `MetadataStore` здесь no-op; это не имитация успешной PostgreSQL-транзакции.
- Настоящая multi-user identity/RLS, project-scoped service credentials и auth gateway.
- Gateway ↔ storage ↔ runner совместимость, реальный стенд/CTF/Claude и user E2E.
- ZIP builder/validator не переписывали и не мокали как готовый API; подключение
  существующего builder — отдельный пакет после согласования сохранения/скачивания.
- Отдельное одобрение и пользовательский каталог неизменяемых версий не реализованы
  этими тестами. В базе нет соответствующего готового API; это зафиксированный gap.
- Существующие session/SDK-тесты целиком не запускались в данном пакете: в используемом
  venv Claude SDK отсутствует; базовые тесты storage работают без него. Новые случаи
  не доказывают поведение Session при каждом некорректном receipt.

## Порядок дальнейшей работы

1. Арина подтверждает актуальность PR9 SHA и готовые операции хранения; Дима — границу
   вызова через gateway и критерии S-01/S-02/S-04. Согласовать строгость receipt, не
   вводя параллельный контракт.
2. Только после отдельного разрешения на исправления — точечные изменения готовой
   реализации по S-01–S-04 и повтор прежнего + opt-in набора. Эти тесты уже воспроизводят
   проблемные случаи; заново писать хранилище не требуется.
3. В безопасной disposable PG-среде отдельно проверить миграции, rollback, конкуренцию,
   потерю ответа после commit, восстановление и запись настоящих байтов/метаданных.
4. Филипп фиксирует реальные сервисные SHA/config и прогоняет общий стенд; Катя проверяет
   путь сохранение → download/ZIP → повторное открытие. Только после явного ОК владельца
   по конкретным PR возможно отдельное решение о merge; пакет тестов его не разрешает.
