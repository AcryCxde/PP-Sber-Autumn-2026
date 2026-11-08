# PPS-234 — storage integration probe

Дата проверки: 2026-11-08  
База: `e51cc62fe4acddf0acb862b9a44055d62556df69`  
Ветка: `test/pps-234-storage-integration`

## Область

Storage-side проверка связи `Project → ProjectRun → Run → Event/Artifact` для UI первого результата: running, pause/waiting-for-input, error, completed, reconnect/replay, terminal guard и подтверждаемость сохранённого результата.

## Статус предшествующих storage-патчей

`git merge-base --is-ancestor <tip> e51cc62` вернул код 1 для обоих tip-коммитов:

- PR #13, `bbd1002797a82104a0559786b2ec7747aedadd44`: **не входит** в базовый SHA.
- PR #14 (актуальная ветка `fix/artifact-confirmation-durability`), `2c8fe917d346b6b4cf6e5492acec7d36e03fb05b`: **не входит** в базовый SHA.

Карта `docs/INTEGRATION_BRANCH_MAP.md` найдена в `origin/docs/integration-branch-map-2026-10-30` (`1b72eaf`), но отсутствует в базовом дереве. Она задаёт порядок PR #13 → #14 → #15 и фиксирует контрактный конфликт PR #15; чужие PR не изменялись и не сливались.

## Контракт базового SHA

| Требование | Реализация в базе | Storage verdict |
|---|---|---|
| Project ↔ Run | `ProjectRun(run_id, project_id, stage, kind, accepted)` | PASS |
| running | `Run.status=running`; UI projection `phase=working` | PASS |
| waiting_for_input | отдельного persisted `Run.status` нет; `completed` + `question.asked` выводится как `phase=waiting` | PASS с оговоркой об alias/projection |
| error | persisted `failed/cancelled/outcome_unknown`; project projection `phase=failed` | PASS; literal `error` отсутствует |
| completed | `Run.status=completed`, `run.completed`, project result `phase=review` | PASS |
| operation ID | поле называется `client_request_id`, UNIQUE; canonical replay возвращает тот же Run | PASS с naming mismatch |
| reconnect/replay | чтение событий по `(run_id, after_seq)`, UNIQUE `(run_id, seq)`; повтор accept не создаёт Run | PASS |
| terminal guard | поздние события блокируются для terminal Run; повтор running не выполняется | PASS |
| artifact receipt | `Artifact(run_id,event_seq,path,content,size_bytes,truncated)` + `artifact.saved` | PARTIAL |
| durable receipt | реальные stored bytes доступны и hash можно пересчитать, но SHA-256 и confirmed-write boundary не сохраняются в базе | FAIL на базовом SHA; это зона невлитого PR #14 |

## Синтетическое доказательство

Команда:

```text
.\.venv\Scripts\python.exe -m pytest backend/tests/test_pps234_storage_integration.py -q -s
```

Результат: `1 passed`.

```json
{
  "operation_id": "pps-234-op-0001",
  "run_id": "e66eefae-8d12-4fac-ae36-8e6b10818ea3",
  "artifact_event_seq": 3,
  "event_count": 5,
  "path": "results/first-result.md",
  "size_bytes": 44,
  "sha256_recomputed_from_stored_bytes": "57d71f904948d45ee45641aa6d619776340b3e02d54323b61607a2a0f2020554",
  "replay_same_run": true,
  "terminal_guard": "rejected_late_append",
  "ui_phases": ["working", "waiting", "failed", "review"]
}
```

`run_id` создаётся заново при каждом тестовом запуске; operation ID, bytes, hash и path фиксированы.

Регрессия:

```text
.\.venv\Scripts\python.exe -m pytest backend/tests/test_repository.py backend/tests/test_projects_api.py backend/tests/test_pps234_storage_integration.py -q
```

Результат: `20 passed, 1 warning` (стороннее Starlette/httpx deprecation warning).

## Итог

Storage базового master подтверждает lifecycle, pause/error/completed projection, reconnect/replay, идемпотентность и terminal guard. Полноценный durable receipt с сохранённым hash и подтверждённой границей записи **не подтверждён** на базовом SHA; нельзя заявлять готовность этого пункта до интеграции и повторной проверки PR #14 после PR #13.
