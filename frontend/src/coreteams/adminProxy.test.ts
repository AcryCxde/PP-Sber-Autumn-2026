import { describe, expect, it } from 'vitest';
import config from '../../vite.config';

describe('admin same-origin proxy contract', () => {
  it('preserves the incoming Host instead of rewriting it away from browser Origin', () => {
    expect(config.server?.proxy?.['/api']).toMatchObject({ changeOrigin: false });
    expect(config.server?.proxy?.['/ws']).toMatchObject({ ws: true });
  });
});
