import { useEffect, useRef, useState } from 'react';
import { useInstanceStore } from '@/lib/stores/instances';
import { translate } from '@/lib/i18n';
import { cn, formatDuration } from '@/lib/utils';
import { agentApi, followRun, type AgentEvent, type AgentToolPreview, type ApprovalDecision } from '@/lib/agent/client';
import { Button } from '@/components/ui/Button';
import { Code, EmptyState, SectionHeader } from '@/components/ui/Feedback';
import { Field, Input, PathInput } from '@/components/ui/Input';

/**
 * Agent workbench.
 *
 * Follows a server-side run and renders three things the user must see to trust it:
 *
 * 1. **What the model is doing** — assistant text and tool activity, in order.
 * 2. **What a pending call would touch** — the resolved path or the full argv, the
 *    working directory, and a risk level. A prompt that only says "allow
 *    run_command" gives the user nothing to judge.
 * 3. **Why it stopped** — a budget, a denial, an error or a cancellation, stated
 *    plainly. A run that simply goes quiet is indistinguishable from a hang.
 *
 * The approval buttons are the only way past a write or a command; this component
 * never auto-approves anything, and it has no "trust this folder" shortcut that would
 * remove the per-call decision the plan committed to.
 */
export function AgentWorkbench() {
  const instances = useInstanceStore((state) => state.instances);
  const chatInstances = instances.filter(
    (instance) => instance.serves_chat && instance.status === 'running',
  );

  const [instanceId, setInstanceId] = useState<string>('');
  const [root, setRoot] = useState('');
  const [prompt, setPrompt] = useState('');
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [status, setStatus] = useState<'idle' | 'running' | 'done' | 'failed' | 'cancelled'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<{ approvalId: string; preview: AgentToolPreview } | null>(null);
  const [busy, setBusy] = useState(false);

  const stopRef = useRef<(() => void) | null>(null);

  useEffect(() => () => stopRef.current?.(), []);

  const activeInstance = instanceId || chatInstances[0]?.id || '';

  const start = async () => {
    setError(null);
    // A second Start while a run is live would open a second stream on the same
    // run: duplicate `seq` keys, duplicated history, and two approval prompts for
    // one call. `busy` alone does not cover it — it clears as soon as `start()`
    // returns, while the run keeps streaming (audit L6).
    if (status === 'running') return;
    if (!activeInstance) {
      setError(translate('agent.needInstance'));
      return;
    }
    if (!root.trim()) {
      setError(translate('agent.needFolder'));
      return;
    }
    if (!prompt.trim()) {
      setError(translate('agent.needPrompt'));
      return;
    }
    setBusy(true);
    try {
      const created = await agentApi.start({
        conversationId: 'agent-' + Date.now().toString(36),
        instanceId: activeInstance,
        root: root.trim(),
        messages: [{ role: 'user', content: prompt.trim() }],
      });
      setRunId(created.run_id);
      setEvents([]);
      setStatus('running');
      setPending(null);
      stopRef.current = followRun(created.run_id, {
        onEvent: (event) => {
          setEvents((current) => [...current, event]);
          if (event.type === 'approval_requested') {
            const preview = event.preview as AgentToolPreview | undefined;
            const approvalId = String(event.approval_id ?? '');
            if (preview && approvalId) setPending({ approvalId, preview });
          }
          if (event.type === 'tool_started' || event.type === 'tool_result' || event.type === 'tool_denied') {
            setPending(null);
          }
          if (event.type === 'error') {
            setError(String(event.message ?? translate('common.error')));
          }
        },
        onEnd: (final) => {
          setStatus((final as typeof status) ?? 'done');
          setPending(null);
        },
        onTransportError: (message) => setError(message),
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : translate('common.error'));
    } finally {
      setBusy(false);
    }
  };

  const decide = async (decision: ApprovalDecision) => {
    if (!runId || !pending) return;
    setBusy(true);
    try {
      await agentApi.approve(runId, pending.approvalId, decision);
      setPending(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : translate('common.error'));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!runId) return;
    try {
      await agentApi.cancel(runId);
      setStatus('cancelled');
      setPending(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : translate('common.error'));
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="border-b border-[var(--border)] px-5 pb-5 pt-4">
        <SectionHeader
          title={translate('agent.title')}
          description={translate('agent.subtitle')}
        />
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Field label={translate('agent.instance')} htmlFor="agent-instance">
            <select
              id="agent-instance"
              value={activeInstance}
              onChange={(event) => setInstanceId(event.target.value)}
              className="h-[var(--control-h)] w-full rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-sm)] outline-none focus:ring-2 focus:ring-[var(--focus-ring)]"
            >
              {chatInstances.length === 0 ? (
                <option value="">{translate('agent.noInstances')}</option>
              ) : (
                chatInstances.map((instance) => (
                  <option key={instance.id} value={instance.id}>
                    {instance.name}
                  </option>
                ))
              )}
            </select>
          </Field>
          <Field
            label={translate('agent.folder')}
            hint={translate('agent.folderHint')}
            htmlFor="agent-root"
          >
            {/* The authorized folder bounds everything the agent can reach, so it is
                picked, not typed. */}
            <PathInput
              id="agent-root"
              kind="dir"
              value={root}
              onValueChange={setRoot}
              placeholder="D:\\projects\\demo"
            />
          </Field>
        </div>
        <div className="mt-3 flex items-end gap-2">
          <Field label={translate('agent.prompt')} htmlFor="agent-prompt" className="flex-1">
            <Input
              id="agent-prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder={translate('agent.promptPlaceholder')}
              readOnly={status === 'running'}
            />
          </Field>
          {status === 'running' ? (
            <Button variant="danger" onClick={() => void cancel()}>
              {translate('agent.cancel')}
            </Button>
          ) : (
            <Button variant="primary" onClick={() => void start()} loading={busy}>
              {translate('agent.start')}
            </Button>
          )}
        </div>
        {error ? (
          <p role="alert" className="mt-2 text-[length:var(--text-xs)] text-[var(--danger)]">
            {error}
          </p>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        {events.length === 0 ? (
          <EmptyState
            title={translate('agent.idle')}
            description={translate('agent.idleHint')}
          />
        ) : (
          <ol className="mx-auto flex max-w-[52rem] flex-col gap-2">
            {events.map((event) => (
              <EventRow key={event.seq} event={event} />
            ))}
          </ol>
        )}
      </div>

      {pending ? (
        <div className="border-t border-[var(--warning)]/40 bg-[var(--warning-subtle)] px-5 py-3">
          <ApprovalCard
            preview={pending.preview}
            busy={busy}
            onDecide={(decision) => void decide(decision)}
          />
        </div>
      ) : null}
    </div>
  );
}

function ApprovalCard({
  preview,
  busy,
  onDecide,
}: {
  preview: AgentToolPreview;
  busy: boolean;
  onDecide: (decision: ApprovalDecision) => void;
}) {
  const highRisk = preview.risk === 'high';
  return (
    <div className="mx-auto max-w-[52rem]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[length:var(--text-xs)] font-semibold text-[var(--warning)]">
          {translate('agent.approvalTitle')}
        </span>
        <Code>{preview.tool}</Code>
        <span
          className={cn(
            'rounded-[var(--radius-pill)] px-1.5 py-[1px] text-[length:var(--text-2xs)]',
            highRisk
              ? 'bg-[var(--danger-subtle)] text-[var(--danger)]'
              : 'bg-[var(--warning-subtle)] text-[var(--warning)]',
          )}
        >
          {translate(`agent.risk.${preview.risk}`)}
        </span>
      </div>

      {preview.path ? (
        <p className="mt-1.5 break-anywhere font-mono text-[length:var(--text-xs)] text-[var(--text-secondary)]">
          {preview.path}
        </p>
      ) : null}

      {preview.argv ? (
        <div className="mt-1.5 rounded-[var(--radius-sm)] bg-[var(--code-bg)] p-2">
          <p className="font-mono text-[length:var(--text-2xs)] text-[var(--code-gutter)]">
            {preview.cwd ?? ''}
          </p>
          <pre className="whitespace-pre-wrap break-anywhere font-mono text-[length:var(--text-xs)] text-[var(--code-fg)]">
            {preview.argv.join(' ')}
          </pre>
        </div>
      ) : null}

      {preview.preview ? (
        <div className="mt-1.5 grid gap-1.5 md:grid-cols-2">
          <pre className="max-h-40 max-w-full overflow-auto whitespace-pre-wrap break-anywhere rounded-[var(--radius-sm)] bg-[var(--surface)] p-2 font-mono text-[length:var(--text-2xs)] text-[var(--danger)]">
            {preview.preview.old}
          </pre>
          <pre className="max-h-40 max-w-full overflow-auto whitespace-pre-wrap break-anywhere rounded-[var(--radius-sm)] bg-[var(--surface)] p-2 font-mono text-[length:var(--text-2xs)] text-[var(--success)]">
            {preview.preview.new}
          </pre>
        </div>
      ) : null}

      {typeof preview.content === 'string' ? (
        <div className="mt-1.5">
          <p className="meta-text">
            {translate('agent.writeContent', {
              bytes: Number(preview.bytes ?? 0),
              chars: Number(preview.content_chars ?? 0),
            })}
          </p>
          <pre
            data-testid="approval-write-content"
            className="mt-1 max-h-40 max-w-full overflow-auto whitespace-pre-wrap break-anywhere rounded-[var(--radius-sm)] bg-[var(--code-bg)] p-2 font-mono text-[length:var(--text-2xs)] text-[var(--code-fg)]"
          >
            {preview.content}
          </pre>
        </div>
      ) : null}

      <div className="mt-2 flex flex-wrap gap-2">
        <Button variant="primary" onClick={() => onDecide('allow_once')} disabled={busy}>
          {translate('agent.allowOnce')}
        </Button>
        <Button variant="secondary" onClick={() => onDecide('allow_session')} disabled={busy}>
          {translate('agent.allowSession')}
        </Button>
        <Button variant="dangerGhost" onClick={() => onDecide('deny')} disabled={busy}>
          {translate('agent.deny')}
        </Button>
      </div>
    </div>
  );
}

function EventRow({ event }: { event: AgentEvent }) {
  const at = typeof event.at === 'number' ? event.at : 0;
  const elapsed = at ? formatDuration(Math.max(0, Date.now() / 1000 - at)) : '';

  if (event.type === 'turn_started') {
    return (
      <li className="meta-text flex items-center gap-2">
        <span className="h-px flex-1 bg-[var(--border)]" />
        {translate('agent.turn', { turn: Number(event.turn ?? 0) })}
        <span className="h-px flex-1 bg-[var(--border)]" />
      </li>
    );
  }

  if (event.type === 'assistant_message') {
    const content = String(event.content ?? '');
    const reasoning = String(event.reasoning ?? '');
    return (
      <li className="rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] p-3">
        {reasoning ? (
          <details className="mb-2">
            <summary className="cursor-pointer text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
              {translate('chat.thinking')}
            </summary>
            <p className="trace mt-1 whitespace-pre-wrap">{reasoning}</p>
          </details>
        ) : null}
        <p className="whitespace-pre-wrap text-[length:var(--text-md)] leading-[var(--leading-cjk)]">
          {content || translate('agent.emptyReply')}
        </p>
      </li>
    );
  }

  if (event.type === 'tool_call') {
    const preview = event.preview as AgentToolPreview | undefined;
    return (
      <li className="rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface-sunken)] p-2.5">
        <p className="flex flex-wrap items-center gap-1.5 text-[length:var(--text-xs)]">
          <Code>{String(event.tool ?? '')}</Code>
          <span className="break-anywhere font-mono text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
            {preview?.summary ?? ''}
          </span>
        </p>
      </li>
    );
  }

  if (event.type === 'tool_result') {
    const ok = Boolean(event.ok);
    return (
      <li
        className={cn(
          'rounded-[var(--radius-md)] border p-2.5',
          ok ? 'border-[var(--border)] bg-[var(--surface)]' : 'border-[var(--danger)]/40 bg-[var(--danger-subtle)]',
        )}
      >
        <p className="flex items-center gap-2 text-[length:var(--text-xs)]">
          <Code>{String(event.tool ?? '')}</Code>
          <span className={ok ? 'text-[var(--success)]' : 'text-[var(--danger)]'}>
            {ok ? translate('agent.ok') : translate('agent.failed')}
          </span>
          {elapsed ? <span className="tnum text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">{elapsed}</span> : null}
        </p>
        <pre className="mt-1.5 max-h-60 overflow-auto whitespace-pre-wrap break-anywhere font-mono text-[length:var(--text-2xs)] text-[var(--text-secondary)]">
          {String(event.output ?? '')}
        </pre>
      </li>
    );
  }

  if (event.type === 'tool_denied') {
    return (
      <li className="rounded-[var(--radius-md)] border border-[var(--warning)]/40 bg-[var(--warning-subtle)] p-2.5 text-[length:var(--text-xs)] text-[var(--warning)]">
        {translate('agent.denied', { tool: String(event.tool ?? '') })}
      </li>
    );
  }

  if (event.type === 'error') {
    return (
      <li
        role="alert"
        className="rounded-[var(--radius-md)] border border-[var(--danger)]/40 bg-[var(--danger-subtle)] p-2.5 text-[length:var(--text-xs)] text-[var(--danger)]"
      >
        {String(event.message ?? translate('common.error'))}
      </li>
    );
  }

  if (event.type === 'done' || event.type === 'cancelled') {
    return (
      <li className="meta-text text-center">
        {translate(event.type === 'done' ? 'agent.finished' : 'agent.cancelled')}
      </li>
    );
  }

  return null;
}
