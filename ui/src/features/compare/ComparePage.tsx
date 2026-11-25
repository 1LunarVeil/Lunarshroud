import { useCallback, useMemo, useRef, useState } from 'react';
import { Columns2, Send, Square } from 'lucide-react';
import type { Instance } from '@/lib/api/types';
import { translate } from '@/lib/i18n';
import { cn, formatDuration } from '@/lib/utils';
import { Button } from '@/components/ui/Button';
import { EmptyState, SectionHeader } from '@/components/ui/Feedback';
import { Markdown } from '@/components/chat/Markdown';
import { MessageBoundary } from '@/components/chat/MessageBoundary';
import { useChatInstances } from '@/features/chat/queries';
import { useChatStream } from '@/features/chat/useChatStream';
import { resolveChatSettings } from '@/features/chat/settings';

/**
 * Compare two models on one prompt.
 *
 * **No backend work is involved.** The backend already runs several instances at
 * once and the completion route is per-instance and stateless, so the only thing
 * that ever needed building is a page that drives two streams. It uses the same
 * `useChatStream` hook twice — two independent instances of it, not one hook
 * shared — so either column can be stopped without touching the other. That is
 * the whole reason this is a page rather than a flag on the chat page: one
 * `abortRef` per page was the blocker.
 *
 * Deliberate limits, stated so they are not mistaken for bugs:
 *
 * - The paired answers are **not persisted**. A comparison is a reading, not a
 *   conversation; saving both halves would double every experiment in the
 *   sidebar. "Keep this answer" would be the natural next step and is not here.
 * - No tool calls: comparing tool behaviour needs an approval channel per column,
 *   and the point of this page is a fast A/B on prose.
 */
export function ComparePage() {
  const instances = useChatInstances();
  const running = instances.data ?? [];

  const [leftId, setLeftId] = useState('');
  const [rightId, setRightId] = useState('');
  const [prompt, setPrompt] = useState('');

  const left = useChatStream();
  const right = useChatStream();

  const leftInstance: Instance | null = useMemo(
    () => running.find((instance) => instance.id === leftId) ?? running[0] ?? null,
    [running, leftId],
  );
  const rightInstance: Instance | null = useMemo(
    () =>
      running.find((instance) => instance.id === rightId) ??
      running.find((instance) => instance.id !== leftInstance?.id) ??
      null,
    [running, rightId, leftInstance?.id],
  );

  // Guard against comparing a model with itself: the second column would be a
  // duplicate, and the user would think the point of the page had failed.
  const sameModel = Boolean(
    leftInstance?.id && rightInstance?.id && leftInstance.id === rightInstance.id,
  );
  const canRun = Boolean(prompt.trim() && leftInstance && rightInstance && !sameModel);

  const runColumn = useCallback(
    (side: 'left' | 'right') => {
      const instance = side === 'left' ? leftInstance : rightInstance;
      const stream = side === 'left' ? left : right;
      if (!instance) return;
      const settings = resolveChatSettings(null);
      void stream.run({
        conversationId: `compare-${side}-${Date.now()}`,
        instanceId: instance.id,
        messages: [{ role: 'user', content: prompt.trim() }],
        attachments: [],
        params: settings.params,
        systemPrompt: settings.systemPrompt,
        knowledgeBaseId: null,
        toolRoot: null,
        // No tools in compare mode: an approval gate per column would turn a
        // fast A/B into two half-finished runs.
        declaredTools: [],
        supportsVision: Boolean(instance.supports_vision),
        thinking: settings.thinking,
        locale: 'zh-CN',
      });
    },
    [left, right, leftInstance, rightInstance, prompt],
  );

  const runBoth = async () => {
    if (!canRun) return;
    // Both start together: a sequential run would make the second model's latency
    // include the first one's, which is exactly what this page exists to compare.
    await Promise.all([runColumn('left'), runColumn('right')]);
  };

  if (instances.isPending) return <SectionHeader title={translate('compare.title')} />;

  if (running.length < 2) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <EmptyState
          icon={<Columns2 size={20} />}
          title={translate('compare.needTwo')}
          description={translate('compare.needTwoHelp')}
        />
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="border-b border-[var(--border)] px-5 pb-3 pt-4">
        <SectionHeader title={translate('compare.title')} description={translate('compare.subtitle')} />
      </div>

      <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] px-5 py-2">
        <ModelPicker
          label={translate('compare.left')}
          value={leftInstance?.id ?? ''}
          instances={running}
          onChange={setLeftId}
        />
        <ModelPicker
          label={translate('compare.right')}
          value={rightInstance?.id ?? ''}
          instances={running}
          onChange={setRightId}
        />
        {sameModel ? (
          <span role="alert" className="text-[length:var(--text-2xs)] text-[var(--danger)]">
            {translate('compare.sameModel')}
          </span>
        ) : null}
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-px overflow-hidden bg-[var(--border)] md:grid-cols-2">
        <CompareColumn
          side="left"
          instance={leftInstance}
          stream={left}
          streaming={left.streaming}
          onStop={left.stop}
        />
        <CompareColumn
          side="right"
          instance={rightInstance}
          stream={right}
          streaming={right.streaming}
          onStop={right.stop}
        />
      </div>

      <div className="flex items-end gap-2 border-t border-[var(--border)] px-5 py-3">
        <textarea
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              void runBoth();
            }
          }}
          rows={2}
          placeholder={translate('compare.promptPlaceholder')}
          aria-label={translate('compare.promptPlaceholder')}
          className="min-h-[var(--control-h-lg)] flex-1 resize-y rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-3 py-2 text-[length:var(--text-sm)]"
        />
        <Button
          variant="primary"
          disabled={!canRun}
          loading={left.streaming || right.streaming}
          onClick={() => void runBoth()}
        >
          <Send size={13} aria-hidden="true" />
          {translate('compare.run')}
        </Button>
      </div>
    </div>
  );
}

