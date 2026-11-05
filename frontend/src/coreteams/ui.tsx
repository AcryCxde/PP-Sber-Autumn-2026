import { useEffect, useRef, type ReactNode } from 'react';
import type { Phase } from './api';
import { MEDIA, PHASE_LABELS, phaseColor } from './context';
import { useVideo } from './hooks';
import { parseMarkdown, type Inline } from './markdown';

const ICONS: Record<string, ReactNode> = {
  arrow: <path d="M4 12h15m-6-6 6 6-6 6" />,
  back: <path d="M20 12H5m6-6-6 6 6 6" />,
  file: (
    <>
      <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
      <path d="M14 3v6h6M8 13h8M8 17h6" />
    </>
  ),
  team: (
    <>
      <circle cx="9" cy="7" r="3" />
      <path d="M3 20v-3a6 6 0 0 1 12 0v3M17 4a3 3 0 0 1 0 6M21 20v-3a5 5 0 0 0-3-4" />
    </>
  ),
  check: <path d="m5 12 4 4L19 6" />,
  chat: (
    <>
      <path d="M21 11a8 8 0 0 1-8 8H8l-5 3 1-6a8 8 0 1 1 17-5Z" />
      <path d="M8 10h8M8 14h5" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  download: <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />,
  play: <path d="m9 5 11 7-11 7Z" />,
};

export function Icon({ name }: { name: keyof typeof ICONS }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {ICONS[name] ?? ICONS.file}
    </svg>
  );
}

export function Tag({ children, color = '' }: { children: ReactNode; color?: string }) {
  return (
    <span className={`tag ${color}`}>
      <i className="pill-dot" />
      {children}
    </span>
  );
}

export function PhaseTag({ phase }: { phase: Phase }) {
  return <Tag color={phaseColor(phase)}>{PHASE_LABELS[phase]}</Tag>;
}

export function Assistant({ children }: { children: ReactNode }) {
  return (
    <div className="assistant-message">
      <span className="assistant-avatar"><Icon name="chat" /></span>
      <div>
        <div className="speaker">Помощник <small>фасилитатор команды</small></div>
        <p>{children}</p>
      </div>
    </div>
  );
}

export function VideoDialog({ time = 0 }: { time?: number }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const requested = useRef(time);

  const jump = (seconds: number) => {
    const video = videoRef.current;
    if (!video) {
      return;
    }
    requested.current = seconds;
    if (video.readyState >= 1) {
      video.currentTime = seconds;
    }
    void video.play().catch(() => undefined);
  };

  return (
    <>
      <video
        ref={videoRef}
        controls
        playsInline
        preload="metadata"
        poster={MEDIA.team}
        src={MEDIA.video}
        aria-label="Ролик CoreTeams"
        onLoadedMetadata={(event) => {
          event.currentTarget.currentTime = requested.current;
        }}
      />
      <div className="chapter-list">
        <button type="button" onClick={() => jump(0)}>00:00 · Ваша задача</button>
        <button type="button" onClick={() => jump(36)}>00:36 · Помощник</button>
        <button type="button" onClick={() => jump(42)}>00:42 · Работа команды</button>
        <button type="button" onClick={() => jump(52)}>00:52 · Единый результат</button>
      </div>
      <p className="helper">Помощник организует работу, специалисты вносят свой вклад, вы принимаете результат.</p>
    </>
  );
}

export function GuideCard({ title, copy, art = 'guide' }: { title: string; copy: string; art?: 'guide' | 'result' }) {
  const video = useVideo();
  return (
    <aside className="guide-card">
      <img
        src={art === 'result' ? MEDIA.result : MEDIA.guide}
        alt={art === 'result' ? 'Команда передаёт единый результат' : 'Помощник с блокнотом из ролика CoreTeams'}
      />
      <div className="guide-copy">
        <span className="guide-role">Ваш помощник · фасилитатор</span>
        <h3>{title}</h3>
        <p>{copy}</p>
        <button className="text-button" type="button" onClick={() => video(36)}>Посмотреть мою роль в видео ↗</button>
        <div className="guide-note">Вы отвечаете за цель и решения.<br />Я — за вопросы и работу команды.</div>
      </div>
    </aside>
  );
}

