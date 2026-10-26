# Deploy handoff: PostgreSQL + Git-as-Storage

Документ описывает безопасное развёртывание и откат текущего storage-контура: PostgreSQL хранит метаданные, а отдельный Git-репозиторий — подтверждённые версии артефактов. Значения паролей, токенов и других секретов здесь намеренно не приводятся.

## 1. Что входит в контур

| Компонент | Назначение | Долговечное состояние |
|---|---|---|
| PostgreSQL | проекты, владельцы, Runs, операции записи, события и метаданные артефактов | volume `pg_data` |
| Web/storage service | внутренний `POST /internal/artifacts`, проверка владельца и API чтения | собственного содержимого артефактов не хранит |
| Git-as-Storage | байты подтверждённых артефактов и история версий | volume `artifact_git` |
| Web state | служебное состояние демонстрационного контура | volume `web_data` |
| Runner workspace | временные файлы выполнения | не является источником восстановления после confirmed write |

PostgreSQL и Git-as-Storage образуют одну логическую систему. Их резервное копирование и восстановление необходимо выполнять согласованно.

## 2. Переменные окружения

В репозиторий, Markdown, CI-логи и командную строку не должны попадать значения секретов. Значения `DATABASE_URL`, `POSTGRES_PASSWORD` и `STORAGE_TOKEN` передаются средой развёртывания или менеджером секретов.

### Storage service

| Переменная | Обязательность | Назначение |
|---|---:|---|
| `DATABASE_URL` | да для PostgreSQL-контура | Строка подключения к PostgreSQL. Значение считается секретом и не публикуется. |
| `STORAGE_ROOT_PATH` | да | Корень постоянного Git-as-Storage. В Compose монтируется volume `artifact_git`. |
| `DATA_DIR` | да | Каталог служебного состояния web/storage service. В Compose монтируется `web_data`. |
| `STORAGE_TOKEN` | да | Сервисный токен внутреннего API. Значение поступает только из менеджера секретов. При пустом значении внутренний API не авторизует запросы. |
| `HOST` | нет | Адрес прослушивания HTTP-сервера. |
| `PORT` | нет | Порт HTTP-сервера. |
| `ARTIFACT_REPO_PATH` | только совместимость | Устаревший alias для `STORAGE_ROOT_PATH`. При наличии обеих переменных используется `STORAGE_ROOT_PATH`. |

### PostgreSQL container

| Переменная | Обязательность | Назначение |
|---|---:|---|
| `POSTGRES_USER` | да | Имя роли PostgreSQL, выдаваемое средой развёртывания. |
| `POSTGRES_DB` | да | Имя базы storage-контура. |
| `POSTGRES_PASSWORD` | да | Пароль роли PostgreSQL; только секретное хранилище, без значения в файлах репозитория. |

### Runner

| Переменная | Обязательность | Назначение |
|---|---:|---|
| `ARTIFACT_STORAGE_URL` | да | Внутренний URL storage service. |
| `STORAGE_TOKEN` | на этапе provisioning | Команда `sessions secrets init` записывает значение в secret-файл Runner; в рабочий контейнер токен не передаётся как обычная переменная окружения. |

## 3. Persistent volumes и mount points

| Volume | Mount point | Режим | Требование |
|---|---|---|---|
| `pg_data` | `/var/lib/postgresql/data` | read/write | Постоянный volume, не удалять при обычном redeploy. |
| `artifact_git` | значение `STORAGE_ROOT_PATH` (`/var/lib/coreteam/artifacts` в Compose) | read/write | Один writer на текущую реализацию; каталог должен поддерживать атомарный rename и Git file locking. |
| `web_data` | значение `DATA_DIR` (`/var/lib/coreteam/web` в Compose) | read/write | Постоянный служебный state демонстрационного сервиса. |
| Runner workspace | `/workspace` | read/write, ephemeral | Не использовать для восстановления подтверждённого результата. |

Запрещено монтировать `artifact_git` поверх workspace Runner или удалять его независимо от `pg_data`: метаданные в БД ссылаются на конкретные Git commit SHA.

## 4. Порядок применения схемы и миграций

В текущем storage-контуре есть два разных механизма, их нельзя смешивать:

1. `init.sql` — bootstrap PostgreSQL-схемы storage demo. Официальный образ PostgreSQL применяет его автоматически **только при первом старте с пустым `pg_data`**.
2. `backend/migrations` — отдельная Alembic-цепочка backend history API. Она не создаёт таблицы storage demo из `init.sql`.

### 4.1. Новый storage-контур

1. Создать постоянные volumes `pg_data`, `artifact_git`, `web_data`.
2. Передать обязательные переменные через deploy-среду; значения секретов не печатать в CI-логах и не проверять командой, раскрывающей итоговую конфигурацию.
3. Запустить только PostgreSQL:

   ```bash
   docker compose up -d db
   ```

4. Дождаться `healthy`. Probe использует `pg_isready` и имена пользователя/БД из окружения контейнера.
5. Убедиться, что `init.sql` был применён без ошибок. Это ожидается только для пустого `pg_data`.
6. Запустить storage service:

   ```bash
   docker compose up -d web
   ```

7. Проверить `GET /health/ready`. Код `200` означает, что:
   - PostgreSQL отвечает на `SELECT 1`;
   - `DATA_DIR` доступен на чтение и запись;
   - `STORAGE_ROOT_PATH` доступен на чтение и запись.
