import { configureStore } from '@reduxjs/toolkit';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { ThemeProvider } from 'styled-components';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiSlice } from '../../store/api';
import type { SetupActions, SetupOperation } from '../../store/endpoints/plugins';
import { darkTheme } from '../../theme/theme';
import { NodeSetupActionsPanel } from './NodeSetupActionsPanel';
Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { value: true, configurable: true });
vi.mock('../../i18n/tolgee', () => ({ useSkulkTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
let root: Root;
let host: HTMLDivElement;
let snapshot: SetupActions;
let calls: { method: string; path: string; body: string }[];
let failRead: boolean;
let loseReply: boolean;
let store: ReturnType<typeof makeStore>;
function makeStore() { return configureStore({ reducer: { [apiSlice.reducerPath]: apiSlice.reducer }, middleware: (defaults) => defaults().concat(apiSlice.middleware) }); }
async function contains(text: string) { await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain(text)); }); }
function button(text: string) { const result = [...host.querySelectorAll('button')].find((item) => item.textContent === text); if (!result) throw new Error(`Missing button ${text}`); return result; }
async function click(text: string) { await act(async () => { button(text).click(); }); }
async function render() { await act(async () => { root.render(<Provider store={store}><ThemeProvider theme={darkTheme}><NodeSetupActionsPanel pluginId="plugin" nodeId="node" /></ThemeProvider></Provider>); }); }
async function fill(value: string) { await act(async () => { const input = host.querySelector('input'); if (!input) throw new Error('Missing field'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(input, value); input.dispatchEvent(new Event('input', { bubbles: true })); }); }
beforeEach(async () => {
  calls = []; failRead = false; loseReply = false;
  snapshot = { nodeId: 'node', revision: 2, schemaDigest: 'a'.repeat(64), credentialRevision: 3, credentialSchemaDigest: 'b'.repeat(64), actions: [{ actionId: 'connect', title: 'Connect existing service', description: 'Ordinary external endpoint only.', schemaDigest: 'c'.repeat(64), parametersSchema: { type: 'object', properties: { host: { type: 'string', title: 'Host' } }, required: ['host'] } }], operations: [] };
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), location.href), init);
    const body = await request.text(); const path = new URL(request.url).pathname;
    calls.push({ method: request.method, path, body });
    if (request.method === 'POST') {
      const operation: SetupOperation = path.endsWith('/resume') ? { ...snapshot.operations[0], phase: 'running' } : { operationId: JSON.parse(body).operationId, nodeId: 'node', actionId: 'connect', phase: 'running', code: null, correctiveAction: null };
      snapshot = { ...snapshot, operations: [operation] };
      return new Response(JSON.stringify(loseReply ? {} : operation), { status: loseReply ? 503 : 200, headers: { 'Content-Type': 'application/json' } });
    }
    return new Response(JSON.stringify(failRead ? {} : snapshot), { status: failRead ? 503 : 200, headers: { 'Content-Type': 'application/json' } });
  });
  store = makeStore(); host = document.createElement('div'); document.body.appendChild(host); root = createRoot(host); await render();
});
afterEach(async () => { await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); }); host.remove(); vi.unstubAllGlobals(); });
it('retains progress after a lost start reply and reconnect without replay', async () => {
  expect(calls).toHaveLength(0);
  await click('Set up connection'); await contains('Connect existing service'); await fill('example.invalid');
  loseReply = true;
  await click('Start setup'); await contains('Setup response was not confirmed'); await contains('running');
  const writes = calls.filter((call) => call.method === 'POST'); expect(writes).toHaveLength(1);
  const mutation = JSON.parse(writes[0].body);
  expect(mutation).toMatchObject({ values: { host: 'example.invalid' }, expectedRevision: 2, expectedCredentialRevision: 3, expectedActionSchemaDigest: 'c'.repeat(64) });
  expect(mutation.operationId).toMatch(/^[a-f0-9]{32}$/); expect(button('Start setup').disabled).toBe(true);
  await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); });
  root = createRoot(host); await render(); await click('Set up connection'); await contains(mutation.operationId);
  expect(button('Start setup').disabled).toBe(true); expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1);
  failRead = true; await click('Reload setup'); await contains('may be stale'); expect(button('Start setup').disabled).toBe(true);
});
it('resumes only the original failed operation without replacement inputs', async () => {
  snapshot.operations = [{ operationId: 'd'.repeat(32), nodeId: 'node', actionId: 'connect', phase: 'failed', code: 'cleanup_setup_refused', correctiveAction: 'Restore the connection.' }];
  await click('Set up connection'); await contains('Restore the connection.'); await click('Resume original setup'); await contains('running');
  const writes = calls.filter((call) => call.method === 'POST'); expect(writes).toHaveLength(1);
  expect(writes[0].path).toBe(`/v1/plugins/plugin/nodes/node/setup-operations/${'d'.repeat(32)}/resume`); expect(writes[0].body).toBe(''); expect(button('Start setup').disabled).toBe(true);
});
it('preserves drafts across changed fences and refuses secret forms', async () => {
  await click('Set up connection'); await contains('Connect existing service'); await fill('draft.invalid'); snapshot = { ...snapshot, revision: 4 };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); }); await contains('Setup prerequisites changed');
  expect(host.querySelector('input')?.value).toBe('draft.invalid'); expect(button('Start setup').disabled).toBe(true);
  snapshot = { ...snapshot, actions: [{ ...snapshot.actions[0], schemaDigest: 'f'.repeat(64), parametersSchema: { type: 'object', properties: { key: { type: 'string', writeOnly: true } } } }] };
  await click('Reload setup'); await contains('requires the plugin’s terminal tool');
  expect(host.querySelector('input')).toBeNull(); expect(button('Start setup').disabled).toBe(true); expect(calls.every((call) => call.method === 'GET')).toBe(true);
});

it('shows owner authorization and submits its reviewed requirement', async () => {
  snapshot.actions = [{ ...snapshot.actions[0], requiresApproval: true, parametersSchema: { type: 'object', properties: {}, additionalProperties: false } }];
  await click('Set up connection'); await contains('also requires owner approval access');
  await click('Start setup'); await contains('running');
  const writes = calls.filter((call) => call.method === 'POST'); expect(writes).toHaveLength(1);
  expect(JSON.parse(writes[0].body)).toMatchObject({ expectedRequiresApproval: true, values: {} });
});
it('requires a new review when setup authorization changes', async () => {
  await click('Set up connection'); await contains('Connect existing service'); await fill('draft.invalid');
  snapshot = { ...snapshot, actions: [{ ...snapshot.actions[0], requiresApproval: true }] };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); }); await contains('Setup prerequisites changed');
  expect(button('Start setup').disabled).toBe(true); expect(calls.every((call) => call.method === 'GET')).toBe(true);
  await click('Reload setup'); await contains('also requires owner approval access');
  expect(button('Start setup').disabled).toBe(false);
});
