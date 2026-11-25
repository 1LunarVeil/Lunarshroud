import {
  Bot,
  BookOpenText,
  Boxes,
  CircleHelp,
  Columns2,
  Gauge,
  MessageSquare,
  Radio,
  Server,
  SlidersHorizontal,
  Sparkles,
  type LucideIcon,
} from 'lucide-react';
import type { PageId } from '@/lib/stores/ui';

/**
 * Navigation model.
 *
 * Groups order the sidebar. The grouping is by *intent*, not by implementation:
 * "converse" is what you do with a model, "serve" is what you expose, "resources"
 * is what you configure, "diagnose" is what you look at when something is wrong.
 *
 * `params` and `settings` are deliberately in different places: `params` is how
 * you launch the *next* instance (a destination), `settings` is the application's
 * own configuration (a sheet, opened from the footer).
 */
export interface NavItem {
  id: PageId;
  icon: LucideIcon;
  labelKey: string;
  /** Shortcut hint shown in the row and used by the command palette. */
  hint?: string;
}

export interface NavGroup {
  key: string;
  labelKey: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    key: 'converse',
    labelKey: 'nav.groupRun',
    items: [
      { id: 'chat', icon: MessageSquare, labelKey: 'nav.chat' },
      { id: 'compare', icon: Columns2, labelKey: 'nav.compare' },
      { id: 'agent', icon: Bot, labelKey: 'nav.agent' },
      { id: 'dashboard', icon: Gauge, labelKey: 'nav.dashboard' },
    ],
  },
  {
    key: 'serve',
    labelKey: 'nav.groupServing',
    items: [{ id: 'serving', icon: Radio, labelKey: 'nav.api_server' }],
  },
  {
    key: 'resources',
    labelKey: 'nav.groupResources',
    items: [
      { id: 'models', icon: Boxes, labelKey: 'nav.models' },
      { id: 'presets', icon: Sparkles, labelKey: 'nav.presets' },
      { id: 'knowledge', icon: BookOpenText, labelKey: 'nav.knowledge' },
    ],
  },
  {
    key: 'diagnose',
    labelKey: 'nav.groupDiagnostics',
    items: [{ id: 'observability', icon: Server, labelKey: 'nav.observability' }],
  },
];

/** Destinations reachable from the toolbar/footer/command palette but not listed. */
export const SECONDARY_ITEMS: NavItem[] = [
  { id: 'params', icon: SlidersHorizontal, labelKey: 'nav.param_editor' },
  { id: 'help', icon: CircleHelp, labelKey: 'nav.help' },
];

export const ALL_NAV_ITEMS: NavItem[] = [
  ...NAV_GROUPS.flatMap((group) => group.items),
  ...SECONDARY_ITEMS,
];

export function navItemFor(id: PageId): NavItem | undefined {
  return ALL_NAV_ITEMS.find((item) => item.id === id);
}
