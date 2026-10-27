# React B: инструкция для Димы — gateway adapter

## Цель

Связать выбранный React B (`web-product`) с существующим gateway, не создавая второй параллельный протокол и не выдавая локальные данные за серверные.

## Первый срез

Поддержать только:

1. `POST /api/projects` — связать React B project с server-side conversation.
2. `POST /api/projects/{projectId}/runs` — принять идемпотентный запуск и поставить его в существующий executor.
3. `GET /api/projects/{projectId}/runs/{runId}` — вернуть V2 snapshot, собранный из публичного состояния gateway.

Маршруты V2 — отдельный adapter-router. Existing `/api/conversations`, `/api/runs/*/events` и `/ws` не меняют семантику.

## Неподдерживаемое в первом срезе

- файлы и SHA-256 ingest;
- `saved=true`, версии документов, approval receipt;
- ZIP;
- утверждение, что выбранные роли были исполнены;
- question/answer в том же Run, пока runner lifecycle не переведён из legacy completed в `waiting_for_input`.

Для этих операций возвращать явный код ошибки/недоступности. Не использовать фиктивные ID, таймеры, артефакты или статусы успеха.

## Обязательные правила

- `Idempotency-Key` обязателен для POST Run; scope — actor + project после появления auth.
- timeout и сеть — неизвестный исход, без автоматического повтора со стороны сервера или UI.
- Snapshot содержит только allowlisted public events и монотонную revision.
- До auth/tenant boundary контур не считается общим стендом и не принимает пользовательские данные.
- Статусы role execution появляются исключительно из подтверждённых runner events.

## Готово, когда

Один project создаётся повторяемо, один Run запускается повторяемо, React B получает фактический `queued/running/completed/failed/outcome_unknown` snapshot после refresh. Проверки покрывают конфликт ключа, чужую пару project/run, отказ executor и отсутствие неподдерживаемых возможностей.
