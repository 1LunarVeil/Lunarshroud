import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Download, Info, Pause, Play, RotateCcw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { useInstanceStore } from '@/lib/stores/instances';
import { instancesApi, historyApi, systemApi } from '@/lib/api';
import type { HistoryRecord, LogLine } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn, formatDuration, formatTime } from '@/lib/utils';
import { Badge, EmptyState, LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';

/**
 * Observability: live logs, resource monitor, launch history.
 *
 * Logs stream over a WebSocket that reconnects with backoff (the shared socket helper
 * refreshes the session token before each attempt, because a restarted backend issues a
 * new one). The buffer is bounded on **both** line count and bytes: a chatty model can
 * emit megabytes in seconds, and 1.0 learned that a line cap alone is not a memory cap.
 */
type Tab = 'logs' | 'monitor' | 'history';

const MAX_LINES = 5000;
const MAX_BYTES = 4 * 1024 * 1024;

export function ObservabilityPage() {
  const [tab, setTab] = useState<Tab>('logs');
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="border-b border-[var(--border)] px-5 pt-4">
        <SectionHeader title={translate('nav.observability')} description={translate('observability.subtitle')} />
        <div role="tablist" aria-label={translate('nav.observability')} className="mt-3 flex gap-1">
          {(
            [
              ['logs', 'nav.logs'],
              ['monitor', 'nav.monitor'],
              ['history', 'nav.history'],
            ] as const
          ).map(([id, labelKey]) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              data-testid={`obs-tab-${id}`}
              onClick={() => setTab(id)}
              className={cn(
                'relative h-8 px-3 text-[length:var(--text-sm)]',
                tab === id ? 'font-medium text-[var(--text)]' : 'text-[var(--text-tertiary)] hover:text-[var(--text)]',
              )}
            >
              {translate(labelKey)}
              {tab === id ? (
                <span aria-hidden="true" className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-[var(--accent)]" />
              ) : null}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        {tab === 'logs' ? <LogsPanel /> : tab === 'monitor' ? <MonitorPanel /> : <HistoryPanel />}
      </div>
    </div>
  );
}

function LogsPanel() {
  const instances = useInstanceStore((state) => state.instances);
  const [instanceId, setInstanceId] = useState('');
  const [lines, setLines] = useState<LogLine[]>([]);
  const [paused, setPaused] = useState(false);
  const [level, setLevel] = useState<'all' | 'stdout' | 'stderr' | 'system'>('all');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState<'connecting' | 'connected' | 'reconnecting'>('connecting');
  const bytes = useRef(0);
  const scroller = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);

  const running = instances.filter((i) => i.status === 'running' || i.status === 'starting');
  const active = instanceId || running[0]?.id || '';
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  useEffect(() => {
    if (!active) {
      setLines([]);
      return;
    }
    setLines([]);
    bytes.current = 0;
    const watcher = instancesApi.watchLogs(active, {
      onMessage: (message) => {
        if (pausedRef.current) return;
        const payload = message as { type?: string; line?: LogLine };
        if (payload.type !== 'logs' || !payload.line) return;
        const incoming = payload.line;
        bytes.current += incoming.line.length;
        setLines((current) => {
          let next = [...current, incoming];
          // Line cap first, then a byte cap: a single enormous line would sail past a
          // line-only bound (measured: one minified blob was 3.9 MB).
          if (next.length > MAX_LINES) next = next.slice(next.length - MAX_LINES);
          while (bytes.current > MAX_BYTES && next.length > 1) {
            const dropped = next.shift();
            bytes.current -= dropped ? dropped.line.length : 0;
          }
          return next;
        });
      },
      onStatus: (value) => setStatus(value === 'connected' ? 'connected' : value === 'closed' ? 'reconnecting' : 'reconnecting'),
    });
    return () => watcher.close();
  }, [active]);

  useEffect(() => {
    if (!stick.current) return;
    const node = scroller.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [lines]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return lines.filter((line) => {
      if (level !== 'all' && line.source !== level) return false;
      if (needle && !line.line.toLowerCase().includes(needle)) return false;
      return true;
    });
  }, [lines, level, search]);

  const download = () => {
    const body = filtered.map((line) => `${line.timestamp} [${line.source}] ${line.line}`).join('\n');
    const blob = new Blob([body], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `lunarshroud-logs-${Date.now()}.txt`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] px-4 py-2">
        <select
          value={active}
          onChange={(event) => setInstanceId(event.target.value)}
          aria-label={translate('nav.logs')}
          className="h-[var(--control-h-sm)] rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-xs)] outline-none"
        >
          {running.length === 0 ? (
            <option value="">{translate('logs.noInstances')}</option>
          ) : (
            running.map((instance) => (
              <option key={instance.id} value={instance.id}>
                {instance.name}
              </option>
            ))
          )}
        </select>

        <select
          value={level}
          onChange={(event) => setLevel(event.target.value as typeof level)}
          aria-label={translate('logs.level')}
          className="h-[var(--control-h-sm)] rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-xs)] outline-none"
        >
          <option value="all">{translate('logs.all')}</option>
          <option value="stdout">stdout</option>
          <option value="stderr">stderr</option>
          <option value="system">system</option>
        </select>

        <Input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder={translate('logs.searchPh')}
          inputSize="sm"
          className="max-w-[16rem]"
        />

        <span className="text-[length:var(--text-2xs)] tnum text-[var(--text-tertiary)]">
          {filtered.length} / {lines.length}
        </span>
        <Badge tone={status === 'connected' ? 'success' : 'warning'} dot>
          {status}
        </Badge>

        <span className="flex-1" />
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setPaused((value) => !value)}
          aria-pressed={paused}
          aria-label={paused ? translate('logs.resume') : translate('logs.pause')}
        >
          {paused ? <Play size={13} aria-hidden="true" /> : <Pause size={13} aria-hidden="true" />}
          {paused ? translate('logs.resume') : translate('logs.pause')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setLines([])}
          aria-label={translate('logs.clear')}
          title={translate('logs.clear')}
        >
          <Trash2 size={13} aria-hidden="true" />
        </Button>
        <Button
          size="sm"
          variant="ghost"
          onClick={download}
          disabled={!filtered.length}
          aria-label={translate('logs.download')}
          title={translate('logs.download')}
        >
          <Download size={13} aria-hidden="true" />
        </Button>
      </div>

      <div
        ref={scroller}
        onScroll={() => {
          const node = scroller.current;
          if (!node) return;
          stick.current = node.scrollHeight - node.scrollTop - node.clientHeight < 40;
        }}
        data-testid="log-view"
        className="min-h-0 flex-1 overflow-auto bg-[var(--code-bg)] p-2"
      >
        {!active ? (
          <div className="p-6 text-center">
            <p className="font-mono text-[length:var(--text-sm)] text-[var(--code-fg)]">
              {translate('logs.noInstances')}
            </p>
            <p className="mt-1 font-mono text-[length:var(--text-2xs)] text-[var(--code-gutter)]">
              {translate('logs.emptySub')}
            </p>
          </div>
        ) : filtered.length === 0 ? (
          <p className="p-6 text-center font-mono text-[length:var(--text-xs)] text-[var(--code-gutter)]">
            {translate('logs.empty')}
          </p>
        ) : (
          <ol className="flex flex-col">
            {filtered.map((line, index) => (
              <li
                key={`${line.timestamp}-${index}`}
                className={cn(
                  'whitespace-pre-wrap break-anywhere font-mono text-[length:var(--text-2xs)] leading-[1.5]',
                  line.source === 'stderr' ? 'text-[var(--danger)]' : 'text-[var(--code-fg)]',
                )}
              >
                <span className="select-none text-[var(--code-gutter)]">{line.timestamp} </span>
                {line.line}
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}

function MonitorPanel() {
  const overview = useQuery({ queryKey: ['system', 'overview'], queryFn: systemApi.overview, refetchInterval: 3000 });
  const data = overview.data;

  if (overview.isPending) return <LoadingState />;

  return (
    <div className="h-full overflow-y-auto p-5">
      <div className="mx-auto grid max-w-[56rem] gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <MetricCard
          label={translate('sidebar.chipCpu')}
          value={data?.cpu_percent === undefined ? '—' : `${Math.round(data.cpu_percent)}%`}
        />
        <MetricCard
          label={translate('sidebar.chipRam')}
          value={
            data
              ? `${data.memory_used_gb.toFixed(1)} / ${data.memory_total_gb.toFixed(1)} GiB`
              : '—'
          }
        />
        <MetricCard
          label={translate('sidebar.chipVram')}
          value={
            data?.gpus?.[0]
              ? `${Math.round(data.gpus[0].memory_used_mb / 1024)} / ${Math.round(data.gpus[0].memory_total_mb / 1024)} GiB`
              : translate('sidebar.chipNoGpu')
          }
          muted={!data?.gpus?.length}
        />
      </div>

      {data?.gpus?.length ? (
        <div className="mx-auto mt-4 max-w-[56rem] rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)]">
          <table className="w-full text-[length:var(--text-xs)]">
            <thead>
              <tr className="border-b border-[var(--border)] text-[var(--text-tertiary)]">
                <th className="px-3 py-1.5 text-start font-medium">GPU</th>
                <th className="px-3 py-1.5 text-end font-medium">{translate('monitor.utilization')}</th>
                <th className="px-3 py-1.5 text-end font-medium">{translate('monitor.vram')}</th>
                <th className="px-3 py-1.5 text-end font-medium">{translate('monitor.temp')}</th>
              </tr>
            </thead>
            <tbody>
              {data.gpus.map((gpu) => (
                <tr key={gpu.index} className="border-b border-[var(--border-faint,var(--border))] last:border-0">
                  <td className="max-w-[18rem] truncate px-3 py-1.5">{gpu.name}</td>
                  <td className="px-3 py-1.5 text-end tnum">
                    {gpu.utilization === null ? '—' : `${Math.round(gpu.utilization)}%`}
                  </td>
                  <td className="px-3 py-1.5 text-end tnum">
                    {Math.round(gpu.memory_used_mb / 1024)} / {Math.round(gpu.memory_total_mb / 1024)} GiB
                  </td>
                  <td className="px-3 py-1.5 text-end tnum">
                    {gpu.temperature === null ? '—' : `${Math.round(gpu.temperature)}°C`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function MetricCard({ label, value, muted = false }: { label: string; value: string; muted?: boolean }) {
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] p-3">
      <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">{label}</p>
      <p
        className={cn(
          'mt-1 text-[length:var(--text-lg)] font-semibold tnum',
          muted ? 'text-[var(--text-tertiary)]' : 'text-[var(--text)]',
        )}
      >
        {value}
      </p>
    </div>
  );
}

function HistoryPanel() {
  const [offset, setOffset] = useState(0);
  const [detailId, setDetailId] = useState<number | null>(null);
  const limit = 50;
  const history = useQuery({
    queryKey: ['history', offset],
    queryFn: () => historyApi.list(limit, offset),
  });

  const records: HistoryRecord[] = history.data?.records ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-[var(--border)] px-4 py-2">
        <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
          {translate('history.prevPage')}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={records.length < limit}
          onClick={() => setOffset(offset + limit)}
        >
          {translate('history.nextPage')}
        </Button>
        <span className="flex-1" />
        <Button
          size="sm"
          variant="dangerGhost"
          onClick={async () => {
            try {
              await historyApi.clear();
              void history.refetch();
            } catch {
              // Clearing is best-effort from the UI; the list refresh is the evidence.
            }
          }}
        >
          {translate('history.clearAll')}
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {history.isPending ? (
          <LoadingState />
        ) : records.length === 0 ? (
          <EmptyState title={translate('history.empty')} description={translate('history.emptySub')} />
        ) : (
          <table className="w-full text-[length:var(--text-xs)]">
            <thead>
              <tr className="sticky top-0 border-b border-[var(--border)] bg-[var(--panel)] text-[var(--text-tertiary)]">
                <th className="px-3 py-1.5 text-start font-medium">{translate('history.time')}</th>
                <th className="px-3 py-1.5 text-start font-medium">{translate('history.status')}</th>
                <th className="px-3 py-1.5 text-end font-medium">{translate('history.duration')}</th>
                <th className="px-3 py-1.5 text-start font-medium">{translate('history.modelKey')}</th>
                <th className="px-3 py-1.5 text-end font-medium">
                  <span className="sr-only">{translate('history.detailTitle')}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {records.map((record) => (
                <tr key={record.id} className="group/row border-b border-[var(--border-faint,var(--border))]">
                  <td className="whitespace-nowrap px-3 py-1.5 tnum">{formatTime(record.started_at)}</td>
                  <td className="px-3 py-1.5">
                    <Badge
                      tone={
                        record.status === 'stopped'
                          ? 'neutral'
                          : record.status === 'failed'
                            ? 'danger'
                            : record.status === 'running'
                              ? 'success'
                              : 'warning'
                      }
                      dot
                    >
                      {translate(`badge.${record.status}`)}
                    </Badge>
                  </td>
                  <td className="px-3 py-1.5 text-end tnum">{formatDuration(record.duration_seconds)}</td>
                  <td className="max-w-[22rem] truncate px-3 py-1.5 font-mono" title={record.model_path ?? ''}>
                    {record.model_path ? record.model_path.split(/[\\/]/).pop() : '—'}
                  </td>
                  {/*
                    Three routes had no caller: detail, relaunch and a single-row
                    delete. They are what makes History a record instead of a
                    read-only log — "that launch worked, run it again" is the whole
                    reason anyone opens this tab.
                  */}
                  <td className="whitespace-nowrap px-3 py-1.5 text-end">
                    <div className="flex items-center justify-end gap-0.5 opacity-0 transition-opacity group-hover/row:opacity-100 group-focus-within/row:opacity-100">
                      <button
                        type="button"
                        onClick={() => setDetailId(record.id)}
                        aria-label={translate('history.detailTitle')}
                        title={translate('history.detailTitle')}
                        className="grid size-6 place-items-center rounded-[var(--radius-sm)] text-[var(--text-tertiary)] hover:bg-[var(--hover)] hover:text-[var(--text)]"
                      >
                        <Info size={12} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        data-testid={`history-relaunch-${record.id}`}
                        onClick={async () => {
                          try {
                            await historyApi.relaunch(record.id);
                            toast.success(translate('history.relaunched'));
                          } catch (error) {
                            toast.error(
                              localizeBackendError(error instanceof Error ? error.message : error),
                            );
                          }
                        }}
                        aria-label={translate('history.relaunch')}
                        title={translate('history.relaunch')}
                        className="grid size-6 place-items-center rounded-[var(--radius-sm)] text-[var(--text-tertiary)] hover:bg-[var(--hover)] hover:text-[var(--text)]"
                      >
                        <RotateCcw size={12} aria-hidden="true" />
                      </button>
                      <button
                        type="button"
                        onClick={async () => {
                          if (!window.confirm(translate('history.deleteConfirm'))) return;
                          try {
                            await historyApi.remove(record.id);
                            void history.refetch();
                          } catch (error) {
                            toast.error(
                              localizeBackendError(error instanceof Error ? error.message : error),
                            );
                          }
                        }}
                        aria-label={translate('common.delete')}
                        className="grid size-6 place-items-center rounded-[var(--radius-sm)] text-[var(--text-tertiary)] hover:bg-[var(--danger-subtle)] hover:text-[var(--danger)]"
                      >
                        <Trash2 size={12} aria-hidden="true" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {detailId ? <HistoryDetail id={detailId} onClose={() => setDetailId(null)} /> : null}
    </div>
  );
}

/**
 * One history record in full, including the launch command.
 *
 * `historyApi.detail` returns the stored argv, which is the only way to answer
 * "what was actually run that day" — the list deliberately carries only a summary.
 */
function HistoryDetail({ id, onClose }: { id: number; onClose: () => void }) {
  const detail = useQuery({
    queryKey: ['history', 'detail', id],
    queryFn: () => historyApi.detail(id),
  });

  return (
    <div
      className="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center bg-[var(--overlay-scrim)]"
      role="dialog"
      aria-modal="true"
      aria-label={translate('history.detailTitle')}
      onKeyDown={(event) => {
        if (event.key === 'Escape') onClose();
      }}
    >
      <div className="flex max-h-[min(34rem,calc(100vh-4rem))] w-[min(46rem,calc(100vw-2rem))] flex-col rounded-[var(--radius-xl)] border border-[var(--border)] bg-[var(--surface)] shadow-[var(--shadow-pop)]">
        <header className="flex items-center gap-2 border-b border-[var(--border)] px-4 py-3">
          <h2 className="flex-1 text-[length:var(--text-md)] font-semibold">
            {translate('history.detailTitle')}
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label={translate('common.close')}>
            ×
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {detail.isPending ? (
            <LoadingState />
          ) : detail.data ? (
            <div className="flex flex-col gap-3">
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[length:var(--text-xs)]">
                <dt className="text-[var(--text-tertiary)]">{translate('history.time')}</dt>
                <dd className="tnum">{formatTime(detail.data.started_at)}</dd>
                <dt className="text-[var(--text-tertiary)]">{translate('history.duration')}</dt>
                <dd className="tnum">{formatDuration(detail.data.duration_seconds)}</dd>
                <dt className="text-[var(--text-tertiary)]">{translate('history.status')}</dt>
                <dd>{translate(`badge.${detail.data.status}`)}</dd>
                <dt className="text-[var(--text-tertiary)]">{translate('history.modelKey')}</dt>
                <dd className="break-anywhere font-mono">{detail.data.model_path ?? '—'}</dd>
              </dl>
              {detail.data.command_text ? (
                <div>
                  <p className="mb-1 text-[length('var(--text-xs)')] text-[var(--text-tertiary)]">
                    {translate('history.launchCommand')}
                  </p>
                  <pre className="overflow-x-auto rounded-[var(--radius-sm)] bg-[var(--code-bg)] p-2 font-mono text-[length:var(--text-2xs)] text-[var(--code-gutter)]">
                    {detail.data.command_text}
                  </pre>
                </div>
              ) : null}
            </div>
          ) : (
            <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {translate('history.detailFailed')}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

export default ObservabilityPage;
