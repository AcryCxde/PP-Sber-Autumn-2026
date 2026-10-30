export type User = {
  id: string;
  email: string;
  role: string;
  created_at: string;
};

// Backend закрывает /ws этим кодом, если в рукопожатии нет действующей сессии.
export const WS_UNAUTHORIZED = 4401;

export class UnauthorizedError extends Error {
  constructor() {
    super('Требуется вход');
    this.name = 'UnauthorizedError';
  }
}

let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function notifyUnauthorized(): void {
  unauthorizedHandler?.();
}

/** fetch для API, требующего входа: 401 сообщает о потере сессии и прерывает вызывающий код. */
export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, init);
  if (response.status === 401) {
    notifyUnauthorized();
    throw new UnauthorizedError();
  }
  return response;
}

export async function fetchCurrentUser(): Promise<User | null> {
  const response = await fetch('/api/auth/me');
  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new Error('Не удалось проверить сессию');
  }
  return (await response.json()) as User;
}

export type AuthMode = 'login' | 'register';

function authErrorMessage(mode: AuthMode, status: number): string {
  if (status === 401) {
    return 'Неверный email или пароль';
  }
  if (status === 409) {
    return 'Этот email уже зарегистрирован';
  }
  if (status === 422) {
    return 'Проверьте email; пароль должен быть не короче 8 символов';
  }
  return mode === 'login' ? 'Не удалось войти' : 'Не удалось зарегистрироваться';
}

export async function authenticate(
  mode: AuthMode,
  email: string,
  password: string,
): Promise<User> {
  const response = await fetch(`/api/auth/${mode}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    throw new Error(authErrorMessage(mode, response.status));
  }
  return (await response.json()) as User;
}

export async function logout(): Promise<void> {
  await fetch('/api/auth/logout', { method: 'POST' });
}
