import { createContext, useContext, type ReactNode } from 'react';
import type { User } from '../auth';
import type { Catalog, Phase } from './api';

export type ShellContextValue = {
  user: User;
  catalog: Catalog;
  toast: (text: string) => void;
  openDialog: (title: string, content: ReactNode) => void;
  closeDialog: () => void;
};

export const ShellContext = createContext<ShellContextValue | null>(null);

export function useShell(): ShellContextValue {
  const value = useContext(ShellContext);
  if (!value) {
    throw new Error('ShellContext is not provided');
  }
  return value;
}

export const PHASE_LABELS: Record<Phase, string> = {
  draft: 'Подготовка',
  idle: 'Готово к следующему этапу',
  working: 'Команда работает',
  waiting: 'Нужен ваш ответ',
  review: 'Проверьте результат',
  failed: 'Запуск не удался',
  ready: 'Все результаты приняты',
  done: 'Проект завершён',
};

export function phaseColor(phase: Phase): string {
  if (phase === 'waiting' || phase === 'failed') {
    return 'amber';
  }
  return ['ready', 'done', 'review'].includes(phase) ? 'green' : '';
}

export const MEDIA = {
  team: '/assets/v9/team.png',
  guide: '/assets/v9/guide.png',
  result: '/assets/v9/result.png',
  book: '/assets/book-spread.png',
  video: '/assets/CoreTeams-v1-720p.mp4',
};
