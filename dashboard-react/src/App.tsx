import { useCallback, useEffect, useMemo, useState } from 'react';
import styled from 'styled-components';

/*
 * Positioning anchor for the mobile menu sheet: the sheet is absolute at
 * top: 100% of this wrapper, so it always opens flush under the header
 * even when the reconnect banner adds height above it. z-index lifts the
 * sheet above the content row while the header keeps its own higher index.
 */
const HeaderAnchor = styled.div`
  position: relative;
  z-index: 19;
`;
import { ThemeProvider } from 'styled-components';
import { darkTheme, lightTheme, GlobalStyle } from './theme';
import { useClusterState, type RawInstances } from './hooks/useClusterState';
import { HeaderNav } from './components/layout/HeaderNav';
import { MobileMenuSheet } from './components/layout/MobileMenuSheet';
import { useIsMobile, MOBILE_BREAKPOINT_PX } from './hooks/useMediaQuery';
import { TopologyGraph } from './components/topology/TopologyGraph';
// ClusterWarnings replaced by inline header warning indicator
import { ConnectionBanner } from './components/status/ConnectionBanner';
import { ToastContainer } from './components/status/ToastContainer';
import { NetworkMesh } from './components/common/NetworkMesh';
import { SceneBackdrop } from './components/common/SceneBackdrop';
import { ShootingStars } from './components/common/ShootingStars';
import { ObservabilityPanel } from './components/observability/ObservabilityPanel';
import { SettingsPanel } from './components/layout/SettingsPanel';
import { TelemetryConsentModal } from './components/layout/TelemetryConsentModal';
import { ModelStorePage } from './components/pages/DownloadsPage';
import { ChatView } from './components/pages/ChatView';
import { OperatorPage } from './components/pages/OperatorPage';
import { StewardChatView } from './components/pages/StewardChatView';
import { IntegrationsPage } from './components/pages/IntegrationsPage';
import { PluginsPage } from './components/pages/PluginsPage';
import { InstancePanel, type InstanceCardData } from './components/layout/InstancePanel';
import { ConversationPanel } from './components/layout/ConversationPanel';
import { addToast } from './hooks/useToast';
import type { InstanceStatus, NodeRunnerState } from './components/cluster/RunningInstanceCard';
import { chatActions } from './store/slices/chatSlice';
import { operatorSession } from './auth/operatorSession';
import { apiSlice } from './store/api';
import { useAppDispatch, useAppSelector } from './store/hooks';
import { uiActions, type ObservabilityTab } from './store/slices/uiSlice';
import { useSkulkTranslation, type SkulkTranslate } from './i18n/tolgee';
import { modelSupportsTextChat } from './types/models';

const Shell = styled.div`
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  height: 100%;
`;

const ContentRow = styled.div`
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: row;
  /* Positioning anchor for the mobile panel drawers: absolute children
   * cover the content area only, so the header (and the toggles that
   * opened a drawer) stays visible and interactive above them. */
  position: relative;
`;

/*
 * Side-panel presentation switch (#474). On desktop the wrapper is
 * display: contents, so the panel participates in ContentRow's flex layout
 * exactly as before. At the phone breakpoint the panel becomes a fixed
 * overlay drawer instead of a column: the desktop side-by-side split left
 * chat's conversation area with no room at all (History + Instances
 * consumed the whole viewport) and crushed the topology graph into a
 * sliver whenever the instance panel opened.
 */
const PanelOverlay = styled.div<{ $side: 'left' | 'right' }>`
  display: contents;

  @media (max-width: ${MOBILE_BREAKPOINT_PX}px) {
    display: block;
    /* Absolute within ContentRow (not fixed over the viewport): the drawer
     * covers content only, leaving the header and its panel toggles
     * interactive, so keyboard/screen-reader users can dismiss via the
     * same aria-pressed toggle that opened it. */
    position: absolute;
    top: 0;
    bottom: 0;
    ${({ $side }) => ($side === 'left' ? 'left: 0;' : 'right: 0;')}
    z-index: 5;
    /* The panels themselves are background: transparent (they sit against
     * the page gradient as desktop columns); as an overlay that reads as
     * see-through, so the wrapper supplies a fully opaque base. */
    background: ${({ theme }) => theme.colors.surface};
    /* Deterministic drawer width: the panels' own 340px would overflow the
     * cap on narrow phones (320px viewports), so the overlay owns the width
     * and the aside fills it. */
    width: min(340px, 88vw);
    box-shadow: ${({ $side }) => ($side === 'left' ? '18px' : '-18px')} 0 48px
      ${({ theme }) => theme.colors.shadowStrong};

    > aside {
      height: 100%;
      width: 100%;
    }
  }
`;

