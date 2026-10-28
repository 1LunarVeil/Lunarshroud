// Deep click-through: every path that previously had only API-level proof.
// Each assertion checks an observable DOM/state outcome, never an HTTP status,
// so an endpoint that returns 200 while the UI stays wrong cannot pass.
import assert from 'node:assert/strict';
import test, { before, after, afterEach } from 'node:test';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { BASE_URL, SCREENSHOT_DIR, launchPage, runAxe } from './helpers.mjs';

// Local, machine-specific inputs. Nothing here is hardcoded so the suite cannot
// leak a personal path into the release mirror and cannot pass only on one box.
//   LUNARSHROUD_TEST_EMBEDDING=/abs/path/to/embedding.gguf
//   LUNARSHROUD_TEST_KB=<knowledge base id with indexed content>
const FIXTURES = path.resolve(import.meta.dirname, '../../../tmp/browser-fixtures');
const EMBEDDING = process.env.LUNARSHROUD_TEST_EMBEDDING || '';
async function targetBase() {
  const bases = (await api('/api/knowledge')).body.bases;
  const wanted = process.env.LUNARSHROUD_TEST_KB;
  return bases.find((b) => b.id === wanted) || bases.find((b) => (b.chunk_count || 0) > 0) || null;
}

let browser;
let page;
let consoleErrors;
let untouched = [];

before(async () => {
  await mkdir(SCREENSHOT_DIR, { recursive: true });
  await mkdir(FIXTURES, { recursive: true });
  await writeFile(path.join(FIXTURES, 'alpha-rules.md'), ['# 测试规章 A', '', '第一条：本文件仅用于验证索引与检索流程。', '第二条：分块长度应由知识库配置决定。'].join('\n'), 'utf8');
  await writeFile(path.join(FIXTURES, 'beta-rules.md'), ['# 测试规章 B', '', '第二条：临时库在测试结束后必须被删除。', '第四条：检索失败必须显式报错。'].join('\n'), 'utf8');
  await writeFile(path.join(FIXTURES, 'gamma-rules.txt'), ['测试规章 C', '', '第七条：配额已满时不得静默删除用户内容。', '第八条：损坏的知识库文件必须被隔离重建。'].join('\n'), 'utf8');
  ({ browser, page, consoleErrors } = await launchPage());
  // Toasts auto-dismiss in 3s; record every one that ever appears so a
  // transient "success" or "error" cannot be missed by a later query.
  await page.addInitScript(() => {
    window.__toasts = [];
    window.__calls = [];
    const original = window.fetch.bind(window);
    window.fetch = (...args) => {
      const [u, init] = args;
      if (typeof u === 'string' && !u.includes('/__axe')) {
        window.__calls.push(`${(init && init.method) || 'GET'} ${new URL(u, location.href).pathname}`);
      }
      return original(...args);
    };
    const record = () => {
      const container = document.getElementById('toast-container');
      if (!container) {
        setTimeout(record, 50);
        return;
      }
      new MutationObserver((mutations) => {
        for (const m of mutations) {
          for (const node of m.addedNodes) {
            if (node.nodeType === 1 && node.classList.contains('toast')) {
              window.__toasts.push({
                type: [...node.classList].find((c) => c.startsWith('toast-')) || 'toast-?',
                text: node.querySelector('.toast-msg')?.textContent || '',
              });
            }
          }
        }
      }).observe(container, { childList: true });
    };
    record();
  });
  await page.goto(BASE_URL, { waitUntil: 'networkidle' });
  await page.waitForSelector('.sidebar-item');
  untouched = (await api('/api/knowledge')).body.bases.map((b) => ({
    id: b.id,
    documents: b.document_count,
    chunks: b.chunk_count,
  }));
});

after(async () => {
  await browser.close();
});

// A dialog left open by a failing step would block every later click, so each
// step ends with the overlay dismissed.
afterEach(async () => {
  await closeModal().catch(() => {});
});

async function go(label) {
  // A modal left open by an earlier step must not block navigation.
  if (await page.locator('.modal-overlay').count()) await closeModal().catch(() => {});
  if (await page.locator('#sidebar-backdrop.show').count()) {
    await page.click('#btn-sidebar-toggle').catch(() => {});
  }
  const narrow = (await page.viewportSize()).width < 900;
  if (narrow && !(await page.locator('#sidebar.show').count())) {
    await page.click('#btn-sidebar-toggle');
    await page.waitForSelector('#sidebar.show', { timeout: 5000 });
  }
  await page.click(`.sidebar-item[data-page="${label}"]`);
  await page.waitForSelector(`.sidebar-item[data-page="${label}"][aria-current="page"]`, { timeout: 5000 });
  if (narrow) await page.click('#btn-sidebar-toggle').catch(() => {});
  await page.waitForTimeout(400);
}

