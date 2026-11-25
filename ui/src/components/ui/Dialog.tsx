import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils';

/**
 * Modal dialog.
 *
 * Built on Radix so focus trapping, `aria-modal`, Escape handling and focus
 * restoration are the platform's job rather than hand-written (1.0 shipped its own
 * stack and needed 267 lines to get half of it).
 *
 * Two decisions the app depends on:
 * - The scrim is a flat tint of `--overlay-scrim`. No backdrop blur: the reading
 *   surface sits behind it, and blur behind long text lowers contrast.
 * - `Dialog.Content` is rendered in a portal above everything else, which is what
 *   the settings sheet and command palette need.
 */
export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogTitle = DialogPrimitive.Title;
export const DialogDescription = DialogPrimitive.Description;

export function DialogContent({
  children,
  className,
  size = 'md',
  showClose = true,
  title,
  description,
}: {
  children: ReactNode;
  className?: string;
  size?: 'sm' | 'md' | 'lg' | 'xl' | 'settings';
  showClose?: boolean;
  title: ReactNode;
  description?: ReactNode;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay
        className={cn(
          'fixed inset-0 z-[var(--z-overlay)] bg-[var(--overlay-scrim)]',
          'data-[state=open]:animate-in data-[state=open]:fade-in',
        )}
      />
      <DialogPrimitive.Content
        className={cn(
          'fixed left-1/2 top-1/2 z-[var(--z-modal)] -translate-x-1/2 -translate-y-1/2',
          'flex max-h-[86vh] flex-col overflow-hidden',
          'border border-[var(--border)] bg-[var(--surface)]',
          'rounded-[var(--radius-xl)] shadow-[var(--shadow-pop)]',
          'focus:outline-none',
          SIZES[size],
          className,
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b border-[var(--border)] px-5 py-4">
          <div className="min-w-0">
            <DialogPrimitive.Title className="text-[length:var(--text-lg)] font-semibold tracking-[var(--tracking-tight)] text-[var(--text)]">
              {title}
            </DialogPrimitive.Title>
            {description ? (
              <DialogPrimitive.Description className="mt-1 text-[length:var(--text-xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                {description}
              </DialogPrimitive.Description>
            ) : null}
          </div>
          {showClose ? (
            <DialogPrimitive.Close
              aria-label="Close"
              className={cn(
                'grid size-7 shrink-0 place-items-center rounded-[var(--radius-sm)]',
                'text-[length:var(--text-xs)] text-[var(--text-tertiary)] transition-colors hover:bg-[var(--hover)] hover:text-[var(--text)]',
                'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
              )}
            >
              <X size={16} aria-hidden="true" />
            </DialogPrimitive.Close>
          ) : null}
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

const SIZES: Record<string, string> = {
  sm: 'w-[min(420px,calc(100vw-2rem))]',
  md: 'w-[min(640px,calc(100vw-2rem))]',
  lg: 'w-[min(880px,calc(100vw-2rem))]',
  xl: 'w-[min(1120px,calc(100vw-2rem))]',
  // The settings sheet: a wide two-pane dialog, fixed to the viewport so it does
  // not jump between tabs of different content height.
  settings: 'h-[min(820px,calc(100vh-3rem))] w-[min(1060px,calc(100vw-2rem))]',
};

/** Sticky footer for dialog actions. */
export function DialogFooter({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        'flex items-center justify-end gap-2 border-t border-[var(--border)] px-5 py-3',
        className,
      )}
    >
      {children}
    </div>
  );
}
