import styled, { css, useTheme } from 'styled-components';
import {
  FiSettings,
  FiSidebar,
  FiDatabase,
  FiMessageSquare,
  FiSun,
  FiMoon,
  FiLink,
  FiPackage,
} from 'react-icons/fi';
import { MdHub, MdAutoAwesome } from 'react-icons/md';
import { VscBug } from 'react-icons/vsc';
import { Button } from '../common/Button';
import SkulkIcon from '../icons/SkulkIcon';
import type { Theme } from '../../theme';
import { useAppDispatch, useAppSelector } from '../../store/hooks';
import { uiActions } from '../../store/slices/uiSlice';
import { useSkulkTranslation } from '../../i18n/tolgee';
import { useGetStewardStatusQuery } from '../../store/endpoints/steward';
import { MOBILE_BREAKPOINT_PX } from '../../hooks/useMediaQuery';

export type NavRoute =
  | 'cluster'
  | 'model-store'
  | 'chat'
  | 'steward'
  | 'integrations'
  | 'plugins'
  | 'operator';

export interface HeaderNavProps {
  showHome?: boolean;
  onHome?: () => void;
  activeRoute?: NavRoute;
  onNavigate?: (route: NavRoute) => void;
  showSidebarToggle?: boolean;
  sidebarVisible?: boolean;
  onToggleSidebar?: () => void;
  showMobileMenuToggle?: boolean;
  mobileMenuOpen?: boolean;
  onToggleMobileMenu?: () => void;
  /**
   * Phone-width presentation: hides the inline nav links and the theme /
   * observability / settings buttons (they move into the mobile menu sheet),
   * keeping only status indicators (downloads, instances, warnings).
   */
  compact?: boolean;
  mobileRightOpen?: boolean;
  onToggleMobileRight?: () => void;
  instanceCount?: number;
  instancesHealthy?: boolean;
  downloadProgress?: { count: number; percentage: number } | null;
  warnings?: { level: 'error' | 'warning'; items: { level: 'error' | 'warning'; message: string }[] } | null;
  onOpenSettings?: () => void;
  className?: string;
}

/* ---- styles ---- */

const Nav = styled.header`
  /* Positioned so the z-index applies: the header must paint above the
   * mobile menu backdrop (18) and sheet (19) in the HeaderAnchor stacking
   * context, keeping the hamburger/X interactive while the menu is open. */
  position: relative;
  z-index: 20;
  background: ${({ theme }) => theme.colors.header};
  border-bottom: none;
  background-image: ${({ theme }) => theme.colors.headerBorder};
  background-size: 100% 1px;
  background-position: bottom;
  background-repeat: no-repeat;
  padding: 16px 24px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
`;

const LeftGroup = styled.div`
  display: flex;
  align-items: center;
  gap: 8px;
`;

const RightGroup = styled.div`
  display: flex;
  align-items: center;
  gap: 8px;
`;

const ToggleBtn = styled(Button)<{ $active: boolean }>`
  ${({ $active }) =>
    $active &&
    css`
      color: ${({ theme }) => theme.colors.gold};
      border-color: ${({ theme }) => theme.colors.goldDim};
    `}
`;

const LogoBtn = styled.button<{ $disabled: boolean }>`
  all: unset;
  cursor: ${({ $disabled }) => ($disabled ? 'default' : 'pointer')};
  display: flex;
  align-items: center;
  gap: 8px;
  transition: opacity 0.15s;

  &:hover {
    opacity: ${({ $disabled }) => ($disabled ? 1 : 0.85)};
  }
`;

const LogoText = styled.span`
  font-size: ${({ theme }) => theme.fontSizes.xxl};
  font-weight: 700;
  font-family: ${({ theme }) => theme.fonts.body};
  color: ${({ theme }) => theme.colors.text};
  filter: drop-shadow(0 0 4px ${({ theme }) => theme.colors.border});
`;

const VersionTag = styled.sup`
  font-size: 10px;
  font-weight: 400;
  font-family: ${({ theme }) => theme.fonts.body};
  color: ${({ theme }) => theme.colors.textSecondary};
  margin-left: 2px;
  position: relative;
  top: -4px;
`;

const NavLink = styled.button<{ $active?: boolean }>`
  all: unset;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: ${({ theme }) => theme.radii.md};
  border: 1px solid ${({ $active, theme }) => $active ? theme.colors.goldDim : theme.colors.border};
  background: ${({ $active, theme }) => $active ? theme.colors.goldBg : 'transparent'};
  font-size: ${({ theme }) => theme.fontSizes.nav};
  font-family: ${({ theme }) => theme.fonts.body};
  color: ${({ $active, theme }) => $active ? theme.colors.gold : theme.colors.textSecondary};
  transition: all 0.15s;

  &:hover {
    border-color: ${({ theme }) => theme.colors.goldDim};
    color: ${({ theme }) => theme.colors.gold};
  }
`;

