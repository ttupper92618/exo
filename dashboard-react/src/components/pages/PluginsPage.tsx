import { useState, useSyncExternalStore } from 'react';
import styled from 'styled-components';
import { useSkulkTranslation } from '../../i18n/tolgee';
import { useGetPluginNodesQuery, useGetNodeConfigurationQuery, useConfigurePluginNodeMutation, type ConfigurableNode, type NodeConfiguration } from '../../store/endpoints/plugins';
import { Button } from '../common/Button';
import { ManagedRuntimesPanel } from './ManagedRuntimesPanel';
import { PluginConfigurationFields, supportedConfigurationSchema } from './PluginConfigurationFields';
import { NodeCredentialsPanel } from './NodeCredentialsPanel';
import { NodePreflightPanel } from './NodePreflightPanel';
import { NodeSetupPanel } from './NodeSetupPanel';
import { NodeSetupActionsPanel } from './NodeSetupActionsPanel';
import { NodeProposalsPanel } from './NodeProposalsPanel';
import { OperatorAccessPanel } from './OperatorAccessPanel';
import { operatorSession } from '../../auth/operatorSession';

const Page = styled.section`padding: 24px; width: 100%; max-width: 900px; margin: 0 auto; box-sizing: border-box;`;
const Card = styled.article`
  margin: 16px 0; padding: 20px; border: 1px solid ${({ theme }) => theme.colors.border};
  border-radius: ${({ theme }) => theme.radii.md}; background: ${({ theme }) => theme.colors.surface};
`;
const Actions = styled.div`display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px;`;

/** A node draft retains its observed revision until the owner explicitly saves/reloads. */
function NodeEditor({ pluginId, configuration, reload }: { pluginId: string; configuration: NodeConfiguration; reload: () => Promise<NodeConfiguration> }) {
  const { t } = useSkulkTranslation();
  const [baseline, setBaseline] = useState(configuration);
  const [values, setValues] = useState(configuration.values);
  const [notice, setNotice] = useState('');
  const [reloading, setReloading] = useState(false);
  const [mutate, { isLoading }] = useConfigurePluginNodeMutation();
  const busy = isLoading || reloading;
  const changedElsewhere = configuration.revision !== baseline.revision || configuration.schemaDigest !== baseline.schemaDigest;
  const dirty = JSON.stringify(values) !== JSON.stringify(baseline.values);
  const supported = supportedConfigurationSchema(baseline.configurationSchema);
  const reloadSettings = async () => {
    setReloading(true);
    try {
      const fresh = await reload();
      setBaseline(fresh);
      setValues(fresh.values);
      setNotice('');
    } catch {
      setNotice(t('plugins.reloadFailed', 'Settings could not be reloaded. Your draft is preserved.'));
    } finally {
      setReloading(false);
    }
  };
  const act = async (operation: 'validate' | 'edit' | 'enable' | 'disable') => {
    setNotice('');
    try {
      const result = await mutate({ pluginId, nodeId: baseline.nodeId, operation,
        expectedRevision: baseline.revision, expectedSchemaDigest: baseline.schemaDigest,
        ...(operation === 'validate' || operation === 'edit' ? { values } : {}),
      }).unwrap();
      if (operation !== 'validate') setBaseline(result.configuration);
      if (operation === 'edit') setValues(result.configuration.values);
      setNotice(operation === 'validate' ? t('plugins.valid', 'Settings are valid.') : t('plugins.saved', 'Settings updated.'));
    } catch {
      setNotice(t('plugins.refused', 'The change was refused. Check your access and settings; reload if another operator changed this node.'));
    }
  };
  return <form onSubmit={(event) => { event.preventDefault(); void act('edit'); }}>
    <p>{baseline.enabled ? t('plugins.enabled', 'Enabled') : t('plugins.disabled', 'Disabled')}</p>
    {changedElsewhere ? <p role="status">{t('plugins.changed', 'This node changed elsewhere. Your draft is preserved; reload before saving.')}</p> : null}
    {supported ? <PluginConfigurationFields schema={baseline.configurationSchema} values={values} onChange={setValues} disabled={busy} /> : <p>{t('plugins.unsupported', 'This settings schema is not yet supported by the dashboard. Use the plugin’s configuration tool.')}</p>}
    <Actions>
      <Button type="button" disabled={!supported || busy || changedElsewhere} onClick={() => void act('validate')}>{t('plugins.validate', 'Validate')}</Button>
      <Button type="submit" disabled={!supported || !dirty || busy || changedElsewhere}>{t('plugins.save', 'Save settings')}</Button>
      <Button type="button" disabled={busy || changedElsewhere || (!baseline.enabled && (!supported || dirty))} onClick={() => void act(baseline.enabled ? 'disable' : 'enable')}>{baseline.enabled ? t('plugins.disable', 'Disable') : t('plugins.enable', 'Enable')}</Button>
      <Button type="button" disabled={busy} onClick={() => void reloadSettings()}>{t('plugins.reload', 'Reload settings')}</Button>
    </Actions>
    {notice ? <p role="status">{notice}</p> : null}
  </form>;
}

