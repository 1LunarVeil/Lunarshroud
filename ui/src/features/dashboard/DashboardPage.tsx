import { useCallback, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import {
  Activity,
  CircleStop,
  FolderOpen,
  Loader2,
  Play,
  RotateCw,
  ScrollText,
  Trash2,
  TriangleAlert,
} from 'lucide-react';
import { useUiStore } from '@/lib/stores/ui';
import { useInstanceStore } from '@/lib/stores/instances';
import { instancesApi, modelsApi, runtimesApi, systemApi } from '@/lib/api';
import type { Instance, RuntimeProfile } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn, formatBytes, formatDuration, basename } from '@/lib/utils';
import { Badge, EmptyState, SectionHeader } from '@/components/ui/Feedback';
import { Button } from '@/components/ui/Button';
import { Dialog, DialogContent } from '@/components/ui/Dialog';
import { Field, Input } from '@/components/ui/Input';

/**
 * Dashboard: what is running, and the actions that change that.
 *
 * The first-run state gets **one** path (choose a model → configure → load). A user who
 * has never loaded a model sees six equally-weighted buttons in 1.0, which is a menu of
 * unknowns rather than a next step.
 *
 * Instance rows read their live state from the WebSocket-fed store, so a start or a crash
 * is reflected without polling. Actions are optimistic only where the server confirms
 * quickly (stop/remove); starting a model takes seconds, so it reports progress instead
 * of pretending.
 */
export function DashboardPage() {
  const instances = useInstanceStore((state) => state.instances);
  const navigate = useUiStore((state) => state.navigate);
  const client = useQueryClient();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [launcherOpen, setLauncherOpen] = useState(false);

  const runtimes = useQuery({ queryKey: ['runtimes'], queryFn: runtimesApi.list, staleTime: 30_000 });
  const config = useQuery({ queryKey: ['system', 'config'], queryFn: systemApi.getConfig });

  const running = instances.filter((i) => i.status === 'running' || i.status === 'starting');
  const stopped = instances.filter((i) => i.status !== 'running' && i.status !== 'starting');

  const act = useCallback(
    async (instance: Instance, action: 'stop' | 'restart' | 'remove') => {
      setBusyId(instance.id);
      try {
        if (action === 'stop') await instancesApi.stop(instance.id, false);
        if (action === 'restart') await instancesApi.restart(instance.id);
        if (action === 'remove') await instancesApi.remove(instance.id);
        await client.invalidateQueries({ queryKey: ['runtimes'] });
      } catch (error) {
        toast.error(localizeBackendError(error instanceof Error ? error.message : error));
      } finally {
        setBusyId(null);
      }
    },
    [client],
  );

  const configuredServer = Boolean(String(config.data?.llama_server_path ?? '').trim());
  const hasModels = Boolean(String(config.data?.default_model_dir ?? '').trim());

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mx-auto w-full max-w-[72rem] px-6 py-5">
        <SectionHeader
          title={translate('nav.dashboard')}
          description={translate('dash.subtitle')}
          action={
            <div className="flex gap-2">
              <Button variant="primary" onClick={() => setLauncherOpen(true)}>
                <Play size={14} aria-hidden="true" />
                {translate('dash.launch')}
              </Button>
            </div>
          }
        />

        {!configuredServer ? (
          <FirstRunCard
            onConfigure={() => navigate('settings')}
            onPickModel={() => navigate('models')}
            onLaunch={() => setLauncherOpen(true)}
            hasServer={configuredServer}
            hasModels={hasModels}
          />
        ) : null}

        {runtimes.data?.runtimes?.length === 0 && configuredServer ? (
          <div className="mt-4 rounded-[var(--radius-lg)] border border-[var(--warning)]/40 bg-[var(--warning-subtle)] p-3">
            <p className="flex items-center gap-2 text-[length:var(--text-sm)] text-[var(--warning)]">
              <TriangleAlert size={15} aria-hidden="true" />
              {translate('dash.emptyNoServer')}
            </p>
            <Button variant="secondary" className="mt-2" onClick={() => navigate('settings')}>
              {translate('dash.stateServer')}
            </Button>
          </div>
        ) : null}

        {running.length > 0 ? (
          <section className="mt-6">
            <h2 className="mb-2 text-[length:var(--text-xs)] font-medium text-[var(--text-tertiary)]">
              {translate('dash.stateTitle')}
            </h2>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {running.map((instance) => (
                <InstanceCard
                  key={instance.id}
                  instance={instance}
                  busy={busyId === instance.id}
                  onStop={() => void act(instance, 'stop')}
                  onRestart={() => void act(instance, 'restart')}
                  onChat={() => navigate('chat')}
                  onLogs={() => navigate('observability')}
                />
              ))}
            </div>
          </section>
        ) : null}

        {stopped.length > 0 ? (
          <section className="mt-6">
            <h2 className="mb-2 text-[length:var(--text-xs)] font-medium text-[var(--text-tertiary)]">
              {translate('dash.title')}
            </h2>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {stopped.map((instance) => (
                <InstanceCard
                  key={instance.id}
                  instance={instance}
                  busy={busyId === instance.id}
                  onRestart={() => void act(instance, 'restart')}
                  onRemove={() => void act(instance, 'remove')}
                  onLogs={() => navigate('observability')}
                />
              ))}
            </div>
          </section>
        ) : null}

        {instances.length === 0 && configuredServer ? (
          <div className="mt-8">
            <EmptyState
              icon={<Activity size={20} />}
              title={translate('dash.empty')}
              description={translate('dash.startHelp')}
              action={
                <Button variant="primary" onClick={() => setLauncherOpen(true)}>
                  {translate('dash.launch')}
                </Button>
              }
            />
          </div>
        ) : null}
      </div>

      <LaunchDialog open={launcherOpen} onClose={() => setLauncherOpen(false)} />
    </div>
  );
}

