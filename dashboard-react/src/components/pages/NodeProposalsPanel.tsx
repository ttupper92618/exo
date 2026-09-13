import { skipToken } from '@reduxjs/toolkit/query';
import { Fragment, useRef, useState } from 'react';
import styled from 'styled-components';
import { useSkulkTranslation } from '../../i18n/tolgee';
import {
  useApproveNodeProposalMutation, useGetNodeProposalOperationQuery,
  useGetNodeProposalReviewQuery, useGetNodeProposalsQuery, useResumeNodeProposalMutation,
  type NodeAddress, type ProposalReference,
} from '../../store/endpoints/plugins';
import { Button } from '../common/Button';

const Panel = styled.section`margin-top: 16px; overflow-wrap: anywhere;`;
const Actions = styled.div`display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0;`;
const Facts = styled.dl`dt { font-weight: 600; } dd { margin: 4px 0 12px; }`;

function retainedOperation(key: string): string {
  try { const value = localStorage.getItem(key); return value && /^[a-f0-9]{32}$/.test(value) ? value : ''; }
  catch { return ''; }
}

/** Review immutable provider terms and explicitly approve once; reconnect only reads. */
function ProposalControls({ pluginId, nodeId, actionsAvailable }: NodeAddress & { actionsAvailable: boolean }) {
  const { t } = useSkulkTranslation();
  const storageKey = `skulk-proposal-operation-v1:${encodeURIComponent(pluginId)}:${encodeURIComponent(nodeId)}`;
  const [operationId, setOperationId] = useState(() => retainedOperation(storageKey));
  const [lookup, setLookup] = useState('');
  const [offset, setOffset] = useState(0);
  const [reference, setReference] = useState<ProposalReference>();
  const [notice, setNotice] = useState('');
  const submitting = useRef(false);
  const [approve, approving] = useApproveNodeProposalMutation();
  const [resume, resuming] = useResumeNodeProposalMutation();
  const listing = useGetNodeProposalsQuery({ pluginId, nodeId, offset }, { refetchOnMountOrArgChange: true });
  const review = useGetNodeProposalReviewQuery(reference ?? skipToken, { refetchOnMountOrArgChange: true });
  const operation = useGetNodeProposalOperationQuery(operationId ? { pluginId, nodeId, operationId } : skipToken,
    { pollingInterval: 3000, skipPollingIfUnfocused: true, refetchOnMountOrArgChange: true });
  const current = review.currentData;
  const observed = operation.currentData;
  const busy = approving.isLoading || resuming.isLoading;
  const remember = (identity: string) => {
    // Persist only a lookup ID, before the POST. No proposal contents, approval
    // material or credentials are stored; reload always performs a fresh GET.
    try { if (identity) localStorage.setItem(storageKey, identity); else localStorage.removeItem(storageKey); }
    catch { setNotice(t('plugins.proposalKeepId', 'Browser storage is unavailable. Keep the operation ID to check progress after reconnecting.')); }
    setOperationId(identity);
  };
  const approveReviewed = async () => {
    if (submitting.current || operationId || !current?.approvalRevision || review.isFetching || review.isError || !actionsAvailable) return;
    submitting.current = true;
    setNotice('');
    const identity = crypto.randomUUID().replaceAll('-', '');
    remember(identity);
    try {
      await approve({ operationId: identity, reference: current.proposal.reference, reviewRevision: current.approvalRevision }).unwrap();
    } catch {
      setNotice(t('plugins.proposalReplyLost', 'Approval response was not confirmed. Check this operation; do not submit the proposal again.'));
    } finally { submitting.current = false; }
  };
  const resumeOriginal = async () => {
    if (submitting.current || observed?.phase !== 'approval_interrupted' || operation.isError || !actionsAvailable) return;
    submitting.current = true;
    setNotice('');
    try { await resume({ pluginId, nodeId, operationId }).unwrap(); }
    catch { setNotice(t('plugins.proposalResumeFailed', 'Recovery response was not confirmed. Refresh this operation before taking another action.')); }
    finally { submitting.current = false; void operation.refetch(); }
  };
  return <Panel>
    <h4>{t('plugins.proposalReviewTitle', 'Proposal review')}</h4>
    <p>{t('plugins.proposalReviewIntro', 'Review the exact terms before approving. Approval may start paid work and requires owner approval access.')}</p>
    <Actions>
      <Button type="button" disabled={listing.isFetching || busy} onClick={() => { setReference(undefined); void listing.refetch(); }}>{t('plugins.proposalRefresh', 'Refresh proposals')}</Button>
      {offset > 0 ? <Button type="button" onClick={() => { setReference(undefined); setOffset(0); }}>{t('plugins.proposalFirst', 'First page')}</Button> : null}
      {listing.currentData?.nextOffset != null ? <Button type="button" onClick={() => { setReference(undefined); setOffset(listing.currentData!.nextOffset!); }}>{t('plugins.proposalNext', 'Next page')}</Button> : null}
    </Actions>
    {listing.isError ? <p role="alert">{t('plugins.proposalsUnavailable', 'Proposals are unavailable. Check plugin health and read access.')}</p> : null}
    {listing.currentData?.proposals.map((item) => <p key={`${item.reference.proposalId}:${item.reference.proposalDigest}`}>
      {item.summary} · {item.state} · {new Date(item.expiresAt * 1000).toISOString()}{' '}
      <Button type="button" disabled={busy || listing.isFetching || listing.isError} onClick={() => { setNotice(''); setReference(item.reference); }}>{t('plugins.proposalReview', 'Review proposal')}</Button>
    </p>)}
    {review.isError ? <p role="alert">{t('plugins.proposalReviewUnavailable', 'Review is unavailable. Refresh before approving.')}</p> : null}
    {reference && current ? <section aria-label={t('plugins.proposalTerms', 'Reviewed terms')}>
      <h5>{current.proposal.summary}</h5>
      <p>{t('plugins.proposalExpiry', 'Proposal expiry')}: {new Date(current.proposal.expiresAt * 1000).toISOString()}</p>
      <Facts>{current.fields.map((field, index) => <Fragment key={index}><dt>{field.label}</dt><dd>{field.value}</dd></Fragment>)}</Facts>
      <p>{t('plugins.proposalDigest', 'Proposal digest')}: {current.proposal.reference.proposalDigest}</p>
      <p>{t('plugins.proposalObserved', 'Reviewed at')}: {new Date(current.observedAt * 1000).toISOString()}</p>
      <Button type="button" disabled={!actionsAvailable || busy || !!operationId || review.isFetching || review.isError || !current.approvalRevision || current.proposal.state !== 'pending_approval' || current.proposal.expiresAt * 1000 <= Date.now()}
        onClick={() => void approveReviewed()}>{t('plugins.proposalApprove', 'Approve and execute reviewed proposal')}</Button>
      {!current.approvalRevision ? <p>{t('plugins.proposalApprovalUnavailable', 'Approval is unavailable. Check the owner approval credential, policy and target.')}</p> : null}
    </section> : null}
    {operationId ? <section aria-label={t('plugins.proposalProgress', 'Proposal operation')}>
      <p>{t('plugins.proposalOperationId', 'Operation ID')}: {operationId}</p>
      {observed ? <p role="status">{observed.phase}{observed.code ? ` · ${observed.code}` : ''}{observed.correctiveAction ? ` · ${observed.correctiveAction}` : ''}</p> : null}
      {observed?.reconciliation ? <section aria-label={t('plugins.proposalCleanup', 'Cleanup observation')}>
        <p>{observed.reconciliation.state === 'absent' ? t('plugins.proposalAbsent', 'Confirmed absent') : observed.reconciliation.state}</p>
        {observed.reconciliation.observedAt != null ? <p>{t('plugins.proposalCleanupObserved', 'Cleanup journal observed at')}: {new Date(observed.reconciliation.observedAt * 1000).toISOString()}</p> : null}
        {observed.reconciliation.stale ? <p role="alert">{t('plugins.proposalCleanupStale', 'Cleanup observation is stale or unavailable. Restore cleanup access and refresh; do not replay the proposal.')}</p> : null}
        <p>{t('plugins.proposalCleanupHistory', 'Cleanup evidence does not change the submission result or prove inference readiness.')}</p>
      </section> : null}
      {operation.isError ? <p role="alert">{t('plugins.proposalStatusUnavailable', 'Operation status is unavailable or stale. Restore access and refresh; do not replay the proposal.')}</p> : null}
      <Actions>
        <Button type="button" disabled={operation.isFetching || busy} onClick={() => void operation.refetch()}>{t('plugins.proposalRefreshOperation', 'Refresh operation')}</Button>
        {observed?.phase === 'approval_interrupted' ? <Button type="button" disabled={!actionsAvailable || busy || operation.isError || operation.isFetching} onClick={() => void resumeOriginal()}>{t('plugins.proposalResume', 'Resume original approval')}</Button> : null}
        <Button type="button" disabled={busy} onClick={() => { remember(''); setReference(undefined); }}>{t('plugins.proposalAnother', 'Review another proposal')}</Button>
      </Actions>
    </section> : null}
    <form onSubmit={(event) => { event.preventDefault(); if (/^[a-f0-9]{32}$/.test(lookup) && !busy) remember(lookup); }}>
      <label>{t('plugins.proposalLookup', 'Existing operation ID')}<input value={lookup} maxLength={32} onChange={(event) => setLookup(event.target.value)} /></label>
      <Button type="submit" disabled={busy || !/^[a-f0-9]{32}$/.test(lookup)}>{t('plugins.proposalObserve', 'Observe operation')}</Button>
    </form>
    {notice ? <p role="status">{notice}</p> : null}
  </Panel>;
}

/** Load proposal metadata only after the operator opens this node's review panel. */
export function NodeProposalsPanel({ pluginId, nodeId, actionsAvailable }: NodeAddress & { actionsAvailable: boolean }) {
  const { t } = useSkulkTranslation();
  const [open, setOpen] = useState(false);
  return <Panel><Button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? t('plugins.proposalsClose', 'Close proposals') : t('plugins.proposalsOpen', 'Review proposals')}</Button>
    {open ? <ProposalControls key={`${pluginId}/${nodeId}`} pluginId={pluginId} nodeId={nodeId} actionsAvailable={actionsAvailable} /> : null}
  </Panel>;
}