/* Dismiss layer behind an open mobile panel drawer; standard flyover blur. */
const PanelBackdrop = styled.div`
  display: none;

  @media (max-width: ${MOBILE_BREAKPOINT_PX}px) {
    display: block;
    position: absolute;
    inset: 0;
    z-index: 4;
    background: ${({ theme }) => theme.colors.shadowStrong};
    backdrop-filter: blur(2px);
  }
`;

const Main = styled.main`
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-x: hidden;
  overflow-y: auto;
`;

interface StoreDownload {
  modelId: string;
  progress: number;
  status: string;
}

/* ── Runner status → InstanceStatus mapping ───────────── */

/** Collapse a single runner's tagged status into the per-node category the
 *  instance card renders. The runner lifecycle is idle -> connecting ->
 *  connected -> loading -> loaded -> warming up -> ready; everything that is not
 *  a terminal ready/failed/stopping state reads as "loading", except the initial
 *  RunnerIdle which reads as "pending" (spawned, not yet driven). */
function runnerNodeState(runner: Record<string, unknown> | undefined): NodeRunnerState {
  if (!runner) return 'pending';
  const key = Object.keys(runner)[0];
  if (key === 'RunnerReady' || key === 'RunnerRunning') return 'ready';
  if (key === 'RunnerFailed') return 'failed';
  if (key === 'RunnerShuttingDown' || key === 'RunnerShutdown') return 'stopping';
  if (key === 'RunnerIdle') return 'pending';
  return 'loading';
}

function deriveInstanceStatus(
  runnerIds: string[],
  runners: Record<string, Record<string, unknown>>,
  t: SkulkTranslate,
): { status: InstanceStatus; message?: string; progress?: number } {
  if (runnerIds.length === 0) {
    return {
      status: 'loading',
      message: t('app.instanceStatus.waitingForRunners', 'Waiting for runners...'),
    };
  }

  const statuses = runnerIds.map((rid) => runners[rid]);

  // If any runner has failed, the instance is failed
  const failed = statuses.find((s) => s && 'RunnerFailed' in s);
  if (failed) {
    const inner = failed.RunnerFailed as Record<string, unknown> | undefined;
    return { status: 'failed', message: inner?.errorMessage as string | undefined };
  }

  // If any runner is shutting down
  if (statuses.some((s) => s && ('RunnerShuttingDown' in s || 'RunnerShutdown' in s))) {
    return { status: 'shutting_down' };
  }

  // If all runners are ready or running
  const allReady = statuses.every((s) => s && ('RunnerReady' in s || 'RunnerRunning' in s));
  if (allReady) {
    const anyRunning = statuses.some((s) => s && 'RunnerRunning' in s);
    return { status: anyRunning ? 'running' : 'ready' };
  }

  // If any runner is warming up
  if (statuses.some((s) => s && 'RunnerWarmingUp' in s)) {
    return { status: 'warming_up' };
  }

  // Loading — try to extract progress from RunnerLoading
  const loading = statuses.find((s) => s && 'RunnerLoading' in s);
  if (loading) {
    const inner = loading.RunnerLoading as Record<string, unknown> | undefined;
    const loaded = inner?.layersLoaded as number | undefined;
    const total = inner?.totalLayers as number | undefined;
    const progress = loaded != null && total != null && total > 0
      ? Math.round((loaded / total) * 100)
      : undefined;
    const message = loaded != null && total != null
      ? t('app.instanceStatus.downloadingLayers', 'Downloading layers {loaded}/{total}...', { loaded, total })
      : t('app.instanceStatus.loadingModel', 'Loading model...');
    return { status: 'loading', message, progress };
  }

  // Connecting or idle
  return { status: 'loading', message: t('app.instanceStatus.connecting', 'Connecting...') };
}