const DownloadBadge = styled.div`
  position: relative;
  width: 28px;
  height: 28px;
`;

const IconToggle = styled.button<{ $active: boolean }>`
  all: unset;
  cursor: pointer;
  display: flex;
  align-items: center;
  color: ${({ $active, theme }) => $active ? theme.colors.gold : theme.colors.textMuted};
  transition: color 0.15s;

  &:hover {
    color: ${({ theme }) => theme.colors.text};
  }

  /* Keyboard focus: all: unset removes the browser outline (Button's pattern). */
  &:focus-visible {
    outline: none;
    box-shadow: 0 0 0 2px ${({ theme }) => theme.colors.goldDim};
  }
`;

const WarningDot = styled.div<{ $level: 'error' | 'warning' }>`
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: help;

  &:hover > .warning-tooltip {
    opacity: 1;
    visibility: visible;
  }
`;

const WarningCircle = styled.div<{ $level: 'error' | 'warning' }>`
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: ${({ $level, theme}) => $level === 'error' ? theme.colors.errorFill : theme.colors.warningFill};
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 700;
  font-family: ${({ theme }) => theme.fonts.body};
  color: ${({ $level, theme}) => $level === 'error' ? theme.colors.errorOnFill : theme.colors.warningOnFill};
`;

const WarningTooltip = styled.div`
  position: absolute;
  top: 100%;
  left: 50%;
  transform: translateX(-50%);
  padding-top: 8px;
  width: 300px;
  opacity: 0;
  visibility: hidden;
  transition: opacity 0.2s, visibility 0.2s;
  z-index: 50;

  /* Anchored under a pip near the left screen edge, the centered 300px box
   * pokes past the right edge of narrow viewports. Even hidden it extends the
   * document's scrollable area (visibility does not remove layout), giving
   * phones a horizontal pan wiggle. Pin it inside the viewport instead:
   * fixed placement contributes no scroll overflow. */
  @media (max-width: ${MOBILE_BREAKPOINT_PX}px) {
    position: fixed;
    top: 60px;
    left: 12px;
    right: 12px;
    width: auto;
    transform: none;
    padding-top: 0;
  }
`;

const WarningTooltipInner = styled.div`
  background: ${({ theme }) => theme.colors.surfaceElevated};
  border: 1px solid ${({ theme }) => theme.colors.goldDim};
  border-radius: ${({ theme }) => theme.radii.md};
  padding: 10px 12px;
  backdrop-filter: blur(8px);
  box-shadow: 0 8px 32px ${({ theme }) => theme.colors.shadowStrong};
  display: flex;
  flex-direction: column;
  gap: 6px;
`;

const WarningItem = styled.div<{ $level: 'error' | 'warning' }>`
  font-size: ${({ theme }) => theme.fontSizes.sm};
  font-family: ${({ theme }) => theme.fonts.body};
  /*
   * Text-only callout — palette-aware on-surface color so the message
   * stays semantic (red text for errors, amber text for warnings) without
   * the saturated solid-fill landing in your face. The header indicator
   * still uses the solid fill because that's the bit meant to *grab*
   * attention; the popover body just needs to be color-coded.
   */
  color: ${({ $level, theme}) => $level === 'error' ? theme.colors.errorOnSurface : theme.colors.warningOnSurface};
  line-height: 1.4;
  display: flex;
  align-items: flex-start;
  gap: 6px;

  &::before {
    content: '●';
    color: ${({ $level, theme}) => $level === 'error' ? theme.colors.errorOnSurface : theme.colors.warningOnSurface};
    flex-shrink: 0;
  }
`;

/*
 * Three-bar hamburger that morphs into an X when the mobile menu is open.
 * Pure CSS transforms (rotate the outer bars onto the diagonal, fade the
 * middle) so the open/close animation runs both ways without JS.
 */
const Hamburger = styled.span<{ $open: boolean }>`
  position: relative;
  display: block;
  width: 18px;
  height: 14px;

  span {
    position: absolute;
    left: 0;
    width: 100%;
    height: 2px;
    border-radius: 1px;
    background: currentColor;
    transition: transform 0.25s ease, opacity 0.2s ease, top 0.25s ease;
  }

  span:nth-child(1) {
    top: ${({ $open }) => ($open ? '6px' : '0')};
    transform: rotate(${({ $open }) => ($open ? '45deg' : '0')});
  }
  span:nth-child(2) {
    top: 6px;
    opacity: ${({ $open }) => ($open ? 0 : 1)};
  }
  span:nth-child(3) {
    top: ${({ $open }) => ($open ? '6px' : '12px')};
    transform: rotate(${({ $open }) => ($open ? '-45deg' : '0')});
  }
`;

