import { create } from 'zustand';
import { detectLocale, setLocale as persistLocale, type Locale } from '@/lib/i18n';

/**
 * UI state that is not server data.
 *
 * Split by lifetime:
 * - `theme`, `locale`, sidebar width: persisted, survive reload.
 * - `route`, `sidebarOpen`: session state, reset on launch.
 * - inspector width/visibility: persisted per the chat settings contract.
 *
 * Server data lives in TanStack Query caches, not here — two stores for one
 * dataset is how 1.0 ended up with a panel and a page disagreeing.
 */

export type ThemePreference = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

/** Pages that own a route. Modal surfaces (settings) are not routes. */
export type PageId =
  | 'chat'
  | 'compare'
  | 'agent'
  | 'dashboard'
  | 'models'
  | 'params'
  | 'presets'
  | 'knowledge'
  | 'serving'
  | 'observability'
  | 'settings'
  | 'help';

const STORAGE = {
  theme: 'lunarshroud_theme',
  sidebarWidth: 'lunarshroud_sidebar_width',
  sidebarCollapsed: 'lunarshroud_sidebar_collapsed',
  inspectorWidth: 'lunarshroud_inspector_width',
  inspectorOpen: 'lunarshroud_inspector_open',
  chatRightView: 'lunarshroud_chat_inspector_view',
} as const;

function readNumber(key: string, fallback: number): number {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? value : fallback;
  } catch {
    return fallback;
  }
}

/**
 * Whether this machine has ever stored a layout width.
 *
 * The backend `chat_sidebar_width` / `chat_panel_width` keys exist so a layout
 * survives moving to another machine (see the comment on them in `config.py`). They
 * were **never read** — the settings sheet wrote them and layout came from
 * localStorage only. `hydrateLayoutFromConfig` below closes that gap, and this
 * predicate is what makes it non-destructive: an existing local value always wins,
 * because it is the one the user last dragged on this machine.
 */
function hasStored(key: string): boolean {
  try {
    return localStorage.getItem(key) !== null;
  } catch {
    return false;
  }
}

function readBool(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    return raw === '1' || raw === 'true';
  } catch {
    return fallback;
  }
}

function write(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // A read-only profile must not break the switch itself.
  }
}

/** Read the stored theme preference, tolerating a corrupt value. */
export function readThemePreference(): ThemePreference {
  try {
    const raw = localStorage.getItem(STORAGE.theme);
    if (raw === 'light' || raw === 'dark' || raw === 'system') return raw;
  } catch {
    // fall through to system
  }
  return 'system';
}

export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference === 'light' || preference === 'dark') return preference;
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

interface UiState {
  route: PageId;
  themePreference: ThemePreference;
  resolvedTheme: ResolvedTheme;
  locale: Locale;
  sidebarWidth: number;
  sidebarCollapsed: boolean;
  inspectorWidth: number;
  inspectorOpen: boolean;
  settingsOpen: boolean;
  /** Which settings section to show when the sheet opens. */
  settingsTab: string;
  /** 'config' = how this conversation generates, 'integrations' = what the model can use. */
  chatRightView: 'config' | 'integrations';

  navigate: (page: PageId) => void;
  setThemePreference: (preference: ThemePreference) => void;
  syncResolvedTheme: () => void;
  setLocale: (locale: Locale) => void;
  setSidebarWidth: (width: number) => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  setInspectorWidth: (width: number) => void;
  setInspectorOpen: (open: boolean) => void;
  hydrateLayoutFromConfig: (config: { sidebar?: number; panel?: number }) => void;
  setSettingsOpen: (open: boolean) => void;
  setSettingsTab: (tab: string) => void;
  openSettings: (tab?: string) => void;
  setChatRightView: (view: 'config' | 'integrations') => void;
}

const initialPreference = readThemePreference();

export const useUiStore = create<UiState>((set, get) => ({
  route: 'chat',
  themePreference: initialPreference,
  resolvedTheme: resolveTheme(initialPreference),
  locale: detectLocale(),
  sidebarWidth: readNumber(STORAGE.sidebarWidth, 272),
  sidebarCollapsed: readBool(STORAGE.sidebarCollapsed, false),
  inspectorWidth: readNumber(STORAGE.inspectorWidth, 320),
  inspectorOpen: readBool(STORAGE.inspectorOpen, false),
  settingsOpen: false,
  settingsTab: 'general',
  chatRightView: (() => {
    try {
      return localStorage.getItem(STORAGE.chatRightView) === 'integrations' ? 'integrations' : 'config';
    } catch {
      return 'config';
    }
  })(),

  /**
   * Navigate to a page.
   *
   * `settings` is an **overlay**, not a route (`PageHost` maps it to the last
   * real page so closing the sheet returns to a live page). Before this branch
   * existed, `navigate('settings')` merely set a route that rendered the chat
   * page underneath — the button appeared to do nothing. Every "open settings"
   * button in the app (dashboard, models, help) used that path.
   */
  navigate: (page) => {
    if (page === 'settings') {
      set({ settingsOpen: true });
      return;
    }
    set({ route: page });
  },

  setThemePreference: (preference) => {
    write(STORAGE.theme, preference);
    set({ themePreference: preference, resolvedTheme: resolveTheme(preference) });
  },

  /** Called by the media-query listener when the OS preference changes. */
  syncResolvedTheme: () => {
    const { themePreference } = get();
    if (themePreference !== 'system') return;
    set({ resolvedTheme: resolveTheme('system') });
  },

  setLocale: (locale) => {
    persistLocale(locale);
    set({ locale });
  },

  setSidebarWidth: (width) => {
    write(STORAGE.sidebarWidth, String(width));
    set({ sidebarWidth: width });
  },

  setSidebarCollapsed: (collapsed) => {
    write(STORAGE.sidebarCollapsed, collapsed ? '1' : '0');
    set({ sidebarCollapsed: collapsed });
  },

  setInspectorWidth: (width) => {
    write(STORAGE.inspectorWidth, String(width));
    set({ inspectorWidth: width });
  },

  /**
   * Seed layout widths from backend config, but only where this machine has no
   * stored value.
   *
   * The config keys exist so a layout survives moving to another machine; before
   * this they were written by the settings sheet and never read, so the promise was
   * empty. A local value always wins: it is what the user last dragged *here*, and
   * overwriting it on every launch would make resizing appear not to stick.
   *
   * `0` is the config's "unset" sentinel (see `config.py`), not a width.
   */
  hydrateLayoutFromConfig: (config: { sidebar?: number; panel?: number }) => {
    const patch: { sidebarWidth?: number; inspectorWidth?: number } = {};
    if (!hasStored(STORAGE.sidebarWidth) && (config.sidebar ?? 0) > 0) {
      patch.sidebarWidth = config.sidebar;
    }
    if (!hasStored(STORAGE.inspectorWidth) && (config.panel ?? 0) > 0) {
      patch.inspectorWidth = config.panel;
    }
    if (Object.keys(patch).length) set(patch);
  },

  setInspectorOpen: (open) => {
    write(STORAGE.inspectorOpen, open ? '1' : '0');
    set({ inspectorOpen: open });
  },

  setSettingsOpen: (open) => set({ settingsOpen: open }),

  setSettingsTab: (tab) => set({ settingsTab: tab, settingsOpen: true }),

  openSettings: (tab) => set(tab ? { settingsOpen: true, settingsTab: tab } : { settingsOpen: true }),

  setChatRightView: (view) => {
    write(STORAGE.chatRightView, view);
    set({ chatRightView: view });
  },
}));

export { STORAGE as UI_STORAGE_KEYS };
