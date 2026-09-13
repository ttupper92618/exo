import { useEffect, useRef, useState } from 'react';
import styled from 'styled-components';
import { operatorSession } from '../../auth/operatorSession';
import { useSkulkTranslation } from '../../i18n/tolgee';
import { apiSlice } from '../../store/api';
import { useAppDispatch } from '../../store/hooks';
import { useGetRuntimeSourceStatusQuery } from '../../store/endpoints/plugins';
import { Button } from '../common/Button';

const Fields = styled.fieldset`
  display: grid; gap: 12px; min-width: 0; margin: 12px 0; padding: 16px;
  border: 1px solid ${({ theme }) => theme.colors.border};
  label { display: grid; gap: 5px; }
  input:not([type=checkbox]) {
    box-sizing: border-box; min-width: 0; width: 100%; padding: 9px;
    border-radius: ${({ theme }) => theme.radii.sm}; border: 1px solid ${({ theme }) => theme.colors.border};
    background: ${({ theme }) => theme.colors.surface}; color: ${({ theme }) => theme.colors.text};
  }
`;

/** Configure an owner-trusted source; credentials stay outside Redux and browser persistence. */
export function RuntimeSourceForm({ pluginId, onSaved }: { pluginId: string; onSaved: () => void }) {
  const { t } = useSkulkTranslation();
  const dispatch = useAppDispatch();
  const source = useGetRuntimeSourceStatusQuery(pluginId, { refetchOnMountOrArgChange: true });
  const tokenInput = useRef<HTMLInputElement>(null);
  const [observedRevision, setObservedRevision] = useState<number | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [metadata, setMetadata] = useState('');
  const [publisher, setPublisher] = useState('');
  const [publicKey, setPublicKey] = useState('');
  const [expiry, setExpiry] = useState('');
  const [trustAccepted, setTrustAccepted] = useState(false);
  const [clearToken, setClearToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const status = source.currentData;
  useEffect(() => { if (status) setObservedRevision((previous) => previous ?? status.revision); }, [status]);
  const changed = !!status && observedRevision !== status.revision;
  const changesTrust = !!publisher || !!publicKey || !!expiry || !status?.trust_revision;
  const save = async () => {
    if (!status || source.error || busy || changed) return;
    const expiresAt = Math.floor(new Date(expiry).getTime() / 1000);
    if (changesTrust && (!trustAccepted || !publisher || !/^[0-9a-f]{64}$/.test(publicKey) || !Number.isFinite(expiresAt) || expiresAt <= Date.now() / 1000)) {
      setNotice(t('plugins.sourceTrustIncomplete', 'Enter the publisher identity, public key and future expiry, then confirm that you trust this key.'));
      return;
    }
    setBusy(true);
    setNotice('');
    // A write-only credential must not enter RTK mutation arguments, action
    // history or cached errors. Clear the DOM before starting the request.
    let token = tokenInput.current?.value ?? '';
    if (tokenInput.current) tokenInput.current.value = '';
    try {
      const body = JSON.stringify({
        expected_revision: status.revision,
        ...(baseUrl ? { base_url: baseUrl } : {}),
        ...(metadata ? { metadata_filename: metadata } : !status.configured ? { metadata_filename: 'runtime.json' } : {}),
        ...(changesTrust ? { trust: { revision: (status.trust_revision ?? 0) + 1, expires_at: expiresAt, publishers: { [publisher]: publicKey } } } : {}),
        ...(token ? { token } : {}),
        clear_token: clearToken,
      });
      token = '';
      const response = await operatorSession.fetch(`/v1/plugins/managed/installations/${encodeURIComponent(pluginId)}/source`, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: { 'Content-Type': 'application/json', 'X-Skulk-Dashboard': 'pairing-v1' },
        body, signal: AbortSignal.timeout(35000),
      });
      if (!response.ok) throw new Error('source update unconfirmed');
      dispatch(apiSlice.util.invalidateTags(['Plugins']));
      onSaved();
    } catch {
      setNotice(t('plugins.sourceSaveUnconfirmed', 'Source update was not confirmed. Refresh status before another save. Direct owner access is required; enter the credential again if it still needs replacement.'));
      // Refreshing observes a possibly accepted change; it never repeats the
      // secret-bearing request after a browser disconnect or timeout.
      void source.refetch();
    } finally {
      token = '';
      setBusy(false);
    }
  };
  const refresh = async () => {
    try {
      const current = await source.refetch().unwrap();
      setObservedRevision(current.revision);
      setTrustAccepted(false);
      setNotice(t('plugins.sourceStatusRefreshed', 'Source status refreshed. Review your entries before saving.'));
    } catch { setNotice(t('plugins.sourceReadFailed', 'Release source status is unavailable. Refresh before saving.')); }
  };
  return <form autoComplete="off" onSubmit={(event) => { event.preventDefault(); void save(); }}>
    <h4>{t('plugins.configureReleaseSource', 'Configure release source')}</h4>
    <p>{t('plugins.sourceOwnerOnly', 'Use a direct owner connection to this host. Obtain the release directory and publisher public key from your trusted private publisher.')}</p>
    {source.error ? <p role="status">{t('plugins.sourceReadFailed', 'Release source status is unavailable. Refresh before saving.')}</p> : null}
    {changed ? <p role="status">{t('plugins.sourceChanged', 'Source settings changed. Refresh status and review your entries before saving.')}</p> : null}
    {status ? <p>{status.configured ? t('plugins.sourceConfigured', 'Source configured. Leave fields blank to retain their current settings.') : t('plugins.sourceNotConfigured', 'No release source is configured.')}{' '}{status.credential_reference ? (status.credential_ready ? t('plugins.sourceCredentialReady', 'Feed credential is ready.') : t('plugins.sourceCredentialMissing', 'Feed credential is unavailable.')) : ''}</p> : null}
    <Fields disabled={busy || source.isFetching || !!source.error || !status || changed}>
      <legend>{t('plugins.sourceSettings', 'Release source settings')}</legend>
      <label>{t('plugins.sourceDirectory', 'HTTPS release directory')}<input type="url" value={baseUrl} maxLength={2048} required={!status?.configured} placeholder="https://releases.example.com/private/" onChange={(event) => setBaseUrl(event.target.value)} /></label>
      <label>{t('plugins.sourceMetadata', 'Signed metadata filename')}<input value={metadata} maxLength={206} placeholder={status?.configured ? '' : 'runtime.json'} onChange={(event) => setMetadata(event.target.value)} /></label>
      <label>{t('plugins.sourceToken', 'Feed credential')}<input ref={tokenInput} type="password" autoComplete="new-password" maxLength={8192} disabled={clearToken} /></label>
      <label><span><input type="checkbox" checked={clearToken} onChange={(event) => { setClearToken(event.target.checked); if (tokenInput.current) tokenInput.current.value = ''; }} /> {t('plugins.sourceAnonymous', 'Use this source without a credential')}</span></label>
      <label>{t('plugins.sourcePublisher', 'Publisher identity')}<input value={publisher} onChange={(event) => { setPublisher(event.target.value); setTrustAccepted(false); }} /></label>
      <label>{t('plugins.sourcePublicKey', 'Ed25519 public key (64 hexadecimal characters)')}<input value={publicKey} maxLength={64} spellCheck={false} onChange={(event) => { setPublicKey(event.target.value); setTrustAccepted(false); }} /></label>
      <label>{t('plugins.sourceTrustExpiry', 'Publisher trust expires')}<input type="datetime-local" value={expiry} onChange={(event) => { setExpiry(event.target.value); setTrustAccepted(false); }} /></label>
      <p>{t('plugins.sourceTrustReplacement', 'Entering publisher details replaces the trusted publisher list with this key. Existing publisher and artifact revocations remain in force.')}</p>
      <label><span><input type="checkbox" checked={trustAccepted} onChange={(event) => setTrustAccepted(event.target.checked)} /> {t('plugins.sourceTrustAccept', 'I trust this publisher key to authorize plugin releases until the expiry shown.')}</span></label>
      <Button type="submit">{t('plugins.saveReleaseSource', 'Save release source')}</Button>
    </Fields>
    <Button type="button" disabled={busy || source.isFetching} onClick={() => void refresh()}>{t('plugins.refreshSource', 'Refresh source status')}</Button>
    {notice ? <p role="status">{notice}</p> : null}
  </form>;
}
