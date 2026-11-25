import { useQuery } from '@tanstack/react-query';
import { backupsApi, systemApi } from '@/lib/api';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { LoadingState, SectionHeader } from '@/components/ui/Feedback';

/**
 * Developer facts: paths, Python version, and per-database writability.
 *
 * Every row here is read-only on purpose — this page answers "where is it" and
 * "can it be written", not "change it". Changing the data location has its own
 * flow in the Data tab, because it needs a copy, a pointer file and a restart,
 * and mixing that into a read-only panel is how users end up clicking something
 * they did not expect.
 */
export function DiagnosticsTab() {
  const paths = useQuery({ queryKey: ['system', 'paths'], queryFn: systemApi.paths });
  const storage = useQuery({ queryKey: ['system', 'storage'], queryFn: systemApi.storage });
  const backups = useQuery({ queryKey: ['backups'], queryFn: backupsApi.list });

  return (
    <div className="flex flex-col gap-5">
      <SectionHeader title={translate('settings.catDeveloper')} />
      {paths.isPending ? <LoadingState label={translate('settings.loading')} /> : null}
      {paths.data ? (
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-[length:var(--text-xs)]">
          {[
            ['settings.devVersion', `${paths.data.app_version}${paths.data.frozen ? ' · packaged' : ' · source'}`],
            ['settings.devPlatform', paths.data.platform],
            ['settings.devPython', paths.data.python_version],
            ['settings.devDataDir', paths.data.data_dir],
            ['settings.devLogsDir', paths.data.logs_dir],
            ['settings.devBackendConfig', paths.data.config_file],
            ['settings.devLauncherPort', String(paths.data.listening_port ?? paths.data.configured_port ?? '')],
          ].map(([labelKey, value]) => (
            <div key={String(labelKey)} className="contents">
              <dt className="text-[var(--text-tertiary)]">{translate(String(labelKey))}</dt>
              <dd className="break-anywhere font-mono text-[var(--text)]">{String(value ?? '')}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {storage.data ? (
        <div className="rounded-[var(--radius-md)] border border-[var(--border)] p-3">
          <p className="mb-2 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
            {translate('settings.catStorage')}
          </p>
          <ul className="flex flex-col gap-1.5">
            {Object.entries(storage.data.databases).map(([label, database]) => (
              <li key={database.path} className="flex items-start justify-between gap-3 text-[length:var(--text-xs)]">
                <div className="min-w-0">
                  <p className="font-mono text-[var(--text-secondary)]">{label}</p>
                  <p className="break-anywhere font-mono text-[var(--text-quaternary)]">
                    {database.path}
                  </p>
                  {database.reason ? (
                    <p className="text-[var(--danger)]">{database.reason}</p>
                  ) : null}
                </div>
                <span
                  className={cn(
                    'shrink-0 rounded-[var(--radius-pill)] px-1.5 py-[1px]',
                    database.writable
                      ? 'bg-[var(--success-subtle)] text-[var(--success)]'
                      : 'bg-[var(--danger-subtle)] text-[var(--danger)]',
                  )}
                >
                  {database.writable
                    ? translate('settings.storageWritable')
                    : translate('settings.storageReadOnly')}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
        {translate('settings.devBackups', { count: backups.data?.backups.length ?? 0 })}
      </p>
    </div>
  );
}
