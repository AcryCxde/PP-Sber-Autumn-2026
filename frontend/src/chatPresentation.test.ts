import { describe, expect, it } from 'vitest';
import type { EventData } from './events';
import { presentChatEvents } from './chatPresentation';

const user = (id: string, run: string): EventData => ({ type: 'user_message', event_id: id, run_id: run });
const reset = (id: string, run?: string): EventData => ({ type: 'context.reset', event_id: id, run_id: run });
const ids = (events: readonly EventData[]) => events.map((event) => event.event_id);

describe('chat reset presentation', () => {
  it('anchors a committed history marker before the first request without mutating canonical data', () => {
    const events = Object.freeze([
      user('old', 'old'), user('new', 'new'), reset('reset', 'new'),
      { type: 'message.created', event_id: 'answer', run_id: 'new', seq: 4 },
    ]);
    expect(ids(presentChatEvents(events))).toEqual(['old', 'reset', 'new', 'answer']);
    expect(ids(events)).toEqual(['old', 'new', 'reset', 'answer']);
    expect(presentChatEvents(events)[2]).toBe(events[1]);
    expect(events[3].seq).toBe(4);
  });

  it('adds a live boundary only after the committed marker arrives', () => {
    const pending = [user('old', 'old'), user('new', 'new')];
    expect(ids(presentChatEvents(pending))).toEqual(['old', 'new']);
    expect(ids(presentChatEvents([...pending, reset('reset', 'new')]))).toEqual(['old', 'reset', 'new']);
    expect(ids(presentChatEvents([...pending, { type: 'run.failed', event_id: 'denied', run_id: 'new' }]))).toEqual(['old', 'new', 'denied']);
  });

  it('keeps every other event ordered across multiple resets and repeated user messages', () => {
    const events = [user('old', 'old'), user('one', 'one'), reset('r1', 'one'),
      { type: 'message.created', event_id: 'a1', run_id: 'one' }, user('two', 'two'),
      user('followup', 'two'), reset('r2', 'two')];
    expect(ids(presentChatEvents(events))).toEqual(['old', 'r1', 'one', 'a1', 'r2', 'two', 'followup']);
  });

  it('does not invent anchors for missing run IDs or unrelated runs', () => {
    const events = [reset('orphan'), user('user', 'other'), reset('missing', 'missing')];
    expect(ids(presentChatEvents(events))).toEqual(['orphan', 'user', 'missing']);
  });

  it('deduplicates replayed marker IDs but never collapses distinct markers', () => {
    const marker = reset('reset', 'new');
    expect(ids(presentChatEvents([user('new', 'new'), marker, { ...marker }]))).toEqual(['reset', 'new']);
    expect(ids(presentChatEvents([user('new', 'new'), marker, reset('distinct', 'new')]))).toEqual(['reset', 'distinct', 'new']);
  });

  it('is idempotent and leaves a marker already before its request in place', () => {
    const events = [user('old', 'old'), reset('reset', 'new'), user('new', 'new')];
    const presented = presentChatEvents(events);
    expect(ids(presented)).toEqual(ids(events));
    expect(presentChatEvents(presented)).toEqual(presented);
  });
});
