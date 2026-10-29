import { describe, expect, it } from 'vitest';
import { answerText, assumptions, exportMarkdown, type Catalog, type Project } from './api';
import { documentTitle, parseInline, parseMarkdown } from './markdown';
import { parseRoute, routeHash } from './router';

const catalog: Catalog = {
  team_suggestion: 'Предложение команды',
  promotion_answer: 'Продумать продвижение',
  marketer: { name: 'Маркетолог', why: '', result: '', origin: '' },
  scenarios: [
    {
      key: 'other',
      label: 'Другая задача',
      roles: [],
      questions: [
        { title: 'Что получить?', help: '', options: [], guide: '', copy: '' },
        { title: 'Кому?', help: '', options: [], guide: '', copy: '' },
        { title: 'Границы?', help: '', options: [], guide: '', copy: '' },
      ],
      stages: [
        { short: 'Разбор', title: 'Карта задачи', result: '', next: '', time: '' },
        { short: 'План', title: 'План решения', result: '', next: '', time: '' },
        { short: 'Шаг', title: 'Бриф на первый шаг', result: '', next: '', time: '' },
      ],
      suggestions: ['Начать с карты задачи', 'Аудитория пока не определена', 'Границы требуют уточнения'],
    },
  ],
};

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p1', name: 'Кофейня', scenario: 'other', example: false, status: 'ready', phase: 'ready',
    stage: 2, stage_title: '', setup_step: 'plan', updated_at: '', conversation_id: 'c1',
    problem: 'Открыть кофейню', context: 'Учесть сезонность',
    answers: [['Предложение команды'], ['Моим клиентам'], []], custom: ['', '', 'Без кредита'],
    team_feedback: '', notes: [], question_index: 2, feedback: '', feedback_note: '', team: [],
    results: [
      [{ version: 1, run_id: 'r1', content: '# Карта задачи\nv1', accepted: false, created_at: '' },
        { version: 2, run_id: 'r2', content: '# Карта задачи\nv2', accepted: true, created_at: '' }],
      [{ version: 1, run_id: 'r3', content: '# План решения', accepted: true, created_at: '' }],
      [],
    ],
    files: [{ id: 'f1', name: 'смета.xlsx', size_bytes: 10 }],
    active_run_id: null, question: null, error_code: null, cost_usd: 0.12, duration_ms: 90000,
    runs_count: 3, created_at: '',
    ...overrides,
  };
}

describe('markdown', () => {
  it('parses headings, lists, paragraphs and inline marks', () => {
    const blocks = parseMarkdown('# Итог\n\nПервая строка\nпродолжение **важно**.\n\n- один\n- два\n\n1. шаг\n2. шаг\n\n> цитата\n\n---');
    expect(blocks.map((block) => block.kind)).toEqual(['heading', 'paragraph', 'list', 'list', 'quote', 'rule']);
    expect(documentTitle(blocks)).toBe('Итог');
    const paragraph = blocks[1];
    expect(paragraph.kind === 'paragraph' && paragraph.inline).toEqual([
      { kind: 'text', text: 'Первая строка продолжение ' },
      { kind: 'strong', text: 'важно' },
      { kind: 'text', text: '.' },
    ]);
    const ordered = blocks[3];
    expect(ordered.kind === 'list' && ordered.ordered).toBe(true);
  });

  it('keeps HTML as plain text', () => {
    expect(parseInline('<img src=x onerror=alert(1)>')).toEqual([
      { kind: 'text', text: '<img src=x onerror=alert(1)>' },
    ]);
  });

  it('parses tables with alignment, escaped pipes and short rows', () => {
    const blocks = parseMarkdown('Вступление\n| Неделя | Тема | Часы |\n|---|:---:|--:|\n| 1 | **Методы** | 3 |\n| 2 | a \\| b |\n\nИтог');
    expect(blocks.map((block) => block.kind)).toEqual(['paragraph', 'table', 'paragraph']);
    const table = blocks[1];
    if (table.kind !== 'table') throw new Error('expected table');
    expect(table.align).toEqual([null, 'center', 'right']);
    expect(table.header.map((cell) => cell[0].text)).toEqual(['Неделя', 'Тема', 'Часы']);
    expect(table.rows[0][1]).toEqual([{ kind: 'strong', text: 'Методы' }]);
    expect(table.rows[1].map((cell) => cell.map((part) => part.text).join(''))).toEqual(['2', 'a | b', '']);
  });

  it('keeps pipes without a delimiter row as text', () => {
    expect(parseMarkdown('a | b\nc | d').map((block) => block.kind)).toEqual(['paragraph']);
    expect(parseMarkdown('| a | b |\n|---|').map((block) => block.kind)).toEqual(['paragraph']);
  });

  it('reads fenced code verbatim', () => {
    expect(parseMarkdown('```\n# не заголовок\n- не список\n```')).toEqual([
      { kind: 'code', text: '# не заголовок\n- не список' },
    ]);
  });
});

describe('router', () => {
  it('round-trips project routes', () => {
    const route = parseRoute('#project/p1/question/2');
    expect(route).toEqual({ page: 'project', projectId: 'p1', step: 'question', question: 2 });
    expect(routeHash(route)).toBe('#project/p1/question/2');
  });

  it('falls back to home and workspace', () => {
    expect(parseRoute('#nowhere')).toEqual({ page: 'home' });
    expect(parseRoute('#project/p1/bogus')).toMatchObject({ step: 'workspace' });
    expect(parseRoute('#project/p1/question/9')).toMatchObject({ question: 2 });
  });
});

describe('project helpers', () => {
  it('resolves team suggestions and custom answers like the prototype', () => {
    expect(answerText(catalog, project(), 0)).toBe('Начать с карты задачи');
    expect(answerText(catalog, project(), 2)).toBe('Без кредита');
    expect(assumptions(catalog, project())).toEqual(['Начать с карты задачи']);
  });

  it('exports the latest version of each stage with project context', () => {
    const text = exportMarkdown(catalog, project(), [0, 1, 2]);
    expect(text).toContain('# Кофейня');
    expect(text).toContain('Учесть сезонность');
    expect(text).toContain('смета.xlsx');
    expect(text).toContain('v2');
    expect(text).not.toContain('v1');
    expect(text).toContain('# План решения');
    expect(text).toContain('Границы? — Без кредита');
  });
});
