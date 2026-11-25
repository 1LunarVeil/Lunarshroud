import { HardDrive, Cpu, MemoryStick } from 'lucide-react';
import { useInstanceStore, useSystemOverview, selectRunningCount } from '@/lib/stores/instances';
import { translate } from '@/lib/i18n';
import { formatBytes } from '@/lib/utils';
import { cn } from '@/lib/utils';

/**
 * Status bar.
 *
 * Persistent, low-key facts: CPU, memory, GPU, backend link, running count.
 * It sits at the window edge rather than in the sidebar so collapsing the sidebar
 * does not hide the machine's state.
 *
 * Numeric values use tabular figures so they do not jitter while updating.
 * When GPU data is unavailable the chip says "no GPU" rather than showing a zero,
 * which would read as "a GPU that is idle".
 */
export function StatusBar() {
  const overview = useSystemOverview();
  const socketStatus = useInstanceStore((state) => state.socketStatus);
  const runningCount = useInstanceStore(selectRunningCount);

  const cpu = overview.data?.cpu_percent;
  // The route sends GB (`memory_used_gb` / `memory_total_gb`). Reading
  // `memory_used_mb` off it made this chip render "—" on every machine, with no
  // error anywhere: `apiGet<T>` casts, so a wrong field name is invisible.
  const memoryUsedGb = overview.data?.memory_used_gb;
  const memoryTotalGb = overview.data?.memory_total_gb;
  const gpu = overview.data?.gpus?.[0];

  return (
    <footer
      aria-label={translate('app.statusLabel')}
      className="flex h-[var(--statusbar-h)] shrink-0 items-center gap-3 border-t border-[var(--border)] bg-[var(--panel)] px-3 text-[length:var(--text-2xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]"
    >
      <span className="flex items-center gap-1.5 tnum">
        <Cpu size={12} aria-hidden="true" />
        <span>{cpu === undefined ? '—' : `${Math.round(cpu)}%`}</span>
      </span>

      <span className="flex items-center gap-1.5 tnum" data-numeric>
        <MemoryStick size={12} aria-hidden="true" />
        <span>
          {memoryUsedGb === undefined || memoryTotalGb === undefined
            ? '—'
            : `${formatBytes(memoryUsedGb * 1024 * 1024 * 1024)} / ${formatBytes(memoryTotalGb * 1024 * 1024 * 1024)}`}
        </span>
      </span>

      {gpu ? (
        <span className="flex items-center gap-1.5 tnum" data-numeric data-testid="status-gpu" title={gpu.name}>
          <HardDrive size={12} aria-hidden="true" />
          <span>
            {formatBytes(gpu.memory_used_mb * 1024 * 1024)} / {formatBytes(gpu.memory_total_mb * 1024 * 1024)}
          </span>
        </span>
      ) : (
        <span
          className="flex items-center gap-1.5"
          data-testid="status-gpu"
          data-empty="true"
          title={translate('app.noGpuHint')}
        >
          <HardDrive size={12} aria-hidden="true" />
          <span>{translate('app.noGpu')}</span>
        </span>
      )}

      <span className="flex-1" />

      <span className="tnum">
        {translate('app.instancesRunning', { count: runningCount })}
      </span>

      <span
        className="flex items-center gap-1.5"
        title={translate(`app.socket.${socketStatus}`)}
      >
        <span
          aria-hidden="true"
          className={cn(
            'size-1.5 rounded-full',
            socketStatus === 'connected'
              ? 'bg-[var(--success)]'
              : socketStatus === 'reconnecting'
                ? 'bg-[var(--warning)]'
                : 'bg-[var(--danger)]',
          )}
        />
        <span>{translate(`app.socket.${socketStatus}`)}</span>
      </span>
    </footer>
  );
}
