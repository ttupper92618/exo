import { beforeEach, expect, it, vi } from 'vitest';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { ThemeProvider } from 'styled-components';
import { darkTheme } from '../theme/theme';
import { OperatorAccessPanel } from '../components/pages/OperatorAccessPanel';
import fixture from './operatorProtocol.fixture.json';
import { OperatorSession, operatorSession } from './operatorSession';
vi.mock('../i18n/tolgee', () => ({ useSkulkTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { value: true, configurable: true });
const origin = 'https://browser-gateway.example';
const deviceId = '44444444-4444-4444-8444-444444444444';
const accessToken = 'a'.repeat(43); const refreshToken = 'r'.repeat(64);
let now: number; let client: OperatorSession;
let calls: Request[]; let publicKey: CryptoKey; let proof: string;
let responseStatus: number; let exchanges: number; let rotations: number;
let exchangeFault: boolean; let holdExchange: Promise<void> | undefined;
function bytes(value: string) { return Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0)); }
function response(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }); }
function tokenPair() { return { accessToken: rotations ? 'b'.repeat(43) : accessToken, refreshToken, accessTokenExpiresAt: new Date(now + 60000).toISOString(), refreshTokenExpiresAt: new Date(now + 3600000).toISOString() }; }
async function server(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const request = input instanceof Request ? input : new Request(input, init);
  calls.push(request.clone()); const path = new URL(request.url).pathname;
  expect(new URL(request.url).origin).toBe(origin);
  if (path.startsWith('/v1/auth/')) {
    expect(request.redirect).toBe('error'); expect(request.cache).toBe('no-store');
    expect(request.headers.get('Authorization')).toBeNull();
    const body = await request.json();
    if (path.endsWith('/challenge')) {
      publicKey = await crypto.subtle.importKey('raw', bytes(body.devicePublicKey), 'Ed25519', false, ['verify']);
      proof = body.invitationId ? fixture.invitationProof : fixture.legacyProof;
      return response({ challenge: fixture.challenge, attemptId: body.invitationId ? fixture.attemptId : undefined, expiresAt: new Date(now + 60000).toISOString() });
    }
    if (path.endsWith('/exchange')) {
      expect(await crypto.subtle.verify('Ed25519', publicKey, bytes(body.signature), bytes(proof))).toBe(true);
      exchanges++; await holdExchange;
      return response({ ...tokenPair(), deviceId, cluster: { clusterId: fixture.clusterId, name: 'Fixture cluster', fingerprint: fixture.fingerprint, publicKey: exchangeFault ? 'f'.repeat(43) : fixture.publicKey } });
    }
    rotations++; expect(body).toEqual({ deviceId, refreshToken }); return response(tokenPair());
  }
  return response({ ok: true }, responseStatus);
}
beforeEach(() => {
  now = Date.parse('2030-01-01T00:00:00Z'); calls = []; rotations = exchanges = 0;
  responseStatus = 200; exchangeFault = false; holdExchange = undefined;
  client = new OperatorSession(() => origin, server, () => now);
});
async function pair(code = fixture.invitation) { await client.review(code); await client.pair('Browser'); }
it.each([fixture.invitation, fixture.legacy])('signs the Python protocol fixture, without persisting credentials', async code => {
  const before = JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } });
  await client.review(code); expect(calls).toHaveLength(0); expect(client.snapshot().mode).toBe('review');
  expect(JSON.stringify(client.snapshot())).not.toContain('nonce'); await client.pair('Browser');
  expect(client.snapshot().deviceId).toBe(deviceId); expect(exchanges).toBe(1);
  await client.fetch(origin + '/v1/plugins'); expect(calls.at(-1)?.headers.get('Authorization')).toBe('Bearer ' + accessToken);
  expect(JSON.stringify(client.snapshot())).not.toContain(accessToken);
  expect(JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } })).toBe(before);
});
it('serializes refresh and never places the bearer in the original request', async () => {
  await pair(); now += 45000;
  const request = new Request(origin + '/v1/plugins', { method: 'POST', body: '{"action":"inspect"}' });
  await Promise.all([client.fetch(request), client.fetch(origin + '/v1/plugins')]);
  expect(rotations).toBe(1); expect(request.headers.get('Authorization')).toBeNull(); expect(request.bodyUsed).toBe(false);
  expect(calls.at(-1)?.headers.get('Authorization')).toBe('Bearer ' + 'b'.repeat(43));
});
it('does not replay a forbidden paid action or discard a valid pairing', async () => {
  await pair(); responseStatus = 403;
  expect((await client.fetch(origin + '/v1/plugins/example/proposals/one/approve', { method: 'POST', body: '{}' })).status).toBe(403);
  expect(calls.filter(c => c.url.endsWith('/approve'))).toHaveLength(1); expect(rotations).toBe(0); expect(client.snapshot().mode).toBe('paired');
});
it('fences revoked access and requires explicit direct access after disconnect', async () => {
  await pair(); responseStatus = 401; await client.fetch(origin + '/v1/plugins');
  expect(client.snapshot().mode).toBe('ended'); const count = calls.length;
  await expect(client.fetch(origin + '/v1/plugins')).rejects.toThrow(); expect(calls).toHaveLength(count);
  client.useDirectAccess(); await client.fetch(origin + '/v1/plugins'); expect(calls.at(-1)?.headers.get('Authorization')).toBeNull();
});
it('does not restore a pairing from an exchange response after disconnect', async () => {
  let release!: () => void; holdExchange = new Promise<void>(resolve => { release = resolve; });
  await client.review(fixture.invitation); const pending = client.pair('Browser');
  while (!exchanges) await new Promise(resolve => setTimeout(resolve, 1));
  client.disconnect(); release(); await expect(pending).rejects.toThrow(); expect(client.snapshot().mode).toBe('ended');
});
it('rejects a forged cluster public key even when its echoed fingerprint matches', async () => {
  exchangeFault = true; await expect(pair()).rejects.toThrow(); expect(client.snapshot().mode).toBe('ended');
});
it('refuses cross-origin requests without forwarding the bearer', async () => {
  await pair(); const count = calls.length;
  await expect(client.fetch('https://foreign.example/v1/plugins')).rejects.toThrow(); expect(calls).toHaveLength(count);
});
it('refuses insecure remote pairing and expired or oversized invitations', async () => {
  client = new OperatorSession(() => 'http://remote.example', server, () => now);
  await expect(client.review(fixture.invitation)).rejects.toThrow(); expect(calls).toHaveLength(0);
  client = new OperatorSession(() => origin, server, () => now); now += 2 * 86400000;
  await expect(client.review(fixture.invitation)).rejects.toThrow();
  await expect(client.review('x'.repeat(32769))).rejects.toThrow(); expect(calls).toHaveLength(0);
});
it('fails closed after a lost rotating refresh response without retrying it', async () => {
  const send: typeof fetch = async (input, init) => {
    if (String(input).endsWith('/token')) { rotations++; throw new TypeError('network unavailable'); }
    return server(input, init);
  };
  client = new OperatorSession(() => origin, send, () => now); await pair(); now += 45000;
  await expect(client.fetch(origin + '/v1/plugins', { method: 'POST', body: '{}' })).rejects.toThrow();
  expect(rotations).toBe(1); expect(client.snapshot().mode).toBe('ended');
  await expect(client.fetch(origin + '/v1/plugins')).rejects.toThrow(); expect(rotations).toBe(1);
  expect(calls.every(c => new URL(c.url).pathname.startsWith('/v1/auth/'))).toBe(true);
});
it('a late rejection of the old token cannot erase a successfully refreshed session', async () => {
  let release!: () => void; let entered!: () => void;
  const waiting = new Promise<void>(resolve => { entered = resolve; });
  const held = new Promise<void>(resolve => { release = resolve; });
  const send: typeof fetch = async (input, init) => {
    if (input instanceof Request && input.url.endsWith('/slow')) { entered(); await held; return response({}, 401); }
    return server(input, init);
  };
  client = new OperatorSession(() => origin, send, () => now); await pair();
  const old = client.fetch(origin + '/slow'); await waiting; now += 45000;
  await client.fetch(origin + '/v1/plugins'); expect(rotations).toBe(1); release();
  expect((await old).status).toBe(401); expect(client.snapshot().mode).toBe('paired');
  await client.fetch(origin + '/v1/plugins'); expect(rotations).toBe(1);
});
it('bounds decompression before parsing an invitation', async () => {
  const stream = new Blob(['x'.repeat(40000)]).stream().pipeThrough(new CompressionStream('deflate'));
  const compressed = await new Response(stream).arrayBuffer();
  const code = btoa(String.fromCharCode(...new Uint8Array(compressed))).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
  await expect(client.review('skulk://pair?z=' + code)).rejects.toThrow(); expect(calls).toHaveLength(0);
});

