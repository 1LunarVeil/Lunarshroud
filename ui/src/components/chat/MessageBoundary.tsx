import { Component, type ErrorInfo, type ReactNode } from 'react';
import { translate } from '@/lib/i18n';

/**
 * Per-message render boundary.
 *
 * Model output is untrusted, and all renderers that touch it (ReactMarkdown,
 * KaTeX, Shiki) can throw on adversarial input despite the budgets in
 * `markdownBudget.ts` — a budget lowers the probability, it does not remove the
 * class. Without a boundary a single throw unmounts the entire React tree: the
 * window goes blank with no message and no recovery, and restarting the app
 * re-renders the same conversation and blanks again (audit, 2026-11-23).
 *
 * The fallback keeps the **raw text** visible. A message the app cannot format is
 * still a message the user asked for; hiding it would be worse than showing it
 * unstyled.
 */
interface Props {
  children: ReactNode;
  /** Raw text to show when the formatted render fails. */
  fallbackText?: string;
  /** Shown above the fallback, so "broken formatting" is distinguishable from "no content". */
  label?: string;
}

interface State {
  failed: boolean;
}

export class MessageBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The console line is the only trace a packaged build leaves; it must name the
    // failure rather than swallow it.
    console.error('message render failed', error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div
        data-testid="message-render-fallback"
        className="my-2 rounded-[var(--radius-md)] border border-[var(--warning)]/40 bg-[var(--warning-subtle)] p-2.5"
      >
        <p className="text-[length:var(--text-xs)] font-medium text-[var(--warning)]">
          {this.props.label ?? translate('chat.renderFailed')}
        </p>
        {this.props.fallbackText ? (
          <pre className="mt-1.5 max-h-60 overflow-auto whitespace-pre-wrap break-anywhere font-mono text-[length:var(--text-2xs)] text-[length:var(--text-sm)] text-[var(--text-secondary)]">
            {this.props.fallbackText}
          </pre>
        ) : null}
      </div>
    );
  }
}
