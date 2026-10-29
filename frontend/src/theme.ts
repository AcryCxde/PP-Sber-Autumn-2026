import { useEffect, useState } from 'react';

export type Theme = 'light' | 'dark';

// Тот же ключ читает скрипт в index.html, чтобы тема применялась до первого рендера.
const THEME_KEY = 'claude-web.theme';

function initialTheme(): Theme {
  const documentTheme = document.documentElement.dataset.theme;
  if (documentTheme === 'light' || documentTheme === 'dark') {
    return documentTheme;
  }
  try {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark') {
      return saved;
    }
  } catch {
    // Хранилище недоступно — берём системную тему.
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

/** Тема всего приложения: атрибут data-theme на <html> и выбор пользователя в localStorage. */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      // Выбор просто не запомнится.
    }
  }, [theme]);

  return [theme, () => setTheme((current) => (current === 'light' ? 'dark' : 'light'))];
}

export function themeToggleLabel(theme: Theme): string {
  return theme === 'light' ? 'Включить тёмную тему' : 'Включить светлую тему';
}
