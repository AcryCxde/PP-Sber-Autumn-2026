import type { Session } from "./journey.ts";
export const OUTCOME_LABELS = {
  completed: "Завершено с результатом",
  incomplete: "Задача не завершена",
  abandoned: "Попытка прекращена",
};
export function outcomeLabel(session: Pick<Session, "open" | "outcome">) {
  return session.open
    ? "Замер идёт"
    : session.outcome
      ? OUTCOME_LABELS[session.outcome]
      : "Итог не указан";
}
export function missingSavedLabel(session: Session) {
  if (session.phase === "interface") return "Не применяется";
  if (session.phase === "baseline") return "Сохранение не отмечено";
  return session.events.some((e) => e.name === "artifact_saved")
    ? "Итоговый артефакт не выбран"
    : "Сохранение не подтверждено";
}
export function finishExplanation(session: Session) {
  const finished = [...session.events]
    .reverse()
    .find((e) => e.name === "session_finished");
  return finished?.status && Object.hasOwn(OUTCOME_LABELS, finished.status)
    ? `В журнале завершения записан итог «${OUTCOME_LABELS[finished.status as keyof typeof OUTCOME_LABELS]}». Он сохраняется при нажатии «Завершить замер» с выбранным итогом. Закрытие вкладки и недоступность CTF сами по себе его не устанавливают.`
    : "Событие завершения в журнале отсутствует. Итог показан из сохранённой записи; точную историю выбора подтвердить нельзя.";
}
