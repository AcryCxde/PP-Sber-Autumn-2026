import { useEffect, useState } from "react";

/** Projects made in the previous (v9) interface: kept on the server, shown read-only. */
type Summary = { id: string; name: string; scenario: string; status: string; stage_title: string; updated_at: string };
type Version = { version: number; content: string; accepted: boolean; created_at: string };
type Detail = Summary & {
  problem: string;
  context: string;
  results: Version[][];
  files: { id: string; name: string; size_bytes: number }[];
};
type Catalog = { scenarios: { key: string; label: string; stages: { title: string }[] }[] };

const STATUS: Record<string, string> = {
  draft: "Подготовка",
  active: "В работе",
  ready: "Все результаты приняты",
  done: "Проект завершён",
};

async function load<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (!response.ok) throw new Error(`Сервис вернул ошибку ${response.status}`);
  return (await response.json()) as T;
}

function markdown(project: Detail, stages: string[]): string {
  const parts = [`# ${project.name || "Проект"}`, project.problem, project.context];
  project.results.forEach((versions, index) => {
    const last = versions.at(-1);
    if (last)
      parts.push(`<!-- ${stages[index] ?? `Этап ${index + 1}`} — версия ${last.version}${last.accepted ? ", одобрено" : ""} -->\n\n${last.content.trim()}`);
  });
  return parts.filter((part) => part.trim()).join("\n\n");
}

function download(name: string, content: string) {
  const url = URL.createObjectURL(new Blob(["﻿" + content], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `CoreTeams-${name.replace(/[<>:"/\\|?*]/g, "").slice(0, 60) || "проект"}.md`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function LegacyProjects() {
  const [projects, setProjects] = useState<Summary[]>([]);
  const [catalog, setCatalog] = useState<Catalog>();
  const [open, setOpen] = useState<Detail>();
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([load<Summary[]>("/api/projects"), load<Catalog>("/api/scenarios")])
      .then(([list, scenarios]) => {
        setProjects(list);
        setCatalog(scenarios);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (!projects.length && !error) return null;
  const stages = (scenario: string) =>
    catalog?.scenarios.find((s) => s.key === scenario)?.stages.map((s) => s.title) ?? [];

  return (
    <section className="projects-section legacy-projects">
      <div className="section-heading">
        <h2>Проекты из прежнего интерфейса</h2>
      </div>
      <p className="muted">
        Сохранены на сервере без изменений. Здесь их можно просмотреть и скачать; продолжить работу можно в новом проекте.
      </p>
      {error ? <p className="error">Не удалось загрузить прежние проекты: {error}</p> : null}
      <div className="project-cards">
        {projects.map((x) => (
          <button
            key={x.id}
            className="project-card"
            aria-expanded={open?.id === x.id}
            onClick={() =>
              open?.id === x.id
                ? setOpen(undefined)
                : load<Detail>(`/api/projects/${encodeURIComponent(x.id)}`)
                    .then(setOpen)
                    .catch((e) => setError(e instanceof Error ? e.message : String(e)))
            }
          >
            <span className="eyebrow">{STATUS[x.status] ?? x.status}</span>
            <strong>{x.name || "Без названия"}</strong>
            <small>
              {x.stage_title} · {new Date(x.updated_at).toLocaleDateString("ru")}
            </small>
          </button>
        ))}
      </div>
      {open ? (
        <article className="legacy-detail">
          <div className="section-heading">
            <h3>{open.name || "Без названия"}</h3>
            <button className="secondary" onClick={() => download(open.name, markdown(open, stages(open.scenario)))}>
              Скачать Markdown
            </button>
          </div>
          {open.problem ? <p>{open.problem}</p> : null}
          {open.context ? <p className="muted">{open.context}</p> : null}
          <p className="muted">
            Материалы: {open.files.map((f) => f.name).join(", ") || "не добавлены"}
          </p>
          {open.results.map((versions, index) =>
            versions.length ? (
              <details key={index} open={index === 0}>
                <summary>
                  {stages(open.scenario)[index] ?? `Этап ${index + 1}`} · версий: {versions.length}
                  {versions.some((v) => v.accepted) ? " · одобрено" : ""}
                </summary>
                {[...versions].reverse().map((v) => (
                  <div key={v.version} className="legacy-version">
                    <p className="eyebrow">
                      Версия {v.version}
                      {v.accepted ? " · одобрена" : ""} · {new Date(v.created_at).toLocaleString("ru")}
                    </p>
                    <div className="document legacy-text">{v.content}</div>
                  </div>
                ))}
              </details>
            ) : null,
          )}
        </article>
      ) : null}
    </section>
  );
}
