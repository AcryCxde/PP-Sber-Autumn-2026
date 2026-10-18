# CoreTeam storage demo

Минимальная реализация S2 Storage по [`ADR-001`](docs/adr/ADR-001-storage.md): итоговый `result.md` уходит из эфемерного workspace Runner в отдельный Git-as-Storage, а метаданные подтверждения сохраняются в PostgreSQL.

## Запуск

Перед запуском передайте обязательные переменные окружения через deploy-среду или менеджер секретов; значения не храните в репозитории. Полный порядок описан в [`docs/deploy-storage-handoff.md`](docs/deploy-storage-handoff.md).

```bash
docker compose up --build -d
```

Web/API будет доступен на <http://127.0.0.1:4173>, PostgreSQL — на `localhost:5432`.

Для чистого повторного развёртывания одноразового локального контура:

```bash
docker compose down -v
docker compose up --build -d
```

> `down -v` удаляет постоянные данные. Обычный `docker compose down` и пересоздание контейнеров сохраняют артефакты.

## Confirmed write

1. Runner формирует `/workspace/project/result.md`.
2. `ArtifactStorageClient` вычисляет `content_sha256` и детерминированный `operation_id`.
3. Runner отправляет артефакт во внутренний `POST /internal/artifacts` с сервисным токеном.
4. Web storage создаёт `operations.state = intent`, затем фиксирует файл коммитом в Git с trailers `Operation-Id` и `Content-SHA256`.
5. В одной транзакции PostgreSQL операция становится `confirmed`, создаётся строка `artifacts`, Run становится `completed`, записываются `artifact_saved` и `completed`.
6. Только после ответа `artifact_saved` Runner пишет локальные события `artifact_saved` и `turn_completed`. При отсутствии подтверждения пишется `turn_failed: artifact_save_failed`.

Повтор запроса с тем же `operation_id` не создаёт второй артефакт: commit восстанавливается по trailer и сверяется по SHA-256.

## Подключение Runner

Storage URL передаётся как обычный env, токен — только файлом секрета:

Передайте `ARTIFACT_STORAGE_URL` через окружение, а `STORAGE_TOKEN` — из менеджера секретов, затем выполните:

```bash
uv --directory runner run sessions secrets init demo-main
uv --directory runner run sessions start demo-main --data runner/evals/fixtures/demo
```

После успешного хода с созданным `result.md` журнал содержит:

```text
... -> artifact_saved -> turn_completed
```

## Скачать подтверждённую версию

Web API читает не workspace Runner и не JSON-снимок, а конкретный Git blob по сохранённому `commit_sha`:

```text
GET /api/projects/{project_id}/runs/{run_id}/artifact
GET /api/projects/{project_id}/artifacts/{artifact_id}
```

Ответ — `text/markdown` с `Content-Disposition: attachment` и заголовком `X-Artifact-Commit`.

## Где лежат данные

| Данные | Постоянное место | Docker volume / точный путь |
|---|---|---|
| `users`, сессии | PostgreSQL | `pg_data`, таблицы `users`, `sessions` |
| `projects` и ownership | PostgreSQL | `pg_data`, таблица `projects` (`owner_id`, `slug`, Git URL/branch) |
| `runs` | PostgreSQL | `pg_data`, таблица `runs` |
| намерения/подтверждения записи | PostgreSQL | `pg_data`, таблица `operations` |
| публичные `events` | PostgreSQL | `pg_data`, таблица `events`, `UNIQUE(run_id, sequence)` |
| метаданные `artifacts` | PostgreSQL | `pg_data`, таблица `artifacts` (`operation_id`, hash, commit, path, size) |
| содержимое артефактов | Git-as-Storage | `artifact_git`, `/var/lib/coreteam/artifacts/projects/{project_id}/runs/{run_id}/result.md` |
| локальное состояние web-demo | файловое служебное состояние | `web_data`, `/var/lib/coreteam/web/state.json`; содержимое артефакта туда не пишется |
| workspace Runner | эфемерный/проектный том исполнения | `/workspace`; не используется API скачивания после confirmed write |

`materials` и `.ctf` уже представлены таблицами `materials`, `ctf_files` и зарезервированной Git-структурой из ADR; текущий минимальный E2E реализует запись итогового Markdown-артефакта.

## Проверки

```bash
uv --directory runner run pytest -q
npm --prefix web-demo test
```

Web-тест создаёт артефакт через внутренний контракт Runner, удаляет исходный workspace, перезапускает server и скачивает ту же версию из Git по `commit_sha`.