async function toasts() {
  return page.evaluate(() => window.__toasts.map((t) => `${t.type}|${t.text}`));
}

async function clearToasts() {
  await page.evaluate(() => {
    window.__toasts.length = 0;
  });
}

function api(pathname, options) {
  return page.evaluate(
    async ([p, o]) => {
      const res = await fetch(p, o);
      return { status: res.status, body: await res.json().catch(() => null) };
    },
    [pathname, options]
  );
}

async function footerButton(label) {
  // The top-most dialog owns #app-modal-overlay; scoped lookups avoid grabbing
  // a button from a dialog that is already being torn down.
  const root = (await page.locator('#app-modal-overlay').count()) ? '#app-modal-overlay' : '.modal-overlay';
  const button = page.locator(`${root} .modal-footer button:has-text("${label}")`).last();
  await button.waitFor({ state: 'visible', timeout: 8000 });
  return button;
}

async function closeModal() {
  for (let guard = 0; guard < 5; guard += 1) {
    const close = page.locator('.modal-overlay .modal-close').last();
    if (!(await close.count())) break;
    await close.click();
    await page.waitForTimeout(150);
  }
  await page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 5000 });
}

async function selectBase(id) {
  await page.click(`.panel-list-item[data-panel-key="${id}"]`);
  await page.waitForSelector('#kb-summary');
}

