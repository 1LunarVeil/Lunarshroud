import { describe, expect, it } from 'vitest';
import {
  buildToolResultMessage,
  chatToolDeclarations,
  describeToolExecution,
  isExecutableTool,
  needsApproval,
  parseToolCallArguments,
  pendingToolCalls,
  resolveSearchRoot,
  resolveToolPath,
  selectToolDeclarations,
  TOOL_DECLARATIONS,
} from '@/lib/ported/chatTools';

/**
 * The path sandbox is the security boundary for model-requested tools. Every
 * rejection case below is a real escape shape, not a hypothetical: absolute paths,
 * `..` traversal, drive-relative paths and UNC paths.
 */

const ROOT = 'D:/fixture-root/project';

describe('resolveToolPath', () => {
  it('resolves a plain relative path inside the root', () => {
    expect(resolveToolPath(ROOT, 'notes/a.txt')).toEqual({ ok: true, path: 'D:/fixture-root/project/notes/a.txt' });
  });

  it('rejects absolute POSIX and Windows paths', () => {
    expect(resolveToolPath(ROOT, '/etc/passwd')).toEqual({ ok: false, reason: 'absolute-path' });
    expect(resolveToolPath(ROOT, 'C:\\Windows\\win.ini')).toEqual({ ok: false, reason: 'absolute-path' });
  });

  it('rejects drive-relative paths whose meaning depends on the process CWD', () => {
    expect(resolveToolPath(ROOT, 'D:foo')).toEqual({ ok: false, reason: 'absolute-path' });
  });

  it('rejects UNC paths before the generic absolute check', () => {
    // Order matters: normalizing first would report `absolute-path` and hide the
    // fact that this shape initiates a network request.
    expect(resolveToolPath(ROOT, '\\\\server\\share')).toEqual({ ok: false, reason: 'unc-path' });
    expect(resolveToolPath(ROOT, '//server/share')).toEqual({ ok: false, reason: 'unc-path' });
  });

  it('rejects .. traversal that climbs out of the root', () => {
    expect(resolveToolPath(ROOT, '../../secrets.txt')).toEqual({ ok: false, reason: 'outside-root' });
  });

  it('allows .. that stays inside the root', () => {
    expect(resolveToolPath(ROOT, 'notes/../a.txt')).toEqual({ ok: true, path: 'D:/fixture-root/project/a.txt' });
  });

  it('does not treat a prefix-sharing sibling as inside the root', () => {
    // `project-evil` shares the string prefix but is a different folder. The
    // fixture avoids a `<users>/<name>` shaped path on purpose: the release-mirror
    // privacy scanner treats that shape as leaked local data.
    expect(resolveToolPath(ROOT, '../project-evil/a.txt')).toEqual({ ok: false, reason: 'outside-root' });
  });

  it('rejects the root itself and empty input', () => {
    expect(resolveToolPath(ROOT, '.')).toEqual({ ok: false, reason: 'is-root' });
    expect(resolveToolPath(ROOT, '')).toEqual({ ok: false, reason: 'empty-path' });
    expect(resolveToolPath('', 'a.txt')).toEqual({ ok: false, reason: 'no-root' });
  });
});

describe('resolveSearchRoot', () => {
  it('accepts the root itself, because "search everything" is the common case', () => {
    expect(resolveSearchRoot(ROOT, '.')).toEqual({ ok: true, path: 'D:/fixture-root/project' });
    expect(resolveSearchRoot(ROOT, '')).toEqual({ ok: true, path: 'D:/fixture-root/project' });
  });

  it('still rejects escapes', () => {
    expect(resolveSearchRoot(ROOT, '../x')).toEqual({ ok: false, reason: 'outside-root' });
    expect(resolveSearchRoot(ROOT, '\\\\srv\\s')).toEqual({ ok: false, reason: 'unc-path' });
  });
});

describe('isExecutableTool', () => {
  it('allows the three read-only tools', () => {
    expect(isExecutableTool('read_file').ok).toBe(true);
    expect(isExecutableTool('grep_search').ok).toBe(true);
    expect(isExecutableTool('file_glob_search').ok).toBe(true);
  });

  it('also allows the agent-mode tools, because they have an approval gate', () => {
    // 1.0 could answer "not allowed" for writes/commands because the chat page had
    // no way to run them. 2.1 added them behind a per-call approval (the same gate
    // the Agent workbench uses), so the name check must let them through and the
    // **risk class** decides the approval requirement instead.
    for (const name of ['write_file', 'edit_file', 'run_command', 'web_search']) {
      expect(isExecutableTool(name).ok).toBe(true);
      expect(needsApproval(name)).toBe(true);
    }
    for (const name of ['read_file', 'grep_search', 'file_glob_search']) {
      expect(needsApproval(name)).toBe(false);
    }
  });

  it('rejects names that are not in the tool registry, including shell verbs', () => {
    for (const name of ['exec', 'shell', 'powershell', '', null, 'read_file2']) {
      expect(isExecutableTool(name).ok).toBe(false);
    }
  });

  it('rejects a name with surrounding whitespace that is not in the list', () => {
    expect(isExecutableTool(' read_file ').ok).toBe(true);
    expect(isExecutableTool(' read_file2 ').ok).toBe(false);
  });
});

