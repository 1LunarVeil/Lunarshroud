import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Copy, Download, Plus, Trash2, Upload } from 'lucide-react';
import { presetsApi } from '@/lib/api';
import type { Preset } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Badge, EmptyState, ErrorState, LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { Button } from '@/components/ui/Button';
import { Field, Input } from '@/components/ui/Input';

/**
 * Presets: named parameter sets.
 *
 * Export writes a file the user can move between machines; import validates before
 * writing, so a wrong file is refused with a reason instead of half-applied. Both go
 * through the same API the parameter editor saves with, so a preset created here is
 * indistinguishable from one created there.
 */
export function PresetsPage() {
  const client = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [newName, setNewName] = useState('');

  const presets = useQuery({ queryKey: ['presets'], queryFn: presetsApi.list });
  const list: Preset[] = presets.data ?? [];
  const selected = list.find((preset) => preset.id === selectedId) ?? list[0] ?? null;

  const create = useMutation({
    mutationFn: (name: string) => presetsApi.create({ name, params: {} }),
    onSuccess: (preset) => {
      setSelectedId(preset.id);
      setNewName('');
      void client.invalidateQueries({ queryKey: ['presets'] });
      toast.success(translate('presets.created'));
    },
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => presetsApi.remove(id),
    onSuccess: () => {
      setSelectedId(null);
      void client.invalidateQueries({ queryKey: ['presets'] });
      toast.success(translate('presets.deleted'));
    },
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const clone = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => presetsApi.clone(id, name),
    onSuccess: (preset) => {
      setSelectedId(preset.id);
      void client.invalidateQueries({ queryKey: ['presets'] });
    },
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  /** Export downloads the preset as JSON so it can move between machines. */
  const exportPreset = async (preset: Preset) => {
    try {
      const payload = await presetsApi.export(preset.id);
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `${preset.name || 'preset'}.json`;
      anchor.click();
      // Revoking immediately can cancel the download in WebView2; defer a tick.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  const importPreset = async (file: File) => {
    try {
      const text = await file.text();
      const parsed = JSON.parse(text) as unknown;
      await presetsApi.import(parsed);
      void client.invalidateQueries({ queryKey: ['presets'] });
      toast.success(translate('presets.imported'));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  if (presets.isPending) return <LoadingState />;
  if (presets.isError) {
    return (
      <ErrorState
        message={localizeBackendError(presets.error instanceof Error ? presets.error.message : presets.error)}
        action={<Button variant="secondary" onClick={() => void presets.refetch()}>{translate('common.retry')}</Button>}
      />
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="border-b border-[var(--border)] px-5 pb-3 pt-4">
        <SectionHeader
          title={translate('nav.presets')}
          description={translate('presets.subtitle')}
          action={
            <div className="flex gap-2">
              <label className="inline-flex h-[var(--control-h)] cursor-pointer items-center gap-1.5 rounded-[var(--radius-md)] border border-[var(--border-control)] px-3 text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--hover)]">
                <Upload size={13} aria-hidden="true" />
                {translate('presets.import')}
                <input
                  type="file"
                  accept="application/json,.json"
                  className="hidden"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) void importPreset(file);
                    event.target.value = '';
                  }}
                />
              </label>
            </div>
          }
        />
        <div className="mt-3 flex items-end gap-2">
          <Field label={translate('presets.newTitle')} htmlFor="preset-new" className="max-w-sm flex-1">
            <Input
              id="preset-new"
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              placeholder={translate('presets.newTitle')}
            />
          </Field>
          <Button
            variant="primary"
            disabled={!newName.trim()}
            loading={create.isPending}
            onClick={() => create.mutate(newName.trim())}
          >
            <Plus size={14} aria-hidden="true" />
            {translate('common.create')}
          </Button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1">
        <ul className="w-[16rem] shrink-0 overflow-y-auto border-e border-[var(--border)] p-1.5">
          {list.length === 0 ? (
            <li>
              <EmptyState compact title={translate('presets.empty')} />
            </li>
          ) : (
            list.map((preset) => (
              <li key={preset.id}>
                <button
                  type="button"
                  data-testid="preset-row"
                  aria-current={selected?.id === preset.id ? 'true' : undefined}
                  onClick={() => setSelectedId(preset.id)}
                  className={cn(
                    'flex h-9 w-full items-center gap-2 rounded-[var(--radius-sm)] px-2 text-start text-[length:var(--text-sm)]',
                    selected?.id === preset.id
                      ? 'bg-[var(--selected)] font-medium'
                      : 'text-[var(--text-secondary)] hover:bg-[var(--hover)]',
                  )}
                >
                  <span className="truncate">{preset.name}</span>
                </button>
              </li>
            ))
          )}
        </ul>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {selected ? (
            <div className="flex max-w-[42rem] flex-col gap-3">
              <div>
                <h2 className="text-[length:var(--text-lg)] font-semibold">{selected.name}</h2>
                {selected.description ? (
                  <p className="mt-1 text-[length:var(--text-sm)] text-[var(--text-secondary)]">
                    {selected.description}
                  </p>
                ) : null}
              </div>

              {selected.tags?.length ? (
                <div className="flex flex-wrap gap-1.5">
                  {selected.tags.map((tag) => (
                    <Badge key={tag} tone="neutral">
                      {tag}
                    </Badge>
                  ))}
                </div>
              ) : null}

              <div className="rounded-[var(--radius-md)] border border-[var(--border)] p-3">
                <p className="mb-2 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
                  {translate('presets.paramDetail')}
                </p>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-anywhere font-mono text-[length:var(--text-2xs)] text-[var(--text-secondary)]">
                  {JSON.stringify(selected.params ?? {}, null, 2)}
                </pre>
              </div>

              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" onClick={() => void exportPreset(selected)}>
                  <Download size={13} aria-hidden="true" />
                  {translate('presets.export')}
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => clone.mutate({ id: selected.id, name: `${selected.name} copy` })}
                  loading={clone.isPending}
                >
                  <Copy size={13} aria-hidden="true" />
                  {translate('presets.clone')}
                </Button>
                <Button
                  variant="dangerGhost"
                  onClick={() => remove.mutate(selected.id)}
                  loading={remove.isPending}
                >
                  <Trash2 size={13} aria-hidden="true" />
                  {translate('common.delete')}
                </Button>
              </div>
            </div>
          ) : (
            <EmptyState title={translate('presets.selectHint')} />
          )}
        </div>
      </div>
    </div>
  );
}

export default PresetsPage;