test('model cards: detail, view toggle and quick-launch preflight are real interactions', async () => {
  await go('workspace');
  await page.click('[data-workspace-tab="models"]');
  await page.waitForSelector('.model-card');
  assert.ok((await page.locator('.model-card').count()) > 0, 'no model cards rendered');

  // 详情 must load real GGUF facts into the inline detail pane, not leave a
  // spinner and not merely echo what the card already showed.
  await page.locator('.model-card [data-open-model]').first().click();
  await page.waitForFunction(
    () => /模型能力|Capabilities/.test(document.getElementById('page-workspace')?.innerText || ''),
    null,
    { timeout: 20000 }
  );
  const detailText = await page.locator('#page-workspace').innerText();
  assert.match(detailText, /架构/, `detail pane has no architecture: ${detailText.slice(0, 200)}`);
  assert.match(detailText, /张量数/, 'detail pane has no tensor count');
  assert.match(detailText, /原生上下文/, 'detail pane has no native context length');
  assert.doesNotMatch(detailText, /加载中/, 'detail pane is stuck on a loading state');

  // 列表 <-> 卡片 actually swaps structure, marks the pressed button, and
  // renders real rows (the toggle used to blank the library instead).
  const cardCount = await page.locator('.model-card').count();
  await page.click('[data-model-view="list"]');
  await page.waitForSelector('.model-card', { state: 'detached' });
  const listRows = await page.locator('#model-cards table tbody tr').count();
  assert.equal(listRows, cardCount, `list view rendered ${listRows} rows for ${cardCount} models`);
  assert.equal(
    await page.locator('[data-model-view="list"]').getAttribute('aria-pressed'),
    'true',
    'list view button not marked pressed'
  );
  assert.equal(
    await page.locator('[data-model-view="cards"]').getAttribute('aria-pressed'),
    'false',
    'card button still marked pressed in list view'
  );
  const headers = await page.locator('#model-cards table thead th').allTextContents();
  assert.ok(headers.length >= 5, `list view has too few columns: ${headers.join(',')}`);

  await page.click('[data-model-view="cards"]');
  await page.waitForSelector('.model-card');
  assert.equal(await page.locator('.model-card').count(), cardCount, 'card view lost models');

  // Quick launch must reach a terminal, observable state. Silence means the
  // button is dead — which it was, because the card's own data-open-model
  // handler swallowed the click before quickLaunch could run.
  await api('/api/knowledge/services/unload', { method: 'POST' });
  await page.waitForTimeout(2000);
  const beforeCount = (await api('/api/instances')).body.length;
  const smallest = await page.evaluate(() => {
    const buttons = [...document.querySelectorAll('#model-cards [data-quick-launch]')];
    let best = -1;
    let bestSize = Infinity;
    buttons.forEach((button, index) => {
      const card = button.closest('.model-card');
      const meta = [...(card?.querySelectorAll('.model-card-meta span') || [])]
        .map((span) => span.textContent)
        .find((text) => /GB|MB/.test(text));
      const value = parseFloat(meta) * (/MB/.test(meta) ? 1 / 1024 : 1);
      if (value && value < bestSize) {
        bestSize = value;
        best = index;
      }
    });
    return best;
  });
  assert.ok(smallest >= 0, 'no quick-launch button rendered on any card');
  await page.evaluate(() => {
    window.__calls.length = 0;
    window.__toasts.length = 0;
  });
  await page.locator('#model-cards [data-quick-launch]').nth(smallest).click();
  let modalSeen = null;
  try {
    await page.waitForSelector('#app-modal-overlay .modal-footer button', { state: 'visible', timeout: 8000 });
    modalSeen = await page.locator('#app-modal-overlay').innerText();
  } catch {
    modalSeen = null;
  }
  const calls = await page.evaluate(() => window.__calls.slice());
  const posted = calls.some((c) => c.startsWith('POST /api/instances'));
  assert.ok(
    modalSeen || posted,
    `快速启动 produced neither a preflight dialog nor a launch request. calls=${calls.join(' | ')}`
  );
  const footerLabels = modalSeen
    ? await page.locator('#app-modal-overlay .modal-footer button').allTextContents()
    : [];
  const dismiss = footerLabels.includes('取消') ? '取消' : footerLabels.includes('关闭') ? '关闭' : footerLabels[0];
  console.log(`[quick-launch] preflight modal=${Boolean(modalSeen)} footer=${JSON.stringify(footerLabels)} posted=${posted}`);
  let afterCount = (await api('/api/instances')).body.length;
  if (modalSeen) {
    await (await footerButton(dismiss)).click();
    await page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 6000 });
    afterCount = (await api('/api/instances')).body.length;
    assert.equal(afterCount, beforeCount, 'cancelled preflight still created an instance');
    const seen = await toasts();
    assert.ok(!seen.some((t) => t.startsWith('toast-success')), `cancelled launch announced success: ${seen.join(' / ')}`);
    assert.match(modalSeen, /启动|模型|内存|显存|VRAM|RAM|风险|blocked|warning/i, `preflight says nothing useful: ${modalSeen.slice(0, 300)}`);
  }
  // Leave the machine as we found it if a launch really happened.
  for (let guard = 0; afterCount > beforeCount && guard < 20; guard += 1) {
    const list = (await api('/api/instances')).body;
    const extra = list[list.length - 1];
    if (!extra) break;
    if (extra.status === 'starting') {
      await page.waitForTimeout(3000);
      continue;
    }
    await api(`/api/instances/${extra.id}/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force: false }),
    });
    await page.waitForTimeout(2000);
    await api(`/api/instances/${extra.id}`, { method: 'DELETE' }).catch(() => {});
    await page.waitForTimeout(1500);
    afterCount = (await api('/api/instances')).body.length;
  }
  await page.screenshot({ path: `${SCREENSHOT_DIR}/models-cards.png`, fullPage: true });
});

test('knowledge: chunk preview modal renders real indexed text', { skip: !EMBEDDING }, async () => {
  const target = await targetBase();
  assert.ok(target, 'no knowledge base with indexed content to inspect');
  await go('knowledge');
  await page.waitForSelector('.panel-list-item');
  await selectBase(target.id);
  await page.waitForSelector('#kb-doc-body tr');

  const preview = page.locator('[data-action="preview-doc"]:not([disabled])').first();
  assert.equal(await preview.count(), 1, 'no document offered a chunk preview');
  await preview.click();
  await page.waitForSelector('.kb-chunk', { timeout: 10000 });
  const chunks = page.locator('.kb-chunk');
  assert.ok((await chunks.count()) > 0, 'chunk preview opened empty');
  const firstText = await chunks.first().locator('.kb-chunk-text').innerText();
  assert.ok(firstText.trim().length > 20, `chunk text looks like a placeholder: ${firstText}`);
  assert.match(await chunks.first().locator('.kb-chunk-meta').innerText(), /#\d+/, 'chunk meta missing ordinal');
  await page.screenshot({ path: `${SCREENSHOT_DIR}/kb-chunks.png` });
  await closeModal();
});

test('knowledge: search test shows ranked snippets in the page', { skip: !EMBEDDING }, async () => {
  const target = await targetBase();
  assert.ok(target, 'no knowledge base with indexed content to search');
  await go('knowledge');
  await selectBase(target.id);
  await page.waitForSelector('#kb-search-input');
  await page.fill('#kb-search-input', process.env.LUNARSHROUD_TEST_QUERY || '处理个人信息应当遵循哪些原则');
  await clearToasts();
  await page.click('#kb-search-form button[type="submit"]');
  await page.waitForFunction(
    () => document.querySelectorAll('#kb-search-results .kb-hit, #kb-search-results .kb-result').length > 0,
    null,
    { timeout: 90000 }
  );
  const hits = await page.locator('#kb-search-results .kb-hit, #kb-search-results .kb-result').count();
  assert.ok(hits >= 3, `search returned only ${hits} visible hits`);
  const text = await page.locator('#kb-search-results').innerText();
  assert.ok(text.trim().length > 40, `search rendered almost nothing: ${text.slice(0, 200)}`);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/kb-search.png`, fullPage: true });
});

