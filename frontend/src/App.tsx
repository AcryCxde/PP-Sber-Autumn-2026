import { useCallback, useEffect, useRef, useState } from 'react';
import {
  artifactFromEvent,
  canonicalMessage,
  decodePersistedEvent,
  decodeSocketMessage,
  sequenceDecision,
  type Artifact,
  type ContextState,
  type EventData,
  type PersistedEvent,
  type StoredMessage,
} from './events';
import {
  apiFetch,
  notifyUnauthorized,
  UnauthorizedError,
  WS_UNAUTHORIZED,
  type User,
} from './auth';
import {
  CHAT_FILE_ACCEPT,
  chatFileUrl,
  createConversation,
  formatFileSize,
  listChatFiles,
  removeChatFile,
  uploadChatFile,
  type ChatFile,
} from './chatFiles';
import { themeToggleLabel, useTheme } from './theme';
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

type ConnectionStatus = 'connecting' | 'connected' | 'disconnected';
type PendingRequest = {
  content: string;
  conversation_id: string | null;
  context_mode: 'resume' | 'reset';
};

const SELECTED_CONVERSATION_KEY = 'claude-web.selected-conversation';
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
    type: message.role === 'user' ? 'user_message' : 'message.created',
    event_id: message.id,
    conversation_id: message.conversation_id,
    run_id: message.run_id ?? undefined,
    content: message.content,
  };
}

function canonicalMessageEvent(value: unknown): EventData | null {
  const message = canonicalMessage(value);
  if (!message) {
    return null;
  }
  return {
    type:
      message.kind === 'context_reset' || message.role === 'system'
        ? 'context.reset'
        : message.role === 'user'
          ? 'user_message'
          : 'message.created',
    event_id: message.id,
    conversation_id: message.conversation_id,
    run_id: message.run_id,
    content: message.content,
  };
}

async function fetchConversationEvents(
  conversationId: string,
): Promise<PersistedEvent[]> {
  const events: PersistedEvent[] = [];
  let offset = 0;

  while (true) {
    const response = await apiFetch(
      `/api/conversations/${conversationId}/events?offset=${offset}&limit=500`,
    );
    if (!response.ok) {
      throw new Error('Не удалось загрузить метаданные событий диалога');
    }
    const page = (await response.json()) as PersistedEvent[];
    events.push(...page);
    if (page.length < 500) {
      return events;
    }
    offset += page.length;
  }
}

type AppProps = {
  user: User;
  onLogout: () => void;
};

