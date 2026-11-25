import { forwardRef, useId, type InputHTMLAttributes, type ReactNode } from 'react';
import { pickPath, type PathFilter } from '@/lib/api/client';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/Button';

/**
 * Text input.
 *
 * The border uses `--border-control` (≥3:1) rather than `--border` (1.3:1):
 * WCAG 1.4.11 requires a control's boundary to be discernible, and the hairline
 * used between surfaces is not. Focus adds a 2px ring *in addition to* the border
 * colour change so keyboard focus is never conveyed by colour alone.
 */
export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  /** Rendered inside the field at the start (icon or prefix). */
  leading?: ReactNode;
  /** Rendered inside the field at the end (unit, short hint, button). */
  trailing?: ReactNode;
  /** Marks the field invalid and wires up `aria-invalid`. */
  invalid?: boolean;
  inputSize?: 'sm' | 'md' | 'lg';
}

const SIZE_CLASS: Record<'sm' | 'md' | 'lg', string> = {
  sm: 'h-[var(--control-h-sm)] text-[length:var(--text-xs)] rounded-[var(--radius-sm)]',
  md: 'h-[var(--control-h)] text-[length:var(--text-sm)] rounded-[var(--radius-md)]',
  lg: 'h-[var(--control-h-lg)] text-[length:var(--text-base)] rounded-[var(--radius-md)]',
};

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, leading, trailing, invalid, inputSize = 'md', ...rest },
  ref,
) {
  return (
    <div
      className={cn(
        'group relative flex items-center gap-1.5 border bg-[var(--surface)]',
        'transition-[border-color,box-shadow] duration-100',
        'focus-within:ring-2 focus-within:ring-[var(--focus-ring)]',
        invalid
          ? 'border-[var(--danger)] focus-within:border-[var(--danger)]'
          : 'border-[var(--border-control)] focus-within:border-[var(--border-focus)]',
        SIZE_CLASS[inputSize],
        className,
      )}
    >
      {leading ? (
        <span className="ps-2.5 text-[length:var(--text-xs)] text-[var(--text-tertiary)]" aria-hidden="true">
          {leading}
        </span>
      ) : null}
      <input
        ref={ref}
        className={cn(
          'min-w-0 flex-1 bg-transparent outline-none placeholder:text-[length:var(--text-2xs)] text-[var(--text-quaternary)]',
          'disabled:cursor-not-allowed disabled:text-[length:var(--text-xs)] text-[var(--text-tertiary)]',
          !leading && 'ps-2.5',
          !trailing && 'pe-2.5',
        )}
        aria-invalid={invalid || undefined}
        {...rest}
      />
      {trailing ? (
        <span className="pe-2.5 text-[length:var(--text-xs)] text-[var(--text-tertiary)]">{trailing}</span>
      ) : null}
    </div>
  );
});

/** Labelled field wrapper: label above, hint below, error replaces the hint. */
export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
  className,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <label htmlFor={htmlFor} className="text-[length:var(--text-xs)] font-medium text-[length:var(--text-sm)] text-[var(--text-secondary)]">
        {label}
      </label>
      {children}
      {error ? (
        <p role="alert" className="text-[length:var(--text-xs)] text-[var(--danger)]">
          {error}
        </p>
      ) : hint ? (
        <p className="text-[length:var(--text-xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">{hint}</p>
      ) : null}
    </div>
  );
}

/**
 * A path field: text input plus a native "Browse…" button.
 *
 * Every path in the app was a bare text input, so setting `llama_server_path` or
 * the model directory meant pasting an absolute path by hand. The shell has had
 * `open_file_dialog` / `open_directory_dialog` all along; only two pages used them,
 * each with its own hand-rolled `window.__TAURI__` cast.
 *
 * Cancelling leaves the field **unchanged** — `pickPath` returns `null` and this
 * component simply does nothing, because "I changed my mind" is not "clear it".
 */
