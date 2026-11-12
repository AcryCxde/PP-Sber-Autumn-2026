import { describe, expect, it, vi } from 'vitest';
import { copyRunId } from './adminClipboard';

describe('Run ID clipboard', () => {
  it('reports unavailable clipboard instead of throwing', async () => {
    expect(await copyRunId('run-123', undefined)).toBe(false);
  });
  it('reports denied clipboard access without false success', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('Permission denied'));
    expect(await copyRunId('run-123', { writeText })).toBe(false);
  });
  it('copies the exact ID and reports success only after writing', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    expect(await copyRunId('run-123', { writeText })).toBe(true);
    expect(writeText).toHaveBeenCalledExactlyOnceWith('run-123');
  });
  it('also contains synchronous clipboard failures', async () => {
    expect(await copyRunId('run-123', { writeText: () => { throw new Error('Unavailable'); } })).toBe(false);
  });
});