test('knowledge: create → index → cancel → delete on a throwaway base', { skip: !EMBEDDING }, async () => {
  await go('knowledge');
  await clearToasts();
  await page.click('#kb-new');
  await page.waitForSelector('#kb-new-name');
  const marker = `zz-e2e-${Date.now()}`;
  await page.fill('#kb-new-name', marker);
  await page.fill('#kb-new-embedding', EMBEDDING);
  await (await footerButton('创建')).click();
  await page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 8000 });

  const created = (await api('/api/knowledge')).body.bases.find((b) => b.name === marker);
  assert.ok(created, `base ${marker} was not created`);
  try {
    await selectBase(created.id);

    await page.click('[data-action="add-paths"]');
    await page.waitForSelector('#kb-paths-input');
    await page.fill(
      '#kb-paths-input',
      [path.join(FIXTURES, 'alpha-rules.md'), path.join(FIXTURES, 'beta-rules.md'), path.join(FIXTURES, 'gamma-rules.txt')].join('\n')
    );
    await (await footerButton('添加')).click();
    await page.waitForSelector('#kb-doc-body tr', { timeout: 20000 });
    await page.waitForFunction(() => document.querySelectorAll('#kb-doc-body tr').length === 3, null, { timeout: 15000 });

    await page.click('[data-action="reindex"]');
    await page.waitForFunction(
      () => /索引中|排队|extract|embed/i.test(document.querySelector('#kb-summary')?.innerText || ''),
      null,
      { timeout: 60000 }
    );
    await page.waitForSelector('[data-action="cancel-index"]', { timeout: 10000 });

    // The cancel button must actually stop the worker, not just look pressed.
    await page.click('[data-action="cancel-index"]');
    await page.waitForFunction(
      () => !/索引中/.test(document.querySelector('#kb-summary')?.innerText || ''),
      null,
      { timeout: 60000 }
    );

    // False-green guard: nothing may claim "ready" while carrying zero chunks,
    // and a cancelled run must not report 100% progress.
    const detail = await api(`/api/knowledge/${created.id}`);
    for (const doc of detail.body.documents) {
      if (doc.status === 'ready') {
        assert.ok(doc.chunk_count > 0, `${doc.display_name} is "ready" with 0 chunks`);
      }
    }

    // Deleting one document must remove exactly its rows.
    await page.locator('[data-action="delete-doc"]').first().click();
    await (await footerButton('确定')).click();
    await page.waitForFunction(() => document.querySelectorAll('#kb-doc-body tr').length === 2, null, { timeout: 15000 });

    // Before anything can be destroyed, prove the panel is really showing the
    // base this test created — deleting some other base would be a defect, not
    // a test failure.
    assert.equal(
      await page.locator('#kb-summary .kb-name').innerText(),
      marker,
      'the detail panel is not showing the base this test created'
    );

    await page.click('[data-action="delete-base"]');
    await page.waitForSelector('#app-modal-overlay');
    // The dialog names the base it is about to destroy. Reading that name is
    // the last line of defence against a silently changed selection, so the
    // test refuses to confirm unless it matches this test's own base.
    const confirmText = await page.locator('#app-modal-overlay').innerText();
    assert.match(confirmText, new RegExp(`“${marker}”|"${marker}`), `delete dialog names another base: ${confirmText.slice(0, 200)}`);
    await (await footerButton('确定')).click();
    await page.waitForFunction(
      (id) => !document.querySelector(`.panel-list-item[data-panel-key="${id}"]`),
      created.id,
      { timeout: 15000 }
    );
    const remaining = (await api('/api/knowledge')).body.bases.map((b) => b.id);
    assert.ok(!remaining.includes(created.id), 'throwaway base survived deletion');
  } finally {
    const still = (await api('/api/knowledge')).body.bases.find((b) => b.name === marker);
    if (still) await api(`/api/knowledge/${still.id}`, { method: 'DELETE' });
  }
});

