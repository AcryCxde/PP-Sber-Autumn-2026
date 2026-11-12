import { afterEach, describe, expect, it, vi } from 'vitest';
import { adminApi, AdminApiError, parseOverview } from './adminApi';
import { UnauthorizedError } from '../auth';

afterEach(() => vi.unstubAllGlobals());
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