function FirstRunCard({
  onConfigure,
  onPickModel,
  onLaunch,
  hasServer,
  hasModels,
}: {
  onConfigure: () => void;
  onPickModel: () => void;
  onLaunch: () => void;
  hasServer: boolean;
  hasModels: boolean;
}) {
  return (
    <div className="mt-5 rounded-[var(--radius-xl)] border border-[var(--border)] bg-[var(--surface)] p-5">
      <h2 className="text-[length:var(--text-lg)] font-semibold tracking-[var(--tracking-tight)]">
        {translate('dash.startTitle')}
      </h2>
      <p className="mt-1 max-w-[60ch] text-[length:var(--text-sm)] leading-[var(--leading-relaxed)] text-[var(--text-secondary)]">
        {translate('dash.startHelp')}
      </p>
      <ol className="mt-4 flex flex-col gap-4">
        <Step
          index={1}
          title={translate('settings.serverPath')}
          state={hasServer ? translate('dash.stateReady') : translate('dash.stateMissing')}
          done={hasServer}
        >
          <Button variant="primary" size="sm" onClick={onConfigure}>
            {translate('help.openSettings')}
          </Button>
        </Step>
        <Step
          index={2}
          title={translate('settings.modelDir')}
          state={hasModels ? translate('dash.stateReady') : translate('dash.stateMissing')}
          done={hasModels}
        >
          <Button variant="secondary" size="sm" onClick={onPickModel}>
            <FolderOpen size={13} aria-hidden="true" />
            {translate('models.useInEditor')}
          </Button>
        </Step>
        <Step
          index={3}
          title={translate('dash.launchModel')}
          state={translate('dash.startPick')}
          done={false}
        >
          <Button variant="secondary" size="sm" onClick={onLaunch}>
            {translate('dash.launch')}
          </Button>
        </Step>
      </ol>
    </div>
  );
}

