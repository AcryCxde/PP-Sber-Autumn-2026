import { buildAnswers, type AnswerValue, type QuestionGroup } from "./domain.ts";

export function FacilitatorQuestions({ group, draft, disabled, onChange, onSubmit }: {
  group: QuestionGroup;
  draft: Record<string, AnswerValue>;
  disabled: boolean;
  onChange: (draft: Record<string, AnswerValue>) => void;
  onSubmit: (answers: Record<string, string>) => void;
}) {
  let answers: Record<string, string> | undefined;
  try { answers = buildAnswers(group, draft); } catch { /* Incomplete draft is not an error. */ }
  return <form className="question-box" onSubmit={e => { e.preventDefault(); if (answers && !disabled) onSubmit(answers); }}>
    <span className="eyebrow">НУЖНО ВАШЕ РЕШЕНИЕ</span>
    <h3>Вопросы фасилитатора</h3>
    {group.questions.map((q, i) => {
      const value = draft[q.text] || { selected: [], custom: "" };
      const change = (v: AnswerValue) => onChange({ ...draft, [q.text]: v });
      return <fieldset key={q.text} disabled={disabled} className="facilitator-question">
        <legend>{q.text}</legend>
        {q.header ? <p>{q.header}</p> : null}
        {q.multi_select ? <small>Можно выбрать несколько вариантов.</small> : null}
        {q.options.map(option => <label className="question-option" key={option.label}>
          <input type={q.multi_select ? "checkbox" : "radio"} name={`question-${group.id}-${i}`}
            checked={value.selected.includes(option.label)} onChange={e => change({
              selected: q.multi_select ? (e.target.checked ? [...value.selected, option.label] : value.selected.filter(v => v !== option.label)) : [option.label],
              custom: q.multi_select ? value.custom : "",
            })} />
          <span>{option.label}{option.description ? <small>{option.description}</small> : null}</span>
        </label>)}
        {q.allow_custom ? <label>
          {group.questions.length === 1 && !q.options.length ? "Ответ фасилитатору" : `Свой ответ — вопрос ${i + 1}`}
          <textarea rows={3} autoComplete="off" aria-label={group.questions.length === 1 && !q.options.length ? "Ответ фасилитатору" : `Свой ответ — вопрос ${i + 1}`} value={value.custom} onChange={e => change({
            selected: q.multi_select || !e.target.value.trim() ? value.selected : [], custom: e.target.value,
          })} />
        </label> : null}
      </fieldset>;
    })}
    <p className="muted">Ответьте на каждый вопрос. Ответ продолжит этот же запуск. При замене вопроса прежние ответы не отправятся автоматически.</p>
    <button className="primary" disabled={disabled || !answers}>Ответить и продолжить</button>
  </form>;
}
