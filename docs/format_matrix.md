# PPS-244 — матрица форматов и storage-контракт

Проверено по коду на SHA `2c47dbfc7ae6f112380bf6f00372dda99ce4ad89` до запуска `PPS-244-20261109T091930Z-0856adaa`.

## Границы проверки

В текущем сервисе нет отдельного HTTP API «сгенерировать DOCX/HTML». Есть две разные файловые границы:

1. **Материалы пользователя** — `POST /api/conversations/{conversation_id}/files`, бинарное содержимое хранится в `ConversationFile.content` и скачивается через `GET /api/conversations/{conversation_id}/files/{file_id}`.
2. **Результаты агента** — файл, созданный агентом, сохраняется как `Artifact.content` (UTF-8 текст) и скачивается через `GET /api/runs/{run_id}/events/{event_seq}/file`.

Поэтому бинарный DOCX можно без потерь сохранить как материал, но текущая ветка не умеет сохранить его как сгенерированный `Artifact`: artifact pipeline принимает `str`, а не произвольные bytes. HTML может быть текстовым artifact, но `.html` запрещён как входной материал.

## Входные форматы (материалы)

Backend проверяет расширение имени файла, а не заявленный клиентом MIME. Максимум одного файла — 5 MiB; также действуют лимиты 20 файлов и 20 MiB на набор материалов.

| Расширение | Канонический MIME | Статус входа |
|---|---|---|
| `.pdf` | `application/pdf` | поддерживается |
| `.doc` | `application/msword` | поддерживается |
| `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | поддерживается |
| `.ppt` | `application/vnd.ms-powerpoint` | поддерживается |
| `.pptx` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | поддерживается |
| `.txt` | `text/plain` | поддерживается |
| `.md` | `text/markdown` | поддерживается |
| `.png` | `image/png` | поддерживается |
| `.jpg`, `.jpeg` | `image/jpeg` | поддерживается |
| `.csv` | `text/csv` | поддерживается |
| `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | поддерживается |
| `.html`, `.htm` | `text/html` | **не поддерживается как входной материал** |

## Выходные форматы (artifact)

| Формат | Канонический MIME | Текущий контракт |
|---|---|---|
| UTF-8 текст, включая `.html` | `text/html` для HTML | сохраняется в `Artifact.content`; download endpoint на текущем SHA отвечает `application/octet-stream` независимо от расширения |
| CSV | `text/csv` | текстовый artifact допустим; отдельный PPS-239 validator проверяет UTF-8, заголовок, строки, обязательные поля и NaN |
| JSON | `application/json` | текстовый artifact допустим; отдельный PPS-239 validator проверяет UTF-8, object/non-empty object array, обязательные поля и null/NaN |
| DOCX | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | **не поддерживается как сгенерированный artifact**: бинарные bytes проходят через строковый UTF-8 контракт |
| Прочие бинарные файлы | MIME по типу | **не поддерживаются без потерь как artifact** |

CSV/JSON здесь являются форматами локального artifact validator (`artifact_validator.py`), а не параметрами HTTP generation API: такого API в текущем сервисе нет. JSON также отсутствует во входном allowlist материалов.

Таким образом, точный MIME на download endpoint сейчас не соответствует каноническому MIME ни для DOCX, ни для HTML: сервис принудительно выставляет `application/octet-stream`. Это ограничение фиксируется тестовым протоколом, а не маскируется клиентским MIME при upload.

## Явно неподдерживаемые входные форматы

Как минимум: `.exe`, `.bin`, `.html`, `.htm`, `.js`, `.zip`. Любое расширение вне allowlist выше отклоняется до записи в storage. PDF **поддерживается как входной материал**, поэтому относить его к неподдерживаемым на текущем SHA неверно.

## Пользовательская ошибка

Для неподдерживаемого расширения upload endpoint возвращает:

```http
HTTP/1.1 415 Unsupported Media Type
Content-Type: application/json

{"detail":"Этот тип файла не поддерживается"}
```

Это ожидаемая валидационная ошибка: она не является `500`, не раскрывает внутренний путь или stack trace и содержит понятное пользователю ограничение. Связанные ошибки: пустой файл — `422 {"detail":"Пустой файл"}`, файл больше 5 MiB — `413 {"detail":"Файл больше 5 МБ"}`.

## Источники контракта

- `backend/materials.py` — allowlist и лимиты входных материалов.
- `backend/api/conversation_files.py` — upload/download материалов и JSON-ошибка.
- `backend/main.py`, `backend/repository.py`, `backend/models.py` — строковый artifact pipeline.
- `backend/api/history.py` — скачивание artifact и фиксированный `application/octet-stream`.
- `backend/tests/test_pps234_storage_integration.py` — связанный storage probe PPS-234.
- PR #20 (`refs/pull/20/head` = `2c8fe917d346b6b4cf6e5492acec7d36e03fb05b`) усиливает durable confirmation, но не добавляет DOCX/HTML generation API и не меняет бинарную границу artifact.
