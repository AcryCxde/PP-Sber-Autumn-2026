# React B: инструкция для Филиппа — storage/API

## Цель

Добавить серверную основу для возможностей React B, которые gateway adapter не может создать сам: файлы, версии, подтверждённое сохранение, ZIP и восстановление.

## Нужные API и данные

1. `POST /api/projects/{projectId}/files`
   - принять bytes, `clientFileId`, SHA-256 и idempotency key;
   - после проверки/сканирования вернуть `fileId` и тот же SHA-256;
   - при ошибке не разрешать старт Run.
2. Persistent project metadata: цель, контекст, выбранные роли, план и связь project ↔ owner/tenant.
3. Immutable document versions: `documentId`, `artifactId`, stage, version, producing `runId`, content/checksum, `savedAt`.
4. Durable save acknowledgement: `saved=true` разрешён только после commit.
5. Independent approval receipt: `receiptId`, `artifactId`, version, `approvedAt` — не флаг внутри Run.
6. `GET /api/projects/{projectId}/runs/{runId}/artifacts/{artifactId}/zip`
   - только для подтверждённой immutable-версии;
   - `application/zip`, manifest и проверяемая точная версия.
7. Project/read/list API для восстановления в новом браузере или после restart.

## Ограничения

- Не принимать browser local storage как доказательство server persistence.
- Не делать billing, product quota gates или «180 демо-кредитов» в API: это перенесено в пользовательский замер.
- Не исполнять инструкции из файлов; bytes — недоверенный ввод.
- До auth, CSRF/CORS политики и tenant isolation не открывать общий адрес для пользователей.

## Критерии готовности

Файл с тем же SHA-256 подтверждён сервером; повтор не создаёт дубликат. Версия переживает restart, старая версия неизменна, receipt привязан к правильной версии, ZIP содержит именно её. Чужой owner/tenant не получает project, file, run, artifact, receipt или ZIP.
