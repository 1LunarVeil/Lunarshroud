import { ChevronRight } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import type { ChatToolCall } from '@/lib/api/types';
import { chatApi } from '@/lib/api';
import { describeToolExecution, parseToolCallArguments } from '@/lib/ported/chatTools';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Markdown } from '@/components/chat/Markdown';

/**
 * A `tool` message: the result of a call the model made.
 *
 * 1.0 had no dedicated component for these — every non-user role rendered as an
 * assistant bubble, so a tool's raw output appeared as if the assistant had said
 * it. That is both confusing and wrong: output is data, not prose.
 *
 * The block shows the tool name, collapsible output, and an explicit error tone
 * when the call failed.
 */
export function ToolResultBlock({
  name,
  content,
  toolCallId,
  isError = false,
}: {
  name?: string | null;
  content: string;
  toolCallId?: string | null;
  isError?: boolean;
}) {
  const title = name || translate('chat.toolResult');
  return (
    <details
      data-testid="tool-result"
      className={cn(
        'group my-2 overflow-hidden rounded-[var(--radius-md)] border',
        isError ? 'border-[var(--danger)]/40 bg-[var(--danger-subtle)]' : 'border-[var(--border)] bg-[var(--surface-sunken)]',
      )}
    >
      <summary
        className={cn(
          'flex cursor-pointer list-none items-center gap-1.5 px-2.5 py-1.5',
          'text-[length:var(--text-xs)] font-medium select-none',
          isError ? 'text-[var(--danger)]' : 'text-[length:var(--text-sm)] text-[var(--text-secondary)]',
          'hover:bg-[var(--hover)]',
          'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
        )}
      >
        <ChevronRight
          size={13}
          aria-hidden="true"
          className="shrink-0 transition-transform duration-100 group-open:rotate-90"
        />
        <span className="truncate">{title}</span>
        {toolCallId ? (
          <span className="truncate font-mono text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
            {toolCallId}
          </span>
        ) : null}
      </summary>
      <div className="max-h-[420px] overflow-auto border-t border-[var(--border)] px-3 py-2">
        <pre className="whitespace-pre-wrap break-anywhere font-mono text-[length:var(--text-xs)] leading-[var(--leading-relaxed)] text-[length:var(--text-sm)] text-[var(--text-secondary)]">
          {content}
        </pre>
      </div>
    </details>
  );
}

/**
 * A rejection slug → text the user can act on.
 *
 * The raw slugs (`no-root`, `outside-root`, `pattern-escape`) are what the pure
 * policy module deals in; showing them verbatim tells a user nothing. `no-root` in
 * particular is the *first-run* state, not an error with the model's call — the
 * right answer is "authorize a folder", not a red string.
 */
function describeReason(reason: string | undefined): string {
  switch (reason) {
    case 'no-root':
      return translate('chat.toolNoRoot');
    case 'bad-arguments':
      return translate('chat.toolArgumentsBad');
    case 'tool-not-allowed':
    case 'tool-unknown':
      return translate('chat.toolBlocked');
    case 'empty-path':
    case 'absolute-path':
    case 'unc-path':
    case 'outside-root':
    case 'is-root':
      return translate('chat.toolOutsideRoot');
    case 'missing-pattern':
    case 'missing-query':
    case 'pattern-escape':
      return translate('chat.toolBadPattern');
    default:
      return String(reason ?? '');
  }
}

/**
 * The calls an assistant message requested.
 *
 * Each call is shown with its **resolved target**, not just the tool name: an
 * approval prompt that only says "read_file" tells the user nothing about what
 * will be read. `describeToolExecution` is the single implementation of that
 * verdict (the same function the execution path consults), so the summary shown
 * here cannot drift from what will actually happen.
 */