export function PathInput({
  kind,
  value,
  onValueChange,
  filters,
  prompt,
  placeholder,
  id,
  disabled,
  inputSize = 'md',
  className,
  browseLabel,
}: {
  kind: 'file' | 'dir';
  value: string;
  onValueChange: (next: string) => void;
  filters?: PathFilter[];
  /** Question asked in the browser dev channel; omit to disable the fallback. */
  prompt?: string;
  placeholder?: string;
  id?: string;
  disabled?: boolean;
  inputSize?: 'sm' | 'md' | 'lg';
  className?: string;
  browseLabel?: string;
}) {
  return (
    <Input
      id={id}
      value={value}
      onChange={(event) => onValueChange(event.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      inputSize={inputSize}
      className={cn('font-mono', className)}
      trailing={
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={disabled}
          // The trailing slot tints its children; reset the text colour so the
          // button is legible (the same trap `ServingPage` hit).
          className="text-[var(--text-secondary)]"
          onClick={() => {
            void pickPath(kind, { filters, current: value || undefined, prompt }).then(
              (picked) => {
                if (picked) onValueChange(picked);
              },
            );
          }}
        >
          {browseLabel ?? translate('params.browseButton')}
        </Button>
      }
    />
  );
}

/** Textarea with the same border/focus treatment as `Input`. */export const Textarea = forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement> & { invalid?: boolean }
>(function Textarea({ className, invalid, ...rest }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn(
        'w-full resize-y border bg-[var(--surface)] px-2.5 py-2',
        'rounded-[var(--radius-md)] text-[length:var(--text-sm)] leading-[var(--leading-normal)]',
        'outline-none transition-[border-color,box-shadow] duration-100',
        'focus:ring-2 focus:ring-[var(--focus-ring)]',
        'placeholder:text-[length:var(--text-2xs)] text-[var(--text-quaternary)]',
        invalid
          ? 'border-[var(--danger)] focus:border-[var(--danger)]'
          : 'border-[var(--border-control)] focus:border-[var(--border-focus)]',
        className,
      )}
      aria-invalid={invalid || undefined}
      {...rest}
    />
  );
});

/** A labelled switch built on a native checkbox so keyboard and AT just work. */
export function Switch({
  checked,
  onCheckedChange,
  disabled,
  label,
  description,
  id,
}: {
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  disabled?: boolean;
  label: ReactNode;
  description?: ReactNode;
  id?: string;
}) {
  const generated = useId();
  const inputId = id ?? generated;
  const descriptionId = description ? `${inputId}-desc` : undefined;
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <label
          htmlFor={inputId}
          className={cn(
            'block text-[length:var(--text-sm)] font-medium text-[var(--text)]',
            disabled ? 'cursor-not-allowed text-[length:var(--text-xs)] text-[var(--text-tertiary)]' : 'cursor-pointer',
          )}
        >
          {label}
        </label>
        {description ? (
          <p id={descriptionId} className="mt-0.5 text-[length:var(--text-xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
            {description}
          </p>
        ) : null}
      </div>
      <input
        id={inputId}
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        aria-describedby={descriptionId}
        onChange={(event) => onCheckedChange(event.target.checked)}
        className={cn(
          'relative mt-0.5 h-[22px] w-[38px] shrink-0 cursor-pointer appearance-none rounded-[var(--radius-pill)]',
          'border border-[var(--border-control)] bg-[var(--surface-sunken)]',
          'transition-colors duration-100',
          'checked:border-[var(--accent)] checked:bg-[var(--accent)]',
          'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
          'disabled:cursor-not-allowed disabled:opacity-45',
          // The knob
          "before:absolute before:top-1/2 before:start-[2px] before:size-[16px] before:-translate-y-1/2",
          'before:rounded-full before:bg-[var(--surface)] before:shadow-[var(--shadow-sm)]',
          'before:transition-[inset-inline-start] before:duration-100',
          'checked:before:start-[18px]',
        )}
      />
    </div>
  );
}
