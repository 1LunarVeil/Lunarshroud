import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Cpu, HardDrive, MemoryStick, Pencil, RefreshCw, Plus, Trash2, Zap } from 'lucide-react';
import { enginesApi, runtimesApi, systemApi } from '@/lib/api';
import { pickPath, openPath } from '@/lib/api/client';
import type { EngineRecord, StorageReport } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn, formatBytes } from '@/lib/utils';
import { Badge, EmptyState, LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { Button } from '@/components/ui/Button';
import { Field, Input, PathInput, Switch } from '@/components/ui/Input';
import { useConfigForm, SaveRow } from '@/features/settings/useConfigForm';

/**
 * System & engines.
 *
 * Three questions live here, and they are the ones a user actually asks:
 *
 * 1. "What is my machine doing right now?" — CPU / RAM / VRAM / disk pressure.
 * 2. "Where does my data live and how big is it?" — the data directory and its
 *    volume. This is the answer to "a knowledge base will eat my C: drive".
 * 3. "Which inference engines does this app know about?" — llama-server builds
 *    it can launch, plus external OpenAI-compatible servers the user already
 *    runs (registry + reachability only; the launcher never installs one).
 */
export function SystemTab() {
  const client = useQueryClient();
  const [live, setLive] = useState(true);

  const overview = useQuery({
    queryKey: ['system', 'overview'],
    queryFn: systemApi.overview,
    refetchInterval: live ? 3000 : false,
  });
  const storage = useQuery({ queryKey: ['system', 'storage'], queryFn: systemApi.storage });
  const paths = useQuery({ queryKey: ['system', 'paths'], queryFn: systemApi.paths });

  return (
    <div className="flex flex-col gap-6">
      <SectionHeader
        title={translate('settings.catSystem')}
        description={translate('settings.systemSubtitle')}
        action={
          <label className="flex items-center gap-2 text-[length:var(--text-xs)] text-[var(--text-secondary)]">
            {translate('settings.systemLive')}
            <Switch checked={live} onCheckedChange={setLive} label={translate('settings.systemLive')} />
          </label>
        }
      />

      <div className="grid gap-2 sm:grid-cols-2">
        <MetricCard
          icon={<Cpu size={14} aria-hidden="true" />}
          label={translate('sidebar.chipCpu')}
          value={overview.data ? `${Math.round(overview.data.cpu_percent)}%` : '—'}
        />
        <MetricCard
          icon={<MemoryStick size={14} aria-hidden="true" />}
          label={translate('sidebar.chipRam')}
          value={
            overview.data
              ? `${overview.data.memory_used_gb.toFixed(1)} / ${overview.data.memory_total_gb.toFixed(1)} GiB`
              : '—'
          }
        />
        <MetricCard
          icon={<HardDrive size={14} aria-hidden="true" />}
          label={translate('sidebar.chipVram')}
          value={
            overview.data?.gpus?.[0]
              ? `${Math.round(overview.data.gpus[0].memory_used_mb / 1024)} / ${Math.round(overview.data.gpus[0].memory_total_mb / 1024)} GiB`
              : translate('sidebar.chipNoGpu')
          }
        />
        <MetricCard
          icon={<HardDrive size={14} aria-hidden="true" />}
          label={translate('settings.systemDisk')}
          value={diskLabel(storage.data)}
        />
      </div>

      <DataDirBlock storage={storage.data} paths={paths.data} />

      <RuntimeBlock />

      <EngineBlock onChanged={() => void client.invalidateQueries({ queryKey: ['engines'] })} />

      <WebSearchBlock />
    </div>
  );
}

/**
 * Optional web search.
 *
 * Two fields, both required, and the copy says plainly that enabling this makes
 * the app send the user's query to the endpoint they name. It is the only
 * outbound call in an otherwise offline product, so it is not hidden in an
 * "advanced" section and the endpoint is visible rather than a built-in provider.
 */
function WebSearchBlock() {
  const { draft, patch, save, saving, dirty, config } = useConfigForm();
  if (config.isPending || !draft) return null;

  return (
    <section className="flex flex-col gap-3 border-t border-[var(--border)] pt-5">
      <SectionHeader
        title={translate('settings.catWebSearch')}
        description={translate('settings.webSearchDesc')}
      />
      <Switch
        checked={Boolean(draft.web_search_enabled)}
        onCheckedChange={(next) => patch({ web_search_enabled: next })}
        label={translate('settings.webSearchEnable')}
        description={translate('settings.webSearchEnableHelp')}
      />
      <Field
        label={translate('settings.webSearchEndpoint')}
        hint={translate('settings.webSearchEndpointHelp')}
        htmlFor="settings-web-search-endpoint"
      >
        <Input
          id="settings-web-search-endpoint"
          value={draft.web_search_endpoint}
          placeholder="http://127.0.0.1:8080"
          onChange={(event) => patch({ web_search_endpoint: event.target.value })}
          className="font-mono"
        />
      </Field>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label={translate('settings.webSearchResults')}
          hint={translate('settings.webSearchResultsHelp')}
          htmlFor="settings-web-search-results"
        >
          <Input
            id="settings-web-search-results"
            type="number"
            min={1}
            max={10}
            value={draft.web_search_max_results}
            onChange={(event) => patch({ web_search_max_results: Number(event.target.value) })}
          />
        </Field>
        <Field
          label={translate('settings.webSearchTimeout')}
          htmlFor="settings-web-search-timeout"
        >
          <Input
            id="settings-web-search-timeout"
            type="number"
            min={1}
            max={120}
            value={draft.web_search_timeout_seconds}
            onChange={(event) => patch({ web_search_timeout_seconds: Number(event.target.value) })}
          />
        </Field>
      </div>
      <SaveRow onSave={() => void save()} saving={saving} dirty={dirty} />
    </section>
  );
}

function diskLabel(storage: StorageReport | undefined): string {
  const usage = storage?.usage;
  if (!usage) return '—';
  if (usage.volume_free_bytes === null) return formatBytes(usage.data_dir_bytes);
  return translate('settings.systemDiskValue', {
    used: formatBytes(usage.data_dir_bytes),
    free: formatBytes(usage.volume_free_bytes),
  });
}

function MetricCard({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2">
      <p className="flex items-center gap-1.5 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
        {icon}
        {label}
      </p>
      <p className="mt-0.5 text-[length:var(--text-sm)] font-medium tnum">{value}</p>
    </div>
  );
}

function DataDirBlock({
  storage,
  paths,
}: {
  storage: StorageReport | undefined;
  paths: { data_dir: string; logs_dir: string } | undefined;
}) {
  const [note, setNote] = useState<string | null>(null);
  const usage = storage?.usage;

  const open = async (target: string | undefined) => {
    if (!target) return;
    const result = await openPath(target);
    if (!result.ok) setNote(result.error ?? translate('settings.openFailed'));
  };

  return (
    <section className="flex flex-col gap-3 border-t border-[var(--border)] pt-5">
      <SectionHeader title={translate('settings.systemDataDir')} />
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[length:var(--text-xs)]">
        <dt className="text-[var(--text-tertiary)]">{translate('settings.devDataDir')}</dt>
        <dd className="break-anywhere font-mono">{storage?.data_dir ?? paths?.data_dir ?? '—'}</dd>
        <dt className="text-[var(--text-tertiary)]">{translate('settings.devLogsDir')}</dt>
        <dd className="break-anywhere font-mono">{paths?.logs_dir ?? '—'}</dd>
        <dt className="text-[var(--text-tertiary)]">{translate('settings.systemDataSize')}</dt>
        <dd className="tnum">
          {usage
            ? `${formatBytes(usage.data_dir_bytes)} · ${translate('settings.systemFileCount', { count: usage.data_dir_files })}`
            : '—'}
          {usage?.truncated ? ` (${translate('settings.systemSizeTruncated')})` : ''}
        </dd>
      </dl>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="secondary"
          size="sm"
          onClick={() => void open(storage?.data_dir ?? paths?.data_dir)}
        >
          {translate('settings.systemOpenDataDir')}
        </Button>
        <Button variant="ghost" size="sm" onClick={() => void open(paths?.logs_dir)}>
          {translate('settings.systemOpenLogsDir')}
        </Button>
      </div>
      {note ? (
        <p role="status" className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
          {note}
        </p>
      ) : null}
      {storage && storage.writable === false ? (
        <p role="alert" className="text-[length:var(--text-xs)] text-[var(--danger)]">
          {translate('settings.storageWarning', { reasons: storage.reasons.join('; ') || '—' })}
        </p>
      ) : null}
    </section>
  );
}

function RuntimeBlock() {
  const client = useQueryClient();
  const runtimes = useQuery({ queryKey: ['runtimes'], queryFn: runtimesApi.list });
  const preferences = useQuery({ queryKey: ['runtimes', 'preferences'], queryFn: runtimesApi.preferences });
  const [path, setPath] = useState('');

  /** The registry names the default under the `gguf` preference key. */
  const defaultRuntimeId = preferences.data?.preferences?.gguf ?? null;

  const setDefaultRuntime = useMutation({
    mutationFn: (id: string) => runtimesApi.setPreferences({ gguf: id }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['runtimes'] });
      toast.success(translate('settings.systemDefaultSet'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const renameRuntime = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => runtimesApi.rename(id, name),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runtimes'] }),
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const register = useMutation({
    mutationFn: (value: string) => runtimesApi.register({ path: value }),
    onSuccess: () => {
      setPath('');
      void client.invalidateQueries({ queryKey: ['runtimes'] });
      toast.success(translate('settings.systemRuntimeAdded'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const probe = useMutation({
    mutationFn: (id: string) => runtimesApi.probe(id),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runtimes'] }),
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => runtimesApi.remove(id),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['runtimes'] }),
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const list = runtimes.data?.runtimes ?? [];

  return (
    <section className="flex flex-col gap-3 border-t border-[var(--border)] pt-5">
      <SectionHeader
        title={translate('settings.systemRuntimes')}
        description={translate('settings.systemRuntimesHelp')}
      />
      {runtimes.isPending ? <LoadingState /> : null}
      {!runtimes.isPending && list.length === 0 ? (
        <EmptyState compact title={translate('settings.systemRuntimesEmpty')} />
      ) : null}
      <ul className="flex flex-col gap-1.5">
        {list.map((runtime) => {
          const isDefaultRuntime = defaultRuntimeId === runtime.id;
          return (
          <li
            key={runtime.id}
            className="flex flex-wrap items-center gap-2 rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate text-[length:var(--text-xs)] font-medium">
                {runtime.display_name || runtime.id}
              </p>
              <p className="break-anywhere font-mono text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                {runtime.executable_path}
              </p>
              {runtime.version ? (
                <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                  {runtime.version}
                  {runtime.build_text ? ` · ${runtime.build_text}` : ''}
                </p>
              ) : null}
            </div>
            <Badge tone={runtime.probe?.probed ? 'success' : 'neutral'} dot>
              {runtime.probe?.probed
                ? translate('settings.systemProbed')
                : translate('settings.systemUnprobed')}
            </Badge>
            {/*
              "Use this one by default" had no UI: `runtimesApi.preferences` and
              `setPreferences` existed, so the preference file was written by
              nothing and the *first registered* runtime always won. That is a
              silent behaviour a user cannot see or change.
            */}
            <Button
              variant={isDefaultRuntime ? 'secondary' : 'ghost'}
              size="sm"
              aria-pressed={isDefaultRuntime}
              onClick={() => setDefaultRuntime.mutate(runtime.id)}
              loading={setDefaultRuntime.isPending}
            >
              {isDefaultRuntime
                ? translate('settings.systemDefaultRuntime')
                : translate('settings.systemMakeDefault')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                const name = window.prompt(translate('settings.systemRenameRuntime'), runtime.display_name || runtime.id);
                if (name && name.trim() && name.trim() !== runtime.display_name) {
                  renameRuntime.mutate({ id: runtime.id, name: name.trim() });
                }
              }}
              aria-label={translate('settings.systemRenameRuntime')}
            >
              <Pencil size={12} aria-hidden="true" />
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => probe.mutate(runtime.id)}
              loading={probe.isPending}
            >
              <Zap size={13} aria-hidden="true" />
              {translate('settings.systemProbe')}
            </Button>
            <Button variant="dangerGhost" size="sm" onClick={() => remove.mutate(runtime.id)}>
              <Trash2 size={13} aria-hidden="true" />
            </Button>
          </li>
          );
        })}
      </ul>
      <Field label={translate('settings.systemAddRuntime')} htmlFor="settings-runtime-path">
        <div className="flex gap-2">
          <PathInput
            id="settings-runtime-path"
            kind="file"
            value={path}
            onValueChange={setPath}
            filters={[{ name: 'Executable', extensions: ['exe'] }]}
          />
          <Button
            variant="primary"
            size="sm"
            disabled={!path.trim()}
            loading={register.isPending}
            onClick={() => register.mutate(path.trim())}
          >
            <Plus size={13} aria-hidden="true" />
            {translate('settings.systemRegister')}
          </Button>
        </div>
      </Field>
    </section>
  );
}

function EngineBlock({ onChanged }: { onChanged: () => void }) {
  const client = useQueryClient();
  const engines = useQuery({ queryKey: ['engines'], queryFn: enginesApi.list });
  const [form, setForm] = useState({
    kind: 'openai_compat',
    label: '',
    base_url: '',
    model: '',
    api_key_env: '',
  });

  const upsert = useMutation({
    mutationFn: () => enginesApi.upsert(form),
    onSuccess: () => {
      setForm({ kind: 'openai_compat', label: '', base_url: '', model: '', api_key_env: '' });
      void client.invalidateQueries({ queryKey: ['engines'] });
      onChanged();
      toast.success(translate('settings.systemEngineAdded'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const probe = useMutation({
    mutationFn: (id: string) => enginesApi.probe(id),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['engines'] }),
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => enginesApi.remove(id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['engines'] });
      onChanged();
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const list: EngineRecord[] = engines.data?.engines ?? [];
  const allowRemote = engines.data?.allow_remote ?? false;

  return (
    <section className="flex flex-col gap-3 border-t border-[var(--border)] pt-5">
      <SectionHeader
        title={translate('settings.systemEngines')}
        description={translate('settings.systemEnginesHelp')}
      />
      {allowRemote ? (
        <p className="rounded-[var(--radius-sm)] bg-[var(--warning-subtle)] px-2 py-1.5 text-[length:var(--text-2xs)] text-[var(--warning)]">
          {translate('settings.systemRemoteAllowed')}
        </p>
      ) : null}
      {engines.isPending ? <LoadingState /> : null}
      {!engines.isPending && list.length === 0 ? (
        <EmptyState compact title={translate('settings.systemEnginesEmpty')} />
      ) : null}
      <ul className="flex flex-col gap-1.5">
        {list.map((engine) => (
          <li
            key={engine.id}
            className="flex flex-wrap items-center gap-2 rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate text-[length:var(--text-xs)] font-medium">
                {engine.label || engine.base_url}
              </p>
              <p className="break-anywhere font-mono text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                {engine.kind} · {engine.base_url}
                {engine.model ? ` · ${engine.model}` : ''}
              </p>
              {engine.last_error ? (
                <p className="text-[length:var(--text-2xs)] text-[var(--danger)]">{engine.last_error}</p>
              ) : null}
            </div>
            <Badge
              tone={engine.last_probe_ok === true ? 'success' : engine.last_probe_ok === false ? 'danger' : 'neutral'}
              dot
            >
              {engine.last_probe_ok === true
                ? translate('settings.systemReachable')
                : engine.last_probe_ok === false
                  ? translate('settings.systemUnreachable')
                  : translate('settings.systemUnprobed')}
            </Badge>
            {engine.capabilities?.length ? (
              <span className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                {engine.capabilities.join(', ')}
              </span>
            ) : null}
            <Button variant="ghost" size="sm" onClick={() => probe.mutate(engine.id)} loading={probe.isPending}>
              <RefreshCw size={13} aria-hidden="true" />
              {translate('settings.systemProbe')}
            </Button>
            <Button variant="dangerGhost" size="sm" onClick={() => remove.mutate(engine.id)}>
              <Trash2 size={13} aria-hidden="true" />
            </Button>
          </li>
        ))}
      </ul>

      <div className="grid gap-2 rounded-[var(--radius-md)] border border-[var(--border)] p-3 sm:grid-cols-2">
        <Field label={translate('settings.systemEngineKind')} htmlFor="settings-engine-kind">
          <select
            id="settings-engine-kind"
            value={form.kind}
            onChange={(event) => setForm({ ...form, kind: event.target.value })}
            className="h-[var(--control-h)] rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-sm)]"
          >
            <option value="openai_compat">openai_compat</option>
            <option value="llama_cpp">llama_cpp</option>
          </select>
        </Field>
        <Field label={translate('settings.systemEngineLabel')} htmlFor="settings-engine-label">
          <Input
            id="settings-engine-label"
            value={form.label}
            onChange={(event) => setForm({ ...form, label: event.target.value })}
          />
        </Field>
        <Field label={translate('settings.systemEngineUrl')} htmlFor="settings-engine-url">
          <Input
            id="settings-engine-url"
            value={form.base_url}
            placeholder="http://127.0.0.1:8000/v1"
            onChange={(event) => setForm({ ...form, base_url: event.target.value })}
            className="font-mono"
          />
        </Field>
        <Field label={translate('settings.systemEngineModel')} htmlFor="settings-engine-model">
          <Input
            id="settings-engine-model"
            value={form.model}
            onChange={(event) => setForm({ ...form, model: event.target.value })}
            className="font-mono"
          />
        </Field>
        <Field
          label={translate('settings.systemEngineKeyEnv')}
          hint={translate('settings.systemEngineKeyEnvHelp')}
          htmlFor="settings-engine-key-env"
        >
          <Input
            id="settings-engine-key-env"
            value={form.api_key_env}
            placeholder="MY_ENGINE_KEY"
            onChange={(event) => setForm({ ...form, api_key_env: event.target.value })}
            className="font-mono"
          />
        </Field>
        <div className="flex items-end">
          <Button
            variant="primary"
            disabled={!form.base_url.trim()}
            loading={upsert.isPending}
            onClick={() => upsert.mutate()}
          >
            {translate('settings.systemEngineAdd')}
          </Button>
        </div>
      </div>
    </section>
  );
}

export { cn, pickPath };
