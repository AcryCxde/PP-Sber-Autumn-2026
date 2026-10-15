import type { Project } from "./types.ts";
import { parseRun, parseSave } from "./api.ts";
export const STORAGE_KEY = "coreteams-workspace-v1";
export function loadProjects(storage: Pick<Storage, "getItem">): Project[] {
  const raw = storage.getItem(STORAGE_KEY);
  if (!raw) return [];
  const v: unknown = JSON.parse(raw);
  if (
    !Array.isArray(v) ||
    !v.every(
      (p) =>
        p &&
        typeof p.id === "string" &&
        typeof p.title === "string" &&
        typeof p.context === "string" &&
        typeof p.updatedAt === "string" &&
        (!p.serverId || typeof p.serverId === "string") &&
        (!p.pending ||
          (["create", "start", "answer", "save"].includes(p.pending.action) &&
            typeof p.pending.requestId === "string" &&
            p.pending.body &&
            typeof p.pending.body === "object")),
    )
  )
    throw new Error("Повреждённое хранилище");
  return v.map((p) => ({
    ...p,
    run: p.run ? parseRun(p.run) : undefined,
    saved: p.saved ? parseSave(p.saved) : undefined,
  }));
}
