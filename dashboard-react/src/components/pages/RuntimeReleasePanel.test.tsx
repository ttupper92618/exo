import { configureStore } from '@reduxjs/toolkit';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { ThemeProvider } from 'styled-components';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiSlice } from '../../store/api';
import type { ManagedRuntime, RuntimeInstallation, RuntimeRelease } from '../../store/endpoints/plugins';
import { darkTheme } from '../../theme/theme';
import { RuntimeReleasePanel } from './RuntimeReleasePanel';

Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { value: true, configurable: true });
vi.mock('../../i18n/tolgee', () => ({ useSkulkTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const runtime: ManagedRuntime = { plugin_id: 'managed.fixture', selected_digest: null, selection_revision: 0, enabled: false, stale: true, error_code: null, operation_id: null, operation_state: null, service: null };
const review: RuntimeRelease = { runtime_digest: 'a'.repeat(64), source_revision: 1, publisher: 'fixture', bundle_id: 'example.plugin', version: '1.2.3', sequence: 1, platform: 'macos-arm64', python_requires: '>=3.13', skulk_build_sha256: 'b'.repeat(64), permissions: ['Read provider inventory'], artifact_bytes: 100, expires_at: 2000000000 };
let root: Root;
let host: HTMLDivElement;
let operation: RuntimeInstallation | null;
let posts: Record<string, unknown>[];
let recoveryPaths: string[];
let sourceAvailable: boolean;
let store: ReturnType<typeof makeStore>;
function makeStore() { return configureStore({ reducer: { [apiSlice.reducerPath]: apiSlice.reducer }, middleware: (defaults) => defaults().concat(apiSlice.middleware) }); }
function response(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
async function mount() {
  root = createRoot(host);
  await act(async () => { root.render(<Provider store={store}><ThemeProvider theme={darkTheme}><RuntimeReleasePanel runtime={runtime} /></ThemeProvider></Provider>); });
}
async function contains(text: string) { await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain(text)); }); }
async function click(label: string) {
  await act(async () => {
    const button = [...host.querySelectorAll('button')].find((item) => item.textContent === label);
    if (!button) throw new Error('Missing button: ' + label);
    button.click();
  });
}
beforeEach(async () => {
  operation = null;
  posts = [];
  recoveryPaths = [];
  sourceAvailable = true;
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    const path = new URL(request.url).pathname;
    if (request.method === 'POST') {
      const body = await request.json() as Record<string, unknown>;
      posts.push(body);
      if (path.endsWith('/recover') && operation) {
        recoveryPaths.push(path);
        operation = { ...operation, attempt: 1, attempt_source_revision: Number(body.expected_source_revision), state: 'staged', downloaded_bytes: 100, error_code: null };
        return response({}, 503);
      }
      if (path.endsWith('/install')) {
        operation = { request: { operation_id: String(body.operation_id), runtime_digest: String(body.runtime_digest), expected_source_revision: Number(body.expected_source_revision) }, review, state: 'staged', downloaded_bytes: 100, error_code: null };
        return response({}, 503);
      }
      return response({ request: body, state: 'accepted', error_code: null });
    }
    if (path.endsWith('/source')) return sourceAvailable ? response({ revision: 2, configured: true, credential_ready: true, credential_reference: null, trust_revision: 1 }) : response({}, 503);
    return response(path.endsWith('/release') ? review : { operation });
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

it('reviews before staging and restores the original accepted install after reconnect', async () => {
  await click('Inspect configured release');
  await contains('example.plugin 1.2.3');
  expect(posts).toHaveLength(0);
  await click('Download and install');
  await contains('Ready for activation');
  expect(posts).toHaveLength(1);
  expect(posts[0]).toMatchObject({ runtime_digest: review.runtime_digest, expected_source_revision: 1 });
  await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); });
  await mount();
  await contains('Ready for activation');
  await click('Refresh installation status');
  expect(posts).toHaveLength(1);
});

it('requires distinct permission acceptance and activation after installation', async () => {
  await click('Inspect configured release');
  await contains('example.plugin 1.2.3');
  await click('Download and install');
  await contains('Ready for activation');
  await click('Activate release');
  expect(posts).toHaveLength(1);
  await act(async () => { host.querySelector<HTMLInputElement>('input[type=checkbox]')?.click(); });
  await click('Activate release');
  await act(async () => { await vi.waitFor(() => expect(posts).toHaveLength(2)); });
  expect(posts[1]).toMatchObject({ action: 'activate', runtime_digest: review.runtime_digest, expected_revision: 0, accept_permissions: true, rollback: false });
});

