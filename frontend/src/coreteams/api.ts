import { apiFetch } from '../auth';

export type ScenarioKey = 'course' | 'book' | 'business' | 'other';
export type SetupStep = 'welcome' | 'context' | 'question' | 'team-plan' | 'plan';
export type Phase =
  | 'draft'
  | 'idle'
  | 'working'
  | 'waiting'
  | 'review'
  | 'failed'
  | 'ready'
  | 'done';

export type Role = { name: string; why: string; result: string; origin: string };
export type Stage = { short: string; title: string; result: string; next: string; time: string };
export type Question = {
  title: string;
  help: string;
  options: string[];
  guide: string;
  copy: string;
};
export type Scenario = {
  key: ScenarioKey;
  label: string;
  roles: Role[];
  questions: Question[];
  stages: Stage[];
  suggestions: string[];
};
export type Catalog = {
  team_suggestion: string;
  scenarios: Scenario[];
  marketer: Role;
  promotion_answer: string;
};

export type ResultVersion = {
  version: number;
  run_id: string;
  content: string;
  accepted: boolean;
  created_at: string;
};
export type AgentQuestion = {
  text: string;
  header: string | null;
  multi_select: boolean;
  options: { label: string; description: string | null }[];
};
export type ProjectSummary = {
  id: string;
  name: string;
  scenario: ScenarioKey;
  example: boolean;
  status: 'draft' | 'active' | 'ready' | 'done';
  phase: Phase;
  stage: number;
  stage_title: string;
  setup_step: SetupStep;
  updated_at: string;
};
export type Project = ProjectSummary & {
  conversation_id: string;
  problem: string;
  context: string;
  answers: string[][];
  custom: string[];
  team_feedback: string;
  notes: string[];
  question_index: number;
  feedback: '' | 'yes' | 'partly' | 'no';
  feedback_note: string;
  team: Role[];
  results: ResultVersion[][];
  files: { id: string; name: string; size_bytes: number }[];
  active_run_id: string | null;
  question: AgentQuestion[] | null;
  error_code: string | null;
  cost_usd: number;
  duration_ms: number;
  runs_count: number;
  created_at: string;
};
export type ProjectPatch = Partial<
  Pick<
    Project,
    | 'scenario'
    | 'name'
    | 'problem'
    | 'context'
    | 'answers'
    | 'custom'
    | 'team_feedback'
    | 'setup_step'
    | 'question_index'
    | 'feedback'
    | 'feedback_note'
  >
> & { note?: string };

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    let detail = 'Не удалось выполнить запрос';
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') {
        detail = body.detail;
      }
    } catch {
      // тело ошибки не JSON — остаётся общее сообщение
    }
    throw new ApiError(response.status, detail);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

const json = (body: unknown): RequestInit => ({
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body),
});

export const api = {
  scenarios: () => request<Catalog>('/api/scenarios'),
  projects: () => request<ProjectSummary[]>('/api/projects'),
  create: (example: boolean) =>
    request<Project>('/api/projects', { method: 'POST', ...json({ example }) }),
  project: (id: string) => request<Project>(`/api/projects/${id}`),
  patch: (id: string, patch: ProjectPatch) =>
    request<Project>(`/api/projects/${id}`, { method: 'PATCH', ...json(patch) }),
  upload: (id: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<Project>(`/api/projects/${id}/files`, { method: 'POST', body: form });
  },
  removeFile: (id: string, fileId: string) =>
    request<Project>(`/api/projects/${id}/files/${fileId}`, { method: 'DELETE' }),
  approve: (id: string) => request<Project>(`/api/projects/${id}/approve`, { method: 'POST' }),
  finish: (id: string) => request<Project>(`/api/projects/${id}/finish`, { method: 'POST' }),
  reopen: (id: string) => request<Project>(`/api/projects/${id}/reopen`, { method: 'POST' }),
};

export function scenarioOf(catalog: Catalog, key: string | undefined): Scenario {
  return catalog.scenarios.find((item) => item.key === key) ?? catalog.scenarios[catalog.scenarios.length - 1];
}

/** Ответ на вопрос подготовки так, как его видит команда (перенос answerText из v9). */
export function answerText(catalog: Catalog, project: Project, index: number): string {
  const scenario = scenarioOf(catalog, project.scenario);
  const chosen = (project.answers[index] ?? []).map((item) =>
    item === catalog.team_suggestion ? scenario.suggestions[index] : item,
  );
  return [...chosen, project.custom[index]?.trim()].filter(Boolean).join(', ') || 'Нужно уточнить';
}

export function assumptions(catalog: Catalog, project: Project): string[] {
  const scenario = scenarioOf(catalog, project.scenario);
  return project.answers
    .map((items, index) => (items.includes(catalog.team_suggestion) ? scenario.suggestions[index] : null))
    .filter((item): item is string => item !== null);
}

export function latestVersion(project: Project, stage: number): ResultVersion | undefined {
  return project.results[stage]?.at(-1);
}

export function formatCost(project: Pick<Project, 'cost_usd' | 'duration_ms' | 'runs_count'>): string {
  if (project.runs_count === 0) {
    return 'Запусков ещё не было';
  }
  const minutes = Math.round(project.duration_ms / 60000);
  const time = minutes >= 1 ? `${minutes} мин` : `${Math.max(1, Math.round(project.duration_ms / 1000))} с`;
  return `$${project.cost_usd.toFixed(2)} · ${time} работы · запусков: ${project.runs_count}`;
}

/** Все одобренные результаты одним Markdown-файлом (перенос markdown() из v9). */
export function exportMarkdown(
  catalog: Catalog,
  project: Project,
  stages: number[],
): string {
  const scenario = scenarioOf(catalog, project.scenario);
  const files = project.files.map((item) => item.name).join(', ') || 'не добавлены';
  const docs = stages
    .map((stage) => ({ stage, doc: latestVersion(project, stage) }))
    .filter((item): item is { stage: number; doc: ResultVersion } => item.doc !== undefined)
    .map(
      ({ stage, doc }) =>
        `<!-- ${scenario.stages[stage].title} — версия ${doc.version}${doc.accepted ? ', одобрено' : ''} -->\n\n${doc.content.trim()}`,
    );
  return [
    `# ${project.name}`,
    `Результат CoreTeams · ${scenario.label}. Тексты подготовлены Claude по вашей задаче и материалам.`,
    '## Контекст автора',
    project.problem,
    project.context,
    `Материалы: ${files}`,
    ...scenario.questions.map((question, index) => `- ${question.title} — ${answerText(catalog, project, index)}`),
    docs.join('\n\n---\n\n'),
  ]
    .filter((part) => part.trim())
    .join('\n\n');
}

export function downloadMarkdown(name: string, content: string): void {
  const blob = new Blob(['﻿' + content], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `CoreTeams-${name.replace(/[<>:"/\\|?*]/g, '').slice(0, 60) || 'проект'}.md`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
