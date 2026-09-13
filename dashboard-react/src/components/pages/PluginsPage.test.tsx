import { configureStore } from '@reduxjs/toolkit';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { ThemeProvider } from 'styled-components';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiSlice } from '../../store/api';
import type { NodeConfiguration } from '../../store/endpoints/plugins';
import { darkTheme } from '../../theme/theme';
import { PluginsPage } from './PluginsPage';

Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { value: true, configurable: true });
vi.mock('../../i18n/tolgee', () => ({ useSkulkTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));

let root: Root;
let host: HTMLDivElement;
let configuration: NodeConfiguration;
let failRead: boolean;
let reads: number;
let mutations: Record<string, unknown>[];
let store: ReturnType<typeof makeStore>;

function makeStore() {
  return configureStore({ reducer: { [apiSlice.reducerPath]: apiSlice.reducer }, middleware: (defaults) => defaults().concat(apiSlice.middleware) });
}
function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}
function button(label: string) {
  const element = [...host.querySelectorAll('button')].find((candidate) => candidate.textContent === label);
  if (!element) throw new Error(`Missing button: ${label}`);
  return element;
}
async function click(label: string) {
  await act(async () => { button(label).click(); });
}
async function choose(value: string) {
  await act(async () => {
    const select = host.querySelector('select');
    if (!select) throw new Error('Missing region field');
    select.value = JSON.stringify(value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}
async function ready() {
  await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain('test.bundle')); });
  await click('Configure');
  await act(async () => { await vi.waitFor(() => expect(host.querySelector('select')).not.toBeNull()); });
}

beforeEach(async () => {
  configuration = { nodeId: 'node-1', revision: 0, schemaDigest: 'a'.repeat(64), enabled: false,
    configurationSchema: { type: 'object', required: ['region'], properties: { region: { type: 'string', title: 'Region', enum: ['east', 'west', 'north'] } } },
    values: { region: 'east' } };
  failRead = false;
  reads = 0;
  mutations = [];
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    expect(request.headers.get('X-Skulk-Dashboard')).toBe('pairing-v1');
    if (new URL(request.url).pathname === '/v1/plugins/managed') return response({ installations: [] });
    if (new URL(request.url).pathname === '/v1/plugins') return response([{ pluginId: 'bridge', available: true, nodes: [
      { nodeId: 'node-1', bundleId: 'test.bundle', version: '1.0.0', status: 'disabled', configurable: true },
    ] }]);
    if (request.method === 'GET') {
      reads += 1;
      return failRead ? response({}, 503) : response(configuration);
    }
    const mutation = await request.json() as Record<string, unknown>;
    mutations.push(mutation);
    if (mutation.expectedRevision !== configuration.revision) return response({}, 409);
    if (mutation.operation !== 'validate') configuration = { ...configuration, revision: configuration.revision + 1, values: mutation.values as Record<string, unknown> ?? configuration.values };
    return response({ configuration, validated: true });
  });
  store = makeStore();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root.render(<Provider store={store}><ThemeProvider theme={darkTheme}><PluginsPage /></ThemeProvider></Provider>); });
});
afterEach(async () => {
  await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); });
  host.remove();
  vi.unstubAllGlobals();
});

it('loads disabled-node settings on demand and fences writes by revision and schema', async () => {
  await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain('test.bundle')); });
  expect(reads).toBe(0);
  await ready();
  await choose('west');
  expect(button('Enable').disabled).toBe(true);
  await click('Save settings');
  await act(async () => { await vi.waitFor(() => expect(configuration.values.region).toBe('west')); });
  expect(mutations).toEqual([{ operation: 'edit', values: { region: 'west' }, expectedRevision: 0, expectedSchemaDigest: 'a'.repeat(64) }]);
});

it('preserves a rejected draft and fetches a fresh revision before subsequent edits', async () => {
  await ready();
  await choose('west');
  configuration = { ...configuration, revision: 1, values: { region: 'north' } };
  await click('Save settings');
  await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain('The change was refused')); });
  expect(host.querySelector('select')?.value).toBe('"west"');
  const previousReads = reads;
  await click('Reload settings');
  await act(async () => { await vi.waitFor(() => expect(host.querySelector('select')?.value).toBe('"north"')); });
  expect(reads).toBeGreaterThan(previousReads);
  await choose('east');
  await click('Save settings');
  await act(async () => { await vi.waitFor(() => expect(mutations.at(-1)?.expectedRevision).toBe(1)); });
});

it('preserves unsaved values when reloading fails', async () => {
  await ready();
  await choose('west');
  failRead = true;
  await click('Reload settings');
  await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain('Your draft is preserved')); });
  expect(host.querySelector('select')?.value).toBe('"west"');
  expect(mutations).toEqual([]);
});
