import { useEffect, useMemo, useState } from "react";
import type { User } from "./auth.tsx";

/** Engagement panel for the admin role: /api/admin (backend/api/admin.py). */
type Totals = {
  users: number;
  admins: number;
  new_users: number;
  active_1d: number;
  active_7d: number;
  active_30d: number;
  active_period: number;
  returning_period: number;
  conversations: number;
  projects: number;
  runs: number;
  failed_runs: number;
  user_messages: number;
  artifacts: number;
  approvals: number;
  files: number;
  cost_usd: number;
  compute_seconds: number;
};
type Day = { day: string; active_users: number; new_users: number; runs: number; user_messages: number };
type Overview = {
  generated_at: string;
  period_days: number;
  totals: Totals;
  daily: Day[];
  funnel: { key: string; label: string; users: number }[];
};
type Row = {
  id: string;
  email: string;
  role: string;
  created_at: string;
  last_seen_at: string | null;
  last_action_at: string | null;
  active_days: number;
  active_days_period: number;
  conversations: number;
  projects: number;
  runs: number;
  failed_runs: number;
  user_messages: number;
  artifacts: number;
  approvals: number;
  files: number;
  cost_usd: number;
  compute_seconds: number;
};

type Segment = "active" | "cooling" | "dormant" | "idle";
const SEGMENTS: Record<Segment, string> = {
  active: "Активен",
  cooling: "Остывает",
  dormant: "Ушёл",
  idle: "Не начинал",
};
const SERIES = {
  active_users: "Активные пользователи",
  new_users: "Новые регистрации",
  runs: "Запуски AI-команды",
  user_messages: "Сообщения пользователей",
} as const;
type Series = keyof typeof SERIES;
type SortKey = "email" | "last" | "active_days_period" | "projects" | "runs" | "artifacts" | "approvals" | "cost_usd";
const PERIODS = [7, 30, 90];
const DAY_MS = 86_400_000;

async function load<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (response.status === 403) throw new Error("Нужна роль администратора");
  if (!response.ok) throw new Error(`Сервис вернул ошибку ${response.status}`);
  return (await response.json()) as T;
}

function lastActivity(row: Row): number | null {
  const times = [row.last_seen_at, row.last_action_at].filter(Boolean).map((t) => Date.parse(t!));
  return times.length ? Math.max(...times) : null;
}

function segment(row: Row, now: number): Segment {
  if (!row.runs && !row.conversations && !row.projects) return "idle";
  const last = lastActivity(row);
  if (last !== null && now - last <= 7 * DAY_MS) return "active";
  if (last !== null && now - last <= 30 * DAY_MS) return "cooling";
  return "dormant";
}

const number = new Intl.NumberFormat("ru-RU");
const percent = (part: number, whole: number) => (whole ? `${Math.round((part / whole) * 100)}%` : "—");
const usd = (value: number) => `$${value.toFixed(value >= 10 ? 0 : 2)}`;

function ago(time: number | null, now: number): string {
  if (time === null) return "никогда";
  const minutes = Math.round((now - time) / 60_000);
  if (minutes < 60) return minutes <= 1 ? "только что" : `${minutes} мин назад`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ч назад`;
  const days = Math.round(hours / 24);
  return days < 60 ? `${days} дн назад` : new Date(time).toLocaleDateString("ru-RU");
}

function shortDay(day: string): string {
  return new Date(`${day}T00:00:00`).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

function csv(rows: Row[], now: number): string {
  const head = ["email", "role", "segment", "registered", "last_activity", "active_days", "active_days_period", "conversations", "projects", "runs", "failed_runs", "user_messages", "artifacts", "approvals", "files", "cost_usd", "compute_seconds"];
  const lines = rows.map((r) => {
    const last = lastActivity(r);
    return [r.email, r.role, segment(r, now), r.created_at, last ? new Date(last).toISOString() : "", r.active_days, r.active_days_period, r.conversations, r.projects, r.runs, r.failed_runs, r.user_messages, r.artifacts, r.approvals, r.files, r.cost_usd, r.compute_seconds]
      .map((v) => `"${String(v).replace(/"/g, '""')}"`)
      .join(",");
  });
  return [head.join(","), ...lines].join("\n");
}

