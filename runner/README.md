# ctrunner — Claude Code в контейнере проекта

**Один изолированный контейнер на проект: Claude Code + Core Team Framework работает с данными клиента, пишет журнал событий и сообщает, работает сессия, зависла или упала.**

Runner держит долгоживущий клиент Claude Agent SDK, принимает команды (этап 1 — файловый inbox, этап 2 — WebSocket гейтвея), пишет каждое событие на диск до отправки и отклоняет с записью в журнал доступ инструментов за пределы проекта.
Спецификация — [`docs/2.4/spec.md`](../docs/2.4/spec.md), протокол — [`docs/2.4/protocol.md`](../docs/2.4/protocol.md), дизайн — [`docs/plans/2026-09-25-runner-design.md`](../docs/plans/2026-09-25-runner-design.md).

## Требования

- Docker 28+ (проверено на Docker Desktop, macOS).
- [uv](https://docs.astral.sh/uv/) 0.9+, Python 3.12 (uv поставит сам).
- Доступ к cliproxy: env-файл шлюза с `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`.
- Сеть при сборке образа: клонируется `github.com/noxxer/core-team`.

## Демо по шагам

Все команды — из каталога `runner/`.

```bash
uv sync
```

```bash
docker build -t coreteams-runner:dev .
```

```bash
source ~/.config/brotherhood/env.zsh
```

Ключ кладётся файлом в `~/.config/coreteams/secrets/<id>/` (0600) и попадает в контейнер только как `/run/secrets` (ro):

```bash
uv run sessions secrets init demo-a
```

```bash
uv run sessions start demo-a --data evals/fixtures/demo
```

```bash
uv run sessions status demo-a
```

Через ~15 с: `state=running health=healthy`. Отправить задачу и смотреть журнал:

```bash
uv run sessions send demo-a "Ответь одним словом: ок"
```

```bash
uv run sessions logs demo-a -f
```

Ожидаемые события: `session_started` → `turn_started` → `sdk`… → `turn_completed`.

Остановить и удалить вместе с томом проекта:

```bash
uv run sessions rm demo-a --volume
```

## Команды `sessions`

| Команда | Что делает |
|---|---|
| `secrets init <id>` | записывает `$ANTHROPIC_AUTH_TOKEN` в файл секрета проекта |
| `start <id> [--data DIR] [--stall-after S] [--image IMG]` | создаёт том `proj-<id>` и запускает `ct-<id>` с изоляцией |
| `send <id> <text>` | задача агенту; во время хода ставится в очередь |
| `answer <id> <fork_id> <label>...` | ответ на развилку `fork_question`, метки по порядку вопросов |
| `status <id>` | состояние Docker, HEALTHCHECK, число рестартов и `health.json` |
| `logs <id> [-f]` | журнал `events.jsonl` |
| `stop` / `restart` / `rm [--volume]` `<id>` | жизненный цикл контейнера |

`<id>` — `[a-z0-9][a-z0-9-]{0,39}`. В контейнер из окружения уходят только адрес шлюза и имена моделей; ключи — только файлом.

## Живость

| `phase` | Условие | `health` |
|---|---|---|
| `idle`, `awaiting_answer` | любое | `ok` |
| `working` | прогресс не дольше `STALL_AFTER_S` (720 с) назад | `ok` |
| `working` | прогресса нет дольше `STALL_AFTER_S` | `stalled` → Docker `unhealthy` |
| `failed` | SDK упал или диск полон | `crashed`, выход 1, Docker перезапускает |

Коды выхода: `78` — нет или отвергнут секрет, неверный env (без бесконечных рестартов); `1` — cliproxy недоступен дольше ~2,5 мин (повторы с backoff) или SDK упал.

## Ограничения этапа 1

- Нет гейтвея: сессия видна через `sessions logs/status`, команды — через inbox.
- Нет checkpoint и resume: после рестарта контейнера начинается новая сессия SDK (этап 2).
- Проверка путей в Bash — эвристика; граница изоляции — монтирование (в контейнере только свой том).
- Агент может прочитать ключ cliproxy из окружения CLI через Bash — поэтому ключ свой на проект и отзывается ротацией.
- Исходящий трафик не фильтруется.

## Разработка

```bash
uv run pytest -q
```

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests evals
```

E2E (Docker, собранный образ, env шлюза; стоит десятки тысяч токенов, ~2 мин):

```bash
source ~/.config/brotherhood/env.zsh && uv run pytest evals -m e2e -q
```

| Eval | Проверяет |
|---|---|
| `test_start` | старт с тестовым проектом, полный ход до `turn_completed` |
| `test_isolation` | выход через symlink в `/workspace/.runner` → `access_denied`, содержимое не утекло, чужой том не виден |
| `test_stall` | `--stall-after 30` + `sleep 200` → `health: stalled` и Docker `unhealthy` |

Без env шлюза e2e пропускаются.
