/** Paired browser credentials live only in this module, never Redux or browser storage. */
export interface OperatorSessionView {
  mode: 'direct' | 'review' | 'connecting' | 'paired' | 'ended';
  clusterName?: string;
  clusterId?: string;
  fingerprint?: string;
  deviceId?: string;
  expiresAt?: string;
}
interface Invitation { clusterId: string; clusterName: string; fingerprint: string; nonce: string; expiresAt: string; invitationId?: string }
interface Tokens { accessToken: string; refreshToken: string; accessExpires: number; refreshExpires: number; deviceId: string }
const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const encoded = /^[A-Za-z0-9_-]+$/;
const failure = () => new Error('Operator authentication failed. Review a current invitation and pair again.');
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw failure();
  return value as Record<string, unknown>;
}
function string(value: unknown, maximum = 256): string {
  if (typeof value !== 'string' || !value.length || value.length > maximum) throw failure();
  return value;
}
function identifier(value: unknown): string { const result = string(value); if (!uuid.test(result)) throw failure(); return result; }
function timestamp(value: unknown): number { const result = Date.parse(string(value)); if (!Number.isFinite(result)) throw failure(); return result; }
function base64(value: ArrayBuffer): string { return btoa(String.fromCharCode(...new Uint8Array(value))).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, ''); }
function unbase64(value: string): Uint8Array<ArrayBuffer> {
  if (!encoded.test(value) || value.length > 32768) throw failure();
  try { return Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/')), (c) => c.charCodeAt(0)); }
  catch { throw failure(); }
}
async function bounded(stream: ReadableStream<Uint8Array>, maximum: number): Promise<Uint8Array<ArrayBuffer>> {
  const reader = stream.getReader(); const chunks: Uint8Array[] = []; let total = 0;
  try {
    while (true) {
      const next = await reader.read(); if (next.done) break;
      total += next.value.byteLength; if (total > maximum) throw failure(); chunks.push(next.value);
    }
    const result = new Uint8Array(total); let offset = 0;
    for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
    return result;
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
}
async function invitation(code: string, now: number): Promise<Invitation> {
  if (code.length > 32768) throw failure();
  const url = new URL(code);
  if (url.protocol !== 'skulk:' || url.hostname !== 'pair' || url.hash || url.username || url.password || url.pathname) throw failure();
  const compressed = url.searchParams.get('z'); const plain = url.searchParams.get('payload');
  if (url.searchParams.size !== 1 || Boolean(compressed) === Boolean(plain)) throw failure();
  const bytes = unbase64(compressed ?? plain ?? '');
  const decoded = compressed ? await bounded(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate')), 32768) : bytes;
  const raw = object(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(decoded)));
  const compact = compressed !== null;
  const version = raw[compact ? 'v' : 'version'];
  if (version !== 1 && version !== 2 && version !== 3) throw failure();
  const result: Invitation = {
    clusterId: identifier(raw[compact ? 'i' : 'clusterId']),
    clusterName: string(raw[compact ? 'n' : 'clusterName'], 128),
    fingerprint: string(raw[compact ? 'f' : 'clusterFingerprint'], 128),
    nonce: string(raw[compact ? 'o' : 'nonce'], 128),
    expiresAt: string(raw[compact ? 'e' : 'expiresAt']),
    ...(version === 3 ? { invitationId: identifier(raw[compact ? 'd' : 'invitationId']) } : {}),
  };
  if (result.nonce.length < 32 || !encoded.test(result.nonce) || timestamp(result.expiresAt) <= now) throw failure();
  return result;
}
function tokens(value: unknown, deviceId: string, now: number): Tokens {
  const raw = object(value); const accessToken = string(raw.accessToken, 128); const refreshToken = string(raw.refreshToken, 128);
  const accessExpires = timestamp(raw.accessTokenExpiresAt); const refreshExpires = timestamp(raw.refreshTokenExpiresAt);
  if (accessToken.length < 40 || refreshToken.length < 40 || !encoded.test(accessToken) || !encoded.test(refreshToken) || accessExpires <= now || refreshExpires <= accessExpires) throw failure();
  return { accessToken, refreshToken, accessExpires, refreshExpires, deviceId };
}

/** Session-bound Ed25519 pairing, serialized refresh, and protected same-origin fetch. */
export class OperatorSession {
  #view: OperatorSessionView = Object.freeze({ mode: 'direct' });
  #invitation?: Invitation;
  #tokens?: Tokens;
  #generation = 0;
  #refresh?: Promise<void>;
  #listeners = new Set<() => void>();
  private readonly origin: () => string;
  private readonly send: typeof fetch;
  private readonly now: () => number;
  constructor(origin: () => string = () => location.origin, send: typeof fetch = (...args) => fetch(...args), now: () => number = Date.now) { this.origin = origin; this.send = send; this.now = now; }
  /** Subscribe only to safe identity/session metadata. */
  subscribe = (listener: () => void): (() => void) => { this.#listeners.add(listener); return () => { this.#listeners.delete(listener); }; };
  /** Stable immutable observation suitable for React's external-store hook. */
  snapshot = (): OperatorSessionView => this.#view;
  #publish(view: OperatorSessionView): void { this.#view = Object.freeze(view); for (const listener of this.#listeners) listener(); }
  #protectedOrigin(): string {
    const url = new URL(this.origin());
    if (url.origin !== this.origin() || (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))) throw new Error('Pair this browser through HTTPS or localhost.');
    return url.origin;
  }
  /** Decode an owner-provided invitation for explicit identity review; no network call. */
  async review(code: string): Promise<void> {
    if (this.#view.mode === 'connecting' || this.#view.mode === 'paired') throw new Error('Disconnect this browser before changing its pairing.');
    this.#protectedOrigin(); const generation = ++this.#generation;
    try {
      const selected = await invitation(code.trim(), this.now());
      if (generation !== this.#generation) return;
      this.#invitation = selected;
      this.#publish({ mode: 'review', clusterId: selected.clusterId, clusterName: selected.clusterName, fingerprint: selected.fingerprint, expiresAt: selected.expiresAt });
    } catch { if (generation === this.#generation) this.disconnect(); throw failure(); }
  }
  async #post(path: string, body: object): Promise<unknown> {
    const response = await this.send(this.#protectedOrigin() + '/v1/auth/' + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      credentials: 'omit', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(10000),
    });
    if (!response.ok || response.redirected || !response.body) throw failure();
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(await bounded(response.body, 32768)));
  }
  /** Sign the existing protocol's exact proof; this does not grant plugin scopes. */
  async pair(deviceName: string): Promise<void> {
    const selected = this.#invitation;
    if (!selected || this.#view.mode !== 'review' || timestamp(selected.expiresAt) <= this.now()) throw failure();
    const name = deviceName.trim().replace(/\s+/g, ' '); if (!name || name.length > 80) throw failure();
    const generation = ++this.#generation; this.#publish({ ...this.#view, mode: 'connecting' });
    try {
      const keys = await crypto.subtle.generateKey('Ed25519', false, ['sign', 'verify']) as CryptoKeyPair;
      const challenge = object(await this.#post('pairing-sessions/challenge', {
        nonce: selected.nonce, deviceName: name, devicePublicKey: base64(await crypto.subtle.exportKey('raw', keys.publicKey)),
        ...(selected.invitationId ? { invitationId: selected.invitationId } : {}),
      }));
      if (generation !== this.#generation) throw failure();
      const proofChallenge = string(challenge.challenge, 128);
      if (!encoded.test(proofChallenge) || timestamp(challenge.expiresAt) <= this.now()) throw failure();
      const attemptId = selected.invitationId ? identifier(challenge.attemptId) : undefined;
      const parts = selected.invitationId ? ['skulk-device-pairing-v2', selected.clusterId, selected.invitationId, selected.nonce, attemptId!, proofChallenge] : ['skulk-device-pairing-v1', selected.clusterId, selected.nonce, proofChallenge];
      const signature = base64(await crypto.subtle.sign('Ed25519', keys.privateKey, new TextEncoder().encode(parts.join('\0'))));
      const result = object(await this.#post('pairing-sessions/exchange', { nonce: selected.nonce, signature, ...(selected.invitationId ? { invitationId: selected.invitationId, attemptId } : {}) }));
      const cluster = object(result.cluster);
      const publicKey = unbase64(string(cluster.publicKey, 64));
      const fingerprint = 'sha256:' + base64(await crypto.subtle.digest('SHA-256', publicKey));
      if (publicKey.length !== 32 || fingerprint !== selected.fingerprint || cluster.clusterId !== selected.clusterId || cluster.fingerprint !== selected.fingerprint || generation !== this.#generation) throw failure();
      this.#tokens = tokens(result, identifier(result.deviceId), this.now()); this.#invitation = undefined;
      this.#publish({ mode: 'paired', clusterName: selected.clusterName, clusterId: selected.clusterId, fingerprint: selected.fingerprint, deviceId: this.#tokens.deviceId, expiresAt: new Date(this.#tokens.accessExpires).toISOString() });
    } catch { if (generation === this.#generation) this.disconnect(); throw failure(); }
  }
  /** Clear credentials and fence late responses; never silently inherit direct owner access. */
  disconnect(): void { this.#generation++; this.#tokens = undefined; this.#invitation = undefined; this.#refresh = undefined; this.#publish({ mode: 'ended' }); }
  /** Explicitly return to the host's independently authorized localhost/Tailscale mode. */
  useDirectAccess(): void { this.disconnect(); this.#publish({ mode: 'direct' }); }
  async #renew(): Promise<void> {
    if (this.#refresh) return this.#refresh;
    const current = this.#tokens; const generation = this.#generation;
    if (!current || current.refreshExpires <= this.now()) { this.disconnect(); throw failure(); }
    const pending = (async () => {
      try {
        const result = await this.#post('token', { deviceId: current.deviceId, refreshToken: current.refreshToken });
        if (generation !== this.#generation) throw failure();
        this.#tokens = tokens(result, current.deviceId, this.now());
        this.#publish({ ...this.#view, expiresAt: new Date(this.#tokens.accessExpires).toISOString() });
      } catch { if (generation === this.#generation) this.disconnect(); throw failure(); }
    })();
    this.#refresh = pending;
    try { await pending; } finally { if (this.#refresh === pending) this.#refresh = undefined; }
  }
  /** Inject credentials below query metadata; failed requests are never automatically replayed. */
  fetch: typeof fetch = async (input, init) => {
    const request = new Request(input instanceof Request ? input.clone() : new URL(String(input), this.origin()), init);
    if (new URL(request.url).origin !== this.origin()) throw new Error('Dashboard requests must stay on this gateway.');
    if (this.#view.mode === 'direct') {
      const generation = this.#generation; const response = await this.send(input, init);
      if (generation !== this.#generation) throw failure();
      return response;
    }
    if (this.#view.mode !== 'paired') throw new Error('Pair this browser or explicitly select direct host access.');
    this.#protectedOrigin();
    if (!this.#tokens || this.#tokens.accessExpires - this.now() < 30000) await this.#renew();
    const current = this.#tokens; const generation = this.#generation; if (!current) throw failure();
    const headers = new Headers(request.headers); headers.set('Authorization', 'Bearer ' + current.accessToken);
    const response = await this.send(new Request(request, { headers, credentials: 'omit', redirect: 'error', cache: 'no-store' }));
    if (generation !== this.#generation) throw failure();
    if (response.status === 401 && this.#tokens === current) this.disconnect();
    // A 403 may only mean a missing plugin grant. Keep the pairing; owners grant
    // privileges independently, and refresh must not retry this potentially paid POST.
    return response;
  };
}
export const operatorSession = new OperatorSession();