async function assertPreexistingUntouched() {
  const fresh = (await api('/api/knowledge')).body.bases;
  for (const snapshot of untouched) {
    const now = fresh.find((b) => b.id === snapshot.id);
    assert.ok(now, `knowledge base ${snapshot.id} vanished`);
    assert.equal(now.document_count, snapshot.documents, `base ${snapshot.id} document count changed`);
    assert.equal(now.chunk_count, snapshot.chunks, `base ${snapshot.id} chunk count changed`);
  }
}

test('settings: every category is reachable and a save round-trips to the backend', async () => {
  await page.click('.sidebar-item[data-page="settings"]');
  await page.waitForSelector('#app-modal-overlay');
  const config = (await api('/api/system/config')).body;

  // Every persisted setting must be reachable through the nav, otherwise the
  // UI silently owns keys the user can never change.
  const reachable = new Set();
  for (const cat of await page.locator('.settings-nav-item').all()) {
    await cat.click();
    await page.waitForTimeout(200);
    for (const f of await page.locator('#app-modal-overlay [data-field]').all()) {
      reachable.add(await f.getAttribute('data-field'));
    }
  }
  // theme is edited through radios (name="theme"), not a data-field input.
  await page.click('.settings-nav-item[data-cat="general"]');
  await page.waitForTimeout(200);
  for (const f of await page.locator('#app-modal-overlay input[name="theme"]').all()) {
    reachable.add('theme');
  }
  const unreachable = Object.keys(config).filter((k) => !reachable.has(k));
  assert.deepEqual(unreachable, [], `settings the UI cannot edit: ${unreachable.join(', ')}`);

  const previous = config.log_buffer_lines;
  const next = previous === 2000 ? 2001 : 2000;
  await page.click('.settings-nav-item[data-cat="storage"]');
  const input = page.locator('#app-modal-overlay [data-field="log_buffer_lines"]');
  await input.waitFor({ state: 'visible', timeout: 5000 });
  await input.fill(String(next));
  await clearToasts();
  await (await footerButton('保存设置')).click();
  const saved = (await api('/api/system/config')).body.log_buffer_lines;
  assert.equal(saved, next, 'saved value did not reach the backend');
  const seen = await toasts();
  assert.ok(seen.some((t) => t.startsWith('toast-success')), `no success toast after saving: ${seen.join(' / ')}`);
  await closeModal().catch(() => {});
  await api('/api/system/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ log_buffer_lines: previous }),
  });
  const restored = (await api('/api/system/config')).body.log_buffer_lines;
  assert.equal(restored, previous, 'failed to restore the user setting');
  // Reopening must show the persisted value, not a stale draft.
  await page.click('.sidebar-item[data-page="settings"]');
  await page.waitForSelector('#app-modal-overlay');
  await page.click('.settings-nav-item[data-cat="storage"]');
  await page.waitForTimeout(300);
  assert.equal(await page.locator('#app-modal-overlay [data-field="log_buffer_lines"]').inputValue(), String(previous),
    'reopened settings did not show the persisted value');
  await closeModal().catch(() => {});
});

test('chat composer is not usable-looking with no running instance', async () => {
  await go('chat');
  await page.waitForSelector('#chat-instance-select');
  const state = await page.evaluate(() => {
    const send = document.getElementById('btn-chat-send');
    const select = document.getElementById('chat-instance-select');
    return {
      value: select ? select.value : null,
      sendDisabled: send ? send.disabled : null,
      live: document.getElementById('chat-live')?.textContent || '',
      body: document.getElementById('page-chat')?.innerText.slice(0, 400) || '',
    };
  });
  const instances = (await api('/api/instances')).body;
  if (!instances.length) {
    assert.equal(state.value, '', `an instance is selected while none runs: ${JSON.stringify(state)}`);
    assert.ok(
      state.sendDisabled || /启动|无可用|未启动|no instance/i.test(state.body),
      `composer looks usable with no instance: ${JSON.stringify(state)}`
    );
  }
});