function ModelPicker({
  label,
  value,
  instances,
  onChange,
}: {
  label: string;
  value: string;
  instances: Array<{ id: string; name: string; model_path: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
      {label}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-[var(--control-h-sm)] max-w-[16rem] rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-1.5 text-[length:var(--text-xs)] text-[var(--text)]"
      >
        {instances.map((instance) => (
          <option key={instance.id} value={instance.id}>
            {instance.name || instance.model_path}
          </option>
        ))}
      </select>
    </label>
  );
}

function CompareColumn({
  side,
  instance,
  stream,
  streaming,
  onStop,
}: {
  side: 'left' | 'right';
  instance: Instance | null;
  stream: ReturnType<typeof useChatStream>;
  streaming: boolean;
  onStop: () => void;
}) {
  const scroller = useRef<HTMLDivElement | null>(null);
  const content = stream.live.content;
  const reasoning = stream.live.reasoning;
  const stats = stream.live.usage;

  return (
    <section
      data-testid={`compare-${side}`}
      className="flex min-h-0 flex-col overflow-hidden bg-[var(--surface)]"
    >
      <header className="flex items-center gap-2 border-b border-[var(--border)] px-3 py-1.5">
        <span className="min-w-0 flex-1 truncate text-[length:var(--text-xs)] font-medium">
          {instance?.name || instance?.model_path || translate('compare.noModel')}
        </span>
        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            aria-label={translate('chat.stop')}
            className="grid size-6 place-items-center rounded-[var(--radius-sm)] text-[var(--danger)] hover:bg-[var(--danger-subtle)]"
          >
            <Square size={11} aria-hidden="true" />
          </button>
        ) : null}
        {stats ? (
          <span className="shrink-0 text-[length:var(--text-2xs)] tnum text-[var(--text-tertiary)]">
            {typeof stats.completion_tokens === 'number' ? `${stats.completion_tokens} tok` : ''}
          </span>
        ) : null}
        {stream.elapsed ? (
          <span className="shrink-0 text-[length:var(--text-2xs)] tnum text-[var(--text-tertiary)]">
            {formatDuration(stream.elapsed)}
          </span>
        ) : null}
      </header>

      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {reasoning ? (
          <details className="mb-2 rounded-[var(--radius-sm)] border border-[var(--border)] p-2">
            <summary className="cursor-pointer text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
              {translate('chat.thinking')}
            </summary>
            <p className="mt-1 whitespace-pre-wrap text-[length:var(--text-2xs)] text-[var(--text-secondary)]">
              {reasoning}
            </p>
          </details>
        ) : null}
        {content ? (
          <MessageBoundary fallbackText={content}>
            <Markdown text={content} className="markdown" />
          </MessageBoundary>
        ) : streaming ? (
          <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
            {translate('chat.generating')}
          </p>
        ) : (
          <p className={cn('text-[length:var(--text-2xs)] text-[var(--text-quaternary)]')}>
            {translate('compare.empty')}
          </p>
        )}
        {stream.live.finishReason === 'length' ? (
          <p className="mt-2 text-[length:var(--text-2xs)] text-[var(--warning)]">
            {translate('chat.thinkingExhaustedShort')}
          </p>
        ) : null}
      </div>
    </section>
  );
}

/** Exported for the route table; the page owns no server state. */
export default ComparePage;
