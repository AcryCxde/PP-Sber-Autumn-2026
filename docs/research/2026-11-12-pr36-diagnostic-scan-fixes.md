# PR36: чтение больших диагностических журналов

Статус: исправлены P2 задержки сканирования и P3 повторного JSON-разбора. Только runner; без изменения публичных событий, ID, ACK, receipt или лимитов shutdown.

## Доказанные проблемы

- Полная страница DiagnosticFeed была ограничена числом строк, а не result-событий. Gateway после каждой такой страницы ожидал polling-интервал, даже если следующая часть истории уже была доступна.
- Decimal-разбор выполнялся второй раз для каждой строки, включая сообщения и tool inputs, не относящиеся к диагностике.

## Исправления

- Feed сообщает `scan_pending`, только когда чтение остановлено границей страницы/байтов и в файле остаётся хвост. EOF и незавершённая строка не требуют немедленного повторного чтения.
- При свободном frame-window gateway продолжает полные страницы с `await asyncio.sleep(0)`: I/O остаётся в worker thread, между ограниченными страницами есть cooperative yield. При EOF, неполном хвосте либо полном окне остаётся обычное ожидание.
- Точный повторный JSON-разбор с Decimal выполняется только для допустимого live SDK result. Non-result и result закрытого turn пропускаются после первого разбора; точная десятичная стоимость сохраняется.
- Существующие lease rotation, окончательные receipts, публичные identity и четырёхсекундный общий drain deadline не изменены.

## Проверки

RED: **5 failed, 15 passed** — оба варианта normal/drain не достигали свежего result до первого polling-wait; полное окно останавливалось на 255 frames; non-result получал лишний Decimal-разбор; отсутствовал признак полного scan-page.

GREEN focused:

```sh
PYTHONPATH=runner/src /tmp/coreteams-admin-plan-venv/bin/python -m pytest \
  runner/tests/test_diagnostic_scan.py runner/tests/test_harness_diagnostic.py \
  runner/tests/test_diagnostic_drain.py runner/tests/test_v0.py -q --tb=short
```

**58 passed**. Новая регрессия создаёт 100000 синтетических non-result строк и свежий result, проверяет normal/drain, отсутствие polling-паузы до его отправки и не менее 390 cooperative yields. Полное окно 256 ожидает, а не вращается в busy-loop. EOF/неполная строка проверены отдельно. Decimal сохраняет значение `0.12345678901234567890123456789`; result после terminal не получает второго разбора.

Финальная штатная команда после свежего `pip install ./runner`, без `PYTHONPATH`:

```sh
env -u PYTHONPATH /tmp/coreteams-admin-plan-venv/bin/python \
  -m pytest runner/tests -m 'not e2e' -q --tb=short
```

**466 passed, 21.76 s**, exit 0. Ruff **0.17.4** (`runner/src`, `runner/tests`, `runner/evals`), compileall и `git diff --check` — PASS.

## Границы

- Не обещается доставка произвольно большого журнала за четыре секунды либо при недоступной сети; сохранённый журнал остаётся условием восстановления.
- Большая отдельная строка всё ещё разбирается целиком один раз; это не новый streaming JSON parser и не изменение journal-формата.
- Проверки используют только синтетические записи и offline-model boundaries. Производство, ключи, реальные модели, контейнеры, commit/push не использовались.
- Existing-code classified Boundary: сохранены все wire-контракты. Python references main/developer/idioms/testing/errors-common/legacy-errors прочитаны до изменений и применены к actual diff.
- Skills `security-review` и `reliability-review` недоступны; checklist-гейты не выполнялись. Notes: parse и Decimal точность, bounded page/window, deadline, EOF и shutdown проверены перечисленными тестами; это не полный аудит.
