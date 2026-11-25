import { Check, Download, FolderPlus, MessageSquarePlus, MoreHorizontal, Pin, Search, Trash2, Upload, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { chatsApi, projectsApi, type Project } from '@/lib/api';
import type { Conversation } from '@/lib/api/types';
import { translate, localizeBackendError } from '@/lib/i18n';
import { cn, formatRelative } from '@/lib/utils';
import { sortConversations, parseConversationImport } from '@/lib/ported/conversations';
import { useChatSearch, useConversations } from '@/features/chat/queries';
import { EmptyState, Skeleton } from '@/components/ui/Feedback';

/**
 * Conversation list, rendered into the sidebar.
 *
 * Search is server-side (`GET /api/chats/search`) because it covers message bodies
 * and attachment text — a client-side filter over titles would silently miss the
 * case people actually search for ("which conversation was that in?").
 *
 * Deleting and pinning mutate the list optimistically in the cache and let the
 * server response settle it; a failed write rolls the list back on the next
 * refetch rather than leaving a phantom row.
 */
export function ChatSidebarList({
  activeId,
  onSelect,
  onNew,
}: {
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  const conversations = useConversations();
  const [query, setQuery] = useState('');
  const search = useChatSearch(query);
  const client = useQueryClient();
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [menuOpen, setMenuOpen] = useState(false);
  const [creatingProject, setCreatingProject] = useState(false);
  const [projectName, setProjectName] = useState('');
  const fileRef = useRef<HTMLInputElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);

  /**
   * Dismiss the list menu on outside click or Escape.
   *
   * It used to close on `onMouseLeave`, which is not a dismissal anyone using a
   * keyboard can perform, and which also closes the menu the moment the pointer
   * travels between the trigger and a menu item. Escape + outside-click is the
   * behaviour every other popup in the app has, and it is what makes the menu
   * reachable without a mouse at all.
   */
  useEffect(() => {
    if (!menuOpen) return;
    const onOutsideClick = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (menuRef.current?.contains(target)) return;
      if (menuButtonRef.current?.contains(target)) return;
      setMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setMenuOpen(false);
        menuButtonRef.current?.focus();
      }
    };
    // `click` rather than `pointerdown`: a `pointerdown` handler that calls
    // setState makes React flush a re-render before the browser dispatches the
    // following `click`, which unmounts the menu item the user just pressed — the
    // item's own onClick then never runs (measured: the menu closed and nothing
    // happened). `click` fires after the item's handler, so dismissal cannot
    // swallow the action.
    document.addEventListener('click', onOutsideClick);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('click', onOutsideClick);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [menuOpen]);
  /**
   * Projects are fetched here rather than at page level: the sidebar is the only
   * consumer, and a project list is tiny. Grouping the conversation list by
   * project is what makes the feature visible — a project nobody can see the
   * effect of is just a row in a database.
   */
  const projects = useQuery({ queryKey: ['projects'], queryFn: projectsApi.list });
  const projectList: Project[] = projects.data?.projects ?? [];

  const ordered = useMemo(
    () => sortConversations<Conversation>(conversations.data ?? []),
    [conversations.data],
  );

  const results = query.trim() ? (search.data?.results ?? []) : [];

  const togglePin = async (conversation: Conversation) => {
    try {
      await chatsApi.update(conversation.id, { pinned: !conversation.pinned });
      await client.invalidateQueries({ queryKey: ['chats', 'list'] });
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  const remove = async (conversation: Conversation) => {
    if (!window.confirm(translate('chat.deleteConfirm', { title: conversation.title || translate('chat.newChat') }))) {
      return;
    }
    try {
      await chatsApi.remove(conversation.id);
      await client.invalidateQueries({ queryKey: ['chats', 'list'] });
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  const commitRename = async (conversation: Conversation) => {
    const next = renameValue.trim();
    setRenaming(null);
    if (!next || next === conversation.title) return;
    try {
      await chatsApi.update(conversation.id, { title: next });
      await client.invalidateQueries({ queryKey: ['chats', 'list'] });
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  /**
   * Export one conversation as a JSON file.
   *
   * `chatsApi.export` returns the server's own document (format tag, version,
   * conversation, messages) — this writes it verbatim instead of re-assembling a
   * client-side shape, so an exported file is always something the importer and
   * the backend agree on.
   */
  const exportConversation = async (conversation: Conversation) => {
    try {
      const payload = await chatsApi.export(conversation.id);
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      const safeTitle = (conversation.title || 'chat').replace(/[\\/:*?"<>|]/g, '_').slice(0, 60);
      anchor.href = url;
      anchor.download = `${safeTitle}.lunarshroud.json`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast.success(translate('chat.exported'));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  /**
   * Import a conversation from a file the user picks.
   *
   * The file is validated **before** the POST: `parseConversationImport` mirrors
   * what `chat_store.import_conversation` accepts, so an unsupported document is
   * rejected with a readable reason instead of a half-written row.
   */
  const importConversation = async (file: File | undefined) => {
    if (!file) return;
    try {
      const text = await file.text();
      let raw: unknown;
      try {
        raw = JSON.parse(text);
      } catch {
        toast.error(translate('chat.importInvalidJson'));
        return;
      }
      // Validate locally first so an unsupported document gets a spoken reason
      // instead of a half-written row. `parseConversationImport` mirrors what
      // `chat_store.import_conversation` accepts.
      const parsed = parseConversationImport(raw);
      if (!parsed.ok) {
        const key = `chat.importReason_${parsed.reason}`;
        const localized = translate(key);
        toast.error(localized === key ? translate('chat.importInvalid') : localized);
        return;
      }
      // POST the **original document**: the backend re-validates and discards
      // ids/timestamps itself. Sending the parsed shape instead would create a
      // second, subtly different contract.
      await chatsApi.import(raw);
      await client.invalidateQueries({ queryKey: ['chats', 'list'] });
      toast.success(translate('chat.imported'));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    } finally {
      if (fileRef.current) fileRef.current.value = '';
    }
  };

  /** Delete every conversation older than `days`, or all but the newest `keep`. */
  const purge = async (body: { older_than_days?: number; keep?: number }, label: string) => {
    if (!window.confirm(translate('chat.purgeConfirm', { scope: label }))) return;
    try {
      const result = await chatsApi.purge(body);
      await client.invalidateQueries({ queryKey: ['chats', 'list'] });
      toast.success(translate('chat.purgeDone', { count: result.deleted }));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  /**
   * Create a project from an inline field, not `window.prompt`.
   *
   * WebView2 in a Tauri window does not implement the blocking `prompt()` (the
   * call returns null immediately), so a `window.prompt` here is dead code in the
   * packaged build — and in the browser suite it blocks the automation. A visible
   * input row works in both, is styleable, and cannot be dismissed into a null that
   * looks like "the user cancelled".
   */
  const createProject = async () => {
    const name = projectName.trim();
    if (!name) return;
    try {
      await projectsApi.create({ name });
      await client.invalidateQueries({ queryKey: ['projects'] });
      setProjectName('');
      setCreatingProject(false);
      toast.success(translate('chat.projectCreated'));
      setMenuOpen(false);
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  /**
   * Move one conversation into a project (or out of every project).
   *
   * `null` is "ungrouped" and is a first-class choice — most conversations have no
   * project, and the menu offers it explicitly rather than making the user create
   * a bucket called "misc".
   */
  const assignProject = async (conversation: Conversation, projectId: string | null) => {
    try {
      await chatsApi.update(conversation.id, { project_id: projectId });
      await client.invalidateQueries({ queryKey: ['chats', 'list'] });
      await client.invalidateQueries({ queryKey: ['projects'] });
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col border-t border-[var(--border)]">
      <div className="flex items-center justify-between px-3 pb-1 pt-2.5">
        <h2 className="text-[length:var(--text-2xs)] font-medium text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
          {translate('chat.conversations')}
        </h2>
        <div className="relative flex items-center gap-0.5">
          {/*
            Import lives in a menu rather than a bare icon: it is a rare,
            destructive-adjacent action (it writes rows), and an always-visible
            upload arrow next to "new chat" reads as "import a message here".
          */}
          <button
            type="button"
            ref={menuButtonRef}
            data-testid="conversation-menu"
            onClick={() => setMenuOpen((value) => !value)}
            aria-label={translate('chat.conversationMenu')}
            aria-expanded={menuOpen}
            aria-haspopup="menu"
            title={translate('chat.conversationMenu')}
            className={cn(
              'grid size-6 place-items-center rounded-[var(--radius-sm)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]',
              'hover:bg-[var(--hover)] hover:text-[var(--text)]',
              'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
            )}
          >
            <MoreHorizontal size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={onNew}
            aria-label={translate('chat.newChat')}
            title={translate('chat.newChat')}
            className={cn(
              'grid size-6 place-items-center rounded-[var(--radius-sm)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]',
              'hover:bg-[var(--hover)] hover:text-[var(--text)]',
              'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
            )}
          >
            <MessageSquarePlus size={15} aria-hidden="true" />
          </button>

          {menuOpen ? (
            <div
              ref={menuRef}
              role="menu"
              className="absolute end-0 top-7 z-[var(--z-popover)] flex w-52 flex-col rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface)] py-1 shadow-[var(--shadow-pop)]"
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  fileRef.current?.click();
                }}
                className="flex items-center gap-2 px-2.5 py-1.5 text-start text-[length:var(--text-xs)] hover:bg-[var(--hover)]"
              >
                <Upload size={12} aria-hidden="true" />
                {translate('chat.importChat')}
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  setCreatingProject(true);
                }}
                className="flex items-center gap-2 px-2.5 py-1.5 text-start text-[length:var(--text-xs)] hover:bg-[var(--hover)]"
              >
                <FolderPlus size={12} aria-hidden="true" />
                {translate('chat.projectCreate')}
              </button>
              <div className="my-1 border-t border-[var(--border)]" />
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  void purge({ older_than_days: 30 }, translate('chat.purgeOlder30'));
                }}
                className="px-2.5 py-1.5 text-start text-[length:var(--text-xs)] hover:bg-[var(--hover)]"
              >
                {translate('chat.purgeOlder30')}
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  void purge({ keep: 20 }, translate('chat.purgeKeep20'));
                }}
                className="px-2.5 py-1.5 text-start text-[length:var(--text-xs)] hover:bg-[var(--hover)]"
              >
                {translate('chat.purgeKeep20')}
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  void purge({ keep: 0 }, translate('chat.purgeAll'));
                }}
                className="px-2.5 py-1.5 text-start text-[length:var(--text-xs)] text-[var(--danger)] hover:bg-[var(--danger-subtle)]"
              >
                {translate('chat.purgeAll')}
              </button>
            </div>
          ) : null}

          <input
            ref={fileRef}
            type="file"
            accept="application/json,.json"
            aria-label={translate('chat.importChat')}
            className="sr-only"
            onChange={(event) => void importConversation(event.target.files?.[0])}
          />
        </div>
      </div>

      {/*
        Inline project creation. It replaces `window.prompt`, which WebView2 in a
        Tauri window does not implement: the packaged app would have created nothing
        and shown no reason. Enter commits, Escape cancels, and the field keeps its
        value if the request fails (the Save-in-place rule from AGENTS §2.20).
      */}
      {creatingProject ? (
        <div className="px-2 pb-1.5">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void createProject();
            }}
            className="flex items-center gap-1"
          >
            <input
              autoFocus
              value={projectName}
              onChange={(event) => setProjectName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  setCreatingProject(false);
                  setProjectName('');
                }
              }}
              aria-label={translate('chat.projectNamePrompt')}
              placeholder={translate('chat.projectNamePrompt')}
              className="h-[var(--control-h-sm)] w-full min-w-0 rounded-[var(--radius-sm)] border border-[var(--border-control)] bg-[var(--surface)] px-2 text-[length:var(--text-xs)] outline-none"
            />
            <button
              type="submit"
              disabled={!projectName.trim()}
              aria-label={translate('common.save')}
              className="grid size-[var(--control-h-sm)] shrink-0 place-items-center rounded-[var(--radius-sm)] text-[var(--accent-text)] disabled:opacity-50"
            >
              <Check size={13} aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={() => {
                setCreatingProject(false);
                setProjectName('');
              }}
              aria-label={translate('common.cancel')}
              className="grid size-[var(--control-h-sm)] shrink-0 place-items-center rounded-[var(--radius-sm)] text-[var(--text-tertiary)]"
            >
              <X size={13} aria-hidden="true" />
            </button>
          </form>
        </div>
      ) : null}

      <div className="px-2 pb-1.5">
        <div className="flex items-center gap-1.5 rounded-[var(--radius-sm)] border border-[var(--border)] bg-[var(--surface)] px-2">
          <Search size={13} aria-hidden="true" className="shrink-0 text-[length:var(--text-xs)] text-[var(--text-tertiary)]" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={translate('chat.searchConversations')}
            aria-label={translate('chat.searchConversations')}
            className="h-7 w-full min-w-0 bg-transparent text-[length:var(--text-xs)] outline-none text-[var(--text-secondary)] placeholder:text-[length:var(--text-xs)] placeholder:text-[var(--text-tertiary)]"
          />
          {query ? (
            <button
              type="button"
              onClick={() => setQuery('')}
              aria-label={translate('common.close')}
              className="grid size-4 place-items-center rounded-full text-[length:var(--text-xs)] text-[var(--text-tertiary)] hover:bg-[var(--hover-strong)]"
            >
              <X size={11} aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {conversations.isPending ? (
          <div className="flex flex-col gap-1 pt-1">
            <Skeleton className="h-7" />
            <Skeleton className="h-7 w-4/5" />
            <Skeleton className="h-7" />
          </div>
        ) : query.trim() ? (
          results.length ? (
            <ul className="flex flex-col gap-[2px]">
              {results.map((hit) => (
                <li key={`${hit.conversation_id}-${hit.match_type}`}>
                  <button
                    type="button"
                    onClick={() => onSelect(hit.conversation_id)}
                    className={cn(
                      'w-full rounded-[var(--radius-sm)] px-2 py-1.5 text-start',
                      'hover:bg-[var(--hover)]',
                      'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
                      hit.conversation_id === activeId && 'bg-[var(--selected)]',
                    )}
                  >
                    <span className="block truncate text-[length:var(--text-xs)] font-medium text-[var(--text)]">
                      {hit.title || translate('chat.newChat')}
                    </span>
                    <span className="block truncate text-[length:var(--text-2xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                      {hit.snippet}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-2 py-3 text-center text-[length:var(--text-2xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
              {translate('command.empty')}
            </p>
          )
        ) : ordered.length ? (
          <ConversationGroups
            conversations={ordered}
            projects={projectList}
            activeId={activeId}
            renaming={renaming}
            renameValue={renameValue}
            onRenameStart={(conversation) => {
              setRenaming(conversation.id);
              setRenameValue(conversation.title ?? '');
            }}
            onRenameChange={setRenameValue}
            onRenameCommit={(conversation) => void commitRename(conversation)}
            onRenameCancel={() => setRenaming(null)}
            onSelect={onSelect}
            onTogglePin={(conversation) => void togglePin(conversation)}
            onExport={(conversation) => void exportConversation(conversation)}
            onRemove={(conversation) => void remove(conversation)}
            onAssign={assignProject}
          />
        ) : (
          <EmptyState
            compact
            title={translate('chat.noConversations')}
            description={translate('chat.emptyHint2')}
            action={
              <button
                type="button"
                onClick={onNew}
                className="text-[length:var(--text-xs)] font-medium text-[var(--accent-text)] hover:underline"
              >
                {translate('chat.newChat')}
              </button>
            }
          />
        )}
      </div>
    </div>
  );
}

/** Exported for the page header's relative-time summary. */
export { formatRelative };

/**
 * Conversations grouped by project.
 *
 * Grouping, not a tree: pinned conversations sit in their own section at the top
 * (they are pinned *across* projects — that is what pinning means), then each
 * project with its members, then the ungrouped remainder. Sections with no members
 * are omitted, because an empty heading is a promise the UI does not keep.
 *
 * A conversation's project is also editable from its row, so moving one between
 * sections does not require opening anything.
 */
function ConversationGroups({
  conversations,
  projects,
  activeId,
  renaming,
  renameValue,
  onRenameStart,
  onRenameChange,
  onRenameCommit,
  onRenameCancel,
  onSelect,
  onTogglePin,
  onExport,
  onRemove,
  onAssign,
}: {
  conversations: Conversation[];
  projects: Project[];
  activeId: string | null;
  renaming: string | null;
  renameValue: string;
  onRenameStart: (conversation: Conversation) => void;
  onRenameChange: (value: string) => void;
  onRenameCommit: (conversation: Conversation) => void;
  onRenameCancel: () => void;
  onSelect: (id: string) => void;
  onTogglePin: (conversation: Conversation) => void;
  onExport: (conversation: Conversation) => void;
  onRemove: (conversation: Conversation) => void;
  onAssign: (conversation: Conversation, projectId: string | null) => Promise<void>;
}) {
  const pinned = conversations.filter((item) => item.pinned);
  const rest = conversations.filter((item) => !item.pinned);
  // Which row has its controls revealed. Only that row renders the project select;
  // see the note at the select for why a permanently-rendered hidden control is a
  // keyboard trap rather than a shortcut.
  const [revealed, setRevealed] = useState<string | null>(null);

  const groups: Array<{ key: string; title: string | null; items: Conversation[] }> = [];
  if (pinned.length) {
    groups.push({ key: '__pinned', title: translate('chat.pinnedGroup'), items: pinned });
  }
  for (const project of projects) {
    const items = rest.filter((item) => item.project_id === project.id);
    if (items.length) {
      groups.push({ key: project.id, title: project.name, items });
    }
  }
  // Anything whose project was deleted (or never set) still has to be reachable.
  const known = new Set(projects.map((item) => item.id));
  const loose = rest.filter((item) => !item.project_id || !known.has(item.project_id));
  const hasNamedGroups = groups.length > pinned.length || pinned.length > 0 || projects.length > 0;
  if (loose.length) {
    // Only label the loose section when there is something to contrast it with.
    groups.push({
      key: '__ungrouped',
      title: hasNamedGroups ? translate('chat.projectUngrouped') : null,
      items: loose,
    });
  }

  const renderRow = (conversation: Conversation) => (
    <li key={conversation.id} className="group/row relative">
      {renaming === conversation.id ? (
        <input
          autoFocus
          value={renameValue}
          onChange={(event) => onRenameChange(event.target.value)}
          onBlur={() => onRenameCommit(conversation)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') onRenameCommit(conversation);
            if (event.key === 'Escape') onRenameCancel();
          }}
          aria-label={translate('chat.renameTitle')}
          className="h-7 w-full rounded-[var(--radius-sm)] border border-[var(--border-focus)] bg-[var(--surface)] px-2 text-[length:var(--text-xs)] outline-none"
        />
      ) : (
        <>
          <button
            type="button"
            data-testid={`conversation-${conversation.id}`}
            onClick={() => onSelect(conversation.id)}
            onDoubleClick={() => onRenameStart(conversation)}
            className={cn(
              'flex h-7 w-full items-center gap-1.5 rounded-[var(--radius-sm)] ps-2 pe-[3.25rem] text-start',
              'text-[length:var(--text-xs)]',
              'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
              conversation.id === activeId
                ? 'bg-[var(--selected)] font-medium text-[var(--text)]'
                : 'text-[var(--text-secondary)] hover:bg-[var(--hover)] hover:text-[var(--text)]',
            )}
            title={conversation.title || translate('chat.newChat')}
          >
            {conversation.pinned ? (
              <Pin size={11} aria-hidden="true" className="shrink-0 text-[var(--accent-text)]" />
            ) : null}
            <span className="truncate">{conversation.title || translate('chat.newChat')}</span>
          </button>
          {projects.length ? (
            /*
              A select rather than a nested menu: "which project is this in" is a
              single value with a short list, and a select both shows the current
              value and changes it in one gesture.

              It exists **only for the row that is hovered or focused**. A
              permanently-rendered `opacity-0` select is still in the accessibility
              tree and in the keyboard tab order — with ten conversations that is ten
              invisible comboboxes, which traps keyboard users and (measured) broke
              the command palette: `getByRole('combobox')` stopped resolving to a
              single element. Revealing on hover *and* focus keeps it keyboard
              reachable without making every row a tab stop.
            */
            revealed === conversation.id ? (
              <select
                autoFocus
                aria-label={translate('chat.projectAssign')}
                value={conversation.project_id ?? ''}
                onChange={(event) => void onAssign(conversation, event.target.value || null)}
                onBlur={() => setRevealed(null)}
                className={cn(
                  'absolute end-[3.4rem] top-1/2 h-[18px] max-w-[4.5rem] -translate-y-1/2 rounded-[var(--radius-xs)]',
                  'border border-[var(--border-control)] bg-[var(--surface)] text-[length:var(--text-2xs)]',
                )}
              >
                <option value="">{translate('chat.projectNone')}</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </select>
            ) : (
              // The project's name is still visible on hover as the control that
              // opens the select; without this the row would have no affordance for
              // "move me into a project".
              <button
                type="button"
                aria-label={translate('chat.projectAssign')}
                onClick={() => setRevealed(conversation.id)}
                className={cn(
                  'absolute end-[3.4rem] top-1/2 grid h-[18px] w-[18px] -translate-y-1/2 place-items-center',
                  'rounded-[var(--radius-xs)] text-[var(--text-tertiary)] hover:bg-[var(--hover-strong)] hover:text-[var(--text)]',
                  'opacity-0 transition-opacity group-hover/row:opacity-100 group-focus-within/row:opacity-100',
                )}
              >
                <FolderPlus size={11} aria-hidden="true" />
              </button>
            )
          ) : null}
          <div
            className={cn(
              'absolute end-1 top-1/2 flex -translate-y-1/2 items-center gap-0.5',
              'opacity-0 transition-opacity group-hover/row:opacity-100 group-focus-within/row:opacity-100',
            )}
          >
            <button
              type="button"
              onClick={() => onTogglePin(conversation)}
              aria-label={translate('chat.pinned')}
              aria-pressed={conversation.pinned}
              className="grid size-[18px] place-items-center rounded-[var(--radius-xs)] text-[var(--text-tertiary)] hover:bg-[var(--hover-strong)] hover:text-[var(--text)]"
            >
              <Pin size={11} aria-hidden="true" />
            </button>
            <button
              type="button"
              data-testid={`conversation-export-${conversation.id}`}
              onClick={() => onExport(conversation)}
              aria-label={translate('chat.export')}
              title={translate('chat.export')}
              className="grid size-[18px] place-items-center rounded-[var(--radius-xs)] text-[var(--text-tertiary)] hover:bg-[var(--hover-strong)] hover:text-[var(--text)]"
            >
              <Download size={11} aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={() => onRemove(conversation)}
              aria-label={translate('common.delete')}
              className="grid size-[18px] place-items-center rounded-[var(--radius-xs)] text-[var(--text-tertiary)] hover:bg-[var(--danger-subtle)] hover:text-[var(--danger)]"
            >
              <Trash2 size={11} aria-hidden="true" />
            </button>
          </div>
        </>
      )}
    </li>
  );

  const flat = groups.length === 1 ? groups[0] : null;
  if (flat && flat.title === null) {
    // No projects at all: adding a heading nobody asked for would be noise.
    return <ul className="flex flex-col gap-[2px]">{flat.items.map(renderRow)}</ul>;
  }

  return (
    <div className="flex flex-col gap-1.5">
      {groups.map((group) => (
        <section key={group.key} data-testid={`conversation-group-${group.key}`}>
          {group.title ? (
            <h3 className="flex items-center gap-1 px-2 pb-0.5 text-[length:var(--text-2xs)] font-medium uppercase tracking-wide text-[var(--text-tertiary)]">
              <span className="truncate">{group.title}</span>
              <span className="shrink-0 tnum">{group.items.length}</span>
            </h3>
          ) : null}
          <ul className="flex flex-col gap-[2px]">{group.items.map(renderRow)}</ul>
        </section>
      ))}
    </div>
  );
}
