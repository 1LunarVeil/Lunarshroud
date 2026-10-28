// Adversarial pass: force real failures and require the UI to admit them.
// A green HTTP call with a wrong-looking screen is exactly what this guards.
//
// Note on the shape of these tests: adding a source auto-schedules indexing,
// so every scenario has to plant the fault *before* the add, or rebuild.
import assert from 'node:assert/strict';
import test, { before, after } from 'node:test';
import path from 'node:path';
import { BASE_URL, SCREENSHOT_DIR, launchPage } from './helpers.mjs';
import { mkdir, writeFile } from 'node:fs/promises';

const FIXTURES = path.resolve(import.meta.dirname, '../../../tmp/browser-fixtures');
// Machine-specific inputs come from the environment so nothing personal is
// committed and the suite cannot pass on only one box.
//   LUNARSHROUD_TEST_EMBEDDING=/abs/path/to/embedding.gguf
//   LUNARSHROUD_TEST_KB=<knowledge base id that must stay untouched>
const EMBEDDING = process.env.LUNARSHROUD_TEST_EMBEDDING || '';
const needsModel = { skip: !EMBEDDING };

let browser;
let page;
let consoleErrors;
const created = [];
let untouched = [];
let basesCache = [];

before(async () => {
  await mkdir(SCREENSHOT_DIR, { recursive: true });
  await mkdir(FIXTURES, { recursive: true });
  await writeFile(path.join(FIXTURES, 'adv-one.md'), '# 规章一\n\n第一条：检索失败必须显式报错。\n', 'utf8');
  // A deliberately large text file: the embedding phase has to last long
  // enough that a service can be killed inside it.
  const paragraphs = [];
  for (let i = 1; i <= 400; i += 1) {
    paragraphs.push(
      `第${i}条 测试条款。本条用于把索引过程拉长到足以在嵌入阶段中途终止服务，` +
        '因此不得少于五百字左右的实际负载。'.repeat(4)
    );
  }
  await writeFile(path.join(FIXTURES, 'adv-big.md'), '# 大文件\n\n' + paragraphs.join('\n\n'), 'utf8');
  ({ browser, page, consoleErrors } = await launchPage());
  await page.goto(BASE_URL, { waitUntil: 'networkidle' });
  await page.waitForSelector('.sidebar-item');
  untouched = (await api('/api/knowledge')).body.bases.map((b) => ({
    id: b.id,
    documents: b.document_count,
    chunks: b.chunk_count,
  }));
});

after(async () => {
  for (const id of created) {
    await page.evaluate((base) => fetch(`/api/knowledge/${base}`, { method: 'DELETE' }), id).catch(() => {});
  }
  // Knowledge services are llama-server children of this dev backend, which has
  // no Job Object of its own: leaving them running would pin gigabytes of VRAM.
  await page
    .evaluate(async () => {
      const list = await (await fetch('/api/instances')).json();
      for (const inst of list) {
        if (!String(inst.name).startsWith('[KB] ')) continue;
        await fetch(`/api/instances/${inst.id}/stop`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ force: true }),
        });
        await fetch(`/api/instances/${inst.id}`, { method: 'DELETE' }).catch(() => {});
      }
    })
    .catch(() => {});
  await browser.close();
});

function api(pathname, options) {
  return page.evaluate(
    async ([p, o]) => {
      const res = await fetch(p, o);
      return { status: res.status, body: await res.json().catch(() => null) };
    },
    [pathname, options]
  );
}

function raw(pathname, options) {
  return page.evaluate(
    async ([p, o]) => {
      const res = await fetch(p, o);
      return { status: res.status, text: await res.text() };
    },
    [pathname, options]
  );
}

async function goKnowledge() {
  await page.click('.sidebar-item[data-page="knowledge"]');
  await page.waitForSelector('.sidebar-item[data-page="knowledge"][aria-current="page"]');
  await page.waitForSelector('.panel-list-item');
  await page.waitForTimeout(400);
}

