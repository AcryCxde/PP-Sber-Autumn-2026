# PR36: исправления frontend по high-review

Статус: исправлены два подтверждённых P2; коммит/push не выполнялись.

## 1. Конфликт уже достигнутого состояния

Ранее `policy_version_conflict` с текущим состоянием, совпадающим с желаемым, разрешал новое подтверждение и приводил к циклу `policy_state_conflict`.

- Reducer переводит такую форму в терминальное `satisfied`: показывает текущее состояние, запрещает новую мутацию и повторную подготовку ключа, позволяет закрыть форму.
- Отдельный `409 policy_state_conflict` без `current` — подтверждённый сервером no-op: форма закрывается, список обновляется, пользователь видит сообщение «состояние уже установлено», а не сообщение о выполнении новой операции.
- Неопределённые сетевые/503-ответы по-прежнему сохраняют отправленный payload/key для точного retry; их классификация не расширена до произвольных 409.

## 2. Снимок при изменении фильтра/страницы

Hook теперь хранит снимок вместе с идентичностью loader. Уже во время render нового запроса чужой снимок скрывается loading-состоянием. Поэтому предыдущие строки не отображаются под новым фильтром даже до эффекта, а ошибка нового запроса не возвращает старые данные.

Обновление того же запроса по-прежнему сохраняет последний успешный снимок с признаками устаревания/ошибки. Старый controller отписывается, отменяется и удаляется прежним механизмом generation/dispose.

## Доказательства

RED:

```sh
npm exec --yes --package=node@22 -- npm --prefix frontend test -- src/coreteams/adminUsersState.test.ts src/coreteams/adminPolling.test.ts
```

До исправлений: **4 failed, 14 passed** — оба направления freeze/unfreeze со статусом conflict вместо satisfied, неверная uncertainty для state-conflict и отсутствующее безопасное разделение снимков.

GREEN:

```sh
npm exec --yes --package=node@22 -- npm --prefix frontend test
npm exec --yes --package=node@22 -- npm --prefix frontend run build
npm exec --yes --package=node@22 -- npm --prefix frontend run lint
```

**61 passed**, TypeScript/Vite build PASS, ESLint PASS. Проверка scoped `git diff --check` PASS.

Chrome с настоящим Vite frontend на отдельном порту 5191 и синтетическим intercepted API проверил:

- конфликт версии с уже достигнутой заморозкой не предлагает новую операцию; отправлен ровно один PATCH, форма закрывается;
- `policy_state_conflict` без current закрывает форму без блокировки и без повторной мутации;
- изменение поиска сразу убирает старые строки во время задержанного запроса; последующий 503 не восстанавливает их.

Скрипт: `/tmp/coreteams-pr36-frontend-regressions.mjs`; результат PASS. Скриншот `/tmp/coreteams-pr36-frontend-regressions.png` визуально просмотрен: новый запрос и сообщение ошибки без прежних записей. Это не приёмка с реальным backend. Browser закрыт; собственный Vite PID проверен по команде и остановлен, порт 5191 свободен (exit143 — ожидаемое завершение SIGTERM).

## Ограничения и правила

Применены existing-code и его reference до правок и по фактическому diff; изменение Module, backend API не изменён. Проверены определения, callers, соседние polling/retry пути и регрессионные тесты. Для browser применён run, готового project run skill не найдено; использован существующий локальный Playwright/Chrome без установки новых зависимостей.

Плагины `typescript-rules`, `security-review`, `reliability-review` недоступны; соответствующие checklist gates **не выполнялись**, полный аудит не заявляется. Security notes: неопределённый запрос не теряет ключ/приватную причину; Reliability notes: новые query-снимки изолированы, same-query stale-refresh сохранён. Реальные модели, backend/runner/production и lockfiles не затрагивались.