describe('chatToolDeclarations', () => {
  it('offers read-only tools when only a folder is authorized', () => {
    const names = chatToolDeclarations({ toolRoot: ROOT }).map((tool) => tool.function.name);
    expect(names).toEqual(['read_file', 'file_glob_search', 'grep_search']);
  });

  it('adds the write/command tools only in agent mode', () => {
    const names = chatToolDeclarations({ toolRoot: ROOT, agentMode: true }).map(
      (tool) => tool.function.name,
    );
    expect(names).toContain('write_file');
    expect(names).toContain('edit_file');
    expect(names).toContain('run_command');
  });

  it('offers web search only when it is both enabled and configured', () => {
    const configured = chatToolDeclarations({
      toolRoot: ROOT,
      webSearchEnabled: true,
      webSearchConfigured: true,
    }).map((tool) => tool.function.name);
    expect(configured).toContain('web_search');

    const halfConfigured = chatToolDeclarations({ toolRoot: ROOT, webSearchEnabled: true }).map(
      (tool) => tool.function.name,
    );
    expect(halfConfigured).not.toContain('web_search');

    const disabled = chatToolDeclarations({
      toolRoot: ROOT,
      webSearchEnabled: false,
      webSearchConfigured: true,
    }).map((tool) => tool.function.name);
    expect(disabled).not.toContain('web_search');
  });

  it('offers nothing without an authorized folder', () => {
    expect(chatToolDeclarations({ toolRoot: '', agentMode: true })).toEqual([]);
  });

  it('server-side --tools still wins over every client declaration', () => {
    expect(
      chatToolDeclarations({ declaredTools: ['read_file'], toolRoot: ROOT, agentMode: true }),
    ).toEqual([]);
  });
});

describe('parseToolCallArguments', () => {
  it('accepts an object as-is', () => {
    expect(parseToolCallArguments({ a: 1 })).toEqual({ a: 1 });
  });

  it('parses JSON text', () => {
    expect(parseToolCallArguments('{"path":"a"}')).toEqual({ path: 'a' });
  });

  it('returns null for malformed JSON so execution is blocked', () => {
    expect(parseToolCallArguments('{"path":')).toBeNull();
  });

  it('returns {} for empty input', () => {
    expect(parseToolCallArguments('')).toEqual({});
  });
});

describe('describeToolExecution', () => {
  it('summarizes a resolved read target', () => {
    const plan = describeToolExecution('read_file', '{"path":"a.txt"}', ROOT);
    expect(plan).toMatchObject({ ok: true, tool: 'read_file', path: 'D:/fixture-root/project/a.txt' });
  });

  it('refuses a read outside the root and says why', () => {
    const plan = describeToolExecution('read_file', '{"path":"../../x"}', ROOT);
    expect(plan.ok).toBe(false);
    expect(plan.reason).toBe('outside-root');
  });

  it('refuses an unknown tool before parsing arguments', () => {
    expect(describeToolExecution('rm_rf', '{}', ROOT)).toMatchObject({
      ok: false,
      reason: 'tool-not-allowed',
    });
  });

  it('refuses malformed arguments', () => {
    expect(describeToolExecution('read_file', '{"path":', ROOT)).toMatchObject({
      ok: false,
      reason: 'bad-arguments',
    });
  });

  it('rejects a glob pattern containing ..', () => {
    expect(
      describeToolExecution('file_glob_search', '{"pattern":"../**/*.txt"}', ROOT),
    ).toMatchObject({ ok: false, reason: 'pattern-escape' });
  });

  it('requires a query for grep', () => {
    expect(describeToolExecution('grep_search', '{}', ROOT)).toMatchObject({
      ok: false,
      reason: 'missing-query',
    });
  });

  it('includes the pattern and the search root in the summary', () => {
    const plan = describeToolExecution('file_glob_search', '{"pattern":"*.md"}', ROOT);
    expect(plan.summary).toBe('D:/fixture-root/project :: *.md');
  });
});

describe('buildToolResultMessage', () => {
  it('pairs the result with its call id', () => {
    expect(buildToolResultMessage({ id: 'call-1' }, { content: 'body' })).toEqual({
      role: 'tool',
      tool_call_id: 'call-1',
      content: 'body',
    });
  });

  it('returns null without an id, because the model cannot pair it', () => {
    expect(buildToolResultMessage({ id: '' }, { content: 'x' })).toBeNull();
    expect(buildToolResultMessage(null, { content: 'x' })).toBeNull();
  });
});

describe('pendingToolCalls', () => {
  it('returns calls that have no recorded result', () => {
    const pending = pendingToolCalls({
      toolCalls: [{ id: 'a' }, { id: 'b' }],
      toolResults: { a: { ok: true, content: '' } },
    });
    expect(pending.map((call) => call.id)).toEqual(['b']);
  });

  it('ignores calls without an id', () => {
    expect(pendingToolCalls({ toolCalls: [{}], toolResults: {} })).toEqual([]);
  });
});

describe('selectToolDeclarations', () => {
  it('declares nothing when the instance already has built-in tools', () => {
    // Server-side tools run on the host without approval; declaring a second copy
    // would create two implementations of one capability.
    expect(selectToolDeclarations(['read_file'], ROOT)).toEqual([]);
  });

  it('declares nothing when no folder is authorized', () => {
    expect(selectToolDeclarations([], '')).toEqual([]);
  });

  it('declares all read-only tools when the instance has none and a root is set', () => {
    // The 1.0 bug: filtering by an `enabled` list that was necessarily empty in
    // this branch, so the whole client-side tool path was dead code.
    const declarations = selectToolDeclarations([], ROOT);
    expect(declarations).toHaveLength(TOOL_DECLARATIONS.length);
    expect(declarations.map((entry) => entry.function.name)).toEqual([
      'read_file',
      'file_glob_search',
      'grep_search',
    ]);
  });
});
