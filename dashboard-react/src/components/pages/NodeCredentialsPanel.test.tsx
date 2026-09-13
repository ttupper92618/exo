import { configureStore } from '@reduxjs/toolkit';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { ThemeProvider } from 'styled-components';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiSlice } from '../../store/api';
import type { NodeCredentials } from '../../store/endpoints/plugins';
import { darkTheme } from '../../theme/theme';
import { NodeCredentialsPanel } from './NodeCredentialsPanel';

Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { value: true, configurable: true });
vi.mock('../../i18n/tolgee', () => ({ useSkulkTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
let root: Root;
let host: HTMLDivElement;
let store: ReturnType<typeof makeStore>;
let current: NodeCredentials;
let posts: Record<string, unknown>[];
let loseResponse: boolean;
function makeStore() { return configureStore({ reducer: { [apiSlice.reducerPath]: apiSlice.reducer }, middleware: (defaults) => defaults().concat(apiSlice.middleware) }); }
function response(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
async function contains(text: string) { await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain(text)); }); }
async function click(label: string) { await act(async () => { [...host.querySelectorAll('button')].find((item) => item.textContent === label)?.click(); }); }
function fill(value: string) { const input = host.querySelector('input, textarea') as HTMLInputElement | HTMLTextAreaElement | null; if (!input) throw new Error('Missing credential input'); input.value = value; }
beforeEach(async () => {
  posts = [];
  loseResponse = false;
  current = { nodeId: 'stable-node', revision: 0, schemaDigest: 'a'.repeat(64), credentials: [{ credentialId: 'witness-rpc', title: 'Cleanup credential', description: 'Authenticate the existing cleanup service.', required: true, ready: false }] };
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.href), init);
    if (request.method === 'POST') {
      expect((host.querySelector('input, textarea') as HTMLInputElement | HTMLTextAreaElement | null)?.value).toBe('');
      const body = await request.json() as Record<string, unknown>;
      posts.push(body);
      current = { ...current, revision: current.revision + 1, credentials: current.credentials.map((item) => ({ ...item, ready: body.operation === 'replace' })) };
      return response(current, loseResponse ? 503 : 200);
    }
    return response(current);
  });
  store = makeStore();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root.render(<Provider store={store}><ThemeProvider theme={darkTheme}><NodeCredentialsPanel pluginId="fixture" nodeId="stable-node" /></ThemeProvider></Provider>); });
  await contains('Cleanup credential');
});
afterEach(async () => {
  await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); });
  host.remove();
  vi.unstubAllGlobals();
});

it('replaces and retires credentials without placing secret values in Redux', async () => {
  fill('synthetic-credential-value');
  await click('Replace credential');
  await contains('Credential metadata updated');
  expect(posts).toHaveLength(1);
  expect(posts[0]).toMatchObject({ operation: 'replace', credentialId: 'witness-rpc', value: 'synthetic-credential-value', expectedRevision: 0, expectedSchemaDigest: current.schemaDigest });
  expect(JSON.stringify(store.getState())).not.toContain('synthetic-credential-value');
  expect(host.querySelector('input')?.value).toBe('');
  await click('Retire future use');
  await contains('Not ready');
  expect(posts[1]).toMatchObject({ operation: 'retire', expectedRevision: 1 });
  expect(posts[1]).not.toHaveProperty('value');
});

it('observes an unconfirmed replacement and requires refresh before another action', async () => {
  loseResponse = true;
  fill('synthetic-unconfirmed-secret');
  await click('Replace credential');
  await contains('The credential update was not confirmed');
  await click('Replace credential');
  expect(posts).toHaveLength(1);
  await click('Refresh credential status');
  await contains('Credential status refreshed');
  await click('Replace credential');
  await contains('Enter a credential before replacing it');
  expect(posts).toHaveLength(1);
  expect(JSON.stringify(store.getState())).not.toContain('synthetic-unconfirmed-secret');
});

it('preserves the revision fence when credentials change while entering a value', async () => {
  fill('synthetic-stale-draft');
  current = { ...current, revision: 2 };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('Credentials changed elsewhere');
  await click('Replace credential');
  expect(posts).toHaveLength(0);
  await click('Refresh credential status');
  await contains('Credential status refreshed');
  expect(host.querySelector('input')?.value).toBe('');
});

it('preserves multiline credential bytes and clears drafts before an unconfirmed submission', async () => {
  const value = '-----BEGIN TEST KEY-----\nsynthetic-multiline-credential\n-----END TEST KEY-----\n';
  fill('single-line-draft');
  await click('Use multiline input');
  expect(host.querySelector('textarea')?.value).toBe('');
  await contains('Multiline credentials are visible while editing');
  fill(value);
  expect(host.querySelector('textarea')?.value).toBe(value);
  expect(JSON.stringify(store.getState())).not.toContain('synthetic-multiline-credential');
  loseResponse = true;
  await click('Replace credential');
  await contains('The credential update was not confirmed');
  expect(posts).toHaveLength(1);
  expect(posts[0].value).toBe(value);
  expect(host.querySelector('textarea')?.value).toBe('');
  expect(JSON.stringify(store.getState())).not.toContain('synthetic-multiline-credential');
  await click('Replace credential');
  expect(posts).toHaveLength(1);
  await click('Refresh credential status');
  await contains('Credential status refreshed');
  fill(value);
  await click('Use single-line input');
  expect(host.querySelector('textarea')).toBeNull();
  expect(host.querySelector('input')?.type).toBe('password');
  expect(host.querySelector('input')?.value).toBe('');
});