it('does not transfer permission acceptance to another release observed after reconnect', async () => {
  operation = { request: { operation_id: 'c'.repeat(32), runtime_digest: review.runtime_digest, expected_source_revision: 1 }, review, state: 'staged', downloaded_bytes: 100, error_code: null };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('example.plugin 1.2.3');
  await act(async () => { host.querySelector<HTMLInputElement>('input[type=checkbox]')?.click(); });
  const replacement = { ...review, runtime_digest: 'd'.repeat(64), version: '2.0.0', permissions: ['A different permission'] };
  operation = { ...operation, request: { ...operation.request, runtime_digest: replacement.runtime_digest }, review: replacement };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('example.plugin 2.0.0');
  expect(host.querySelector<HTMLInputElement>('input[type=checkbox]')?.checked).toBe(false);
  await click('Activate release');
  expect(posts).toHaveLength(0);
});

it('recovers the original installation with the current source and only observes after a lost response', async () => {
  const original = { operation_id: 'c'.repeat(32), runtime_digest: review.runtime_digest, expected_source_revision: 1 };
  operation = { request: original, review, state: 'recovery_required', downloaded_bytes: 0, error_code: 'download_failed' };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('Installation needs recovery');
  await act(async () => { await vi.waitFor(() => expect([...host.querySelectorAll('button')].find((item) => item.textContent === 'Retry this installation')?.disabled).toBe(false)); });
  expect(posts).toHaveLength(0);
  await click('Retry this installation');
  await contains('Ready for activation');
  expect(posts).toEqual([{ expected_source_revision: 2 }]);
  expect(recoveryPaths).toEqual([`/v1/plugins/managed/installations/${runtime.plugin_id}/install/${original.operation_id}/recover`]);
  expect(operation?.request).toEqual(original);
  await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); });
  await mount();
  await contains('Ready for activation');
  await click('Refresh installation status');
  expect(posts).toHaveLength(1);
});

it('withholds recovery when source readiness is unavailable and permits an explicit status refresh', async () => {
  sourceAvailable = false;
  operation = { request: { operation_id: 'c'.repeat(32), runtime_digest: review.runtime_digest, expected_source_revision: 1 }, review, state: 'recovery_required', downloaded_bytes: 0, error_code: 'download_failed' };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('Restore the release source');
  await click('Retry this installation');
  expect(posts).toHaveLength(0);
  sourceAvailable = true;
  await click('Refresh installation status');
  await act(async () => { await vi.waitFor(() => expect([...host.querySelectorAll('button')].find((item) => item.textContent === 'Retry this installation')?.disabled).toBe(false)); });
  await click('Retry this installation');
  await contains('Ready for activation');
  expect(posts).toEqual([{ expected_source_revision: 2 }]);
});


it('selects a stopped runtime after permission review and permits later explicit activation', async () => {
  await click('Inspect configured release');
  await contains('example.plugin 1.2.3');
  await click('Download and install');
  await contains('Ready for activation');
  await click('Select with owner stopped');
  expect(posts).toHaveLength(1);
  await act(async () => { host.querySelector<HTMLInputElement>('input[type=checkbox]')?.click(); });
  await click('Select with owner stopped');
  await act(async () => { await vi.waitFor(() => expect(posts).toHaveLength(2)); });
  expect(posts[1]).toMatchObject({ action: 'select', runtime_digest: review.runtime_digest, expected_revision: 0, accept_permissions: true });
  await click('Activate release');
  expect(posts).toHaveLength(2);
  const selected: ManagedRuntime = { ...runtime, selected_digest: review.runtime_digest, selection_revision: 1, operation_id: String(posts[1].operation_id), operation_state: 'complete' };
  await act(async () => { root.render(<Provider store={store}><ThemeProvider theme={darkTheme}><RuntimeReleasePanel runtime={selected} /></ThemeProvider></Provider>); });
  await click('Select with owner stopped');
  expect(posts).toHaveLength(2);
  await click('Activate release');
  await act(async () => { await vi.waitFor(() => expect(posts).toHaveLength(3)); });
  expect(posts[2]).toMatchObject({ action: 'activate', expected_revision: 1 });
});
