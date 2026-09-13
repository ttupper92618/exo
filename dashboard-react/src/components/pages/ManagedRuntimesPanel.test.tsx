import { configureStore } from '@reduxjs/toolkit';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { ThemeProvider } from 'styled-components';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiSlice } from '../../store/api';
import type { ManagedOperation, ManagedRuntime } from '../../store/endpoints/plugins';
import { darkTheme } from '../../theme/theme';
import { ManagedRuntimesPanel } from './ManagedRuntimesPanel';

Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { value: true, configurable: true });
vi.mock('../../i18n/tolgee', () => ({ useSkulkTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));

let root: Root;
let host: HTMLDivElement;
let runtime: ManagedRuntime;
let operation: ManagedOperation | null;
let posts: { path: string; body: Record<string, unknown> | null }[];
let operationReads: string[];
let store: ReturnType<typeof makeStore>;

function makeStore() {
  return configureStore({ reducer: { [apiSlice.reducerPath]: apiSlice.reducer }, middleware: (defaults) => defaults().concat(apiSlice.middleware) });
}
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}
async function mount() {
  root = createRoot(host);
  await act(async () => { root.render(<Provider store={store}><ThemeProvider theme={darkTheme}><ManagedRuntimesPanel /></ThemeProvider></Provider>); });
}
async function click(label: string) {
  await act(async () => {
    const button = [...host.querySelectorAll('button')].find((item) => item.textContent === label);
    if (!button) throw new Error(`Missing button: ${label}`);
    button.click();
  });
}
async function contains(text: string) {
  await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain(text)); });
}

beforeEach(async () => {
  runtime = { plugin_id: 'managed.fixture', selected_digest: 'a'.repeat(64), selection_revision: 7, enabled: true, stale: false, error_code: null, operation_id: null, operation_state: null,
    service: { state: 'running', active_digest: 'a'.repeat(64), observed_at: 1 } };
  operation = null;
  posts = [];
  operationReads = [];
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    const path = new URL(request.url).pathname;
    expect(request.headers.get('X-Skulk-Dashboard')).toBe('pairing-v1');
    if (request.method === 'POST') {
      const body = path.endsWith('/recover') ? null : await request.json() as Record<string, unknown>;
      posts.push({ path, body });
      if (path.endsWith('/recover')) {
        if (!operation) throw new Error('Missing retained operation');
        operation = { ...operation, state: 'complete' };
        runtime = { ...runtime, operation_state: 'complete', enabled: false };
        return response(operation);
      }
      const id = String(body?.operation_id);
      operation = { request: { operation_id: id, action: body?.action === 'uninstall' ? 'uninstall' : 'disable' }, state: 'applying', error_code: null };
      runtime = { ...runtime, operation_id: id, operation_state: 'applying' };
      // The manager accepted the original request but the browser lost its response.
      return response({}, 503);
    }
    if (path === '/v1/plugins/managed') return response({ installations: [runtime] });
    operationReads.push(path);
    return operation ? response(operation) : response({}, 404);
  });
  store = makeStore();
  host = document.createElement('div');
  document.body.appendChild(host);
  await mount();
});
afterEach(async () => {
  await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); });
  host.remove();
  vi.unstubAllGlobals();
});

it('recovers a server-retained operation reference after reconnect without another submission', async () => {
  await contains('managed.fixture');
  await click('Disable runtime');
  await contains('Applying');
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({ action: 'disable', expected_revision: 7 });
  const id = String(posts[0].body?.operation_id);
  expect(id).toMatch(/^[a-f0-9]{32}$/);
  await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); });
  await mount();
  await contains('Applying');
  await click('Refresh operation status');
  expect(posts).toHaveLength(1);
  expect(operationReads.some((path) => path.endsWith('/' + id))).toBe(true);
});

it('requires a separate recovery action for the original local operation', async () => {
  operation = { request: { operation_id: 'b'.repeat(32), action: 'disable' }, state: 'recovery_required', error_code: 'local_io_failed' };
  runtime = { ...runtime, operation_id: operation.request.operation_id, operation_state: operation.state };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('Recover local operation');
  expect(posts).toHaveLength(0);
  await click('Disable runtime');
  expect(posts).toHaveLength(0);
  await click('Recover local operation');
  await contains('Complete');
  expect(posts).toEqual([{ path: '/v1/plugins/managed/installations/managed.fixture/operations/' + 'b'.repeat(32) + '/recover', body: null }]);
});

it('offers explicit withdrawal of a failed initial activation without replaying it', async () => {
  operation = { request: { operation_id: 'c'.repeat(32), action: 'activate' }, state: 'recovery_required', error_code: 'validation_failed' };
  runtime = { ...runtime, enabled: false, selected_digest: null, selection_revision: 0, operation_id: operation.request.operation_id, operation_state: operation.state };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('Disable or uninstall withdraws this pending local change');
  expect(posts).toHaveLength(0);
  await click('Disable runtime');
  await contains('Applying');
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({ action: 'disable', expected_revision: 0 });
  expect(posts[0].body?.operation_id).not.toBe('c'.repeat(32));
  await click('Disable runtime');
  expect(posts).toHaveLength(1);
});


it('retains an uncertain uninstall across reconnect without another submission', async () => {
  await contains('managed.fixture');
  await click('Uninstall plugin');
  await contains('Applying');
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({ action: 'uninstall', expected_revision: 7 });
  await click('Uninstall plugin');
  expect(posts).toHaveLength(1);
  await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); });
  await mount();
  await contains('Applying');
  await click('Refresh operation status');
  expect(posts).toHaveLength(1);
});

it('shows retained uninstall status and offers explicit release reinstallation', async () => {
  runtime = { ...runtime, uninstalled: true, enabled: false, service: null };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('Plugin uninstalled; cleanup state retained');
  await click('Uninstall plugin');
  await click('Disable runtime');
  expect(posts).toHaveLength(0);
  expect([...host.querySelectorAll('button')].find((item) => item.textContent === 'Install a release')?.disabled).toBe(false);
});


it('allows uninstall to withdraw a stalled reinstallation while retaining uninstalled status', async () => {
  operation = { request: { operation_id: 'd'.repeat(32), action: 'select' }, state: 'recovery_required', error_code: 'validation_failed' };
  runtime = { ...runtime, uninstalled: true, enabled: false, operation_id: operation.request.operation_id, operation_state: operation.state };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('Disable or uninstall withdraws this pending local change');
  await click('Disable runtime');
  expect(posts).toHaveLength(0);
  await click('Uninstall plugin');
  await contains('Applying');
  expect(posts).toHaveLength(1);
  expect(posts[0].body).toMatchObject({ action: 'uninstall', expected_revision: 7 });
});
