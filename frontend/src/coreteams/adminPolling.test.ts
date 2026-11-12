import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAdminPolling, pollStateForLoader } from './adminPolling';
import { AdminApiError } from './adminApi';
import { UnauthorizedError } from '../auth';

const sample = { as_of: '2026-11-12T00:00:00Z', value: 1 };
const settle = () => vi.advanceTimersByTimeAsync(0);
afterEach(() => vi.useRealTimers());
describe('admin polling', () => {
  it('never exposes the previous loader snapshot while a replacement is loading or fails', async () => {
    vi.useFakeTimers();
    const first = vi.fn().mockResolvedValue(sample);
    const second = vi.fn().mockRejectedValue(new Error('network'));
    const old = createAdminPolling(first);
    old.start(); await settle();
    const snapshot = { load: first, state: old.state() };
    expect(pollStateForLoader(snapshot, second)).toMatchObject({ status: 'loading', data: null, asOf: null, lastSuccess: null });
    const replacement = createAdminPolling(second);
    replacement.start(); await settle();
    expect(pollStateForLoader({ load: second, state: replacement.state() }, second)).toMatchObject({ status: 'error', data: null });
    expect(pollStateForLoader(snapshot, first).data).toBe(sample);
    old.dispose(); replacement.dispose();
  });
  it('loads, polls at 15 seconds and pauses while hidden', async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockResolvedValue(sample);
    const poll = createAdminPolling(load);
    expect(poll.state().status).toBe('loading');
    poll.start(); await settle();
    expect(load).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(15000);
    expect(load).toHaveBeenCalledTimes(2);
    poll.visible(false);
    await vi.advanceTimersByTimeAsync(60000);
    expect(load).toHaveBeenCalledTimes(2);
    poll.visible(true); await settle();
    expect(load).toHaveBeenCalledTimes(3);
    poll.dispose();
  });
  it('retains last success on failures, marks stale and caps backoff', async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockResolvedValueOnce(sample).mockRejectedValue(new Error('network'));
    const poll = createAdminPolling(load);
    poll.start(); await settle();
    const success = poll.state().lastSuccess;
    await vi.advanceTimersByTimeAsync(46000);
    expect(poll.state()).toMatchObject({ data: sample, refreshError: true, stale: true, lastSuccess: success });
    poll.dispose();
  });
  it('a repeated old server snapshot does not refresh data age', async () => {
    vi.useFakeTimers();
    const poll = createAdminPolling(vi.fn().mockResolvedValue(sample));
    poll.start(); await settle();
    await vi.advanceTimersByTimeAsync(46000);
    expect(poll.state().stale).toBe(true);
    poll.dispose();
  });
  it('uses bounded 15/30/60 second backoff and does not shift last success on failure', async () => {
    vi.useFakeTimers();
    const load = vi.fn().mockRejectedValue(new Error('network'));
    const poll = createAdminPolling(load);
    poll.start(); await settle();
    await vi.advanceTimersByTimeAsync(15000); expect(load).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(30000); expect(load).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(60000); expect(load).toHaveBeenCalledTimes(4);
    await vi.advanceTimersByTimeAsync(60000); expect(load).toHaveBeenCalledTimes(5);
    expect(poll.state().lastSuccess).toBeNull();
    poll.dispose();
  });
  it('late success cannot restore a snapshot after a different request reports forbidden', async () => {
    vi.useFakeTimers();
    let resolve!: (value: typeof sample) => void;
    const poll = createAdminPolling(() => new Promise<typeof sample>((done) => { resolve = done; }));
    poll.start(); poll.revoke('forbidden'); resolve(sample); await settle();
    expect(poll.state()).toMatchObject({ status: 'forbidden', data: null, asOf: null });
    poll.dispose();
  });
  it('aborts old generation, ignores late responses and never overlaps refresh', async () => {
    vi.useFakeTimers();
    const resolves: ((value: typeof sample) => void)[] = [];
    const load = vi.fn(() => new Promise<typeof sample>((resolve) => resolves.push(resolve)));
    const poll = createAdminPolling(load);
    poll.start(); poll.refresh();
    expect(load).toHaveBeenCalledTimes(1);
    poll.replace(load);
    expect(load).toHaveBeenCalledTimes(2);
    resolves[1]({ ...sample, value: 2 }); await settle();
    resolves[0](sample); await settle();
    expect(poll.state().data?.value).toBe(2);
    poll.dispose();
  });
  it.each([new UnauthorizedError(), new AdminApiError(403, 'forbidden')])('clears caches and stops after auth loss', async (error) => {
    vi.useFakeTimers();
    const revoked = vi.fn();
    const load = vi.fn().mockResolvedValueOnce(sample).mockRejectedValueOnce(error);
    const poll = createAdminPolling(load, revoked);
    poll.start(); await settle();
    await vi.advanceTimersByTimeAsync(15000);
    expect(poll.state().data).toBeNull();
    expect(revoked).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(60000);
    expect(load).toHaveBeenCalledTimes(2);
    poll.dispose();
  });
});
