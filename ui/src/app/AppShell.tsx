import { useEffect, useRef } from 'react';
import { PanelLeftOpen, Settings } from 'lucide-react';
import { useUiStore } from '@/lib/stores/ui';
import { useInstanceStore } from '@/lib/stores/instances';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Sidebar } from '@/app/Sidebar';
import { Toolbar } from '@/app/Toolbar';
import { StatusBar } from '@/app/StatusBar';
import { CommandPalette } from '@/app/CommandPalette';
import { PageHost } from '@/app/PageHost';
import { SettingsSheet } from '@/features/settings/SettingsSheet';

/**
 * The window shell.
 *
 * Three fixed bands and one scrolling region, because this is an application
 * window, not a page: the sidebar and status bar never scroll away, and each pane
 * owns its own scroll container.
 *
 * ```
 * ┌─────────────────────────────────────────────┐
 * │ toolbar (48)                                │
 * ├──────────┬──────────────────────────────────┤
 * │ sidebar  │ page (own scroll)                │
 * │ (280)    │                                  │
 * ├──────────┴──────────────────────────────────┤
 * │ status bar (28)                             │
 * └─────────────────────────────────────────────┘
 * ```
 *
 * The sidebar is collapsible to zero width, and the only way back is the toggle
 * that replaces it — a 1.0 lesson: hiding a column behind a third control in the
 * far corner means nobody finds it again.
 */
export function AppShell() {
  const collapsed = useUiStore((state) => state.sidebarCollapsed);
  const setCollapsed = useUiStore((state) => state.setSidebarCollapsed);
  const width = useUiStore((state) => state.sidebarWidth);
  const setWidth = useUiStore((state) => state.setSidebarWidth);
  const setSettingsOpen = useUiStore((state) => state.setSettingsOpen);

  const dragging = useRef(false);

  // Sidebar resize. Pointer capture keeps the drag alive when the cursor leaves
  // the 6px handle, and the keyboard path is handled in the handle's own onKeyDown.
  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      if (!dragging.current) return;
      const next = Math.min(440, Math.max(224, event.clientX));
      setWidth(next);
    };
    const onUp = () => {
      dragging.current = false;
      document.body.style.cursor = '';
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
  }, [setWidth]);

  return (
    <div className="flex h-full min-w-0 flex-col bg-[var(--canvas)] text-[var(--text)]">
      <Toolbar />
      <div className="flex min-h-0 min-w-0 flex-1">
        {collapsed ? (
          <div className="flex w-[var(--sidebar-rail-w)] shrink-0 flex-col items-center border-e border-[var(--border)] bg-[var(--panel)] pt-2">
            <button
              type="button"
              onClick={() => setCollapsed(false)}
              aria-label={translate('sidebar.expand')}
              title={translate('sidebar.expand')}
              className={cn(
                'grid size-8 place-items-center rounded-[var(--radius-sm)] text-[length:var(--text-sm)] text-[var(--text-secondary)]',
                'hover:bg-[var(--hover)] hover:text-[var(--text)]',
                'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
              )}
            >
              <PanelLeftOpen size={17} aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={() => setSettingsOpen(true)}
              aria-label={translate('nav.settings')}
              title={translate('nav.settings')}
              className={cn(
                'mt-1 grid size-8 place-items-center rounded-[var(--radius-sm)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]',
                'hover:bg-[var(--hover)] hover:text-[var(--text)]',
                'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
              )}
            >
              <Settings size={17} aria-hidden="true" />
            </button>
          </div>
        ) : (
          <>
            <Sidebar width={width} />
            {/* Resize handle: a real separator with a keyboard path. */}
            <div
              role="separator"
              aria-orientation="vertical"
              aria-label={translate('sidebar.resize')}
              aria-valuemin={224}
              aria-valuemax={440}
              aria-valuenow={width}
              tabIndex={0}
              onPointerDown={(event) => {
                event.preventDefault();
                dragging.current = true;
                document.body.style.cursor = 'col-resize';
              }}
              onKeyDown={(event) => {
                if (event.key === 'ArrowLeft') {
                  event.preventDefault();
                  setWidth(Math.max(224, width - 16));
                } else if (event.key === 'ArrowRight') {
                  event.preventDefault();
                  setWidth(Math.min(440, width + 16));
                }
              }}
              className={cn(
                'w-[3px] shrink-0 cursor-col-resize bg-transparent',
                'hover:bg-[var(--border-strong)] focus-visible:bg-[var(--border-focus)] focus-visible:outline-none',
                'transition-colors duration-100',
              )}
            />
          </>
        )}

        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <PageHost />
        </main>
      </div>
      <StatusBar />
      <CommandPalette />
      <SettingsSheet />
    </div>
  );
}

/** Re-exported for the toolbar's instance count. */
export { useInstanceStore };
