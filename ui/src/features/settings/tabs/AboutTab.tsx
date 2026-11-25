import { useQuery } from '@tanstack/react-query';
import { systemApi } from '@/lib/api';
import { translate } from '@/lib/i18n';
import { LoadingState, SectionHeader } from '@/components/ui/Feedback';

/** About: version and the runtime facts a bug report needs. */
export function AboutTab() {
  const paths = useQuery({ queryKey: ['system', 'paths'], queryFn: systemApi.paths });

  return (
    <div className="flex flex-col gap-4">
      <SectionHeader
        title={translate('settings.catAbout')}
        description={translate('settings.aboutSubtitle')}
      />
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
    </div>
  );
}

export { LoadingState };
