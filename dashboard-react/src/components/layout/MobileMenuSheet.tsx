import styled from 'styled-components';
import { FiSettings, FiDatabase, FiMessageSquare, FiSun, FiMoon, FiLink, FiPackage } from 'react-icons/fi';
import { MdHub, MdAutoAwesome } from 'react-icons/md';
import { VscBug } from 'react-icons/vsc';
import { useAppDispatch, useAppSelector } from '../../store/hooks';
import { uiActions } from '../../store/slices/uiSlice';
import { useSkulkTranslation } from '../../i18n/tolgee';
import { useGetStewardStatusQuery } from '../../store/endpoints/steward';
import type { NavRoute } from './HeaderNav';

/**
 * Props for the phone-width navigation sheet that replaces the header's
 * inline nav links and icon buttons below the mobile breakpoint.
 */
export interface MobileMenuSheetProps {
  /** Whether the sheet is expanded. The component stays mounted so the slide animation can run both ways. */
  open: boolean;
  /** The currently active route, highlighted in the menu. */
  activeRoute: NavRoute;
  /** Navigate to a route. The sheet closes itself via `onClose` after calling this. */
  onNavigate: (route: NavRoute) => void;
  /** Open the settings dialog. */
  onOpenSettings: () => void;
  /** Close the sheet (backdrop tap, or after any menu action). */
  onClose: () => void;
}

const Backdrop = styled.div<{ $open: boolean }>`
  position: fixed;
  inset: 0;
  z-index: 18;
  background: ${({ theme }) => theme.colors.shadowStrong};
  /* Standard flyover treatment (SettingsPanel / ObservabilityPanel): the
   * content behind the menu blurs, not just dims. */
  backdrop-filter: blur(2px);
  opacity: ${({ $open }) => ($open ? 1 : 0)};
  visibility: ${({ $open }) => ($open ? 'visible' : 'hidden')};
  transition: opacity 0.2s ease, visibility 0.2s;
`;

const Sheet = styled.nav<{ $open: boolean }>`
  /*
   * Anchored to the bottom of the header wrapper (position: relative in
   * App) rather than the viewport, so anything rendered above the header
   * (the reconnect banner) pushes the sheet down with it.
   */
  position: absolute;
  top: 100%;
  left: 0;
  right: 0;
  z-index: 19; /* under the header (20) so the sheet emerges from beneath it */
  padding: 8px 12px 12px;
  /* Fully opaque: the header token is translucent by design (it sits over
   * the page gradient), which made the menu see-through over content. */
  background: ${({ theme }) => theme.colors.surface};
  border-bottom: 1px solid ${({ theme }) => theme.colors.border};
  box-shadow: 0 12px 32px ${({ theme }) => theme.colors.shadowStrong};
  display: flex;
  flex-direction: column;
  gap: 4px;
  transform: translateY(${({ $open }) => ($open ? '0' : '-100%')});
  /*
   * visibility flips AFTER the slide-up finishes (it transitions discretely
   * at the end), so the close animation stays visible while the closed
   * sheet, which now rests exactly over the header, stops intercepting
   * pointer events aimed at the hamburger.
   */
  visibility: ${({ $open }) => ($open ? 'visible' : 'hidden')};
  transition: transform 0.25s ease, visibility 0.25s;
`;

const MenuRow = styled.button<{ $active?: boolean }>`
  all: unset;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border-radius: ${({ theme }) => theme.radii.md};
  font-size: ${({ theme }) => theme.fontSizes.nav};
  font-family: ${({ theme }) => theme.fonts.body};
  color: ${({ $active, theme }) => ($active ? theme.colors.gold : theme.colors.text)};
  background: ${({ $active, theme }) => ($active ? theme.colors.goldBg : 'transparent')};
  border: 1px solid ${({ $active, theme }) => ($active ? theme.colors.goldDim : 'transparent')};

  &:active {
    background: ${({ theme }) => theme.colors.goldBg};
  }

  /* Keyboard focus: all: unset removes the browser outline (Button's pattern). */
  &:focus-visible {
    outline: none;
    box-shadow: 0 0 0 2px ${({ theme }) => theme.colors.goldDim};
  }
`;

