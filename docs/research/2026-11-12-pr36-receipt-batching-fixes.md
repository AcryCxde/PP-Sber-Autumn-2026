# PR36: пакетные sidecar receipts и общий lifecycle-fold

## Область и подтверждённые проблемы

Исследованы runner `gateway.py`, `harness_diagnostic.py`, `v0.py`, связанные tests и protocol contracts. До исправления каждая историческая diagnostic frame вызывала отдельные `to_thread(receipts.contains)` и открытие SQLite; каждый ACK записывался отдельной транзакцией. Диагностика отдельно повторяла правила terminal/fatal/state-corrupt публичного Translator.

## Исправление

- `DiagnosticReceipts.known_ids` проверяет ограниченную страницу ID одним соединением и параметризованным SELECT; `record_many` сохраняет снимок mailbox одной транзакцией. Пустые страницы не открывают SQLite. Горячее окно остаётся 256; gateway делает один переход в поток для страницы и один для снимка ACK.
- Повтор того же receipt идемпотентен; другое terminal status того же ID вызывает конфликт с откатом всего batch. Pending очищается только после успешной durable-записи. Sidecar хранит только ID/status; wire capabilities, canonical public payload, source IDs и ACK/cursor не менялись.
- `turn_lifecycle.py` содержит общий минимальный fold и `FailReason`. Его используют публичный Translator и DiagnosticFeed. В fold нет ролей, путей, исходных SDK payload; только ordered ID/tombstones для точного replay. Существующий health-модуль `liveness.py` оставлен без изменений.
- Pinned fixture `runner/tests/journals/translator_f945365.py.txt` побайтово взята из `f945365:runner/src/ctrunner/v0.py`. Сравнение 100 синтетических историй / 15 000 событий проверяет public draft IDs/order/types/payload и совпадение diagnostic source IDs. Матрица malformed failures проверяет точный тип/текст исключений старого Translator, включая отсутствующий/null/пустой/non-string reason и fatal valid failure.

## Проверки

1. RED новых batch API: **2 failed** (`AttributeError` до реализации).
2. Focused existing/batch/lifecycle/drain: **76 passed**; первоначальные batching/parity tests **4 passed**.
3. Измерение через счётчик настоящих SQLite connections: **1024 уже подтверждённых результата → 5 lookup connections** (страницы включают start-событие), вместо 1024 отдельных lookup connections. **256 ACK → 1 connection/transaction**, вместо 256. Это call-count, не обещание времени исполнения.
4. Первый общий запуск выявил коллизию имени с существующим `liveness.py`: **8 collection errors**. Новый fold перемещён в уникальный `turn_lifecycle.py`, оригинальный health-модуль восстановлен из HEAD; после этого **470 passed**. Этот промежуточный результат не заменяет последний прогон.
5. Дополнительная exception-parity RED: **4 failed, 5 passed**; восстановлен точный legacy error message. Последний полный runner после свежего `pip install ./runner`:

```sh
env -u PYTHONPATH /tmp/coreteams-admin-plan-venv/bin/python \
  -m pytest runner/tests -m 'not e2e' -q --tb=short
```

**478 passed, 21.48 s**. После этого изменён только перенос длинной строки теста; повтор batch/parity **12 passed**.

6. Ruff **0.17.4** `ruff check runner/src runner/tests runner/evals`, compileall и `git diff --check` — PASS. Допустимые S608 подавления документируют только генерируемые `?` placeholders; ID/status передаются отдельными bound parameters.
7. Координатор сообщил независимую перепроверку исправлений и точных legacy exception types/messages; её результаты не выдаются за собственный запуск.

## Ограничения

Размер возвращаемой страницы и mailbox ограничен; tombstones остаются O(число turns), чтобы duplicate start не воскрешал terminal turn. Количество транзакций ограничено страницами/mailbox snapshots, а не всем журналом. Кооперативный drain по-прежнему имеет общий срок; произвольный журнал, задержки диска/сети не дают гарантии доставки за четыре секунды. SQLite timeout и ошибка sidecar не превращаются в успешный receipt; recovery опирается на сохранённый журнал.

Применены existing-code/Python reference routing и доступные DB rules (здесь SQLite, PostgreSQL runtime не применяется). Skills `security-review`/`reliability-review` недоступны; формальные checklist gates не выполнялись. Security/reliability notes: тестировались atomic rollback/conflict, storage failure, replay, batching и lifecycle parity. Модели, production, контейнеры, коммиты и push не использовались.
