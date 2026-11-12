import type { EventData } from './events';

/** Admission commits reset after acceptance; only its visual boundary precedes the request. */
export function presentChatEvents(events: readonly EventData[]): EventData[] {
  const firstUserByRun = new Map<string, number>();
  events.forEach((event, index) => {
    if (event.type === 'user_message' && event.run_id && !firstUserByRun.has(event.run_id)) {
      firstUserByRun.set(event.run_id, index);
    }
  });

  const before = new Map<number, EventData[]>();
  const omitted = new Set<number>();
  const seenMarkers = new Set<string>();
  events.forEach((event, index) => {
    if (event.type !== 'context.reset') return;
    if (event.event_id) {
      if (seenMarkers.has(event.event_id)) {
        omitted.add(index);
        return;
      }
      seenMarkers.add(event.event_id);
    }
    const anchor = event.run_id ? firstUserByRun.get(event.run_id) : undefined;
    if (anchor !== undefined && anchor < index) {
      const markers = before.get(anchor) ?? [];
      markers.push(event);
      before.set(anchor, markers);
      omitted.add(index);
    }
  });

  const presented: EventData[] = [];
  events.forEach((event, index) => {
    const markers = before.get(index);
    if (markers) presented.push(...markers);
    if (!omitted.has(index)) presented.push(event);
  });
  return presented;
}
