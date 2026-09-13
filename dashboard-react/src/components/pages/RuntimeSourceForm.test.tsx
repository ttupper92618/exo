import { configureStore } from '@reduxjs/toolkit';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { ThemeProvider } from 'styled-components';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { apiSlice } from '../../store/api';
import { darkTheme } from '../../theme/theme';
import { RuntimeSourceForm } from './RuntimeSourceForm';

Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { value: true, configurable: true });
vi.mock('../../i18n/tolgee', () => ({ useSkulkTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
let root: Root;
let host: HTMLDivElement;
let store: ReturnType<typeof makeStore>;
let source: { revision: number; configured: boolean; credential_reference: string | null; credential_ready: boolean; trust_revision: number | null };
let writes: Record<string, unknown>[];
let lostResponse: boolean;
let saved: ReturnType<typeof vi.fn<() => void>>;
function makeStore() { return configureStore({ reducer: { [apiSlice.reducerPath]: apiSlice.reducer }, middleware: (defaults) => defaults().concat(apiSlice.middleware) }); }
function response(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
function input(label: string) {
  const field = [...host.querySelectorAll('label')].find((item) => item.textContent?.trim() === label)?.querySelector('input');
  if (!field) throw new Error('Missing input: ' + label);
  return field;
}
async function fill(label: string, value: string) {
  await act(async () => {
    const field = input(label);
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(field, value);
    field.dispatchEvent(new Event('input', { bubbles: true }));
  });
}
async function submit() { await act(async () => { host.querySelector('form')?.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); }); }
async function contains(text: string) { await act(async () => { await vi.waitFor(() => expect(host.textContent).toContain(text)); }); }
beforeEach(async () => {
  source = { revision: 0, configured: false, credential_reference: null, credential_ready: false, trust_revision: null };
  writes = [];
  lostResponse = false;
  saved = vi.fn();
  vi.stubGlobal('fetch', async (requestInput: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(new URL(String(requestInput instanceof Request ? requestInput.url : requestInput), location.href), requestInput instanceof Request ? requestInput : init);
    if (request.method === 'POST') {
      expect(input('Feed credential').value).toBe('');
      expect(request.headers.get('X-Skulk-Dashboard')).toBe('pairing-v1');
      writes.push(await request.json() as Record<string, unknown>);
      source = { ...source, revision: source.revision + 1, configured: true, credential_ready: true, trust_revision: 1 };
      return response(source, lostResponse ? 503 : 200);
    }
    return response(source);
  });
  store = makeStore();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root.render(<Provider store={store}><ThemeProvider theme={darkTheme}><RuntimeSourceForm pluginId="managed.fixture" onSaved={saved} /></ThemeProvider></Provider>); });
  await contains('No release source');
});
afterEach(async () => {
  await act(async () => { root.unmount(); store.dispatch(apiSlice.util.resetApiState()); });
  host.remove();
  vi.unstubAllGlobals();
});

it('requires explicit publisher trust and excludes the write-only credential from Redux', async () => {
  await fill('HTTPS release directory', 'https://releases.example.test/private/');
  await fill('Publisher identity', 'example.publisher');
  await fill('Ed25519 public key (64 hexadecimal characters)', 'a'.repeat(64));
  await fill('Publisher trust expires', '2099-01-01T12:00');
  await fill('Feed credential', 'private-fixture-token');
  await submit();
  expect(writes).toHaveLength(0);
  await contains('confirm that you trust this key');
  await act(async () => { input('I trust this publisher key to authorize plugin releases until the expiry shown.').click(); });
  await submit();
  await act(async () => { await vi.waitFor(() => expect(saved).toHaveBeenCalledOnce()); });
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({ expected_revision: 0, base_url: 'https://releases.example.test/private/', metadata_filename: 'runtime.json', token: 'private-fixture-token', trust: { revision: 1, publishers: { 'example.publisher': 'a'.repeat(64) } } });
  expect(JSON.stringify(store.getState())).not.toContain('private-fixture-token');
  expect(host.textContent).not.toContain('private-fixture-token');
});

it('rotates only the credential and observes an unconfirmed save without replay', async () => {
  source = { revision: 4, configured: true, credential_reference: 'b'.repeat(32), credential_ready: false, trust_revision: 3 };
  await act(async () => { store.dispatch(apiSlice.util.invalidateTags(['Plugins'])); });
  await contains('Feed credential is unavailable');
  await act(async () => { [...host.querySelectorAll('button')].find((item) => item.textContent === 'Refresh source status')?.click(); });
  await contains('Source status refreshed');
  await fill('Feed credential', 'replacement-fixture-secret');
  lostResponse = true;
  await submit();
  await contains('Source update was not confirmed');
  await contains('Feed credential is ready');
  expect(writes).toEqual([{ expected_revision: 4, token: 'replacement-fixture-secret', clear_token: false }]);
  expect(saved).not.toHaveBeenCalled();
  expect(JSON.stringify(store.getState())).not.toContain('replacement-fixture-secret');
  expect(input('Feed credential').value).toBe('');
  await contains('Source settings changed');
  await submit();
  expect(writes).toHaveLength(1);
});

it('withdraws consent when the entered publisher key changes', async () => {
  await fill('Publisher identity', 'example.publisher');
  await fill('Ed25519 public key (64 hexadecimal characters)', 'a'.repeat(64));
  await fill('Publisher trust expires', '2099-01-01T12:00');
  await act(async () => { input('I trust this publisher key to authorize plugin releases until the expiry shown.').click(); });
  await fill('Ed25519 public key (64 hexadecimal characters)', 'b'.repeat(64));
  expect(input('I trust this publisher key to authorize plugin releases until the expiry shown.').checked).toBe(false);
  await submit();
  expect(writes).toHaveLength(0);
});
