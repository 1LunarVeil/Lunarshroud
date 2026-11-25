import { useQuery } from '@tanstack/react-query';
import { Check, Circle, ExternalLink } from 'lucide-react';
import { systemApi, runtimesApi, modelsApi } from '@/lib/api';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Code, SectionHeader } from '@/components/ui/Feedback';
import { Button } from '@/components/ui/Button';
import { useUiStore } from '@/lib/stores/ui';

/**
 * Getting-started guide.
 *
 * The checklist is **derived from the real configuration**, not a static list. A guide
 * that tells everyone "step 1: set the llama-server path" is useless to someone who has
 * already done it, and a guide that shows a tick without checking is worse than no guide.
 *
 * The llama.cpp release links are the one place in the app that intentionally points at
 * the public internet. They are anchors the user clicks; nothing is fetched.
 */
export function HelpPage() {
  const navigate = useUiStore((state) => state.navigate);
  const openSettings = useUiStore((state) => state.openSettings);
  const config = useQuery({ queryKey: ['system', 'config'], queryFn: systemApi.getConfig });
  const runtimes = useQuery({ queryKey: ['runtimes'], queryFn: runtimesApi.list, staleTime: 30_000 });
  const directory = String(config.data?.default_model_dir ?? '');
  const models = useQuery({
    queryKey: ['models', 'scan', directory],
    queryFn: () => modelsApi.scan(directory),
    enabled: Boolean(directory),
    staleTime: 60_000,
  });

  const hasServer = Boolean(String(config.data?.llama_server_path ?? '').trim());
  const hasRuntime = (runtimes.data?.runtimes ?? []).length > 0;
  const modelCount = models.data?.models?.length ?? 0;
  const hasModels = modelCount > 0;

  // The checklist is derived from the real configuration, so a completed step shows a
  // tick because it *is* done — never because the guide assumes it.
  const steps = [
    {
      done: hasServer,
      title: translate('help.check.serverMissing'),
      body: hasServer ? translate('help.check.serverOk') : translate('help.check.serverProbe'),
      action: hasServer ? null : (
        <Button size="sm" variant="primary" onClick={() => openSettings('general')}>
          {translate('help.openSettings')}
        </Button>
      ),
    },
    {
      done: hasRuntime,
      title: translate('help.check.serverFromRuntime'),
      body: hasRuntime ? translate('help.check.serverOk') : translate('help.check.serverProbe'),
      action: hasRuntime ? null : (
        // `navigate('runtimes')` used to point at a route that does not exist in
        // `PageHost`: the click replaced the whole window with the crash boundary.
        // Runtime management now lives in Settings → 系统与引擎.
        <Button size="sm" variant="secondary" onClick={() => openSettings('system')}>
          {translate('help.openRuntimes')}
        </Button>
      ),
    },
    {
      done: hasModels,
      title: translate('help.check.dirMissing'),
      body: hasModels
        ? translate('help.check.modelsOk', { count: modelCount })
        : translate('help.check.modelsNone'),
      action: hasModels ? null : (
        <Button size="sm" variant="secondary" onClick={() => openSettings('general')}>
          {translate('help.openSettings')}
        </Button>
      ),
    },
    {
      done: false,
      title: translate('help.check.notLaunched'),
      body: translate('help.check.launched'),
      action: (
        <Button size="sm" variant="primary" onClick={() => navigate('params')}>
          {translate('app.pickModel')}
        </Button>
      ),
    },
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mx-auto w-full max-w-[56rem] px-6 py-5">
        <SectionHeader title={translate('help.title')} description={translate('help.subtitle')} />

        <ol className="mt-5 flex flex-col gap-2">
          {steps.map((step, index) => (
            <li
              key={step.title}
              className={cn(
                'flex items-start gap-3 rounded-[var(--radius-md)] border p-3',
                step.done
                  ? 'border-[var(--success)]/30 bg-[var(--success-subtle)]'
                  : 'border-[var(--border)] bg-[var(--surface)]',
              )}
            >
              <span
                aria-hidden="true"
                className={cn(
                  'mt-0.5 grid size-5 shrink-0 place-items-center rounded-full',
                  step.done
                    ? 'fill-success'
                    : 'bg-[var(--surface-sunken)] text-[var(--text-tertiary)]',
                )}
              >
                {step.done ? <Check size={12} /> : <Circle size={8} />}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-[length:var(--text-sm)] font-medium">
                  {index + 1}. {step.title}
                </p>
                <p className="mt-0.5 text-[length:var(--text-xs)] leading-[var(--leading-relaxed)] text-[var(--text-secondary)]">
                  {step.body}
                </p>
                {step.action ? <div className="mt-2">{step.action}</div> : null}
              </div>
            </li>
          ))}
        </ol>

        <section className="mt-6">
          <h2 className="text-[length:var(--text-md)] font-semibold">{translate('help.gpuTitle')}</h2>
          <div className="mt-2 overflow-hidden rounded-[var(--radius-md)] border border-[var(--border)]">
            <table className="w-full text-[length:var(--text-xs)]">
              <thead>
                <tr className="border-b border-[var(--border)] bg-[var(--surface-sunken)] text-start">
                  <th className="px-3 py-1.5 text-start font-medium">{translate('help.gpu')}</th>
                  <th className="px-3 py-1.5 text-start font-medium">{translate('help.quickStart')}</th>
                </tr>
              </thead>
              <tbody>
                {[
                  ['NVIDIA (RTX 30/40/50)', 'bin-win-cuda12-x64'],
                  ['AMD 独显（官方 ROCm 支持列表）', 'bin-win-rocm-*-x64'],
                  ['AMD 其它 / 核显', 'bin-win-vulkan-x64'],
                  ['Intel 核显 / Arc', 'bin-win-openvino-*-x64'],
                  ['仅 CPU', 'bin-win-cpu-x64'],
                ].map(([gpu, pkg]) => (
                  <tr key={pkg} className="border-b border-[var(--border-faint,var(--border))] last:border-0">
                    <td className="px-3 py-1.5">{gpu}</td>
                    <td className="px-3 py-1.5">
                      <Code>{pkg}</Code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-[length:var(--text-xs)] text-[var(--text-secondary)]">
            {translate('help.quickStart')}{' '}
            <a
              href="https://github.com/ggml-org/llama.cpp/releases"
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center gap-1 text-[var(--accent-text)] underline-offset-2 hover:underline"
            >
              github.com/ggml-org/llama.cpp/releases
              <ExternalLink size={11} aria-hidden="true" />
            </a>
          </p>
        </section>

        <section className="mt-6 rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] p-4">
          <p className="text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
            {translate('help.mistakesTitle')}
          </p>
          <p className="mt-1 text-[length:var(--text-xs)] leading-[var(--leading-relaxed)] text-[var(--text-tertiary)]">
            {translate('help.subtitle')}
          </p>
        </section>
      </div>
    </div>
  );
}

export default HelpPage;