8. Выполнить smoke test confirmed write на тестовом Run и проверить чтение той же версии по `artifact_id` и `commit_sha`.

### 4.2. Обновление существующего storage-контура

`init.sql` не является механизмом инкрементальных миграций и повторно поверх существующего `pg_data` не запускается. До появления отдельной версионированной цепочки storage-миграций допустим только следующий порядок:

1. Остановить новые записи или перевести сервис в maintenance/read-only режим.
2. Сделать согласованный backup PostgreSQL и Git-as-Storage.
3. Проверить восстановление backup на отдельном контуре.
4. Применить заранее подготовленную и отревьюенную forward-only SQL-миграцию одним экземпляром migrator job.
5. Развернуть совместимую версию приложения.
6. Проверить readiness и confirmed-write/read smoke test.
7. Возобновить запись.

Нельзя использовать повторный запуск `init.sql` как upgrade-процедуру.

### 4.3. Alembic backend history API

Если вместе со storage-контуром разворачивается Python backend history API, его цепочка применяется отдельно:

```bash
python -m alembic -c backend/alembic.ini current
python -m alembic -c backend/alembic.ini upgrade head
python -m alembic -c backend/alembic.ini current
```

Перед запуском `DATABASE_URL` должен указывать именно на БД backend history API, а не по ошибке на PostgreSQL storage demo.

## 5. Rollback

### 5.1. Откат приложения без отката данных

Предпочтительный вариант, если новая версия не выполнила несовместимую миграцию:

1. Остановить новые записи.
2. Вернуть предыдущий image/tag приложения.
3. Не удалять volumes.
4. Запустить storage service и дождаться readiness.
5. Проверить чтение ранее подтверждённого артефакта по сохранённому `commit_sha`.
6. Возобновить запись.

### 5.2. Откат схемы/данных storage-контура

У bootstrap-схемы `init.sql` нет автоматического downgrade. Надёжный rollback — восстановление **согласованной пары** backup:

1. Остановить web/storage service и все writers.
2. Сохранить аварийную копию текущих `pg_data` и `artifact_git` для расследования.
3. Восстановить PostgreSQL из backup выбранной точки времени.
4. Восстановить Git-as-Storage из backup той же точки времени.
5. Запустить PostgreSQL и дождаться `pg_isready`.
6. Запустить storage service и дождаться `GET /health/ready = 200`.
7. Проверить несколько ссылок `artifact metadata -> commit_sha -> git_path` и SHA-256 восстановленных байтов.
8. Только после проверки вернуть трафик.

Нельзя откатывать только PostgreSQL или только Git volume: это создаёт подтверждённые метаданные без blob либо orphan commits без метаданных.

### 5.3. Alembic downgrade

Техническая команда для обратимой ревизии:

```bash
python -m alembic -c backend/alembic.ini downgrade <target-revision>
```

Но миграции `20260929_0005` и `20261012_0006` содержат необратимое удаление/редактирование чувствительных legacy-данных и не восстанавливают их через `downgrade()`. Для перехода через эти ревизии нужен backup restore, а не Alembic downgrade.

### 5.4. Полное удаление данных

`docker compose down -v` удаляет постоянные volumes и не является rollback. Команду допустимо использовать только для заведомо одноразового локального контура после отдельного подтверждения удаления данных.

## 6. Healthcheck и readiness probes

### PostgreSQL

Compose-проверка:

```text
pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

Рекомендуемые параметры:

- interval: 5 секунд;
- timeout: 3 секунды;
- retries: 20;
- успешный probe обязателен до запуска storage service.

Для Kubernetes используйте `exec` readiness probe с той же командой и переменными из окружения Pod. Пароль probe не нужен и в команду не передаётся.

### Storage service и директории

HTTP readiness endpoint:

```text
GET /health/ready
```

Ответы:

- `200 {"status":"ready", ...}` — БД доступна, `DATA_DIR` и `STORAGE_ROOT_PATH` читаются и доступны для записи;
- `503 {"status":"not_ready"}` — хотя бы одна зависимость недоступна.

Рекомендуемый Kubernetes probe:

```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: 4173
  periodSeconds: 10
  timeoutSeconds: 3
  failureThreshold: 6
```

Этот endpoint не возвращает DSN, пути хоста, токены, пароли или диагностические детали исключений.

## 7. Проверки после deploy/rollback

Минимальный набор:

1. PostgreSQL healthy.
2. Storage readiness возвращает `200`.
3. Запись нового checkpoint создаёт одну подтверждённую версию.
4. Повтор того же `operation_id` и SHA-256 возвращает прежние `artifact_id` и `commit_sha`, не создавая дубль.
5. Новая запись в terminal или `outcome_unknown` Run отклоняется.
6. Пользователь другого владельца не читает и не изменяет чужие project/run/artifact IDs.
7. Скачанные байты совпадают с исходными, SHA-256 совпадает с подтверждённым хэшем.

## 8. Явно вне scope

- Собственный ZIP-builder не добавляется: упаковка результатов должна выполняться только существующим `result-package-builder`.
- Серверная выгрузка research-метрик не включается.
- Этот handoff не разрешает merge в `main` и не меняет draft-статус PR.
