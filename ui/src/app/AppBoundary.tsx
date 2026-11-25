import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * Last-resort boundary around the whole application.
 *
 * The per-message `MessageBoundary` handles untrusted content. This one exists for
 * everything else: a page module that throws while rendering (a malformed backend
 * payload reaching a `.map`, a hook invariant violated by a race) would otherwise
 * leave the window blank — the exact failure mode that made the 2026-11-03 "903
 * tests green, product unusable" incident so hard to diagnose from a user report.
 *
 * **No inline colours.** The first draft hard-coded the palette here, on the
 * reasoning that a crashed app might have lost its stylesheet. That reasoning is
 * wrong in this shell: `app.css` is a bundled asset imported by `main.tsx`, so if
 * it failed to load the app never rendered at all and this boundary would never
 * run. Meanwhile the colour literals broke AGENTS §2.36, which exists because a
 * theme override cannot reach a baked value. The tokens below are available here
 * exactly as they are everywhere else.
 */
interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class AppBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // No console line here would make a packaged-mode blank window undiagnosable:
    // devtools are off in release builds, so the log transport is the only trace.
    console.error('application render failed', error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div
        role="alert"
        data-testid="app-crash"
        className="grid min-h-screen place-items-center bg-[var(--canvas)] p-6 text-[var(--text)]"
      >
        <div className="max-w-xl">
          <h1 className="mb-2 text-[length:var(--text-lg)] font-semibold">
            The interface hit an unexpected error · 界面遇到意外错误
          </h1>
          <p className="mb-3 text-[length:var(--text-sm)] text-[var(--text-secondary)]">
            Reloading usually recovers. If it happens again, the detail below is worth
            reporting.
            <br />
            重新加载通常即可恢复；若反复出现，请附上下面的信息。
          </p>
          <pre className="max-h-64 overflow-auto rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] p-3 font-mono text-[length:var(--text-2xs)] whitespace-pre-wrap break-words">
            {String(this.state.error?.stack ?? this.state.error?.message ?? this.state.error)}
          </pre>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-3 h-[var(--control-h)] rounded-[var(--radius-md)] fill-primary px-3.5 text-[length:var(--text-sm)] font-medium"
          >
            Reload · 重新加载
          </button>
        </div>
      </div>
    );
  }
}
