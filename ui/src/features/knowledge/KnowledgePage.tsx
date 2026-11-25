import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { BookOpen, FolderPlus, List, RefreshCw, Search, Trash2 } from 'lucide-react';
import { knowledgeApi } from '@/lib/api';
import { pickPath } from '@/lib/api/client';
import type { KnowledgeBase, KnowledgeDetail, KnowledgeDocument } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn, formatBytes } from '@/lib/utils';
import { Badge, EmptyState, ErrorState, LoadingState, SectionHeader } from '@/components/ui/Feedback';
import { Button } from '@/components/ui/Button';
import { Field, Input, PathInput } from '@/components/ui/Input';
import { KnowledgeSettings, KnowledgeServices } from '@/features/knowledge/KnowledgeSettings';

/**
 * Knowledge bases.
 *
 * Indexing is asynchronous on the server (`POST /index` schedules work), so this page
 * **polls** the base status while one is running and stops when it settles. The status
 * is read from the API rather than inferred from the UI text: a small index can finish
 * between two polls, and asserting on "the label no longer says indexing" would be both
 * false-positive and false-negative prone (AGENTS §6.10 records that lesson).
 */
export function KnowledgePage() {
  const client = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [newEmbedding, setNewEmbedding] = useState('');
  const [query, setQuery] = useState('');
  const [chunksFor, setChunksFor] = useState<KnowledgeDocument | null>(null);

  const bases = useQuery({ queryKey: ['knowledge'], queryFn: knowledgeApi.list, refetchInterval: 3000 });
  const list: KnowledgeBase[] = bases.data?.bases ?? [];
  const selected = list.find((base) => base.id === selectedId) ?? list[0] ?? null;

  const detail = useQuery<KnowledgeDetail>({
    queryKey: ['knowledge', selected?.id],
    queryFn: () => knowledgeApi.detail(selected?.id as string),
    enabled: Boolean(selected?.id),
    // Poll while indexing so progress advances without a manual refresh.
    refetchInterval: (query) => {
      const status = (query.state.data as KnowledgeDetail | undefined)?.base?.status;
      return status === 'indexing' ? 1500 : false;
    },
  });

  const search = useMutation({
    mutationFn: ({ id, q }: { id: string; q: string }) => knowledgeApi.search(id, q),
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const create = useMutation({
    mutationFn: () => knowledgeApi.create({ name: newName.trim(), embedding_model: newEmbedding.trim() }),
    onSuccess: (base) => {
      setSelectedId(base.id);
      setCreating(false);
      setNewName('');
      setNewEmbedding('');
      void client.invalidateQueries({ queryKey: ['knowledge'] });
      toast.success(translate('knowledge.created'));
    },
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const index = useMutation({
    mutationFn: (id: string) => knowledgeApi.index(id, false),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['knowledge'] });
      toast.success(translate('knowledge.scheduled'));
    },
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const cancel = useMutation({
    mutationFn: (id: string) => knowledgeApi.cancel(id),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['knowledge'] }),
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => knowledgeApi.remove(id),
    onSuccess: () => {
      setSelectedId(null);
      void client.invalidateQueries({ queryKey: ['knowledge'] });
      toast.success(translate('knowledge.deleted'));
    },
    onError: (error) => toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  /** Remove one document and its chunks, keeping the rest of the base intact. */
  const removeDocument = async (baseId: string, documentId: string) => {
    try {
      await knowledgeApi.removeDocument(baseId, documentId);
      void client.invalidateQueries({ queryKey: ['knowledge'] });
      void client.invalidateQueries({ queryKey: ['knowledge', baseId] });
      toast.success(translate('knowledge.documentRemoved'));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  /** Adds absolute paths chosen through the native directory dialog. */
  const addDocuments = async (baseId: string) => {    // One shared helper instead of a hand-rolled `window.__TAURI__` cast. `null`
    // means "cancelled": this used to fall through to `window.prompt` on every
    // cancel, which is a dialog the user did not ask for.
    const path = await pickPath('dir', { prompt: translate('knowledge.addPathsPlaceholder') });
    if (!path) return;
    try {
      await knowledgeApi.addDocuments(baseId, [path]);
      void client.invalidateQueries({ queryKey: ['knowledge'] });
      void client.invalidateQueries({ queryKey: ['knowledge', baseId] });
      toast.success(translate('knowledge.added'));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="border-b border-[var(--border)] px-5 pb-3 pt-4">
        <SectionHeader
          title={translate('nav.knowledge')}
          description={translate('knowledge.subtitle')}
          action={
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" onClick={() => void bases.refetch()} loading={bases.isFetching}>
                <RefreshCw size={13} aria-hidden="true" />
                {translate('common.refresh')}
              </Button>
              <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
                {translate('knowledge.create')}
              </Button>
            </div>
          }
        />
      </div>

      <div className="flex min-h-0 flex-1">
        <ul className="w-[15rem] shrink-0 overflow-y-auto border-e border-[var(--border)] p-1.5">
          {bases.isPending ? (
            <li className="p-3"><LoadingState /></li>
          ) : list.length === 0 ? (
            <li><EmptyState compact icon={<BookOpen size={16} />} title={translate('knowledge.empty')} /></li>
          ) : (
            list.map((base) => (
              <li key={base.id}>
                <button
                  type="button"
                  data-testid="kb-row"
                  aria-current={selected?.id === base.id ? 'true' : undefined}
                  onClick={() => {
                    setSelectedId(base.id);
                    search.reset();
                  }}
                  className={cn(
                    'w-full rounded-[var(--radius-sm)] px-2 py-1.5 text-start',
                    selected?.id === base.id ? 'bg-[var(--selected)]' : 'hover:bg-[var(--hover)]',
                  )}
                >
                  <span className="flex items-center gap-1.5">
                    <span className="min-w-0 flex-1 truncate text-[length:var(--text-sm)] font-medium">
                      {base.name}
                    </span>
                    <StatusChip status={base.status} />
                  </span>
                  <span className="mt-0.5 block text-[length:var(--text-2xs)] tnum text-[var(--text-tertiary)]">
                    {translate('knowledge.documents') + ' ' + base.document_count + ' · ' + translate('knowledge.chunks') + ' ' + base.chunk_count}
                  </span>
                </button>
              </li>
            ))
          )}
        </ul>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {!selected ? (
            <EmptyState
              title={translate('knowledge.empty')}
              description={translate('knowledge.emptySub')}
              action={<Button variant="primary" onClick={() => setCreating(true)}>{translate('knowledge.create')}</Button>}
            />
          ) : detail.isPending ? (
            <LoadingState />
          ) : detail.isError ? (
            <ErrorState message={localizeBackendError(detail.error instanceof Error ? detail.error.message : detail.error)} />
          ) : (
            <div className="flex max-w-[56rem] flex-col gap-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h2 className="text-[length:var(--text-lg)] font-semibold">{selected.name}</h2>
                  <p className="mt-0.5 text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                    {selected.embedding_model || translate('knowledge.embeddingModel')}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => void addDocuments(selected.id)}
                    disabled={selected.status === 'indexing'}
                  >
                    <FolderPlus size={13} aria-hidden="true" />
                    {translate('knowledge.addFolder')}
                  </Button>
                  {selected.status === 'indexing' ? (
                    <Button variant="dangerGhost" size="sm" onClick={() => cancel.mutate(selected.id)} loading={cancel.isPending}>
                      {translate('knowledge.cancelIndex')}
                    </Button>
                  ) : (
                    <Button variant="primary" size="sm" onClick={() => index.mutate(selected.id)} loading={index.isPending}>
                      {translate('knowledge.reindex')}
                    </Button>
                  )}
                  <Button variant="dangerGhost" size="sm" onClick={() => remove.mutate(selected.id)} loading={remove.isPending}>
                    <Trash2 size={13} aria-hidden="true" />
                  </Button>
                </div>
              </div>

              {selected.status === 'indexing' ? (
                <div>
                  <div className="h-1.5 overflow-hidden rounded-[var(--radius-pill)] bg-[var(--surface-sunken)]">
                    <div
                      className="h-full bg-[var(--accent)] transition-[width] duration-300"
                      style={{ width: `${Math.min(100, Math.max(2, selected.progress))}%` }}
                    />
                  </div>
                  <p className="mt-1 text-[length:var(--text-2xs)] tnum text-[var(--text-tertiary)]">
                    {translate('knowledge.status.indexing')} {selected.progress}%
                  </p>
                </div>
              ) : null}

              {selected.error ? (
                <p className="rounded-[var(--radius-sm)] bg-[var(--danger-subtle)] px-2.5 py-1.5 text-[length:var(--text-xs)] text-[var(--danger)]">
                  {selected.error}
                </p>
              ) : null}

              <div className="rounded-[var(--radius-md)] border border-[var(--border)]">
                <table className="w-full text-[length:var(--text-xs)]">
                  <thead>
                    <tr className="border-b border-[var(--border)] text-start text-[var(--text-tertiary)]">
                      <th className="px-3 py-1.5 text-start font-medium">{translate('knowledge.colFile')}</th>
                      <th className="px-3 py-1.5 text-start font-medium">{translate('knowledge.colStatus')}</th>
                      <th className="px-3 py-1.5 text-start font-medium">{translate('knowledge.colChunks')}</th>
                      <th className="px-3 py-1.5 text-end font-medium">
                        <span className="sr-only">{translate('knowledge.colActions')}</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(detail.data?.documents ?? []).length === 0 ? (
                      <tr>
                        <td colSpan={3} className="px-3 py-6 text-center text-[var(--text-tertiary)]">
                          {translate('knowledge.noDocuments')}
                        </td>
                      </tr>
                    ) : (
                      (detail.data?.documents ?? []).map((document) => (
                        <tr key={document.id} className="border-b border-[var(--border-faint,var(--border))] last:border-0">
                          <td className="max-w-[24rem] truncate px-3 py-1.5" title={document.path}>
                            {document.display_name}
                          </td>
                          <td className="px-3 py-1.5">
                            <span
                              className={cn(
                                'rounded-[var(--radius-pill)] px-1.5 py-[1px] text-[length:var(--text-2xs)]',
                                document.status === 'indexed'
                                  ? 'bg-[var(--success-subtle)] text-[var(--success)]'
                                  : document.status === 'error'
                                    ? 'bg-[var(--danger-subtle)] text-[var(--danger)]'
                                    : 'bg-[var(--surface-sunken)] text-[var(--text-secondary)]',
                              )}
                            >
                              {document.status}
                            </span>
                          </td>
                          <td className="px-3 py-1.5 text-end tnum">{document.chunk_count}</td>
                          {/*
                            Two routes had no caller: delete one document, and read its
                            chunks. Without them a single bad file could only be removed
                            by deleting the whole base, and "why did retrieval miss this"
                            had no answer short of opening the SQLite file.
                          */}
                          <td className="whitespace-nowrap px-3 py-1.5 text-end">
                            <div className="flex items-center justify-end gap-0.5">
                              <button
                                type="button"
                                onClick={() => setChunksFor(document)}
                                aria-label={translate('knowledge.viewChunks')}
                                title={translate('knowledge.viewChunks')}
                                className="grid size-6 place-items-center rounded-[var(--radius-sm)] text-[var(--text-tertiary)] hover:bg-[var(--hover)] hover:text-[var(--text)]"
                              >
                                <List size={12} aria-hidden="true" />
                              </button>
                              <button
                                type="button"
                                onClick={() => {
                                  if (!window.confirm(translate('knowledge.removeDocumentConfirm', { name: document.display_name }))) return;
                                  void removeDocument(selected.id, document.id);
                                }}
                                aria-label={translate('knowledge.removeDocument')}
                                title={translate('knowledge.removeDocument')}
                                className="grid size-6 place-items-center rounded-[var(--radius-sm)] text-[var(--text-tertiary)] hover:bg-[var(--danger-subtle)] hover:text-[var(--danger)]"
                              >
                                <Trash2 size={12} aria-hidden="true" />
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>

              <div className="rounded-[var(--radius-md)] border border-[var(--border)] p-3">
                <p className="mb-2 flex items-center gap-1.5 text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
                  <Search size={13} aria-hidden="true" />
                  {translate('knowledge.searchTitle')}
                </p>
                <div className="flex gap-2">
                  <Input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder={translate('knowledge.searchPlaceholder')}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && query.trim()) {
                        search.mutate({ id: selected.id, q: query.trim() });
                      }
                    }}
                  />
                  <Button
                    variant="secondary"
                    onClick={() => search.mutate({ id: selected.id, q: query.trim() })}
                    disabled={!query.trim()}
                    loading={search.isPending}
                  >
                    {translate('common.search')}
                  </Button>
                </div>
                {search.data?.results?.length ? (
                  <ol className="mt-2 flex flex-col gap-1.5">
                    {search.data.results.slice(0, 8).map((hit, index) => (
                      <li key={`${hit.chunk_id ?? index}`} className="text-[length:var(--text-2xs)]">
                        <span className="font-mono text-[var(--accent-text)]">[{index + 1}]</span>{' '}
                        <span className="text-[var(--text-tertiary)]">{hit.document_name ?? ''}</span>
                        <span className="text-[var(--text-quaternary)]"> · {hit.score.toFixed(3)}</span>
                        <p className="mt-0.5 leading-[var(--leading-relaxed)] text-[var(--text-secondary)]">
                          {hit.text}
                        </p>
                      </li>
                    ))}
                  </ol>
                ) : search.isSuccess ? (
                  <p className="mt-2 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                    {translate('knowledge.searchNoResults')}
                  </p>
                ) : null}
              </div>

              {/*
                Settings for this base. Every field here is accepted by
                `PATCH /api/knowledge/{id}` — the backend has supported all of
                them since 1.0, and 2.0 rendered none of them, so embedding /
                rerank / OCR / chunking were API-only and a user could not
                configure the very models the indexer needs.
              */}
              <KnowledgeSettings
                base={detail.data?.base ?? null}
                onSaved={() => void client.invalidateQueries({ queryKey: ['knowledge'] })}
              />

              <KnowledgeServices />
            </div>
          )}
        </div>
      </div>

      {chunksFor && selected ? (
        <ChunksDialog baseId={selected.id} document={chunksFor} onClose={() => setChunksFor(null)} />
      ) : null}

      {creating ? (
        <div className="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center bg-[var(--overlay-scrim)]">
          <div className="w-[min(28rem,calc(100vw-2rem))] rounded-[var(--radius-xl)] border border-[var(--border)] bg-[var(--surface)] p-5 shadow-[var(--shadow-pop)]">
            <h2 className="text-[length:var(--text-lg)] font-semibold">{translate('knowledge.create')}</h2>
            <div className="mt-3 flex flex-col gap-3">
              <Field label={translate('knowledge.newName')} htmlFor="kb-name">
                <Input
                  id="kb-name"
                  autoFocus
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                  placeholder={translate('knowledge.newNamePlaceholder')}
                />
              </Field>
              <Field label={translate('knowledge.embeddingModel')} htmlFor="kb-embedding" hint={translate('knowledge.embeddingModelHelp')}>
                <PathInput
                  id="kb-embedding"
                  kind="file"
                  value={newEmbedding}
                  onValueChange={setNewEmbedding}
                  placeholder={translate('knowledge.embeddingModel')}
                  filters={[{ name: 'GGUF model', extensions: ['gguf'] }]}
                />
              </Field>
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <Button variant="secondary" onClick={() => setCreating(false)}>
                {translate('common.cancel')}
              </Button>
              <Button
                variant="primary"
                disabled={!newName.trim()}
                loading={create.isPending}
                onClick={() => create.mutate()}
              >
                {translate('common.create')}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function StatusChip({ status }: { status: string }) {  const tone =
    status === 'indexing'
      ? 'warning'
      : status === 'ready'
        ? 'success'
        : status === 'error'
          ? 'danger'
          : 'neutral';
  return (
    <Badge tone={tone} dot>
      {translate(`knowledge.status.${status}`)}
    </Badge>
  );
}

export default KnowledgePage;

/**
 * Read one document's indexed chunks.
 *
 * This is the answer to "why did retrieval miss this file": the chunks are what
 * the vector search actually sees. `knowledgeApi.chunks` existed with no caller,
 * so the only way to inspect them was to open `knowledge.db` by hand.
 */
function ChunksDialog({
  baseId,
  document,
  onClose,
}: {
  baseId: string;
  document: KnowledgeDocument;
  onClose: () => void;
}) {
  const chunks = useQuery({
    queryKey: ['knowledge', baseId, 'chunks', document.id],
    queryFn: () => knowledgeApi.chunks(baseId, document.id, 200),
  });

  return (
    <div
      className="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center bg-[var(--overlay-scrim)]"
      role="dialog"
      aria-modal="true"
      aria-label={translate('knowledge.viewChunks')}
      onKeyDown={(event) => {
        if (event.key === 'Escape') onClose();
      }}
    >
      <div className="flex max-h-[min(38rem,calc(100vh-4rem))] w-[min(50rem,calc(100vw-2rem))] flex-col rounded-[var(--radius-xl)] border border-[var(--border)] bg-[var(--surface)] shadow-[var(--shadow-pop)]">
        <header className="flex items-center gap-2 border-b border-[var(--border)] px-4 py-3">
          <h2 className="min-w-0 flex-1 truncate text-[length:var(--text-md)] font-semibold">
            {document.display_name}
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label={translate('common.close')}>
            ×
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
          {chunks.isPending ? (
            <LoadingState />
          ) : (chunks.data?.chunks.length ?? 0) === 0 ? (
            <p className="px-1 py-2 text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {translate('knowledge.noChunks')}
            </p>
          ) : (
            <ol className="flex flex-col gap-1.5">
              {(chunks.data?.chunks ?? []).map((chunk, index) => (
                <li
                  key={chunk.id ?? index}
                  className="rounded-[var(--radius-sm)] border border-[var(--border)] px-2 py-1.5"
                >
                  <p className="mb-0.5 text-[length:var(--text-2xs)] text-[var(--text-quaternary)]">
                    #{index + 1}
                    {chunk.page ? ` · ${translate('knowledge.page')} ${chunk.page}` : ''}
                  </p>
                  <p className="whitespace-pre-wrap text-[length:var(--text-2xs)] leading-[var(--leading-relaxed)] text-[var(--text-secondary)]">
                    {chunk.text}
                  </p>
                </li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </div>
  );
}
export { formatBytes };
