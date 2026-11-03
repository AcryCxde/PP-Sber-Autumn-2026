# CoreTeams — контракты и сборка сервисов

**CoreTeams — Рабочее пространство с AI-командой для сложных задач вне одной экспертизы.**

Дата среза: 16 октября 2026. Аудитория: WEB, gateway, runner, storage и инфраструктура.
Статус: **DRAFT / REVIEW_REQUIRED; совместимость общей сборки не подтверждена**.
Этот PR переносит командную навигацию и план проверки в общий репозиторий. Он не
меняет API, не принимает предложения за владельцев сервисов и не сливает код.

## С чего начать

1. Найти исходный контракт и точный SHA в таблице ниже. Название ветки — не версия.
2. Посмотреть [изменения ADR и mapping на ревью](review-2026-10-16.md).
3. Передать следующий стороне данные из [порядка подключения](integration.md).
4. Приложить фактический результат по [проверкам и ограничениям](verification.md).

Исходные форматы меняются в документах владельцев, а не в этом индексе. Документы
веток могут отсутствовать в `master`: закреплённые ссылки намеренно абсолютные.
Старый [аудит 15 октября](https://github.com/pokatyusha/project-harness/blob/36a041aa6724421768e28e5a5278d2e98283d69a/handoff/2026-10-15/contracts/README.md)
остаётся историческим срезом, не актуальным manifest общей сборки.

## Исходники и проверяемые кандидаты

| Граница / материал | Исходник по SHA | Что это доказывает и чего не доказывает |
|---|---|---|
| WEB B → gateway | [B V2 proposal, ce8b6f4](https://github.com/AcryCxde/PP-Sber-Autumn-2026/blob/ce8b6f4259f682f74e128fcce12e0290e02f9186/web-product/docs/ctf-v2-contract.md), [draft PR #11 → PPS-2](https://github.com/AcryCxde/PP-Sber-Autumn-2026/pull/11) | Реализована клиентская часть 16–21; HTTP-проекция ещё PROPOSED, совместимость с живым gateway не подтверждена. A — reference, не замена B |
| Runner → gateway, WS v=1 | [protocol, 8bfe889](https://github.com/AcryCxde/PP-Sber-Autumn-2026/blob/8bfe889928c8b2755eb3545eee4758162e7bde0e/docs/2.4/protocol.md) | Существующие hello/welcome, event, command/ack, heartbeat, questions/answers; входящие порты runner не нужны |
| Публичные события | [events.v0, 8bfe889](https://github.com/AcryCxde/PP-Sber-Autumn-2026/blob/8bfe889928c8b2755eb3545eee4758162e7bde0e/docs/contracts/events-v0.md) | Расширенная ветка с input/usage/hash; не создавать второй enum вместо неё. WEB camelCase — проекция, не новый runner wire |
| Реализация runner | [PR #10, f9dca8e](https://github.com/AcryCxde/PP-Sber-Autumn-2026/tree/f9dca8e57e778543a29d98ef704e466ab8fb3776/runner) | База runner и отдельная ветка 8bfe889 — разные ревизии; выбрать совместимый набор, не считать все ветки уже собранными |
| Gateway receiver — кандидат | [инструкция, cd6a51f](https://github.com/AcryCxde/PP-Sber-Autumn-2026/blob/cd6a51fefd456a3e50f068cc29ac55b21ff137cc/docs/2.4/gateway-receiver.md), [ветка](https://github.com/AcryCxde/PP-Sber-Autumn-2026/tree/codex/runner-gateway-interface-2026-10-16) | Добавлены mountable WS receiver и transport journal. Не готовый browser API, не центральное хранилище и не весь gateway |
| Gateway → storage: модель | [ADR-001, PR #7 head d9b463a](https://github.com/KolbasnyaAr/PP-Sber-Autumn-2026/blob/d9b463aedfdc3f1ecbae32509dd7e47e15ca71d0/docs/adr/ADR-001-storage.md) | ADR и его текущая реализация проверяются отдельно; Proposed не означает Accepted/Implemented |
| Реальный код storage / infra | [PR #9 base 265d703](https://github.com/KolbasnyaAr/PP-Sber-Autumn-2026/tree/265d7034a4347130872b05c05eb1b541e5568e53) | GitArtifactStorage, metadata и HTTP/client код существуют; их наличие не закрывает найденные дефекты и реальный PG/E2E |
| Сверка всех сторон | [mapping, e03d026](https://github.com/AcryCxde/PP-Sber-Autumn-2026/blob/e03d0261ed7e4efddd06be4ac235e5b8ea8d0efc/docs/2.4/contract-mapping-web-v2.md) | Исходный документ вопросов/решений. Доработки на ревью отделены от принятой реализации |
| Проверки хранения | [отчёт, 67a114f](https://github.com/AcryCxde/PP-Sber-Autumn-2026/blob/67a114ff018421cd5ad0e9ccfde2a1043b7aa6a1/docs/storage-contract-tests-2026-10-16.md), [тестовая ветка](https://github.com/AcryCxde/PP-Sber-Autumn-2026/tree/codex/storage-contract-tests-2026-10-16) | Тесты и отчёт, не исправление production-кода |

Полные SHA закреплены в URL. Перед новой проверкой сравнить актуальные heads и
обновить manifest: этот срез не обещает, что авторская ветка больше не менялась.

## Готовое runner-поведение не переписываем

- [ZIP builder, d7db14a](https://github.com/AcryCxde/PP-Sber-Autumn-2026/tree/d7db14afffee64d2dea86076bf7358931c6ee40f).
- [ZIP validator, 8122458](https://github.com/AcryCxde/PP-Sber-Autumn-2026/tree/812245885714c08d57594203a857aa10ff8c338b).
- [Fork recovery / WSS, 61d4853](https://github.com/AcryCxde/PP-Sber-Autumn-2026/tree/61d48533365234e14f95ed90bc3fddb7f5e7a28e).
- [Матрица изоляции, 8be086c](https://github.com/AcryCxde/PP-Sber-Autumn-2026/tree/8be086cf8fa69d0a32c06c755af4ec396d7ee395).

Наличие этих веток не означает, что они включены в стенд или все их проверки PASS.

## Ответственность и разрешения

Дима — gateway, подключение runner и технический merge-owner; Арина — storage и
версии; Филипп — runtime/конфигурация/стенд/общий E2E; Катя — React B и продуктовая
приёмка. На 16–26 октября runner-интеграцию продолжают Дима и Филипп, без ожидания
ответа отсутствующего Ильи и без переписывания его готовых веток.

Задачи: [PPS-169](https://ru.yougile.com/team/3f3c9f205187/#PPS-169),
[170](https://ru.yougile.com/team/3f3c9f205187/#PPS-170),
[167](https://ru.yougile.com/team/3f3c9f205187/#PPS-167),
[166](https://ru.yougile.com/team/3f3c9f205187/#PPS-166),
[138](https://ru.yougile.com/team/3f3c9f205187/#PPS-138),
[139](https://ru.yougile.com/team/3f3c9f205187/#PPS-139).
Статус карточки сам по себе не подтверждает совместимость.

Merge — только после проверки Кати и её отдельного явного ОК на конкретные PR.
Этот документационный draft и прежнее временное окно такого ОК не дают.
PR #11 остаётся draft; auto-merge не включать. В этом пакете нет секретов,
переписки, идентификаторов участников исследований или новых сроков.
