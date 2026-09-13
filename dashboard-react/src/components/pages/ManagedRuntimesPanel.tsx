import { useState } from 'react';
import styled from 'styled-components';
import { useSkulkTranslation } from '../../i18n/tolgee';
import {
  useGetManagedRuntimesQuery, useGetManagedOperationQuery,
  useWithdrawManagedRuntimeMutation, useRecoverManagedOperationMutation,
  useRegisterManagedRuntimeMutation,
  type ManagedRuntime,
} from '../../store/endpoints/plugins';
import { Button } from '../common/Button';
import { RuntimeReleasePanel } from './RuntimeReleasePanel';
import { RuntimeSourceForm } from './RuntimeSourceForm';

const RuntimeCard = styled.article`
  margin: 12px 0; padding: 16px; border: 1px solid ${({ theme }) => theme.colors.border};
  border-radius: ${({ theme }) => theme.radii.md}; background: ${({ theme }) => theme.colors.surface};
  overflow-wrap: anywhere;
`;
const Actions = styled.div`display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px;`;

/** Read server-retained operation references; reconnect never submits another mutation. */
function RuntimeControls({ runtime, unavailable }: { runtime: ManagedRuntime; unavailable: boolean }) {
  const { t } = useSkulkTranslation();
  const [notice, setNotice] = useState('');
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [releaseOpen, setReleaseOpen] = useState(false);
  const [withdraw, withdrawing] = useWithdrawManagedRuntimeMutation();
  const [recover, recovering] = useRecoverManagedOperationMutation();
  const operationId = submitted ?? runtime.operation_id;
  const operation = useGetManagedOperationQuery({ pluginId: runtime.plugin_id, operationId: operationId ?? '' }, {
    skip: !operationId,
    pollingInterval: 2000,
    skipPollingIfUnfocused: true,
  });
  const state = operation.currentData?.state ?? (operationId === runtime.operation_id ? runtime.operation_state : null);
  const stateLabel = state ? {
    accepted: t('plugins.operationAccepted', 'Accepted'),
    applying: t('plugins.operationApplying', 'Applying'),
    complete: t('plugins.operationComplete', 'Complete'),
    failed: t('plugins.operationFailed', 'Failed'),
    recovery_required: t('plugins.operationRecovery', 'Recovery needed'),
    superseded: t('plugins.operationSuperseded', 'Withdrawn by a later operation'),
  }[state] : t('plugins.runtimeStatusUnknown', 'Status unavailable');
  const pending = state === 'accepted' || state === 'applying';
  const withdrawable = state === 'recovery_required' && !!operation.currentData && !['disable', 'uninstall'].includes(operation.currentData.request.action);
  const confirmed = state === 'complete' || state === 'failed' || state === 'superseded' || withdrawable;
  const busy = withdrawing.isLoading || recovering.isLoading;
  const withdrawRuntime = async (action: 'disable' | 'uninstall') => {
    const id = crypto.randomUUID().replaceAll('-', '');
    setSubmitted(id);
    setNotice('');
    try {
      await withdraw({ action, pluginId: runtime.plugin_id, operationId: id, expectedRevision: runtime.selection_revision }).unwrap();
    } catch {
      setNotice(t('plugins.runtimeUncertain', 'The request did not return a confirmed result. Refresh operation status before taking another action.'));
    }
  };
  const recoverOperation = async () => {
    if (!operationId) return;
    setNotice('');
    try {
      await recover({ pluginId: runtime.plugin_id, operationId }).unwrap();
    } catch {
      setNotice(t('plugins.runtimeRecoveryFailed', 'Recovery was refused. Check service health and your plugin permissions.'));
    }
  };
  return <RuntimeCard aria-label={runtime.plugin_id}>
    <h3>{runtime.plugin_id}</h3>
    <p>{runtime.uninstalled ? t('plugins.runtimeUninstalled', 'Plugin uninstalled; cleanup state retained') : runtime.enabled ? t('plugins.runtimeEnabled', 'Runtime enabled') : t('plugins.runtimeDisabled', 'Runtime disabled')}</p>
    <p>{t('plugins.selectedRelease', 'Selected release')}: {runtime.selected_digest?.slice(0, 12) ?? t('plugins.noRelease', 'None selected')}</p>
    <p>{t('plugins.activeRelease', 'Active release')}: {runtime.service?.active_digest?.slice(0, 12) ?? t('plugins.noActiveRelease', 'None active')}</p>
    {runtime.stale || unavailable ? <p role="status">{t('plugins.runtimeStale', 'Service health is stale or unavailable.')}</p> : null}
    {runtime.error_code ? <p role="status">{t('plugins.runtimeNeedsAttention', 'The local service needs attention.')}</p> : null}
    {operationId ? <p role="status">{t('plugins.runtimeOperation', 'Local operation')}: {stateLabel}</p> : null}
    {operation.error ? <p role="status">{t('plugins.runtimeReadFailed', 'Operation status could not be read. The original request has not been resubmitted.')}</p> : null}
    <Actions>
      <Button type="button" disabled={runtime.uninstalled || (!runtime.enabled && !withdrawable) || unavailable || busy || pending || (state === 'recovery_required' && !withdrawable) || (!!submitted && !confirmed)} onClick={() => void withdrawRuntime('disable')}>{t('plugins.disableRuntime', 'Disable runtime')}</Button>
      <Button type="button" disabled={(runtime.uninstalled && !withdrawable) || (!runtime.selected_digest && !withdrawable) || unavailable || busy || pending || (state === 'recovery_required' && !withdrawable) || (!!submitted && !confirmed)} onClick={() => void withdrawRuntime('uninstall')}>{t('plugins.uninstallRuntime', 'Uninstall plugin')}</Button>
      {state === 'recovery_required' ? <Button type="button" disabled={unavailable || busy} onClick={() => void recoverOperation()}>{t('plugins.recoverRuntime', 'Recover local operation')}</Button> : null}
      {operationId ? <Button type="button" disabled={operation.isFetching || busy} onClick={() => void operation.refetch()}>{t('plugins.refreshOperation', 'Refresh operation status')}</Button> : null}
    </Actions>
    {withdrawable ? <p>{t('plugins.withdrawInterruptedRuntime', 'Disable or uninstall withdraws this pending local change without running its release. Installation history and cleanup records are retained.')}</p> : null}
    <p>{t('plugins.runtimeCleanup', 'Disable and uninstall stop future capability work. Cleanup supervision, credentials, records and recovery artifacts are retained. Uninstall is not a data purge. Select or activate a verified release to reinstall.')}</p>
    {notice && state !== 'complete' ? <p role="status">{notice}</p> : null}
    <Button type="button" onClick={() => setReleaseOpen(!releaseOpen)}>{releaseOpen ? t('plugins.closeReleaseInstallation', 'Close release installation') : t('plugins.openReleaseInstallation', 'Install a release')}</Button>
    {releaseOpen ? <RuntimeReleasePanel runtime={runtime} /> : null}
  </RuntimeCard>;
}

