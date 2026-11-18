# PR #36: завершение диагностики и минимальная свёртка

Дата: 2026-11-12. Исправления третьего раунда review, без rollout.

## Что воспроизведено

- `drain()` завершал private loop до последнего чтения журнала; public loop отправлял финальный `usage.reported`, после чего отменял оставшиеся задачи. Новый live-WS тест воспроизвёл отсутствие private snapshot.
- `DiagnosticFeed` создавал второй полный `Translator`, сохраняющий роли и файловые пути, хотя диагностика использует только результат SDK.
- RED текущего source snapshot: 3 failed, 7 passed. Повторный запуск без обновления установленного wheel сначала повторил старые ошибки; это не результат нового кода. После установки свежего `./runner` проверки без `PYTHONPATH` используют исправленный код.

## Изменения

- `gateway.py`: единый monotonic deadline четыре секунды с момента первого `drain()`, внутри существующего внешнего бюджета пять секунд. Public flush и private final poll/ACK координируются; receive остаётся активным до завершения или deadline. Отсутствие ACK не блокирует shutdown бесконечно. При начале drain обычный retry backoff сбрасывается; последующие попытки остаются ограниченными общим deadline.
- Отсутствие private capability не добавляет ожидание диагностики. Ошибка optional sidecar не отменяет public flush.
- `harness_diagnostic.py`: только live/closed turn IDs. Не сохраняются роли, tool IDs, prompts, write-пути или артефакты. Точные duplicate/terminal semantics требуют tombstone ID, поэтому память O(число уникальных turns), не O(1); полное состояние каждого run больше не дублируется.
- `v0.py`: вынесен общий `usage_draft()` через прежние `_Out` и `_usage`. Одинаковые journal seq/turn/project дают прежний ordinal-zero event ID и прежние восемь полей usage. Public payload, ACK, cursor и дедупликация не изменены.

## Проверки

- Новые live-WebSocket случаи: результат непосредственно перед drain, accepted ACK до shutdown, отсутствующий ACK с bounded exit, старый receiver без capability.
- Сравнение минимальной свёртки с public Translator: отсутствие started, несколько SDK results, invalid/missing usage, fork question/answer, все причины terminal/interruption, повтор turn ID, result после terminal.
- Тысяча assistant/file/role событий не оставляет файловых или ролевых структур в DiagnosticFeed.
- Source focused suite (drain, feed, public v0/usage/artifacts, gateway client): **79 passed** до дополнительной old-capability параметризации.
- Точная offline runner-команда после свежего `pip install --no-deps ./runner`, без `PYTHONPATH`: **456 passed**. Последующее test-only усиление проверки тысячи событий: focused feed/drain **18 passed**.
- Ruff **0.17.4**: backend `--select E4,E7,E9,F` и runner `src tests evals` — PASS. `compileall` runner и `git diff --check` — PASS.
- Общий backend gate: `env -u PYTHONPATH /tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests -q` — **496 passed, 18 skipped**, 77.17 s. Этот snapshot включает исправления всех трёх исполнителей; skipped не выдаются за выполненные проверки.
- Независимый reviewer подтвердил оба runner-исправления: собственные **48 passed** и differential parity на 300 синтетических историях / 30 000 событий относительно старого Translator `2e39669`; идентификаторы, порядок и payload совпали. Эти числа — evidence reviewer, не мой повторный прогон.

## Ограничения

- Deadline — ограниченное завершение, не гарантия доставки при недоступном получателе: непринятый snapshot остаётся в retained journal для следующего запуска. При уничтожении журнала backfill невозможен.
- Унаследованный hot window 256 transient diagnostics может задержать более поздний snapshot; это исправление не создаёт новую очередь и не отбрасывает данные.
- Подключения только к локальному синтетическому WS; модели, production, Docker и пользовательские данные не использовались. Коммитов/push нет.
- Применены existing-code, python-rules и соответствующие references до правок/по diff. `security-review` и `reliability-review` недоступны: формальные checklist gates не запускались; приведённые проверки — конкретные correctness/reliability evidence, не полный аудит.
