import { useRef, useState, useSyncExternalStore } from 'react';
import styled from 'styled-components';
import { operatorSession } from '../../auth/operatorSession';
import { useSkulkTranslation } from '../../i18n/tolgee';
import { Button } from '../common/Button';
const Panel = styled.section`
  padding: 16px; border: 1px solid ${({ theme }) => theme.colors.border}; border-radius: ${({ theme }) => theme.radii.md}; overflow-wrap: anywhere;
  label { display: grid; gap: 6px; margin: 10px 0; }
  input { padding: 8px; min-width: 0; color: ${({ theme }) => theme.colors.text}; background: ${({ theme }) => theme.colors.surface}; border: 1px solid ${({ theme }) => theme.colors.border}; }
  button { margin: 6px 8px 0 0; }
`;
/** Review the cluster and explicitly pair this tab without persisting credentials. */
export function OperatorAccessPanel() {
  const { t } = useSkulkTranslation();
  const view = useSyncExternalStore(operatorSession.subscribe, operatorSession.snapshot);
  const invitation = useRef<HTMLInputElement>(null);
  const [name, setName] = useState('Dashboard browser');
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const working = useRef(false);
  const [notice, setNotice] = useState('');
  const review = async () => {
    if (working.current) return; working.current = true;
    let code = invitation.current?.value ?? ''; if (invitation.current) invitation.current.value = '';
    setBusy(true); setNotice('');
    try { const pending = operatorSession.review(code); code = ''; await pending; }
    catch { setNotice(t('operator.invalidInvitation', 'Review failed. Use a current invitation on the intended gateway through HTTPS or localhost.')); }
    finally { code = ''; working.current = false; setBusy(false); }
  };
  const pair = async () => {
    if (working.current) return; working.current = true; setBusy(true); setNotice('');
    try { await operatorSession.pair(name); }
    catch { setNotice(t('operator.pairingFailed', 'Pairing was not confirmed. Review a current invitation before retrying.')); }
    finally { working.current = false; setBusy(false); }
  };
  return <Panel aria-label={t('operator.browserAccess', 'Browser access')}>
    <Button type="button" onClick={() => setOpen(!open)}>{t('operator.browserAccess', 'Browser access')}</Button>
    <span>{view.mode === 'paired' ? `${view.clusterName} · ${view.deviceId}` : view.mode === 'direct' ? t('operator.direct', 'Direct host access') : t('operator.unpaired', 'Browser is not paired')}</span>
    {open ? <>
      <p>{t('operator.sessionOnly', 'Pair this tab with the gateway serving this page. Credentials stay in memory and disappear on reload or disconnect. Plugin privileges must be granted separately by the owner.')}</p>
      {view.mode !== 'paired' && view.mode !== 'connecting' ? <>
        <label>{t('operator.invitation', 'Owner invitation')}<input type="password" autoComplete="off" maxLength={32768} ref={invitation} disabled={busy} /></label>
        <Button type="button" disabled={busy} onClick={() => void review()}>{t('operator.reviewInvitation', 'Review invitation')}</Button>
      </> : null}
      {view.mode === 'review' ? <>
        <p>{view.clusterName} · {view.clusterId}</p><p>{t('operator.fingerprint', 'Cluster fingerprint')}: {view.fingerprint}</p>
        <p>{t('operator.gateway', 'Gateway')}: {location.origin}</p>
        <label>{t('operator.deviceName', 'Browser name')}<input value={name} maxLength={80} onChange={(event) => setName(event.target.value)} disabled={busy} /></label>
        <Button type="button" disabled={busy} onClick={() => void pair()}>{t('operator.confirmPair', 'Pair this browser')}</Button>
      </> : null}
      {view.mode !== 'direct' ? <>
        <Button type="button" onClick={() => { operatorSession.disconnect(); setNotice(''); }}>{t('operator.disconnect', 'Disconnect browser')}</Button>
        <Button type="button" disabled={busy} onClick={() => { operatorSession.useDirectAccess(); setNotice(''); }}>{t('operator.useDirect', 'Use direct host access')}</Button>
      </> : null}
      {view.mode === 'paired' ? <p>{t('operator.deviceGrant', 'Give the displayed device ID to the owner to grant plugins:read, plugins:manage or plugins:approve. This browser cannot grant itself access.')}</p> : null}
      {notice ? <p role="alert">{notice}</p> : null}
    </> : null}
  </Panel>;
}
