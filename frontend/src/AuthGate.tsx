import { useCallback, useEffect, useState, type FormEvent } from 'react';
import Shell from './coreteams/Shell';
import {
  authenticate,
  fetchCurrentUser,
  logout,
  setUnauthorizedHandler,
  type AuthMode,
  type User,
} from './auth';

const SELECTED_CONVERSATION_KEY = 'claude-web.selected-conversation';

function AuthScreen({
  notice,
  onAuthenticated,
}: {
  notice: string | null;
  onAuthenticated: (user: User) => void;
}) {
  const [mode, setMode] = useState<AuthMode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);
    try {
      onAuthenticated(await authenticate(mode, email.trim(), password));
    } catch (authError) {
      setError(authError instanceof Error ? authError.message : 'Не удалось войти');
      setIsSubmitting(false);
    }
  };

  const isLogin = mode === 'login';
  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={(event) => void submit(event)}>
        <div className="eyebrow">Claude Web</div>
        <h1>{isLogin ? 'Вход' : 'Регистрация'}</h1>
        {notice && <div className="auth-notice">{notice}</div>}

        <label className="auth-field">
          <span>Email</span>
          <input
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
            autoFocus
          />
        </label>
        <label className="auth-field">
          <span>Пароль</span>
          <input
            type="password"
            autoComplete={isLogin ? 'current-password' : 'new-password'}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            minLength={8}
            required
          />
          {!isLogin && <small>Не короче 8 символов</small>}
        </label>

        {error && <div className="auth-error" role="alert">{error}</div>}

        <button className="auth-submit" type="submit" disabled={isSubmitting}>
          {isSubmitting ? 'Подождите…' : isLogin ? 'Войти' : 'Зарегистрироваться'}
        </button>
        <button
          className="auth-switch"
          type="button"
          onClick={() => {
            setMode(isLogin ? 'register' : 'login');
            setError(null);
          }}
        >
          {isLogin ? 'Нет аккаунта? Зарегистрироваться' : 'Уже есть аккаунт? Войти'}
        </button>
      </form>
    </div>
  );
}

function AuthGate() {
  // undefined — сессия ещё проверяется.
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchCurrentUser()
      .then((current) => {
        if (!cancelled) {
          setUser(current);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setUser(null);
          setNotice('Backend недоступен. Попробуйте войти позже.');
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    // Срабатывает только из открытого чата: его API и сокет требуют входа.
    setUnauthorizedHandler(() => {
      setNotice('Сессия истекла. Войдите снова.');
      setUser(null);
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const handleLogout = useCallback(async () => {
    await logout().catch(() => undefined);
    window.localStorage.removeItem(SELECTED_CONVERSATION_KEY);
    setNotice(null);
    setUser(null);
  }, []);

  if (user === undefined) {
    return <div className="auth-shell"><div className="sidebar-state">Проверка сессии…</div></div>;
  }
  if (user === null) {
    return (
      <AuthScreen
        notice={notice}
        onAuthenticated={(authenticated) => {
          setNotice(null);
          setUser(authenticated);
        }}
      />
    );
  }
  // key: смена пользователя пересоздаёт чат вместе с сокетом и всем состоянием.
  return <Shell key={user.id} user={user} onLogout={() => void handleLogout()} />;
}

export default AuthGate;
