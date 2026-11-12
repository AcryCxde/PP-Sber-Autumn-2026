import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { AdminApiError } from './adminApi';
import { UnauthorizedError } from '../auth';
import { createAdminPolling, initialPollState, pollStateForLoader, type AdminLoader, type PollState } from './adminPolling';

export const AdminAccessContext = createContext<(status: 'unauthorized' | 'forbidden') => void>(() => {});
export function useAdminPolling<T extends { as_of: string }>(load: AdminLoader<T>) {
  const revoke = useContext(AdminAccessContext);
  const [snapshot, setSnapshot] = useState<{ load: AdminLoader<T>; state: PollState<T> }>(() => ({ load, state: initialPollState<T>() }));
  const state = pollStateForLoader(snapshot, load);
  const polling = useRef<ReturnType<typeof createAdminPolling<T>> | null>(null);
  useEffect(() => {
    const controller: ReturnType<typeof createAdminPolling<T>> = createAdminPolling(load, (): void => { revoke(controller.state().status === 'unauthorized' ? 'unauthorized' : 'forbidden'); });
    polling.current = controller;
    const unsubscribe = controller.subscribe(() => setSnapshot({ load, state: controller.state() }));
    const visibility = () => controller.visible(document.visibilityState !== 'hidden');
    visibility(); controller.start(); document.addEventListener('visibilitychange', visibility);
    return () => { unsubscribe(); controller.dispose(); document.removeEventListener('visibilitychange', visibility); };
  }, [load, revoke]);
  return { ...state, refresh: () => polling.current?.refresh() };
}
export function useAdminFailure() {
  const revoke = useContext(AdminAccessContext);
  return (error: unknown): boolean => {
    if (error instanceof UnauthorizedError) { revoke('unauthorized'); return true; }
    if (error instanceof AdminApiError && error.status === 403) { revoke('forbidden'); return true; }
    return false;
  };
}
