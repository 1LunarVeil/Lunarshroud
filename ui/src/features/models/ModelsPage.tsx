import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import { useRef } from 'react';
import { FolderOpen, RefreshCw, Search } from 'lucide-react';
import { useUiStore } from '@/lib/stores/ui';
import { useHandoffStore } from '@/lib/stores/handoff';
import { modelsApi, systemApi } from '@/lib/api';
import type { ModelEntry } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn, formatBytes } from '@/lib/utils';
import { Badge, EmptyState, ErrorState, LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { ManifestEditor } from '@/features/models/ManifestEditor';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';

/**
 * Model library.
 *
 * The list is **virtualized**: a model folder routinely holds hundreds of GGUF files,
 * and rendering every row with its capability badges made the page stutter in 1.0. Only
 * the rows in view are mounted.
 *
 * Capability data has two costs, and the page keeps them separate (a 1.0 lesson worth
 * preserving): the scan attaches a cheap summary per file (native context, quantisation,
 * whether it is an mmproj/draft head), while identifying what companion files *are*
 * requires reading each one's header and happens only for the selected model. The list
 * never pays that price per row.
 */
export function ModelsPage() {
  const navigate = useUiStore((state) => state.navigate);
  const config = useQuery({ queryKey: ['system', 'config'], queryFn: systemApi.getConfig });
  const directory = String(config.data?.default_model_dir ?? '');

  const [search, setSearch] = useState('');
  const [selectedPath, setSelectedPath] = useState<string | null>(null);

  const scan = useQuery({
    queryKey: ['models', 'scan', directory],
    queryFn: () => modelsApi.scan(directory, true, true),
    enabled: Boolean(directory),
    staleTime: 60_000,
  });

  const models = useMemo(() => {
    const list = scan.data?.models ?? [];
    const needle = search.trim().toLowerCase();
    const filtered = needle
      ? list.filter(
          (model) =>
            model.filename.toLowerCase().includes(needle) ||
            (model.quant ?? '').toLowerCase().includes(needle) ||
            (model.manifest?.model_key ?? '').toLowerCase().includes(needle),
        )
      : list;
    // Companion files are not loadable models; keep them out of the primary list.
    return filtered.filter((model) => !model.capabilities?.is_mmproj && !model.capabilities?.is_draft);
  }, [scan.data, search]);

  const selected = models.find((model) => model.path === selectedPath) ?? models[0] ?? null;

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const virtualizer = useVirtualizer({
    count: models.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 44,
    overscan: 12,
  });

  if (!directory) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center p-6">
        <EmptyState
          icon={<FolderOpen size={20} />}
          title={translate('models.noDir')}
          description={translate('models.noDirSub')}
          action={
            <Button variant="primary" onClick={() => navigate('settings')}>
              {translate('settings.modelDir')}
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="border-b border-[var(--border)] px-5 pb-3 pt-4">
        <SectionHeader
          title={translate('nav.models')}
          description={directory}
          action={
            <Button variant="secondary" size="sm" onClick={() => void scan.refetch()} loading={scan.isFetching}>
              <RefreshCw size={13} aria-hidden="true" />
              {translate('models.rescan')}
            </Button>
          }
        />
        <div className="mt-3 flex items-center gap-2">
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={translate('models.searchPh')}
            leading={<Search size={13} aria-hidden="true" />}
            className="max-w-md"
          />
          <span className="text-[length:var(--text-2xs)] tnum text-[var(--text-tertiary)]">
            {String(models.length)}
          </span>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        <div
          ref={scrollRef}
          className="min-h-0 flex-1 overflow-y-auto"
          data-testid="model-list"
        >
          {scan.isPending ? (
            <LoadingState />
          ) : scan.isError ? (
            <ErrorState
              message={localizeBackendError(scan.error instanceof Error ? scan.error.message : scan.error)}
              action={
                <Button variant="secondary" onClick={() => void scan.refetch()}>
                  {translate('common.retry')}
                </Button>
              }
            />
          ) : models.length === 0 ? (
            <EmptyState
              title={search ? translate('models.empty') : translate('models.empty')}
              description={search ? undefined : translate('models.emptySub')}
            />
          ) : (
            <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
              {virtualizer.getVirtualItems().map((item) => {
                const model = models[item.index];
                if (!model) return null;
                return (
                  <div
                    key={model.path}
                    style={{
                      position: 'absolute',
                      top: 0,
                      left: 0,
                      width: '100%',
                      transform: `translateY(${item.start}px)`,
                    }}
                  >
                    <ModelRow
                      model={model}
                      active={selected?.path === model.path}
                      onSelect={() => setSelectedPath(model.path)}
                    />
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <aside className="w-[22rem] shrink-0 overflow-y-auto border-s border-[var(--border)] bg-[var(--panel)] p-4">
          {selected ? (
            <ModelDetail
              model={selected}
              onConfigure={() => {
                useHandoffStore.getState().requestModel(selected.path);
                navigate('params');
              }}
            />
          ) : (
            <EmptyState compact title={translate('models.selectHint')} />
          )}
        </aside>
      </div>
    </div>
  );
}

function ModelRow({
  model,
  active,
  onSelect,
}: {
  model: ModelEntry;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? 'true' : undefined}
      data-testid="model-row"
      className={cn(
        'flex h-11 w-full items-center gap-3 border-b border-[var(--border-faint,var(--border))] px-4 text-start',
        'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
        active ? 'bg-[var(--selected)]' : 'hover:bg-[var(--hover)]',
      )}
    >
      <span className="min-w-0 flex-1 truncate text-[length:var(--text-sm)]" title={model.path}>
        {model.filename}
      </span>
      {model.quant ? <Badge tone="neutral">{model.quant}</Badge> : null}
      <span className="w-20 shrink-0 text-end text-[length:var(--text-2xs)] tnum text-[var(--text-tertiary)]">
        {formatBytes(model.size_bytes)}
      </span>
    </button>
  );
}

function ModelDetail({ model, onConfigure }: { model: ModelEntry; onConfigure: () => void }) {
  const caps = model.capabilities ?? {};
  const nativeContext = Number(caps.n_ctx_train ?? caps.context_length ?? 0);
  return (
    <div className="flex flex-col gap-3">
      <div>
        <p className="break-anywhere text-[length:var(--text-sm)] font-medium">{model.filename}</p>
        <p className="mt-1 break-anywhere font-mono text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
          {model.path}
        </p>
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-[length:var(--text-2xs)]">
        <dt className="text-[var(--text-tertiary)]">{translate('models.quant')}</dt>
        <dd className="tnum text-[var(--text)]">{formatBytes(model.size_bytes)}</dd>
        <dt className="text-[var(--text-tertiary)]">{translate('models.quant')}</dt>
        <dd className="text-[var(--text)]">{model.quant ?? '—'}</dd>
        <dt className="text-[var(--text-tertiary)]">{translate('models.ctx')}</dt>
        <dd className="tnum text-[var(--text)]">{nativeContext ? nativeContext.toLocaleString() : '—'}</dd>
        <dt className="text-[var(--text-tertiary)]">{translate('models.arch')}</dt>
        <dd className="text-[var(--text)]">{String(caps.architecture ?? '—')}</dd>
      </dl>

      <div className="flex flex-wrap gap-1.5">
        {caps.has_vision_encoder ? <Badge tone="accent">{translate('models.badgeVision')}</Badge> : null}
        {caps.is_draft ? <Badge tone="info">{translate('models.badgeAssistant')}</Badge> : null}
        {caps.is_mmproj ? <Badge tone="info">mmproj</Badge> : null}
        {model.manifest?.model_key ? <Badge tone="success">{translate('models.manifestKey')}</Badge> : null}
      </div>

      {caps.error ? (
        <p className="rounded-[var(--radius-sm)] bg-[var(--warning-subtle)] px-2 py-1 text-[length:var(--text-2xs)] text-[var(--warning)]">
          {String(caps.error)}
        </p>
      ) : null}

      <Button variant="primary" onClick={onConfigure}>
        {translate('models.useInEditor')}
      </Button>

      {/*
        The manifest editor was the missing entrance: seven routes and a full test
        suite existed, and this panel showed only the paragraph that used to live
        below. A manifest is where a model's context length and mmproj pairing are
        recorded next to the file, so it belongs here rather than on a page of its own.
      */}
      <ManifestEditor modelPath={model.path} />
    </div>
  );
}

export default ModelsPage;
