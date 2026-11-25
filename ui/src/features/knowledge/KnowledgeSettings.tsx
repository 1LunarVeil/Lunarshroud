import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Settings2, Unplug } from 'lucide-react';
import { knowledgeApi } from '@/lib/api';
import type { KnowledgeBase, KnowledgeDetail } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { Button } from '@/components/ui/Button';
import { Field, Input, PathInput } from '@/components/ui/Input';

/**
 * Per-base retrieval configuration.
 *
 * Fields and their backend names are `backend/api/knowledge.py`'s
 * `_KNOWLEDGE_FIELDS` verbatim. The page used to create a base with a name and an
 * embedding model and then show **no** way to change anything — rerank, OCR,
 * chunking and the score floor were reachable only through the API.
 *
 * Validation mirrors the backend so the user gets the reason before a round trip:
 * rerank needs a model, OCR needs both a main model and an mmproj.
 */
export function KnowledgeSettings({
  base,
  onSaved,
}: {
  base: KnowledgeBase | null;
  onSaved: () => void;
}) {
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<KnowledgeDetail['base'] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const current = draft ?? base;

  const save = useMutation({
    mutationFn: () => {
      if (!current) throw new Error('no base');
      const rerankEnabled = Boolean(current.rerank_enabled);
      if (rerankEnabled && !String(current.rerank_model ?? '').trim()) {
        throw new Error(translate('knowledge.rerankNeedsModel'));
      }
      const ocrModel = String(current.ocr_model ?? '').trim();
      if (ocrModel && !String(current.ocr_mmproj ?? '').trim()) {
        throw new Error(translate('knowledge.ocrNeedsMmproj'));
      }
      return knowledgeApi.update(current.id, {
        embedding_model: current.embedding_model,
        rerank_enabled: rerankEnabled,
        rerank_model: current.rerank_model,
        ocr_model: current.ocr_model,
        ocr_mmproj: current.ocr_mmproj,
        ocr_prompt: current.ocr_prompt,
        chunk_chars: current.chunk_chars,
        chunk_overlap: current.chunk_overlap,
        top_k: current.top_k,
        min_score: current.min_score,
      });
    },
    onSuccess: () => {
      setError(null);
      setDraft(null);
      onSaved();
      void client.invalidateQueries({ queryKey: ['knowledge', current?.id] });
      toast.success(translate('knowledge.settingsSaved'));
    },
    onError: (caught) => {
      const message = localizeBackendError(caught instanceof Error ? caught.message : caught);
      setError(message);
      toast.error(message);
    },
  });

  if (!base) return null;

  const patch = (changes: Partial<KnowledgeBase>) =>
    setDraft((previous) => ({ ...(previous ?? base), ...changes }));

  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--border)]">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-start text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)] hover:bg-[var(--hover)]"
      >
        <Settings2 size={13} aria-hidden="true" />
        {translate('knowledge.settings')}
      </button>

      {open ? (
        <div className="flex flex-col gap-3 border-t border-[var(--border)] p-3">
          <Field
            label={translate('knowledge.embeddingModel')}
            hint={translate('knowledge.embeddingModelHelp')}
            htmlFor="kb-edit-embedding"
          >
            <PathInput
              id="kb-edit-embedding"
              kind="file"
              value={current?.embedding_model ?? ''}
              onValueChange={(next) => patch({ embedding_model: next })}
              filters={[{ name: 'GGUF model', extensions: ['gguf'] }]}
            />
          </Field>

          <label className="flex items-center gap-2 text-[length:var(--text-xs)]">
            <input
              type="checkbox"
              checked={Boolean(current?.rerank_enabled)}
              onChange={(event) => patch({ rerank_enabled: event.target.checked })}
              className="accent-[var(--accent)]"
            />
            {translate('knowledge.rerankEnabled')}
          </label>
          {current?.rerank_enabled ? (
            <Field label={translate('knowledge.rerankModel')} htmlFor="kb-edit-rerank">
              <PathInput
                id="kb-edit-rerank"
                kind="file"
                value={current.rerank_model ?? ''}
                onValueChange={(next) => patch({ rerank_model: next })}
                filters={[{ name: 'GGUF model', extensions: ['gguf'] }]}
              />
            </Field>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={translate('knowledge.ocrModel')} htmlFor="kb-edit-ocr">
              <PathInput
                id="kb-edit-ocr"
                kind="file"
                value={current?.ocr_model ?? ''}
                onValueChange={(next) => patch({ ocr_model: next })}
                filters={[{ name: 'GGUF model', extensions: ['gguf'] }]}
              />
            </Field>
            <Field label={translate('knowledge.ocrMmproj')} htmlFor="kb-edit-mmproj">
              <PathInput
                id="kb-edit-mmproj"
                kind="file"
                value={current?.ocr_mmproj ?? ''}
                onValueChange={(next) => patch({ ocr_mmproj: next })}
                filters={[{ name: 'GGUF model', extensions: ['gguf'] }]}
              />
            </Field>
          </div>

          <Field label={translate('knowledge.ocrPrompt')} htmlFor="kb-edit-ocr-prompt">
            <Input
              id="kb-edit-ocr-prompt"
              value={current?.ocr_prompt ?? ''}
              onChange={(event) => patch({ ocr_prompt: event.target.value })}
            />
          </Field>

          <div className="grid gap-3 sm:grid-cols-2">
            <NumberField
              id="kb-edit-chunk-chars"
              label={translate('knowledge.chunkChars')}
              value={Number(current?.chunk_chars ?? 1200)}
              onChange={(value) => patch({ chunk_chars: value })}
            />
            <NumberField
              id="kb-edit-chunk-overlap"
              label={translate('knowledge.chunkOverlap')}
              value={Number(current?.chunk_overlap ?? 200)}
              onChange={(value) => patch({ chunk_overlap: value })}
            />
            <NumberField
              id="kb-edit-top-k"
              label={translate('knowledge.topK')}
              value={Number(current?.top_k ?? 6)}
              onChange={(value) => patch({ top_k: value })}
            />
            <Field label={translate('knowledge.minScore')} htmlFor="kb-edit-min-score">
              <Input
                id="kb-edit-min-score"
                type="number"
                step={0.05}
                min={0}
                max={1}
                value={Number(current?.min_score ?? 0)}
                onChange={(event) => patch({ min_score: Number(event.target.value) })}
              />
            </Field>
          </div>

          {error ? (
            <p role="alert" className="text-[length:var(--text-xs)] text-[var(--danger)]">
              {error}
            </p>
          ) : null}

          <div className="flex justify-end">
            <Button variant="primary" size="sm" loading={save.isPending} onClick={() => save.mutate()}>
              {translate('knowledge.save')}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function NumberField({
  id,
  label,
  value,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <Field label={label} htmlFor={id}>
      <Input
        id={id}
        type="number"
        value={Number.isFinite(value) ? value : 0}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </Field>
  );
}

/**
 * The embedding / rerank / OCR services the indexer spawns.
 *
 * These are real child processes holding VRAM. Before this panel they were
 * invisible: the reaper unloaded them after ten idle minutes and nothing told the
 * user they had been running. The "unload now" button is the manual counterpart of
 * that timer.
 */
export function KnowledgeServices() {
  const client = useQueryClient();
  const status = useQuery({
    queryKey: ['knowledge', 'services'],
    queryFn: knowledgeApi.servicesStatus,
    refetchInterval: 5000,
  });

  const unload = useMutation({
    mutationFn: () => knowledgeApi.unloadServices(),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['knowledge', 'services'] });
      toast.success(translate('knowledge.servicesUnloaded'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const services = status.data?.services ?? [];

  return (
    <div className="rounded-[var(--radius-md)] border border-[var(--border)] p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
          <Unplug size={13} aria-hidden="true" />
          {translate('knowledge.services')}
        </p>
        {services.length ? (
          <Button
            variant="dangerGhost"
            size="sm"
            loading={unload.isPending}
            onClick={() => unload.mutate()}
          >
            {translate('knowledge.unload')}
          </Button>
        ) : null}
      </div>
      {services.length === 0 ? (
        <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
          {translate('knowledge.servicesIdle')}
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {services.map((service) => (
            <li
              key={service.instance_id ?? service.name ?? service.kind}
              className="flex items-center gap-2 text-[length:var(--text-2xs)]"
            >
              <span className="rounded-[var(--radius-pill)] bg-[var(--surface-sunken)] px-1.5 py-[1px] font-mono">
                {service.kind}
              </span>
              <span className="min-w-0 flex-1 truncate text-[var(--text-secondary)]">
                {service.name ?? service.model ?? ''}
              </span>
              <span className="tnum text-[var(--text-tertiary)]">
                {service.port ? `:${service.port}` : service.status ?? ''}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