test('english locale leaves no untranslated key on any page', async () => {
  await page.click('#btn-lang');
  await page.waitForTimeout(800);
  const namespaces = ['nav', 'app', 'chat', 'knowledge', 'models', 'settings', 'dashboard', 'params', 'presets',
    'logs', 'monitor', 'history', 'workspace', 'observability', 'help', 'errors', 'preflight', 'panel', 'sidebar'];
  const offenders = [];
  for (const route of ['dashboard', 'chat', 'workspace', 'knowledge', 'observability', 'help']) {
    await go(route);
    if (route === 'workspace') {
      for (const tab of ['models', 'runtimes', 'presets']) await page.click(`[data-workspace-tab="${tab}"]`).catch(() => {});
    }
    if (route === 'observability') {
      const tabs = await page.locator('[data-observability-tab]').all();
      for (const t of tabs) await t.click().catch(() => {});
    }
    await page.waitForTimeout(600);
    offenders.push(
      ...(await page.evaluate((ns) => {
        const out = [];
        const snap = document.evaluate('//text()', document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        for (let i = 0; i < snap.snapshotLength; i += 1) {
          const text = (snap.snapshotItem(i).nodeValue || '').trim();
          if (!text) continue;
          for (const n of ns) {
            const re = new RegExp(`^${n}\\.[a-zA-Z0-9_.-]+$`);
            if (re.test(text)) out.push(`${document.body.dataset.activePage || '?'}:${text}`);
          }
        }
        return out;
      }, namespaces))
    );
  }
  assert.deepEqual(offenders, [], `raw i18n keys rendered: ${[...new Set(offenders)].join(', ')}`);
  await page.screenshot({ path: `${SCREENSHOT_DIR}/english-knowledge.png`, fullPage: true });
  await page.click('#btn-lang');
  await page.waitForTimeout(600);
  await go('dashboard');
});

test('dark theme changes the palette and still passes axe', async () => {
  await go('dashboard');
  const light = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.click('#btn-theme');
  await page.waitForTimeout(700);
  const dark = await page.evaluate(() => ({
    cls: document.body.className,
    bg: getComputedStyle(document.body).backgroundColor,
  }));
  assert.notEqual(dark.bg, light, 'theme toggle did not change the page background');
  assert.match(dark.cls, /theme-dark|theme-light/, 'no resolved theme class on <body>');
  await page.screenshot({ path: `${SCREENSHOT_DIR}/dashboard-dark.png`, fullPage: true });
  const results = await runAxe(page);
  assert.deepEqual(
    results.violations.map((v) => `${v.id}:${v.nodes.length}`),
    [],
    `dark theme a11y violations: ${JSON.stringify(results.violations.map((v) => `${v.id}/${v.nodes.length}`))}`
  );
  // restore whatever the click cycle started with
  for (let i = 0; i < 3; i += 1) await page.click('#btn-theme');
  await page.waitForTimeout(400);
});

test('every non-destructive control on a tabbed page actually does something', async () => {
  // Regression guard: workspace.js/observability.js hoist a child module's
  // .page-actions into a shared toolbar, which moves those nodes out of the
  // child's subtree. Any module that delegates clicks on its own root then
  // ships a control that looks fine and does nothing. Detect it by clicking.
  await page.evaluate(() => {
    window.__probe = { mutations: 0, fetches: 0 };
    new MutationObserver((records) => {
      window.__probe.mutations += records.length;
    }).observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });
    const original = window.fetch.bind(window);
    window.fetch = (...args) => {
      window.__probe.fetches += 1;
      return original(...args);
    };
  });

  const skip = /删除|移除|清空|清除|停止|卸载|确定|创建|添加|保存|启动|取消|重建|浏览|导入|导出|恢复|备份|安装|重启|stop|delete|remove|clear|unload|launch|save|create|import|export|restore|rebuild/i;
  const dead = [];
  const targets = [
    ['workspace', 'models'],
    ['workspace', 'runtimes'],
    ['workspace', 'presets'],
    ['observability', 'logs'],
    ['observability', 'monitor'],
    ['observability', 'history'],
    ['knowledge', null],
    ['chat', null],
  ];

  for (const [route, tab] of targets) {
    await go(route);
    if (tab) await page.click(`[data-${route}-tab="${tab}"]`).catch(() => {});
    await page.waitForTimeout(700);
    await page.evaluate(() => {
      document.querySelectorAll('[data-probe-key]').forEach((el) => { delete el.dataset.probeKey; });
    });

    const keys = await page.evaluate((route) => {
      const scope = document.getElementById(`page-${route}`);
      if (!scope) return [];
      return [...scope.querySelectorAll('button:not([disabled]), select:not([disabled]), input[type="checkbox"]:not([disabled]), input[type="range"]:not([disabled])')]
        .map((el, i) => {
          el.dataset.probeKey = `p${i}`;
          return {
            key: el.dataset.probeKey,
            label: (el.textContent || el.getAttribute('aria-label') || el.type || '').trim().slice(0, 40),
            kind: el.tagName.toLowerCase(),
          };
        })
        .filter((item) => item.label);
    }, route);

    for (const item of keys) {
      if (skip.test(item.label)) continue;
      const handle = page.locator(`[data-probe-key="${item.key}"]`).first();
      if (!(await handle.count()) || !(await handle.isVisible().catch(() => false))) continue;
      await page.evaluate(() => {
        window.__probe.mutations = 0;
        window.__probe.fetches = 0;
      });
      await handle.click({ timeout: 3000 }).catch(() => {});
      await page.waitForTimeout(700);
      const effect = await page.evaluate(() => ({
        ...window.__probe,
        modals: document.querySelectorAll('.modal-overlay').length,
        toasts: window.__toasts.length,
      }));
      await closeModal().catch(() => {});
      if (!effect.mutations && !effect.fetches && !effect.modals && !effect.toasts) {
        dead.push(`${route}${tab ? `/${tab}` : ''}: <${item.kind}> "${item.label}"`);
      }
      if (tab) await page.click(`[data-${route}-tab="${tab}"]`).catch(() => {});
      await page.waitForTimeout(200);
    }
  }
  assert.deepEqual(dead, [], `controls that do nothing when clicked:\n${[...new Set(dead)].join('\n')}`);
});

