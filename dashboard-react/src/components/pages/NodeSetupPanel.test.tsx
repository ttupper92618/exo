import { configureStore } from '@reduxjs/toolkit';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { ThemeProvider } from 'styled-components';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiSlice } from '../../store/api';
import { darkTheme } from '../../theme/theme';
import { NodeSetupPanel } from './NodeSetupPanel';

Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { value: true, configurable: true });
vi.mock('../../i18n/tolgee', () => ({ useSkulkTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
let root: Root;
let host: HTMLDivElement;
let calls: string[];
let failure: boolean;
let store: ReturnType<typeof makeStore>;
function makeStore() { return configureStore({ reducer: { [apiSlice.reducerPath]: apiSlice.reducer }, middleware: (defaults) => defaults().concat(apiSlice.middleware) }); }
async function contains(text: string) { await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain(text)); }); }
async function check() { await act(async () => { host.querySelector('button')?.click(); }); }
beforeEach(async () => {
  calls = [];
  failure = false;
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.href), init);
    calls.push(request.method);
    return new Response(JSON.stringify(failure ? {} : { nodeId: 'node', revision: 4, credentialRevision: 2, schemaDigest: 'a'.repeat(64), artifacts: [
      { name: 'owner.json', title: 'Owner identity', mediaType: 'application/json', content: '{\"owner\":\"public\"}' },
    ] }), { status: failure ? 503 : 200, headers: { 'Content-Type': 'application/json' } });
  });
  store = makeStore();
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host);
  await act(async () => { root.render(<Provider store={store}><ThemeProvider theme={darkTheme}><NodeSetupPanel pluginId="plugin" nodeId="node" /></ThemeProvider></Provider>); });
});
afterEach(async () => { await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); }); host.remove(); vi.unstubAllGlobals(); });
it('downloads inert public setup files after an explicit read', async () => {
  expect(calls).toHaveLength(0);
  await check();
  await contains('Owner identity');
  const link = host.querySelector('a');
  expect(link?.download).toBe('owner.json');
  expect(link?.href).toBe('data:text/plain;charset=utf-8,' + encodeURIComponent('{"owner":"public"}'));
  expect(host.textContent).toContain('Settings / credential revisions: 4 / 2');
  expect(calls).toEqual(['GET']);
});
it('withdraws old downloads after a failed refresh', async () => {
  await check(); await contains('Owner identity');
  failure = true;
  await check(); await contains('Setup files are unavailable.');
  expect(host.querySelector('a')).toBeNull();
  expect(calls).toEqual(['GET', 'GET']);
});
