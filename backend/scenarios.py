"""Сценарии CoreTeams: направления, вопросы, команда и этапы проекта.

Перенос правил прототипа web-demo/public/v9/app.js. Направление выбирает пользователь,
текст не классифицируется. Отсюда же собираются промпты этапов для Claude.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Role:
    name: str
    why: str
    result: str
    origin: str


@dataclass(frozen=True)
class Stage:
    short: str
    title: str
    result: str
    next: str
    time: str


@dataclass(frozen=True)
class Question:
    title: str
    help: str
    options: tuple[str, ...]
    guide: str
    copy: str


TEAM_SUGGESTION = "Предложение команды"
SCENARIO_KEYS = ("course", "book", "business", "other")

SCENARIO_LABELS = {
    "course": "Обучение и курс",
    "book": "Книга и материалы",
    "business": "Бизнес и новая идея",
    "other": "Другая задача / пока не знаю",
}

ROLES: dict[str, tuple[Role, ...]] = {
    "course": (
        Role("Методист", "Связать цель обучения с практикой", "Структура программы и задания", "Новая предметная роль"),
        Role("Исследователь аудитории", "Понять опыт и ограничения учеников", "Вопросы для проверки потребностей и нагрузки", "Специализация аналитика"),
        Role("Редактор", "Сделать материалы последовательными и понятными", "Согласованный текст и замечания к ясности", "Новая предметная роль"),
    ),
    "book": (
        Role("Редактор", "Связать замысел, читателя и структуру книги", "Редакционная концепция и план глав", "Новая предметная роль"),
        Role("Дизайнер", "Подобрать визуальный язык под формат чтения", "Требования к оформлению и образцу разворота", "Специализация дизайнера"),
        Role("Специалист по подготовке издания", "Не упустить требования к передаче макета", "Чек-лист файлов, прав и технических проверок", "Новая предметная роль"),
    ),
    "business": (
        Role("Исследователь рынка", "Проверить, кому и зачем нужно предложение", "Карта гипотез спроса и план интервью", "Специализация аналитика"),
        Role("Финансовый аналитик", "Выявить неизвестные расходы и источники дохода", "Структура расчёта и список исходных данных", "Специализация аналитика"),
        Role("Специалист по запуску", "Связать выводы в последовательность действий", "План первой проверки идеи и ограничения", "Новая предметная роль"),
    ),
    "other": (
        Role("Аналитик задачи", "Отделить цель от предположений и неизвестного", "Карта вопросов и недостающих данных", "Специализация аналитика"),
        Role("Исследователь решений", "Сравнить возможные подходы", "Варианты решения и основания выбора", "Специализация аналитика"),
        Role("Редактор результата", "Объединить выводы в понятный документ", "Структура решения и следующий шаг", "Новая предметная роль"),
    ),
}
MARKETER = Role(
    "Маркетолог",
    "Вы выбрали продвижение: нужны каналы первых контактов",
    "Гипотезы каналов и сообщений для теста",
    "Новая предметная роль",
)
PROMOTION_ANSWER = "Продумать продвижение"

COURSE_STAGES = (
    Stage("Диагностика", "Карта проблем и концепция", "Что стоит изменить в курсе и какую логику обучения выбрать.", "Перейти к программе", "3–5 минут"),
    Stage("Программа", "Обновлённая программа", "Последовательность модулей, практика и ожидаемый результат каждого.", "Перейти к первому модулю", "5–8 минут"),
    Stage("Первый модуль", "Первый модуль курса", "Сценарий занятия, практическое задание и критерии проверки.", "Принять итоговый результат", "4–6 минут"),
)
GENERIC_STAGE_TITLES = {
    "book": ("Концепция книги", "Редакционный план", "Бриф на образец и выпуск"),
    "business": ("Карта гипотез идеи", "План проверки идеи", "Бриф на первый эксперимент"),
    "other": ("Карта задачи", "План решения", "Бриф на первый шаг"),
}
GENERIC_STAGE_RESULTS = {
    "book": (
        "Для кого книга, её замысел и открытые вопросы.",
        "Структура и порядок подготовки материалов.",
        "Требования к образцу и передаче в производство; не готовый печатный макет.",
    ),
    "business": (
        "Что известно и что нужно проверить до вложений.",
        "Последовательность проверок спроса и ограничений.",
        "Условия теста, необходимые данные и критерии решения.",
    ),
    "other": (
        "Цель, ограничения и неизвестные.",
        "Варианты действий и порядок проверки.",
        "Что сделать первым и как оценить результат.",
    ),
}

COURSE_QUESTIONS = (
    Question(
        "Что сейчас важнее всего улучшить?",
        "Можно выбрать несколько пунктов. Это поможет команде расставить приоритеты.",
        ("Больше практики", "Понятнее объяснять сложное", "Помочь ученикам дойти до конца", "Обновить содержание"),
        "Здесь не нужен идеальный ответ",
        "Вы знаете свой курс лучше всех. Отметьте то, что замечали сами. Если причин пока не видно — мы начнём с диагностики.",
    ),
    Question(
        "Кто будет учиться на курсе?",
        "Какой опыт уже есть у ваших учеников? Можно выбрать несколько групп.",
        ("Начинают с нуля", "Уже знают основы", "Работают в этой области", "Аудитория смешанная"),
        "Посмотрим глазами ученика",
        "Команда проверит, какие знания можно считать исходными, а что нужно объяснить. От этого зависит сложность первого задания.",
    ),
    Question(
        "Что обязательно нужно сохранить?",
        "Обозначьте границы: команда не будет менять всё подряд.",
        ("Авторские примеры и кейсы", "Длительность курса", "Основные темы", "Стиль и тон автора"),
        "Решение остаётся за вами",
        "Мы предложим изменения и объясним их смысл. Переход к следующему результату — только после вашего одобрения.",
    ),
)
COURSE_SUGGESTIONS = (
    "Начать с проверки учебной логики",
    "Смешанная аудитория — требуется проверка",
    "Сохранить авторские кейсы — требуется подтверждение",
)
GENERIC_SUGGESTIONS = (
    "Начать с карты задачи — подтвердить",
    "Аудитория пока не определена",
    "Границы требуют уточнения",
)

# Учебный пример из прототипа: вымышленный курс для знакомства с сервисом.
EXAMPLE_PROJECT = {
    "scenario": "course",
    "name": "Исследования для начинающих дизайнеров",
    "problem": (
        "Ученики долго изучают теорию и редко доходят до самостоятельного исследования. "
        "Хочу больше практики и понятный первый шаг."
    ),
    "context": (
        "Курс на 5 недель. Важно сохранить реальные кейсы автора. "
        "Ученики совмещают обучение с работой."
    ),
}


def scenario_of(value: str | None) -> str:
    return value if value in SCENARIO_KEYS else "other"


def stages_for(scenario: str) -> tuple[Stage, ...]:
    scenario = scenario_of(scenario)
    if scenario == "course":
        return COURSE_STAGES
    titles = GENERIC_STAGE_TITLES[scenario]
    results = GENERIC_STAGE_RESULTS[scenario]
    return tuple(
        Stage(
            short=("Разбор", "План", "Первый шаг")[index],
            title=titles[index],
            result=results[index],
            next=f"Перейти: {titles[index + 1].lower()}" if index < 2 else "Принять итоговый результат",
            time=base.time,
        )
        for index, base in enumerate(COURSE_STAGES)
    )


def questions_for(scenario: str) -> tuple[Question, ...]:
    scenario = scenario_of(scenario)
    if scenario == "course":
        return COURSE_QUESTIONS
    book, business = scenario == "book", scenario == "business"
    return (
        Question(
            "Что вы хотите получить в первую очередь?",
            "Можно выбрать несколько ответов и добавить свой.",
            ("Определить замысел книги", "Собрать структуру", "Улучшить текст", "Подготовиться к изданию") if book
            else ("Проверить спрос", "Разобраться с расходами", "Спланировать запуск", PROMOTION_ANSWER) if business
            else ("Разобраться в проблеме", "Сравнить решения", "Получить план действий", "Подготовить материалы"),
            "Начнём с нужного вам результата",
            "По выбранному направлению я предложу роли и объясню их вклад. Состав — подготовленная схема, а не автоматический анализ текста.",
        ),
        Question(
            "Для кого вы пишете?" if book else "Кому должен помочь результат?",
            "Укажите аудиторию. Если пока не знаете — это тоже важная информация.",
            ("Для широкой аудитории", "Для специалистов", "Для детей и родителей", "Пока не определено") if book
            else ("Мне и моему проекту", "Моим клиентам", "Моим сотрудникам", "Пока не определено"),
            "Не будем додумывать за вас",
            "Неизвестное останется вопросом для проверки. Предположение не станет фактом только потому, что его предложила команда.",
        ),
        Question(
            "Какие границы важно соблюдать?",
            "Выберите несколько ограничений или опишите свои.",
            ("Ограниченный бюджет", "Фиксированный срок", "Сохранить мой замысел", "Использовать готовые материалы"),
            "Вы определяете границы",
            "Команда предложит порядок работы. До начала вы увидите роли, результаты и порядок работы.",
        ),
    )


def suggestions_for(scenario: str) -> tuple[str, ...]:
    return COURSE_SUGGESTIONS if scenario_of(scenario) == "course" else GENERIC_SUGGESTIONS


def team_for(scenario: str, answers: list[list[str]]) -> tuple[Role, ...]:
    scenario = scenario_of(scenario)
    team = ROLES[scenario]
    first = answers[0] if answers else []
    if scenario == "business" and PROMOTION_ANSWER in first:
        team = (*team, MARKETER)
    return team


def answer_text(scenario: str, answers: list[list[str]], custom: list[str], index: int) -> str:
    suggestions = suggestions_for(scenario)
    chosen = [suggestions[index] if item == TEAM_SUGGESTION else item for item in answers[index]]
    parts = [*chosen, custom[index].strip()]
    return ", ".join(part for part in parts if part) or "Нужно уточнить"


def assumptions(scenario: str, answers: list[list[str]]) -> list[str]:
    suggestions = suggestions_for(scenario)
    return [suggestions[index] for index, items in enumerate(answers) if TEAM_SUGGESTION in items]


def catalog() -> dict[str, Any]:
    """Всё, что нужно интерфейсу для подготовки проекта, в одном ответе."""
    return {
        "team_suggestion": TEAM_SUGGESTION,
        "scenarios": [
            {
                "key": key,
                "label": SCENARIO_LABELS[key],
                "roles": [asdict(role) for role in ROLES[key]],
                "questions": [
                    {**asdict(question), "options": list(question.options)}
                    for question in questions_for(key)
                ],
                "stages": [asdict(stage) for stage in stages_for(key)],
                "suggestions": list(suggestions_for(key)),
            }
            for key in SCENARIO_KEYS
        ],
        "marketer": asdict(MARKETER),
        "promotion_answer": PROMOTION_ANSWER,
    }


@dataclass(frozen=True)
class PromptInput:
    scenario: str
    name: str
    problem: str
    context: str
    answers: list[list[str]]
    custom: list[str]
    team_feedback: str
    notes: list[str]
    material_names: list[str]
    previous_result: str | None = None
    current_result: str | None = None
    revision: str | None = None


def build_stage_prompt(data: PromptInput, stage: int, kind: str) -> str:
    """Промпт этапа: Claude пишет результат как фасилитатор, связывающий выводы ролей."""
    scenario = scenario_of(data.scenario)
    stage_info = stages_for(scenario)[stage]
    team = team_for(scenario, data.answers)
    questions = questions_for(scenario)
    lines = [
        "Ты — фасилитатор CoreTeams. Ты организуешь работу команды вокруг задачи пользователя "
        "и возвращаешь ему единый результат на согласование.",
        "",
        f"Направление: {SCENARIO_LABELS[scenario]}.",
        "Команда (каждая роль вносит свою часть, ты объединяешь выводы):",
        *[f"- {role.name}: {role.why}. Результат роли: {role.result}." for role in team],
        "",
        f"## Проект «{data.name.strip() or 'Без названия'}»",
        f"Цель пользователя: {data.problem.strip() or 'не указана'}",
    ]
    if data.context.strip():
        lines.append(f"Контекст: {data.context.strip()}")
    for index, question in enumerate(questions):
        lines.append(f"{question.title} — {answer_text(scenario, data.answers, data.custom, index)}")
    guessed = assumptions(scenario, data.answers)
    if guessed:
        lines.append("Предположения команды, которые пользователь не подтвердил: " + "; ".join(guessed) + ".")
    if data.team_feedback.strip():
        lines.append(f"Замечание пользователя о составе команды: {data.team_feedback.strip()}")
    if data.notes:
        lines.append("Уточнения пользователя:")
        lines.extend(f"- {note}" for note in data.notes[-10:])
    if data.material_names:
        lines.append(
            "Материалы пользователя лежат в папке materials/ рабочей директории: "
            + ", ".join(data.material_names)
            + ". Прочитай их перед работой и опирайся на них."
        )
    else:
        lines.append("Материалов нет — опирайся на описание и явно отмечай, каких данных не хватает.")

    lines += ["", f"## Этап {stage + 1} из 3: {stage_info.title}", f"Ожидаемый результат: {stage_info.result}"]
    if data.previous_result:
        lines += ["", "Одобренный пользователем результат предыдущего этапа:", data.previous_result]
    if kind == "revision" and data.current_result:
        lines += [
            "",
            "Пользователь попросил доработать текущую версию результата этого этапа.",
            "Текущая версия:",
            data.current_result,
            "",
            f"Замечание пользователя: {(data.revision or '').strip()}",
            "Подготовь новую полную версию с учётом замечания.",
        ]

    lines += [
        "",
        "## Формат ответа",
        f"Ответь одним документом в Markdown, начиная с заголовка «# {stage_info.title}».",
        "Включи разделы «На чём основан этот вариант» и «Что остаётся гипотезой».",
        "Не выдавай предположения за проверенные факты. Не создавай и не изменяй файлы: результат — только текст ответа.",
        "Пиши по-русски, конкретно для этого проекта.",
    ]
    return "\n".join(lines)
