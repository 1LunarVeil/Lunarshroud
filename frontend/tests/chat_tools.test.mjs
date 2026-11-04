import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_READ_BYTES,
  READ_ONLY_TOOLS,
  TOOL_DECLARATIONS,
  buildToolResultMessage,
  describeToolExecution,
  isExecutableTool,
  parseToolCallArguments,
  pendingToolCalls,
  resolveToolPath,
  selectToolDeclarations,
} from '../js/chat_tools.mjs';

test('only the three read-only tools are executable', () => {
  for (const name of READ_ONLY_TOOLS) {
    assert.equal(isExecutableTool(name).ok, true, `${name} should be executable`);
  }
  // 写/执行类工具必须被拒绝 —— 这是整个批准机制的地基
  for (const name of ['write_file', 'exec', 'delete_file', 'shell', '']) {
    assert.equal(isExecutableTool(name).ok, false, `${name} must not be executable`);
  }
  assert.equal(isExecutableTool(null).reason, 'not-a-name');
  assert.equal(isExecutableTool('exec').reason, 'not-allowed');
});

test('every declared tool is in the executable whitelist', () => {
  // 声明了却执行不了 → 界面上会出现一个"允许"按钮，点了必失败
  for (const declaration of TOOL_DECLARATIONS) {
    const name = declaration.function.name;
    assert.ok(READ_ONLY_TOOLS.includes(name), `${name} is declared but not executable`);
    assert.equal(declaration.type, 'function');
    assert.equal(declaration.function.parameters.type, 'object');
  }
});

test('resolveToolPath accepts relative paths inside the root', () => {
  const root = 'C:/work/project';
  const inside = resolveToolPath(root, 'src/main.py');
  assert.equal(inside.ok, true);
  assert.match(inside.path, /src\/main\.py$/);
  // 反斜杠会被归一化
  const win = resolveToolPath(root, 'src\\lib\\util.js');
  assert.equal(win.ok, true);
  assert.match(win.path, /src\/lib\/util\.js$/);
});

test('resolveToolPath refuses every escape shape', () => {
  const root = 'C:/work/project';
  const cases = [
    ['../secret.txt', 'outside-root'],
    ['a/../../secret', 'outside-root'],
    ['C:/Windows/win.ini', 'absolute-path'],
    ['D:relative.txt', 'absolute-path'],
    ['/etc/passwd', 'absolute-path'],
    ['\\\\server\\share\\x', 'unc-path'],
    ['', 'empty-path'],
    [null, 'empty-path'],
  ];
  for (const [candidate, reason] of cases) {
    const verdict = resolveToolPath(root, candidate);
    assert.equal(verdict.ok, false, `${candidate} should be refused`);
    assert.equal(verdict.reason, reason, `${candidate} reason`);
  }
  // 没有授权根
  assert.equal(resolveToolPath('', 'a.txt').reason, 'no-root');
  // 根目录本身不算"文件"
  assert.equal(resolveToolPath(root, '.').reason, 'is-root');
});

test('a sibling directory sharing the prefix is outside the root', () => {
  // 纯 startswith 比较会放行 C:/work/project-evil
  const verdict = resolveToolPath('C:/work/project', '../project-evil/x.txt');
  assert.equal(verdict.ok, false);
});

test('parseToolCallArguments tolerates junk without throwing', () => {
  assert.deepEqual(parseToolCallArguments('{"path":"a.txt"}'), { path: 'a.txt' });
  assert.deepEqual(parseToolCallArguments(''), {});
  assert.deepEqual(parseToolCallArguments(null), {});
  assert.deepEqual(parseToolCallArguments({ a: 1 }), { a: 1 });
  // 坏 JSON / 非对象一律 null，让调用方显示"参数不合法"并禁止执行
  for (const bad of ['{oops', '[1,2]', '"text"', '42']) {
    assert.equal(parseToolCallArguments(bad), null, `${bad} should be rejected`);
  }
});

