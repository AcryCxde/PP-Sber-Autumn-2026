# PR36: deadline изменения AI-доступа

Дата: 2026-11-12. Scope: frontend mutation deadline; без изменения API backend, флагов и зависимостей.

## Подтверждённая проблема

`AdminUsers.submit()` ожидал PATCH без ограничения времени. Зависший fetch либо чтение тела ответа оставляли форму в submitting; безопасный повтор с прежним ключом был недоступен.

## Исправление

- Только `adminApi.changeAiAccess` получает deadline 30 секунд. Внутренний AbortController отменяет fetch; Promise.race ограничивает всю попытку, включая чтение JSON, даже если реализация игнорирует AbortSignal.
- Deadline даёт обычную неопределённую ошибку, а не подтверждённый отказ. Существующий reducer сохраняет исходные reason/expected_version/idempotency key и разрешает точный повтор. Причина и отмена остаются заблокированы до разрешения результата.
- Отмена внешним сигналом при уходе/размонтировании завершает попытку и очищает таймер/listener. Уже отменённый запрос вообще не отправляется.
- UI проверяет identity конкретного AbortController перед применением результата и очисткой busy. Опоздавшее завершение первой попытки не влияет на повторную попытку или новую форму.
- Abort не доказывает отмену операции на сервере. Повтор использует прежний ключ; успешный результат не показывается оптимистически.

## Доказательства

RED: `npm exec --yes --package=node@22 -- npm --prefix frontend test -- src/coreteams/adminApi.test.ts --testTimeout=1000` → **3 failed, 5 passed**: зависший fetch, зависшее чтение JSON, отмена игнорирующего сигнал fetch.

GREEN:

```sh
npm exec --yes --package=node@22 -- npm --prefix frontend test
npm exec --yes --package=node@22 -- npm --prefix frontend run build
npm exec --yes --package=node@22 -- npm --prefix frontend run lint
git diff --check
```

Первоначальный результат: **70 passed**, build/lint/diff-check PASS. После исправления позднего 401 (ниже): **72 passed**, build/lint/diff-check PASS. Новые проверки дополнительно покрывают уже отменённый signal, очистку таймера после успеха и неизменность reducer payload/key после deadline.

Chrome: настоящий Vite frontend с синтетическим API и искусственным fetch, игнорирующим отмену. Playwright clock продвинут на 30001 мс. PASS: снятие submitting, uncertain-текст, блокировка редактирования/отмены/Escape, повтор с побайтово тем же body и headers/key, успешный receipt; поздний 409 первой попытки не открывает конфликтную форму; размонтирование отменяет новую зависшую попытку и не восстанавливает диалог. Скрипт `/tmp/coreteams-admin-mutation-deadline.mjs`; скриншот `/tmp/coreteams-admin-mutation-timeout.png` прочитан и просмотрен. Это не новый real-backend acceptance и не тест реальной модели.

Cleanup: собственный Vite PID 11548 на loopback 5197 проверен по команде и остановлен; Chrome закрыт в finally. Backend, БД и чужие процессы не затрагивались.

## Дополнительное замечание ревью: поздний 401 отменённой попытки

Подтверждено RED двумя тестами: deadline/unmount → успешный повтор → поздний 401 исходного fetch вызывал глобальный unauthorized handler до компонентной проверки. Сам Promise.race не отменяет побочные эффекты продолжающегося запроса.

`frontend/src/auth.ts` теперь после fetch и **до** обработки 401 проверяет `init.signal.throwIfAborted()`. Это сохраняет обычное поведение активного 401, но исключает глобальное уведомление от отменённой попытки, включая уже отменённый polling. Сигнатура `apiFetch` и обработка неотменённых запросов не менялись.

RED: **2 failed, 9 passed** в `adminApi.test.ts`. GREEN: **72 frontend tests passed**, build/lint/diff-check PASS. Тесты подтверждают отсутствие unauthorized notification при late 401 после timeout/unmount и сохранение notification для последующего активного 401.

Chrome-скрипт повторён с настоящими late 401 вместо 409/503: после успешного точного retry поздний 401 не убирает страницу/уведомление успеха; после размонтирования поздний 401 не разлогинивает пользователя (кнопка «Выйти» остаётся). PASS. Собственный повторный Vite PID 15988 на 5197 проверен по команде и остановлен; Chrome закрыт.

## Правила и ограничения

Изучены определения, callers, reducer, соседний polling deadline, существующие API/state tests, auth lifecycle. Изменение Module: сигнатура и wire-контракт сохранены; deadline намеренно меняет только окончание зависшей попытки. `existing-code` и reference-routing применены до правок и к фактическому diff; `run` применён для браузерной проверки.

Plugins `skill-issue-typescript` (`typescript-rules`), `security-review` и `reliability-review` недоступны. Их формальные checklist gates не выполнялись. Security notes: private payload не логируется; полномочия не расширены. Reliability notes: физический запрос, игнорирующий abort, может продолжаться, но его поздний результат изолирован; серверный исход до успешного точного replay остаётся неопределённым.

Коммитов, push, деплоя, изменения lockfiles и модельных вызовов нет.