/** Observe independently supervised runtimes even when no plugin child is available. */
export function ManagedRuntimesPanel() {
  const { t } = useSkulkTranslation();
  const query = useGetManagedRuntimesQuery(undefined, { pollingInterval: 5000, skipPollingIfUnfocused: true });
  const [register, registering] = useRegisterManagedRuntimeMutation();
  const [setupId, setSetupId] = useState<string | null>(null);
  const [registrationUncertain, setRegistrationUncertain] = useState(false);
  const addPlugin = async () => {
    const id = setupId ?? `managed.${crypto.randomUUID().replaceAll('-', '')}`;
    setSetupId(id);
    setRegistrationUncertain(false);
    try { await register(id).unwrap(); }
    catch { setRegistrationUncertain(true); }
  };
  return <section aria-label={t('plugins.managedRuntimes', 'Managed runtimes')}>
    <h2>{t('plugins.managedRuntimes', 'Managed runtimes')}</h2>
    <Button type="button" disabled={registering.isLoading || !!query.error || query.isLoading || !!setupId} onClick={() => void addPlugin()}>{t('plugins.addManagedPlugin', 'Add plugin')}</Button>
    {registrationUncertain ? <p role="status">{t('plugins.registrationUncertain', 'Registration was not confirmed. Refresh source status to check the retained installation before continuing.')}</p> : null}
    {registrationUncertain ? <Button type="button" disabled={registering.isLoading} onClick={() => void addPlugin()}>{t('plugins.retryRegistration', 'Retry the same registration')}</Button> : null}
    {setupId && !registering.isLoading ? <>
      <RuntimeSourceForm pluginId={setupId} onSaved={() => { setSetupId(null); setRegistrationUncertain(false); }} />
      <Button type="button" onClick={() => { setSetupId(null); setRegistrationUncertain(false); }}>{t('plugins.closeSourceSetup', 'Close source setup')}</Button>
    </> : null}
    {query.isLoading ? <p>{t('plugins.loadingRuntimes', 'Loading local services…')}</p> : null}
    {query.error ? <p role="status">{t('plugins.managerUnavailable', 'Local runtime management is unavailable. Check local service setup and your plugin permissions.')}</p> : null}
    {query.data?.installations.length === 0 ? <p>{t('plugins.noManagedRuntimes', 'No managed runtimes are installed.')}</p> : null}
    {query.data?.installations.map((runtime) => <RuntimeControls key={runtime.plugin_id} runtime={runtime} unavailable={!!query.error} />)}
  </section>;
}
