import { ChevronRight, Clock } from 'lucide-react';
import { useEffect, useState } from 'react';
import { cn } from '@/lib/utils';
import { translate } from '@/lib/i18n';
import { Markdown } from '@/components/chat/Markdown';
import { MessageBoundary } from '@/components/chat/MessageBoundary';

/**
 * Collapsible reasoning ("thinking") panel.
 *
 * Open/close policy comes from `reasoningPanelOpen` in `ported/chatStream.ts`, and
 * the precedence rule there is the important part: **a manual toggle always wins**.
 * Without it, "open while thinking, collapse when finished" snaps shut on the very
 * message the user just opened — and because the panel re-renders on every chunk,
 * that reads as a broken control rather than a policy.
 *
 * The panel renders muted text (`--trace-ink`) so long reasoning does not compete
 * with the answer. Its header always states what it is and how long it took, so a
 * collapsed panel is still informative.
 */
export function ReasoningPanel({
  text,
  mode = 'auto',
  streaming = false,
  durationMs,
}: {
  text: string;
  mode?: 'auto' | 'expand' | 'collapse' | string;
  streaming?: boolean;
  durationMs?: number | null;
}) {
  const [touched, setTouched] = useState(false);
  const [chosen, setChosen] = useState(false);

  // Derive the open state from the shared policy unless the user has decided.
  const shouldOpen = (() => {
    if (!text) return false;
    if (touched) return chosen;
    if (mode === 'expand') return true;
    if (mode === 'collapse') return false;
    return Boolean(streaming);
  })();

  // While streaming with the automatic policy, keep the panel open: the reader is
  // watching progress. Once streaming ends it collapses, unless the user opened it.
  const [open, setOpen] = useState(shouldOpen);
  useEffect(() => {
    if (!touched) setOpen(shouldOpen);
  }, [shouldOpen, touched]);

  if (!text) return null;

  const seconds = durationMs && durationMs > 0 ? Math.round(durationMs / 1000) : null;
  const label =
    seconds !== null
      ? translate('chat.workedFor', { duration: `${seconds}s` })
      : translate('chat.thinking');

  return (
    <div
      data-testid="reasoning-panel"
      className={cn(
        'my-2 overflow-hidden rounded-[var(--radius-md)] border border-[var(--border-faint,var(--border))]',
        'bg-[var(--surface-sunken)]',
      )}
    >
      <button
        type="button"
        onClick={() => {
          setTouched(true);
          setChosen(!open);
          setOpen(!open);
        }}
        aria-expanded={open}
        aria-label={translate(open ? 'chat.reasoningCollapse' : 'chat.reasoningExpand')}
        className={cn(
          'flex w-full items-center gap-1.5 px-2.5 py-1.5 text-start',
          'text-[length:var(--text-xs)] font-medium text-[length:var(--text-xs)] text-[var(--text-tertiary)]',
          'hover:bg-[var(--hover)] hover:text-[length:var(--text-sm)] text-[var(--text-secondary)]',
          'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
        )}
      >
        <ChevronRight
          size={13}
          aria-hidden="true"
          className={cn('shrink-0 transition-transform duration-100', open && 'rotate-90')}
        />
        <Clock size={12} aria-hidden="true" className="shrink-0" />
        <span>{label}</span>
        {streaming ? (
          <span aria-hidden="true" className="ms-1 size-1.5 animate-pulse rounded-full bg-[var(--accent)]" />
        ) : null}
      </button>
      {open ? (
        <div className="border-t border-[var(--border)] px-3 py-2">
          <div className="trace">
            <MessageBoundary fallbackText={text}>
              <Markdown text={text} className="markdown" />
            </MessageBoundary>
          </div>
        </div>
      ) : null}
    </div>
  );
}
