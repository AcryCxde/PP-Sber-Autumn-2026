import { useEffect, useState } from 'react';
import type { SetupStep } from './api';

export type Route =
  | { page: 'home' | 'catalog' | 'profile' | 'chat' }
  | { page: 'project'; projectId: string; step: SetupStep | 'workspace'; question: number };

const STEPS = new Set(['welcome', 'context', 'question', 'team-plan', 'plan', 'workspace']);

/** Хеш-маршруты как в прототипе v9: #home, #catalog, #profile, #project/{id}/{шаг}[/{вопрос}]. */
export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, '').split('/');
  if (parts[0] === 'catalog' || parts[0] === 'profile' || parts[0] === 'chat') {
    return { page: parts[0] };
  }
  if (parts[0] === 'project' && parts[1]) {
    const step = STEPS.has(parts[2]) ? (parts[2] as SetupStep | 'workspace') : 'workspace';
    const question = Math.min(2, Math.max(0, Number(parts[3]) || 0));
    return { page: 'project', projectId: parts[1], step, question };
  }
  return { page: 'home' };
}

export function routeHash(route: Route): string {
  if (route.page !== 'project') {
    return `#${route.page}`;
  }
  const question = route.step === 'question' ? `/${route.question}` : '';
  return `#project/${route.projectId}/${route.step}${question}`;
}

export function navigate(route: Route): void {
  const hash = routeHash(route);
  if (window.location.hash !== hash) {
    window.location.hash = hash;
  }
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const update = () => {
      setRoute(parseRoute(window.location.hash));
      window.scrollTo({ top: 0 });
    };
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);
  return route;
}