it('reviews and pairs through the visible panel while clearing the invitation input', async () => {
  vi.spyOn(operatorSession, 'snapshot').mockImplementation(client.snapshot);
  vi.spyOn(operatorSession, 'subscribe').mockImplementation(client.subscribe);
  vi.spyOn(operatorSession, 'review').mockImplementation(code => client.review(code));
  vi.spyOn(operatorSession, 'pair').mockImplementation(name => client.pair(name));
  vi.spyOn(operatorSession, 'disconnect').mockImplementation(() => client.disconnect());
  const host=document.createElement('div'); document.body.appendChild(host); const root=createRoot(host);
  const button=(text: string) => { const result=[...host.querySelectorAll('button')].find(b=>b.textContent===text); if(!result)throw new Error('Missing button '+text);return result; };
  try {
    await act(async()=>{root.render(createElement(ThemeProvider,{theme:darkTheme},createElement(OperatorAccessPanel)));});
    await act(async()=>{button('Browser access').click();});
    const input=host.querySelector('input[type=password]') as HTMLInputElement; input.value=fixture.invitation;
    await act(async()=>{button('Review invitation').click(); await vi.waitFor(()=>expect(host.textContent).toContain(fixture.fingerprint));});
    expect(input.value).toBe(''); expect(calls).toHaveLength(0);
    await act(async()=>{button('Pair this browser').click(); await vi.waitFor(()=>expect(host.textContent).toContain(deviceId));});
    expect(exchanges).toBe(1); expect(host.textContent).not.toContain(accessToken); expect(host.textContent).not.toContain(refreshToken);
    await act(async()=>{button('Disconnect browser').click();}); expect(client.snapshot().mode).toBe('ended');
  } finally { await act(async()=>root.unmount());host.remove();vi.restoreAllMocks(); }
});