const InstanceToggle = styled.button<{ $healthy: boolean; $active: boolean }>`
  all: unset;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2px;
  border-radius: 50%;
  transition: all 0.15s;

  &:hover {
    filter: brightness(1.2);
  }
`;

/* ---- icons (react-icons) ---- */

const SidebarIcon = () => <FiSidebar size={18} />;
const ClusterIcon = () => <MdHub size={16} />;
const StoreIcon = () => <FiDatabase size={16} />;
const ChatIcon = () => <FiMessageSquare size={16} />;
const StewardIcon = () => <MdAutoAwesome size={16} />;
const IntegrationsIcon = () => <FiLink size={16} />;

const ObservabilityIcon = () => <VscBug size={16} />;
const SettingsIcon = () => <FiSettings size={16} />;

function ProgressCircle({ count, percentage }: { count: number; percentage: number }) {
  const theme = useTheme() as Theme;
  const r = 10;
  const circ = 2 * Math.PI * r;
  const offset = circ * (1 - percentage / 100);

  return (
    <DownloadBadge>
      <svg width="28" height="28" viewBox="0 0 28 28">
        <circle cx="14" cy="14" r={r} fill="none" stroke={theme.colors.borderStrong} strokeWidth="2" />
        <circle
          cx="14" cy="14" r={r}
          fill="none" stroke={theme.colors.gold} strokeWidth="2"
          strokeDasharray={circ} strokeDashoffset={offset}
          strokeLinecap="round"
          transform="rotate(-90 14 14)"
          style={{ transition: 'stroke-dashoffset 0.3s ease-out' }}
        />
        <text x="14" y="14" textAnchor="middle" dominantBaseline="central" fill={theme.colors.gold} fontSize="8" fontFamily="monospace">
          {count}
        </text>
      </svg>
    </DownloadBadge>
  );
}

/* ---- component ---- */

