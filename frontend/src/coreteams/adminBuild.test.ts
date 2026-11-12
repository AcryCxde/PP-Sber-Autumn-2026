import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const dockerfile = readFileSync(new URL('../../Dockerfile', import.meta.url), 'utf8');
const compose = readFileSync(new URL('../../../docker-compose.yml', import.meta.url), 'utf8');

describe('admin Docker build-time opt-in', () => {
  it('passes the explicit false-by-default flag into the Vite build stage', () => {
    const stage = dockerfile.split('FROM node:')[1];
    expect(stage).toContain('ARG VITE_ADMIN_SHELL_ENABLED=false');
    expect(stage).toContain('ENV VITE_ADMIN_SHELL_ENABLED=${VITE_ADMIN_SHELL_ENABLED}');
    expect(stage.indexOf('ENV VITE_ADMIN_SHELL_ENABLED=')).toBeLessThan(stage.indexOf('RUN npm run build'));
    expect(compose).toMatch(/frontend:\s+build:\s+context: \.\s+dockerfile: frontend\/Dockerfile\s+args:\s+VITE_ADMIN_SHELL_ENABLED: \$\{VITE_ADMIN_SHELL_ENABLED:-false\}/);
  });
});
