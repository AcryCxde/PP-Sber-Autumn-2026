import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import App from '../App';
import type { User } from '../auth';
import { themeToggleLabel, useTheme } from '../theme';
import { api, type Catalog as ScenarioCatalog } from './api';
import { errorText } from './actions';
import { ShellContext } from './context';
import { AboutDialog, Catalog, Home, Profile } from './Pages';
import { ProjectPage } from './ProjectPage';
import { useRoute } from './router';
import { SocketContext, useGatewaySocket } from './socket';
import { Modal } from './ui';
import './coreteams.css';
import './coreteams-extra.css';

function Brand() {
  return (
    <a className="brand" href="#home" aria-label="CoreTeams — главная">
      <svg viewBox="0 0 64 64" aria-hidden="true">
        <rect width="64" height="64" rx="17" fill="#2a2338" />
        <path d="M28 29 36 24M28 35l8 5" stroke="white" strokeWidth="3" />
        <circle cx="22" cy="32" r="8" fill="#a487ff" />
        <circle cx="42" cy="22" r="6" fill="#ffb829" />
        <circle cx="42" cy="42" r="6" fill="#55d7a5" />
      </svg>
      <span>core<b>teams</b></span>
    </a>
  );
}

const NAV = [
  ['home', 'Главная'],
  ['catalog', 'Примеры'],
  ['profile', 'Мои проекты'],
] as const;

export default function Shell({ user, onLogout }: { user: User; onLogout: () => void }) {
  const route = useRoute();
  const socket = useGatewaySocket();
  const [theme, toggleTheme] = useTheme();
  const [catalog, setCatalog] = useState<ScenarioCatalog | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ title: string; content: ReactNode } | null>(null);
  // id различает одинаковые сообщения подряд: каждое показывается заново.
  const [toastState, setToastState] = useState<{ id: number; text: string } | null>(null);
  const toastText = toastState?.text ?? null;

  useEffect(() => {
    api.scenarios().then(setCatalog).catch((error: unknown) => {
      setLoadError(errorText(error, 'Не удалось загрузить сценарии'));
    });
  }, []);

  useEffect(() => {
    if (!toastState) return;
    const timer = window.setTimeout(() => setToastState(null), 4500);
    return () => window.clearTimeout(timer);
  }, [toastState]);

  const toast = useCallback((text: string) => {
    setToastState((previous) => ({ id: (previous?.id ?? 0) + 1, text }));
  }, []);
  const closeDialog = useCallback(() => setDialog(null), []);
  const openDialog = useCallback((title: string, content: ReactNode) => setDialog({ title, content }), []);

  const shell = useMemo(
    () => (catalog ? { user, catalog, toast, openDialog, closeDialog } : null),
    [catalog, user, toast, openDialog, closeDialog],
  );

  if (route.page === 'chat') {
    // Прежний интерфейс диалогов со своим соединением и раскладкой.
    return <App user={user} onLogout={onLogout} />;
  }

  let page: ReactNode = <div className="page"><p className="helper">{loadError ?? 'Загрузка…'}</p></div>;
  if (shell) {
    if (route.page === 'catalog') page = <Catalog />;
    else if (route.page === 'profile') page = <Profile />;
    else if (route.page === 'project') page = <ProjectPage key={route.projectId} route={route} />;
    else page = <Home />;
  }
  const current = route.page === 'project' ? null : route.page;

  return (
    <SocketContext.Provider value={socket}>
      <div className="ct ct-root">
        <a className="skip-link" href="#main" onClick={(event) => {
          event.preventDefault();
          document.getElementById('main')?.focus();
        }}>Перейти к содержимому</a>
        <header className="site-header">
          <Brand />
          <nav aria-label="Основная навигация">
            {NAV.map(([key, label]) => (
              <a key={key} href={`#${key}`} aria-current={current === key ? 'page' : undefined}>{label}</a>
            ))}
          </nav>
          <span className="demo-label ct-account">
            <button
              className="ct-theme-toggle"
              type="button"
              onClick={toggleTheme}
              aria-label={themeToggleLabel(theme)}
              title={themeToggleLabel(theme)}
            >
              {theme === 'light' ? '☾' : '☀'}
            </button>
            <span title={user.email}>{user.email}</span>
            <button className="text-button" type="button" onClick={onLogout}>Выйти</button>
          </span>
        </header>
        <main id="main" tabIndex={-1}>
          {shell ? <ShellContext.Provider value={shell}>{page}</ShellContext.Provider> : page}
        </main>
        <footer className="site-footer">
          <span>CoreTeams · команда вокруг вашей задачи</span>
          <button className="text-button" type="button" onClick={() => openDialog('Как работает CoreTeams', <AboutDialog />)}>
            Как работает CoreTeams
          </button>
        </footer>
        {shell && (
          <ShellContext.Provider value={shell}>
            <Modal title={dialog?.title ?? null} content={dialog?.content} onClose={closeDialog} />
          </ShellContext.Provider>
        )}
        <div className="toast" role="status" aria-live="polite" hidden={!toastText}>{toastText}</div>
      </div>
    </SocketContext.Provider>
  );
}
