import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { CalendarClock, Play, Plus, Trash2 } from 'lucide-react';
import { instancesApi, scheduledApi, type Schedule, type ScheduledTask } from '@/lib/api';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/Button';
import { Field, Input } from '@/components/ui/Input';
import { SectionHeader } from '@/components/ui/Feedback';

/**
 * Scheduled tasks.
 *
 * Three things the UI has to state rather than leave implicit, because getting them
 * wrong is how a scheduler becomes a surprise:
 *
 * - **The launcher must be running.** These are not Windows scheduled tasks; the loop
 *   lives in this process, so a closed window means nothing fires.
 * - **Runs are plain chat turns, with no tools.** That is why an unattended fire
 *   cannot write a file or run a command — it is a structural limit, not a setting,
 *   and the helper text says so instead of offering a toggle that does not exist.
 * - **Run history is per task.** "It failed twice while I was away" is answered by
 *   the last-error line, not by a generic status.
 */
const WEEKDAYS = [
  'settings.scheduleWeekday0',
  'settings.scheduleWeekday1',
  'settings.scheduleWeekday2',
  'settings.scheduleWeekday3',
  'settings.scheduleWeekday4',
  'settings.scheduleWeekday5',
  'settings.scheduleWeekday6',
] as const;

type Kind = Schedule['kind'];

function formatMoment(value: number | null): string {
  if (!value) return translate('settings.scheduleNever');
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return translate('settings.scheduleNever');
  return date.toLocaleString();
}

