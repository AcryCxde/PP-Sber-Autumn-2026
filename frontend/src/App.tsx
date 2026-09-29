import { useCallback, useEffect, useRef, useState } from 'react';
import './App.css';

type Conversation = {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
};

type StoredMessage = {
  id: string;
  conversation_id: string;
  run_id: string | null;
  role: string;
  sequence: number;
  content: string;
  created_at: string;
};

type EventData = {
  type: string;
  conversation_id?: string;
  run_id?: string;
  seq?: number;
  content?: unknown;
  [key: string]: unknown;
};

type ConnectionStatus = 'connecting' | 'connected' | 'disconnected';

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

function App() {
  const [prompt, setPrompt] = useState('');
  const [events, setEvents] = useState<EventData[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
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

  const refreshConversations = useCallback(async (): Promise<Conversation[]> => {
    const response = await fetch('/api/conversations?limit=100');
    if (!response.ok) {
      throw new Error('Не удалось загрузить список диалогов');
    }

    const items = (await response.json()) as Conversation[];
    setConversations(items);
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
      if (selectionVersion !== selectionVersionRef.current) {
        return;
      }

      activeConversationRef.current = conversationId;
      setActiveConversationId(conversationId);
      setEvents(
        messages.map((message) => ({
          type: message.role === 'user' ? 'user_message' : 'message',
          event_id: message.id,
          conversation_id: message.conversation_id,
          run_id: message.run_id ?? undefined,
          content: message.content,
        })),
      );
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
        if (!cancelled && items.length > 0) {
          await selectConversation(items[0].id);
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : 'Не удалось загрузить историю');
          setIsHistoryLoading(false);
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
  }, [refreshConversations, selectConversation]);

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
          ws.send(
            JSON.stringify({
              type: 'run.subscribe',
              run_id: runId,
              after_seq: lastSeqByRunRef.current.get(runId) ?? 0,
            }),
          );
        } else {
          replayingRunRef.current = null;
          setIsReplaying(false);
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
          const terminalStatuses = new Set(['completed', 'failed', 'cancelled']);
          if (
            event.run_id === replayingRunRef.current &&
            typeof event.status === 'string' &&
            terminalStatuses.has(event.status)
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

        if (
          event.type === 'raw' &&
          event.error_code === 'run_not_found' &&
          event.run_id === replayingRunRef.current
        ) {
          replayingRunRef.current = null;
          if (event.run_id === activeRunRef.current) {
            activeRunRef.current = null;
            setIsRunning(false);
          }
          setIsReplaying(false);
        }

        if (event.run_id && typeof event.seq === 'number') {
          const lastSeq = lastSeqByRunRef.current.get(event.run_id) ?? 0;
          if (event.seq <= lastSeq) {
            return;
          }
          lastSeqByRunRef.current.set(event.run_id, event.seq);
        }

        if (event.type === 'history') {
          if (event.conversation_id && event.run_id) {
            selectionVersionRef.current += 1;
            activeConversationRef.current = event.conversation_id;
            activeRunRef.current = event.run_id;
            setActiveConversationId(event.conversation_id);
            setIsHistoryLoading(false);
            void refreshConversations().catch(() => undefined);
          }
          return;
        }

        if (event.type === 'result' && event.run_id === activeRunRef.current) {
          activeRunRef.current = null;
          setIsRunning(false);
          void refreshConversations().catch(() => undefined);
        }

        if (
          event.type === 'raw' &&
          event.error_code === 'claude_request_failed' &&
          event.run_id === activeRunRef.current
        ) {
          activeRunRef.current = null;
          setIsRunning(false);
        }

        if (event.type === 'thinking') {
          return;
        }

        if (
          event.conversation_id &&
          event.conversation_id !== activeConversationRef.current
        ) {
          return;
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
    setActiveConversationId(null);
    setIsHistoryLoading(false);
    setEvents([]);
    setError(null);
  };

  const sendPrompt = () => {
    const ws = wsRef.current;
    const text = prompt.trim();

    if (
      !ws ||
      ws.readyState !== WebSocket.OPEN ||
      !text ||
      isRunning ||
      isReplaying
    ) {
      return;
    }

    selectionVersionRef.current += 1;
    activeConversationRef.current = null;
    activeRunRef.current = null;
    setActiveConversationId(null);
    setIsHistoryLoading(false);
    setEvents([
      {
        type: 'user_message',
        content: text,
      },
    ]);
    setPrompt('');
    setError(null);
    setIsRunning(true);

    ws.send(
      JSON.stringify({
        type: 'run.create',
        content: text,
      }),
    );
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

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-header">
          <div>
            <div className="eyebrow">Claude Web</div>
            <h1>История</h1>
          </div>
          <button
            className="new-chat-button"
            type="button"
            onClick={startNewConversation}
            disabled={isRunning || isReplaying}
            title="Новый диалог"
          >
            +
          </button>
        </div>

        <div className="conversation-list">
          {isHistoryLoading && <div className="sidebar-state">Загрузка истории…</div>}
          {!isHistoryLoading && conversations.length === 0 && (
            <div className="sidebar-state">Диалогов пока нет</div>
          )}

          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              type="button"
              className={`conversation-item ${
                activeConversationId === conversation.id ? 'active' : ''
              }`}
              onClick={() => void selectConversation(conversation.id)}
              disabled={isReplaying}
            >
              <span className="conversation-title">{conversation.title}</span>
              <span className="conversation-time">
                {formatConversationTime(conversation.updated_at)}
              </span>
            </button>
          ))}
        </div>
      </aside>

      <section className="app">
        <header className="header">
          <div className="header-title">
            <span className={`status-dot ${connectionStatus}`} />
            <span>{connectionLabel}</span>
          </div>
          {isReplaying ? (
            <div className="run-status">Восстановление событий…</div>
          ) : (
            isRunning && <div className="run-status">Claude выполняет запрос…</div>
          )}
        </header>

        <main className="chat">
          <div className="chat-content">
            {events.length === 0 && !isHistoryLoading && (
              <div className="empty-state">
                <div className="empty-mark">C</div>
                <h2>Новый диалог</h2>
                <p>Напишите запрос, чтобы начать работу с Claude Code.</p>
              </div>
            )}

            {events.map((event, index) => {
              const key =
                typeof event.event_id === 'string'
                  ? event.event_id
                  : `${event.run_id ?? 'local'}-${event.seq ?? index}-${event.type}`;

              if (event.type === 'user_message') {
                return (
                  <div key={key} className="message-row user">
                    <div className="message user-message">
                      <div className="message-label">Вы</div>
                      <div className="message-content">{String(event.content ?? '')}</div>
                    </div>
                  </div>
                );
              }

              if (event.type === 'message') {
                return (
                  <div key={key} className="message-row claude">
                    <div className="message claude-message">
                      <div className="message-label">Claude</div>
                      <div className="message-content">{String(event.content ?? '')}</div>
                    </div>
                  </div>
                );
              }

              if (event.type === 'tool') {
                const input = isRecord(event.input) ? event.input : {};
                const filePath = typeof input.file_path === 'string' ? input.file_path : null;

                return (
                  <div key={key} className="tool-row">
                    <div className="tool-card">
                      <div className="tool-header">
                        <span className="tool-icon">⚙</span>
                        <strong>{String(event.name ?? 'Tool')}</strong>
                        <span className="tool-status">выполняется</span>
                      </div>
                      {filePath ? (
                        <div className="tool-path">{filePath}</div>
                      ) : (
                        <pre className="tool-input">{JSON.stringify(input, null, 2)}</pre>
                      )}
                    </div>
                  </div>
                );
              }

              if (event.type === 'tool_result') {
                const file = isRecord(event.file) ? event.file : {};
                const filePath = typeof file.path === 'string' ? file.path : null;
                const lineCount = typeof file.num_lines === 'number' ? file.num_lines : null;

                return (
                  <div key={key} className="tool-row">
                    <div className="tool-result">
                      <div className="tool-result-header">
                        <span>✓</span>
                        <strong>Результат инструмента</strong>
                        <span className="tool-status completed">готово</span>
                      </div>
                      {filePath && <div className="file-info">📄 {filePath}</div>}
                      {lineCount !== null && <div className="file-info">{lineCount} строк</div>}
                    </div>
                  </div>
                );
              }

              if (event.type === 'result') {
                const turns = typeof event.num_turns === 'number' ? event.num_turns : 0;
                const cost = typeof event.cost_usd === 'number' ? event.cost_usd : 0;
                const duration = typeof event.duration_ms === 'number' ? event.duration_ms : 0;

                return (
                  <div key={key} className="result-row">
                    <div className="result-card">
                      <div className="result-header">Запрос завершён</div>
                      <div className="result-stats">
                        <span>Шагов: {turns}</span>
                        <span>Стоимость: ${cost.toFixed(4)}</span>
                        <span>Время: {(duration / 1000).toFixed(1)} с</span>
                      </div>
                    </div>
                  </div>
                );
              }

              if (event.type === 'raw') {
                return (
                  <div key={key} className="warning-row">
                    <div className="warning-card">⚠ {String(event.content ?? 'Ошибка')}</div>
                  </div>
                );
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
              placeholder={
                isReplaying
                  ? 'Восстанавливаем события…'
                  : isRunning
                    ? 'Дождитесь завершения запроса…'
                    : 'Напишите сообщение…'
              }
              disabled={isRunning || isReplaying}
              rows={1}
            />
            <button
              type="button"
              onClick={sendPrompt}
              disabled={
                !prompt.trim() ||
                isRunning ||
                isReplaying ||
                connectionStatus !== 'connected'
              }
              aria-label="Отправить запрос"
            >
              ↑
            </button>
          </div>
          <div className="input-hint">Enter — отправить · Shift + Enter — новая строка</div>
        </div>
      </section>
    </div>
  );
}

export default App;