async function newBase(name, embedding) {
  await page.click('#kb-new');
  await page.waitForSelector('#kb-new-name');
  await page.fill('#kb-new-name', name);
  if (embedding) await page.fill('#kb-new-embedding', embedding);
  await page.locator('#app-modal-overlay .modal-footer button:has-text("创建")').last().click();
  await page.waitForSelector('#app-modal-overlay', { state: 'detached', timeout: 8000 });
  const base = (await api('/api/knowledge')).body.bases.find((b) => b.name === name);
  assert.ok(base, `throwaway base ${name} was not created`);
  created.push(base.id);
  await page.waitForSelector('#kb-summary');
  return base;
}

async function addPathsViaUi(lines) {
  await page.click('[data-action="add-paths"]');
  await page.waitForSelector('#kb-paths-input');
  await page.fill('#kb-paths-input', lines);
  await page.locator('#app-modal-overlay .modal-footer button:has-text("添加")').last().click();
  await page.waitForTimeout(900);
}

async function summaryText() {
  return page.evaluate(() => document.querySelector('#kb-summary')?.innerText || '');
}

// The panel refreshes on its own poll, and a small index can finish before the
// next request, so "wait until it looks like it started" is racy in both
// directions. Wait on the stored row instead: it must move past the moment the
// job was triggered and then sit in a terminal state.
async function waitTerminal(id, { since = 0, timeoutMs = 300000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const base = (await api(`/api/knowledge/${id}`)).body.base;
    const settled = base.status !== 'indexing' && base.status !== 'queued';
    if (settled && Number(base.updated_at) > since) {
      await page.waitForTimeout(1500);
      return base;
    }
    await page.waitForTimeout(500);
  }
  const last = (await api(`/api/knowledge/${id}`)).body.base;
  throw new Error(
    `base ${id} stuck in ${last.status} (progress ${last.progress}, message ${JSON.stringify(last.error)})`
  );
}

function stamp() {
  return Date.now() / 1000;
}

test('a named path that does not exist is refused, and the dialog keeps the user text', needsModel, async () => {
  await goKnowledge();
  const base = await newBase(`zz-adv-missing-${Date.now()}`, EMBEDDING);

  const ghost = path.join(FIXTURES, 'does-not-exist.md');
  const res = await raw(`/api/knowledge/${base.id}/documents`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths: [path.join(FIXTURES, 'adv-one.md'), ghost] }),
  });
  assert.equal(res.status, 400, `a missing named path returned ${res.status}: ${res.text}`);
  assert.match(res.text, /does not exist/, `error text is not actionable: ${res.text}`);
  assert.equal((await api(`/api/knowledge/${base.id}`)).body.documents.length, 0, 'a partial add slipped through');

  await addPathsViaUi(ghost);
  const alerts = await page.locator('#toast-container .toast-error').allTextContents();
  assert.ok(alerts.length > 0, `no error toast for a missing path: ${JSON.stringify(alerts)}`);
  assert.match(alerts.join(' '), /不存在|does not exist/, `toast is not localized: ${alerts.join(' ')}`);
  assert.ok(await page.locator('#app-modal-overlay').count(), 'the add-paths dialog closed even though nothing was added');
  assert.equal(await page.locator('#kb-paths-input').inputValue(), ghost, 'the dialog lost what the user typed');
  await page.locator('#app-modal-overlay .modal-close').last().click();
  await page.waitForTimeout(300);

  await addPathsViaUi(path.join(FIXTURES, 'adv-one.md'));
  await page.waitForFunction(() => document.querySelectorAll('#kb-doc-body tr').length === 1, null, { timeout: 20000 });
});