test('describeToolExecution spells out the actual target before approval', () => {
  const root = 'C:/work/project';
  const read = describeToolExecution('read_file', '{"path":"notes.md"}', root);
  assert.equal(read.ok, true);
  assert.match(read.summary, /notes\.md$/);

  const glob = describeToolExecution('file_glob_search', '{"pattern":"*.txt"}', root);
  assert.equal(glob.ok, true);
  assert.match(glob.summary, /\.txt$/);

  const grep = describeToolExecution('grep_search', '{"query":"needle"}', root);
  assert.equal(grep.ok, true);
  assert.match(grep.summary, /needle/);
});

test('describeToolExecution blocks anything it cannot vouch for', () => {
  const root = 'C:/work/project';
  // 不在白名单
  assert.equal(describeToolExecution('exec', '{}', root).reason, 'tool-not-allowed');
  // 坏参数
  assert.equal(describeToolExecution('read_file', '{oops', root).reason, 'bad-arguments');
  // 逃逸
  assert.equal(describeToolExecution('read_file', '{"path":"../../etc"}', root).reason, 'outside-root');
  // 没有根
  assert.equal(describeToolExecution('read_file', '{"path":"a"}', '').reason, 'no-root');
  // 缺必填参数
  assert.equal(describeToolExecution('file_glob_search', '{}', root).reason, 'missing-pattern');
  assert.equal(describeToolExecution('grep_search', '{}', root).reason, 'missing-query');
  // 模式里带 ..
  assert.equal(
    describeToolExecution('file_glob_search', '{"pattern":"../*"}', root).reason,
    'pattern-escape'
  );
});

test('buildToolResultMessage carries the call id and caps the length', () => {
  const call = { id: 'call_1', function: { name: 'read_file', arguments: '{}' } };
  const message = buildToolResultMessage(call, { ok: true, content: 'abc' });
  assert.deepEqual(message, { role: 'tool', tool_call_id: 'call_1', content: 'abc' });
  // 没有 id 就无法对回调用 → 返回 null，让调用方别发
  assert.equal(buildToolResultMessage({ function: {} }, { ok: true, content: 'x' }), null);
  // 超长结果被截到上限
  const long = buildToolResultMessage(call, { ok: true, content: 'x'.repeat(MAX_READ_BYTES + 500) });
  assert.equal(long.content.length, MAX_READ_BYTES);
});

test('pendingToolCalls only lists calls without a recorded result', () => {
  const message = {
    toolCalls: [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
    toolResults: { a: { tool: 'read_file' } },
  };
  assert.deepEqual(pendingToolCalls(message).map((call) => call.id), ['b', 'c']);
  // 全部有结果 → 空
  assert.deepEqual(
    pendingToolCalls({ toolCalls: [{ id: 'a' }], toolResults: { a: {} } }),
    []
  );
  // 没有 toolCalls 时不炸
  assert.deepEqual(pendingToolCalls({}), []);
  assert.deepEqual(pendingToolCalls(null), []);
});

test('selectToolDeclarations offers read-only tools only when the server has none and a folder is authorized', () => {
  // 状态 1：服务端已启用工具 → 不声明第二份（交给 llama-server 自己执行）
  assert.deepEqual(selectToolDeclarations(['read_file', 'grep_search'], 'C:/root'), []);

  // 状态 2：**没有**服务端工具 + 已授权目录 → 声明全部只读工具。
  // 这一条是旧实现失效的那一格：它用 enabled（此时必然为空）去过滤清单，
  // 结果恒为空数组，于是批准条、执行端点、后端执行器全部不可达。
  const offered = selectToolDeclarations([], 'C:/root');
  assert.equal(offered.length, TOOL_DECLARATIONS.length);
  assert.deepEqual(
    offered.map((item) => item.function.name).sort(),
    [...READ_ONLY_TOOLS].sort()
  );

  // 状态 3：没有服务端工具 + 未授权目录 → 不声明（没有任何可执行的目录）
  assert.deepEqual(selectToolDeclarations([], ''), []);
  assert.deepEqual(selectToolDeclarations([], '   '), []);

  // 缺省/脏输入不得抛，按"没有服务端工具"处理
  assert.deepEqual(selectToolDeclarations(undefined, ''), []);
  assert.equal(selectToolDeclarations(null, 'C:/root').length, TOOL_DECLARATIONS.length);
  assert.equal(selectToolDeclarations('read_file', 'C:/root').length, TOOL_DECLARATIONS.length);
});
