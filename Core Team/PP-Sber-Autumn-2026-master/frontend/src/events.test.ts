import { describe, expect, it } from 'vitest';
import {
  artifactFromEvent,
  decodePersistedEvent,
  decodeSocketMessage,
  isSafeRelativeArtifactPath,
  sequenceDecision,
} from './events';

describe('event decoding', () => {
  it('projects public message fields without retaining arbitrary payload data', () => {
    const event = decodePersistedEvent({
      run_id: 'run-1',
      seq: 2,
      type: 'message.created',
      created_at: '2026-10-12T00:00:00Z',
      payload: { content: 'hello', secret: 'must-not-survive' },
    });

    expect(event).toEqual({
      type: 'message.created',
      run_id: 'run-1',
      seq: 2,
      conversation_id: undefined,
      content: 'hello',
    });
    expect(JSON.stringify(event)).not.toContain('must-not-survive');
  });

  it('turns legacy internal events into sequence-consuming no-ops', () => {
    const event = decodePersistedEvent({
      run_id: 'run-1',
      seq: 3,
      type: 'tool_result',
      created_at: '2026-10-12T00:00:00Z',
      payload: { content: 'secret output' },
    });

    expect(event).toEqual({
      type: 'ignored',
      run_id: 'run-1',
      seq: 3,
      conversation_id: undefined,
    });
    expect(JSON.stringify(event)).not.toContain('secret output');
  });

  it('distinguishes duplicate, contiguous, and gapped sequences', () => {
    expect(sequenceDecision(2, 2).kind).toBe('duplicate');
    expect(sequenceDecision(2, 3)).toEqual({ kind: 'applied', nextSeq: 3 });
    expect(sequenceDecision(2, 4)).toEqual({ kind: 'gap', nextSeq: 2 });
  });

  it('decodes bounded replay completion without advancing a cursor', () => {
    expect(decodeSocketMessage({
      type: 'replay.complete',
      run_id: 'run-1',
      last_seq: 4,
      status: 'running',
      terminal: false,
    })).toEqual({
      type: 'replay.complete',
      run_id: 'run-1',
      last_seq: 4,
      status: 'running',
      terminal: false,
    });
  });

  it('keeps outcome unknown distinct from failure', () => {
    expect(decodeSocketMessage({
      type: 'run.outcome_unknown',
      conversation_id: 'conversation-1',
      run_id: 'run-1',
      seq: 4,
      status: 'outcome_unknown',
      error_code: 'heartbeat_lost',
      session_state: 'unavailable',
      message: 'safe message',
    })).toMatchObject({
      type: 'run.outcome_unknown',
      status: 'outcome_unknown',
      session_state: 'unavailable',
    });
  });
});

describe('artifact projection', () => {
  it.each([
    '/host/secret.txt',
    'C:\\secret.txt',
    '../secret.txt',
    '.git/config',
    '.runner/state.json',
  ])('rejects unsafe path %s', (path: string) => {
    expect(isSafeRelativeArtifactPath(path)).toBe(false);
  });

  it('accepts and projects a relative artifact path', () => {
    const event = decodeSocketMessage({
      type: 'artifact.saved',
      conversation_id: 'conversation-1',
      run_id: 'run-1',
      seq: 3,
      path: 'reports/result.md',
      size_bytes: 42,
      num_lines: 2,
      truncated: false,
    });

    expect(event).not.toBeNull();
    expect(artifactFromEvent(event!)).toEqual({
      run_id: 'run-1',
      seq: 3,
      path: 'reports/result.md',
      size_bytes: 42,
      num_lines: 2,
      downloadable: true,
    });
  });
});
