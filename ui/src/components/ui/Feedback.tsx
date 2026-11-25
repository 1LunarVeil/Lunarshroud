import { Inbox, Loader2, TriangleAlert } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

/**
 * Unified empty state.
 *
 * Every list in the app uses this so "nothing here" never renders as a bare blank
 * pane. `title` names the missing thing; `action` offers the single next step.
 * Copy rules: sentence case, no apology, no "Oops".
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
  compact = false,
}: {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center text-center',
        compact ? 'gap-2 px-4 py-8' : 'gap-3 px-6 py-16',
        className,
      )}
    >
      <div
        aria-hidden="true"
        className={cn(
          'grid place-items-center rounded-[var(--radius-lg)] bg-[var(--surface-sunken)]',
          'text-[length:var(--text-xs)] text-[var(--text-tertiary)]',
          compact ? 'size-9' : 'size-12',
        )}
      >
        {icon ?? <Inbox size={compact ? 16 : 20} />}
      </div>
      <p className={cn('font-medium text-[var(--text)]', compact ? 'text-[length:var(--text-sm)]' : 'text-[length:var(--text-md)]')}>
        {title}
      </p>
      {description ? (
        <p className="max-w-[42ch] text-[length:var(--text-xs)] leading-[var(--leading-relaxed)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
          {description}
        </p>
      ) : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}

/** Error state: says what failed and offers a retry. Never hides the reason. */
export function ErrorState({
  title,
  message,
  action,
  className,
}: {
  title?: ReactNode;
  message: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-3 px-6 py-12 text-center', className)}>
      <div
        aria-hidden="true"
        className="grid size-10 place-items-center rounded-[var(--radius-lg)] bg-[var(--danger-subtle)] text-[var(--danger)]"
      >
        <TriangleAlert size={18} />
      </div>
      <p className="text-[length:var(--text-md)] font-medium text-[var(--text)]">{title ?? 'Something went wrong'}</p>
      <p className="max-w-[52ch] break-anywhere text-[length:var(--text-xs)] leading-[var(--leading-relaxed)] text-[length:var(--text-sm)] text-[var(--text-secondary)]">
        {message}
      </p>
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}

/** Loading placeholder that keeps the pane's final geometry. */
export function LoadingState({ label = 'Loading…', className }: { label?: string; className?: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn('flex items-center justify-center gap-2 px-6 py-12 text-[length:var(--text-sm)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]', className)}
    >
      <Loader2 size={15} aria-hidden="true" className="animate-spin" />
      <span className="min-w-0 truncate">{label}</span>
    </div>
  );
}

/** Skeleton block for lists; width is a percentage range to avoid a uniform look. */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={cn('animate-pulse rounded-[var(--radius-sm)] bg-[var(--surface-sunken)]', className)}
    />
  );
}

/** A section heading with an optional description and trailing action. */
export function SectionHeader({
  title,
  description,
  action,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex items-start justify-between gap-4', className)}>
      <div className="min-w-0">
        <h2 className="text-[length:var(--text-md)] font-semibold tracking-[var(--tracking-tight)] text-[var(--text)]">
          {title}
        </h2>
        {description ? (
          <p className="mt-0.5 text-[length:var(--text-xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">{description}</p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

/** Status pill. Colour is never the only signal: the label always says the state. */
const TONES = {
  neutral: 'bg-[var(--surface-sunken)] text-[length:var(--text-sm)] text-[var(--text-secondary)]',
  success: 'bg-[var(--success-subtle)] text-[var(--success)]',
  warning: 'bg-[var(--warning-subtle)] text-[var(--warning)]',
  danger: 'bg-[var(--danger-subtle)] text-[var(--danger)]',
  info: 'bg-[var(--info-subtle)] text-[var(--info)]',
  accent: 'bg-[var(--accent-subtle)] text-[var(--accent-text)]',
} as const;

export type BadgeTone = keyof typeof TONES;

export function Badge({
  children,
  tone = 'neutral',
  dot = false,
  className,
  title,
}: {
  children: ReactNode;
  tone?: BadgeTone;
  dot?: boolean;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex items-center gap-1 rounded-[var(--radius-pill)] px-2 py-[2px]',
        'text-[length:var(--text-2xs)] font-medium whitespace-nowrap',
        TONES[tone],
        className,
      )}
    >
      {dot ? <span aria-hidden="true" className="size-1.5 rounded-full bg-current" /> : null}
      {children}
    </span>
  );
}

/** Monospace inline value for paths, ids and commands. Selectable and copyable. */
export function Code({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <code
      className={cn(
        'break-anywhere rounded-[var(--radius-xs)] bg-[var(--code-inline-bg)] px-1.5 py-[1px]',
        'font-mono text-[0.92em] text-[var(--code-inline-fg)]',
        className,
      )}
    >
      {children}
    </code>
  );
}
