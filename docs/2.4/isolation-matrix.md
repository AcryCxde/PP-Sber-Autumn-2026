# Негативная матрица изоляции runner (PPS-132)

Автоматизированная матрица: `runner/tests/test_isolation_matrix.py`. Доказывает, что граница
проекта на стороне runner отклоняет чужие пути, id и секреты, а отказ не оставляет побочных
эффектов (файл не создан, inbox пуст, журнал и события v0 без утечки).

## Что считается границей

1. Том `proj-<id>` монтируется только в контейнер `ct-<id>` (`sessions.run_args`), секреты — `:ro`.
2. Внутри контейнера сторож `guard.check` ограничивает файловые инструменты корнем
   `/workspace/project` с раскрытием симлинков. Для Bash это только эвристика, не граница.
3. Команды: `accept_command` и `Inbox` (id `[0-9A-Za-z_-]{1,64}`, реестр развилок).
4. Наружу: `Redactor` на записи в журнал, затем `Translator` (v0) публикует только выбранные поля.

## Матрица

| Вектор | Слой, где проверяется | Статус |
|---|---|---|
| `../` (в т.ч. глубокий), абсолютный путь в соседний том | guard.check | PASS |
| `/etc/passwd`, `/proc/self/environ`, `/run/secrets/*` (Read, Grep, Glob, Bash) | guard.check | PASS |
| `.runner/events.jsonl`, запись в `.runner/inbox`, `claude/` | guard.check | PASS |
| Двойник корня `project-evil`, `~`, `/usr/../etc`, `/dev/null/../..` | guard.check | PASS |
| Glob с `..` в статической и динамической части | guard.check | PASS |
| NUL-байт в пути | guard.check (исключение; `Session._verdict` превращает в Deny) | PASS |
| Симлинк на внешний файл/каталог, цепочка симлинков, висячий симлинк (Write) | guard.check + realpath | PASS |
| Симлинк из seed-каталога | seed_project + guard.check | PASS |
| `..\x`, `%2e%2e/x`, юникод-точки (`．．`, `‥`) | guard.check: буквальные имена внутри корня | PASS |
| Bash: `cd /`, `$(echo /etc/passwd)`, `python -c "open('/etc/passwd')"`, редирект (в т.ч. `echo x>/etc/foo`) | эвристика guard | PASS |
| Bash: `$HOME/...`, путь из base64, путь, собранный интерпретатором | эвристика guard | XFAIL |
| Hardlink на внешний файл | guard.check | XFAIL |
| Плохой `id` команды (пусто, пробел, `\n`, `\r\n`, NUL, `../`, `/`, 65 символов, кириллица, fullwidth) x message/stop/fork_answer | accept_command | PASS |
| Чужой/закрытый `fork_id`, `fork_id` с управляющими символами и `..` | accept_command | PASS |
| Ответ с чужими, лишними или отсутствующими вопросами развилки | accept_command | PASS |
| Повтор `id` с другим телом не перезаписывает исходную команду | accept_command + has_command | PASS |
| Файл в inbox с неверным kind/id/типами не доставляется, уходит в `rejected` | Inbox.peek | PASS |
| CLI `fork-answer` с чужим `fork_id` | inbox.answers_for | PASS |
| Чужой `project_id` в кадре команды не попадает в файл inbox | accept_command | PASS |
| Кадр команды с чужим `project_id` отклоняется | accept_command | XFAIL |
| `project_id` вне `[a-z0-9][a-z0-9-]{0,39}` (11 вариантов) | RunnerConfig, RunSpec, `sessions` CLI | PASS |
| Только свой том и свои секреты в `docker run`; нет `--privileged`, `--network`, `docker.sock`, `--cap-add` | sessions.run_args | PASS |
| Секреты и чужие ключи не попадают в env контейнера | sessions.container_env | PASS |
| Токены anthropic/gateway, `/run/secrets`, `/workspace/.runner`, `/proc`, env, чужой `project_id` в draft'ах v0 | EventLog+Redactor -> Translator | PASS |
| Артефакты вне проекта (`.runner`, `claude`, `.git`, `..`, `//`, двойник корня) не публикуются | Translator | PASS |
| Внутренний путь в авторском тексте модели (`message.created`) | Translator | XFAIL |
| Токен в base64/hex | Redactor | XFAIL |

## Найденные уязвимости (XFAIL, strict)

1. **Hardlink** (`guard.py`, `check`): hardlink неотличим от файла проекта, `realpath` его не раскрывает.
   Граница: тома раздельные (hardlink через них даёт EXDEV), агент не может сам создать ссылку на файл
   вне корня без доступа к нему. Предложение: не монтировать в один том чужое; при необходимости
   сверять `st_nlink > 1` для Read/Edit.
2. **Bash-эвристика** (`guard.py:114`, `_bash_paths`): переменные, подстановки и интерпретаторы
   обходят проверку. Известное свойство (docstring модуля). Предложение: не полагаться на неё,
   держать границу в монтировании; при необходимости запретить Bash вне белого списка команд.
3. **Чужой `project_id` в кадре команды** (`gateway.py:139`, `accept_command`): runner не знает
   собственный `project_id` в этой функции и принимает кадр с чужим. Изоляция держится на маршрутизации
   gateway по соединению. Предложение: передать `project_id` в `accept_command` и отклонять кадр, где поле
   есть и не совпадает.
4. **Путь в тексте модели** (`v0.py:226`, `_assistant_block`): текст публикуется как есть, внутренние
   пути не вырезаются. Предложение: маскировать `/run/secrets`, `/workspace/.runner`, а корень проекта
   сводить к относительному пути.
5. **Закодированный токен** (`redact.py:28`, `Redactor.text`): маскируется только дословное значение.
   Предложение: добавить base64, hex и percent-encoding вариантов секрета.

Исправлено: приклеенные к операторам shell пути (`echo x>/etc/foo`, `ls&&cat</etc/passwd`)
не проверялись; `guard._bash_paths` теперь делит токен по операторам.

## Чего матрица не покрывает

- Gateway, backend, хранилище: авторизация соединения, привязка токена к проекту, права на
  чужие `artifact_id`/`material_id` вне runner. В runner `artifact_id` — непрозрачный uuid5 от
  `project_id`, `sha` и пути, принять его извне runner не может; `material_id` как сущность
  отсутствует (есть только seed-каталог).
- Реальный docker: монтирование, `--read-only`, `no-new-privileges` проверены только по
  аргументам `docker run`, контейнер не запускается; `fs.protected_hardlinks` и EXDEV не проверяются.
- Реальный Claude SDK: решения `can_use_tool` и hook сторожа проверены на уровне `guard.check`.
- Эксфильтрация через разрешённые каналы (содержимое файлов проекта в тексте модели).

## Запуск

```
cd runner
uv run pytest tests/test_isolation_matrix.py -q      # матрица
uv run pytest -q                                     # все тесты
```

XFAIL строгие: исправление уязвимости превратит тест в XPASS и упадёт, тогда нужно снять
`xfail` и обновить статус здесь.
