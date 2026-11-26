import { Paperclip, SendHorizontal, Square } from 'lucide-react';
import {
  forwardRef,
  useCallback,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import { translate } from '@/lib/i18n';
import { cn, formatBytes } from '@/lib/utils';
import {
  ATTACHMENT_ACCEPT,
  MAX_ATTACHMENT_CHARS,
  MAX_IMAGE_BYTES,
  MAX_MESSAGE_ATTACHMENT_CHARS,
  classifyAttachmentFile,
  formatAttachmentSize,
  looksBinaryText,
  type AttachmentKind,
} from '@/lib/ported/attachments';

/**
 * The composer.
 *
 * This is the window's signature element and its most-used control, so it gets the
 * compositional freedom the rest of the app does not: a 28px outer radius with an
 * 18px send circle inset 10px, concentric by construction (outer = inner + inset).
 *
 * Behaviour that must hold:
 * - **Enter sends, Shift+Enter inserts a line break**, and IME composition is
 *   respected: sending mid-composition breaks Chinese and Japanese input.
 * - The field grows to a ceiling and then scrolls, so a long draft never pushes
 *   the transcript off screen.
 * - While generating, the send button becomes stop. One control, two states, so
 *   the user's finger stays in the same place.
 * - Attachments are read locally (File API). Nothing is uploaded.
 */
export interface PendingAttachment {
  id: string;
  name: string;
  kind: AttachmentKind;
  content: string;
  size: number;
}

export interface ComposerHandle {
  focus: () => void;
  clear: () => void;
  insertText: (text: string) => void;
}

export interface ComposerProps {
  value: string;
  onValueChange: (value: string) => void;
  onSend: (attachments: PendingAttachment[]) => void;
  onStop: () => void;
  streaming: boolean;
  disabled?: boolean;
  supportsVision: boolean;
  contextLabel?: string;
  contextPercent?: number | null;
  placeholder?: string;
  /** Which key combo sends. Defaults to `enter` so a caller that omits it is unchanged. */
  sendKey?: 'enter' | 'ctrl-enter';
}

const MAX_ROWS_PX = 220;

export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer(
  {
    value,
    onValueChange,
    onSend,
    onStop,
    streaming,
    disabled = false,
    supportsVision,
    contextLabel,
    contextPercent = null,
    placeholder,
    sendKey = 'enter',
  },
  ref,
) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const composing = useRef(false);

  useImperativeHandle(ref, () => ({
    focus: () => textareaRef.current?.focus(),
    clear: () => setAttachments([]),
    insertText: (text: string) => {
      const field = textareaRef.current;
      if (!field) {
        onValueChange(`${value}${text}`);
        return;
      }
      const start = field.selectionStart ?? value.length;
      const end = field.selectionEnd ?? value.length;
      const next = `${value.slice(0, start)}${text}${value.slice(end)}`;
      onValueChange(next);
      requestAnimationFrame(() => {
        field.focus();
        const caret = start + text.length;
        field.setSelectionRange(caret, caret);
      });
    },
  }));

  // Autosize: reset to the minimum, then grow to fit, clamped by the ceiling.
  useLayoutEffect(() => {
    const field = textareaRef.current;
    if (!field) return;
    field.style.height = 'auto';
    field.style.height = `${Math.min(field.scrollHeight, MAX_ROWS_PX)}px`;
  }, [value]);

  const addFiles = useCallback(
    async (files: FileList | File[]) => {
      setError(null);
      const accepted = Array.from(files);
      const next: PendingAttachment[] = [];
      let textTotal = attachments.reduce(
        (sum, attachment) => sum + (attachment.kind === 'text' ? attachment.content.length : 0),
        0,
      );

      for (const file of accepted) {
        const verdict = classifyAttachmentFile({
          name: file.name,
          size: file.size,
          supportsVision,
        });
        if (!verdict.ok) {
          setError(
            verdict.reason === 'no-vision'
              ? translate('chat.attachNoVision')
              : verdict.reason === 'too-large'
                ? translate('chat.attachTooLarge', { name: file.name })
                : translate('chat.attachUnsupported', { name: file.name }),
          );
          continue;
        }

        if (verdict.kind === 'image') {
          if (file.size > MAX_IMAGE_BYTES) {
            setError(translate('chat.attachTooLarge', { name: file.name }));
            continue;
          }
          const dataUrl = await readAsDataUrl(file);
          next.push({
            id: `${file.name}-${file.lastModified}`,
            name: file.name,
            kind: 'image',
            content: dataUrl,
            size: file.size,
          });
          continue;
        }

        // Check the size **before** reading. `await file.text()` on a 4 GB log in
        // the accept list allocates the whole thing first, and only the cumulative
        // check afterwards would reject it — the app freezes before the user is
        // told anything (audit L7). The byte cap is the char cap with headroom for
        // multi-byte UTF-8.
        if (file.size > MAX_ATTACHMENT_CHARS * 4) {
          setError(translate('chat.attachTooLarge', { name: file.name }));
          continue;
        }
        const text = await file.text();
        if (looksBinaryText(text)) {
          setError(translate('chat.attachBinary', { name: file.name }));
          continue;
        }
        if (text.length > MAX_ATTACHMENT_CHARS) {
          setError(translate('chat.attachTooLarge', { name: file.name }));
          continue;
        }
        textTotal += text.length;
        if (textTotal > MAX_MESSAGE_ATTACHMENT_CHARS) {
          setError(translate('chat.attachTooLarge', { name: file.name }));
          break;
        }
        next.push({
          id: `${file.name}-${file.lastModified}`,
          name: file.name,
          kind: 'text',
          content: text,
          size: file.size,
        });
      }

      if (next.length) {
        setAttachments((current) => {
          const merged = [...current, ...next];
          return merged.slice(0, 5);
        });
      }
      if (fileRef.current) fileRef.current.value = '';
    },
    [attachments, supportsVision],
  );

  const submit = () => {
    if (streaming || disabled) return;
    const trimmed = value.trim();
    if (!trimmed && !attachments.length) return;
    onSend(attachments);
    setAttachments([]);
  };

  const canSend = Boolean(value.trim() || attachments.length) && !disabled;

  return (
    <div className="pointer-events-none sticky bottom-0 z-[var(--z-sticky)] px-[var(--thread-pad)] pb-3">
      <div className="pointer-events-auto mx-auto w-full max-w-[var(--thread-max)]">
        {error ? (
          <p role="alert" className="mb-1.5 px-1 text-[length:var(--text-xs)] text-[var(--danger)]">
            {error}
          </p>
        ) : null}

        <div
          data-testid="composer"
          className={cn(
            'border border-[var(--border)] bg-[var(--composer-surface)]',
            'rounded-[var(--radius-composer)] p-2.5 shadow-[var(--shadow-composer)]',
            'transition-[border-color] duration-100',
            'focus-within:border-[var(--border-control)]',
          )}
        >
          {attachments.length ? (
            <ul
              aria-label={translate('chat.pendingAttachments')}
              className="mb-2 flex flex-wrap gap-1.5"
            >
              {attachments.map((attachment) => (
                <li
                  key={attachment.id}
                  className={cn(
                    'inline-flex max-w-[15rem] items-center gap-1.5 rounded-[var(--radius-sm)] px-1.5 py-1',
                    'text-[length:var(--text-2xs)]',
                    attachment.kind === 'image'
                      ? 'bg-[var(--accent-subtle)] text-[var(--accent-text)]'
                      : 'bg-[var(--surface-sunken)] text-[length:var(--text-sm)] text-[var(--text-secondary)]',
                  )}
                >
                  <span className="truncate">{attachment.name}</span>
                  <span className="shrink-0 tnum text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                    {formatAttachmentSize(attachment.size)}
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      setAttachments((current) => current.filter((item) => item.id !== attachment.id))
                    }
                    aria-label={translate('chat.removeAttachment')}
                    className="grid size-4 shrink-0 place-items-center rounded-full hover:bg-[var(--hover-strong)]"
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          ) : null}

          <textarea
            ref={textareaRef}
            value={value}
            rows={1}
            onChange={(event) => onValueChange(event.target.value)}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            onKeyDown={(event) => {
              // `sendKey` is a user preference: 'enter' (default) or 'ctrl-enter'.
              // With 'ctrl-enter', a bare Enter must insert a newline, so it is
              // **not** prevented here — the textarea's default does the right thing.
              const sends =
                sendKey === 'ctrl-enter'
                  ? event.key === 'Enter' && (event.ctrlKey || event.metaKey)
                  : event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.metaKey;
              if (!sends) return;
              // Never send mid-composition: an IME commit is not a send intent.
              if (composing.current || event.nativeEvent.isComposing) return;
              event.preventDefault();
              submit();
            }}
            onPaste={(event) => {
              const files = event.clipboardData?.files;
              if (files && files.length) {
                event.preventDefault();
                void addFiles(files);
              }
            }}
            placeholder={placeholder ?? translate('chat.composerPlaceholder')}
            aria-label={translate('chat.composerPlaceholder')}
            disabled={disabled}
            className={cn(
              'block max-h-[220px] w-full resize-none bg-transparent px-1.5 py-1',
              'text-[length:var(--text-md)] leading-[var(--leading-relaxed)] outline-none',
              'placeholder:text-[length:var(--text-2xs)] text-[var(--text-quaternary)]',
              'disabled:cursor-not-allowed',
            )}
          />

          <div className="mt-1 flex items-center gap-1">
            <input
              ref={fileRef}
              type="file"
              multiple
              accept={`${ATTACHMENT_ACCEPT},image/png,image/jpeg,image/webp,image/gif,image/bmp`}
              onChange={(event) => {
                if (event.target.files) void addFiles(event.target.files);
              }}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              disabled={disabled}
              aria-label={translate('chat.attach')}
              title={translate('chat.attach')}
              className={cn(
                'grid size-[26px] place-items-center rounded-full text-[length:var(--text-xs)] text-[var(--text-tertiary)]',
                'hover:bg-[var(--hover)] hover:text-[var(--text)]',
                'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
                'disabled:opacity-40',
              )}
            >
              <Paperclip size={15} aria-hidden="true" />
            </button>

            <span className="flex-1" />

            {contextLabel ? (
              <span
                className={cn(
                  'me-1 flex items-center gap-1 text-[length:var(--text-2xs)] tnum',
                  contextPercent !== null && contextPercent >= 85
                    ? 'text-[var(--warning)]'
                    : 'text-[length:var(--text-xs)] text-[var(--text-tertiary)]',
                )}
                title={contextLabel}
              >
                {contextPercent !== null ? `${contextPercent}%` : translate('chat.contextUnknown')}
              </span>
            ) : null}

            {streaming ? (
              <button
                type="button"
                data-testid="composer-stop"
                onClick={onStop}
                aria-label={translate('chat.stop')}
                title={translate('chat.stop')}
                className={cn(
                  'grid size-[30px] place-items-center rounded-full',
                  'fill-danger',
                  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
                )}
              >
                <Square size={13} aria-hidden="true" className="fill-current" />
              </button>
            ) : (
              <button
                type="button"
                data-testid="composer-send"
                onClick={submit}
                disabled={!canSend}
                aria-label={translate('chat.send')}
                title={translate('chat.send')}
                className={cn(
                  // 18px circle + 6px padding = 30px, inset 10px from the 28px shell.
                  'grid size-[30px] place-items-center rounded-full',
                  // `.fill-primary` owns both the fill and its hover state: the
                  // theme-dependent pair must not be re-applied as utilities.
                  'fill-primary',
                  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--border-focus)]',
                  'disabled:bg-[var(--surface-sunken)] disabled:text-[length:var(--text-2xs)] text-[var(--text-quaternary)]',
                  'disabled:hover:bg-[var(--surface-sunken)]',
                )}
              >
                <SendHorizontal size={15} aria-hidden="true" />
              </button>
            )}
          </div>
        </div>

        <p className="mt-1.5 px-1 text-center text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
          {translate('chat.localOnly')}
        </p>
      </div>
    </div>
  );
});

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error('read failed'));
    reader.readAsDataURL(file);
  });
}

export { formatBytes };
