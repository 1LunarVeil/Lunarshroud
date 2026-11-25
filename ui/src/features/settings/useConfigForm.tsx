import { useQueryClient, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { toast } from 'sonner';
import { systemApi } from '@/lib/api';
import { translate, localizeBackendError } from '@/lib/i18n';
import { Button } from '@/components/ui/Button';
import { LoadingState } from '@/components/ui/Feedback';
import type { AppConfig } from '@/lib/api/types';

/**
 * Shared config-form plumbing for every settings tab.
 *
 * **Draft-until-save** is deliberate: each tab edits a local copy and only one
 * button writes. Two lessons are encoded here:
 *
 * - `dirty` is "a draft exists", not a deep comparison against the server. A
 *   normalization difference (null rendered as "", a number arriving as a string)
 *   otherwise makes opening and closing the sheet look like an edit, and the user
 *   gets a "discard changes?" prompt for a change nobody made.
 * - A save that fails **keeps the draft**. Dropping it would silently discard
 *   everything the user typed, which is worse than the failure itself.
 */
export function useConfigForm() {
  const client = useQueryClient();
  const config = useQuery({ queryKey: ['system', 'config'], queryFn: systemApi.getConfig });
  const [draft, setDraft] = useState<AppConfig | null>(null);
  const [saving, setSaving] = useState(false);

  if (config.data && draft === null) {
    // Seeding during render (not in an effect) avoids a frame where the tab
    // renders with a stale draft from a previous open.
    setDraft({ ...config.data });
  }

  const patch = (changes: Partial<AppConfig>) => {
    setDraft((current) => (current ? { ...current, ...changes } : current));
  };

  const save = async () => {
    if (!draft) return false;
    setSaving(true);
    try {
      await systemApi.updateConfig(draft);
      await client.invalidateQueries({ queryKey: ['system', 'config'] });
      await client.invalidateQueries({ queryKey: ['api-server'] });
      await client.invalidateQueries({ queryKey: ['system', 'paths'] });
      toast.success(translate('settings.saved'));
      setDraft(null);
      return true;
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
      return false;
    } finally {
      setSaving(false);
    }
  };

  return { config, draft, patch, setDraft, save, saving, dirty: draft !== null };
}

/** The save button every config tab ends with. */
export function SaveRow({
  onSave,
  saving,
  dirty,
}: {
  onSave: () => void;
  saving: boolean;
  dirty: boolean;
}) {
  return (
    <div className="flex justify-end gap-2 border-t border-[var(--border)] pt-4">
      <Button variant="primary" onClick={onSave} loading={saving} disabled={!dirty}>
        {translate('settings.save')}
      </Button>
    </div>
  );
}

/** Loading gate shared by the config tabs. */
export function ConfigLoading({ pending }: { pending: boolean }) {
  if (!pending) return null;
  return <LoadingState label={translate('settings.loading')} />;
}