test('a base with no embedding model reports an actionable error instead of "空闲"', needsModel, async () => {
  await goKnowledge();
  const base = await newBase(`zz-adv-nomodel-${Date.now()}`, '');
  const since = stamp();
  await addPathsViaUi(path.join(FIXTURES, 'adv-one.md'));
  const finalBase = await waitTerminal(base.id, { since });

  const detail = (await api(`/api/knowledge/${base.id}`)).body;
  const summary = await summaryText();
  assert.equal(finalBase.status, 'error', `base reported ${finalBase.status} with no embedding model`);
  assert.match(summary, /embedding model|嵌入|出错/, `summary gives no next step: ${summary.slice(0, 300)}`);
  assert.equal(detail.base.chunk_count, 0, 'chunks exist without an embedding model');
  assert.ok(
    detail.documents.every((d) => d.status !== 'ready' || d.chunk_count > 0),
    `a document claims ready with no chunks: ${JSON.stringify(detail.documents)}`
  );
  await page.screenshot({ path: `${SCREENSHOT_DIR}/adversarial-no-model.png`, fullPage: true });
});

test('an unreachable embedding model surfaces the launch failure', needsModel, async () => {
  await goKnowledge();
  const base = await newBase(`zz-adv-badmodel-${Date.now()}`, EMBEDDING);
  const firstRun = stamp();
  await addPathsViaUi(path.join(FIXTURES, 'adv-one.md'));
  await waitTerminal(base.id, { since: firstRun });

  await page.fill('#kb-field-embedding_model', path.join(FIXTURES, 'not-a-model.gguf'));
  await page.click('[data-action="save-settings"]');
  await page.waitForTimeout(1500);
  assert.match(
    (await api(`/api/knowledge/${base.id}`)).body.base.embedding_model,
    /not-a-model/,
    'the bad model path did not persist'
  );

  const rebuild = stamp();
  await page.click('[data-action="reindex-force"]');
  await page.locator('#app-modal-overlay .modal-footer button:has-text("确定")').last().click();
  // Wait for the rebuild to actually start before waiting for it to stop;
  // the panel still shows the previous run's terminal state right after the click.
  const rebuilt = await waitTerminal(base.id, { since: rebuild });
  const detail = (await api(`/api/knowledge/${base.id}`)).body;
  assert.equal(rebuilt.status, 'error', `base status is ${rebuilt.status}, not error`);
  assert.match(await summaryText(), /embedding service failed|嵌入|出错/, 'the cause is hidden from the user');
  await page.screenshot({ path: `${SCREENSHOT_DIR}/adversarial-badmodel.png`, fullPage: true });
});

