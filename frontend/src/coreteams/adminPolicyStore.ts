import type { PolicyDraft } from './adminUsersState';

export function createAdminPolicyStore() {
  let draft: PolicyDraft | null = null;
  const listeners = new Set<() => void>();
  function set(next: PolicyDraft | null) {
    if (draft === next) return;
    draft = next;
    for (const listener of listeners) listener();
  }
  return {
    get: () => draft,
    set,
    clear: () => set(null),
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    leave() {
      if (!draft?.attempted) set(null);
      else if (draft.status === 'submitting') set({ ...draft, status: 'editing', error: 'Ответ на предыдущую попытку не подтверждён. Повторите подтверждение с сохранёнными данными.' });
    },
  };
}
export type AdminPolicyStore = ReturnType<typeof createAdminPolicyStore>;
