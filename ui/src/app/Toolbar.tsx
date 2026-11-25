import { ChevronDown, PanelRight, Settings, StopCircle } from 'lucide-react';
import { useState } from 'react';
import { useUiStore } from '@/lib/stores/ui';
import { useInstanceStore, selectRunningCount } from '@/lib/stores/instances';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { instancesApi } from '@/lib/api';
import { toast } from 'sonner';
import { localizeBackendError } from '@/lib/i18n';
import { ThemeToggle } from '@/app/ThemeToggle';
import { LocaleToggle } from '@/app/LocaleToggle';

/**
 * Toolbar.
 *
 * Centred model capsule + window-level actions. The capsule is the single
 * strongest element in the window only when a model is loaded; with nothing
 * loaded it reads as a quiet prompt ("Choose a model"), because an empty app
 * should ask for the one thing it needs rather than show zeroes.
 *
 * The stop button lives here rather than on the dashboard: stopping the model is
 * the action a user reaches for *while chatting*, and making them change pages to
 * find it was a 1.0 complaint.
 */
export function Toolbar() {
  const navigate = useUiStore((state) => state.navigate);
  const route = useUiStore((state) => state.route);
  const instances = useInstanceStore((state) => state.instances);
  const runningCount = useInstanceStore(selectRunningCount);
  const [stopping, setStopping] = useState(false);
  const inspectorOpen = useUiStore((state) => state.inspectorOpen);
  const setInspectorOpen = useUiStore((state) => state.setInspectorOpen);
  const setSettingsOpen = useUiStore((state) => state.setSettingsOpen);
  const setSettingsTab = useUiStore((state) => state.openSettings);

  const active = instances.find((instance) => instance.status === 'running' && instance.serves_chat) ?? null;

  const stopActive = async () => {
    if (!active) return;
    setStopping(true);
    try {
      await instancesApi.stop(active.id);
      toast.success(translate('dash.stopRequested', { name: active.name }));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    } finally {
      setStopping(false);
    }
  };

  return (
    <header className="drag-region flex h-[var(--toolbar-h)] shrink-0 items-center gap-2 border-b border-[var(--border)] bg-[var(--panel)] px-3">
      {/*
        Three bands: breadcrumb (hugs its text), the model capsule (centred in the
        remaining space, capped), and the actions. The previous version had
        `flex-1` on both the left and right bands, which pushed the capsule into a
        wide dead zone and left a large empty gap under the breadcrumb — the
        "功能键间隔很大、浪费空间" complaint. `me-auto` on the breadcrumb gives the
        capsule its centring without a third greedy column.
      */}
      <div className="flex min-w-0 max-w-[12rem] shrink items-center">
        <span className="truncate text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
          {translate(`nav.${route}`)}
        </span>
      </div>

      {/* Model capsule: centred via auto margins, never wider than 36rem. */}
      <div className="no-drag mx-auto flex min-w-0 max-w-[36rem] flex-1 justify-center">
        <div
          className={cn(
            'flex h-[30px] min-w-0 items-center gap-1 rounded-[var(--radius-pill)] border ps-1 pe-1',
            active
              ? 'border-[var(--border)] bg-[var(--surface)] shadow-[var(--shadow-sm)]'
              : 'border-dashed border-[var(--border-strong)] bg-transparent',
          )}
        >
          <button
            type="button"
            data-testid="model-capsule"
            onClick={() => navigate('models')}
            title={active ? active.model_path : translate('app.modelCapsuleIdle')}
            className={cn(
              // Explicit 24px height: the pill is 30px tall, but a text button inside it
              // collapsed to its 18.6px line box and failed the WCAG 2.5.8 target floor.
              'flex h-[24px] min-w-0 items-center gap-2 rounded-[var(--radius-pill)] ps-2.5 pe-2',
              'text-[length:var(--text-xs)] transition-colors',
              'hover:bg-[var(--hover)]',
              'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
            )}
          >
            <span
              aria-hidden="true"
              className={cn(
                'size-1.5 shrink-0 rounded-full',
                active ? 'bg-[var(--success)]' : 'bg-[var(--text-quaternary)]',
              )}
            />
            <span className={cn('truncate', active ? 'text-[var(--text)]' : 'text-[length:var(--text-xs)] text-[var(--text-tertiary)]')}>
              {active
                ? active.name || translate('app.modelCapsuleLoaded')
                : translate('app.modelCapsuleIdle')}
            </span>
            <ChevronDown size={13} aria-hidden="true" className="shrink-0 text-[length:var(--text-xs)] text-[var(--text-tertiary)]" />
          </button>
          {active ? (
            <button
              type="button"
              data-testid="stop-active-model"
              onClick={stopActive}
              disabled={stopping}
              aria-label={translate('chat.stop')}
              title={translate('chat.stop')}
              className={cn(
                // 24px, not 20: WCAG 2.5.8 sets a 24x24 CSS px floor for a pointer target.
                'grid size-[24px] shrink-0 place-items-center rounded-full',
                'text-[length:var(--text-xs)] text-[var(--text-tertiary)] hover:bg-[var(--danger-subtle)] hover:text-[var(--danger)]',
                'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
                'disabled:cursor-not-allowed disabled:text-[var(--text-quaternary)]',
              )}
            >
              <StopCircle size={15} aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </div>

      <div className="no-drag ms-auto flex shrink-0 items-center gap-1">
        {runningCount > 0 ? (
          <span
            className="me-1 hidden items-center gap-1.5 rounded-[var(--radius-pill)] bg-[var(--success-subtle)] px-2 py-[2px] text-[length:var(--text-2xs)] font-medium text-[var(--success)] sm:inline-flex"
            title={translate('app.instancesRunning', { count: runningCount })}
          >
            <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
            {runningCount}
          </span>
        ) : null}
        {/*
          Inspector toggle. It is on the toolbar rather than inside the chat page
          because the panel changes the width of the transcript and users look for
          that control next to the window chrome, not buried in a message.
        */}
        <button
          type="button"
          data-testid="toggle-inspector"
          aria-pressed={inspectorOpen}
          aria-label={translate('chat.inspector')}
          title={translate('chat.inspector')}
          onClick={() => setInspectorOpen(!inspectorOpen)}
          className={cn(
            'grid size-8 place-items-center rounded-[var(--radius-sm)]',
            inspectorOpen
              ? 'bg-[var(--selected)] text-[var(--accent-text)]'
              : 'text-[var(--text-tertiary)] hover:bg-[var(--hover)] hover:text-[var(--text)]',
          )}
        >
          <PanelRight size={16} aria-hidden="true" />
        </button>
        {/*
          Settings is reachable from the toolbar as well as the sidebar footer:
          with the sidebar collapsed the footer disappears, and a user who hid the
          sidebar had no way back to settings at all (2.0.0).
        */}
        <button
          type="button"
          data-testid="toolbar-settings"
          aria-label={translate('nav.settings')}
          title={translate('nav.settings')}
          onClick={() => {
            setSettingsTab('general');
            setSettingsOpen(true);
          }}
          className={cn(
            'grid size-8 place-items-center rounded-[var(--radius-sm)]',
            'text-[var(--text-tertiary)] hover:bg-[var(--hover)] hover:text-[var(--text)]',
          )}
        >
          <Settings size={16} aria-hidden="true" />
        </button>
        <LocaleToggle />
        <ThemeToggle />
      </div>
    </header>
  );
}