export function ToolCallBlock({
  calls,
  root,
  results,
  onApprove,
  onDeny,
  pending = false,
}: {
  calls: ChatToolCall[];
  root: string | null;
  results?: Record<string, { ok: boolean; content: string }>;
  onApprove?: (call: ChatToolCall) => void;
  onDeny?: (call: ChatToolCall) => void;
  pending?: boolean;
}) {
  const rows = useMemo(
    () =>
      calls.map((call) => ({
        call,
        plan: describeToolExecution(call.function.name, call.function.arguments, root ?? ''),
        args: parseToolCallArguments(call.function.arguments),
      })),
    [calls, root],
  );

  /**
   * Server-resolved previews, keyed by call id.
   *
   * The client-side `describeToolExecution` stays as the **first frame** so the
   * card renders immediately and never disappears if the request fails; the
   * server result replaces it when it arrives. `agent_preview.describe` is the
   * one implementation the approval gate also uses, so the card and the executor
   * cannot disagree about what a call touches — having only the client copy meant
   * two implementations of the same security-relevant judgement.
   */
  const [previews, setPreviews] = useState<
    Record<string, { ok: boolean; summary: string; risk?: string; argv?: string[] }>
  >({});

  useEffect(() => {
    if (!root) return;
    let cancelled = false;
    for (const call of calls) {
      const id = call.id;
      if (!id || previews[id]) continue;
      const args = parseToolCallArguments(call.function.arguments) ?? {};
      void chatApi
        .previewTool({ name: call.function.name, arguments: args, root })
        .then((result) => {
          if (cancelled) return;
          setPreviews((current) =>
            current[id]
              ? current
              : {
                  ...current,
                  [id]: {
                    ok: Boolean(result.ok),
                    summary: String(result.summary ?? result.target ?? ''),
                    risk: result.risk,
                    argv: result.argv,
                  },
                },
          );
        })
        .catch(() => {
          // Keep the client-side verdict; a failed preview must not erase the
          // approval card — the user still needs to be able to deny.
        });
    }
    return () => {
      cancelled = true;
    };
    // `previews` is intentionally not a dependency: it is the write target, and
    // including it would re-run the loop on its own result.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [calls, root]);

  if (!rows.length) return null;

  return (
    <div data-testid="tool-calls" className="my-2 flex flex-col gap-1.5">
      {rows.map(({ call, plan: clientPlan, args }) => {
        const server = call.id ? previews[call.id] : undefined;
        // Server verdict wins when present; the client verdict is the fallback.
        // `agent_preview.describe` knows every tool the chat page can declare
        // (including `web_search`), so there is no per-tool exclusion here — a
        // rejection from the server is authoritative precisely because it is the
        // one implementation the executor also uses.
        const plan = server
          ? {
              ...clientPlan,
              ok: server.ok,
              summary: server.summary || clientPlan.summary,
            }
          : clientPlan;
        const result = call.id ? results?.[call.id] : undefined;
        return (
          <div
            key={call.id || `${call.function.name}-${plan.summary}`}
            className={cn(
              'overflow-hidden rounded-[var(--radius-md)] border',
              result
                ? 'border-[var(--border)] bg-[var(--surface)]'
                : plan.ok
                  ? 'border-[var(--accent)]/35 bg-[var(--accent-subtle)]'
                  : 'border-[var(--danger)]/40 bg-[var(--danger-subtle)]',
            )}
          >
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-2.5 py-1.5">
              <span className="font-mono text-[length:var(--text-xs)] font-medium text-[var(--text)]">
                {call.function.name || 'tool'}
              </span>
              {plan.ok ? (
                <span className="min-w-0 flex-1 truncate font-mono text-[length:var(--text-2xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                  {plan.summary}
                </span>
              ) : (
                <span className="min-w-0 flex-1 truncate text-[length:var(--text-2xs)] text-[var(--danger)]">
                  {describeReason(plan.reason)}
                </span>
              )}
              {result ? (
                <span
                  className={cn(
                    'rounded-[var(--radius-pill)] px-1.5 py-[1px] text-[length:var(--text-2xs)]',
                    result.ok
                      ? 'bg-[var(--success-subtle)] text-[var(--success)]'
                      : 'bg-[var(--danger-subtle)] text-[var(--danger)]',
                  )}
                >
                  {result.ok ? 'ok' : 'error'}
                </span>
              ) : pending && plan.ok ? (
                <>
                  <span className="text-[length:var(--text-2xs)] text-[var(--accent-text)]">
                    {translate('chat.toolPending')}
                  </span>
                  {onDeny ? (
                    <button
                      type="button"
                      onClick={() => onDeny(call)}
                      className="h-6 rounded-[var(--radius-sm)] px-2 text-[length:var(--text-2xs)] text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--hover)]"
                    >
                      {translate('chat.toolDeny')}
                    </button>
                  ) : null}
                  {onApprove ? (
                    <button
                      type="button"
                      onClick={() => onApprove(call)}
                      className="h-6 rounded-[var(--radius-sm)] fill-primary px-2 text-[length:var(--text-2xs)] font-medium"
                    >
                      {translate('chat.toolApprove')}
                    </button>
                  ) : null}
                </>
              ) : null}
            </div>
            {args ? (
              <div className="border-t border-[var(--border)]/60 px-2.5 py-1.5">
                <pre className="whitespace-pre-wrap break-anywhere font-mono text-[length:var(--text-2xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                  {JSON.stringify(args, null, 2)}
                </pre>
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

/** Citations returned by the knowledge base, rendered as a collapsible list. */
export function Citations({
  sources,
}: {
  sources: Array<{ index: number; title: string; page?: number | null; heading?: string | null; snippet: string; ocr?: boolean }>;
}) {
  if (!sources.length) return null;
  return (
    <details
      data-testid="citations"
      className="group my-2 rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface-sunken)]"
    >
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-2.5 py-1.5 text-[length:var(--text-xs)] font-medium text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--hover)]">
        <ChevronRight size={13} aria-hidden="true" className="transition-transform group-open:rotate-90" />
        {translate('chat.sourcesCount', { count: sources.length })}
      </summary>
      <ol className="flex flex-col gap-2 border-t border-[var(--border)] p-2.5">
        {sources.map((source) => (
          <li key={`${source.index}-${source.title}`} className="text-[length:var(--text-xs)]">
            <div className="flex flex-wrap items-baseline gap-1.5">
              <span className="font-mono text-[length:var(--text-2xs)] text-[var(--accent-text)]">[{source.index}]</span>
              <span className="font-medium text-[var(--text)]">{source.title}</span>
              {source.page ? (
                <span className="text-[length:var(--text-2xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                  {translate('chat.sourcePage', { page: source.page })}
                </span>
              ) : null}
              {source.heading ? (
                <span className="truncate text-[length:var(--text-2xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                  {source.heading}
                </span>
              ) : null}
              {source.ocr ? (
                <span className="rounded-[var(--radius-pill)] bg-[var(--warning-subtle)] px-1.5 text-[length:var(--text-2xs)] text-[var(--warning)]">
                  OCR
                </span>
              ) : null}
            </div>
            <p className="mt-1 max-w-[70ch] leading-[var(--leading-relaxed)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {source.snippet}
            </p>
          </li>
        ))}
      </ol>
    </details>
  );
}

export { Markdown };
