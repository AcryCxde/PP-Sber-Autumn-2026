# Запуск Claude Web

Приложение состоит из двух процессов:

- FastAPI backend на `http://localhost:8000`;
- Vite/React frontend на `http://localhost:5173`.

Команды ниже выполняются из корня репозитория, если не указано иначе.

## 1. Предварительные требования

Необходимо установить:

- Python 3.11 или новее;
- Node.js 20 или новее;
- npm;
- Claude Code CLI.

Проверка:

```bash
python --version
node --version
npm --version
claude --version
```

Claude Code CLI должен быть авторизован. Backend запускает его не в репозитории gateway, а в отдельном workspace диалога. При необходимости сначала запустите `claude` в отдельном терминале и выполните интерактивную авторизацию.

## 2. Подготовка backend

Создайте виртуальное окружение:

```bash
python -m venv .venv
```

Активация в PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Активация в Git Bash:

```bash
source .venv/Scripts/activate
```

Установите backend с зависимостями разработки:

```bash
python -m pip install -e ".[test]"
```

При необходимости создайте локальную конфигурацию:

```bash
cp backend/.env.example .env
```

По умолчанию при локальном запуске используется SQLite-база `backend/data/history.db`. Каталог и база создаются автоматически и игнорируются Git.

В docker compose backend работает с Postgres 16 (сервис `db`). Пароль задаётся в `.env` в корне репозитория (шаблон — `.env.example`); без `POSTGRES_PASSWORD` compose не запустится. Схему создают те же миграции Alembic.

```bash
cp .env.example .env
```

Для локального backend на Postgres укажите, например:

```dotenv
DATABASE_URL=postgresql+asyncpg://coreteams:<пароль>@127.0.0.1:5432/coreteams
```

Перенос истории из прежнего SQLite-файла в пустой Postgres (backend остановлен; исходный файл не меняется):

```bash
docker compose stop backend
docker compose run --rm --no-deps backend sh -c 'python -m backend.transfer_sqlite /app/backend/data/history.db "$DATABASE_URL"'
docker compose up -d backend
```

Тесты по умолчанию идут на SQLite. Чтобы прогнать их на Postgres, укажите сервер, где тестовому пользователю разрешено `CREATE DATABASE`; каждый тест получает свою временную базу:

```bash
docker run -d --name coreteams-pg-test -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:16-alpine
TEST_POSTGRES_URL=postgresql+asyncpg://test:test@127.0.0.1:55432/test python -m pytest -q backend/tests
```

Для Claude Code CLI используется безопасный режим:

```dotenv
CLAUDE_PERMISSION_MODE=acceptEdits
```

Он автоматически разрешает встроенные операции `Write`/`Edit`, но не включает `--dangerously-skip-permissions` и не даёт безусловный доступ произвольным shell-командам. Текущий web-интерфейс не реализует интерактивный permission prompt; операции, которые требуют отдельного подтверждения даже в `acceptEdits`, будут отклонены до появления специального approval flow.

Укажите постоянный каталог workspace вне репозитория gateway и вне каталога SQLite:

```dotenv
WORKSPACE_ROOT=~/.claude-web/workspaces
SUBSCRIPTION_WINDOW_SECONDS=15
SUBSCRIPTION_POLL_INTERVAL_SECONDS=0.25
MAX_CONCURRENT_RUNS=4
```

Каждый conversation использует подкаталог `<WORKSPACE_ROOT>/<conversation_id>`. Backend откажется стартовать, если workspace пересекается с gateway или каталогом БД.

API и `/ws` доступны только после входа: пользователь регистрируется по email и паролю (не короче 8 символов) на экране входа, backend выдаёт httpOnly-cookie сессии. Каждый видит только свои диалоги и проекты. Диалоги, созданные до миграции `20261027_0007`, остаются без владельца и скрыты от всех.

```dotenv
AUTH_SESSION_TTL_SECONDS=1209600
AUTH_COOKIE_SECURE=false
```

`AUTH_COOKIE_SECURE=true` нужен, когда приложение открывают по HTTPS; по http браузер не вернёт Secure-cookie и вход не сработает.

## 3. Подготовка frontend

```bash
cd frontend
npm install
cd ..
```

## 4. Запуск backend

Откройте первый терминал, активируйте `.venv` и выполните из корня репозитория:

```bash
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Используйте один worker. Backend не запускает второй process на той же БД: с SQLite он удерживает OS file lock рядом с файлом базы, с Postgres — advisory lock на отдельном соединении. Startup reconciliation активных run безопасен только при единственном владельце executor.

При старте backend автоматически применяет Alembic migrations.

Ручное применение migrations, если потребуется:

```bash
python -m alembic -c backend/alembic.ini upgrade head
```

Перед первым применением security migration `20261012_0006`:

1. остановите старый backend;
2. сделайте backup `history.db`;
3. обновите backend и frontend одновременно;
4. примените migration и запустите backend;
5. не подключайте приложение к небезопасной backup-копии.

Миграция необратимо удаляет старые snapshots артефактов, редактирует tool/session/raw payload, очищает старые session IDs и требует явного context reset для существующих диалогов. При startup незавершённые `pending/running` запуски переводятся в `outcome_unknown`.

## 5. Запуск frontend

Откройте второй терминал:

```bash
cd frontend
npm run dev
```

Vite выводит адрес приложения, обычно:

```text
http://localhost:5173
```

Откройте его в браузере. Dev server проксирует `/api` и `/ws` на backend `localhost:8000`.

## 6. Быстрая проверка

Без входа API отвечает `401`:

```bash
curl -i http://localhost:8000/api/auth/me
```

Регистрация и запрос с полученной cookie:

```bash
curl -c cookies.txt -H "content-type: application/json" -d '{"email":"demo@example.com","password":"demo-password"}' http://localhost:8000/api/auth/register
```

```bash
curl -b cookies.txt http://localhost:8000/api/projects
```

Ожидаемый ответ для нового пользователя — `[]`. Во frontend после регистрации откроется главная страница CoreTeams; созданный проект и его запуски появятся в списке проектов.

## 7. Запуск тестов

Backend:

```bash
python -m pytest
```

Frontend lint и production build:

```bash
cd frontend
npm run test
npm run lint
npm run build
```

## 8. Остановка

В каждом терминале нажмите `Ctrl+C`.

## 9. Частые проблемы

### `claude` не найден

Убедитесь, что Claude Code CLI установлен и доступен в `PATH`:

```bash
claude --version
```

### Frontend открылся, но запросы не выполняются

Проверьте, что backend запущен на порту `8000`, а в терминале Vite нет ошибок proxy.

### Ошибка открытия SQLite

Проверьте права на запись в `backend/data`. Обычно каталог создаётся автоматически при применении migrations.

### Порт уже используется

Остановите процесс, занимающий `8000` или `5173`. Если изменить backend-порт, также обновите target proxy в `frontend/vite.config.ts`.
