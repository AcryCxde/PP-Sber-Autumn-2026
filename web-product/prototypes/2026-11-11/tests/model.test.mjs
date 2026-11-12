import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  PROJECT_LIMIT, createSkill, defaultPlan, freshProject, markdown,
  personaInfo, sections, selectPersona, versionFrom,
} from '../src/model.ts';

test('роль задаётся для нового проекта и меняет план', () => {
  const empty = freshProject();
  assert.equal(empty.persona, null);
  const owner = selectPersona(empty, 'owner');
  const implementer = selectPersona(owner, 'implementer');
  assert.equal(owner.persona, 'owner');
  assert.equal(implementer.persona, 'implementer');
  assert.deepEqual(implementer.plan, defaultPlan('implementer', implementer.team));
  assert.equal(implementer.team[0].name, personaInfo.implementer.team[0].name);
  assert.equal(PROJECT_LIMIT, 5);
});

test('версия фиксирует самостоятельный снимок, не меняющийся вместе с проектом', () => {
  const project = selectPersona(freshProject(), 'owner');
  project.brief = 'Выбрать формат услуги';
  project.dialogue = [{ question: 'Что известно?', answer: 'Есть три интервью' }];
  project.plan[0].roles.push('Аналитик');
  const first = versionFrom(project, 'Первый результат');
  project.brief = 'Другая задача';
  project.dialogue[0].answer = 'Другое основание';
  project.plan[0].roles.push('Исследователь');
  assert.equal(first.snapshot.brief, 'Выбрать формат услуги');
  assert.equal(first.snapshot.dialogue[0].answer, 'Есть три интервью');
  assert.deepEqual(first.snapshot.plan[0].roles, ['Фасилитатор', 'Аналитик']);
  project.versions.push(first);
  assert.equal(versionFrom(project, 'Новые условия').n, 2);
});

test('карта гипотез входит в результат, а общий результат содержит обязательные блоки', () => {
  const project = selectPersona(freshProject(), 'owner');
  project.brief = 'Решить, арендовать ли помещение';
  const version = versionFrom(project, 'Первый результат');
  const headings = sections(version).map(section => section.title);
  for (const heading of ['Что уже решено', 'Что изменилось', 'Что нужно от вас', 'Основания и допущения', 'Карта гипотез', 'План проверки', 'Первый шаг']) {
    assert.ok(headings.includes(heading), heading);
  }
  assert.match(markdown(version), /Локальный макет/);
  assert.match(markdown(version), /## Карта гипотез/);
});

test('скилл содержит полный пакет и честный статус проверки', () => {
  const project = selectPersona(freshProject(), 'implementer');
  project.brief = 'Проверить навигацию посетителей';
  const version = versionFrom(project, 'Первый результат');
  const skill = createSkill(project, version);
  const paths = Object.keys(skill.files);
  for (const suffix of ['SKILL.md', 'README.md', 'REPORT.md', 'references/result-template.md', 'references/example-input.md', 'references/acceptance-checklist.md']) {
    assert.ok(paths.some(path => path.endsWith(suffix)), suffix);
  }
  assert.equal(skill.trial, 'not_run');
  assert.match(skill.files[paths.find(path => path.endsWith('SKILL.md'))], /Не утверждайте, что тест или анализ файла выполнен/);
});
