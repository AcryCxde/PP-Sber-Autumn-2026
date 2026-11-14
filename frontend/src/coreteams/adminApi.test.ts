import { afterEach, describe, expect, it, vi } from 'vitest';
import { adminApi, AdminApiError, parseOverview } from './adminApi';
import { setUnauthorizedHandler, UnauthorizedError } from '../auth';

afterEach(() => { setUnauthorizedHandler(null); vi.unstubAllGlobals(); vi.useRealTimers(); });
const mutation = { frozen: true, reason: 'Причина', expected_version: 0 };
const mutationResult = { user: { id: 'u', email: 'u@example.ru', created_at: '2026-11-12T00:00:00Z', last_seen_at: null, ai_access: { frozen: true, version: 1 } }, audit_id: 'a', changed_at: '2026-11-12T00:00:00Z' };

describe('AI-access mutation deadline', () => {
  it('times out an abort-ignoring fetch and retries the exact payload and key', async () => {
    vi.useFakeTimers();
    let finish!: (response: Response) => void;
    const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve; }))
      .mockResolvedValueOnce(new Response(JSON.stringify(mutationResult)));
    vi.stubGlobal('fetch', fetch);
    const first = adminApi.changeAiAccess('u', mutation, 'stable-key');
    const outcome = expect(first).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(30000);
    await outcome;
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    const retry = await adminApi.changeAiAccess('u', mutation, 'stable-key');
    expect(retry.audit_id).toBe('a');
    expect(fetch.mock.calls[1][1].body).toBe(fetch.mock.calls[0][1].body);
    expect(fetch.mock.calls[1][1].headers).toEqual(fetch.mock.calls[0][1].headers);
    finish(new Response(JSON.stringify({ ...mutationResult, audit_id: 'late' })));
    await Promise.resolve();
    expect(retry.audit_id).toBe('a');
    expect(vi.getTimerCount()).toBe(0);
  });
  it.each(['deadline', 'unmount'])('ignores a late 401 after %s while preserving active 401 handling', async (cancellation) => {
    vi.useFakeTimers();
    const unauthorized = vi.fn(); setUnauthorizedHandler(unauthorized);
    let finish!: (response: Response) => void;
    const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { finish = resolve; }))
      .mockResolvedValueOnce(new Response(JSON.stringify(mutationResult)))
      .mockResolvedValueOnce(new Response('', { status: 401 }));
    vi.stubGlobal('fetch', fetch);
    const controller = new AbortController();
    const outcome = expect(adminApi.changeAiAccess('u', mutation, 'stable-key', controller.signal)).rejects.toThrow();
    if (cancellation === 'deadline') await vi.advanceTimersByTimeAsync(30000);
    else controller.abort();
    await outcome;
    await expect(adminApi.changeAiAccess('u', mutation, 'stable-key')).resolves.toMatchObject({ audit_id: 'a' });
    finish(new Response('', { status: 401 }));
    await vi.advanceTimersByTimeAsync(0);
    expect(unauthorized).not.toHaveBeenCalled();
    await expect(adminApi.changeAiAccess('u', mutation, 'active-key')).rejects.toBeInstanceOf(UnauthorizedError);
    expect(unauthorized).toHaveBeenCalledTimes(1);
  });
  it('bounds a response body that never finishes, not only the fetch', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => new Promise(() => {}) }));
    const outcome = expect(adminApi.changeAiAccess('u', mutation, 'key')).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(30000);
    await outcome;
    expect(vi.getTimerCount()).toBe(0);
  });
  it('does not send a request after cancellation and clears the deadline after success', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(mutationResult)));
    vi.stubGlobal('fetch', fetch);
    const cancelled = new AbortController(); cancelled.abort();
    await expect(adminApi.changeAiAccess('u', mutation, 'key', cancelled.signal)).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    await expect(adminApi.changeAiAccess('u', mutation, 'key')).resolves.toMatchObject({ audit_id: 'a' });
    expect(vi.getTimerCount()).toBe(0);
  });
  it('settles on external cancellation even when fetch ignores abort', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise(() => {})));
    const controller = new AbortController();
    const outcome = expect(adminApi.changeAiAccess('u', mutation, 'key', controller.signal)).rejects.toThrow();
    controller.abort();
    await outcome;
    expect(vi.getTimerCount()).toBe(0);
  });
});
describe('admin API boundary', () => {
  it('rejects malformed snapshots rather than displaying fabricated zeros', () => {
    expect(() => parseOverview({ as_of: 'invalid', totals: {} })).toThrow();
  });
  it('encodes search and passes abort signals', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ as_of: '2026-11-12T00:00:00Z', items: [], total: 0, page: 1, page_size: 25 })));
    vi.stubGlobal('fetch', fetch);
    const signal = new AbortController().signal;
    await adminApi.users({ q: 'a+b@example.ru', page: 1 }, signal);
    expect(fetch.mock.calls[0][0]).toContain('q=a%2Bb%40example.ru');
    expect(fetch.mock.calls[0][1].signal).toBe(signal);
  });
  it('preserves structured conflicts and sends mutation guards', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ code: 'policy_version_conflict', detail: 'Обновите данные', current: { frozen: true, version: 2 } }), { status: 409 }));
    vi.stubGlobal('fetch', fetch);
    await expect(adminApi.changeAiAccess('u1', { frozen: true, reason: 'Причина', expected_version: 1 }, 'key')).rejects.toMatchObject({ status: 409, code: 'policy_version_conflict', current: { version: 2 } });
    expect(fetch.mock.calls[0][1].headers).toMatchObject({ 'Idempotency-Key': 'key', 'X-CoreTeams-Admin': '1' });
  });
  it('401 throws UnauthorizedError and 403 remains distinguishable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 401 })));
    await expect(adminApi.users({ page: 1 })).rejects.toBeInstanceOf(UnauthorizedError);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 403 })));
    await expect(adminApi.users({ page: 1 })).rejects.toBeInstanceOf(AdminApiError);
  });
  it('does not expose an untrusted server error body to the UI', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'SECRET', code: 'untrusted' }), { status: 500 })));
    await expect(adminApi.users({ page: 1 })).rejects.toMatchObject({ message: 'Не удалось выполнить запрос администратора.' });
  });
});
