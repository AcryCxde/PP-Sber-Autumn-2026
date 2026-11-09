## PPS-244 — сквозная проверка форматов и storage

- **Run ID:** `PPS-244-20261109T091930Z-0856adaa`
- **Commit SHA:** `2c47dbfc7ae6f112380bf6f00372dda99ce4ad89`
- **Связи:** PPS-234 (`docs/tests/PPS-234-storage-integration.md`), PR #20 (`https://github.com/AcryCxde/PP-Sber-Autumn-2026/pull/20`)
- **Матрица до Run:** `docs/format_matrix.md`

### Чек-лист

- [x] Ожидаемый формат согласован до Run: `docs/format_matrix.md`.
- [x] Получены файлы нужного расширения; проверены размер, байты и структура. MIME endpoint отдельно зафиксирован как FAIL.
- [x] Проверено первое и повторное чтение; SHA-256 совпали.
- [x] HTML проверен отдельным независимым прогоном.
- [x] Ошибки и ограничения показаны пользователю: HTTP 415 + понятный JSON `detail`.

### DOCX

| Шаг | Статус | Результат |
|---|---|---|
| Генерация сервисом | FAIL | API генерации формата отсутствует; использован валидный синтетический DOCX fixture |
| Сохранение и первое чтение | PASS | 937 bytes; valid ZIP/OPC structure and word/document.xml |
| MIME | FAIL | ожидался `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, получен `application/octet-stream` |
| Повторное чтение | PASS | `482e97f2371a7b5b643016d939d0ee1e965f4b619c1876c7d2ca89877c1a9a4e` = `482e97f2371a7b5b643016d939d0ee1e965f4b619c1876c7d2ca89877c1a9a4e` |

### HTML

| Шаг | Статус | Результат |
|---|---|---|
| Генерация отдельным Run | PASS | terminal event `run.completed` |
| Сохранение и первое чтение | PASS | 213 bytes; UTF-8 and basic HTML structure are valid |
| MIME | FAIL | ожидался `text/html`, получен `application/octet-stream` |
| Повторное чтение | PASS | `c78c1c41f6adefef8c5c0d85d67b241d6523f1443089bba5686369096b9e2a57` = `c78c1c41f6adefef8c5c0d85d67b241d6523f1443089bba5686369096b9e2a57` |

### Негативный сценарий

- **PASS** — `.exe` отклонён с HTTP `415` и JSON `{"detail": "Этот тип файла не поддерживается"}`; 500 не возник.

### Ограничения / итог

- Byte-preserving storage и повторное чтение DOCX: **PASS** через material storage.
- Text artifact storage и повторное чтение HTML: **PASS**.
- Формат-специфичный MIME при скачивании: **FAIL** — оба endpoint возвращают `application/octet-stream`.
- Генерация DOCX сервисом: **FAIL / capability absent** — текущий `Artifact.content` текстовый и отдельного format-generation API нет.
- PR #20 добавляет durable artifact confirmation, но на проверенном SHA не влит и format/MIME gap не закрывает.

### Артефакты для прикрепления

- `artifacts/test_result.docx`
- `artifacts/test_result.html`
- `artifacts/pps_244_evidence.json`
- `artifacts/pps_244_yougile_report.md`
- `docs/format_matrix.md`
