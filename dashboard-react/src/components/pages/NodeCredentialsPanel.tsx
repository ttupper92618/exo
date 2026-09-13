import { useRef, useState } from 'react';
import styled from 'styled-components';
import { operatorSession } from '../../auth/operatorSession';
import { useSkulkTranslation } from '../../i18n/tolgee';
import { useGetNodeCredentialsQuery, type NodeAddress, type NodeCredentials } from '../../store/endpoints/plugins';
import { Button } from '../common/Button';

const Field = styled.fieldset`
  min-width: 0; margin: 12px 0; padding: 14px; border: 1px solid ${({ theme }) => theme.colors.border};
  label { display: grid; gap: 8px; }
  input, textarea { width: 100%; min-width: 0; box-sizing: border-box; padding: 9px; border-radius: ${({ theme }) => theme.radii.sm};
    border: 1px solid ${({ theme }) => theme.colors.border}; background: ${({ theme }) => theme.colors.surface}; color: ${({ theme }) => theme.colors.text}; }
  textarea { resize: vertical; }
`;
const Actions = styled.div`display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px;`;

/** Keep drafts outside state/cache and require a fresh review after concurrent or uncertain writes. */
function CredentialEditor({ address, current, reload, unavailable }: { address: NodeAddress; current: NodeCredentials; reload: () => Promise<NodeCredentials>; unavailable: boolean }) {
  const { t } = useSkulkTranslation();
  const [baseline, setBaseline] = useState(current);
  const inputs = useRef<Record<string, HTMLInputElement | HTMLTextAreaElement | null>>({});
  const [multiline, setMultiline] = useState<ReadonlySet<string>>(() => new Set());
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [notice, setNotice] = useState('');
  const changed = baseline.revision !== current.revision || baseline.schemaDigest !== current.schemaDigest;
  const clearInputs = () => { for (const input of Object.values(inputs.current)) if (input) input.value = ''; };
  const switchInput = (credentialId: string) => {
    // Password inputs sanitize away newlines. Keep multiline bytes in an
    // uncontrolled textarea, clearing the old draft instead of copying secrets
    // through React state when the owner changes the input mode.
    const input = inputs.current[credentialId];
    if (input) input.value = '';
    setMultiline((previous) => {
      const next = new Set(previous);
      if (next.has(credentialId)) next.delete(credentialId); else next.add(credentialId);
      return next;
    });
  };
  const refresh = async () => {
    setBusy(true);
    try {
      const fresh = await reload();
      clearInputs();
      setBaseline(fresh);
      setUncertain(false);
      setNotice(t('plugins.credentialsRefreshed', 'Credential status refreshed. Enter a new value only if another replacement is needed.'));
    } catch { setNotice(t('plugins.credentialsUnavailable', 'Credential status is unavailable. No change has been repeated.')); }
    finally { setBusy(false); }
  };
  const change = async (credentialId: string, operation: 'replace' | 'retire') => {
    if (busy || changed || uncertain || unavailable) return;
    const input = inputs.current[credentialId];
    let value = operation === 'replace' ? input?.value ?? '' : '';
    if (operation === 'replace' && !value) { setNotice(t('plugins.credentialValueRequired', 'Enter a credential before replacing it.')); return; }
    if (input) input.value = '';
    setBusy(true);
    setNotice('');
    try {
      // The secret-bearing body bypasses Redux actions and query caches. Only
      // safe metadata is fetched after a confirmed or unconfirmed write.
      const body = JSON.stringify({ operation, operationId: crypto.randomUUID().replaceAll('-', ''), credentialId,
        expectedRevision: baseline.revision, expectedSchemaDigest: baseline.schemaDigest,
        ...(operation === 'replace' ? { value } : {}),
      });
      value = '';
      const response = await operatorSession.fetch(`/v1/plugins/${encodeURIComponent(address.pluginId)}/nodes/${encodeURIComponent(address.nodeId)}/credentials`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Skulk-Dashboard': 'pairing-v1' },
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', body, signal: AbortSignal.timeout(35000),
      });
      if (!response.ok) throw new Error('credential update unconfirmed');
      const fresh = await reload();
      clearInputs();
      setBaseline(fresh);
      setNotice(t('plugins.credentialUpdated', 'Credential metadata updated. Stored values remain private.'));
    } catch {
      setUncertain(true);
      setNotice(t('plugins.credentialUnconfirmed', 'The credential update was not confirmed. Refresh status before another action; the request has not been repeated.'));
      void reload().catch(() => undefined);
    } finally { value = ''; setBusy(false); }
  };
  return <section>
    {changed ? <p role="status">{t('plugins.credentialsChanged', 'Credentials changed elsewhere. Refresh status before replacing a value.')}</p> : null}
    {baseline.credentials.map((credential) => <Field key={credential.credentialId} disabled={busy || changed || uncertain || unavailable}>
      <legend>{credential.title}</legend>
      <p>{credential.description}</p>
      <p>{credential.required ? t('plugins.credentialRequired', 'Required') : t('plugins.credentialOptional', 'Optional')} · {current.credentials.find((item) => item.credentialId === credential.credentialId)?.ready ? t('plugins.credentialReady', 'Ready') : t('plugins.credentialNotReady', 'Not ready')}</p>
      <Button type="button" onClick={() => switchInput(credential.credentialId)}>{multiline.has(credential.credentialId) ? t('plugins.singleLineCredential', 'Use single-line input') : t('plugins.multilineCredential', 'Use multiline input')}</Button>
      {multiline.has(credential.credentialId) ? <p>{t('plugins.multilineCredentialVisible', 'Multiline credentials are visible while editing. Switching input mode clears the draft.')}</p> : null}
      <label>{t('plugins.replacementCredential', 'Replacement value')}{multiline.has(credential.credentialId)
        ? <textarea ref={(element) => { inputs.current[credential.credentialId] = element; }} rows={4} autoComplete="off" autoCapitalize="off" spellCheck={false} maxLength={4096} />
        : <input ref={(element) => { inputs.current[credential.credentialId] = element; }} type="password" autoComplete="new-password" maxLength={4096} />}</label>
      <Actions>
        <Button type="button" onClick={() => void change(credential.credentialId, 'replace')}>{t('plugins.replaceCredential', 'Replace credential')}</Button>
        <Button type="button" onClick={() => void change(credential.credentialId, 'retire')}>{t('plugins.retireCredential', 'Retire future use')}</Button>
      </Actions>
    </Field>)}
    <Button type="button" disabled={busy} onClick={() => void refresh()}>{t('plugins.refreshCredentials', 'Refresh credential status')}</Button>
    {notice ? <p role="status">{notice}</p> : null}
    <p>{t('plugins.credentialCleanupHistory', 'Retirement blocks future credential use. Historical versions needed by cleanup remain protected. Credential changes do not approve spending.')}</p>
  </section>;
}

/** Render only plugin-declared credential inputs, independently of ordinary configuration. */
export function NodeCredentialsPanel(address: NodeAddress) {
  const { t } = useSkulkTranslation();
  const query = useGetNodeCredentialsQuery(address, { refetchOnMountOrArgChange: true });
  return <section aria-label={t('plugins.nodeCredentials', 'Node credentials')}>
    {query.error ? <p role="status">{t('plugins.credentialsUnavailable', 'Credential status is unavailable. No change has been repeated.')}</p> : null}
    {query.currentData ? <CredentialEditor address={address} current={query.currentData} unavailable={!!query.error} reload={() => query.refetch().unwrap()} /> : <Button type="button" disabled={query.isFetching} onClick={() => void query.refetch()}>{t('plugins.refreshCredentials', 'Refresh credential status')}</Button>}
  </section>;
}
