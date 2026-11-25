import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Copy, Download, RefreshCw, Trash2 } from 'lucide-react';
import { apiServerApi, systemApi } from '@/lib/api';
import type { ApiServerLogEntry, ApiServerStatus } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Badge, EmptyState, LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { Button } from '@/components/ui/Button';
import { Field, Input, Switch } from '@/components/ui/Input';

/**
 * Local API server: the OpenAI-compatible gateway.
 *
 * Two planes, deliberately separate (see `backend/api/gateway.py`): the page talks to
 * the **management** API on the launcher port with the session token; external clients
 * talk to the **data** plane on its own port with a Bearer key. This page never proxies
 * `/v1/*` — doing so would put the session token in a path external tools can reach.
 *
 * The request log is an in-memory ring buffer of the last 200 calls. It is deliberately
 * volatile: persisting every request would grow a file that gets backed up, and the
 * question it answers ("what was that 4xx just now?") is always immediate.
 */
export function ServingPage() {
  const client = useQueryClient();
  const [draftKey, setDraftKey] = useState('');

  const status = useQuery({
    queryKey: ['api-server', 'status'],
    queryFn: apiServerApi.status,
    refetchInterval: 2000,
  });
  const logs = useQuery({
    queryKey: ['api-server', 'logs'],
    queryFn: () => apiServerApi.logs(100),
    refetchInterval: 2000,
  });
  const recipes = useQuery({ queryKey: ['api-server', 'recipes'], queryFn: apiServerApi.recipes, staleTime: 60_000 });
  const config = useQuery({ queryKey: ['system', 'config'], queryFn: systemApi.getConfig });

  const data: ApiServerStatus | undefined = status.data;

  const restart = useMutation({
    mutationFn: apiServerApi.restart,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['api-server'] });
      void client.invalidateQueries({ queryKey: ['system', 'config'] });
    },
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const saveConfig = useMutation({
    mutationFn: (patch: Record<string, unknown>) => systemApi.updateConfig(patch),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['system', 'config'] });
      void client.invalidateQueries({ queryKey: ['api-server'] });
      toast.success(translate('apiServer.saved'));
    },
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const clearLogs = useMutation({
    mutationFn: apiServerApi.clearLogs,
    onSuccess: () => void client.invalidateQueries({ queryKey: ['api-server', 'logs'] }),
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.success(translate('apiServer.copied'));
    } catch {
      toast.error(translate('app.copyFail'));
    }
  };

  /** Export the log as JSONL: one JSON object per line is what `jq` and log tools read. */
  const exportLogs = () => {
    const entries: ApiServerLogEntry[] = logs.data?.entries ?? [];
    if (!entries.length) return;
    const body = entries.map((entry) => JSON.stringify(entry)).join('\n');
    const blob = new Blob([body], { type: 'application/x-ndjson' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'lunarshroud-api-requests.jsonl';
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  const toggle = async (enabled: boolean) => {
    saveConfig.mutate({ api_server_enabled: enabled });
    // Applying the flag takes effect on the next start; restart now so the toggle means
    // what it says instead of "takes effect after a relaunch".
    setTimeout(() => restart.mutate(), 400);
  };

  if (status.isPending || config.isPending || !config.data) return <LoadingState />;

  // `config.data` is the fully typed `AppConfig`; no index signature by design
  // (a stray field would let a typo typecheck — see `types.ts`). This page only
  // reads a handful of keys, so it projects them rather than widening the shape.
  const current = config.data;
  const running = data?.running ?? false;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mx-auto w-full max-w-[64rem] px-6 py-5">
        <SectionHeader
          title={translate('apiServer.title')}
          description={translate('apiServer.subtitle')}
          action={
            <div className="flex items-center gap-2">
              <Badge tone={running ? 'success' : data?.error ? 'danger' : 'neutral'} dot>
                {running
                  ? translate('apiServer.stateRunning')
                  : data?.error
                    ? translate('apiServer.stateFailed')
                    : translate('apiServer.stateStopped')}
              </Badge>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void status.refetch()}
                loading={status.isFetching}
                aria-label={translate('common.refresh')}
                title={translate('common.refresh')}
              >
                <RefreshCw size={13} aria-hidden="true" />
              </Button>
            </div>
          }
        />

        <div className="mt-4 rounded-[var(--radius-lg)] border border-[var(--border)] bg-[var(--surface)] p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-[length:var(--text-sm)] font-medium">{translate('apiServer.enable')}</p>
              <p className="mt-0.5 text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                {running && data?.base_url ? data.base_url : translate('apiServer.settingsHint')}
              </p>
            </div>
            <div className="flex items-center gap-2">
              {running && data?.base_url ? (
                <Button variant="secondary" size="sm" onClick={() => void copy(data.base_url)}>
                  <Copy size={13} aria-hidden="true" />
                  {translate('apiServer.copyUrl')}
                </Button>
              ) : null}
              <Switch
                checked={Boolean(current.api_server_enabled)}
                onCheckedChange={(next) => void toggle(next)}
                label={translate('apiServer.enable')}
                id="api-enabled"
              />
            </div>
          </div>

          {data?.error ? (
            <p className="mt-2 break-anywhere rounded-[var(--radius-sm)] bg-[var(--danger-subtle)] px-2 py-1 text-[length:var(--text-xs)] text-[var(--danger)]">
              {data.error}
            </p>
          ) : null}

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <Field label={translate('apiServer.port')} hint={translate('apiServer.portHelp')} htmlFor="api-port">
              <Input
                id="api-port"
                type="number"
                defaultValue={Number(current.api_server_port ?? 1234)}
                onBlur={(event) => {
                  const next = Number(event.target.value);
                  if (Number.isFinite(next) && next !== Number(current.api_server_port)) {
                    saveConfig.mutate({ api_server_port: next });
                  }
                }}
              />
            </Field>
            <Field label={translate('apiServer.key')} hint={translate('apiServer.keyKeepHint')} htmlFor="api-key">
              <Input
                id="api-key"
                type="password"
                autoComplete="new-password"
                value={draftKey}
                onChange={(event) => setDraftKey(event.target.value)}
                placeholder={data?.key_configured ? translate('apiServer.keySet') : translate('apiServer.keyUnset')}
                trailing={
                  draftKey ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => {
                        saveConfig.mutate({ api_server_key: draftKey });
                        setDraftKey('');
                      }}
                    >
                      {translate('apiServer.save')}
                    </Button>
                  ) : undefined
                }
              />
            </Field>
            <Field
              label={translate('apiServer.cors')}
              hint={translate('apiServer.corsHelp')}
              htmlFor="api-cors"
              className="md:col-span-2"
            >
              <Input
                id="api-cors"
                defaultValue={String(current.api_server_cors_origins ?? '')}
                placeholder="http://127.0.0.1:3000"
                className="font-mono"
                onBlur={(event) => {
                  if (event.target.value !== String(current.api_server_cors_origins ?? '')) {
                    saveConfig.mutate({ api_server_cors_origins: event.target.value });
                  }
                }}
              />
            </Field>
          </div>
        </div>

        <div className="mt-4 rounded-[var(--radius-lg)] border border-[var(--border)] bg-[var(--surface)] p-4">
          <p className="mb-2 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
            {translate('apiServer.loadedModels')}
          </p>
          {data?.models?.length ? (
            <ul className="flex flex-col gap-1.5">
              {data.models.map((model) => (
                <li key={model.id} className="flex items-center justify-between gap-3 text-[length:var(--text-xs)]">
                  <span className="truncate font-mono" title={model.id}>
                    {model.id}
                  </span>
                  <span className="shrink-0 text-[var(--text-tertiary)]">:{model.port}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {translate('apiServer.noModelsHint')}
            </p>
          )}
        </div>

        {recipes.data?.recipes?.length ? (
          <div className="mt-4 rounded-[var(--radius-lg)] border border-[var(--border)] bg-[var(--surface)] p-4">
            <p className="mb-2 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
              {translate('apiServer.recipes')}
            </p>
            <p className="mb-2 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
              {translate('apiServer.recipesHint')}
            </p>
            <div className="flex flex-col gap-2">
              {recipes.data.recipes.map((recipe) => (
                <div key={recipe.label} className="rounded-[var(--radius-sm)] bg-[var(--code-bg)] p-2">
                  <div className="mb-1 flex items-center justify-between">
                    <span className="font-mono text-[length:var(--text-2xs)] text-[var(--code-gutter)]">
                      {recipe.label}
                    </span>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => void copy(recipe.code)}
                      aria-label={translate('apiServer.copy')}
                      title={translate('apiServer.copy')}
                    >
                      <Copy size={12} aria-hidden="true" />
                    </Button>
                  </div>
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-anywhere font-mono text-[length:var(--text-2xs)] text-[var(--code-fg)]">
                    {recipe.code}
                  </pre>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        <div className="mt-4 rounded-[var(--radius-lg)] border border-[var(--border)] bg-[var(--surface)] p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
                {translate('apiServer.requestLog')}
              </p>
              <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                {translate('apiServer.requestLogHint')}
              </p>
            </div>
            <div className="flex gap-1">
              <Button size="sm" variant="secondary" onClick={exportLogs} disabled={!logs.data?.entries?.length}>
                <Download size={12} aria-hidden="true" />
                {translate('apiServer.exportLog')}
              </Button>
              <Button size="sm" variant="dangerGhost" onClick={() => clearLogs.mutate()} loading={clearLogs.isPending}>
                <Trash2 size={12} aria-hidden="true" />
                {translate('apiServer.clearLog')}
              </Button>
            </div>
          </div>

          {logs.data?.entries?.length ? (
            <table className="mt-2 w-full text-[length:var(--text-2xs)]">
              <thead>
                <tr className="border-b border-[var(--border)] text-[var(--text-tertiary)]">
                  <th className="py-1 text-start font-medium">{translate('apiServer.colTime')}</th>
                  <th className="py-1 text-start font-medium">{translate('apiServer.colMethod')}</th>
                  <th className="py-1 text-start font-medium">{translate('apiServer.colPath')}</th>
                  <th className="py-1 text-end font-medium">{translate('apiServer.colStatus')}</th>
                  <th className="py-1 text-end font-medium">{translate('apiServer.colMs')}</th>
                </tr>
              </thead>
              <tbody>
                {logs.data.entries.slice(0, 60).map((entry, index) => (
                  <tr key={`${entry.at}-${index}`} className="border-b border-[var(--border-faint,var(--border))]">
                    <td className="py-0.5 font-mono">{entry.at}</td>
                    <td className="py-0.5">{entry.method}</td>
                    <td className="max-w-[16rem] truncate py-0.5 font-mono" title={entry.path}>
                      {entry.path}
                    </td>
                    <td
                      className={cn(
                        'py-0.5 text-end tnum',
                        entry.status >= 500
                          ? 'text-[var(--danger)]'
                          : entry.status >= 400
                            ? 'text-[var(--warning)]'
                            : 'text-[var(--success)]',
                      )}
                    >
                      {entry.status}
                    </td>
                    <td className="py-0.5 text-end tnum">{entry.ms}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState compact title={translate('apiServer.noLogs')} description={translate('apiServer.noLogsHint')} />
          )}
        </div>
      </div>
    </div>
  );
}

export default ServingPage;