export function ScheduledTasksTab() {
  const client = useQueryClient();
  const tasks = useQuery({ queryKey: ['scheduled-tasks'], queryFn: () => scheduledApi.list() });
  const instances = useQuery({
    queryKey: ['instances'],
    queryFn: () => instancesApi.list(),
    // A task can only run against a running instance, so the picker reflects live
    // state rather than a snapshot taken when the sheet opened.
    refetchInterval: 5000,
  });
  const [creating, setCreating] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [form, setForm] = useState({
    name: '',
    prompt: '',
    kind: 'daily' as Kind,
    hours: 2,
    at: '09:00',
    weekday: 0,
    instanceId: '',
  });

  const invalidate = () => void client.invalidateQueries({ queryKey: ['scheduled-tasks'] });

  const create = useMutation({
    mutationFn: () => scheduledApi.create({
      name: form.name.trim(),
      prompt: form.prompt.trim(),
      schedule: buildSchedule(form),
      instance_id: form.instanceId || null,
    }),
    onSuccess: () => {
      setCreating(false);
      setForm({ ...form, name: '', prompt: '' });
      invalidate();
      toast.success(translate('settings.scheduleCreated'));
    },
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      scheduledApi.update(id, { enabled }),
    onSuccess: invalidate,
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const remove = useMutation({
    mutationFn: (id: string) => scheduledApi.remove(id),
    onSuccess: invalidate,
    onError: (error) =>
      toast.error(localizeBackendError(error instanceof Error ? error.message : error)),
  });

  const runNow = useMutation({
    mutationFn: (id: string) => scheduledApi.runNow(id),
    onSuccess: () => {
      invalidate();
      toast.success(translate('settings.scheduleRan'));
    },
    onError: (error) => {
      invalidate();
      toast.error(describeRunError(error));
    },
  });

  const running = useMemo(
    () => (instances.data ?? []).filter((item) => item.status === 'running' && item.serves_chat),
    [instances.data],
  );
  const list: ScheduledTask[] = tasks.data?.tasks ?? [];

  return (
    <div className="flex flex-col gap-3">
      <SectionHeader
        title={translate('settings.scheduleTitle')}
        description={translate('settings.scheduleHelp')}
        action={
          <Button variant="secondary" size="sm" onClick={() => setCreating((value) => !value)}>
            <Plus size={12} aria-hidden="true" />
            {translate('settings.scheduleCreate')}
          </Button>
        }
      />

      {list.length === 0 && !creating ? (
        <p className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
          {translate('settings.scheduleEmpty')}
        </p>
      ) : null}

      {list.length ? (
        <ul className="flex flex-col gap-1">
          {list.map((task) => {
            const open = expanded === task.id;
            return (
              <li
                key={task.id}
                className="rounded-[var(--radius-sm)] border border-[var(--border)] px-2 py-1.5"
              >
                <div className="flex items-start gap-2">
                  <label className="mt-0.5 flex items-center">
                    <input
                      type="checkbox"
                      checked={task.enabled}
                      aria-label={translate('settings.scheduleEnabled')}
                      onChange={(event) =>
                        toggle.mutate({ id: task.id, enabled: event.target.checked })
                      }
                      className="accent-[var(--accent)]"
                    />
                  </label>
                  <div className="min-w-0 flex-1">
                    <p className="flex items-center gap-1.5">
                      <span className="truncate text-[length:var(--text-xs)] font-medium">
                        {task.name}
                      </span>
                      <code className="shrink-0 rounded-[var(--radius-xs)] bg-[var(--surface-sunken)] px-1 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                        {task.schedule_label}
                      </code>
                    </p>
                    <p className="truncate text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                      {task.prompt}
                    </p>
                    <p className="text-[length:var(--text-2xs)] text-[var(--text-quaternary)]">
                      {translate('settings.scheduleNextAt', { when: formatMoment(task.next_run_at) })}
                      {task.instance_id ? '' : ` · ${translate('settings.scheduleAnyInstance')}`}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-0.5">
                    <button
                      type="button"
                      aria-label={translate('settings.scheduleRunNow')}
                      disabled={runNow.isPending}
                      onClick={() => runNow.mutate(task.id)}
                      className="grid size-5 place-items-center rounded-[var(--radius-xs)] text-[var(--text-tertiary)] hover:bg-[var(--hover)] hover:text-[var(--text)]"
                    >
                      <Play size={11} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      aria-label={translate('settings.scheduleShowRuns')}
                      aria-expanded={open}
                      onClick={() => setExpanded(open ? null : task.id)}
                      className="grid size-5 place-items-center rounded-[var(--radius-xs)] text-[var(--text-tertiary)] hover:bg-[var(--hover)] hover:text-[var(--text)]"
                    >
                      <CalendarClock size={11} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      aria-label={translate('common.delete')}
                      onClick={() => {
                        if (
                          window.confirm(
                            translate('settings.scheduleDeleteConfirm', { name: task.name }),
                          )
                        ) {
                          remove.mutate(task.id);
                        }
                      }}
                      className="grid size-5 place-items-center rounded-[var(--radius-xs)] text-[var(--text-tertiary)] hover:bg-[var(--danger-subtle)] hover:text-[var(--danger)]"
                    >
                      <Trash2 size={11} aria-hidden="true" />
                    </button>
                  </div>
                </div>
                {open ? <RunHistory taskId={task.id} /> : null}
              </li>
            );
          })}
        </ul>
      ) : null}

      {creating ? (
        <div className="flex flex-col gap-2 rounded-[var(--radius-md)] border border-[var(--border)] p-2.5">
          <Field label={translate('settings.scheduleName')} htmlFor="schedule-name">
            <Input
              id="schedule-name"
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder={translate('settings.scheduleNamePh')}
            />
          </Field>
          <Field
            label={translate('settings.schedulePrompt')}
            hint={translate('settings.schedulePromptHelp')}
            htmlFor="schedule-prompt"
          >
            <textarea
              id="schedule-prompt"
              value={form.prompt}
              onChange={(event) => setForm({ ...form, prompt: event.target.value })}
              rows={4}
              className="w-full resize-y rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-2 py-1.5 text-[length:var(--text-xs)]"
            />
          </Field>

          <fieldset className="flex flex-col gap-1.5">
            <legend className="text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]">
              {translate('settings.scheduleWhen')}
            </legend>
            <div className="flex gap-1">
              {(['once', 'hourly', 'daily', 'weekly'] as const).map((kind) => (
                <button
                  key={kind}
                  type="button"
                  aria-pressed={form.kind === kind}
                  onClick={() => setForm({ ...form, kind })}
                  className={cn(
                    'h-[var(--control-h-sm)] flex-1 rounded-[var(--radius-sm)] border text-[length:var(--text-xs)]',
                    form.kind === kind
                      ? 'border-[var(--accent)] bg-[var(--accent-subtle)] font-medium text-[var(--accent-text)]'
                      : 'border-[var(--border-control)] text-[var(--text-secondary)] hover:bg-[var(--hover)]',
                  )}
                >
                  {translate(`settings.scheduleKind_${kind}`)}
                </button>
              ))}
            </div>

            {form.kind === 'hourly' ? (
              <Field
                label={translate('settings.scheduleHours')}
                htmlFor="schedule-hours"
              >
                <Input
                  id="schedule-hours"
                  type="number"
                  min={1}
                  max={168}
                  value={form.hours}
                  onChange={(event) => setForm({ ...form, hours: Number(event.target.value) })}
                />
              </Field>
            ) : null}

            {form.kind === 'daily' || form.kind === 'weekly' ? (
              <div className="flex gap-2">
                <Field label={translate('settings.scheduleAt')} htmlFor="schedule-at">
                  <Input
                    id="schedule-at"
                    type="time"
                    value={form.at}
                    onChange={(event) => setForm({ ...form, at: event.target.value })}
                  />
                </Field>
                {form.kind === 'weekly' ? (
                  <Field label={translate('settings.scheduleWeekday')} htmlFor="schedule-weekday">
                    <select
                      id="schedule-weekday"
                      value={form.weekday}
                      onChange={(event) =>
                        setForm({ ...form, weekday: Number(event.target.value) })
                      }
                      className="h-[var(--control-h)] w-full rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-sm)]"
                    >
                      {WEEKDAYS.map((key, index) => (
                        <option key={key} value={index}>
                          {translate(key)}
                        </option>
                      ))}
                    </select>
                  </Field>
                ) : null}
              </div>
            ) : null}

            <Field
              label={translate('settings.scheduleInstance')}
              hint={translate('settings.scheduleInstanceHelp')}
              htmlFor="schedule-instance"
            >
              <select
                id="schedule-instance"
                value={form.instanceId}
                onChange={(event) => setForm({ ...form, instanceId: event.target.value })}
                className="h-[var(--control-h)] w-full rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-sm)]"
              >
                <option value="">{translate('settings.scheduleAnyInstance')}</option>
                {running.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </Field>
          </fieldset>

          <p className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
            {translate('settings.scheduleNoToolsNote')}
          </p>

          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => setCreating(false)}>
              {translate('common.cancel')}
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={!form.name.trim() || !form.prompt.trim()}
              loading={create.isPending}
              onClick={() => create.mutate()}
            >
              {translate('common.save')}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function buildSchedule(form: {
  kind: Kind;
  hours: number;
  at: string;
  weekday: number;
}): Schedule {
  if (form.kind === 'once') return { kind: 'once' };
  if (form.kind === 'hourly') return { kind: 'hourly', hours: form.hours };
  if (form.kind === 'daily') return { kind: 'daily', at: form.at };
  return { kind: 'weekly', at: form.at, weekday: form.weekday };
}

/**
 * A refused run's answer is `{code, message}`; the code is what the UI can word, so
 * the mapped text wins over the raw English message.
 */
function describeRunError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  for (const code of ['no_instance', 'instance_missing', 'instance_stopped', 'empty_reply']) {
    if (raw.includes(code)) return translate(`settings.scheduleError_${code}`);
  }
  return localizeBackendError(raw);
}

function RunHistory({ taskId }: { taskId: string }) {
  const runs = useQuery({
    queryKey: ['scheduled-tasks', taskId, 'runs'],
    queryFn: () => scheduledApi.runs(taskId, 10),
  });
  const items = runs.data?.runs ?? [];

  if (runs.isPending) {
    return (
      <p className="mt-1.5 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
        {translate('settings.scheduleRunsLoading')}
      </p>
    );
  }
  if (!items.length) {
    return (
      <p className="mt-1.5 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
        {translate('settings.scheduleRunsEmpty')}
      </p>
    );
  }

  return (
    <ul className="mt-1.5 flex flex-col gap-0.5 border-t border-[var(--border)] pt-1.5">
      {items.map((run) => (
        <li
          key={run.id}
          className="flex items-baseline gap-2 text-[length:var(--text-2xs)] text-[var(--text-tertiary)]"
        >
          <span
            className={cn(
              'shrink-0 font-medium',
              run.status === 'ok'
                ? 'text-[var(--success)]'
                : run.status === 'failed'
                  ? 'text-[var(--danger)]'
                  : 'text-[var(--text-secondary)]',
            )}
          >
            {translate(`settings.scheduleRun_${run.status}`)}
          </span>
          <span className="shrink-0 tnum">{formatMoment(run.started_at * 1000)}</span>
          {run.error ? <span className="truncate">{run.error}</span> : null}
        </li>
      ))}
    </ul>
  );
}
