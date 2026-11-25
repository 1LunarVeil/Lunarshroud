import { cva, type VariantProps } from 'class-variance-authority';
import { forwardRef, type ButtonHTMLAttributes } from 'react';
import { cn } from '@/lib/utils';

/**
 * Button.
 *
 * Variant vocabulary follows what the app actually needs, not a generic kit:
 * - `primary` is the near-black fill (macOS System Settings language). It is the
 *   single strongest control on a screen, so use it once per view.
 * - `secondary` is a bordered neutral for the second action.
 * - `ghost` is borderless and used in toolbars and row actions.
 * - `danger` is reserved for destructive confirmation.
 * - `link` is for inline navigation inside text.
 *
 * Every variant keeps a visible `focus-visible` ring that differs from hover
 * (the ring is an outline, the hover state is a background change), and the
 * disabled state removes hover treatment instead of relying on opacity alone.
 */
const buttonVariants = cva(
  [
    'inline-flex items-center justify-center gap-1.5 whitespace-nowrap',
    'font-medium select-none',
    'transition-[background-color,border-color,color,opacity] duration-100',
    'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
    // Disabled state: **not** `opacity`. Fading the whole control fades its label too,
    // and a disabled primary button measured 3.1:1 — below AA. A disabled control still
    // has to be readable ("why can I not click this?"), so the fill and the text are both
    // neutralised explicitly instead.
    'disabled:pointer-events-none disabled:cursor-not-allowed',
    'disabled:bg-[var(--surface-sunken)] disabled:text-[var(--text-tertiary)] disabled:border-transparent',
    'disabled:shadow-none',
  ],
  {
    variants: {
      variant: {
        // `fill-primary` is a plain CSS class, not a utility: `@theme inline`
        // bakes the light value into utilities, so the dark-mode override never
        // applied and the label measured 1.01:1 against its own button.
        primary: 'fill-primary active:scale-[0.99]',
        secondary:
          'bg-[var(--surface)] text-[var(--text)] border border-[var(--border-control)] hover:bg-[var(--hover)]',
        ghost:
          'bg-transparent text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--hover)] hover:text-[var(--text)]',
        subtle:
          'bg-[var(--primary-subtle)] text-[var(--text)] hover:bg-[var(--primary-subtle-hover)]',
        danger: 'fill-danger',
        dangerGhost:
          'bg-transparent text-[var(--danger)] hover:bg-[var(--danger-subtle)]',
        link:
          'bg-transparent text-[var(--accent-text)] underline-offset-2 hover:underline hover:bg-transparent',
      },
      size: {
        sm: 'h-[var(--control-h-sm)] px-2 text-[length:var(--text-xs)] rounded-[var(--radius-sm)]',
        md: 'h-[var(--control-h)] px-3 text-[length:var(--text-sm)] rounded-[var(--radius-md)]',
        lg: 'h-[var(--control-h-lg)] px-4 text-[length:var(--text-base)] rounded-[var(--radius-md)]',
        icon: 'h-[var(--control-h)] w-[var(--control-h)] rounded-[var(--radius-md)]',
        iconSm: 'h-[var(--control-h-sm)] w-[var(--control-h-sm)] rounded-[var(--radius-sm)]',
        pill: 'h-[var(--control-h)] px-3.5 text-[length:var(--text-sm)] rounded-[var(--radius-pill)]',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** Renders a spinner and sets `aria-busy`; the label stays visible. */
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, loading = false, disabled, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={rest.type ?? 'button'}
      className={cn(buttonVariants({ variant, size }), className)}
      aria-busy={loading || undefined}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? (
        <span
          aria-hidden="true"
          className="size-3 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      ) : null}
      {children}
    </button>
  );
});

export { buttonVariants };
