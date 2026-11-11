import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { UNAUTHORIZED_EVENT } from "./api.ts";

export type User = { id: string; email: string; role: string; created_at: string };
type Mode = "login" | "register";

async function currentUser(): Promise<User | null> {
  const response = await fetch("/api/auth/me", { credentials: "same-origin" });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("Не удалось проверить сессию");
  return (await response.json()) as User;
}

function authError(mode: Mode, status: number): string {
  if (status === 401) return "Неверный email или пароль";
  if (status === 409) return "Этот email уже зарегистрирован";
  if (status === 422) return "Проверьте email; пароль должен быть не короче 8 символов";
  return mode === "login" ? "Не удалось войти" : "Не удалось зарегистрироваться";
}

function SignIn({ notice, onSignedIn }: { notice: string; onSignedIn: (user: User) => void }) {
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const login = mode === "login";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const response = await fetch(`/api/auth/${mode}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim(), password }),
      });
      if (!response.ok) throw new Error(authError(mode, response.status));
      onSignedIn((await response.json()) as User);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось войти");
      setBusy(false);
    }
  }

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={(e) => void submit(e)}>
        <p className="eyebrow">CoreTeams</p>
        <h1>{login ? "Вход" : "Регистрация"}</h1>
        <p className="muted">Рабочее пространство с AI-командой для сложных задач вне одной экспертизы.</p>
        {notice ? <p className="notice" role="status">{notice}</p> : null}
        <label>
          Email
          <input type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
        </label>
        <label>
          Пароль
          <input
            type="password"
            autoComplete={login ? "current-password" : "new-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            required
          />
          {login ? null : <small className="muted">Не короче 8 символов</small>}
        </label>
        {error ? <p className="error" role="alert">{error}</p> : null}
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "Подождите…" : login ? "Войти" : "Зарегистрироваться"}
        </button>
        <button
          className="text-btn"
          type="button"
          onClick={() => {
            setMode(login ? "register" : "login");
            setError("");
          }}
        >
          {login ? "Нет аккаунта? Зарегистрироваться" : "Уже есть аккаунт? Войти"}
        </button>
      </form>
    </main>
  );
}

/** Renders the workspace only for a signed-in user; contract V2 uses the session cookie. */
export function AuthGate({ children }: { children: (user: User, logout: () => void) => ReactNode }) {
  // undefined: the session is still being checked.
  const [user, setUser] = useState<User | null | undefined>(undefined);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let cancelled = false;
    currentUser()
      .then((value) => !cancelled && setUser(value))
      .catch(() => {
        if (cancelled) return;
        setUser(null);
        setNotice("Сервис недоступен. Попробуйте войти позже.");
      });
    const expired = () => {
      setNotice("Сессия истекла. Войдите снова — черновики на этом устройстве сохранены.");
      setUser(null);
    };
    window.addEventListener(UNAUTHORIZED_EVENT, expired);
    return () => {
      cancelled = true;
      window.removeEventListener(UNAUTHORIZED_EVENT, expired);
    };
  }, []);

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }).catch(() => undefined);
    setNotice("");
    setUser(null);
  }

  if (user === undefined) return <main className="auth-page"><p className="muted">Проверка сессии…</p></main>;
  if (user === null)
    return <SignIn notice={notice} onSignedIn={(value) => { setNotice(""); setUser(value); }} />;
  return <>{children(user, () => void logout())}</>;
}