export function Flow({
  children,
  aside,
  step,
  onBack,
}: {
  children: ReactNode;
  aside: ReactNode;
  step: number;
  onBack: () => void;
}) {
  return (
    <div className="flow-page">
      <div className="flow-top">
        <button className="back" type="button" onClick={onBack}><Icon name="back" />Назад</button>
        <div className="flow-progress">
          <span>Подготовка проекта</span>
          {[0, 1, 2, 3].map((index) => <i key={index} className={index <= step ? 'on' : ''} />)}
        </div>
      </div>
      <div className="flow-layout">
        <section className="flow-body">{children}</section>
        {aside}
      </div>
    </div>
  );
}

function InlineText({ parts }: { parts: Inline[] }) {
  return (
    <>
      {parts.map((part, index) => {
        if (part.kind === 'strong') return <strong key={index}>{part.text}</strong>;
        if (part.kind === 'em') return <em key={index}>{part.text}</em>;
        if (part.kind === 'code') return <code key={index}>{part.text}</code>;
        return <span key={index}>{part.text}</span>;
      })}
    </>
  );
}

export function Markdown({ source }: { source: string }) {
  return (
    <>
      {parseMarkdown(source).map((block, index) => {
        switch (block.kind) {
          case 'heading':
            return block.level <= 1
              ? <h3 key={index}><InlineText parts={block.inline} /></h3>
              : <h4 key={index}><InlineText parts={block.inline} /></h4>;
          case 'list':
            return block.ordered ? (
              <ol key={index}>{block.items.map((item, i) => <li key={i}><InlineText parts={item} /></li>)}</ol>
            ) : (
              <ul key={index}>{block.items.map((item, i) => <li key={i}><InlineText parts={item} /></li>)}</ul>
            );
          case 'quote':
            return <blockquote key={index}><InlineText parts={block.inline} /></blockquote>;
          case 'code':
            return <pre key={index}><code>{block.text}</code></pre>;
          case 'table':
            return (
              <div key={index} className="ct-table-wrap">
                <table>
                  <thead>
                    <tr>{block.header.map((cell, i) => (
                      <th key={i} style={{ textAlign: block.align[i] ?? undefined }}><InlineText parts={cell} /></th>
                    ))}</tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, r) => (
                      <tr key={r}>{row.map((cell, i) => (
                        <td key={i} style={{ textAlign: block.align[i] ?? undefined }}><InlineText parts={cell} /></td>
                      ))}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          case 'rule':
            return <hr key={index} />;
          default:
            return <p key={index}><InlineText parts={block.inline} /></p>;
        }
      })}
    </>
  );
}

export function ResultPaper({ version, accepted, content }: { version: number; accepted: boolean; content: string }) {
  return (
    <div className="paper">
      <div className="paper-top">
        <span>РЕЗУЛЬТАТ КОМАНДЫ · ВЕРСИЯ {version}</span>
        <span>{accepted ? 'Одобрен' : 'Для вашего решения'}</span>
      </div>
      <div className="paper-body finding"><Markdown source={content} /></div>
    </div>
  );
}

export function Modal({
  title,
  content,
  onClose,
}: {
  title: string | null;
  content: ReactNode;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement | null>(null);
  const opener = useRef<Element | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) {
      return;
    }
    if (title !== null && !dialog.open) {
      opener.current = document.activeElement;
      dialog.showModal();
    }
    if (title === null && dialog.open) {
      dialog.querySelector('video')?.pause();
      dialog.close();
      if (opener.current instanceof HTMLElement && opener.current.isConnected) {
        opener.current.focus();
      }
    }
  }, [title]);

  return (
    <dialog
      ref={ref}
      id="dialog"
      aria-labelledby="dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        if (
          event.target === event.currentTarget &&
          (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)
        ) {
          onClose();
        }
      }}
    >
      {title !== null && (
        <div id="dialog-body">
          <header className="dialog-head">
            <h2 id="dialog-title">{title}</h2>
            <button className="close-button" type="button" onClick={onClose} aria-label="Закрыть окно">×</button>
          </header>
          <div className="dialog-content">{content}</div>
        </div>
      )}
    </dialog>
  );
}
