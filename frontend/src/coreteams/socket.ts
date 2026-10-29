import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { notifyUnauthorized, WS_UNAUTHORIZED } from '../auth';
import { decodeSocketMessage, type EventData } from '../events';

export type SocketStatus = 'connecting' | 'connected' | 'disconnected';
type Listener = (event: EventData) => void;

export type GatewaySocket = {
  status: SocketStatus;
  /** false — соединения сейчас нет, команда не отправлена. */
  send: (command: Record<string, unknown>) => boolean;
  subscribe: (listener: Listener) => () => void;
};

function websocketUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}

/** Одно соединение /ws на всю оболочку CoreTeams; переподключается сам, кроме потери сессии. */
export function useGatewaySocket(): GatewaySocket {
  const [status, setStatus] = useState<SocketStatus>('connecting');
  // Изменяемое состояние соединения, общее для обработчиков; в рендере не читается.
  const [connection] = useState(() => ({ ws: null as WebSocket | null, listeners: new Set<Listener>() }));

  useEffect(() => {
    let disposed = false;
    let delay = 500;
    let timer: number | null = null;

    const connect = () => {
      if (disposed) {
        return;
      }
      setStatus('connecting');
      const ws = new WebSocket(websocketUrl());
      connection.ws = ws;
      ws.onopen = () => {
        delay = 500;
        setStatus('connected');
      };
      ws.onmessage = (message) => {
        let parsed: unknown;
        try {
          parsed = JSON.parse(message.data) as unknown;
        } catch {
          return;
        }
        const event = decodeSocketMessage(parsed);
        if (event) {
          connection.listeners.forEach((listener) => listener(event));
        }
      };
      ws.onclose = (closeEvent) => {
        if (connection.ws !== ws) {
          return;
        }
        connection.ws = null;
        setStatus('disconnected');
        if (closeEvent.code === WS_UNAUTHORIZED) {
          disposed = true;
          notifyUnauthorized();
          return;
        }
        if (!disposed) {
          timer = window.setTimeout(connect, delay);
          delay = Math.min(delay * 2, 5000);
        }
      };
    };

    connect();
    return () => {
      disposed = true;
      if (timer !== null) {
        window.clearTimeout(timer);
      }
      const ws = connection.ws;
      connection.ws = null;
      ws?.close();
    };
  }, [connection]);

  return useMemo(
    () => ({
      status,
      send: (command) => {
        const ws = connection.ws;
        if (ws?.readyState !== WebSocket.OPEN) {
          return false;
        }
        ws.send(JSON.stringify(command));
        return true;
      },
      subscribe: (listener) => {
        connection.listeners.add(listener);
        return () => {
          connection.listeners.delete(listener);
        };
      },
    }),
    [status, connection],
  );
}

export const SocketContext = createContext<GatewaySocket | null>(null);

export function useSocket(): GatewaySocket {
  const socket = useContext(SocketContext);
  if (!socket) {
    throw new Error('SocketContext is not provided');
  }
  return socket;
}
