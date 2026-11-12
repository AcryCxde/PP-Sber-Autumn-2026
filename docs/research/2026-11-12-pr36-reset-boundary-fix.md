# PR36: граница reset в истории — проверка и минимальное исправление

Статус: дефект воспроизведён; после технического разрешения координатора реализована чистая presentation-проекция. Backend, DTO и сохранённая история не изменялись.

## Подтверждённые факты

- `backend/repository.py:350–359`: пользовательское сообщение записывается при acceptance и получает durable sequence.
- `backend/repository.py:469–489`: `context_reset` записывается только при успешном admission, поэтому получает следующий sequence. Это намеренно сохраняет прежний контекст, если запуск не допущен.
- `backend/repository.py:1346–1359` и `backend/api/history.py:123–136`: history API возвращает сообщения в порядке durable sequence.
- `frontend/src/App.tsx:229–245`: история без перестановки преобразуется в UI-события.
- `frontend/src/App.tsx:499–507`: live `context.reset` добавляется в конец после уже показанного `run.accepted`.
- `frontend/src/App.tsx:906–913`: массив напрямую определяет расположение reset-разделителя и пользовательского сообщения. Это действующий chat UI: `frontend/src/coreteams/Shell.tsx:86` монтирует App.

## Runtime-проверка

В отдельной временной SQLite созданы только синтетические владелец, диалог и одно старое сообщение. Через существующие `accept_run(..., requested_context_mode='reset')`, `policy_transaction(... admit_run(...))` и `list_messages` получено:

```text
before_admission: [(1, text, old context), (2, text, first new context)]
after_admission:  [(1, text, old context), (2, text, first new context),
                  (3, context_reset, Контекст Claude был явно сброшен.)]
```

Значит, live и восстановленная история показывают первый запрос новой сессии перед её разделителем. Проба использовала `python -I`, существующий тестовый venv, одноразовую локальную SQLite; БД закрыта и временный каталог удалён. Модели и стенд не вызывались.

## Минимальное предложение

Не менять сохранённые sequence, ID, порядок public events, acceptance/admission или session state. Не создавать marker при acceptance: это вернуло бы ранее исправленную ошибку при недопуске.

Добавить чистую presentation-проекцию массива chat events непосредственно перед render:

1. Только уже существующий подтверждённый `context.reset` с `run_id` считается основанием для визуальной границы.
2. Найти первое пользовательское сообщение с тем же `run_id` и показать этот marker непосредственно перед ним.
3. Если пользовательского сообщения/доказанной связи нет, сохранить фактическое место marker; ничего не угадывать.
4. Остальные сообщения и события сохраняют порядок; canonical массив, sequence tracker, replay/dedup и message IDs не переписываются.
5. Один и тот же helper применяется для history/live через их общий render-массив. Дополнительных DTO-полей не требуется: `run_id` уже есть в обоих путях.

Координатор отдельно разрешил это локальное изменение визуальной интерпретации существующих данных. `frontend/src/chatPresentation.ts` реализует проекцию, `frontend/src/App.tsx` вызывает её только непосредственно перед render. Идентичные replay-marker ID визуально дедуплицируются; distinct ID не объединяются. Альтернатива с новым `before_message_id` в API/event DTO не потребовалась.

## Обязательные регрессии

- История `[old, reset-user, reset-marker, assistant]` отображается `[old, reset-marker, reset-user, assistant]`; исходный массив и sequence неизменны.
- Live: сначала пользовательский запрос без marker; после committed reset marker появляется перед ним. Ответ модели остаётся после запроса.
- Pending/denied reset без committed marker не рисует разделитель и не меняет контекст.
- Несколько последовательных reset-run: каждый marker относится только к своему первому user message.
- Повтор marker/replay не создаёт дополнительной визуальной границы; поздний marker не перескакивает к другому run.
- Marker без `run_id` или без подходящего user message сохраняет исходное место.
- Существующие backend reset-admission/rollback/replay тесты остаются зелёными; frontend test/build/lint обязательны.

## Ограничения

RED: targeted Vitest завершился ошибкой отсутствующего модуля `chatPresentation` до реализации. GREEN: шесть новых pure-helper тестов прошли. Общий frontend-прогон: **89 passed**; build, lint и scoped diff-check — PASS. Общий набор включает параллельные изменения другого исполнителя; мои собственные регрессии — ровно шесть. `backend/tests/test_reset_admission.py`: **5 passed**. Использованы команды `npm exec --yes --package=node@22 -- npm --prefix frontend {test,run build,run lint}` и `/tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests/test_reset_admission.py -q`.

Браузерный прогон не выполнялся: live/history подтверждены общей чистой render-проекцией и unit-тестами последовательного добавления committed marker. Web-product не менялся и не является потребителем этого helper; его повторные сборки не выполнялись по уточнению координатора. `typescript-rules`, `security-review`, `reliability-review` недоступны; формальные checklist-аудиты не выполнялись. Existing-code reference повторно сопоставлен фактическому diff: Boundary-изменение presentation согласовано, durable/API/event контракты сохранены. Python/SQL production diff отсутствует, дополнительные language/SQL reference rows не срабатывают. Процессы приложения, модели и контейнеры не запускались; коммитов/push нет.