export function HeaderNav({
  showHome = true,
  onHome,
  activeRoute = 'cluster',
  onNavigate,
  showSidebarToggle = false,
  sidebarVisible = true,
  onToggleSidebar,
  showMobileMenuToggle = false,
  mobileMenuOpen = false,
  onToggleMobileMenu,
  mobileRightOpen = false,
  onToggleMobileRight,
  compact = false,
  instanceCount = 0,
  instancesHealthy = true,
  downloadProgress = null,
  warnings = null,
  onOpenSettings,
  className,
}: HeaderNavProps) {
  // The steward link appears only while intelligent-fabric mode is on;
  // polling keeps it in step with Settings changes made on any node.
  const { data: stewardStatus } = useGetStewardStatusQuery(undefined, {
    pollingInterval: 30000,
  });
  const stewardEnabled = stewardStatus?.enabled ?? false;
  const { t } = useSkulkTranslation();
  const theme = useTheme() as Theme;
  const dispatch = useAppDispatch();
  const themeName = useAppSelector((s) => s.ui.theme);
  const toggleTheme = () => dispatch(uiActions.toggleTheme());
  // Observability panel is global UI state — the button toggles it open/closed and
  // visually reflects whether it's currently visible. Distinct from `activeRoute`
  // because the panel overlays the current route rather than navigating away.
  const observabilityPanelOpen = useAppSelector((s) => s.ui.observabilityPanelOpen);
  const openObservability = (tab?: 'live' | 'node' | 'traces', nodeId?: string) =>
    dispatch(uiActions.openObservability({ tab, nodeId }));
  const closeObservability = () => dispatch(uiActions.closeObservability());
  const navigate = (route: NavRoute) => {
    onNavigate?.(route);
    if (route === 'cluster') onHome?.();
  };

  return (
    <Nav className={className}>
      <LeftGroup>
        {showMobileMenuToggle && (
          <ToggleBtn
            variant="outline"
            size="lg"
            icon
            $active={mobileMenuOpen}
            onClick={onToggleMobileMenu}
            aria-label={t('header.toggleMobileMenu', 'Toggle mobile menu')}
            aria-pressed={mobileMenuOpen}
          >
            <Hamburger $open={mobileMenuOpen} aria-hidden>
              <span />
              <span />
              <span />
            </Hamburger>
          </ToggleBtn>
        )}
        {showSidebarToggle && (
          <IconToggle
            type="button"
            $active={sidebarVisible}
            onClick={onToggleSidebar}
            aria-label={t('header.toggleSidebar', 'Toggle sidebar')}
            aria-pressed={sidebarVisible}
          >
            <SidebarIcon />
          </IconToggle>
        )}
        <LogoBtn $disabled={!showHome} onClick={showHome ? () => navigate('cluster') : undefined}>
          <SkulkIcon size={32} color={theme.colors.text} />
          <LogoText>{t('header.brand', 'Skulk')}<VersionTag>{__APP_VERSION__}</VersionTag></LogoText>
        </LogoBtn>
        {warnings && warnings.items.length > 0 && (
          <WarningDot $level={warnings.level}>
            <WarningCircle $level={warnings.level}>!</WarningCircle>
            <WarningTooltip className="warning-tooltip">
              <WarningTooltipInner>
                {warnings.items.map((item, i) => (
                  <WarningItem key={i} $level={item.level}>{item.message}</WarningItem>
                ))}
              </WarningTooltipInner>
            </WarningTooltip>
          </WarningDot>
        )}
      </LeftGroup>

      <RightGroup>
        {downloadProgress && <ProgressCircle count={downloadProgress.count} percentage={downloadProgress.percentage} />}

        {!compact && (<>
        <NavLink $active={activeRoute === 'cluster'} onClick={() => navigate('cluster')}>
          <ClusterIcon /> {t('header.nav.cluster', 'Cluster')}
        </NavLink>

        <NavLink $active={activeRoute === 'model-store'} onClick={() => navigate('model-store')}>
          <StoreIcon /> {t('header.nav.modelStore', 'Model Store')}
        </NavLink>

        <NavLink $active={activeRoute === 'chat'} onClick={() => navigate('chat')}>
          <ChatIcon /> {t('header.nav.chat', 'Chat')}
        </NavLink>

        {stewardEnabled && (
          <NavLink $active={activeRoute === 'steward'} onClick={() => navigate('steward')}>
            <StewardIcon /> {t('header.nav.steward', 'Skulk')}
          </NavLink>
        )}

        <NavLink $active={activeRoute === 'integrations'} onClick={() => navigate('integrations')}>
          <IntegrationsIcon /> {t('header.nav.integrations', 'Integrations')}
        </NavLink>
        <NavLink $active={activeRoute === 'plugins'} onClick={() => navigate('plugins')}>
          <FiPackage /> {t('header.nav.plugins', 'Plugins')}
        </NavLink>
        </>)}

        {instanceCount > 0 && (
          <InstanceToggle
            $healthy={instancesHealthy}
            $active={mobileRightOpen}
            onClick={onToggleMobileRight}
            aria-label={t('header.toggleInstancesPanel', 'Toggle instances panel')}
            aria-pressed={mobileRightOpen}
          >
            <svg width="28" height="28" viewBox="0 0 28 28">
              <circle
                cx="14" cy="14" r="11"
                fill="none"
                stroke={instancesHealthy ? theme.colors.healthy : theme.colors.error}
                strokeWidth="2"
                opacity={mobileRightOpen ? 1 : 0.7}
              />
              <text
                x="14" y="14"
                textAnchor="middle"
                dominantBaseline="central"
                fill={instancesHealthy ? theme.colors.healthy : theme.colors.error}
                fontSize="13"
                fontFamily="'Outfit', sans-serif"
                fontWeight="700"
              >
                {instanceCount}
              </text>
            </svg>
          </InstanceToggle>
        )}

        {!compact && (<>
        <Button
          variant="ghost"
          size="lg"
          icon
          onClick={toggleTheme}
          aria-label={themeName === 'dark'
            ? t('header.switchToLightTheme', 'Switch to light theme')
            : t('header.switchToDarkTheme', 'Switch to dark theme')}
          title={themeName === 'dark'
            ? t('header.switchToLightTheme', 'Switch to light theme')
            : t('header.switchToDarkTheme', 'Switch to dark theme')}
        >
          {themeName === 'dark' ? <FiSun size={16} /> : <FiMoon size={16} />}
        </Button>

        <Button
          variant={observabilityPanelOpen ? 'outline' : 'ghost'}
          size="lg"
          icon
          onClick={() => (observabilityPanelOpen ? closeObservability() : openObservability())}
          aria-label={t('header.observability', 'Observability')}
          aria-pressed={observabilityPanelOpen}
          title={t('header.observability', 'Observability')}
        >
          <ObservabilityIcon />
        </Button>

        <Button
          variant="ghost"
          size="lg"
          icon
          onClick={() => onOpenSettings?.()}
          aria-label={t('header.settings', 'Settings')}
        >
          <SettingsIcon />
        </Button>
        </>)}
      </RightGroup>
    </Nav>
  );
}
