# demo-gateway — целостность сохранения артефактов, 27.10.2026

## Срез и границы

- База проверена через GitHub: `demo-gateway@0acfc842cc5129b1a7a245fc292d855d2dea5618`.
- Ветка исправления: `fix/artifact-save-integrity-2026-10-27`, отдельная копия репозитория.
- Кандидат подготовлен для отдельного draft PR к `demo-gateway`, для ревью Димы и Арины.
  Точный опубликованный head закрепляется в PR; тесты ниже относятся к этому исправлению поверх базы.
  Deployment, merge/auto-merge и параллельные агенты не выполнялись.
- Изменяется существующий путь `EXECUTOR=runner → backend SQLite artifact snapshot`.
  Никакого нового хранилища, схемы БД, API B или wire-схемы receipt не вводится. Legacy CLI-путь не менялся.
- Связанный существующий follow-up: PPS-218 (Дима, смежное review Арины); эта локальная правка
  не закрывает карточку и не заменяет согласование её требований к другому storage-клиенту.

## Исправлено

1. `backend/runner_executor.py`: фактический размер проверяется до сохранения; превышение
   `artifact_max_download_bytes` больше не превращается в `content=None` с успешным receipt.
2. UTF-8 декодируется строго. Непредставимые байты отклоняются, а не заменяются на U+FFFD.
   Валидные UTF-8, CRLF, BOM, пустой файл и точная граница размера сохраняются без изменения байтов.
3. Проверки объявленных runner hash/размера сохранены. Их несовпадение, неподдерживаемое
   содержимое и ошибки чтения/записи дают существующий публичный `artifact_save_failed`, без утечки
   содержимого/ошибок SQL/Docker. Эти ситуации не подтверждают save и не дают run.completed.
4. `backend/repository.py`: существующий `append_artifact` получил внутренний необязательный
   `expected_sha256`. Для runner он проверяет реально перечитанные SQL-колонки snapshot:
   content, размер, путь, отсутствие truncated и SHA-256 исходных байтов. Это SELECT колонок,
   не чтение прежнего ORM-объекта из identity map.
5. Readback происходит после flush и **до commit той же транзакции**. При несовпадении/ошибке
   откатываются snapshot и artifact.saved event. При успехе commit завершается до публикации
   saved и вызова существующего `transport.confirm_saved`.
6. Нормальный повтор запроса сохраняет прежний Run и не создаёт второй snapshot/receipt.

## Реально выполненные проверки

Окружение: Windows, готовый Python test environment; временный настоящий SQLite, HTTP/WebSocket
TestClient и существующие FakeDocker/FakeRunner. Полная регрессия также включает прежний loopback
тест реального GatewayClient с временным Uvicorn. Docker daemon, реальные контейнеры, Claude/CTF,
внешний стенд и пользовательский E2E **не запускались**.

| Набор | PASS | FAIL | XFAIL / SKIP |
|---|---:|---:|---:|
| Baseline: 4 существующих executor + первые 17 новых integrity-проверок | 10 | 11 | 0 / 0 |
| Первоначальный fix: executor + integrity + transport + repository | 52 | 0 | 0 / 0 |
| Финальные 19 новых integrity-проверок (подмножество строки ниже) | 19 | 0 | 0 / 0 |
| Финальная полная backend-регрессия | **124** | **0** | **0 / 0** |

Baseline11FAIL — не одиннадцать отдельных production-инцидентов: семь проверок обнаруживают
ложное успешное сохранение/непроверенный снимок, четыре — некорректную классификацию ошибок.
Затем добавлены два отдельных сценария commit-failure и readback-query-failure. Результаты наборов
не складываются. Один Starlette/httpx deprecation warning, исходные зависимости проекта не менялись.

Новые проверки:

- 5 положительных: ASCII, Unicode+CRLF, BOM, пустой файл, точная граница **байтов**, а не символов.
  Скачанные байты/hash совпадают с исходными до и после перезапуска приложения с той же БД.
  В момент confirm_saved отдельное SQLite-соединение уже видит точные committed bytes.
- 13 отрицательных: oversize, invalid UTF-8, wrong hash/size, read/write/commit failure,
  readback query failure, подмена content/size/path, исчезновение snapshot, truncated snapshot.
  Проверяется отсутствие сохранённого snapshot, persisted/public artifact.saved, подтверждения
  транспорту и run.completed, а не только код HTTP/терминальный статус.
- 1 replay: тот же client_request_id возвращает тот же Run; одна команда, один snapshot и один receipt.

Полный машинный отчёт: `.test-runs/backend-final.xml` (124 tests / 0 errors / 0 failures / 0 skipped),
baseline: `.test-runs/baseline.xml`. Эти каталоги исключены из Git; реальные секреты в тестах не используются.

### Воспроизведение

Использовать test dependencies из pyproject.toml, доступный пакет runner через `runner/src`,
websockets15.x для существующего runner-клиента. В этой проверке использованы pytest9.1.1,
pytest-asyncio1.4.0, SQLAlchemy2.1.4, FastAPI0.143.1, Docker SDK7.2.0, Uvicorn0.54.0, websockets15.0.1.
Недостающие SDK/Uvicorn/websockets установлены только в игнорируемый `.test-deps`; общий venv не изменялся.

```powershell
$env:PYTHONPATH = (Join-Path $PWD '.test-deps') + ';' + (Join-Path $PWD 'runner\src')
python -m pytest backend/tests -q -ra --tb=short -p no:cacheprovider --basetemp=<fresh-short-temp> --junitxml=.test-runs/backend-final.xml
```

Начальный черновой replay-тест ошибочно ожидал повторной отправки terminal event; он был остановлен
и исправлен под существующий контракт `run.accepted(idempotent_replay=true)` без изменения production
протокола. В baseline засчитан только последующий завершённый запуск. Полный набор первоначально
упирался в отсутствующий Uvicorn и затем в PYTHONPATH runner; после исправления окружения выполнен
полностью, без исключения/skip проблемных тестов.

## Что остаётся

- Хранилище пока текстовое: произвольные бинарные файлы/ZIP и большие артефакты требуют согласованной
  byte-preserving storage-реализации. Сейчас неподдерживаемые байты/размер отвергаются безопасно;
  это не реализация бинарного или потокового хранения. Docker archive read всё ещё использует существующий
  in-memory helper; его потоковые лимиты этим пакетом не менялись.
- Wire receipt и version_id по source commit не переопределялись. Canonical document/version/approval,
  обязательность всех полей старого протокола и интеграция хранилища Арины — отдельное согласование.
- Две БД (backend и RunnerTransport) не стали общей транзакцией. Crash после commit snapshot,
  но до confirm_saved, и восстановление доставки через эту границу требуют отдельного recovery-контракта
  и fault/E2E проверки. Normal replay PASS не доказывает этот crash-сценарий.
- PostgreSQL/Git-as-Storage, настоящий Docker/mount/security, auth/ownership, подключение React B,
  structured questions/same-public-Run и общий end-to-end путь — **NOT_RUN / не исправлялись**.
- Принятие кандидата Димой/Ариной ещё не подтверждено. PPS-218 остаётся открытой;
  нет утверждения DEPLOYMENT_READY, пользовательского результата или права на merge.
