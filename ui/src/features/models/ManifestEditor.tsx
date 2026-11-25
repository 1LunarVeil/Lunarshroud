import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { CheckCircle2, FileCode2, Trash2 } from 'lucide-react';
import { modelsApi } from '@/lib/api';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/Button';
import { Badge, LoadingState } from '@/components/ui/Feedback';

/**
 * Model manifest editor.
 *
 * Seven backend routes for this existed with **zero call sites** — including a
 * full test suite (`test_manifest_editor.py`) — while the Models page showed a
 * single line of grey text. A manifest is the reference app's core differentiator:
 * it is where a model's context length, mmproj pairing and default parameters are
 * recorded next to the file rather than re-guessed on every launch.
 *
 * Three decisions worth stating:
 *
 * - **Validation is the server's.** `validateManifest` is a pure endpoint; this
 *   editor never re-implements the schema, because two implementations of one
 *   schema drift and the user then sees a green tick over an invalid file.
 * - **Save requires a clean validation.** Writing an invalid manifest is worse
 *   than not writing one: the next launch reads it and fails somewhere else.
 * - **Delete asks first.** The manifest is hand-written text with no history.
 */
export function ManifestEditor({ modelPath }: { modelPath: string }) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const [format, setFormat] = useState<'yaml' | 'json'>('yaml');
  const [validation, setValidation] = useState<{
    ok: boolean;
    errors: string[];
    warnings: string[];
    model_key?: string | null;
  } | null>(null);

  const raw = useQuery({
    queryKey: ['models', 'manifest', 'raw', modelPath],
    queryFn: () => modelsApi.manifestRaw(modelPath),
    enabled: Boolean(modelPath),
    retry: false,
  });

  // Seed the editor from the server once the raw read lands.
  useEffect(() => {
    if (raw.data && draft === null) {
      setDraft(raw.data.content);
      setFormat(raw.data.format === 'json' ? 'json' : 'yaml');
    }
  }, [raw.data, draft]);

  useEffect(() => {
    // A different model means a different manifest; stale text here would be
    // saved onto the wrong file.
    setDraft(null);
    setValidation(null);
  }, [modelPath]);

  const validate = useMutation({
    mutationFn: () =>
      modelsApi.validateManifest({ model: modelPath, content: draft ?? '', format }),
    onSuccess: (result) => setValidation(result),
    onError: (error) => {
      setValidation(null);
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    },
  });

  const save = useMutation({
    mutationFn: () =>
      modelsApi.saveManifest({ model: modelPath, content: draft ?? '', format, overwrite: true }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['models'] });
      toast.success(translate('models.manifestSaved'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const remove = useMutation({
    mutationFn: () => modelsApi.deleteManifest(modelPath, format),
    onSuccess: () => {
      setDraft('');
      setValidation(null);
      void client.invalidateQueries({ queryKey: ['models'] });
      toast.success(translate('models.manifestDeleted'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  if (raw.isPending) return <LoadingState />;

  const exists = Boolean(raw.data?.content);

  return (
    <div className="flex flex-col gap-2 rounded-[var(--radius-md)] border border-[var(--border)] p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <FileCode2 size={13} aria-hidden="true" className="text-[var(--text-tertiary)]" />
        <span className="flex-1 text-[length:var(--text-xs)] font-medium">
          {translate('models.manifest')}
        </span>
        {exists ? (
          <Badge tone="neutral">{translate('models.manifestPresent')}</Badge>
        ) : (
          <Badge tone="neutral">{translate('models.manifestAbsent')}</Badge>
        )}
        <select
          value={format}
          onChange={(event) => {
            setFormat(event.target.value === 'json' ? 'json' : 'yaml');
            setValidation(null);
          }}
          aria-label={translate('models.manifestFormat')}
          className="h-[var(--control-h-sm)] rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-1.5 text-[length:var(--text-2xs)]"
        >
          <option value="yaml">YAML</option>
          <option value="json">JSON</option>
        </select>
      </div>

      <textarea
        value={draft ?? ''}
        onChange={(event) => {
          setDraft(event.target.value);
          // A stale green tick over edited text is the failure mode this clears.
          setValidation(null);
        }}
        rows={10}
        spellCheck={false}
        placeholder={translate('models.manifestPlaceholder')}
        aria-label={translate('models.manifest')}
        className="w-full resize-y rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface-sunken)] px-2 py-1.5 font-mono text-[length:var(--text-2xs)] leading-[var(--leading-relaxed)]"
      />

      {validation ? (
        <div
          className={cn(
            'rounded-[var(--radius-sm)] px-2 py-1.5 text-[length:var(--text-2xs)]',
            validation.ok
              ? 'bg-[var(--success-subtle)] text-[var(--success)]'
              : 'bg-[var(--danger-subtle)] text-[var(--danger)]',
          )}
          role={validation.ok ? 'status' : 'alert'}
        >
          {validation.ok ? (
            <span className="flex items-center gap-1">
              <CheckCircle2 size={11} aria-hidden="true" />
              {translate('models.manifestValid')}
              {validation.model_key ? (
                <span className="font-mono text-[var(--text-tertiary)]"> · {validation.model_key}</span>
              ) : null}
            </span>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {validation.errors.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          )}
          {validation.warnings.length ? (
            <ul className="mt-1 flex flex-col gap-0.5 text-[var(--warning)]">
              {validation.warnings.map((message) => (
                <li key={message}>{message}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={draft === null}
          loading={validate.isPending}
          onClick={() => validate.mutate()}
        >
          {translate('models.manifestValidate')}
        </Button>
        <Button
          variant="primary"
          size="sm"
          // Only a validated document may be written: an invalid manifest makes
          // the next launch fail somewhere far from here.
          disabled={!validation?.ok}
          loading={save.isPending}
          onClick={() => save.mutate()}
        >
          {translate('common.save')}
        </Button>
        {exists ? (
          <Button
            variant="dangerGhost"
            size="sm"
            loading={remove.isPending}
            onClick={() => {
              if (window.confirm(translate('models.manifestDeleteConfirm'))) remove.mutate();
            }}
          >
            <Trash2 size={12} aria-hidden="true" />
            {translate('common.delete')}
          </Button>
        ) : null}
      </div>
    </div>
  );
}