function App({ user, onLogout }: AppProps) {
  const [theme, toggleTheme] = useTheme();
  const [prompt, setPrompt] = useState('');
  const [events, setEvents] = useState<EventData[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [files, setFiles] = useState<ChatFile[]>([]);
  const [isUploading, setIsUploading] = useState(false);
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
  const resubscribeTimerRef = useRef<number | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const activeConversationRef = useRef<string | null>(null);
  const activeRunRef = useRef<string | null>(null);
  const replayingRunRef = useRef<string | null>(null);
  const selectionVersionRef = useRef(0);
  const lastSeqByRunRef = useRef(new Map<string, number>());
  const pendingRequestsRef = useRef(new Map<string, PendingRequest>());

  const subscribeToRun = useCallback((runId: string, delayMs = 0) => {
    activeRunRef.current = runId;
    setIsRunning(true);
    if (resubscribeTimerRef.current !== null) {
      window.clearTimeout(resubscribeTimerRef.current);
      resubscribeTimerRef.current = null;
    }

    const sendSubscription = () => {
      const ws = wsRef.current;
      if (ws?.readyState !== WebSocket.OPEN || activeRunRef.current !== runId) {
        return;
      }
      replayingRunRef.current = runId;
      setIsReplaying(true);
      ws.send(JSON.stringify({
        type: 'run.subscribe',
        run_id: runId,
        after_seq: lastSeqByRunRef.current.get(runId) ?? 0,
      }));
    };

    if (delayMs > 0) {
      resubscribeTimerRef.current = window.setTimeout(sendSubscription, delayMs);
    } else {
      sendSubscription();
    }
  }, []);

  const refreshConversations = useCallback(async (): Promise<Conversation[]> => {
    // Диалоги проектов живут в «Моих проектах»; здесь — только свободные.
    const response = await apiFetch('/api/conversations?limit=100&standalone=true');
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

  const selectConversation = useCallback(async (
    conversationId: string,
    activeRunId: string | null = null,
  ) => {
    const selectionVersion = ++selectionVersionRef.current;
    setError(null);
    setIsHistoryLoading(true);

    try {
      const metadataPromise = fetchConversationEvents(conversationId)
        .then((events) => ({ events, unavailable: false }))
        .catch((metadataError: unknown) => {
          if (metadataError instanceof UnauthorizedError) {
            throw metadataError;
          }
          return { events: [] as PersistedEvent[], unavailable: true };
        });
      const filesPromise = listChatFiles(conversationId).catch((filesError: unknown) => {
        if (filesError instanceof UnauthorizedError) {
          throw filesError;
        }
        return [] as ChatFile[];
      });
      const response = await apiFetch(`/api/conversations/${conversationId}/messages`);
      if (!response.ok) {
        throw new Error('Не удалось загрузить сообщения диалога');
      }
      const messages = (await response.json()) as StoredMessage[];
      const conversationFiles = await filesPromise;
      if (selectionVersion !== selectionVersionRef.current) {
        return false;
      }
      activeConversationRef.current = conversationId;
      setActiveConversationId(conversationId);
      window.localStorage.setItem(SELECTED_CONVERSATION_KEY, conversationId);
      setResetContextOnNextRun(false);
      lastSeqByRunRef.current.clear();
      setArtifacts([]);
      setFiles(conversationFiles);
      setEvents(messages.map(storedMessageEvent));

      const metadata = await metadataPromise;
      const persistedEvents = metadata.events;
      const metadataUnavailable = metadata.unavailable;
      if (selectionVersion !== selectionVersionRef.current) {
        return false;
      }

      const decodedEvents = persistedEvents
        .map(decodePersistedEvent)
        .filter((event): event is EventData => event !== null);
      const eventsByRun = new Map<string, EventData[]>();
      for (const event of decodedEvents) {
        if (!event.run_id || typeof event.seq !== 'number') {
          continue;
        }
        const runEvents = eventsByRun.get(event.run_id) ?? [];
        runEvents.push(event);
        eventsByRun.set(event.run_id, runEvents);
      }

      const terminalEventsByRun = new Map<string, EventData>();
      for (const event of decodedEvents) {
        if (
          event.run_id &&
          (event.type === 'run.completed' ||
            event.type === 'run.failed' ||
            event.type === 'run.outcome_unknown')
        ) {
          terminalEventsByRun.set(event.run_id, event);
        }
      }
      const terminalRunIds = new Set(terminalEventsByRun.keys());
      const storedArtifacts = new Map<string, Artifact>();
      const activeRunEvents: EventData[] = [];
      for (const [runId, runEvents] of eventsByRun) {
        let cursor = 0;
        for (const event of runEvents.sort((left, right) => (left.seq ?? 0) - (right.seq ?? 0))) {
          const decision = sequenceDecision(cursor, event.seq ?? 0);
          if (decision.kind === 'gap') {
            break;
          }
          if (decision.kind === 'duplicate') {
            continue;
          }
          cursor = decision.nextSeq;
          if (
            runId === activeRunId &&
            !terminalRunIds.has(runId) &&
            event.type === 'message.created'
          ) {
            activeRunEvents.push(event);
          }
          const artifact = artifactFromEvent(event);
          if (artifact) {
            storedArtifacts.set(`${artifact.run_id}:${artifact.seq}`, artifact);
          }
        }
        lastSeqByRunRef.current.set(runId, cursor);
      }

      const restoredEvents: EventData[] = [];
      const restoredTerminalRuns = new Set<string>();
      messages.forEach((message, index) => {
        restoredEvents.push(storedMessageEvent(message));
        const runId = message.run_id;
        const nextRunId = messages[index + 1]?.run_id ?? null;
        if (runId && runId !== nextRunId) {
          const terminalEvent = terminalEventsByRun.get(runId);
          if (terminalEvent) {
            restoredEvents.push(terminalEvent);
            restoredTerminalRuns.add(runId);
          }
        }
      });
      for (const [runId, terminalEvent] of terminalEventsByRun) {
        if (!restoredTerminalRuns.has(runId)) {
          restoredEvents.push(terminalEvent);
        }
      }

      setArtifacts(Array.from(storedArtifacts.values()));
      setEvents([...restoredEvents, ...activeRunEvents]);
      if (metadataUnavailable) {
        setError(
          'Сообщения загружены, но метаданные запусков временно недоступны.',
        );
      }
      return activeRunId === null || !terminalRunIds.has(activeRunId);
    } catch (loadError) {
      if (loadError instanceof UnauthorizedError) {
        return false;
      }
      if (selectionVersion === selectionVersionRef.current) {
        setError(loadError instanceof Error ? loadError.message : 'Не удалось загрузить диалог');
      }
      return false;
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
        const shouldSubscribe = await selectConversation(
          selected.id,
          selected.active_run_id,
        );
        if (shouldSubscribe && selected.active_run_id) {
          subscribeToRun(selected.active_run_id);
        }
      } catch (loadError) {
        if (!cancelled && !(loadError instanceof UnauthorizedError)) {
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
          subscribeToRun(runId);
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

        let parsed: unknown;
        try {
          parsed = JSON.parse(message.data) as unknown;
        } catch {
          setError('Backend вернул некорректное WebSocket-сообщение');
          return;
        }
        const event = decodeSocketMessage(parsed);
        if (!event) {
          setError('Backend вернул неподдерживаемое WebSocket-сообщение');
          return;
        }

        if (event.type === 'replay.complete' && event.run_id) {
          const cursor = lastSeqByRunRef.current.get(event.run_id) ?? 0;
          if ((event.last_seq ?? cursor) > cursor) {
            subscribeToRun(event.run_id);
          } else if (event.terminal !== true) {
            subscribeToRun(event.run_id, 200);
          } else {
            replayingRunRef.current = null;
            setIsReplaying(false);
          }
          return;
        }

        if (event.type === 'command.error') {
          const requestId = event.client_request_id ?? null;
          if (requestId) {
            const pendingRequest = pendingRequestsRef.current.get(requestId);
            if (pendingRequest) {
              setPrompt(pendingRequest.content);
            }
            pendingRequestsRef.current.delete(requestId);
            setEvents((previous) => previous.filter((item) => item.client_request_id !== requestId));
          }
          if (event.error_code === 'conversation_context_unavailable') {
            setContextState('unavailable');
          }
          if (activeRunRef.current === null && pendingRequestsRef.current.size === 0) {
            setIsRunning(false);
          }
          setError(event.message ?? 'Команда не выполнена');
          return;
        }

        if (!event.run_id || typeof event.seq !== 'number') {
          return;
        }
        const lastSeq = lastSeqByRunRef.current.get(event.run_id) ?? 0;
        const sequence = sequenceDecision(lastSeq, event.seq);
        if (sequence.kind === 'duplicate') {
          return;
        }
        if (sequence.kind === 'gap') {
          subscribeToRun(event.run_id);
          return;
        }
        lastSeqByRunRef.current.set(event.run_id, sequence.nextSeq);

        if (event.type === 'ignored') {
          return;
        }

        if (event.type === 'context.reset') {
          const marker = canonicalMessageEvent(event.canonical_message);
          if (marker) {
            setEvents((previous) => {
              const markerId = marker.event_id;
              return markerId && previous.some((item) => item.event_id === markerId)
                ? previous
                : [...previous, marker];
            });
          }
          setContextState('reset');
          return;
        }

        if (event.type === 'run.accepted') {
          const requestId = event.client_request_id ?? null;
          const canonical = canonicalMessageEvent(event.canonical_message);
          if (event.conversation_id) {
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
              const canonicalExists = canonicalId
                ? previous.some((item) => item.event_id === canonicalId)
                : false;
              if (optimisticIndex === -1) {
                return canonicalExists ? previous : [...previous, canonical];
              }
              const next = [...previous];
              next[optimisticIndex] = canonical;
              return next.filter(
                (item, index) => index === optimisticIndex || !canonicalId || item.event_id !== canonicalId,
              );
            });
            pendingRequestsRef.current.delete(requestId);
          }
          setResetContextOnNextRun(false);
          setIsHistoryLoading(false);
          void refreshConversations().catch(() => undefined);
          if (event.idempotent_replay === true) {
            subscribeToRun(event.run_id);
          }
          return;
        }

        if (event.conversation_id && event.conversation_id !== activeConversationRef.current) {
          return;
        }

        if (event.type === 'message.created') {
          setEvents((previous) => [...previous, event]);
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
          return;
        }

        if (
          event.type === 'run.completed' ||
          event.type === 'run.failed' ||
          event.type === 'run.outcome_unknown'
        ) {
          if (event.run_id === activeRunRef.current) {
            activeRunRef.current = null;
          }
          if (resubscribeTimerRef.current !== null) {
            window.clearTimeout(resubscribeTimerRef.current);
            resubscribeTimerRef.current = null;
          }
          setIsRunning(false);
          setIsReplaying(false);
          replayingRunRef.current = null;
          if (event.session_state) {
            setContextState(event.session_state);
          }
          if (event.type === 'run.outcome_unknown') {
            setContextState('unavailable');
          }
          setEvents((previous) => [...previous, event]);
          void refreshConversations().catch(() => undefined);
        }
      };

      ws.onerror = () => {
        if (wsRef.current === ws) {
          setError('Ошибка WebSocket-соединения');
        }
      };

      ws.onclose = (closeEvent) => {
        if (wsRef.current !== ws) {
          return;
        }
        wsRef.current = null;
        if (closeEvent.code === WS_UNAUTHORIZED) {
          // Сессии нет: переподключение без входа бесполезно.
          disposed = true;
          setConnectionStatus('disconnected');
          notifyUnauthorized();
          return;
        }
        if (resubscribeTimerRef.current !== null) {
          window.clearTimeout(resubscribeTimerRef.current);
          resubscribeTimerRef.current = null;
        }
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
      if (resubscribeTimerRef.current !== null) {
        window.clearTimeout(resubscribeTimerRef.current);
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [refreshConversations, subscribeToRun]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [events]);

  const startNewConversation = () => {
    selectionVersionRef.current += 1;
    activeConversationRef.current = null;
    activeRunRef.current = null;
    lastSeqByRunRef.current.clear();
    window.localStorage.removeItem(SELECTED_CONVERSATION_KEY);
    setActiveConversationId(null);
    setContextState('new');
    setResetContextOnNextRun(false);
    setIsHistoryLoading(false);
    if (resubscribeTimerRef.current !== null) {
      window.clearTimeout(resubscribeTimerRef.current);
      resubscribeTimerRef.current = null;
    }
    setArtifacts([]);
    setFiles([]);
    setEvents([]);
    setError(null);
  };

  const attachFiles = async (list: FileList | null) => {
    if (!list?.length) {
      return;
    }
    const selectionVersion = selectionVersionRef.current;
    setIsUploading(true);
    setError(null);
    try {
      let conversationId = activeConversationRef.current;
      if (!conversationId) {
        // Документы прикладывают к диалогу, поэтому до первого сообщения создаём пустой.
        conversationId = await createConversation();
        if (selectionVersion !== selectionVersionRef.current) {
          return;
        }
        activeConversationRef.current = conversationId;
        setActiveConversationId(conversationId);
        window.localStorage.setItem(SELECTED_CONVERSATION_KEY, conversationId);
        void refreshConversations().catch(() => undefined);
      }
      for (const file of Array.from(list)) {
        try {
          const next = await uploadChatFile(conversationId, file);
          if (activeConversationRef.current === conversationId) {
            setFiles(next);
          }
        } catch (uploadError) {
          if (uploadError instanceof UnauthorizedError) {
            return;
          }
          setError(`${file.name}: ${uploadError instanceof Error ? uploadError.message : 'файл не загружен'}`);
        }
      }
    } catch (createError) {
      if (!(createError instanceof UnauthorizedError)) {
        setError(createError instanceof Error ? createError.message : 'Не удалось приложить файлы');
      }
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  };

  const detachFile = async (file: ChatFile) => {
    const conversationId = activeConversationRef.current;
    if (!conversationId) {
      return;
    }
    try {
      const next = await removeChatFile(conversationId, file.id);
      if (activeConversationRef.current === conversationId) {
        setFiles(next);
      }
    } catch (removeError) {
      if (!(removeError instanceof UnauthorizedError)) {
        setError(removeError instanceof Error ? removeError.message : `Не удалось убрать ${file.name}`);
      }
    }
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
            <a className="eyebrow back-to-projects" href="#home">← CoreTeams</a>
            <h1>Свободный диалог</h1>
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
                void selectConversation(
                  conversation.id,
                  conversation.active_run_id,
                ).then((shouldSubscribe) => {
                  if (shouldSubscribe && conversation.active_run_id) {
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

        <div className="sidebar-account">
          <span className="account-email" title={user.email}>{user.email}</span>
          <button className="logout-button" type="button" onClick={onLogout}>Выйти</button>
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
              onClick={toggleTheme}
              aria-label={themeToggleLabel(theme)}
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
                </div>
              </div>
            )}

            {artifacts.length > 0 && (
              <div className="artifacts-panel">
                <div className="panel-title">Файлы, созданные Claude</div>
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
              if (event.type === 'message.created') {
                return <div key={key} className="message-row claude"><div className="message claude-message"><div className="message-label">Claude</div><div className="message-content">{event.content ?? ''}</div></div></div>;
              }
              if (event.type === 'run.completed') {
                return <div key={key} className="result-row"><div className="result-card"><div className="result-header">Запрос завершён</div></div></div>;
              }
              if (event.type === 'run.failed') {
                return <div key={key} className="warning-row"><div className="warning-card">⚠ {event.message ?? 'Запрос завершился с ошибкой'}</div></div>;
              }
              if (event.type === 'run.outcome_unknown') {
                return <div key={key} className="warning-row"><div className="warning-card outcome-unknown">⚠ Результат запуска неизвестен. Перед продолжением сбросьте контекст.</div></div>;
              }
              return null;
            })}

            {error && <div className="app-error">{error}</div>}
            <div ref={messagesEndRef} />
          </div>
        </main>

        <div className="input-area">
          {files.length > 0 && activeConversationId && (
            <ul className="attachment-list" aria-label="Приложенные документы">
              {files.map((file) => (
                <li key={file.id} className="attachment-chip">
                  <a href={chatFileUrl(activeConversationId, file.id)} download title="Скачать исходный файл">
                    📎 {file.name}
                  </a>
                  <span className="attachment-size">{formatFileSize(file.size_bytes)}</span>
                  <button type="button" onClick={() => void detachFile(file)} aria-label={`Убрать ${file.name}`}>×</button>
                </li>
              ))}
            </ul>
          )}
          <div className="input-wrapper">
            <label
              className={`attach-button ${isUploading || isReplaying ? 'disabled' : ''}`}
              title="Приложить документы"
            >
              {isUploading ? '…' : '📎'}
              <input
                ref={fileInputRef}
                className="file-input"
                aria-label="Приложить документы"
                type="file"
                multiple
                accept={CHAT_FILE_ACCEPT}
                disabled={isUploading || isReplaying}
                onChange={(event) => void attachFiles(event.target.files)}
              />
            </label>
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
          <div className="input-hint">Enter — отправить · Shift + Enter — новая строка · 📎 — документы до 5 МБ, Claude прочитает их из папки materials/</div>
        </div>
      </section>
    </div>
  );
}

export default App;
