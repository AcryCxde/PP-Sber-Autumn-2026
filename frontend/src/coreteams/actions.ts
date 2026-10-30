import { useCallback } from 'react';
import { UnauthorizedError } from '../auth';
import { api, type ProjectSummary } from './api';
import { useShell } from './context';
import { navigate } from './router';

export function projectRoute(project: Pick<ProjectSummary, 'id' | 'status' | 'setup_step'>, question = 0) {
  return project.status === 'draft'
    ? { page: 'project' as const, projectId: project.id, step: project.setup_step, question }
    : { page: 'project' as const, projectId: project.id, step: 'workspace' as const, question: 0 };
}

export function errorText(error: unknown, fallback: string): string | null {
  if (error instanceof UnauthorizedError) {
    return null;
  }
  return error instanceof Error ? error.message : fallback;
}

/** Новый проект или продолжение незаполненного черновика (как newProject в v9). */
export function useStartProject() {
  const { toast } = useShell();
  return useCallback(
    async (example: boolean) => {
      try {
        const projects = await api.projects();
        const unfinished = projects.find(
          (item) => item.status === 'draft' && item.example === example && (example || !item.name.trim()),
        );
        const project = unfinished ?? (await api.create(example));
        navigate(projectRoute(project));
      } catch (error) {
        const message = errorText(error, 'Не удалось создать проект');
        if (message) toast(message);
      }
    },
    [toast],
  );
}