export function App() {
  const { t } = useSkulkTranslation();
  const {
    topology,
    localNodeId,
    connected,
    downloads,
    instances,
    runners,
    nodeThunderbolt,
    nodeThunderboltBridge,
    nodeRdmaCtl,
    nodeCapabilities,
    nodeResources,
    thunderboltBridgeCycles,
  } = useClusterState();
  const realtimeTranscriptionAvailable = Boolean(
    localNodeId && nodeCapabilities[localNodeId]?.includes('stt.realtime'),
  );
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Phone-width header: nav and icon actions collapse into the hamburger
  // sheet. The open flag resets when the viewport grows past the breakpoint
  // so a rotation or window resize never strands an invisible open menu.
  const isMobile = useIsMobile();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  useEffect(() => {
    if (!isMobile) setMobileMenuOpen(false);
  }, [isMobile]);
  // Drawer exclusivity must also hold for PERSISTED state, not just the
  // toggle handlers: both flags can arrive true from a desktop session, and
  // independent render predicates would stack both drawers over a phone
  // viewport. Instances yields to History (the user is mid-conversation).
  useEffect(() => {
    if (isMobile && historyPanelOpen && panelOpen) dispatch(uiActions.togglePanel());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMobile]);
  const [storeDownloads, setStoreDownloads] = useState<StoreDownload[]>([]);
  // Observability panel state lives on the global UI store so any component (toolbar
  // nav, per-node bug icons, future cross-links) can open the panel to a specific tab.
  const dispatch = useAppDispatch();
  useEffect(() => {
    let identity = operatorSession.snapshot();
    return operatorSession.subscribe(() => {
      const next = operatorSession.snapshot();
      if (next.mode !== identity.mode || next.deviceId !== identity.deviceId) dispatch(apiSlice.util.resetApiState());
      identity = next;
    });
  }, [dispatch]);
  const openObservability = (tab?: ObservabilityTab, nodeId?: string) =>
    dispatch(uiActions.openObservability({ tab, nodeId }));
  const activeRoute = useAppSelector((s) => s.ui.activeRoute);
  const panelOpen = useAppSelector((s) => s.ui.panelOpen);
  const historyPanelOpen = useAppSelector((s) => s.ui.historyPanelOpen);
  const themeName = useAppSelector((s) => s.ui.theme);
  const activeTheme = themeName === 'light' ? lightTheme : darkTheme;

  // Reflect theme on the html root so non-styled-components surfaces (highlight.js,
  // scrollbars, etc.) can react via `html[data-theme='light'] …` selectors.
  useEffect(() => {
    if (typeof document === 'undefined') return;
    document.documentElement.setAttribute('data-theme', themeName);
  }, [themeName]);
  const setActiveRoute = (route: typeof activeRoute) => {
    dispatch(uiActions.setActiveRoute(route));
    const path = route === 'cluster' ? '/' : `/${route}`;
    if (window.location.pathname !== path) history.pushState(null, '', path);
  };

  // On load, honour the URL path so /operator (and future deep-links) work.
  useEffect(() => {
    const path = window.location.pathname.replace(/^\//, '') || 'cluster';
    const valid: typeof activeRoute[] = ['cluster', 'model-store', 'chat', 'steward', 'integrations', 'plugins', 'operator'];
    if (valid.includes(path as typeof activeRoute)) {
      dispatch(uiActions.setActiveRoute(path as typeof activeRoute));
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const togglePanel = () => dispatch(uiActions.togglePanel());
  const toggleHistoryPanel = () => dispatch(uiActions.toggleHistoryPanel());
  const conversationsMap = useAppSelector((s) => s.chat.conversations);
  const allConversations = useMemo(
    () => Object.values(conversationsMap).sort((a, b) => b.updatedAt - a.updatedAt),
    [conversationsMap],
  );
  const activeConversationId = useAppSelector((s) => s.chat.activeConversationId);
  const selectedModelId = useAppSelector((s) => s.chat.selectedModelId);
  const selectConversation = (id: string) => dispatch(chatActions.selectConversation(id));
  const deleteConversation = (id: string) => dispatch(chatActions.deleteConversation(id));
  const newConversation = (modelId: string) => dispatch(chatActions.newConversation(modelId));

  // Compute cluster warnings from topology
  const clusterWarnings = useMemo(() => {
    const nodes = topology?.nodes;
    if (!nodes) return null;
    const items: { level: 'error' | 'warning'; message: string }[] = [];

    const getNodeName = (nodeId: string): string =>
      nodes[nodeId]?.friendly_name ?? `${nodeId.slice(0, 8)}...`;

    // Version mismatch (error)
    const entries = Object.values(nodes).filter(
      (n) => n.skulk_commit && n.skulk_commit !== 'Unknown' && n.skulk_commit !== 'unknown',
    );
    if (entries.length >= 2) {
      const commits = new Set(entries.map((n) => n.skulk_commit));
      if (commits.size > 1) {
        const details = entries.map((n) => {
          const ver = n.skulk_version ?? '?';
          const commit = n.skulk_commit?.slice(0, 7);
          return `${n.friendly_name ?? t('common.unknown', 'Unknown')} (${ver}${commit ? `-${commit}` : ''})`;
        }).join(', ');
        items.push({
          level: 'error',
          message: t(
            'app.warnings.versionMismatch',
            'Version mismatch: {details}. Update all nodes with git pull && uv sync.',
            { details },
          ),
        });
      }
    }

    // macOS build mismatch (warning)
    const macosNodes = Object.entries(nodes).filter(([, node]) => {
      const version = node.os_version;
      return typeof version === 'string' && /^\d/.test(version);
    });
    if (macosNodes.length >= 2) {
      const builds = new Set(macosNodes.map(([, node]) => node.os_build_version ?? node.os_version ?? 'unknown'));
      if (builds.size > 1) {
        const details = macosNodes
          .map(([nodeId, node]) => `${node.friendly_name ?? getNodeName(nodeId)} (macOS ${node.os_version ?? '?'} ${node.os_build_version ?? 'unknown'})`)
          .join(', ');
        items.push({
          level: 'warning',
          message: t(
            'app.warnings.macosVersionMismatch',
            'macOS version mismatch: {details}. Keep cluster nodes on the same macOS build for best compatibility.',
            { details },
          ),
        });
      }
    }

    // RDMA phantom (warning)
    const hasRdmaPhantom = Object.values(nodes).some(
      (n) => n.rdma_enabled && n.rdma_interfaces_present === false,
    );
    if (hasRdmaPhantom) {
      items.push({
        level: 'warning',
        message: t(
          'app.warnings.rdmaMissingInterfaces',
          'RDMA reported as enabled but no RDMA interfaces exist. Thunderbolt 5 (M4 Pro/Max+) is required for tensor parallel.',
        ),
      });
    }

    // TB5 present but RDMA disabled (warning)
    const thunderboltNodeIds = Object.entries(nodeThunderbolt)
      .filter(([, info]) => info.interfaces.length > 0)
      .map(([nodeId]) => nodeId);
    if (thunderboltNodeIds.length >= 2) {
      const nodesWithRdmaDisabled = thunderboltNodeIds.filter((nodeId) => nodeRdmaCtl[nodeId]?.enabled !== true);
      if (nodesWithRdmaDisabled.length > 0) {
        const details = nodesWithRdmaDisabled.map(getNodeName).join(', ');
        items.push({
          level: 'warning',
          message: t(
            'app.warnings.thunderboltRdmaDisabled',
            'Thunderbolt 5 detected but RDMA is not enabled on: {details}. Enable rdma_ctl on those nodes to unlock tensor parallel.',
            { details },
          ),
        });
      }
    }

    // Mac Studio en2 RDMA misuse (warning)
    const affectedMacStudioNodes = new Set<string>();
    for (const edge of topology.edges) {
      if (edge.sourceRdmaIface === 'rdma_en2') {
        const node = nodes[edge.source];
        if (node?.system_info?.model_id === 'Mac Studio' && nodeRdmaCtl[edge.source]?.enabled) {
          affectedMacStudioNodes.add(`${getNodeName(edge.source)} → ${getNodeName(edge.target)} (en2)`);
        }
      }
      if (edge.sinkRdmaIface === 'rdma_en2') {
        const node = nodes[edge.target];
        if (node?.system_info?.model_id === 'Mac Studio' && nodeRdmaCtl[edge.target]?.enabled) {
          affectedMacStudioNodes.add(`${getNodeName(edge.target)} → ${getNodeName(edge.source)} (en2)`);
        }
      }
    }
    if (affectedMacStudioNodes.size > 0) {
      items.push({
        level: 'warning',
        message: t(
          'app.warnings.macStudioRdmaEn2',
          'Mac Studio RDMA is using the Ethernet-adjacent en2 Thunderbolt port on: {details}. Move those cables to one of the other TB ports.',
          { details: [...affectedMacStudioNodes].join(', ') },
        ),
      });
    }

    // Thunderbolt bridge cycle detection (warning)
    if (thunderboltBridgeCycles.length > 0) {
      const cycle = thunderboltBridgeCycles[0];
      const serviceName = cycle
        .map((nodeId) => nodeThunderboltBridge[nodeId]?.serviceName)
        .find((name): name is string => typeof name === 'string' && name.length > 0)
        ?? t('app.warnings.thunderboltBridgeServiceName', 'Thunderbolt Bridge');
      items.push({
        level: 'warning',
        message: t(
          'app.warnings.thunderboltBridgeCycle',
          'Thunderbolt Bridge cycle detected across {path}. Disable "{serviceName}" on one affected node to break the loop.',
          { path: cycle.map(getNodeName).join(' -> '), serviceName },
        ),
      });
    }

    if (items.length === 0) return null;
    const worstLevel = items.some((i) => i.level === 'error') ? 'error' : 'warning';
    return { level: worstLevel as 'error' | 'warning', items };
  }, [nodeRdmaCtl, nodeThunderbolt, nodeThunderboltBridge, thunderboltBridgeCycles, topology, t]);

  // Poll store downloads for the header progress indicator
  const pollStoreDownloads = useCallback(async () => {
    try {
      const res = await fetch('/store/downloads');
      if (res.ok) {
        const data = await res.json();
        setStoreDownloads(data.downloads ?? []);
      }
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    pollStoreDownloads();
    const id = setInterval(pollStoreDownloads, 3000);
    return () => clearInterval(id);
  }, [pollStoreDownloads]);

  const downloadProgress = useMemo(() => {
    if (storeDownloads.length === 0) return null;
    const totalPct = storeDownloads.reduce((sum, d) => sum + (d.progress * 100), 0);
    const avgPct = Math.round(totalPct / storeDownloads.length);
    return { count: storeDownloads.length, percentage: avgPct };
  }, [storeDownloads]);

  // Derive instance card data for the right panel
  // Fabric-maintained system placements (the intelligent-fabric steward)
  // are not user instances: filtering at the source hides them from every
  // consumer at once (instance cards, chat picker, model-store page,
  // placement views). The steward is reachable through its own chat surface.
  const visibleInstances = useMemo<RawInstances>(() => {
    const filtered: RawInstances = {};
    for (const [iid, inst] of Object.entries(instances)) {
      const inner =
        inst.MlxRingInstance ?? inst.MlxJacclInstance ?? inst.LlamaRpcInstance;
      if (inner?.systemRole) {
        continue;
      }
      filtered[iid] = inst;
    }
    return filtered;
  }, [instances]);

  const instanceCards = useMemo<InstanceCardData[]>(() => {
    const cards: InstanceCardData[] = [];
    for (const [iid, inst] of Object.entries(visibleInstances)) {
      // Instance is a tagged union: MlxRing / MlxJaccl (in-process) and
      // LlamaRpc (pooled multi-node GGUF, #328). Handling only the first two
      // silently dropped every pooled instance from the Active Instances panel.
      const instanceType: InstanceCardData['instanceType'] = inst.MlxRingInstance
        ? 'MlxRing'
        : inst.MlxJacclInstance
          ? 'MlxJaccl'
          : 'LlamaRpc';
      const inner =
        inst.MlxRingInstance ?? inst.MlxJacclInstance ?? inst.LlamaRpcInstance;
      if (!inner) continue;
      const sa = inner.shardAssignments;
      const modelId = sa?.modelId;
      if (!modelId) continue;

      const instanceId = inner.instanceId ?? iid;
      const nodeToRunner = sa?.nodeToRunner;
      const runnerIds = nodeToRunner ? Object.values(nodeToRunner) : [];
      const nodeIds = nodeToRunner ? Object.keys(nodeToRunner) : [];

      // Derive sharding and model type from shard metadata
      const runnerToShard = sa?.runnerToShard;
      let sharding: 'Pipeline' | 'Tensor' = 'Pipeline';
      // A pooled instance is always served by llama-server across the RPC pair,
      // so default its engine to 'served' regardless of shard-card ordering;
      // the backend read below only refines the in-process MLX/llama_cpp split.
      let engine: InstanceCardData['engine'] = instanceType === 'LlamaRpc' ? 'served' : 'mlx';
      let isEmbedding = false;
      let supportsTextChat = true;
      let speculation: InstanceCardData['speculation'];
      if (runnerToShard) {
        const firstShard = Object.values(runnerToShard)[0];
        if (firstShard && 'TensorShardMetadata' in firstShard) {
          sharding = 'Tensor';
        }
        // Check model card tasks for embedding
        const shardInner = firstShard ? Object.values(firstShard)[0] as Record<string, unknown> | undefined : undefined;
        const mc = (shardInner?.modelCard ?? shardInner?.model_card) as Record<string, unknown> | undefined;
        const tasks = mc?.tasks as string[] | undefined;
        if (tasks?.includes('TextEmbedding')) isEmbedding = true;
        supportsTextChat = modelSupportsTextChat({
          tasks,
          capabilities: mc?.capabilities as string[] | undefined,
        });

        // Engine: every instance is wrapped as an MlxRing/Jaccl instance on the
        // wire, so the card's placement backends (not the wrapper) tell us which
        // engine actually serves it: in-process MLX, in-process llama.cpp, or the
        // served llama-server. Used for the type label instead of assuming MLX.
        const placement = mc?.placement as Record<string, unknown> | undefined;
        const backends = (placement?.compatibleBackends ?? placement?.compatible_backends) as
          | string[]
          | undefined;
        const firstBackend = backends?.[0] ?? '';
        if (firstBackend.startsWith('llama_server')) engine = 'served';
        else if (firstBackend.startsWith('llama_cpp')) engine = 'llama_cpp';
        else engine = 'mlx';

        // Speculative-decoding status comes from the card's runtime section —
        // the card is the rank-invariant source of truth for whether drafting
        // engages (#254), so the badge needs no extra wire data. A card that
        // blocks multi-node speculation shows no badge on multi-node
        // placements (it runs plain distributed decode there). Like the
        // modelCard read above, accept both camelCase and snake_case keys.
        const rt = mc?.runtime as Record<string, unknown> | undefined | null;
        const rtKey = (camel: string, snake: string): unknown =>
          rt?.[camel] ?? rt?.[snake];
        const declaresSidecar =
          !!rtKey('mtpSidecarRepo', 'mtp_sidecar_repo') &&
          !!rtKey('mtpHeads', 'mtp_heads');
        const declaresAssistant = !!rtKey('assistantModelRepo', 'assistant_model_repo');
        const worldSize = runnerIds.length;
        const blockedForPlacement =
          rtKey('speculativeMultiNode', 'speculative_multi_node') === false &&
          worldSize > 1;
        if ((declaresSidecar || declaresAssistant) && !blockedForPlacement) {
          const depth = rtKey('mtpMaxDepth', 'mtp_max_depth');
          speculation = {
            kind: declaresAssistant ? 'assistant' : 'sidecar',
            depth: typeof depth === 'number' ? depth : 1,
          };
        }

        // Served engine (llama-server) speculation: the card declares
        // served_spec_type (draft_mtp / draft_eagle3 / draft_simple) with the
        // draft depth in served_spec_n_max. Surface the same MTP badge as the
        // in-process MLX drafters so served-MTP instances read as MTP too.
        if (!speculation) {
          const servedSpec = rtKey('servedSpecType', 'served_spec_type');
          if (typeof servedSpec === 'string' && servedSpec.startsWith('draft')) {
            const nmax = rtKey('servedSpecNMax', 'served_spec_n_max');
            speculation = { kind: 'sidecar', depth: typeof nmax === 'number' ? nmax : 1 };
          }
        }
      }

      // Derive status from runners
      const derived = deriveInstanceStatus(runnerIds, runners, t);

      // Per-node status for EVERY node the instance is placed on (all ranks of a
      // pipeline / tensor placement), sorted for a stable, readable order, each
      // carrying that node's runner phase so the card shows the laggard.
      const nodeStatuses = nodeIds
        .map((id) => ({
          name: topology?.nodes[id]?.friendly_name || id.slice(0, 8),
          state: runnerNodeState(nodeToRunner ? runners[nodeToRunner[id]] : undefined),
        }))
        .sort((a, b) => a.name.localeCompare(b.name));

      cards.push({
        instanceId,
        modelId,
        sharding,
        instanceType,
        engine,
        nodeStatuses,
        status: derived.status,
        statusMessage: derived.message,
        loadProgress: derived.progress,
        isEmbedding,
        supportsTextChat,
        speculation,
      });
    }
    return cards;
  }, [visibleInstances, runners, topology, t]);

  const hasInstances = instanceCards.length > 0;

  const handleDeleteInstance = useCallback(async (instanceId: string) => {
    try {
      const res = await fetch(`/instance/${instanceId}`, { method: 'DELETE' });
      if (res.ok) {
        addToast({ type: 'success', message: t('app.toasts.instanceDeleted', 'Instance deleted') });
      } else {
        addToast({ type: 'error', message: t('app.toasts.instanceDeleteFailed', 'Failed to delete instance') });
      }
    } catch {
      addToast({ type: 'error', message: t('app.toasts.instanceDeleteFailed', 'Failed to delete instance') });
    }
  }, [t]);

  return (
    <ThemeProvider theme={activeTheme}>
      <GlobalStyle />
      {/* Phone width: a third of the mesh particles; the busy full-density
          field reads as visual noise over content on a small screen. The key
          remounts the canvas when the breakpoint flips so the particle field
          re-seeds at the new density. */}
      {/* The night palette replaces the abstract mesh with the star field
          crowning the viewport and fading out on the way down, so every
          screen opens under the brand sky without a painting competing
          with content. Palettes without a scene keep the mesh. */}
      <SceneBackdrop />
      <ShootingStars />
      {activeTheme.colors.scene === 'none' && (
        <NetworkMesh key={isMobile ? 'mesh-mobile' : 'mesh-desktop'} radius={2.5} count={isMobile ? 14 : 43} linkDistance={430} />
      )}
      <Shell>
        <ConnectionBanner connected={connected} />
        <HeaderAnchor>
        <HeaderNav
          showHome
          activeRoute={activeRoute}
          onNavigate={(route) => setActiveRoute(route)}
          onOpenSettings={() => setSettingsOpen(true)}
          downloadProgress={downloadProgress}
          warnings={clusterWarnings}
          compact={isMobile}
          showMobileMenuToggle={isMobile}
          mobileMenuOpen={mobileMenuOpen}
          onToggleMobileMenu={() => setMobileMenuOpen((v) => !v)}
          showSidebarToggle={activeRoute === 'chat' && allConversations.length > 0}
          sidebarVisible={historyPanelOpen}
          onToggleSidebar={() => {
            // Phone width fits one drawer: opening History closes Instances.
            if (isMobile && !historyPanelOpen && panelOpen) togglePanel();
            toggleHistoryPanel();
          }}
          instanceCount={instanceCards.length}
          instancesHealthy={instanceCards.every((c) => c.status !== 'failed')}
          mobileRightOpen={panelOpen}
          onToggleMobileRight={() => {
            if (isMobile && !panelOpen && historyPanelOpen) toggleHistoryPanel();
            togglePanel();
          }}
        />
        {isMobile && (
          <MobileMenuSheet
            open={mobileMenuOpen}
            activeRoute={activeRoute}
            onNavigate={(route) => setActiveRoute(route)}
            onOpenSettings={() => setSettingsOpen(true)}
            onClose={() => setMobileMenuOpen(false)}
          />
        )}
        </HeaderAnchor>
        <ContentRow>
          {isMobile && ((activeRoute === 'chat' && allConversations.length > 0 && historyPanelOpen) || (hasInstances && panelOpen)) && (
            <PanelBackdrop
              onClick={() => {
                // Only flip flags whose drawer is actually rendered: the
                // persisted flags can be true while the drawer is absent
                // (history off the chat route), and blind toggling would
                // invert that hidden state. Dismissal is also available via
                // the header's aria-pressed panel toggles; this layer is
                // pointer convenience.
                if (activeRoute === 'chat' && allConversations.length > 0 && historyPanelOpen) toggleHistoryPanel();
                if (hasInstances && panelOpen) togglePanel();
              }}
              role="presentation"
            />
          )}
          {activeRoute === 'chat' && allConversations.length > 0 && historyPanelOpen && (
            <PanelOverlay $side="left">
            <ConversationPanel
              conversations={allConversations}
              activeConversationId={activeConversationId}
              onSelect={selectConversation}
              onDelete={deleteConversation}
              onNewChat={() => { if (selectedModelId) newConversation(selectedModelId); }}
            />
            </PanelOverlay>
          )}
          <Main>
            {activeRoute === 'model-store' ? (
              <ModelStorePage
                topology={topology}
                nodeResources={nodeResources}
                downloads={downloads}
                instances={visibleInstances}
                runners={runners}
                onChat={(modelId) => { dispatch(chatActions.selectModel(modelId)); setActiveRoute('chat'); }}
              />
            ) : activeRoute === 'chat' ? (
              <ChatView
                readyInstances={instanceCards}
                realtimeTranscriptionAvailable={realtimeTranscriptionAvailable}
              />
            ) : activeRoute === 'steward' ? (
              <StewardChatView readyInstances={instanceCards} />
            ) : activeRoute === 'integrations' ? (
              <IntegrationsPage readyInstances={instanceCards} />
            ) : activeRoute === 'operator' ? (
              <OperatorPage />
            ) : activeRoute === 'plugins' ? (
              <PluginsPage />
            ) : topology ? (
              <TopologyGraph
                data={topology}
                onInspectNode={(nodeId) => openObservability('node', nodeId)}
              />
            ) : (
              <EmptyState>
                {connected
                  ? t('app.empty.loadingClusterState', 'Loading cluster state...')
                  : t('app.empty.connectingBackend', 'Connecting to backend...')}
              </EmptyState>
            )}
          </Main>
          {hasInstances && panelOpen && (
            <PanelOverlay $side="right">
            <InstancePanel
              instances={instanceCards}
              onDelete={handleDeleteInstance}
              onChat={(modelId) => { dispatch(chatActions.selectModel(modelId)); setActiveRoute('chat'); }}
            />
            </PanelOverlay>
          )}
        </ContentRow>
        <ToastContainer />
        <ObservabilityPanel />
        <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} />
        <TelemetryConsentModal />
      </Shell>
    </ThemeProvider>
  );
}

const EmptyState = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  font-family: ${({ theme }) => theme.fonts.mono};
  font-size: 13px;
  color: ${({ theme }) => theme.colors.textMuted};
  text-transform: uppercase;
  letter-spacing: 2px;
`;
