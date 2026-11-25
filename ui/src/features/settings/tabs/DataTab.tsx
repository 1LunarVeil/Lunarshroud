import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Info } from 'lucide-react';
import { backupsApi, systemApi } from '@/lib/api';
import { pickPath } from '@/lib/api/client';
import { translate, localizeBackendError } from '@/lib/i18n';
import { formatBytes, formatTime } from '@/lib/utils';
import { Badge, EmptyState, LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { Button } from '@/components/ui/Button';
import { Field, Input } from '@/components/ui/Input';

/**
 * Data: where the databases live, backups, and (later) migration.
 *
 * Backups were fully implemented on the backend (`backupsApi` had every call)
 * with **no caller anywhere** — a feature that exists, is tested, and cannot be
 * reached. This tab is that missing entrance.
 */
export function DataTab() {
  const client = useQueryClient();
  const storage = useQuery({ queryKey: ['system', 'storage'], queryFn: systemApi.storage });
  const backups = useQuery({ queryKey: ['backups'], queryFn: backupsApi.list });
  const [includeLogs, setIncludeLogs] = useState(false);

  const create = useMutation({
    mutationFn: () => backupsApi.create(includeLogs),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['backups'] });
      toast.success(translate('settings.dataBackupCreated'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const restore = useMutation({
    mutationFn: (name: string) => backupsApi.restore(name),
    onSuccess: () =>
      toast.success(translate('settings.dataRestoreQueued')),
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const remove = useMutation({
    mutationFn: (name: string) => backupsApi.remove(name),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['backups'] }),
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const inspect = useMutation({
    mutationFn: (name: string) => backupsApi.inspect(name),
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const cancelPending = useMutation({
    mutationFn: () => backupsApi.cancelPending(),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['backups'] });
      toast.success(translate('settings.dataRestoreCancelled'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const usage = storage.data?.usage;

  return (
    <div className="flex flex-col gap-6">
      <SectionHeader
        title={translate('settings.catData')}
        description={translate('settings.dataSubtitle')}
      />

      <section className="flex flex-col gap-3">
        <SectionHeader title={translate('settings.systemDataDir')} />
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[length:var(--text-xs)]">
          <dt className="text-[var(--text-tertiary)]">{translate('settings.devDataDir')}</dt>
          <dd className="break-anywhere font-mono">{storage.data?.data_dir ?? '—'}</dd>
          <dt className="text-[var(--text-tertiary)]">{translate('settings.systemDataSize')}</dt>
          <dd className="tnum">
            {usage
              ? `${formatBytes(usage.data_dir_bytes)} · ${translate('settings.systemFileCount', { count: usage.data_dir_files })}`
              : '—'}
          </dd>
          {Object.entries(usage?.database_bytes ?? {}).map(([label, bytes]) => (
            <div key={label} className="contents">
              <dt className="font-mono text-[var(--text-tertiary)]">{label}.db</dt>
              <dd className="tnum">{formatBytes(bytes)}</dd>
            </div>
          ))}
        </dl>
        <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
          {translate('settings.dataDirHelp')}
        </p>
      </section>

      <section className="flex flex-col gap-3 border-t border-[var(--border)] pt-5">
        <SectionHeader title={translate('settings.catBackup')} />
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-[length:var(--text-xs)] text-[var(--text-secondary)]">
            <input
              type="checkbox"
              checked={includeLogs}
              onChange={(event) => setIncludeLogs(event.target.checked)}
              className="accent-[var(--accent)]"
            />
            {translate('settings.dataIncludeLogs')}
          </label>
          <Button variant="primary" size="sm" loading={create.isPending} onClick={() => create.mutate()}>
            {translate('settings.dataCreateBackup')}
          </Button>
        </div>

        {backups.isPending ? <LoadingState /> : null}
        {!backups.isPending && (backups.data?.backups.length ?? 0) === 0 ? (
          <EmptyState compact title={translate('settings.dataNoBackups')} />
        ) : null}
        <ul className="flex flex-col gap-1.5">
          {(backups.data?.backups ?? []).map((entry) => (
            <li
              key={entry.name}
              className="flex flex-wrap items-center gap-2 rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] px-3 py-2"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono text-[length:var(--text-xs)]">{entry.name}</p>
                <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                  {formatTime(entry.created_at)} · {formatBytes(entry.size)}
                </p>
              </div>
              {entry.verified ? (
                <Badge tone="success" dot>
                  {translate('settings.dataVerified')}
                </Badge>
              ) : null}
              <Button
                variant="secondary"
                size="sm"
                loading={inspect.isPending}
                onClick={() => inspect.mutate(entry.name)}
              >
                <Info size={12} aria-hidden="true" />
                {translate('settings.dataInspect')}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                loading={restore.isPending}
                onClick={() => restore.mutate(entry.name)}
              >
                {translate('settings.dataRestore')}
              </Button>
              <Button variant="dangerGhost" size="sm" onClick={() => remove.mutate(entry.name)}>
                {translate('common.delete')}
              </Button>
            </li>
          ))}
        </ul>
        {/*
          A staged restore used to be one-way from the UI: `backupsApi.cancelPending`
          existed and nothing could reach it, so a user who picked the wrong backup
          had to let the app restart and hope. Cancelling is now a visible action
          whenever a restore is pending.
        */}
        {backups.data?.pending ? (
          <div className="flex flex-wrap items-center gap-2 rounded-[var(--radius-sm)] bg-[var(--warning-subtle)] px-2.5 py-1.5">
            <span className="flex-1 text-[length:var(--text-2xs)] text-[var(--warning)]">
              {translate('settings.dataRestorePending', { name: backups.data.pending })}
            </span>
            <Button
              variant="dangerGhost"
              size="sm"
              loading={cancelPending.isPending}
              onClick={() => cancelPending.mutate()}
            >
              {translate('settings.dataCancelRestore')}
            </Button>
          </div>
        ) : null}
        {inspect.data ? (
          <pre className="max-h-48 overflow-auto rounded-[var(--radius-sm)] bg-[var(--code-bg)] p-2 font-mono text-[length:var(--text-2xs)] text-[var(--code-gutter)]">
            {JSON.stringify(inspect.data, null, 2)}
          </pre>
        ) : null}
        <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
          {translate('settings.dataRestoreHelp')}
        </p>
      </section>

      <DataDirMove current={storage.data?.data_dir} />
    </div>
  );
}

/**
 * Move the data directory.
 *
 * The move is **copy + pointer**, never delete: the old directory stays as the
 * rollback. The backend writes `<exe>/data-location.txt` and the next start reads
 * it, so "moved" takes effect after a restart rather than pretending to be live.
 */
function DataDirMove({ current }: { current: string | undefined }) {
  const [target, setTarget] = useState('');
  const [note, setNote] = useState<string | null>(null);

  const migrate = useMutation({
    mutationFn: (destination: string) => systemApi.migrateDataDir(destination),
    onSuccess: (result) => {
      setNote(
        translate('settings.dataMigrateDone', {
          path: result.target,
          restart: translate(result.restart_required ? 'settings.dataRestartYes' : 'settings.dataRestartNo'),
        }),
      );
      toast.success(translate('settings.dataMigrateToast'));
    },
    onError: (error) => {
      const message = localizeBackendError(error instanceof Error ? error.message : error);
      setNote(message);
      toast.error(message);
    },
  });

  const choose = async () => {
    const picked = await pickPath('dir', { current, prompt: translate('settings.dataPickPrompt') });
    if (picked) setTarget(picked);
  };

  return (
    <section className="flex flex-col gap-3 border-t border-[var(--border)] pt-5">
      <SectionHeader
        title={translate('settings.dataMigrate')}
        description={translate('settings.dataMigrateHelp')}
      />
      <Field label={translate('settings.dataTarget')} htmlFor="settings-data-target">
        <div className="flex gap-2">
          <Input
            id="settings-data-target"
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            placeholder="D:\\LunarShroudData"
            className="font-mono"
          />
          <Button variant="secondary" size="sm" onClick={() => void choose()}>
            {translate('settings.browse')}
          </Button>
          <Button
            variant="primary"
            size="sm"
            disabled={!target.trim()}
            loading={migrate.isPending}
            onClick={() => migrate.mutate(target.trim())}
          >
            {translate('settings.dataMigrateAction')}
          </Button>
        </div>
      </Field>
      {note ? (
        <p role="status" className="break-anywhere text-[length:var(--text-2xs)] text-[var(--text-secondary)]">
          {note}
        </p>
      ) : null}
    </section>
  );
}
