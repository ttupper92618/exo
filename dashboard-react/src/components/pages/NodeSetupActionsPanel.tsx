import { useRef, useState } from 'react';
import styled from 'styled-components';
import { useSkulkTranslation } from '../../i18n/tolgee';
import { useGetNodeSetupActionsQuery, useStartNodeSetupMutation, useResumeNodeSetupMutation, type NodeAddress, type SetupAction, type SetupActions } from '../../store/endpoints/plugins';
import { Button } from '../common/Button';
import { PluginConfigurationFields, supportedConfigurationSchema } from './PluginConfigurationFields';

const Panel = styled.section`overflow-wrap: anywhere; margin-top: 12px;`;
const Form = styled.form`margin: 12px 0; padding: 12px; border: 1px solid ${({ theme }) => theme.colors.border}; border-radius: ${({ theme }) => theme.radii.sm};`;

/** One draft preserves its reviewed fences even while progress polling observes changes. */
function SetupForm({ address, action, snapshot, blocked }: { address: NodeAddress; action: SetupAction; snapshot: SetupActions; blocked: boolean }) {
  const { t } = useSkulkTranslation();
  const [baseline] = useState({ action, snapshot });
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [notice, setNotice] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const sending = useRef(false);
  const [start, status] = useStartNodeSetupMutation();
  const changed = action.requiresApproval !== baseline.action.requiresApproval || snapshot.revision !== baseline.snapshot.revision || snapshot.schemaDigest !== baseline.snapshot.schemaDigest || snapshot.credentialRevision !== baseline.snapshot.credentialRevision || snapshot.credentialSchemaDigest !== baseline.snapshot.credentialSchemaDigest || action.schemaDigest !== baseline.action.schemaDigest;
  const supported = supportedConfigurationSchema(baseline.action.parametersSchema);
  const disabled = blocked || changed || submitted || status.isLoading || !supported;
  const submit = async () => {
    if (sending.current || disabled) return;
    sending.current = true;
    const operationId = crypto.randomUUID().replaceAll('-', '');
    setSubmitted(true);
    setNotice(`${t('plugins.setupSubmitted', 'Setup submitted. Operation')}: ${operationId}`);
    try {
      await start({ ...address, mutation: { operationId, actionId: baseline.action.actionId,
        expectedRevision: baseline.snapshot.revision, expectedSchemaDigest: baseline.snapshot.schemaDigest,
        expectedCredentialRevision: baseline.snapshot.credentialRevision, expectedCredentialSchemaDigest: baseline.snapshot.credentialSchemaDigest,
        expectedActionSchemaDigest: baseline.action.schemaDigest, expectedRequiresApproval: baseline.action.requiresApproval === true, values,
      } }).unwrap();
    } catch {
      setNotice(`${t('plugins.setupUnconfirmed', 'Setup response was not confirmed. Reload setup to inspect retained progress before another action. Operation')}: ${operationId}`);
    } finally { sending.current = false; }
  };
  return <Form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <h4>{baseline.action.title}</h4><p>{baseline.action.description}</p>
    {baseline.action.requiresApproval ? <p>{t('plugins.setupApprovalRequired', 'This setup action also requires owner approval access. It does not approve a paid proposal.')}</p> : null}
    {supported ? <PluginConfigurationFields schema={baseline.action.parametersSchema} values={values} onChange={setValues} disabled={disabled} /> : <p>{t('plugins.setupUnsupported', 'This setup form requires the plugin’s terminal tool.')}</p>}
    {changed ? <p role="status">{t('plugins.setupChanged', 'Setup prerequisites changed. Reload setup before submitting this draft.')}</p> : null}
    <Button type="submit" disabled={disabled}>{t('plugins.startSetup', 'Start setup')}</Button>
    {notice ? <p role="status">{notice}</p> : null}
  </Form>;
}

/** Read server-owned setup forms and progress; reconnect never resubmits work. */
export function NodeSetupActionsPanel(address: NodeAddress) {
  const { t } = useSkulkTranslation();
  const [open, setOpen] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [notice, setNotice] = useState('');
  const [reloading, setReloading] = useState(false);
  const query = useGetNodeSetupActionsQuery(address, { skip: !open, pollingInterval: 2000, skipPollingIfUnfocused: true, refetchOnMountOrArgChange: true });
  const [resume, resumeStatus] = useResumeNodeSetupMutation();
  const resuming = useRef(false);
  const snapshot = query.currentData;
  const pending = snapshot?.operations.some((operation) => operation.phase === 'queued' || operation.phase === 'running') ?? false;
  const stale = Boolean(query.error);
  const reload = async () => {
    setReloading(true);
    try { await query.refetch().unwrap(); setGeneration((value) => value + 1); setNotice(''); }
    catch { setNotice(t('plugins.setupReloadFailed', 'Setup could not be refreshed. Retained observations may be stale.')); }
    finally { setReloading(false); }
  };
  const resumeOperation = async (operationId: string) => {
    if (resuming.current) return;
    resuming.current = true;
    try { await resume({ ...address, operationId }).unwrap(); setNotice(''); }
    catch { setNotice(t('plugins.setupResumeUnconfirmed', 'Resume was not confirmed. Reload setup to inspect the same operation.')); }
    finally { resuming.current = false; }
  };
  return <Panel aria-label={t('plugins.ownerSetup', 'Owner setup')}>
    <Button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? t('plugins.closeSetup', 'Close setup') : t('plugins.openSetup', 'Set up connection')}</Button>
    {open ? <>
      <p>{t('plugins.setupSeparate', 'Setup prepares this node. Preflight, enablement and spending approval are separate actions.')}</p>
      <Button type="button" disabled={query.isFetching || reloading || resumeStatus.isLoading} onClick={() => void reload()}>{t('plugins.reloadSetup', 'Reload setup')}</Button>
      {query.isLoading ? <p>{t('plugins.setupLoading', 'Loading setup…')}</p> : null}
      {stale ? <p role="alert">{t('plugins.setupStale', 'Setup is unavailable. Any retained progress shown below may be stale. Check access and owner health.')}</p> : null}
      {snapshot ? <>
        {snapshot.actions.map((action) => <SetupForm key={`${generation}:${action.actionId}`} address={address} action={action} snapshot={snapshot} blocked={stale || pending || reloading || resumeStatus.isLoading} />)}
        <ul>{snapshot.operations.map((operation) => <li key={operation.operationId}>
          <span>{operation.actionId} · {operation.operationId} · {operation.phase}</span>
          {operation.code ? <p>{operation.code}: {operation.correctiveAction}</p> : null}
          {operation.phase === 'complete' ? <p>{t('plugins.setupComplete', 'Setup complete. Run preflight before enabling.')}</p> : null}
          {operation.phase === 'failed' ? <Button type="button" disabled={stale || pending || reloading || resumeStatus.isLoading} onClick={() => void resumeOperation(operation.operationId)}>{t('plugins.resumeSetup', 'Resume original setup')}</Button> : null}
        </li>)}</ul>
      </> : null}
      {notice ? <p role="status">{notice}</p> : null}
    </> : null}
  </Panel>;
}
