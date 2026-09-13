import { configureStore } from '@reduxjs/toolkit';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { ThemeProvider } from 'styled-components';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiSlice } from '../../store/api';
import { darkTheme } from '../../theme/theme';
import { NodePreflightPanel } from './NodePreflightPanel';

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
    return new Response(JSON.stringify(failure ? {} : { nodeId: 'node', revision: 4, observedAt: 1800000000, checks: [
      { code: 'runtime', passed: true, correctiveAction: null },
      { code: 'credentials', passed: false, correctiveAction: 'Supply the required credential.' },
    ] }), { status: failure ? 503 : 200, headers: { 'Content-Type': 'application/json' } });
  });
  store = makeStore();
  host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host);
  await act(async () => { root.render(<Provider store={store}><ThemeProvider theme={darkTheme}><NodePreflightPanel pluginId="plugin" nodeId="node" /></ThemeProvider></Provider>); });
});
afterEach(async () => { await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); }); host.remove(); vi.unstubAllGlobals(); });
it('runs explicit read checks and renders corrective actions without enabling', async () => {
  expect(calls).toHaveLength(0);
  await check();
  await contains('Supply the required credential.');
  expect(host.textContent).toContain('Checked settings revision 4');
  expect(host.textContent).toContain('Enabling runs fresh checks');
  expect(calls).toEqual(['GET']);
});
it('does not present an earlier report as current after a failed refresh', async () => {
  await check(); await contains('Supply the required credential.');
  failure = true;
  await check(); await contains('Setup checks are unavailable.');
  expect(host.textContent).not.toContain('Checked settings revision');
  expect(calls).toEqual(['GET', 'GET']);
});