const Divider = styled.div`
  height: 1px;
  margin: 6px 4px;
  background: ${({ theme }) => theme.colors.border};
`;

/**
 * Full-width sheet that slides down from under the header at phone width,
 * carrying everything the desktop header shows inline: the three routes,
 * the observability panel toggle, settings, and the theme switch. Every
 * action closes the sheet so the content behind it is immediately visible.
 */
export function MobileMenuSheet({ open, activeRoute, onNavigate, onOpenSettings, onClose }: MobileMenuSheetProps) {
  const { data: stewardStatus } = useGetStewardStatusQuery(undefined, {
    pollingInterval: 30000,
  });
  const stewardEnabled = stewardStatus?.enabled ?? false;
  const { t } = useSkulkTranslation();
  const dispatch = useAppDispatch();
  const themeName = useAppSelector((s) => s.ui.theme);
  const observabilityPanelOpen = useAppSelector((s) => s.ui.observabilityPanelOpen);

  const go = (route: NavRoute) => {
    onNavigate(route);
    onClose();
  };

  return (
    <>
      <Backdrop $open={open} onClick={onClose} aria-hidden />
      <Sheet $open={open} aria-hidden={!open} aria-label={t('header.mobileMenu', 'Menu')}>
        <MenuRow $active={activeRoute === 'cluster'} onClick={() => go('cluster')} tabIndex={open ? 0 : -1}>
          <MdHub size={18} /> {t('header.nav.cluster', 'Cluster')}
        </MenuRow>
        <MenuRow $active={activeRoute === 'model-store'} onClick={() => go('model-store')} tabIndex={open ? 0 : -1}>
          <FiDatabase size={18} /> {t('header.nav.modelStore', 'Model Store')}
        </MenuRow>
        <MenuRow $active={activeRoute === 'chat'} onClick={() => go('chat')} tabIndex={open ? 0 : -1}>
          <FiMessageSquare size={18} /> {t('header.nav.chat', 'Chat')}
        </MenuRow>
        {stewardEnabled && (
          <MenuRow $active={activeRoute === 'steward'} onClick={() => go('steward')} tabIndex={open ? 0 : -1}>
            <MdAutoAwesome size={18} /> {t('header.nav.steward', 'Skulk')}
          </MenuRow>
        )}
        <MenuRow $active={activeRoute === 'integrations'} onClick={() => go('integrations')} tabIndex={open ? 0 : -1}>
          <FiLink size={18} /> {t('header.nav.integrations', 'Integrations')}
        </MenuRow>
        <MenuRow $active={activeRoute === 'plugins'} onClick={() => go('plugins')} tabIndex={open ? 0 : -1}>
          <FiPackage size={18} /> {t('header.nav.plugins', 'Plugins')}
        </MenuRow>
        <Divider />
        <MenuRow
          $active={observabilityPanelOpen}
          onClick={() => {
            dispatch(observabilityPanelOpen ? uiActions.closeObservability() : uiActions.openObservability({}));
            onClose();
          }}
          tabIndex={open ? 0 : -1}
        >
          <VscBug size={18} /> {t('header.observability', 'Observability')}
        </MenuRow>
        <MenuRow
          onClick={() => {
            onOpenSettings();
            onClose();
          }}
          tabIndex={open ? 0 : -1}
        >
          <FiSettings size={18} /> {t('header.settings', 'Settings')}
        </MenuRow>
        <MenuRow
          onClick={() => {
            dispatch(uiActions.toggleTheme());
            onClose();
          }}
          tabIndex={open ? 0 : -1}
        >
          {themeName === 'dark' ? <FiSun size={18} /> : <FiMoon size={18} />}
          {themeName === 'dark'
            ? t('header.switchToLightTheme', 'Switch to light theme')
            : t('header.switchToDarkTheme', 'Switch to dark theme')}
        </MenuRow>
      </Sheet>
    </>
  );
}
