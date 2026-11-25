import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { usageApi } from '@/lib/api';
import type { UsageSummary } from '@/lib/api/types';
import { translate } from '@/lib/i18n';
import { formatBytes } from '@/lib/utils';
import { LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { cn } from '@/lib/utils';

/**
 * Usage statistics.
 *
 * The numbers come from the token counts each assistant message already stores,
 * so they include every conversation this install has had — and **only** those.
 * The page says so: presenting local chat traffic as "everything this machine
 * generated" would be wrong the moment a user points a script at the local API
 * gateway.
 *
 * The chart is CSS bars, not a charting library: 365 flex children cost nothing,
 * and a chart library would add hundreds of kilobytes to a bundle that is
 * embedded in the desktop binary.
 */
const RANGES = [30, 90, 365] as const;

export function UsageTab() {
  const summary = useQuery({ queryKey: ['usage', 'summary'], queryFn: () => usageApi.summary(30) });
  // Ranges are separate queries so switching back to a range is instant.
  return (
    <div className="flex flex-col gap-5">
      <SectionHeader
        title={translate('settings.catUsage')}
        description={translate('settings.usageSubtitle')}
      />
      <UsageBody data={summary.data} pending={summary.isPending} />
      <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
        {translate('settings.usageScope')}
      </p>
    </div>
  );
}

function UsageBody({ data, pending }: { data: UsageSummary | undefined; pending: boolean }) {
  const [range, setRange] = useState<number>(30);
  const query = useQuery({
    queryKey: ['usage', 'summary', range],
    queryFn: () => usageApi.summary(range),
  });
  /*
    Normalise once, here, rather than sprinkling `?? []` at every read.
    `apiGet<T>` is a cast, not a validator: a backend that predates `models` (a
    stale dev process, or a UI updated ahead of its backend) sends a document that
    does not match the type. Reading `view.models.length` then threw
    `Cannot read properties of undefined` and the app boundary replaced the whole
    window with the crash screen — a version skew turned into a white screen.
  */
  const raw = query.data ?? data;
  const view: UsageSummary | null = raw
    ? {
        ...raw,
        daily: raw.daily ?? [],
        models: raw.models ?? [],
        models_total: raw.models_total ?? raw.models?.length ?? 0,
      }
    : null;

  return (
    <>
      <div className="flex gap-1">
        {RANGES.map((days) => (
          <button
            key={days}
            type="button"
            onClick={() => setRange(days)}
            aria-pressed={range === days}
            className={cn(
              'min-h-[var(--control-h-sm)] rounded-[var(--radius-pill)] px-3 text-[length:var(--text-xs)]',
              range === days
                ? 'bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                : 'text-[var(--text-secondary)] hover:bg-[var(--hover)]',
            )}
          >
            {translate('settings.usageRange', { days })}
          </button>
        ))}
      </div>

      {pending && !view ? <LoadingState /> : null}
      {view ? (
        <>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label={translate('settings.usageRequests')} value={String(view.totals.requests)} />
            <Stat label={translate('settings.usageTotalTokens')} value={formatTokens(view.totals.total_tokens)} />
            <Stat
              label={translate('settings.usageSpeed')}
              value={
                view.totals.avg_tokens_per_second === null
                  ? '—'
                  : `${view.totals.avg_tokens_per_second.toFixed(1)} tok/s`
              }
            />
            <Stat label={translate('settings.usageActiveDays')} value={String(view.active_days)} />
            <Stat label={translate('settings.usageStreak')} value={String(view.longest_streak)} />
            <Stat label={translate('settings.usagePrompt')} value={formatTokens(view.totals.prompt_tokens)} />
            <Stat label={translate('settings.usageCompletion')} value={formatTokens(view.totals.completion_tokens)} />
            <Stat
              label={translate('settings.usagePeak')}
              value={
                view.peak_day
                  ? `${view.peak_day.date} · ${formatTokens(view.peak_day.tokens)}`
                  : '—'
              }
            />
          </div>

          {view.top_model ? (
            <p className="text-[length:var(--text-xs)] text-[var(--text-secondary)]">
              {translate('settings.usageTopModel')}：{view.top_model.model}（
              {formatTokens(view.top_model.tokens)}）
            </p>
          ) : null}

          <DailyChart daily={view.daily} />

          {/*
            The full per-model table, not just the leader. "Which model costs me the
            most tokens" and "which model do I call most often" are different
            questions, and a single top-model line answers neither when the request
            counts differ. The cap is stated when it applies instead of the table
            silently ending.

            `?? []` is deliberate: a backend that predates this field (a stale dev
            process, or a user who updated the UI without the backend) must render an
            empty table, not crash the whole window. `apiGet<T>` does not validate, so
            assuming the field exists turns a version skew into a white screen.
          */}
          {(view.models ?? []).length ? (
            <div className="flex flex-col gap-1">
              <p className="text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
                {translate('settings.usageByModel')}
              </p>
              <table className="w-full border-collapse text-[length:var(--text-2xs)]">
                <thead>
                  <tr className="text-[var(--text-tertiary)]">
                    <th scope="col" className="py-1 pe-2 text-start font-medium">
                      {translate('settings.usageModel')}
                    </th>
                    <th scope="col" className="py-1 pe-2 text-end font-medium">
                      {translate('settings.usageRequests')}
                    </th>
                    <th scope="col" className="py-1 pe-2 text-end font-medium">
                      {translate('settings.usageTotalTokens')}
                    </th>
                    <th scope="col" className="py-1 text-end font-medium">
                      {translate('settings.usageSpeed')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {view.models.map((row) => (
                    <tr key={row.model} className="border-t border-[var(--border)]">
                      <td className="max-w-[16rem] truncate py-1 pe-2" title={row.model}>
                        {row.model}
                      </td>
                      <td className="py-1 pe-2 text-end tnum">{row.requests}</td>
                      <td className="py-1 pe-2 text-end tnum">
                        {formatTokens(row.total_tokens)}
                      </td>
                      <td className="py-1 text-end tnum text-[var(--text-tertiary)]">
                        {row.avg_tokens_per_second === null
                          ? '—'
                          : `${row.avg_tokens_per_second.toFixed(1)} tok/s`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {view.models_total > view.models.length ? (
                <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                  {translate('settings.usageModelsMore', {
                    shown: view.models.length,
                    total: view.models_total,
                  })}
                </p>
              ) : null}
            </div>
          ) : null}

          {!view.has_data ? (
            <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {translate('settings.usageEmpty')}
            </p>
          ) : null}
        </>
      ) : null}
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2">
      <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">{label}</p>
      <p className="mt-0.5 truncate text-[length:var(--text-sm)] font-medium tnum">{value}</p>
    </div>
  );
}

function formatTokens(value: number): string {
  if (!Number.isFinite(value)) return '0';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(Math.round(value));
}

function DailyChart({ daily }: { daily: Array<{ date: string; tokens: number; requests: number }> }) {
  const peak = daily.reduce((max, day) => Math.max(max, day.tokens), 0) || 1;
  return (
    <div
      className="flex h-24 items-end gap-[1px] rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] p-2"
      role="img"
      aria-label={translate('settings.usageChartLabel')}
    >
      {daily.map((day) => (
        <div
          key={day.date}
          title={`${day.date} · ${formatTokens(day.tokens)} · ${day.requests}`}
          className="flex-1 rounded-t-[2px] bg-[var(--accent)]"
          style={{
            height: `${Math.max(day.tokens > 0 ? 4 : 1, (day.tokens / peak) * 100)}%`,
            opacity: day.tokens > 0 ? 1 : 0.15,
          }}
        />
      ))}
    </div>
  );
}

export { formatBytes };
