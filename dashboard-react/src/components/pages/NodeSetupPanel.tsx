import styled from 'styled-components';
import { useSkulkTranslation } from '../../i18n/tolgee';
import { useLazyGetNodeSetupQuery, type NodeAddress } from '../../store/endpoints/plugins';
import { Button } from '../common/Button';

const Panel = styled.section`overflow-wrap: anywhere; margin-top: 12px;`;

/** Read and download public setup files; preparation and credential changes stay server-owned. */
export function NodeSetupPanel(address: NodeAddress) {
  const { t } = useSkulkTranslation();
  const [read, query] = useLazyGetNodeSetupQuery();
  const report = query.currentData;
  return <Panel aria-label={t('plugins.setupFiles', 'Setup files')}>
    <Button type="button" disabled={query.isFetching} onClick={() => void read(address)}>{t('plugins.readSetupFiles', 'Read setup files')}</Button>
    {query.error ? <p role="alert">{t('plugins.setupFilesUnavailable', 'Setup files are unavailable. Check plugin access and the owner’s credential status.')}</p> : null}
    {report && !query.error && !query.isFetching ? <>
      <p>{t('plugins.setupFileRevisions', 'Settings / credential revisions')}: {report.revision} / {report.credentialRevision}</p>
      <ul>{report.artifacts.map((artifact) => <li key={artifact.name}>
        <a href={`data:text/plain;charset=utf-8,${encodeURIComponent(artifact.content)}`} download={artifact.name}>{artifact.title} ({artifact.name})</a>
      </li>)}</ul>
      <p>{t('plugins.publicSetupFiles', 'These files contain public setup information. Private keys and credentials stay on the host.')}</p>
    </> : null}
  </Panel>;
}
