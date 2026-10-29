"""Правила сценариев CoreTeams; перенос web-demo/test/v9-scenarios.test.js."""

import re

import pytest

from backend.scenarios import (
    MARKETER,
    SCENARIO_KEYS,
    TEAM_SUGGESTION,
    PromptInput,
    answer_text,
    build_stage_prompt,
    catalog,
    questions_for,
    scenario_of,
    stages_for,
    team_for,
)


def prompt_input(scenario: str, **overrides) -> PromptInput:
    values = dict(
        scenario=scenario,
        name="Мой проект",
        problem="Без найма команды проверить идею",
        context="Учесть сезонность",
        answers=[["Сначала сравнить варианты"], [], []],
        custom=["", "", ""],
        team_feedback="",
        notes=[],
        material_names=[],
    )
    values.update(overrides)
    return PromptInput(**values)


def test_unknown_scenario_falls_back_to_other() -> None:
    assert scenario_of(None) == "other"
    assert scenario_of("crypto") == "other"
    assert stages_for("crypto")[0].title == "Карта задачи"


@pytest.mark.parametrize("scenario", SCENARIO_KEYS)
def test_all_scenarios_have_three_stages_questions_and_complete_roles(scenario) -> None:
    assert len(stages_for(scenario)) == 3
    assert len(questions_for(scenario)) == 3
    for role in team_for(scenario, [[], [], []]):
        assert all([role.name, role.why, role.result, role.origin])


def test_course_keeps_its_own_stages_and_questions() -> None:
    assert stages_for("course")[1].title == "Обновлённая программа"
    assert re.search("курсе", questions_for("course")[1].title)


def test_marketer_follows_promotion_answer_without_duplicates() -> None:
    assert len(team_for("business", [["Проверить спрос"], [], []])) == 3
    team = team_for("business", [["Проверить спрос", "Продумать продвижение"], [], []])
    assert len(team) == 4
    assert [role.name for role in team].count(MARKETER.name) == 1
    # Только для бизнеса: тот же ответ в другом сценарии роль не добавляет.
    assert len(team_for("other", [["Продумать продвижение"], [], []])) == 3


def test_answer_text_resolves_team_suggestion_and_custom() -> None:
    answers = [[TEAM_SUGGESTION], [], []]
    custom = ["своё", "", ""]
    assert answer_text("course", answers, custom, 0) == "Начать с проверки учебной логики, своё"
    assert answer_text("course", answers, custom, 1) == "Нужно уточнить"


@pytest.mark.parametrize("scenario", ["book", "business", "other"])
def test_non_course_prompts_do_not_leak_course_content_and_keep_user_input(scenario) -> None:
    for stage in range(3):
        prompt = build_stage_prompt(prompt_input(scenario), stage, "stage")
        assert not re.search(r"ученик|теори|модул|программ.*курс", prompt)
        assert "Без найма команды" in prompt
        assert "Сначала сравнить варианты" in prompt
        assert "Учесть сезонность" in prompt
        assert f"# {stages_for(scenario)[stage].title}" in prompt


def test_prompt_marks_assumptions_and_lists_materials() -> None:
    prompt = build_stage_prompt(
        prompt_input(
            "course",
            answers=[[TEAM_SUGGESTION], [], []],
            material_names=["программа.md"],
            team_feedback="Добавить юриста",
            notes=["Учесть выходные"],
        ),
        0,
        "stage",
    )
    assert "не подтвердил: Начать с проверки учебной логики" in prompt
    assert "materials/" in prompt and "программа.md" in prompt
    assert "Добавить юриста" in prompt
    assert "Учесть выходные" in prompt


def test_revision_prompt_carries_current_version_and_remark() -> None:
    prompt = build_stage_prompt(
        prompt_input("other", current_result="# Карта задачи\nВерсия 1", revision="Добавить риски"),
        0,
        "revision",
    )
    assert "Версия 1" in prompt
    assert "Замечание пользователя: Добавить риски" in prompt


def test_next_stage_prompt_includes_approved_previous_result() -> None:
    prompt = build_stage_prompt(prompt_input("other", previous_result="Одобренный разбор"), 1, "stage")
    assert "Одобренный разбор" in prompt
    assert "Этап 2 из 3: План решения" in prompt


def test_catalog_is_serializable_for_the_interface() -> None:
    data = catalog()
    assert [item["key"] for item in data["scenarios"]] == list(SCENARIO_KEYS)
    assert all(len(item["questions"]) == 3 for item in data["scenarios"])
    assert data["promotion_answer"] == "Продумать продвижение"
