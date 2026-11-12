# PR #36: неопределённая AI-операция и навигация

## Проблема

`AdminUsers` хранил draft, исходный payload и idempotency key в собственном `useState`. Переход на «Обзор» размонтировал компонент и удалял эти данные. Abort при размонтировании не доказывает отмену серверного commit; создание нового ключа после возвращения не заменяет replay первой операции.

## Исправление

- В памяти авторизованной оболочки создан `AdminPolicyStore`, доступный через контекст. Он живёт дольше страницы пользователей и не является module-global singleton.
- `AdminUsers` читает draft через `useSyncExternalStore`. Переход между разделами сохраняет только попытку с неопределённым исходом: тот же пользователь-цель, версия, причина, действие и ключ.
- Незавершённая попытка отменяется при уходе; `submitting` становится доступным для точного retry состоянием `editing` с `attempted=true`. Поздние ответы старого компонента игнорируются существующим guard.
- Завершение операции очищает store. Logout, изменение ID/роли авторизованного пользователя и отзыв административного доступа очищают его либо создают новую область хранения.
- Private reason не записывается в localStorage/sessionStorage/URL. Перезагрузка страницы по-прежнему не восстанавливает операцию: область исправления — навигация внутри текущей авторизованной оболочки.

## Изменённые файлы

- `frontend/src/coreteams/adminPolicyStore.ts`, `adminPolicyStore.test.ts`.
- `frontend/src/coreteams/adminHooks.tsx`, `AdminUsers.tsx`, `Shell.tsx`.

## Проверки

RED: новый store-тест до реализации завершился отсутствием модуля; это не представляется runtime-воспроизведением старого UI.

```sh
npm exec --yes --package=node@22 -- npm --prefix frontend test
npm exec --yes --package=node@22 -- npm --prefix frontend run build
npm exec --yes --package=node@22 -- npm --prefix frontend run lint
```

Результат: **93 passed**, build/lint PASS. Четыре store-регрессии проверяют неизменность payload/key, уход во время submitting, очистку и независимость auth-scopes, подписки.

Настоящий frontend в Chrome, синтетический intercepted API, без backend/моделей:

1. Настоящий 30-секундный deadline PATCH → Overview → Users → восстановленный locked draft → retry с побайтово теми же JSON-полями и ключом → успешный receipt.
2. Уход до разрешения PATCH → возвращение с повторяемым draft, а не навсегда `submitting`.
3. После success навигация не восстанавливает форму.
4. Logout из Overview и вход другим администратором очищают приватную причину и ключ.
5. Permission-403 очищает draft; после повторного входа в раздел он не появляется.
6. Page errors: 0. Вариант сетевого отказа также прошёл.

Скрипт: `/tmp/coreteams-admin-navigation-regression.mjs`. Первый timeout-прогон остановился на 30-секундном Playwright ожидании, совпавшем с deadline приложения; после увеличения только ожидания теста до 45 секунд весь сценарий PASS. Скриншот `/tmp/coreteams-admin-operation-navigation.png` просмотрен.

Собственный Vite5199 остановлен, порт свободен; браузер успешного прогона закрыт. Коммитов/push/деплоя нет.

## Notes об ограничениях

Изменение классифицировано как Module: внутренний React store и все его потребители обновлены вместе; API и серверная идемпотентность не меняются. `existing-code` и его reference-routing применены перед правками и к итоговому diff; выполнены scoped-проверка call sites, неразрушение payload/key и проверки auth-scope. `typescript-rules`, `security-review`, `reliability-review` отсутствуют: их checklist-гейты не выполнялись. Browser evidence относится к synthetic API, не production и не реальному серверному commit.
