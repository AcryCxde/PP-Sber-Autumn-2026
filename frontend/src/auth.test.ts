import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  apiFetch,
  authenticate,
  fetchCurrentUser,
  setUnauthorizedHandler,
  UnauthorizedError,
} from './auth';

function stubFetch(status: number, body: unknown = {}) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe('apiFetch', () => {
  it('notifies and throws on 401', async () => {
    stubFetch(401);
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    await expect(apiFetch('/api/conversations')).rejects.toBeInstanceOf(UnauthorizedError);
    expect(handler).toHaveBeenCalledOnce();
  });

  it('passes other statuses through', async () => {
    stubFetch(404);
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    const response = await apiFetch('/api/conversations/x/messages');
    expect(response.status).toBe(404);
    expect(handler).not.toHaveBeenCalled();
  });
});

describe('auth requests', () => {
  it('treats 401 from /me as signed out without notifying', async () => {
    stubFetch(401);
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    await expect(fetchCurrentUser()).resolves.toBeNull();
    expect(handler).not.toHaveBeenCalled();
  });

  it('maps backend errors to readable messages', async () => {
    stubFetch(409);
    await expect(authenticate('register', 'a@example.com', 'password1')).rejects.toThrow(
      'Этот email уже зарегистрирован',
    );
    stubFetch(401);
    await expect(authenticate('login', 'a@example.com', 'password1')).rejects.toThrow(
      'Неверный email или пароль',
    );
  });

  it('posts credentials as JSON', async () => {
    const fetchMock = stubFetch(200, { id: '1', email: 'a@example.com', created_at: '' });

    await expect(authenticate('login', 'a@example.com', 'password1')).resolves.toMatchObject({
      email: 'a@example.com',
    });
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/login', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ email: 'a@example.com', password: 'password1' }),
    }));
  });
});
