import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Trash2 } from 'lucide-react';
import { memoryApi } from '@/lib/api';
import { translate, localizeBackendError } from '@/lib/i18n';
import { Button } from '@/components/ui/Button';
import { Field } from '@/components/ui/Input';
import { SectionHeader } from '@/components/ui/Feedback';

/**
 * Persistent memory editor.
 *
 * Memory is text **injected into every request**, so the editor is a line list
 * with a visible byte budget rather than a free-text blob: the budget is what
 * keeps a note file from becoming a silent context leak, and users need to see it
 * to act on it. The server enforces the same number; this only shows it.
 *
 * Saving replaces the whole set, which is why the editor holds a draft and the
 * save button is explicit — same rule as the settings sheet: nothing is written
 * until asked for.
 */
export function MemoryEditor() {
  const client = useQueryClient();
  const memory = useQuery({ queryKey: ['memory', 'personal'], queryFn: () => memoryApi.get('personal') });
  const [entries, setEntries] = useState<string[]>([]);
  const [newEntry, setNewEntry] = useState('');
  const [dirty, setDirty] = useState(false);

  // Seed from the server once, and again after a save clears `dirty`.
  useEffect(() => {
    if (!dirty && memory.data) setEntries(memory.data.entries);
  }, [memory.data, dirty]);

  const bytes = useMemo(
    () => entries.reduce((total, entry) => total + new TextEncoder().encode(entry).length, 0),
    [entries],
  );
  const budget = memory.data?.max_bytes ?? 4096;
  const overBudget = bytes > budget;

  const save = useMutation({
    mutationFn: () => memoryApi.replace('personal', entries),
    onSuccess: () => {
      setDirty(false);
      void client.invalidateQueries({ queryKey: ['memory'] });
      toast.success(translate('settings.memorySaved'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const clear = useMutation({
    mutationFn: () => memoryApi.clear('personal'),
    onSuccess: () => {
      setEntries([]);
      setDirty(false);
      void client.invalidateQueries({ queryKey: ['memory'] });
      toast.success(translate('settings.memoryCleared'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const add = () => {
    const text = newEntry.trim();
    if (!text) return;
    setEntries((current) => [...current, text]);
    setNewEntry('');
    setDirty(true);
  };

  return (
    <div className="flex flex-col gap-3">
      <SectionHeader title={translate('settings.memoryTitle')} description={translate('settings.memoryHelp')} />

      {entries.length === 0 ? (
        <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
          {translate('settings.memoryEmpty')}
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {entries.map((entry, index) => (
            <li
              key={`${index}-${entry.slice(0, 24)}`}
              className="group flex items-start gap-2 rounded-[var(--radius-sm)] border border-[var(--border)] px-2 py-1.5"
            >
              <span className="min-w-0 flex-1 text-[length:var(--text-xs)] leading-[var(--leading-relaxed)]">
                {entry}
              </span>
              <button
                type="button"
                aria-label={translate('common.delete')}
                onClick={() => {
                  setEntries((current) => current.filter((_, item) => item !== index));
                  setDirty(true);
                }}
                className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-[var(--radius-xs)] text-[var(--text-tertiary)] hover:bg-[var(--danger-subtle)] hover:text-[var(--danger)]"
              >
                <Trash2 size={11} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}

      <Field label={translate('settings.memoryAdd')} htmlFor="memory-new-entry">
        <div className="flex gap-2">
          <input
            id="memory-new-entry"
            value={newEntry}
            onChange={(event) => setNewEntry(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                add();
              }
            }}
            placeholder={translate('settings.memoryPlaceholder')}
            className="h-[var(--control-h)] min-w-0 flex-1 rounded-[var(--radius-md)] border border-[var(--border-control)] bg-[var(--surface)] px-2.5 text-[length:var(--text-xs)]"
          />
          <Button variant="secondary" size="sm" disabled={!newEntry.trim()} onClick={add}>
            {translate('common.add')}
          </Button>
        </div>
      </Field>

      <div className="flex items-center justify-between gap-2">
        <span
          className={
            overBudget
              ? 'text-[length:var(--text-2xs)] text-[var(--danger)]'
              : 'text-[length:var(--text-2xs)] text-[var(--text-tertiary)]'
          }
        >
          {translate('settings.memoryUsage', { used: bytes, limit: budget })}
        </span>
        <div className="flex gap-2">
          {entries.length > 0 ? (
            <Button
              variant="dangerGhost"
              size="sm"
              loading={clear.isPending}
              onClick={() => {
                if (window.confirm(translate('settings.memoryClearConfirm'))) clear.mutate();
              }}
            >
              {translate('settings.memoryClear')}
            </Button>
          ) : null}
          <Button
            variant="primary"
            size="sm"
            disabled={!dirty || overBudget}
            loading={save.isPending}
            onClick={() => save.mutate()}
          >
            {translate('settings.save')}
          </Button>
        </div>
      </div>
    </div>
  );
}
