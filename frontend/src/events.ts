export type ContextState = 'new' | 'active' | 'reset' | 'unavailable';

export type StoredMessage = {
  id: string;
  conversation_id: string;
  run_id: string | null;
  role: string;
  kind: string;
  sequence: number;
  content: string;
  created_at: string;
};

export type PersistedEvent = {
  run_id: string;
  seq: number;
  type: string;
  payload: unknown;
  created_at: string;
};

export type CanonicalMessage = {
  id: string;
  conversation_id?: string;
  run_id?: string;
  role: string;
  kind: string;
  content: string;
};

export type EventData = {
  type: string;
  conversation_id?: string;
  run_id?: string;
  seq?: number;
  client_request_id?: string;
  context_mode?: string;
  content?: string;
  event_id?: string;
  optimistic?: boolean;
  idempotent_replay?: boolean;
  canonical_message?: CanonicalMessage;
  session_state?: ContextState;
  status?: string;
  terminal?: boolean;
  last_seq?: number;
  error_code?: string;
  message?: string;
  can_reset_context?: boolean;
  path?: string;
  num_lines?: number | null;
  size_bytes?: number | null;
  truncated?: boolean;
};

export type Artifact = {
  run_id: string;
  seq: number;
  path: string;
  num_lines: number | null;
  size_bytes: number | null;
  downloadable: boolean;
};

export type SequenceDecision =
  | { kind: 'applied'; nextSeq: number }
  | { kind: 'duplicate'; nextSeq: number }
  | { kind: 'gap'; nextSeq: number };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function stringField(record: Record<string, unknown>, key: string): string | undefined {
  return typeof record[key] === 'string' ? record[key] : undefined;
}

function numberField(record: Record<string, unknown>, key: string): number | undefined {
  return typeof record[key] === 'number' && Number.isInteger(record[key])
    ? record[key]
    : undefined;
}

export function contextState(value: unknown): ContextState | undefined {
  return value === 'new' || value === 'active' || value === 'reset' || value === 'unavailable'
    ? value
    : undefined;
}

export function canonicalMessage(value: unknown): CanonicalMessage | undefined {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.content !== 'string') {
    return undefined;
  }
  return {
    id: value.id,
    conversation_id: stringField(value, 'conversation_id'),
    run_id: stringField(value, 'run_id'),
    role: stringField(value, 'role') ?? 'user',
    kind: stringField(value, 'kind') ?? 'text',
    content: value.content,
  };
}

export function isSafeRelativeArtifactPath(path: string): boolean {
  if (!path || path.includes('\0') || path.includes('\\') || path.startsWith('/')) {
    return false;
  }
  if (/^[a-zA-Z]:/.test(path)) {
    return false;
  }
  const parts = path.split('/');
  return parts.every(
    (part) => part.length > 0 && part !== '.' && part !== '..' && part !== '.git' && part !== '.runner',
  );
}

function decodeSequenced(record: Record<string, unknown>): EventData | null {
  const type = stringField(record, 'type');
  const runId = stringField(record, 'run_id');
  const seq = numberField(record, 'seq');
  if (!type || !runId || seq === undefined || seq < 1) {
    return null;
  }

  const base: EventData = {
    type,
    run_id: runId,
    seq,
    conversation_id: stringField(record, 'conversation_id'),
  };

  if (type === 'run.accepted') {
    return {
      ...base,
      client_request_id: stringField(record, 'client_request_id'),
      context_mode: stringField(record, 'context_mode'),
      canonical_message: canonicalMessage(record.message),
      idempotent_replay: record.idempotent_replay === true,
    };
  }
  if (type === 'context.reset') {
    return { ...base, canonical_message: canonicalMessage(record.message) };
  }
  if (type === 'message.created') {
    const content = stringField(record, 'content');
    return content === undefined ? { ...base, type: 'ignored' } : { ...base, content };
  }
  if (type === 'artifact.saved') {
    const path = stringField(record, 'path');
    if (!path || !isSafeRelativeArtifactPath(path)) {
      return { ...base, type: 'ignored' };
    }
    return {
      ...base,
      path,
      num_lines: numberField(record, 'num_lines') ?? null,
      size_bytes: numberField(record, 'size_bytes') ?? null,
      truncated: record.truncated === true,
    };
  }
  if (type === 'run.completed') {
    return {
      ...base,
      status: 'completed',
      session_state: contextState(record.session_state),
    };
  }
  if (type === 'run.failed' || type === 'run.outcome_unknown') {
    return {
      ...base,
      status: stringField(record, 'status'),
      error_code: stringField(record, 'error_code'),
      message: stringField(record, 'message'),
      session_state: contextState(record.session_state),
      can_reset_context: record.can_reset_context === true,
    };
  }
  if (type === 'run.started' || type === 'legacy.redacted') {
    return { ...base, type: 'ignored' };
  }
  return { ...base, type: 'ignored' };
}

export function decodePersistedEvent(value: unknown): EventData | null {
  if (!isRecord(value) || !isRecord(value.payload)) {
    return null;
  }
  return decodeSequenced({
    ...value.payload,
    type: value.type,
    run_id: value.run_id,
    seq: value.seq,
  });
}

export function decodeSocketMessage(value: unknown): EventData | null {
  if (!isRecord(value)) {
    return null;
  }
  const type = stringField(value, 'type');
  if (type === 'replay.complete') {
    const runId = stringField(value, 'run_id');
    const lastSeq = numberField(value, 'last_seq');
    if (!runId || lastSeq === undefined) {
      return null;
    }
    return {
      type,
      run_id: runId,
      last_seq: lastSeq,
      status: stringField(value, 'status'),
      terminal: value.terminal === true,
    };
  }
  if (type === 'command.error') {
    return {
      type,
      client_request_id: stringField(value, 'client_request_id'),
      conversation_id: stringField(value, 'conversation_id'),
      error_code: stringField(value, 'code'),
      message: stringField(value, 'message'),
    };
  }
  return decodeSequenced(value);
}

export function sequenceDecision(lastSeq: number, eventSeq: number): SequenceDecision {
  if (eventSeq <= lastSeq) {
    return { kind: 'duplicate', nextSeq: lastSeq };
  }
  if (eventSeq !== lastSeq + 1) {
    return { kind: 'gap', nextSeq: lastSeq };
  }
  return { kind: 'applied', nextSeq: eventSeq };
}

export function artifactFromEvent(event: EventData): Artifact | null {
  if (
    event.type !== 'artifact.saved' ||
    !event.run_id ||
    typeof event.seq !== 'number' ||
    !event.path ||
    !isSafeRelativeArtifactPath(event.path)
  ) {
    return null;
  }
  return {
    run_id: event.run_id,
    seq: event.seq,
    path: event.path,
    num_lines: event.num_lines ?? null,
    size_bytes: event.size_bytes ?? null,
    downloadable: event.truncated !== true && typeof event.size_bytes === 'number',
  };
}