function download(name: string, content: string) {
  const url = URL.createObjectURL(new Blob(["﻿" + content], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="admin-tile">
      <span>{label}</span>
      <strong>{value}</strong>
      {note ? <small>{note}</small> : null}
    </div>
  );
}

function DailyChart({ daily, series }: { daily: Day[]; series: Series }) {
  const [hover, setHover] = useState<number>();
  const width = 760;
  const height = 200;
  const top = 12;
  const bottom = 26;
  const left = 32;
  const max = Math.max(1, ...daily.map((d) => d[series]));
  const ticks = max <= 4 ? Array.from({ length: max + 1 }, (_, i) => i) : [0, Math.round(max / 2), max];
  const plot = height - top - bottom;
  const slot = (width - left) / daily.length;
  const bar = Math.max(2, Math.min(28, slot - 2));
  const y = (value: number) => top + plot - (value / max) * plot;
  const labelEvery = Math.ceil(daily.length / 8);
  const shown = hover === undefined ? undefined : daily[hover];

  return (
    <figure className="admin-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${SERIES[series]} по дням`} onMouseLeave={() => setHover(undefined)}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={left} x2={width} y1={y(t)} y2={y(t)} className="admin-grid" />
            <text x={left - 8} y={y(t) + 4} textAnchor="end" className="admin-axis">
              {t}
            </text>
          </g>
        ))}
        {daily.map((d, i) => {
          const x = left + i * slot + (slot - bar) / 2;
          const value = d[series];
          const h = Math.max(value ? 2 : 0, top + plot - y(value));
          const r = Math.min(4, bar / 2, h);
          return (
            <g key={d.day} onMouseEnter={() => setHover(i)}>
              <rect x={left + i * slot} y={top} width={slot} height={plot} fill="transparent" />
              {h > 0 ? (
                <path
                  className={hover === i ? "admin-bar is-hover" : "admin-bar"}
                  d={`M${x},${top + plot} V${top + plot - h + r} Q${x},${top + plot - h} ${x + r},${top + plot - h} H${x + bar - r} Q${x + bar},${top + plot - h} ${x + bar},${top + plot - h + r} V${top + plot} Z`}
                />
              ) : null}
              {i % labelEvery === 0 ? (
                <text x={x + bar / 2} y={height - 8} textAnchor="middle" className="admin-axis">
                  {shortDay(d.day)}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
      {shown ? (
        <div
          className="admin-tooltip"
          style={{ left: `${((left + (hover! + 0.5) * slot) / width) * 100}%`, top: `${(y(shown[series]) / height) * 100}%` }}
        >
          <small>{shortDay(shown.day)}</small>
          <strong>{number.format(shown[series])}</strong>
          <small>{SERIES[series].toLowerCase()}</small>
        </div>
      ) : null}
    </figure>
  );
}

function Funnel({ steps }: { steps: Overview["funnel"] }) {
  const total = steps[0]?.users ?? 0;
  return (
    <ol className="admin-funnel">
      {steps.map((step, i) => (
        <li key={step.key}>
          <div>
            <span>{step.label}</span>
            <strong>
              {number.format(step.users)} <small>{percent(step.users, total)}</small>
            </strong>
          </div>
          <span className="admin-funnel-track">
            <span style={{ width: total ? `${(step.users / total) * 100}%` : 0 }} />
          </span>
          {i > 0 ? <small>из предыдущего шага: {percent(step.users, steps[i - 1].users)}</small> : null}
        </li>
      ))}
    </ol>
  );
}

export function AdminPanel({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [days, setDays] = useState(30);
  const [overview, setOverview] = useState<Overview>();
  const [rows, setRows] = useState<Row[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [series, setSeries] = useState<Series>("active_users");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Segment | "all">("all");
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({ key: "last", desc: true });
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    Promise.all([load<Overview>(`/api/admin/overview?days=${days}`), load<Row[]>(`/api/admin/users?days=${days}`)])
      .then(([o, r]) => {
        if (cancelled) return;
        setOverview(o);
        setRows(r);
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [days, reload]);

  const now = overview ? Date.parse(overview.generated_at) : Date.now();
  const counts = useMemo(() => {
    const result: Record<Segment, number> = { active: 0, cooling: 0, dormant: 0, idle: 0 };
    rows.forEach((r) => result[segment(r, now)]++);
    return result;
  }, [rows, now]);
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const value = (r: Row): number | string =>
      sort.key === "email" ? r.email : sort.key === "last" ? (lastActivity(r) ?? 0) : r[sort.key];
    return rows
      .filter((r) => (filter === "all" || segment(r, now) === filter) && r.email.includes(needle))
      .sort((a, b) => {
        const [x, y] = [value(a), value(b)];
        const order = typeof x === "string" ? x.localeCompare(y as string) : x - (y as number);
        return sort.desc ? -order : order;
      });
  }, [rows, query, filter, sort, now]);

  const t = overview?.totals;
  const header = (key: SortKey, label: string) => (
    <th aria-sort={sort.key === key ? (sort.desc ? "descending" : "ascending") : "none"}>
      <button
        className="text-btn"
        onClick={() => setSort((s) => ({ key, desc: s.key === key ? !s.desc : key !== "email" }))}
      >
        {label}
        {sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}
      </button>
    </th>
  );

  return (
    <div className="app admin-app">
      <header className="topbar">
        <a className="brand" href="/">
          <span aria-hidden>C</span>CoreTeams
        </a>
        <span className="top-caption">Администрирование</span>
        <nav aria-label="Основная навигация">
          <a className="text-btn" href="/">
            Интерфейс CoreTeams
          </a>
        </nav>
        <span className="account">
          <span title={user.email}>{user.email}</span>
          <button className="text-btn" onClick={onLogout}>
            Выйти
          </button>
        </span>
      </header>
      <main id="main" className="admin-page">
        <div className="section-heading admin-heading">
          <div>
            <p className="eyebrow">ПАНЕЛЬ АДМИНИСТРАТОРА</p>
            <h1>Вовлечённость пользователей</h1>
          </div>
          <div className="admin-controls">
            <div className="admin-segmented" role="group" aria-label="Период">
              {PERIODS.map((p) => (
                <button key={p} aria-pressed={days === p} onClick={() => setDays(p)}>
                  {p} дн
                </button>
              ))}
            </div>
            <button className="secondary" onClick={() => setReload((n) => n + 1)} disabled={loading}>
              {loading ? "Загрузка…" : "Обновить"}
            </button>
          </div>
        </div>
        {error ? <p className="error" role="alert">Не удалось загрузить статистику: {error}</p> : null}
        {t && overview ? (
          <>
            <section className="admin-tiles" aria-label="Ключевые показатели">
              <Tile label="Пользователи" value={number.format(t.users)} note={`+${t.new_users} за ${days} дн`} />
              <Tile label="Активны сегодня" value={number.format(t.active_1d)} note="DAU" />
              <Tile label="Активны за 7 дней" value={number.format(t.active_7d)} note="WAU" />
              <Tile label="Активны за 30 дней" value={number.format(t.active_30d)} note={`MAU · DAU/MAU ${percent(t.active_1d, t.active_30d)}`} />
              <Tile
                label="Вернулись"
                value={percent(t.returning_period, t.active_period)}
                note={`${t.returning_period} из ${t.active_period} активных были 2+ дня`}
              />
              <Tile
                label="Запуски AI-команды"
                value={number.format(t.runs)}
                note={`с ошибкой ${percent(t.failed_runs, t.runs)} · ${(t.runs / Math.max(1, t.users)).toFixed(1)} на пользователя`}
              />
              <Tile label="Документы и одобрения" value={`${number.format(t.artifacts)} / ${number.format(t.approvals)}`} note={`проектов ${t.projects}, файлов ${t.files}`} />
              <Tile label="Затраты" value={usd(t.cost_usd)} note={`${Math.round(t.compute_seconds / 60)} мин вычислений`} />
            </section>

            <div className="admin-grid-2">
              <section className="admin-card">
                <div className="section-heading">
                  <h2>{SERIES[series]} по дням</h2>
                  <select value={series} onChange={(e) => setSeries(e.target.value as Series)} aria-label="Показатель графика">
                    {Object.entries(SERIES).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </div>
                <DailyChart daily={overview.daily} series={series} />
                <p className="muted">
                  Активность считается по дням, когда пользователь работал в интерфейсе; история копится с момента
                  включения панели, до этого видны только действия (запуски и сообщения).
                </p>
              </section>
              <section className="admin-card">
                <h2>Воронка за всё время</h2>
                <Funnel steps={overview.funnel} />
              </section>
            </div>

            <section className="admin-card">
              <div className="section-heading">
                <h2>Пользователи</h2>
                <button className="secondary" onClick={() => download(`coreteams-users-${days}d.csv`, csv(visible, now))} disabled={!visible.length}>
                  Скачать CSV
                </button>
              </div>
              <div className="admin-filters">
                <input type="search" placeholder="Поиск по email" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Поиск по email" />
                <div className="admin-segmented" role="group" aria-label="Сегмент">
                  <button aria-pressed={filter === "all"} onClick={() => setFilter("all")}>
                    Все {rows.length}
                  </button>
                  {(Object.keys(SEGMENTS) as Segment[]).map((s) => (
                    <button key={s} aria-pressed={filter === s} onClick={() => setFilter(s)}>
                      {SEGMENTS[s]} {counts[s]}
                    </button>
                  ))}
                </div>
              </div>
              <p className="muted">
                Активен — заходил за 7 дней, остывает — за 30, ушёл — позже; «не начинал» — ни одного проекта и чата.
              </p>
              <div className="admin-table-wrap">
                <table className="admin-table">
                  <thead>
                    <tr>
                      {header("email", "Пользователь")}
                      <th>Статус</th>
                      {header("last", "Последняя активность")}
                      {header("active_days_period", `Дней активности (${days} дн)`)}
                      {header("projects", "Проекты")}
                      {header("runs", "Запуски")}
                      {header("artifacts", "Документы")}
                      {header("approvals", "Одобрения")}
                      {header("cost_usd", "Затраты")}
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((r) => {
                      const s = segment(r, now);
                      return (
                        <tr key={r.id}>
                          <td>
                            <span className="admin-email" title={r.email}>{r.email}</span>
                            <small>
                              {r.role === "admin" ? "админ · " : ""}с {new Date(r.created_at).toLocaleDateString("ru-RU")}
                            </small>
                          </td>
                          <td>
                            <span className={`admin-badge is-${s}`}>{SEGMENTS[s]}</span>
                          </td>
                          <td>{ago(lastActivity(r), now)}</td>
                          <td>
                            {r.active_days_period}
                            <small> всего {r.active_days}</small>
                          </td>
                          <td>
                            {r.projects}
                            <small> чатов {r.conversations}</small>
                          </td>
                          <td>
                            {r.runs}
                            {r.failed_runs ? <small> ошибок {r.failed_runs}</small> : null}
                          </td>
                          <td>{r.artifacts}</td>
                          <td>{r.approvals}</td>
                          <td>{usd(r.cost_usd)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {!visible.length ? <p className="empty">Нет пользователей под этот фильтр.</p> : null}
              </div>
            </section>
          </>
        ) : !error ? (
          <p className="muted">Загрузка статистики…</p>
        ) : null}
      </main>
    </div>
  );
}
