import { useCallback, useEffect, useRef, useState } from 'react';
import './App.css';

type Conversation = {
  id: string;
  title: string;
  status: string;
  context_state: 'new' | 'active' | 'reset' | 'unavailable';
  active_run_id: string | null;
  created_at: string;
  updated_at: string;
};

type StoredMessage = {
  id: string;
  conversation_id: string;
  run_id: string | null;
  role: string;
  kind: string;
  sequence: number;
  content: string;
  created_at: string;
};

type EventData = {
  type: string;
  conversation_id?: string;
  run_id?: string;
  seq?: number;
  client_request_id?: string;
  content?: unknown;
  optimistic?: boolean;
  [key: string]: unknown;
};

type PersistedEvent = {
  run_id: string;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type Artifact = {
  run_id: string;
  seq: number;
  path: string;
  num_lines: number | null;
  size_bytes: number | null;
  downloadable: boolean;
};

type ConnectionStatus = 'connecting' | 'connected' | 'disconnected';
type Theme = 'light' | 'dark';
type ContextState = Conversation['context_state'];
type PendingRequest = {
  content: string;
  conversation_id: string | null;
  context_mode: 'resume' | 'reset';
};

const SELECTED_CONVERSATION_KEY = 'claude-web.selected-conversation';
const THEME_KEY = 'claude-web.theme';
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled']);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function websocketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}

function formatConversationTime(value: string): string {
  return new Intl.DateTimeFormat('ru', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}

function initialTheme(): Theme {
  const documentTheme = document.documentElement.dataset.theme;
  if (documentTheme === 'light' || documentTheme === 'dark') {
    return documentTheme;
  }
  const saved = window.localStorage.getItem(THEME_KEY);
  if (saved === 'light' || saved === 'dark') {
    return saved;
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function storedMessageEvent(message: StoredMessage): EventData {
  if (message.kind === 'context_reset' || message.role === 'system') {
    return {
      type: 'context.reset',
      event_id: message.id,
      conversation_id: message.conversation_id,
      run_id: message.run_id ?? undefined,
      content: message.content,
    };
  }
  return {
    type: message.role === 'user' ? 'user_message' : 'message',
    event_id: message.id,
    conversation_id: message.conversation_id,
    run_id: message.run_id ?? undefined,
    content: message.content,
  };
}

function canonicalMessageEvent(value: unknown): EventData | null {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.content !== 'string') {
    return null;
  }
  const role = typeof value.role === 'string' ? value.role : 'user';
  const kind = typeof value.kind === 'string' ? value.kind : 'text';
  return {
    type: kind === 'context_reset' || role === 'system' ? 'context.reset' : role === 'user' ? 'user_message' : 'message',
    event_id: value.id,
    conversation_id: typeof value.conversation_id === 'string' ? value.conversation_id : undefined,
    run_id: typeof value.run_id === 'string' ? value.run_id : undefined,
    content: value.content,
  };
}

function agentNames(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => {
      if (typeof item === 'string') {
        return item;
      }
      if (isRecord(item) && typeof item.name === 'string') {
        return item.name;
      }
      return null;
    })
    .filter((item): item is string => Boolean(item));
}

function usedAgentNames(value: unknown): string[] {
  if (!isRecord(value) || !isRecord(value.by_type)) {
    return [];
  }
  return Object.entries(value.by_type)
    .filter(([, stats]) => {
      if (typeof stats === 'number') {
        return stats > 0;
      }
      if (!isRecord(stats)) {
        return false;
      }
      return Object.values(stats).some((count) => typeof count === 'number' && count > 0);
    })
    .map(([name]) => name);
}

function artifactFromEvent(event: EventData): Artifact | null {
  if (event.type !== 'tool_result' || !event.run_id || typeof event.seq !== 'number') {
    return null;
  }
  const file = isRecord(event.file) ? event.file : null;
  if (!file || typeof file.path !== 'string' || !file.path) {
    return null;
  }
  return {
    run_id: event.run_id,
    seq: event.seq,
    path: file.path,
    num_lines: typeof file.num_lines === 'number' ? file.num_lines : null,
    size_bytes: typeof file.content_size_bytes === 'number' ? file.content_size_bytes : null,
    downloadable:
      file.content_truncated === false &&
      typeof file.content_size_bytes === 'number',
  };
}

async function fetchRunEvents(runId: string): Promise<PersistedEvent[]> {
  const events: PersistedEvent[] = [];
  let afterSeq = 0;

  while (true) {
    const response = await fetch(`/api/runs/${runId}/events?after_seq=${afterSeq}&limit=500`);
    if (!response.ok) {
      return events;
    }
    const page = (await response.json()) as PersistedEvent[];
    events.push(...page);
    if (page.length < 500) {
      return events;
    }
    afterSeq = page[page.length - 1].seq;
  }
}

function App() {
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [prompt, setPrompt] = useState('');
  const [events, setEvents] = useState<EventData[]>([]);
  const [availableAgents, setAvailableAgents] = useState<string[]>([]);
  const [usedAgents, setUsedAgents] = useState<string[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [contextState, setContextState] = useState<ContextState>('new');
  const [resetContextOnNextRun, setResetContextOnNextRun] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('connecting');
  const [isRunning, setIsRunning] = useState(false);
  const [isReplaying, setIsReplaying] = useState(false);
  const [isHistoryLoading, setIsHistoryLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const activeConversationRef = useRef<string | null>(null);
  const activeRunRef = useRef<string | null>(null);
  const replayingRunRef = useRef<string | null>(null);
  const selectionVersionRef = useRef(0);
  const lastSeqByRunRef = useRef(new Map<string, number>());
  const pendingRequestsRef = useRef(new Map<string, PendingRequest>());

  const subscribeToRun = useCallback((runId: string) => {
    activeRunRef.current = runId;
    setIsRunning(true);
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      replayingRunRef.current = runId;
      setIsReplaying(true);
      ws.send(JSON.stringify({
        type: 'run.subscribe',
        run_id: runId,
        after_seq: lastSeqByRunRef.current.get(runId) ?? 0,
      }));
    }
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    window.localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  const refreshConversations = useCallback(async (): Promise<Conversation[]> => {
    const response = await fetch('/api/conversations?limit=100');
    if (!response.ok) {
      throw new Error('Не удалось загрузить список диалогов');
    }
    const items = (await response.json()) as Conversation[];
    setConversations(items);

    const activeId = activeConversationRef.current;
    const activeConversation = items.find((item) => item.id === activeId);
    if (activeConversation) {
      setContextState(activeConversation.context_state);
    }
    return items;
  }, []);

  const selectConversation = useCallback(async (conversationId: string) => {
    const selectionVersion = ++selectionVersionRef.current;
    setError(null);
    setIsHistoryLoading(true);

    try {
      const response = await fetch(`/api/conversations/${conversationId}/messages`);
      if (!response.ok) {
        throw new Error('Не удалось загрузить сообщения диалога');
      }
      const messages = (await response.json()) as StoredMessage[];
      const runIds = Array.from(
        new Set(
          messages
            .map((message) => message.run_id)
            .filter((runId): runId is string => Boolean(runId)),
        ),
      );
      const persistedEvents = (await Promise.all(runIds.map(fetchRunEvents))).flat();
      if (selectionVersion !== selectionVersionRef.current) {
        return;
      }

      const flattenedEvents: EventData[] = persistedEvents.map((event) => ({
        ...event.payload,
        type: event.type,
        run_id: event.run_id,
        seq: event.seq,
      }));
      const available = new Set<string>();
      const used = new Set<string>();
      const storedArtifacts = new Map<string, Artifact>();
      for (const event of flattenedEvents) {
        if (event.type === 'session') {
          for (const name of agentNames(event.agents)) {
            available.add(name);
          }
        }
        if (event.type === 'result') {
          for (const name of usedAgentNames(event.subagent_stats)) {
            used.add(name);
          }
        }
        const artifact = artifactFromEvent(event);
        if (artifact) {
          storedArtifacts.set(`${artifact.run_id}:${artifact.seq}`, artifact);
        }
      }

      activeConversationRef.current = conversationId;
      setActiveConversationId(conversationId);
      window.localStorage.setItem(SELECTED_CONVERSATION_KEY, conversationId);
      setResetContextOnNextRun(false);
      setAvailableAgents(Array.from(available).sort());
      setUsedAgents(Array.from(used).sort());
      setArtifacts(Array.from(storedArtifacts.values()));
      setEvents(messages.map(storedMessageEvent));
    } catch (loadError) {
      if (selectionVersion === selectionVersionRef.current) {
        setError(loadError instanceof Error ? loadError.message : 'Не удалось загрузить диалог');
      }
    } finally {
      if (selectionVersion === selectionVersionRef.current) {
        setIsHistoryLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    const initializeHistory = async () => {
      try {
        const items = await refreshConversations();
        if (cancelled || items.length === 0) {
          return;
        }
        const savedId = window.localStorage.getItem(SELECTED_CONVERSATION_KEY);
        const selected = items.find((item) => item.id === savedId) ?? items[0];
        setContextState(selected.context_state);
        await selectConversation(selected.id);
        if (selected.active_run_id) {
          subscribeToRun(selected.active_run_id);
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : 'Не удалось загрузить историю');
        }
      } finally {
        if (!cancelled) {
          setIsHistoryLoading(false);
        }
      }
    };

    void initializeHistory();
    return () => {
      cancelled = true;
    };
  }, [refreshConversations, selectConversation, subscribeToRun]);

  useEffect(() => {
    let disposed = false;
    let reconnectDelay = 500;

    const connect = () => {
      if (disposed) {
        return;
      }
      setConnectionStatus('connecting');
      const ws = new WebSocket(websocketUrl());
      wsRef.current = ws;

      ws.onopen = () => {
        if (disposed || wsRef.current !== ws) {
          ws.close();
          return;
        }
        reconnectDelay = 500;
        setConnectionStatus('connected');
        setError(null);

        const runId = activeRunRef.current;
        if (runId) {
          replayingRunRef.current = runId;
          setIsReplaying(true);
          ws.send(JSON.stringify({
            type: 'run.subscribe',
            run_id: runId,
            after_seq: lastSeqByRunRef.current.get(runId) ?? 0,
          }));
        } else {
          const pending = pendingRequestsRef.current.entries().next().value as
            | [string, PendingRequest]
            | undefined;
          if (pending) {
            const [clientRequestId, request] = pending;
            ws.send(JSON.stringify({
              type: 'run.create',
              conversation_id: request.conversation_id,
              client_request_id: clientRequestId,
              content: request.content,
              context_mode: request.context_mode,
            }));
          } else {
            replayingRunRef.current = null;
            setIsReplaying(false);
          }
        }
      };

      ws.onmessage = (message) => {
        if (wsRef.current !== ws) {
          return;
        }

        let event: EventData;
        try {
          event = JSON.parse(message.data) as EventData;
        } catch {
          setError('Backend вернул некорректное WebSocket-сообщение');
          return;
        }

        if (event.type === 'replay.complete') {
          if (
            event.run_id === replayingRunRef.current &&
            typeof event.status === 'string' &&
            TERMINAL_STATUSES.has(event.status)
          ) {
            replayingRunRef.current = null;
            if (event.run_id === activeRunRef.current) {
              activeRunRef.current = null;
              setIsRunning(false);
            }
            setIsReplaying(false);
          }
          return;
        }

        if (event.type === 'command.error') {
          const requestId = typeof event.client_request_id === 'string' ? event.client_request_id : null;
          if (requestId) {
            const pendingRequest = pendingRequestsRef.current.get(requestId);
            if (pendingRequest) {
              setPrompt(pendingRequest.content);
            }
            pendingRequestsRef.current.delete(requestId);
            setEvents((previous) => previous.filter((item) => item.client_request_id !== requestId));
          }
          if (event.code === 'conversation_context_unavailable') {
            setContextState('unavailable');
          }
          setIsRunning(false);
          setError(String(event.message ?? 'Команда не выполнена'));
          return;
        }

        if (event.run_id && typeof event.seq === 'number') {
          const lastSeq = lastSeqByRunRef.current.get(event.run_id) ?? 0;
          if (event.seq <= lastSeq) {
            return;
          }
          lastSeqByRunRef.current.set(event.run_id, event.seq);
        }

        if (event.type === 'session') {
          if (event.conversation_id !== activeConversationRef.current) {
            return;
          }
          const names = agentNames(event.agents);
          if (names.length > 0) {
            setAvailableAgents((previous) =>
              Array.from(new Set([...previous, ...names])).sort(),
            );
          }
          return;
        }

        if (event.type === 'context.reset') {
          const marker = canonicalMessageEvent(event.message);
          if (marker) {
            setEvents((previous) => {
              const markerId = marker.event_id;
              if (
                typeof markerId === 'string' &&
                previous.some((item) => item.event_id === markerId)
              ) {
                return previous;
              }
              return [...previous, marker];
            });
          }
          setContextState('reset');
          return;
        }

        if (event.type === 'run.accepted') {
          const requestId = typeof event.client_request_id === 'string' ? event.client_request_id : null;
          const canonical = canonicalMessageEvent(event.message);
          if (event.conversation_id && event.run_id) {
            selectionVersionRef.current += 1;
            activeConversationRef.current = event.conversation_id;
            activeRunRef.current = event.run_id;
            setActiveConversationId(event.conversation_id);
            window.localStorage.setItem(SELECTED_CONVERSATION_KEY, event.conversation_id);
          }
          if (requestId && canonical) {
            setEvents((previous) => {
              const optimisticIndex = previous.findIndex((item) => item.client_request_id === requestId);
              const canonicalId = canonical.event_id;
              const canonicalExists =
                typeof canonicalId === 'string' &&
                previous.some((item) => item.event_id === canonicalId);

              if (optimisticIndex === -1) {
                return canonicalExists ? previous : [...previous, canonical];
              }
              const next = [...previous];
              next[optimisticIndex] = canonical;
              return next.filter(
                (item, index) =>
                  index === optimisticIndex ||
                  typeof canonicalId !== 'string' ||
                  item.event_id !== canonicalId,
              );
            });
            pendingRequestsRef.current.delete(requestId);
          }
          setResetContextOnNextRun(false);
          setIsHistoryLoading(false);
          void refreshConversations().catch(() => undefined);

          if (event.idempotent_replay === true && event.run_id) {
            replayingRunRef.current = event.run_id;
            setIsReplaying(true);
            ws.send(JSON.stringify({
              type: 'run.subscribe',
              run_id: event.run_id,
              after_seq: lastSeqByRunRef.current.get(event.run_id) ?? event.seq ?? 0,
            }));
          }
          return;
        }

        if (event.type === 'result' && event.run_id === activeRunRef.current) {
          if (event.conversation_id === activeConversationRef.current) {
            const names = usedAgentNames(event.subagent_stats);
            if (names.length > 0) {
              setUsedAgents((previous) =>
                Array.from(new Set([...previous, ...names])).sort(),
              );
            }
          }
          activeRunRef.current = null;
          setIsRunning(false);
          if (typeof event.session_state === 'string') {
            setContextState(event.session_state as ContextState);
          }
          void refreshConversations().catch(() => undefined);
        }

        if (event.type === 'run.failed' && event.run_id === activeRunRef.current) {
          activeRunRef.current = null;
          setIsRunning(false);
          if (typeof event.session_state === 'string') {
            setContextState(event.session_state as ContextState);
          }
          setError(String(event.message ?? 'Запрос завершился с ошибкой'));
          void refreshConversations().catch(() => undefined);
        }

        if (event.type === 'thinking') {
          return;
        }
        if (event.conversation_id && event.conversation_id !== activeConversationRef.current) {
          return;
        }
        const artifact = artifactFromEvent(event);
        if (artifact) {
          setArtifacts((previous) => {
            const key = `${artifact.run_id}:${artifact.seq}`;
            return previous.some((item) => `${item.run_id}:${item.seq}` === key)
              ? previous
              : [...previous, artifact];
          });
        }
        setEvents((previous) => [...previous, event]);
      };

      ws.onerror = () => {
        if (wsRef.current === ws) {
          setError('Ошибка WebSocket-соединения');
        }
      };

      ws.onclose = () => {
        if (wsRef.current !== ws) {
          return;
        }
        wsRef.current = null;
        replayingRunRef.current = null;
        setIsReplaying(false);
        setConnectionStatus('disconnected');
        if (!disposed) {
          reconnectTimerRef.current = window.setTimeout(() => {
            reconnectDelay = Math.min(reconnectDelay * 2, 5000);
            connect();
          }, reconnectDelay);
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [refreshConversations]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [events]);

  const startNewConversation = () => {
    selectionVersionRef.current += 1;
    activeConversationRef.current = null;
    activeRunRef.current = null;
    window.localStorage.removeItem(SELECTED_CONVERSATION_KEY);
    setActiveConversationId(null);
    setContextState('new');
    setResetContextOnNextRun(false);
    setIsHistoryLoading(false);
    setAvailableAgents([]);
    setUsedAgents([]);
    setArtifacts([]);
    setEvents([]);
    setError(null);
  };

  const sendPrompt = () => {
    const ws = wsRef.current;
    const text = prompt.trim();
    if (!ws || ws.readyState !== WebSocket.OPEN || !text || isRunning || isReplaying) {
      return;
    }

    const clientRequestId = crypto.randomUUID();
    const conversationId = activeConversationRef.current;
    const contextMode = resetContextOnNextRun ? 'reset' : 'resume';
    pendingRequestsRef.current.set(clientRequestId, {
      content: text,
      conversation_id: conversationId,
      context_mode: contextMode,
    });
    setEvents((previous) => [
      ...previous,
      {
        type: 'user_message',
        content: text,
        client_request_id: clientRequestId,
        optimistic: true,
      },
    ]);
    setPrompt('');
    setError(null);
    setIsRunning(true);

    ws.send(JSON.stringify({
      type: 'run.create',
      conversation_id: conversationId,
      client_request_id: clientRequestId,
      content: text,
      context_mode: contextMode,
    }));
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendPrompt();
    }
  };

  const connectionLabel = {
    connecting: 'Подключение',
    connected: 'Подключено',
    disconnected: 'Нет соединения',
  }[connectionStatus];

  const contextLabel = {
    new: 'Новый контекст',
    active: 'Контекст активен',
    reset: 'Контекст сброшен',
    unavailable: 'Контекст недоступен',
  }[contextState];

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-header">
          <div>
            <div className="eyebrow">Claude Web</div>
            <h1>История</h1>
          </div>
          <button className="new-chat-button" type="button" onClick={startNewConversation} disabled={isRunning || isReplaying} title="Новый диалог">+</button>
        </div>

        <div className="conversation-list">
          {isHistoryLoading && <div className="sidebar-state">Загрузка истории…</div>}
          {!isHistoryLoading && conversations.length === 0 && <div className="sidebar-state">Диалогов пока нет</div>}
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              type="button"
              className={`conversation-item ${activeConversationId === conversation.id ? 'active' : ''}`}
              onClick={() => {
                setContextState(conversation.context_state);
                void selectConversation(conversation.id).then(() => {
                  if (conversation.active_run_id) {
                    subscribeToRun(conversation.active_run_id);
                  }
                });
              }}
              disabled={isReplaying || isRunning}
            >
              <span className="conversation-title">{conversation.title}</span>
              <span className="conversation-time">{formatConversationTime(conversation.updated_at)}</span>
            </button>
          ))}
        </div>
      </aside>

      <section className="app">
        <header className="header">
          <div className="header-statuses">
            <div className="header-title"><span className={`status-dot ${connectionStatus}`} /><span>{connectionLabel}</span></div>
            <div className={`context-status ${contextState}`}>{contextLabel}</div>
          </div>
          <div className="header-actions">
            {isReplaying ? <div className="run-status">Восстановление событий…</div> : isRunning && <div className="run-status">Claude выполняет запрос…</div>}
            <button
              className="theme-toggle"
              type="button"
              onClick={() => setTheme((current) => current === 'light' ? 'dark' : 'light')}
              aria-label={theme === 'light' ? 'Включить тёмную тему' : 'Включить светлую тему'}
              title={theme === 'light' ? 'Тёмная тема' : 'Светлая тема'}
            >
              {theme === 'light' ? '☾' : '☀'}
            </button>
          </div>
        </header>

        <main className="chat">
          <div className="chat-content">
            {events.length === 0 && !isHistoryLoading && (
              <div className="empty-state"><div className="empty-mark">C</div><h2>Новый диалог</h2><p>Напишите запрос, чтобы начать работу с Claude Code.</p></div>
            )}

            {events.length > 0 && (
              <div className="agent-roles-panel">
                <div className="panel-title">Роли сессии</div>
                <div className="role-groups">
                  <div className="role-group">
                    <span className="role-label">Основная</span>
                    <span className="role-badge primary">Claude Code</span>
                  </div>
                  {usedAgents.length > 0 && (
                    <div className="role-group">
                      <span className="role-label">Привлечены</span>
                      {usedAgents.map((agent) => <span key={agent} className="role-badge used">{agent}</span>)}
                    </div>
                  )}
                  {availableAgents.length > 0 && (
                    <details className="available-roles">
                      <summary>Доступные роли: {availableAgents.length}</summary>
                      <div className="role-badges">
                        {availableAgents.map((agent) => <span key={agent} className="role-badge">{agent}</span>)}
                      </div>
                    </details>
                  )}
                </div>
              </div>
            )}

            {artifacts.length > 0 && (
              <div className="artifacts-panel">
                <div className="panel-title">Файлы сессии</div>
                <div className="artifact-list">
                  {artifacts.map((artifact) => (
                    <div key={`${artifact.run_id}:${artifact.seq}`} className="artifact-item">
                      <div className="artifact-info">
                        <span className="artifact-icon">📄</span>
                        <span className="artifact-path">{artifact.path}</span>
                        {artifact.num_lines !== null && <span className="artifact-meta">{artifact.num_lines} строк</span>}
                        {artifact.size_bytes !== null && <span className="artifact-meta">{(artifact.size_bytes / 1024).toFixed(1)} КБ</span>}
                      </div>
                      {artifact.downloadable ? (
                        <a
                          className="artifact-download"
                          href={`/api/runs/${artifact.run_id}/events/${artifact.seq}/file`}
                          download
                        >
                          Скачать
                        </a>
                      ) : (
                        <span className="artifact-unavailable">Snapshot недоступен</span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {contextState === 'unavailable' && (
              <div className="context-warning">
                <div><strong>Контекст Claude недоступен.</strong><span> Продолжение требует явного сброса сессии.</span></div>
                <button type="button" onClick={() => setResetContextOnNextRun(true)} disabled={isRunning}>Сбросить при следующем запросе</button>
              </div>
            )}
            {resetContextOnNextRun && <div className="context-reset-pending">Следующий запрос начнёт новую Claude-сессию в этом чате.</div>}

            {events.map((event, index) => {
              const key = typeof event.event_id === 'string' ? event.event_id : `${event.run_id ?? event.client_request_id ?? 'local'}-${event.seq ?? index}-${event.type}`;

              if (event.type === 'context.reset') {
                return <div key={key} className="context-divider"><span>{String(event.content ?? 'Контекст Claude был сброшен')}</span></div>;
              }
              if (event.type === 'user_message') {
                return <div key={key} className={`message-row user ${event.optimistic ? 'optimistic' : ''}`}><div className="message user-message"><div className="message-label">Вы</div><div className="message-content">{String(event.content ?? '')}</div></div></div>;
              }
              if (event.type === 'message') {
                return <div key={key} className="message-row claude"><div className="message claude-message"><div className="message-label">Claude</div><div className="message-content">{String(event.content ?? '')}</div></div></div>;
              }
              if (event.type === 'tool') {
                const input = isRecord(event.input) ? event.input : {};
                const filePath = typeof input.file_path === 'string' ? input.file_path : null;
                return <div key={key} className="tool-row"><div className="tool-card"><div className="tool-header"><span className="tool-icon">⚙</span><strong>{String(event.name ?? 'Tool')}</strong><span className="tool-status">выполняется</span></div>{filePath ? <div className="tool-path">{filePath}</div> : <pre className="tool-input">{JSON.stringify(input, null, 2)}</pre>}</div></div>;
              }
              if (event.type === 'tool_result') {
                const file = isRecord(event.file) ? event.file : {};
                const filePath = typeof file.path === 'string' ? file.path : null;
                const lineCount = typeof file.num_lines === 'number' ? file.num_lines : null;
                return <div key={key} className="tool-row"><div className="tool-result"><div className="tool-result-header"><span>✓</span><strong>Результат инструмента</strong><span className="tool-status completed">готово</span></div>{filePath && <div className="file-info">📄 {filePath}</div>}{lineCount !== null && <div className="file-info">{lineCount} строк</div>}</div></div>;
              }
              if (event.type === 'result') {
                const turns = typeof event.num_turns === 'number' ? event.num_turns : 0;
                const cost = typeof event.cost_usd === 'number' ? event.cost_usd : 0;
                const duration = typeof event.duration_ms === 'number' ? event.duration_ms : 0;
                return <div key={key} className="result-row"><div className="result-card"><div className="result-header">Запрос завершён</div><div className="result-stats"><span>Шагов: {turns}</span><span>Стоимость: ${cost.toFixed(4)}</span><span>Время: {(duration / 1000).toFixed(1)} с</span></div></div></div>;
              }
              if (event.type === 'run.failed' || event.type === 'raw') {
                return <div key={key} className="warning-row"><div className="warning-card">⚠ {String(event.message ?? event.content ?? 'Ошибка')}</div></div>;
              }
              return null;
            })}

            {error && <div className="app-error">{error}</div>}
            <div ref={messagesEndRef} />
          </div>
        </main>

        <div className="input-area">
          <div className="input-wrapper">
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={isReplaying ? 'Восстанавливаем события…' : isRunning ? 'Дождитесь завершения запроса…' : 'Напишите сообщение…'}
              disabled={isRunning || isReplaying}
              rows={1}
            />
            <button type="button" onClick={sendPrompt} disabled={!prompt.trim() || isRunning || isReplaying || connectionStatus !== 'connected'} aria-label="Отправить запрос">↑</button>
          </div>
          <div className="input-hint">Enter — отправить · Shift + Enter — новая строка</div>
        </div>
      </section>
    </div>
  );
}

export default App;
