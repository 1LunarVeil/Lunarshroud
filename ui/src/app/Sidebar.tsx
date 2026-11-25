import { Settings } from 'lucide-react';
import { useUiStore } from '@/lib/stores/ui';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { NAV_GROUPS } from '@/app/navigation';

/**
 * Sidebar: grouped navigation, with room for a page-owned list beneath it.
 *
 * Structure (top to bottom):
 *   1. brand
 *   2. nav groups
 *   3. the current page's list  ← filled by `SidebarListSlot`
 *   4. footer: settings
 *
 * The list belongs *inside* the sidebar rather than as a separate column. A third
 * column splits the window into three unrelated strips, which is exactly the 1.0
 * problem the DESIGN_1.0 revision notes describe.
 */
export function Sidebar({ width }: { width: number }) {
  const route = useUiStore((state) => state.route);
  const navigate = useUiStore((state) => state.navigate);
  const setSettingsOpen = useUiStore((state) => state.setSettingsOpen);

  return (
    <nav
      aria-label={translate('nav.primary')}
      style={{ width }}
      // Below 720px the sidebar overlays the content instead of shrinking it. A 272px
      // column in a 320px window leaves 48px for the transcript, which is unusable and
      // also overflows. The desktop minimum window is 1024px, so this only matters for
      // the reflow audit and for a user who drags the window very small.
      className="flex shrink-0 flex-col border-e border-[var(--border)] bg-[var(--panel)] max-[720px]:fixed max-[720px]:inset-y-0 max-[720px]:start-0 max-[720px]:z-[var(--z-sidebar)]"
    >
      <div className="flex items-center gap-2 px-4 pb-1 pt-4">
        <img src="./logo.png" alt="" aria-hidden="true" className="size-5 rounded-[var(--radius-xs)]" />
        <span className="truncate text-[length:var(--text-sm)] font-semibold tracking-[var(--tracking-tight)]">
          LunarShroud
        </span>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-2">
        {NAV_GROUPS.map((group) => (
          <div key={group.key} className="mb-3">
            <p className="px-2 pb-1 text-[length:var(--text-2xs)] font-medium text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {translate(group.labelKey)}
            </p>
            <ul className="flex flex-col gap-[2px]">
              {group.items.map((item) => {
                const active = route === item.id;
                const Icon = item.icon;
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      data-testid={`nav-${item.id}`}
                      aria-current={active ? 'page' : undefined}
                      onClick={() => navigate(item.id)}
                      className={cn(
                        'flex h-[30px] w-full items-center gap-2.5 rounded-[var(--radius-md)] ps-2.5 pe-2 text-start',
                        'text-[length:var(--text-sm)] transition-colors duration-100',
                        'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
                        active
                          ? 'bg-[var(--selected)] font-medium text-[var(--text)]'
                          : 'text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--hover)] hover:text-[var(--text)]',
                      )}
                    >
                      <Icon
                        size={16}
                        aria-hidden="true"
                        className={active ? 'text-[var(--accent-text)]' : 'text-[length:var(--text-xs)] text-[var(--text-tertiary)]'}
                      />
                      <span className="truncate">{translate(item.labelKey)}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>

      {/* The page's own list is rendered here by the page, via a portal target. */}
      <div id="sidebar-list-slot" className="min-h-0 shrink-0 overflow-hidden empty:hidden" />

      <div className="border-t border-[var(--border)] p-2">
        <button
          type="button"
          data-testid="nav-settings"
          onClick={() => setSettingsOpen(true)}
          className={cn(
            'flex h-[30px] w-full items-center gap-2.5 rounded-[var(--radius-md)] ps-2.5 pe-2',
            'text-[length:var(--text-sm)] text-[length:var(--text-sm)] text-[var(--text-secondary)]',
            'hover:bg-[var(--hover)] hover:text-[var(--text)]',
            'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
          )}
        >
          <Settings size={16} aria-hidden="true" className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]" />
          <span>{translate('nav.settings')}</span>
        </button>
      </div>
    </nav>
  );
}
