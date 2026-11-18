# PR36: отказ операции, период и rollout

Дата: 2026-11-12. Изменения локальные, без commit/push/deploy.

## P2: операционный 403 не отзывает административный доступ

Подтверждён RED: `useAdminFailure` отзывал доступ при любом `AdminApiError(403, ...)`, включая `invalid_admin_origin`, возвращаемый до мутации при сохранённой роли admin. Новый тест: 2 failed / 4 passed до исправления.

Исправление: только известные операционные коды `invalid_admin_origin` и `self_freeze_forbidden` не отзывают доступ ко всему разделу. Форма показывает безопасное объяснение отказа, снимает неопределённость и разрешает исправление/закрытие; данные не выдаются за сохранённые. Прочие 403 (`admin_required`, обычный `request_failed` от server role guard и неизвестные коды) продолжают отзывать доступ. Активный 401 также сохраняет прежнее поведение. Read-only polling по-прежнему прекращается и очищает данные при любом 403. Late-response и deadline guards не изменялись.

Файлы: `frontend/src/coreteams/adminApi.ts`, `adminHooks.tsx`, `adminUsersState.ts`, новый `adminFailure.test.tsx`.

## P3: общий алгоритм периода

`AdminShared.PeriodControl.apply` теперь вызывает `fixedPeriod(Number(days))`; начало и конец используют один момент времени. Custom interval по-прежнему проходит `parseExplicitPeriod`. Проверены определение helper, оба существующих потребителя и тест фиксированных границ/DST; календарная семантика не менялась.

## P1 rollout: противоречие с ранее утверждённым требованием

Исходная implementation specification прямо требует unavailable при выключенном UI flag и запрещает возвращать старую AdminPanel:
- `docs/plans/2026-11-12-admin-redesign-implementation.md:333,337,343`;
- `docs/research/2026-11-12-admin-rollout-checklist.md:10,15`.

`web-product/src/main.tsx` этому соответствует: старый hash перенаправляет в новую оболочку независимо от её флага. Координатор после проверки источников подтвердил сохранить fail-closed/no legacy fallback. Поэтому прежнюю панель не восстанавливали и default flag не включали. Выпуск требует согласованной сборки основного frontend с `VITE_ADMIN_SHELL_ENABLED=true` после staging gates. При неверном порядке выпуска или сборке с false админская UI-доступность теряется намеренно; соответствие утверждённому контракту не устраняет этот release-impact. Откат UI — unavailable либо последняя enforcement-compatible сборка. Review thread автоматически не разрешался. Изменение стратегии fallback требует отдельного продуктового решения.

## Проверки и ограничения

RED: 2 failed / 4 passed в новом `adminFailure.test.tsx`. Итоговый полный frontend-прогон: **89 tests passed**, build/lint PASS; в параллельной рабочей копии к этому моменту появился также чужой `chatPresentation.test.ts`, поэтому это общий snapshot, не 89 новых собственных тестов. Собственный новый файл содержит 7 сценариев: два операционных отказа без revoke, три настоящих/неизвестных permission-denial с revoke, прямой read-403 с остановкой polling, активный unauthorized. Scoped diff-check — PASS. Команды: `npm exec --yes --package=node@22 -- npm --prefix frontend test`, аналогично `run build`, `run lint`. До параллельного добавления chat-тестов промежуточный полный прогон был 82 passed.

Read-only проверка CI для опубликованного `6f8647f`: classify/build/python/frontend/web-product/compose/schema-postgres/gate SUCCESS, publish SKIPPED (наблюдение перед началом локальных исправлений, 2026-11-12; это не CI новых изменений).

Применён `existing-code`, reference routing: module boundary, определение/потребители/errors/tests прочитаны перед edit и scoped diff проверен после изменений. `typescript-rules`, `security-review`, `reliability-review` недоступны; их checklist gates не выполнялись. Security/reliability notes: server auth/Origin guard не изменены; неизвестный 403 остаётся fail-closed; network/503 uncertainty не ослаблена. Browser acceptance для этой локальной правки не запускалась; SSR hook-probe и controller tests не выдаются за браузерную проверку.