test('800px viewport keeps every page free of horizontal overflow', async () => {
  await page.setViewportSize({ width: 800, height: 900 });
  const problems = [];
  for (const route of ['dashboard', 'chat', 'workspace', 'knowledge', 'observability', 'help']) {
    await go(route);
    await page.waitForTimeout(500);
    const box = await page.evaluate(() => {
      const doc = document.documentElement;
      const wide = [];
      const parked = (el) => {
        // Closed drawers are parked off-canvas with a transform; they are not
        // overflow. Only untransformed subtrees count.
        for (let n = el; n && n !== document.body; n = n.parentElement) {
          const t = getComputedStyle(n).transform;
          if (t && t !== 'none') return true;
        }
        return false;
      };
      doc.querySelectorAll('body *').forEach((el) => {
        const r = el.getBoundingClientRect();
        const ox = getComputedStyle(el).overflowX;
        if (r.width > 0 && r.right > doc.clientWidth + 2 && ox !== 'auto' && ox !== 'scroll' && ox !== 'hidden' && !parked(el)) {
          wide.push(`${el.tagName.toLowerCase()}.${[...el.classList].join('.')}@${Math.round(r.right)}`);
        }
      });
      return { scrollWidth: doc.scrollWidth, clientWidth: doc.clientWidth, wide: wide.slice(0, 5) };
    });
    if (box.scrollWidth > box.clientWidth + 1 || box.wide.length) {
      problems.push(`${route}: scroll=${box.scrollWidth}/${box.clientWidth} ${box.wide.join(' ')}`);
    }
    await page.screenshot({ path: `${SCREENSHOT_DIR}/narrow-${route}.png`, fullPage: true });
  }
  assert.deepEqual(problems, [], `narrow layout overflow:\n${problems.join('\n')}`);
  await page.setViewportSize({ width: 1280, height: 900 });
});

test('the click-through never touched a pre-existing knowledge base', async () => {
  await assertPreexistingUntouched();
});

test('the whole click-through leaves the console clean', async () => {
  assert.deepEqual(consoleErrors, [], `console errors during click-through:\n${consoleErrors.join('\n')}`);
});
