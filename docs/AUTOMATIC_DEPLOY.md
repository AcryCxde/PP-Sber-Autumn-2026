# Автоматическое обновление стенда

Merge в `master` запускает CI, публикацию проверенных образов и отдельный Deploy.
Повтор: GitHub → Actions → Deploy → Run workflow → master. Одинаково с любого устройства;
SSH, локальный Docker, ввод SHA, SQL и изменение серверных approvals не нужны.

Контроллер устанавливается один раз в `/opt/coreteams-deploy/controller`. Workflow передаёт
только release JSON. Сервер независимо проверяет GitHub provenance и digest, сериализует
операции и выполняет их через systemd независимо от SSH-соединения. Новый controller
не устанавливается из очередного release artifact. Обновление самого controller — отдельная
операция инфраструктуры; новые модели/revisions не требуют её.

## Алгоритм

1. Под lock проверить immutable release и ancestry: уже установленный здоровый SHA — no-op,
   более старый — superseded, несвязанный — отказ. После начала target закреплён.
2. Закрыть frontend Service, остановить frontend/backend, дождаться завершения всех writers;
   затем штатно остановить PostgreSQL и SeaweedFS.
3. Потоково зашифровать полный cold snapshot четырёх PVC (БД, backend data, home, S3).
   Проверить ciphertext SHA256, расшифровать в изолированный каталог и сравнить полное
   дерево файлов, владельцев, режимов, ссылок и содержимого. Никаких списков таблиц или строк.
4. Запустить из копии PostgreSQL с точным image digest и исходным PGDATA: locale, encoding,
   provider, роли, sequences и данные сохраняются автоматически. NetworkPolicy изолирует
   rehearsal DB и migration Job. Job получает только DATABASE_URL, без application secrets,
   PVC приложения, service-account token и доступа к S3. Миграции имеют контракт DB-only.
5. Candidate image выполняет `backend.migrate plan`, `upgrade`, `check-models` на копии.
   Один head, допустимый upgrade path; тот же head допустим. Сохранить schema digest.
6. Запустить исходные DB/S3, выполнить такой же upgrade отдельным Job candidate digest,
   проверить revision и schema digest. Запустить backend с DATABASE_MIGRATIONS=verify:
   web startup не исполняет DDL. Запустить frontend за закрытым Service.
7. Проверить private auth/admin/403 и frontend, затем записать opening и открыть Service.
   Проверить публичный HTTPS; сохранить current/previous release и журнал фаз.

Каждый Job: backoffLimit=0, restartPolicy=Never, deadline; повтор upgrade дополнительно
защищён PostgreSQL advisory lock и Alembic revision. Журнал сохраняется атомарно с fsync.
Обрыв во время Job: следующий запуск ждёт существующий Job. Успешно завершённый target
распознаётся по Job, revision и schema. Неизвестный/неуспешный исход до открытия трафика
восстанавливает проверенный полный snapshot и исходные образы. Автоматического downgrade нет.
До готовности backup source data не меняется. После opening snapshot автоматически не
возвращается: это могло бы потерять новые записи. Retry проверяет и завершает тот же target.

Backup хранится зашифрованным на том же узле (компромисс: не защищает от потери узла).
Retention — последние 5 законченных попыток; текущая незавершённая попытка не удаляется.
Перед остановкой проверяются свободное место (трёхкратный объём PVC плюс 1 GiB), memory,
доступ к credentials и наличие четырёх настроенных каталогов. Лимит по объёму настраивается
один раз; превышение отклоняется до остановки. Чтение и шифрование потоковые, без лимита
192 MiB или 100000 строк. Cold backup включает S3 и backend files, но migration Job не
должен менять внешние системы. EXECUTOR=runner требует отдельной процедуры drain;
этот controller проверяет поддерживаемый EXECUTOR=cli до остановки.

Официальные контракты: [workflow_run](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run),
[Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/),
[Alembic data migrations](https://alembic.sqlalchemy.org/en/latest/cookbook.html#data-migrations-general-techniques).

## Журнал приложения и приёмка

`deployment_audits` хранит события создания схемы и проверенные кандидаты релизов:
SHA, GitHub CI run/attempt, Alembic head и время. Candidate-owned команда
`backend.migrate record` получает проверенный manifest и записывает строку после
private smoke, до открытия трафика. Повтор одного run/attempt не создаёт дублей.
Контроллер вызывает стабильный CLI; имена таблиц, ORM и новые revisions остаются
в коде приложения. Для добавления этой модели серверные файлы не меняются.

Изолированная серверная приёмка выполнена на отдельном namespace и копии данных:
три сгенерированные revisions, 120001 строка с ненулевой sequence, сбои до DDL,
после COMMIT и при старте backend, продолжение после backup/COMMIT, same-SHA retry.
Результат сохранён в `/var/lib/coreteams-deploy/acceptance/evidence.json`;
workloads и plaintext-копии после проверки удалены. Эту проверку контроллера
дополняют реальные merge → CI → Deploy: GitHub provenance не подменяется тестовым API.