function Step({
  index,
  title,
  state,
  done,
  children,
}: {
  index: number;
  title: string;
  /** Human-readable status ("Configured" / "Not configured" / "Launch"). */
  state: string;
  done: boolean;
  children?: React.ReactNode;
}) {
  return (
    <li className="flex items-start gap-3">
      <span
        className={cn(
          'grid size-6 shrink-0 place-items-center rounded-full text-[length:var(--text-xs)] font-medium tnum',
          done
            ? 'bg-[var(--success-subtle)] text-[var(--success)]'
            : 'bg-[var(--surface-sunken)] text-[var(--text-tertiary)]',
        )}
      >
        {index}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[length:var(--text-sm)] font-medium text-[var(--text)]">{title}</p>
        <p
          className={cn(
            'mt-0.5 text-[length:var(--text-2xs)]',
            done ? 'text-[var(--success)]' : 'text-[var(--text-tertiary)]',
          )}
        >
          {state}
        </p>
        {children ? <div className="mt-2">{children}</div> : null}
      </div>
    </li>
  );
}

const STATUS_TONE: Record<string, 'success' | 'warning' | 'danger' | 'neutral' | 'info'> = {
  running: 'success',
  starting: 'info',
  stopping: 'warning',
  stopped: 'neutral',
  idle: 'neutral',
  failed: 'danger',
};

function InstanceCard({
  instance,
  busy,
  onStop,
  onRestart,
  onRemove,
  onChat,
  onLogs,
}: {
  instance: Instance;
  busy: boolean;
  onStop?: () => void;
  onRestart?: () => void;
  onRemove?: () => void;
  onChat?: () => void;
  onLogs?: () => void;
}) {
  const live = instance.status === 'running' || instance.status === 'starting';
  return (
    <article
      data-testid={`instance-${instance.id}`}
      className="flex flex-col rounded-[var(--radius-lg)] border border-[var(--border)] bg-[var(--surface)] p-3"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-[length:var(--text-sm)] font-medium" title={instance.name}>
            {instance.name}
          </p>
          <p className="mt-0.5 truncate font-mono text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
            {basename(instance.model_path)}
          </p>
        </div>
        <Badge tone={STATUS_TONE[instance.status] ?? 'neutral'} dot>
          {translate(`badge.${instance.status}`)}
        </Badge>
      </div>

      <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[length:var(--text-2xs)] tnum">
        <div className="flex justify-between gap-2">
          <dt className="text-[var(--text-tertiary)]">{translate('dash.port')}</dt>
          <dd className="text-[var(--text)]">{instance.port ?? '—'}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-[var(--text-tertiary)]">{translate('dash.launchPortHelp')}</dt>
          <dd className="text-[var(--text)]">
            {instance.context_size ? instance.context_size.toLocaleString() : '—'}
          </dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-[var(--text-tertiary)]">{translate('dash.duration')}</dt>
          <dd className="text-[var(--text)]">
            {/* A run that never became ready has no meaningful uptime; showing "0s"
                would look like a hang rather than a failure. */}
            {instance.ever_ready ? formatDuration(instance.duration_seconds) : '—'}
          </dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-[var(--text-tertiary)]">{translate('dash.runtime')}</dt>
          <dd className="truncate text-[var(--text)]" title={instance.runtime_label ?? ''}>
            {instance.runtime_label ?? '—'}
          </dd>
        </div>
      </dl>

      {instance.warnings.length ? (
        <ul className="mt-2 flex flex-col gap-1">
          {instance.warnings.slice(0, 2).map((warning) => (
            <li
              key={warning}
              className="rounded-[var(--radius-sm)] bg-[var(--warning-subtle)] px-2 py-1 text-[length:var(--text-2xs)] text-[var(--warning)]"
            >
              {warning}
            </li>
          ))}
        </ul>
      ) : null}

      {instance.error_message ? (
        <p className="mt-2 break-anywhere rounded-[var(--radius-sm)] bg-[var(--danger-subtle)] px-2 py-1 text-[length:var(--text-2xs)] text-[var(--danger)]">
          {instance.error_message}
        </p>
      ) : null}

      <div className="mt-2.5 flex flex-wrap items-center gap-1">
        {live ? (
          <>
            {instance.serves_chat && onChat ? (
              <Button size="sm" variant="secondary" onClick={onChat}>
                {translate('dash.chat')}
              </Button>
            ) : null}
            {onLogs ? (
              <Button size="sm" variant="ghost" onClick={onLogs} title={translate('dash.logs')}>
                <ScrollText size={13} aria-hidden="true" />
              </Button>
            ) : null}
            {onRestart ? (
              <Button size="sm" variant="ghost" onClick={onRestart} disabled={busy} title={translate('dash.restart')}>
                <RotateCw size={13} aria-hidden="true" />
              </Button>
            ) : null}
            {onStop ? (
              <Button size="sm" variant="dangerGhost" onClick={onStop} disabled={busy} title={translate('dash.stop')}>
                {busy ? (
                  <Loader2 size={13} className="animate-spin" aria-hidden="true" />
                ) : (
                  <CircleStop size={13} aria-hidden="true" />
                )}
                {translate('dash.stop')}
              </Button>
            ) : null}
          </>
        ) : (
          <>
            {onRestart ? (
              <Button size="sm" variant="secondary" onClick={onRestart} disabled={busy}>
                <RotateCw size={13} aria-hidden="true" />
                {translate('dash.restart')}
              </Button>
            ) : null}
            {onLogs ? (
              <Button size="sm" variant="ghost" onClick={onLogs} title={translate('dash.logs')}>
                <ScrollText size={13} aria-hidden="true" />
              </Button>
            ) : null}
            {onRemove ? (
              <Button size="sm" variant="dangerGhost" onClick={onRemove} disabled={busy} title={translate('dash.remove')}>
                <Trash2 size={13} aria-hidden="true" />
              </Button>
            ) : null}
          </>
        )}
      </div>
    </article>
  );
}

function LaunchDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const client = useQueryClient();
  const [runtimeId, setRuntimeId] = useState('');
  const [modelPath, setModelPath] = useState('');
  const [name, setName] = useState('');
  const [ctxSize, setCtxSize] = useState('');
  const [gpuLayers, setGpuLayers] = useState('');
  const [port, setPort] = useState('');
  const [starting, setStarting] = useState(false);
  const [diagnosis, setDiagnosis] = useState<Array<{ severity: string; message: string; hint?: string }>>([]);

  const runtimes = useQuery({
    queryKey: ['runtimes'],
    queryFn: runtimesApi.list,
    enabled: open,
    staleTime: 30_000,
  });
  const config = useQuery({ queryKey: ['system', 'config'], queryFn: systemApi.getConfig, enabled: open });
  const models = useQuery({
    queryKey: ['models', 'scan', config.data?.default_model_dir],
    queryFn: () => modelsApi.scan(String(config.data?.default_model_dir ?? '')),
    enabled: open && Boolean(config.data?.default_model_dir),
    staleTime: 60_000,
  });

  const profiles: RuntimeProfile[] = runtimes.data?.runtimes ?? [];
  const effectiveRuntime = runtimeId || profiles[0]?.id || '';

  const launch = async () => {
    if (!modelPath.trim()) {
      toast.error(translate('dash.launchModel'));
      return;
    }
    setStarting(true);
    setDiagnosis([]);
    try {
      const params: Record<string, unknown> = { model: modelPath.trim() };
      if (ctxSize.trim()) params.ctx_size = Number(ctxSize);
      if (gpuLayers.trim()) params.n_gpu_layers = Number(gpuLayers);
      if (port.trim()) params.port = Number(port);
      else params.port = 0;

      // Diagnose first: a preflight is far cheaper than a failed load, and its output
      // is the same text the instance would report on failure.
      try {
        const preflight = await instancesApi.diagnose({
          params,
          runtime_id: effectiveRuntime || undefined,
        });
        const blocking = preflight.blocking ?? [];
        if (blocking.length) {
          setDiagnosis(blocking.map((entry) => ({ severity: 'error', message: entry.message, hint: entry.hint })));
          return;
        }
        setDiagnosis((preflight.warnings ?? []).map((entry) => ({ severity: 'warning', message: entry.message, hint: entry.hint })));
      } catch {
        // A failed preflight must not block a launch the user asked for; the instance
        // start path re-validates and reports its own error.
      }

      await instancesApi.create({
        params,
        runtime_id: effectiveRuntime || undefined,
        name: name.trim() || undefined,
      });
      await client.invalidateQueries({ queryKey: ['runtimes'] });
      toast.success(translate('dash.launched'));
      onClose();
      setModelPath('');
      setName('');
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    } finally {
      setStarting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      {open ? (
        <DialogContent title={translate('dash.launch')} size="md">
          <div className="flex flex-col gap-4 p-5">
            <Field label={translate('runtimes.title')} hint={translate('dash.launchRuntimeHelp')}>
              <select
                value={effectiveRuntime}
                onChange={(event) => setRuntimeId(event.target.value)}
                className="h-[var(--control-h)] w-full rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-sm)] outline-none focus:ring-2 focus:ring-[var(--focus-ring)]"
              >
                {profiles.length === 0 ? (
                  <option value="">{translate('dash.emptyNoServer')}</option>
                ) : (
                  profiles.map((profile) => (
                    <option key={profile.id} value={profile.id}>
                      {profile.display_name}
                    </option>
                  ))
                )}
              </select>
            </Field>

            <Field label={translate('dash.model')} hint={translate('dash.launchModel')} htmlFor="launch-model">
              <Input
                id="launch-model"
                value={modelPath}
                onChange={(event) => setModelPath(event.target.value)}
                placeholder={translate('dash.launchModel')}
                className="font-mono"
                list="launch-models"
              />
              <datalist id="launch-models">
                {(models.data?.models ?? []).slice(0, 200).map((model) => (
                  <option key={model.path} value={model.path}>
                    {model.filename} · {formatBytes(model.size_bytes)}
                  </option>
                ))}
              </datalist>
            </Field>

            <div className="grid grid-cols-3 gap-3">
              <Field label={translate('dash.launchPortHelp')} htmlFor="launch-ctx">
                <Input
                  id="launch-ctx"
                  type="number"
                  value={ctxSize}
                  onChange={(event) => setCtxSize(event.target.value)}
                  placeholder="4096"
                />
              </Field>
              <Field label={translate('dash.launchRuntime')} htmlFor="launch-gpu">
                <Input
                  id="launch-gpu"
                  type="number"
                  value={gpuLayers}
                  onChange={(event) => setGpuLayers(event.target.value)}
                  placeholder="99"
                />
              </Field>
              <Field label={translate('dash.port')} htmlFor="launch-port">
                <Input
                  id="launch-port"
                  type="number"
                  value={port}
                  onChange={(event) => setPort(event.target.value)}
                  placeholder={translate('dash.launchPortPh')}
                />
              </Field>
            </div>

            <Field label={translate('dash.launchName')} htmlFor="launch-name">
              <Input
                id="launch-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder={translate('dash.namePh')}
              />
            </Field>

            {diagnosis.length ? (
              <ul className="flex flex-col gap-1.5">
                {diagnosis.map((entry) => (
                  <li
                    key={entry.message}
                    className={cn(
                      'rounded-[var(--radius-sm)] px-2 py-1.5 text-[length:var(--text-2xs)]',
                      entry.severity === 'error'
                        ? 'bg-[var(--danger-subtle)] text-[var(--danger)]'
                        : 'bg-[var(--warning-subtle)] text-[var(--warning)]',
                    )}
                  >
                    {entry.message}
                    {entry.hint ? (
                      <span className="mt-0.5 block opacity-80">{entry.hint}</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}

            <div className="flex justify-end gap-2 border-t border-[var(--border)] pt-3">
              <Button variant="secondary" onClick={onClose}>
                {translate('common.cancel')}
              </Button>
              <Button variant="primary" onClick={() => void launch()} loading={starting}>
                {translate('dash.launch')}
              </Button>
            </div>
          </div>
        </DialogContent>
      ) : null}
    </Dialog>
  );
}

export default DashboardPage;
