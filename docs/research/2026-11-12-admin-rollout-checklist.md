# Админка: приёмка и безопасное включение

Статус: локальная приёмка Task10, не production deployment. Дата: 2026-11-12.

## Порядок будущего выпуска

1. Применить добавочную миграцию на резервируемой БД; не удалять audit/policy и не приписывать историческим Run ложный admission.
2. Развернуть enforcement-compatible backend на всех workers при `admin_policy_management_enabled=false`. Убедиться, что старых pre-enforcement workers нет. Уже допущенные Run завершаются штатно; не отменять их при смене версии.
3. Развернуть совместимый receiver, затем runner. `runner_harness_diagnostics_enabled` включается оператором отдельно только после проверки сохранности diagnostic receipts, source binding и восстановления проекции. Отсутствие capability/связи означает unknown, не нулевой расход.
4. Собрать основной frontend с `VITE_ADMIN_SHELL_ENABLED=true`. Это **build-time** Vite flag: изменение переменной у уже собранного nginx-контейнера не включает UI, нужна пересборка frontend. При `false` или отсутствии значения остаётся одна общая оболочка с read-only unavailable, без запросов admin API. Старый `/product/#admin` всегда направляет в `/#admin/overview`: с выключенным флагом пользователь увидит unavailable, а не старую админку. Старая AdminPanel больше не монтируется.
5. После обязательных race/auth/privacy/protocol/browser gates оператор включает `admin_policy_management_enabled`. В текущей разработке persisted defaults не изменены и deployment не выполнялся.

## Откат

- UI: `VITE_ADMIN_SHELL_ENABLED=false` и пересборка. Не возвращать старую AdminPanel как управление.
- Management: `admin_policy_management_enabled=false` закрывает новые административные изменения, но **не** снимает существующие freezes и enforcement новых AI-запусков.
- Backend: только enforcement-compatible release. Если его нет, закрыть новые AI-starts нейтральным 503, сохранив завершение уже допущенной работы. Не возвращаться к pre-enforcement workers.
- Diagnostic receiver rollback: private metadata не встраивается в public usage; versioned storage envelope старая версия читает как unknown. Не удалять receipt/source tables ради отката.
- После включения заморозок destructive DB downgrade запрещён без отдельной maintenance procedure и переноса политики. Проверки downgrade разрешены только на изолированных тестовых данных.

## Локальная приёмка

Запущены реальные frontend и FastAPI с отдельной SQLite, workspace и синтетическими аккаунтами. Только граница CLI заменена детерминированным fake process, без запросов к моделям. Перехвата admin API не было.

Проверены: реальная cookie-auth, anonymous 401/nonadmin 403, UI freeze и запись private audit, отказ нового WS-запуска при freeze, штатное завершение уже admitted Run, UI unfreeze и новый admission, различение failed/outcome_unknown, unknown-safe estimate, logout cleanup, desktop 1440/mobile 375 и forced-colors. Вход и чтение обычного `/api/auth/me` при freeze остаются доступны; причина отсутствует в нём. Чужой Origin отвергнут 403.

Найден и исправлен dev-only proxy defect: `changeOrigin:true` переписывал Host и ломал same-origin guard admin mutations. Теперь основной Vite сохраняет incoming Host, backend guard не ослаблен; production nginx не изменялся. Добавлен config regression, real-browser freeze/unfreeze после исправления прошёл.

Скриншоты локальной приёмки (временные файлы, не production):
- `/tmp/coreteams-admin-real-overview.png`
- `/tmp/coreteams-admin-real-users-mobile.png`
- `/tmp/coreteams-admin-real-forced-colors.png`

Временные acceptance scripts `/tmp/coreteams-task10-{app.py,browser.mjs,compat.mjs,pg.py}` не являются production tooling. Все использованные учётные записи/суммы/запуски синтетические. Реальные upstream model/prices, подписка, production routing и staging latency остаются непроверенными. Полные security/reliability checklist-аудиты недоступны; конкретные проверки не выдаются за такой аудит.
