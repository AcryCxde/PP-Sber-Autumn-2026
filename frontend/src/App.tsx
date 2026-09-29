import { useEffect, useRef, useState } from 'react';
import './App.css';

type EventData = {
  type: string;
  [key: string]: any;
};

function App() {
  const [prompt, setPrompt] = useState('');
  const [events, setEvents] = useState<EventData[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const ws = new WebSocket('ws://localhost:8000/ws');

    wsRef.current = ws;

    ws.onopen = () => {
      console.log('WebSocket connected');
    };

    ws.onmessage = (event) => {
      try {
        const data: EventData = JSON.parse(event.data);

        // Thinking не показываем
        if (data.type === 'thinking') {
          return;
        }

        setEvents((prev) => [...prev, data]);
      } catch (error) {
        console.error('Invalid WebSocket message:', event.data);
      }
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    ws.onclose = () => {
      console.log('WebSocket closed');
    };

    return () => {
      ws.close();
    };
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: 'smooth',
    });
  }, [events]);

  const sendPrompt = () => {
    const ws = wsRef.current;

    if (!ws || ws.readyState !== WebSocket.OPEN || !prompt.trim()) {
      return;
    }

    const text = prompt.trim();

    // Сразу показываем сообщение пользователя
    setEvents((prev) => [
      ...prev,
      {
        type: 'user_message',
        content: text,
      },
    ]);

    ws.send(text);
    setPrompt('');
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendPrompt();
    }
  };

  return (
    <div className="app">
      <header className="header">
        <div className="header-title">
          <div className="status-dot" />
          <span>Claude Code</span>
        </div>
      </header>

      <main className="chat">
        <div className="chat-content">
          {events.length === 0 && (
            <div className="empty-state">
              <h2>Claude Code</h2>
              <p>Напиши запрос, чтобы начать работу</p>
            </div>
          )}

          {events.map((event, index) => {
            if (event.type === 'user_message') {
              return (
                <div key={index} className="message-row user">
                  <div className="message user-message">
                    <div className="message-label">You</div>

                    <div className="message-content">{event.content}</div>
                  </div>
                </div>
              );
            }
            // --------------------------------
            // Claude message
            // --------------------------------

            if (event.type === 'message') {
              return (
                <div key={index} className="message-row claude">
                  <div className="message claude-message">
                    <div className="message-label">Claude</div>

                    <div className="message-content">{event.content}</div>
                  </div>
                </div>
              );
            }

            // --------------------------------
            // Tool call
            // --------------------------------

            if (event.type === 'tool') {
              const filePath = event.input?.file_path;

              return (
                <div key={index} className="tool-row">
                  <div className="tool-card">
                    <div className="tool-header">
                      <span className="tool-icon">⚙</span>

                      <strong>{event.name}</strong>

                      <span className="tool-status">running</span>
                    </div>

                    {filePath && <div className="tool-path">{filePath}</div>}

                    {!filePath && (
                      <pre className="tool-input">{JSON.stringify(event.input, null, 2)}</pre>
                    )}
                  </div>
                </div>
              );
            }

            // --------------------------------
            // Tool result
            // --------------------------------

            if (event.type === 'tool_result') {
              return (
                <div key={index} className="tool-row">
                  <div className="tool-result">
                    <div className="tool-result-header">
                      <span>✓</span>
                      <strong>Read</strong>
                      <span className="tool-status completed">completed</span>
                    </div>

                    {event.file?.path && <div className="file-info">📄 {event.file.path}</div>}

                    {event.file?.num_lines != null && (
                      <div className="file-info">{event.file.num_lines} lines</div>
                    )}
                  </div>
                </div>
              );
            }

            // --------------------------------
            // Final result
            // --------------------------------

            if (event.type === 'result') {
              return (
                <div key={index} className="result-row">
                  <div className="result-card">
                    <div className="result-header">Completed</div>

                    <div className="result-stats">
                      <span>Turns: {event.num_turns}</span>

                      <span>Cost: ${event.cost_usd?.toFixed(4)}</span>

                      <span>Time: {(event.duration_ms / 1000).toFixed(1)}s</span>
                    </div>
                  </div>
                </div>
              );
            }

            // --------------------------------
            // Warning / raw
            // --------------------------------

            if (event.type === 'raw') {
              return (
                <div key={index} className="warning-row">
                  <div className="warning-card">⚠ {event.content}</div>
                </div>
              );
            }

            // --------------------------------
            // Unknown event
            // --------------------------------

            return null;
          })}

          <div ref={messagesEndRef} />
        </div>
      </main>

      <div className="input-area">
        <div className="input-wrapper">
          <textarea
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Напишите сообщение..."
            rows={1}
          />

          <button onClick={sendPrompt} disabled={!prompt.trim()}>
            ↑
          </button>
        </div>

        <div className="input-hint">Enter — отправить · Shift + Enter — новая строка</div>
      </div>
    </div>
  );
}

export default App;
