# Пакет развёртывания

Ветка: `release/coreteams-integration-2026-10-27`.

Текущий подтверждённый контур: `frontend → gateway → runner → CTF/Claude`.
React B, server storage, версии, receipt и ZIP сюда ещё не включены.

## Подготовка

Нужны Docker Engine, Docker Compose v2, отдельный каталог runner-context и секреты в закрытом окружении хоста.

```sh
cp deploy/.env.release.example .env.gateway
# заполнить значения из секретного хранилища
docker compose build
docker compose up -d
docker compose ps
```

Не передавать `.env.gateway`, токены или gateway_token через Git, Vite или чат.
В production вместо прямого mount docker.sock нужен socket proxy; прямой сокет даёт backend привилегии хоста.

## Gates до публичного URL

1. Успешен GitHub Actions `Release gates` на точном SHA.
2. Gateway применил миграции; runner завершил hello/welcome и event replay.
3. Пройден `facilitator.question → fork_answer → тот же runId`.
4. Проверен restart без ложного `running`; есть rollback-owner и логи.

## Явные блокеры

- PostgreSQL требует принятого драйвера и ORM-миграций.
- Для версий/receipt/ZIP нужен storage API с read-back байтов.
- React B подключается только после API mapping и технического E2E.

Откат: `docker compose down`; тома и workspace не удалять без решения владельца.
