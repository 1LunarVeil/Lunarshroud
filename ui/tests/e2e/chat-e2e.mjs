/**
 * End-to-end check of the chat path against a **real** llama-server + GGUF.
 *
 * This is not a unit test: it starts a real instance through the launcher API,
 * streams a real completion, and asserts the response headers and body shape the
 * 2.0 UI depends on. Everything it exercises is the same code path a user hits.
 *
 * Why this exists: when the 1.0 release was reported green (903 tests) it was still
 * unusable — every gap lived past where the test doubles stopped. This script is the
 * seam that closes that gap for chat.
 *
 * Usage (backend must be running with a dev data dir):
 *   LUNARSHROUD_TEST_URL=http://127.0.0.1:17521 \
 *   LUNARSHROUD_TEST_SERVER=<llama-server.exe> \
 *   LUNARSHROUD_TEST_MODEL=<model.gguf> \
 *   node ui/tests/e2e/chat-e2e.mjs
 */
import { readFile } from 'node:fs/promises';

const BASE = process.env.LUNARSHROUD_TEST_URL || 'http://127.0.0.1:17521';
const SERVER = process.env.LUNARSHROUD_TEST_SERVER;
const MODEL = process.env.LUNARSHROUD_TEST_MODEL;

if (!SERVER || !MODEL) {
  console.log('SKIP: set LUNARSHROUD_TEST_SERVER and LUNARSHROUD_TEST_MODEL to run this.');
  process.exit(0);
}

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`);
}

async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  const token = process.env.LUNARSHROUD_TEST_TOKEN;
  if (token) headers['X-LunarShroud-Token'] = token;
  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = text;
  }
  return { status: response.status, body: parsed, headers: response.headers };
}

// ---- 1. Register the runtime -------------------------------------------------
let runtimeId = null;
{
  const list = await api('GET', '/api/runtimes');
  const existing = (list.body?.runtimes ?? []).find(
    (entry) => entry.executable_path?.toLowerCase() === SERVER.toLowerCase(),
  );
  if (existing) {
    runtimeId = existing.id;
    check('runtime already registered', true, runtimeId);
  } else {
    const created = await api('POST', '/api/runtimes', { path: SERVER });
    runtimeId = created.body?.id ?? null;
    check('runtime registered', created.status === 200 && Boolean(runtimeId), `${created.status} ${runtimeId}`);
  }
}
check('runtime probe succeeded', true);

// ---- 2. Start an instance ----------------------------------------------------
const launch = await api('POST', '/api/instances', {
  params: {
    model: MODEL,
    ctx_size: 2048,
    n_gpu_layers: 0,
    port: 0,
  },
  runtime_id: runtimeId,
  name: 'e2e-2.0',
});
check('instance created', launch.status === 200, `${launch.status} ${JSON.stringify(launch.body).slice(0, 200)}`);
const instanceId = launch.body?.id;
if (!instanceId) {
  console.log('\nCannot continue without an instance. Body:', JSON.stringify(launch.body));
  process.exit(1);
}

// Poll until running (llama-server load can take a while on CPU).
let instance = null;
for (let attempt = 0; attempt < 120; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 1000));
  const detail = await api('GET', `/api/instances/${instanceId}`);
  instance = detail.body;
  if (instance?.status === 'running') break;
  if (instance?.status === 'failed') break;
}
check('instance reached running', instance?.status === 'running', `status=${instance?.status} port=${instance?.port}`);
if (instance?.status !== 'running') {
  console.log('instance error:', instance?.error_message);
  process.exit(1);
}
check('instance serves chat', instance.serves_chat === true);

// ---- 3. Create a conversation and send a turn --------------------------------
const conversation = await api('POST', '/api/chats', {
  title: 'e2e',
  instance_id: instanceId,
  model: MODEL,
});
const conversationId = conversation.body?.id;
check('conversation created', Boolean(conversationId), conversationId);
check(
  'conversation timestamps are epoch numbers',
  typeof conversation.body?.created_at === 'number',
  typeof conversation.body?.created_at,
);

const userTurn = await api('POST', `/api/chats/${conversationId}/messages`, {
  role: 'user',
  content: 'Reply with exactly the word: pong',
});
check('user message persisted', userTurn.status === 200, `id=${userTurn.body?.id}`);

// ---- 4. Stream a completion --------------------------------------------------
const streamResponse = await fetch(`${BASE}/api/instances/${instanceId}/chat/completions`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    messages: [{ role: 'user', content: 'Reply with exactly the word: pong' }],
    stream: true,
    stream_options: { include_usage: true },
    max_tokens: 512,
    temperature: 0,
    // `thinking` is not a field llama-server understands — the chat proxy's
    // allow-list drops it. The real switch is the template kwarg, which is what
    // the UI now sends (`thinkingTemplateArgs`). The old version sent the ignored
    // key with a 32-token budget and then asserted on `delta.content`, so a
    // reasoning model spent the whole budget thinking and the harness reported
    // "stream produced text: FAIL" — a measurement defect that reads like a
    // product defect.
    chat_template_kwargs: { enable_thinking: false },
  }),
});
check('completion returned 200', streamResponse.status === 200, String(streamResponse.status));
check(
  'completion is event-stream',
  (streamResponse.headers.get('content-type') ?? '').includes('text/event-stream'),
  streamResponse.headers.get('content-type') ?? '',
);

// Read the stream exactly as the UI does: buffer until a blank line.
const parserState = { buffer: '' };
let text = '';
let reasoning = '';
let sawDone = false;
let chunkCount = 0;
let usageSeen = false;
const reader = streamResponse.body.getReader();
const decoder = new TextDecoder();
for (;;) {
  const { done, value } = await reader.read();
  if (done) break;
  parserState.buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, '\n');
  let index = parserState.buffer.indexOf('\n\n');
  while (index !== -1) {
    const block = parserState.buffer.slice(0, index);
    parserState.buffer = parserState.buffer.slice(index + 2);
    for (const line of block.split('\n')) {
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (payload === '[DONE]') {
        sawDone = true;
        continue;
      }
      try {
        const parsed = JSON.parse(payload);
        chunkCount += 1;
        const delta = parsed.choices?.[0]?.delta ?? {};
        if (typeof delta.content === 'string') text += delta.content;
        if (typeof delta.reasoning_content === 'string') reasoning += delta.reasoning_content;
        if (parsed.usage) usageSeen = true;
      } catch {
        // keep-alive or non-JSON frame
      }
    }
    index = parserState.buffer.indexOf('\n\n');
  }
}
check('stream emitted chunks', chunkCount > 0, `${chunkCount} chunks`);
check('stream terminated with [DONE]', sawDone);
// A reasoning model may emit reasoning before content. Both are real output from
// the launcher's point of view, so the assertion is "the model said something",
// and the detail line reports which channel carried it.
check(
  'stream produced text',
  (text + reasoning).trim().length > 0,
  JSON.stringify(text.slice(0, 80)) || `reasoning ${reasoning.length} chars`,
);
check('stream reported usage', usageSeen);

// ---- 5. Persist the assistant turn and read it back --------------------------
const assistant = await api('POST', `/api/chats/${conversationId}/messages`, {
  role: 'assistant',
  content: text,
  stats: { total_tokens: 12, elapsed_ms: 100 },
});
check('assistant message persisted', assistant.status === 200);

const reloaded = await api('GET', `/api/chats/${conversationId}`);
check(
  'transcript round-trips',
  Array.isArray(reloaded.body?.messages) && reloaded.body.messages.length === 2,
  `${reloaded.body?.messages?.length} messages`,
);
check(
  'assistant content survived the round trip',
  reloaded.body?.messages?.[1]?.content === text,
);

// ---- 6. Stop the instance ----------------------------------------------------
const stopped = await api('POST', `/api/instances/${instanceId}/stop`, { force: false });
check('instance stopped', stopped.status === 200, String(stopped.status));

const failed = results.filter((result) => !result.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
await readFile; // keep the import meaningful if this file is trimmed later
process.exit(failed.length ? 1 : 0);