test('killing the embedding service mid-index terminates visibly, never hangs', needsModel, async () => {
  await goKnowledge();
  // Force the indexer to start a service we can identify, instead of reusing
  // one a previous run left behind.
  await api('/api/knowledge/services/unload', { method: 'POST' });
  const known = new Set((await api('/api/instances')).body.map((i) => i.id));

  const base = await newBase(`zz-adv-die-${Date.now()}`, EMBEDDING);
  const since = stamp();
  await addPathsViaUi(path.join(FIXTURES, 'adv-big.md'));

  const deadline = Date.now() + 180000;
  let embedding = null;
  while (Date.now() < deadline && !embedding) {
    embedding = (await api('/api/instances')).body.find(
      (i) => !known.has(i.id) && String(i.name).startsWith('[KB] Embedding')
    ) || null;
    if (!embedding) await page.waitForTimeout(500);
  }
  assert.ok(embedding, 'the indexer never started a fresh embedding service through ProcessManager');
  await api(`/api/instances/${embedding.id}/stop`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force: true }),
  });

  // The property under test is honesty, not which branch the race lands on:
  // the run must leave the busy state, and no document may claim success it
  // cannot back with chunks.
  const finalBase = await waitTerminal(base.id, { since });
  const detail = (await api(`/api/knowledge/${base.id}`)).body;
  const summary = await summaryText();
  assert.notEqual(finalBase.status, 'indexing', 'the base stayed "indexing" after its service died');
  assert.notEqual(finalBase.status, 'queued', 'the base stayed "queued" after its service died');
  if (finalBase.status === 'idle') {
    assert.ok(finalBase.chunk_count > 0, 'reported idle with zero chunks after a service death');
    assert.equal(
      finalBase.chunk_count,
      detail.documents.reduce((sum, d) => sum + (d.chunk_count || 0), 0),
      'the base chunk total disagrees with its documents'
    );
  } else {
    assert.equal(finalBase.status, 'error', `unexpected terminal status ${finalBase.status}`);
    assert.match(summary, /出错|错误|failed/, `the summary does not admit the failure: ${summary.slice(0, 300)}`);
  }
  const lying = detail.documents.filter((d) => d.status === 'ready' && d.chunk_count === 0);
  assert.equal(lying.length, 0, 'documents claim ready with zero chunks');

  // "staged" is a deliberate persisted intermediate (extracted, not yet
  // embedded), so the honest test is not what state it sits in but whether the
  // user can finish the job from the UI.
  const retry = stamp();
  await page.click('[data-action="reindex"]');
  const recovered = await waitTerminal(base.id, { since: retry });
  assert.equal(
    recovered.status,
    'idle',
    `a retry after the service death ended as ${recovered.status}: ${JSON.stringify(recovered.error)}`
  );
  assert.ok(recovered.chunk_count > 0, 'the retry indexed nothing');
  const after = (await api(`/api/knowledge/${base.id}`)).body.documents;
  assert.ok(
    after.every((d) => d.status === 'ready' && d.chunk_count > 0),
    `documents did not recover: ${JSON.stringify(after.map((d) => [d.status, d.chunk_count]))}`
  );
  await page.screenshot({ path: `${SCREENSHOT_DIR}/adversarial-killed-service.png`, fullPage: true });
});

test('rerank enabled without a rerank model reports failure, not "no results"', needsModel, async () => {
  await goKnowledge();
  const base = await newBase(`zz-adv-rerank-${Date.now()}`, EMBEDDING);
  const seeded = stamp();
  await addPathsViaUi(path.join(FIXTURES, 'adv-one.md'));
  await waitTerminal(base.id, { since: seeded });

  await api(`/api/knowledge/${base.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rerank_enabled: true, rerank_model: '' }),
  });
  await page.waitForTimeout(400);

  await page.fill('#kb-search-input', '检索失败');
  await page.click('#kb-search-form button[type="submit"]');
  await page.waitForFunction(
    () => !/正在检索|Searching/i.test(document.querySelector('#kb-search-results')?.innerText || ''),
    null,
    { timeout: 240000 }
  );
  const text = await page.locator('#kb-search-results').innerText();
  assert.ok(
    await page.locator('#kb-search-results [role="alert"]').count(),
    `rerank misconfiguration was presented as a normal state: "${text.slice(0, 200)}"`
  );
  assert.doesNotMatch(text, /没有结果|no results/i, 'a configuration failure looked like an empty search');
});

test('every pre-existing knowledge base survived the adversarial run', async () => {
  basesCache = (await api('/api/knowledge')).body.bases;
  for (const snapshot of untouched) {
    const now = basesCache.find((b) => b.id === snapshot.id);
    assert.ok(now, `knowledge base ${snapshot.id} vanished`);
    assert.equal(now.document_count, snapshot.documents, `base ${snapshot.id} document count changed`);
    assert.equal(now.chunk_count, snapshot.chunks, `base ${snapshot.id} chunk count changed`);
    assert.notEqual(now.status, 'indexing', `base ${snapshot.id} is still indexing`);
  }
});

test('adversarial pass leaves no unexpected console errors', async () => {
  // This suite provokes 400s on purpose, and app.js mirrors every toast into
  // the console, so those two classes are expected noise, not defects.
  const unexpected = consoleErrors.filter(
    (line) => !/status of 400 \(Bad Request\)/.test(line) && !/^\[Toast\]/.test(line)
  );
  assert.deepEqual(unexpected, [], `unexpected console errors:\n${unexpected.join('\n')}`);
});