function NodeCard({ pluginId, node }: { pluginId: string; node: ConfigurableNode }) {
  const { t } = useSkulkTranslation();
  const [expanded, setExpanded] = useState(false);
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const query = useGetNodeConfigurationQuery({ pluginId, nodeId: node.nodeId }, { skip: !expanded || !node.configurable });
  return <Card>
    <h3>{node.bundleId} · {node.version}</h3><p>{node.status}</p>
    {node.configurable ? <Button type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? t('plugins.close', 'Close settings') : t('plugins.configure', 'Configure')}</Button> : <p>{t('plugins.noSettings', 'This node has no configurable settings.')}</p>}
    {expanded && query.isLoading ? <p>{t('plugins.loadingSettings', 'Loading settings…')}</p> : null}
    {expanded && query.error ? <p role="alert">{t('plugins.settingsUnavailable', 'Settings are unavailable. Check node health and your plugin permissions.')}</p> : null}
    {expanded && query.data ? <NodeEditor pluginId={pluginId} configuration={query.data} reload={() => query.refetch().unwrap()} /> : null}
    {node.preflightAvailable ? <NodePreflightPanel pluginId={pluginId} nodeId={node.nodeId} /> : null}
    {node.proposalsAvailable ? <NodeProposalsPanel pluginId={pluginId} nodeId={node.nodeId} actionsAvailable={node.proposalActionsAvailable === true} /> : null}
    {node.setupActionsAvailable ? <NodeSetupActionsPanel pluginId={pluginId} nodeId={node.nodeId} /> : null}
    {node.setupAvailable ? <NodeSetupPanel pluginId={pluginId} nodeId={node.nodeId} /> : null}
    {node.credentialsConfigurable ? <Button type="button" aria-expanded={credentialsOpen} onClick={() => setCredentialsOpen(!credentialsOpen)}>{credentialsOpen ? t('plugins.closeCredentials', 'Close credentials') : t('plugins.manageCredentials', 'Manage credentials')}</Button> : null}
    {credentialsOpen ? <NodeCredentialsPanel pluginId={pluginId} nodeId={node.nodeId} /> : null}
  </Card>;
}

/** Render configuration declared by installed plugins, without provider-specific UI. */
function PluginInventory() {
  const { t } = useSkulkTranslation();
  const query = useGetPluginNodesQuery(undefined, { pollingInterval: 5000, skipPollingIfUnfocused: true });
  return <>
    <h1>{t('plugins.title', 'Plugins')}</h1>
    <p>{t('plugins.intro', 'Manage the settings of capability nodes installed on this Skulk host.')}</p>
    <ManagedRuntimesPanel />
    <Button type="button" disabled={query.isFetching} onClick={() => void query.refetch()}>{t('plugins.refresh', 'Refresh')}</Button>
    {query.isLoading ? <p>{t('plugins.loading', 'Loading plugins…')}</p> : null}
    {query.error ? <p role="alert">{t('plugins.accessRequired', 'Plugin management is unavailable. Open the host dashboard through localhost or Tailscale, or use a paired operator with plugin access.')}</p> : null}
    {query.data?.length === 0 ? <p>{t('plugins.empty', 'No installed plugins expose node settings.')}</p> : null}
    {query.data?.map((plugin) => <section key={plugin.pluginId} aria-label={plugin.pluginId}>
      {!plugin.available ? <p role="status">{plugin.pluginId}: {t('plugins.unavailable', 'Management is unavailable.')}</p> : null}
      {plugin.nodes.map((node) => <NodeCard key={node.nodeId} pluginId={plugin.pluginId} node={node} />)}
    </section>)}
  </>;
}

/** Remount sensitive drafts when the browser changes its authorization identity. */
export function PluginsPage() {
  const session = useSyncExternalStore(operatorSession.subscribe, operatorSession.snapshot);
  return <Page><OperatorAccessPanel /><PluginInventory key={`${session.mode}:${session.deviceId ?? ''}`} /></Page>;
}
