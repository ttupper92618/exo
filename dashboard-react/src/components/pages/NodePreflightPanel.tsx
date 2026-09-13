import styled from 'styled-components';
import { useSkulkTranslation } from '../../i18n/tolgee';
import { useLazyGetNodePreflightQuery, type NodeAddress } from '../../store/endpoints/plugins';
import { Button } from '../common/Button';

const Panel = styled.section`overflow-wrap: anywhere; margin-top: 12px;`;

/** Run explicit setup observations without treating a prior success as enable authority. */
export function NodePreflightPanel(address: NodeAddress) {
  const { t } = useSkulkTranslation();
  const [run, query] = useLazyGetNodePreflightQuery();
  const report = query.currentData;
  return <Panel aria-label={t('plugins.preflight', 'Setup checks')}>
    <Button type="button" disabled={query.isFetching} onClick={() => void run(address)}>{query.isFetching ? t('plugins.checkingSetup', 'Checking setup…') : t('plugins.checkSetup', 'Check setup')}</Button>
    {query.error ? <p role="alert">{t('plugins.checksUnavailable', 'Setup checks are unavailable. Check plugin access and service health, then retry.')}</p> : null}
    {report && !query.error ? <>
      <p>{t('plugins.checkedRevision', 'Checked settings revision')} {report.revision} · {new Date(report.observedAt * 1000).toLocaleString()}</p>
      <ul>{report.checks.map((check) => <li key={check.code}>
        <strong>{check.passed ? t('plugins.checkPassed', 'Passed') : t('plugins.checkFailed', 'Needs attention')}</strong> · <code>{check.code}</code>
        {check.correctiveAction ? <p>{check.correctiveAction}</p> : null}
      </li>)}</ul>
      <p>{t('plugins.checksAreObservations', 'These are observations. Enabling runs fresh checks and does not approve spending.')}</p>
    </> : null}
  </Panel>;
}
