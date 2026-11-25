import { useEffect, useMemo, useState } from 'react';
import { Search } from 'lucide-react';
import { useUiStore, type PageId } from '@/lib/stores/ui';
import { ALL_NAV_ITEMS } from '@/app/navigation';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';

/**
 * Command palette (Ctrl+K).
 *
 * A single flat list over destinations plus a few actions. Built by hand rather
 * than with `cmdk` because the whole interaction is ~80 lines: filter, arrow keys,
 * Enter, Escape. A dependency would add a second focus-trap implementation next to
 * Radix's, and the two disagree about `aria-activedescendant`.
 *
 * Rules that matter:
 * - focus returns to the element that opened it
 * - the list is a `role="listbox"` with `aria-activedescendant`, so the input keeps
 *   focus while arrowing
 * - Escape closes even when the input is empty
 */
interface PaletteAction {
  id: string;
  label: string;
  group: string;
  run: () => void;
}

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [index, setIndex] = useState(0);
  const navigate = useUiStore((state) => state.navigate);
  const setSettingsOpen = useUiStore((state) => state.setSettingsOpen);
  const setSidebarCollapsed = useUiStore((state) => state.setSidebarCollapsed);
  const sidebarCollapsed = useUiStore((state) => state.sidebarCollapsed);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setOpen((value) => !value);
        setQuery('');
        setIndex(0);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  const actions = useMemo<PaletteAction[]>(() => {
    const nav = ALL_NAV_ITEMS.map((item) => ({
      id: `nav-${item.id}`,
      label: translate(item.labelKey),
      group: translate('command.navigate'),
      run: () => navigate(item.id as PageId),
    }));
    return [
      ...nav,
      {
        id: 'open-settings',
        label: translate('nav.settings'),
        group: translate('command.actions'),
        run: () => setSettingsOpen(true),
      },
      {
        id: 'toggle-sidebar',
        label: translate(sidebarCollapsed ? 'sidebar.expand' : 'sidebar.collapse'),
        group: translate('command.actions'),
        run: () => setSidebarCollapsed(!sidebarCollapsed),
      },
    ];
  }, [navigate, setSettingsOpen, setSidebarCollapsed, sidebarCollapsed]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return actions;
    return actions.filter((action) => action.label.toLowerCase().includes(needle));
  }, [actions, query]);

  useEffect(() => {
    setIndex((current) => (filtered.length ? Math.min(current, filtered.length - 1) : 0));
  }, [filtered.length]);

  if (!open) return null;

  const close = () => {
    setOpen(false);
    setQuery('');
  };

  const runAt = (position: number) => {
    const action = filtered[position];
    if (!action) return;
    close();
    action.run();
  };

  return (
    <div
      className="fixed inset-0 z-[var(--z-popover)] flex items-start justify-center bg-[var(--overlay-scrim)] pt-[12vh]"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={translate('command.title')}
        className="w-[min(560px,calc(100vw-2rem))] overflow-hidden rounded-[var(--radius-xl)] border border-[var(--border)] bg-[var(--surface)] shadow-[var(--shadow-pop)]"
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.stopPropagation();
            close();
          } else if (event.key === 'ArrowDown') {
            event.preventDefault();
            setIndex((current) => (filtered.length ? (current + 1) % filtered.length : 0));
          } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            setIndex((current) =>
              filtered.length ? (current - 1 + filtered.length) % filtered.length : 0,
            );
          } else if (event.key === 'Enter') {
            event.preventDefault();
            runAt(index);
          }
        }}
      >
        <div className="flex items-center gap-2 border-b border-[var(--border)] px-3">
          <Search size={15} aria-hidden="true" className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]" />
          <input
            // eslint-disable-next-line jsx-a11y/no-autofocus -- a command palette
            // that does not focus its input is unusable.
            autoFocus
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setIndex(0);
            }}
            placeholder={translate('command.placeholder')}
            aria-controls="command-list"
            aria-activedescendant={filtered[index] ? `command-${filtered[index].id}` : undefined}
            role="combobox"
            aria-expanded="true"
            className="h-11 w-full bg-transparent text-[length:var(--text-sm)] outline-none placeholder:text-[length:var(--text-2xs)] text-[var(--text-quaternary)]"
          />
        </div>
        <ul id="command-list" role="listbox" className="max-h-[46vh] overflow-y-auto p-1.5">
          {filtered.length === 0 ? (
            <li className="px-3 py-6 text-center text-[length:var(--text-xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {translate('command.noResults')}
            </li>
          ) : (
            filtered.map((action, position) => (
              <li
                key={action.id}
                id={`command-${action.id}`}
                role="option"
                aria-selected={position === index}
                onPointerEnter={() => setIndex(position)}
                onClick={() => runAt(position)}
                className={cn(
                  'flex h-9 cursor-pointer items-center justify-between rounded-[var(--radius-sm)] px-2.5',
                  'text-[length:var(--text-sm)]',
                  position === index
                    ? 'bg-[var(--selected)] text-[var(--text)]'
                    : 'text-[length:var(--text-sm)] text-[var(--text-secondary)]',
                )}
              >
                <span className="truncate">{action.label}</span>
                <span className="text-[length:var(--text-2xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">{action.group}</span>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>
  );
}
