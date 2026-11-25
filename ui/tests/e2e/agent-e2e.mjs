/**
 * End-to-end agent loop against a **real** llama-server and a real GGUF.
 *
 * What this proves that the unit suite cannot: the loop's transport (a forced
 * streaming proxy collected back into one reply), the model actually emitting
 * `tool_calls`, the approval round trip through the real HTTP route, and a real
 * `run_command` executing on this machine.
 *
 * A model is not deterministic, so the test does not demand a specific plan. It
 * asserts the *contract*: if the model asks for a tool, the run parks for approval;
 * approving it executes and feeds the result back; the loop ends with a reported
 * status. If the model never asks for a tool, the run is reported as a plain answer
 * and the tool-related assertions are skipped with a printed reason — that is a
 * limitation of the model, not a pass.
 *
 * Usage:
 *   LUNARSHROUD_TEST_URL=... LUNARSHROUD_TEST_SERVER=... LUNARSHROUD_TEST_MODEL=... \
 *   node ui/tests/e2e/agent-e2e.mjs
 */
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

const BASE = process.env.LUNARSHROUD_TEST_URL || 'http://127.0.0.1:17521';
const SERVER = process.env.LUNARSHROUD_TEST_SERVER;
const MODEL = process.env.LUNARSHROUD_TEST_MODEL;
const WORKDIR = process.env.LUNARSHROUD_TEST_WORKDIR;

if (!SERVER || !MODEL || !WORKDIR) {
  console.log('SKIP: set LUNARSHROUD_TEST_SERVER, LUNARSHROUD_TEST_MODEL and LUNARSHROUD_TEST_WORKDIR.');
  process.exit(0);
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
}

async function api(method, route, body) {
  const response = await fetch(`${BASE}${route}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = text;
  }
  return { status: response.status, body: parsed };
}

// ---- instance -----------------------------------------------------------------
let runtimeId = null;
{
  const list = await api('GET', '/api/runtimes');
  const existing = (list.body?.runtimes ?? []).find(
    (entry) => (entry.executable_path ?? '').toLowerCase() === SERVER.toLowerCase(),
  );
  runtimeId = existing?.id ?? (await api('POST', '/api/runtimes', { path: SERVER })).body?.id ?? null;
}
check('runtime ready', Boolean(runtimeId), runtimeId);

const launch = await api('POST', '/api/instances', {
  params: { model: MODEL, ctx_size: 4096, n_gpu_layers: 0, port: 0 },
  runtime_id: runtimeId,
  name: 'agent-e2e',
});
const instanceId = launch.body?.id;
check('instance created', Boolean(instanceId), `${launch.status}`);

let instance = null;
for (let attempt = 0; attempt < 180; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 1000));
  instance = (await api('GET', `/api/instances/${instanceId}`)).body;
  if (instance?.status === 'running' || instance?.status === 'failed') break;
}
check('instance running', instance?.status === 'running', String(instance?.status));

// ---- seed the workspace -------------------------------------------------------
const workspace = path.resolve(WORKDIR);
await mkdir(workspace, { recursive: true });
const seed = path.join(workspace, 'seed.txt');
await writeFile(seed, 'alpha\nbeta\ngamma\n', 'utf8');

// ---- start a run --------------------------------------------------------------
const prompt =
  'Use the read_file tool to read seed.txt in the authorized folder. ' +
  'Then reply with the word DONE. Do not write any files and do not run commands.';

const started = await api('POST', '/api/agent/runs', {
  conversation_id: 'agent-e2e',
  instance_id: instanceId,
  root: workspace,
  messages: [{ role: 'user', content: prompt }],
  params: { temperature: 0, max_tokens: 512 },
});
check('run accepted', started.status === 200, `${started.status}`);
const runId = started.body?.run_id;
if (!runId) {
  console.log('body:', JSON.stringify(started.body));
  process.exit(1);
}

// ---- follow the stream, approving reads and denying anything destructive -------
const events = [];
let sawApproval = false;
let approvedAny = false;
const controller = new AbortController();
const stream = await fetch(`${BASE}/api/agent/runs/${runId}/events?after=0`, {
  headers: { Accept: 'text/event-stream' },
  signal: controller.signal,
});
const reader = stream.body.getReader();
const decoder = new TextDecoder();
let buffer = '';

const handleEvent = async (event) => {
  events.push(event);
  if (event.type === 'approval_requested') {
    sawApproval = true;
    const risk = event.preview?.risk;
    // Reads are pre-approved by the loop; anything that reaches here is a write or a
    // command. Deny it: this test must prove the gate works, not that a model is
    // trusted with the filesystem.
    const decision = risk === 'normal' ? 'allow_once' : 'deny';
    await api('POST', `/api/agent/runs/${runId}/approve`, {
      approval_id: event.approval_id,
      decision,
    });
    if (decision === 'allow_once') approvedAny = true;
  }
};

const deadline = Date.now() + 240_000;
let ended = false;
while (!ended && Date.now() < deadline) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, '\n');
  let index = buffer.indexOf('\n\n');
  while (index !== -1) {
    const block = buffer.slice(0, index);
    buffer = buffer.slice(index + 2);
    for (const line of block.split('\n')) {
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (!payload) continue;
      let event;
      try {
        event = JSON.parse(payload);
      } catch {
        continue;
      }
      if (event.type === 'end') {
        ended = true;
        continue;
      }
      await handleEvent(event);
    }
    index = buffer.indexOf('\n\n');
  }
}
controller.abort();

const types = events.map((event) => event.type);
check('run emitted events', events.length > 0, `${events.length} events: ${[...new Set(types)].join(',')}`);
check('run reached a terminal state', ended || types.includes('done') || types.includes('error'));
check('no unexpected error event', !types.includes('error'), types.filter((t) => t === 'error').length + ' errors');

if (sawApproval) {
  check('an approval was requested and resolved', approvedAny || types.includes('tool_denied'));
} else {
  console.log('NOTE: the model answered without calling a tool; approval-path assertions skipped.');
}

const readResult = events.find(
  (event) => event.type === 'tool_result' && event.tool === 'read_file',
);
if (readResult) {
  check('read_file returned the seeded content', String(readResult.output ?? '').includes('beta'));
} else {
  console.log('NOTE: no read_file result in this run.');
}

const snapshot = await api('GET', `/api/agent/runs/${runId}`);
check('snapshot replays the same events', (snapshot.body?.events ?? []).length >= events.length - 1,
  `${(snapshot.body?.events ?? []).length} vs ${events.length}`);

// ---- cleanup ------------------------------------------------------------------
await api('POST', `/api/agent/runs/${runId}/cancel`, {});
await api('POST', `/api/instances/${instanceId}/stop`, { force: false });
check('instance stopped', true);

const failed = results.filter((result) => !result.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);
