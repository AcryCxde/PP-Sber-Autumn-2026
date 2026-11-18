import { UnauthorizedError } from '../auth';
import { AdminApiError } from './adminApi';

export type PollState<T> = {
  status: 'loading' | 'ready' | 'error' | 'unauthorized' | 'forbidden'; data: T | null;
  lastSuccess: number | null; asOf: string | null; stale: boolean; refreshError: boolean; refreshing: boolean;
};
export type AdminLoader<T> = (signal: AbortSignal) => Promise<T>;
export function initialPollState<T>(): PollState<T> { return { status: 'loading', data: null, lastSuccess: null, asOf: null, stale: false, refreshError: false, refreshing: false }; }
export function pollStateForLoader<T>(snapshot: { load: AdminLoader<T>; state: PollState<T> }, load: AdminLoader<T>): PollState<T> {
  return snapshot.load === load ? snapshot.state : initialPollState<T>();
}
export function createAdminPolling<T extends { as_of: string }>(initialLoad: AdminLoader<T>, revoked: () => void = () => {}) {
  let value: PollState<T> = initialPollState<T>();
  let load = initialLoad; let generation = 0; let controller: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined; let staleTimer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false; let hidden = false; let stopped = false; let failures = 0;
  let serverAnchor: { server: number; local: number } | null = null;
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach((listener) => listener());
  const update = (patch: Partial<PollState<T>>) => { value = { ...value, ...patch }; emit(); };
  const clear = () => { clearTimeout(timer); clearTimeout(staleTimer); };
  const schedule = () => { if (!disposed && !hidden && !stopped) timer = setTimeout(() => { void refresh(); }, Math.min(60000, 15000 * 2 ** Math.max(0, failures - 1))); };
  const revoke = (status: 'unauthorized' | 'forbidden') => {
    stopped = true; generation++; controller?.abort(); controller = null; clear();
    update({ status, data: null, lastSuccess: null, asOf: null, stale: false, refreshing: false, refreshError: false }); revoked();
  };
  async function refresh() {
    if (disposed || hidden || stopped || controller) return;
    clearTimeout(timer); const current = generation; const active = new AbortController(); controller = active;
    update({ refreshing: true });
    let timedOut = false;
    let rejectAborted!: (reason: unknown) => void;
    const aborted = new Promise<never>((_, reject) => { rejectAborted = reject; });
    const onAbort = () => rejectAborted(active.signal.reason);
    active.signal.addEventListener('abort', onAbort, { once: true });
    // Race the deadline as well as aborting fetch: a loader may ignore its signal.
    const deadline = setTimeout(() => {
      timedOut = true;
      active.abort(new Error('Admin request deadline exceeded'));
    }, 30000);
    try {
      const data = await Promise.race([load(active.signal), aborted]);
      if (disposed || current !== generation) return;
      failures = 0;
      const local = Date.now(); const server = Date.parse(data.as_of);
      serverAnchor ??= { server, local };
      const snapshotAge = Math.max(0, serverAnchor.server + local - serverAnchor.local - server);
      update({ status: 'ready', data, lastSuccess: local, asOf: data.as_of, stale: snapshotAge > 45000, refreshError: false });
      clearTimeout(staleTimer);
      if (snapshotAge <= 45000) staleTimer = setTimeout(() => update({ stale: true }), 45001 - snapshotAge);
    } catch (error) {
      if (disposed || current !== generation || (active.signal.aborted && !timedOut)) return;
      if (error instanceof UnauthorizedError) { revoke('unauthorized'); return; }
      if (error instanceof AdminApiError && error.status === 403) { revoke('forbidden'); return; }
      failures++;
      update({ status: value.data ? 'ready' : 'error', refreshError: true });
    } finally {
      clearTimeout(deadline);
      active.signal.removeEventListener('abort', onAbort);
      if (current === generation) { controller = null; update({ refreshing: false }); schedule(); }
    }
  }
  return {
    state: () => value,
    subscribe(listener: () => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
    start() { void refresh(); }, refresh() { void refresh(); }, revoke,
    replace(next: AdminLoader<T>) {
      generation++; controller?.abort(); controller = null; clear(); failures = 0; serverAnchor = null; load = next;
      if (stopped || disposed) return;
      update({ status: 'loading', data: null, lastSuccess: null, asOf: null, stale: false, refreshError: false, refreshing: false }); void refresh();
    },
    visible(visible: boolean) {
      if (hidden === !visible) return;
      hidden = !visible; clearTimeout(timer);
      if (visible) void refresh();
    },
    dispose() { disposed = true; generation++; controller?.abort(); controller = null; clear(); listeners.clear(); },
  };
}
