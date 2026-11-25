import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Plus, Search, Trash2 } from 'lucide-react';
import { promptsApi } from '@/lib/api';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/Button';
import { Field, Input } from '@/components/ui/Input';
import type { Prompt } from '@/lib/api/types';

/**
 * Prompt library dialog.
 *
 * Every piece of this existed before this dialog did: `promptsApi` had full CRUD,
 * `usePromptLibrary` was written and never imported, `ComposerHandle.insertText`
 * was implemented and never called, and the i18n block (`chat.prompts`,
 * `chat.promptSave`, …) was orphaned. The feature was complete except for the one
 * thing a user could see.
 *
 * Two behaviours worth stating because they are choices, not defaults:
 *
 * - **Insert, never replace.** A prompt goes in at the caret, so a saved template
 *   can be combined with typed context instead of wiping it.
 * - **Delete asks first.** These are user-authored texts and there is no undo.
 */
export function PromptLibraryDialog({
  open,
  onClose,
  onInsert,
  draft,
}: {
  open: boolean;
  onClose: () => void;
  onInsert: (text: string) => void;
  /** Current composer text, offered as "save this as a prompt". */
  draft: string;
}) {
  const client = useQueryClient();
  const [query, setQuery] = useState('');
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const prompts = useQuery({ queryKey: ['prompts'], queryFn: promptsApi.list, enabled: open });

  const invalidate = () => void client.invalidateQueries({ queryKey: ['prompts'] });

  const create = useMutation({
    mutationFn: () => promptsApi.create({ title: title.trim(), content: content.trim() }),
    onSuccess: () => {
      setTitle('');
      setContent('');
      invalidate();
      toast.success(translate('chat.promptSavedToast'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => promptsApi.remove(id),
    onSuccess: invalidate,
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const list: Prompt[] = useMemo(() => {
    const all = prompts.data?.prompts ?? [];
    const needle = query.trim().toLowerCase();
    if (!needle) return all;
    return all.filter(
      (prompt) =>
        prompt.title.toLowerCase().includes(needle) ||
        prompt.content.toLowerCase().includes(needle),
    );
  }, [prompts.data, query]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[var(--z-modal)] flex items-center justify-center bg-[var(--overlay-scrim)]"
      role="dialog"
      aria-modal="true"
      aria-label={translate('chat.prompts')}
      data-testid="prompt-library"
      onKeyDown={(event) => {
        if (event.key === 'Escape') onClose();
      }}
    >
      <div className="flex max-h-[min(38rem,calc(100vh-4rem))] w-[min(44rem,calc(100vw-2rem))] flex-col rounded-[var(--radius-xl)] border border-[var(--border)] bg-[var(--surface)] shadow-[var(--shadow-pop)]">
        <header className="flex items-center gap-2 border-b border-[var(--border)] px-4 py-3">
          <h2 className="flex-1 text-[length:var(--text-md)] font-semibold">
            {translate('chat.prompts')}
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label={translate('common.close')}>
            ×
          </Button>
        </header>

        <div className="border-b border-[var(--border)] px-4 py-2">
          <Input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={translate('common.search')}
            leading={<Search size={13} aria-hidden="true" />}
          />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          {prompts.isPending ? (
            <p className="px-2 py-3 text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {translate('settings.loading')}
            </p>
          ) : list.length === 0 ? (
            <p className="px-2 py-3 text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {translate('chat.promptEmpty')}
            </p>
          ) : (
            <ul className="flex flex-col gap-1">
              {list.map((prompt) => (
                <li key={prompt.id}>
                  <div
                    className={cn(
                      'group flex items-start gap-2 rounded-[var(--radius-md)] px-2 py-1.5',
                      'hover:bg-[var(--hover)]',
                    )}
                  >
                    <button
                      type="button"
                      data-testid="prompt-insert"
                      onClick={() => {
                        onInsert(prompt.content);
                        onClose();
                      }}
                      className="min-w-0 flex-1 text-start"
                    >
                      <span className="block truncate text-[length:var(--text-sm)] font-medium">
                        {prompt.title}
                      </span>
                      <span className="mt-0.5 line-clamp-2 block text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                        {prompt.content}
                      </span>
                    </button>
                    <button
                      type="button"
                      aria-label={translate('common.delete')}
                      onClick={() => {
                        if (window.confirm(translate('chat.promptDeleteConfirm', { title: prompt.title }))) {
                          remove.mutate(prompt.id);
                        }
                      }}
                      className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-[var(--radius-sm)] text-[var(--text-tertiary)] opacity-0 group-hover:opacity-100 hover:text-[var(--danger)] focus-visible:opacity-100"
                    >
                      <Trash2 size={13} aria-hidden="true" />
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex flex-col gap-2 border-t border-[var(--border)] px-4 py-3">
          <Field label={translate('chat.prompts')} htmlFor="prompt-title">
            <Input
              id="prompt-title"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder={translate('chat.promptTitlePlaceholder')}
            />
          </Field>
          <Field label={translate('chat.promptInsert')} htmlFor="prompt-content">
            <textarea
              id="prompt-content"
              value={content}
              onChange={(event) => setContent(event.target.value)}
              rows={3}
              placeholder={translate('chat.promptContentPlaceholder')}
              className="w-full resize-y rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2 py-1.5 text-[length:var(--text-xs)]"
            />
          </Field>
          <div className="flex justify-between gap-2">
            <Button
              variant="ghost"
              size="sm"
              disabled={!draft.trim()}
              onClick={() => setContent(draft)}
              title={translate('chat.promptSaveCurrent')}
            >
              {translate('chat.promptSaveCurrent')}
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={!title.trim() || !content.trim()}
              loading={create.isPending}
              onClick={() => create.mutate()}
            >
              <Plus size={13} aria-hidden="true" />
              {translate('common.save')}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
