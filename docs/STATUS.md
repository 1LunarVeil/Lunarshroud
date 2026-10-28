# Current status

Last reviewed: 2026-10-27 (Asia/Shanghai)  
Current candidate: `0.9.0-beta.2`  
Release posture: Windows beta; do **not** describe as 1.0 or stable.

## 2026-10-25/27 audit remediation, multi-backend support and real-device inference

```text
backend    python -m pytest backend/tests -q      763 passed, 1 skipped
frontend   node --test frontend/tests/*.test.mjs  57 passed
browser    npm run test:browser (core-paths)      3 passed
lint       python -m ruff check backend tools      clean
mirror     tools/check_release_mirror.py           CLEAN, 225 files
rust       cargo test                              not run on VS 2026 (ring/cc)
build      PyInstaller onedir                      rebuilt 2026-10-27 23:33
portable   release/LunarShroud-0.9.0-beta.2-portable-x64.zip  rebuilt 2026-10-27
```

### Multi-backend support (CPU / Vulkan / OpenVINO / ROCm)

Verified against four real llama.cpp b11929 Windows packages (kept in
`study/`): they share an identical `--version` and an identical GPU flag
surface, so the launcher identifies the backend from sibling accelerator
DLLs and enumerates devices via `--list-devices`. On the audit machine
(AMD CPU + AMD Radeon iGPU + NVIDIA discrete GPU): CPU→(none), Vulkan→
{AMD iGPU, NVIDIA dGPU}, OpenVINO→{CPU, GPU.0 NVIDIA, GPU.1 AMD iGPU},
ROCm→(none). Real-inference matrix through the launcher API with a
4B Q4_K_M GGUF:

- Vulkan NVIDIA (`--device Vulkan1`) → running, correct answer.
- Vulkan AMD iGPU (`--device Vulkan0`) → running, correct answer.
- OpenVINO CPU (`OPENVINO0`, translated to `GGML_OPENVINO_DEVICE=CPU`) →
  running, correct answer.
- OpenVINO GPU.0 (`OPENVINO1`) → the upstream OpenVINO GPU plugin crashes
  (0xC0000409) right after model init; the launcher-side diagnosis is
  accurate, and the workaround is documented in-app (CPU device, or the
  Vulkan/CUDA build).
- ROCm with no supported AMD GPU → service runs on CPU at ~6.8 tok/s; the
  launcher now warns on the instance card instead of staying silent.
- Invalid OpenVINO device name → 400 with a readable, localized error.

### Security/quality fixes in this window

- Cross-origin CORS exposure fixed: knowledge citations and the
  knowledge-miss notice now reach the packaged (`tauri.localhost`) UI;
  previously both headers were filtered by the browser because the expose
  list carried only `X-LunarShroud-Dropped-Turns`. Pinned by a
  real-middleware test plus a static reconciliation test over every header
  the frontend reads.
- OpenVINO builds do not accept `--device`; the launcher translates the
  chosen device into `GGML_OPENVINO_DEVICE` and refuses unknown names (400).
- Settings ↔ runtime-registry default now sync in both directions.
- Knowledge-service idle reaper no longer races a concurrent acquire.

### Repository hygiene (2026-10-27)

Internal audit/remediation reports moved from the repository root to
`archive/audit-2026/` (mirror-excluded by directory rule); stale caches
(`.coverage`, `.pytest_cache`, `.ruff_cache`, `webview2-data/`, root logs)
and a 2.6 GB leftover test fixture were removed; `dsh-session-*` moved to
`archive/sessions/`. The portable zip was rebuilt from the freshly deployed
root pair.

Not verified: ROCm acceleration on a supported AMD discrete GPU, real NPU
inference (no NPU on this machine), NSIS install/uninstall round, multi-hour
soak.

## 2026-10-23/24 click-through, false-green audit, retrieval measurement and visual pass

```text
backend    python -m pytest backend/tests -q      731 passed, 1 skipped
frontend   node --test frontend/tests/*.test.mjs  57 passed
browser    node --test frontend/tests/browser/core-paths.test.mjs   3 passed
browser    deep-paths + adversarial (local, env-driven)            22 passed
lint       python -m ruff check backend tools      clean
tokens     backend/tests/test_design_tokens.py     clean
mirror     tools/check_release_mirror.py           CLEAN, 220 files
retrieval  377-chunk legal KB / 21 labelled + 10 unanswerable questions
           dense 33% hit@1 MRR 0.440 · hybrid 47% 0.610 · +doc cap 47% 0.612
           shipped (hybrid + cap + rerank) 76% hit@1 · 95% hit@3 · MRR 0.841
build      PyInstaller onedir                      rebuilt 2026-10-24
tauri      cargo tauri build --ci                  building 2026-10-24
nsis       bundle/nsis/LunarShroud_0.9.0-beta.2_x64-setup.exe  built, not installed
rust       cargo test                              not run on VS 2026 (ring/cc)
```

### Deployed pair (2026-10-24 00:26, launch- and render-verified)

`tools/Publish-PortableRuntime.ps1 -Destination .` replaced the repository-root
pair that the desktop shortcut launches. Deployed hashes match the build outputs
byte for byte:

```text
LunarShroud.exe            3326a90c54b1a18e…  == src-tauri/target/release/lunarshroud.exe
backend/backend.exe        a6e3934fdee6ddb6…  == backend/dist/backend/backend.exe
installer                  src-tauri/target/release/bundle/nsis/LunarShroud_0.9.0-beta.2_x64-setup.exe
```

The packaged window was captured with `PrintWindow(PW_RENDERFULLCONTENT)` and
shows this round's UI: the 26px page title, the first-run "下一步 / 当前状态"
panel with its three working actions, the labelled theme/about buttons and the
sidebar status block. Earlier on 2026-10-23 (hashes `f0253d3d…` / `5fe68d59…`)
the same check confirmed the consolidated five-destination navigation had
finally reached the shipped binary. After closing the app no `llama-server.exe`
survived and GPU memory returned to 0 MiB.

Not verified: NSIS install/uninstall, tray/`close_to_tray`, and a chat round-trip
with a running model inside the packaged app.

Everything above was verified against the **source tree and a dev backend**
first, and then against the rebuilt repository-root pair (the launch check
described above). Retrieval numbers come from `tools/eval_knowledge_retrieval.py`
run against the real index.

Defects this round found by clicking rather than by calling APIs — each fixed and
verified red→green, see `AGENTS.md` §2.27–§2.30:

- Hybrid retrieval was dense-only: the FTS arm quoted the whole question, so it
  matched nothing on natural language, and the existing test happened to use an
  exact 3-character phrase that passed either way.
- A child module's `.page-actions` is hoisted into the shared tab toolbars, which
  removes it from that module's subtree: the 卡片/列表 toggle never received its
  own clicks, its pressed state never resynced, and list view had never been
  implemented at all (selecting it hid the model grid).
- Card 快速启动 was swallowed by the card's own `[data-open-model]` delegate.
- Index cancellation had a backend, an API client method and tests, but no control.
- Chat overflowed an 800px viewport because its edge bleed hardcoded the wide
  layout's paddings.
- A user-named missing source path was skipped silently while the UI reported
  success; the add-paths dialog closed on failure and lost the user's text.
- Node test dependencies placed inside `frontend/` (Tauri's `frontendDist`) broke
  `cargo tauri build`; the JS package moved to the repository root and
  `test_web_root_hygiene.py` now pins the allowlist.


The source tree now includes the quota/data-safety and frontend reliability
remediation recorded in the internal remediation report. Rust, Tauri/NSIS, real
model/GPU soak and code signing remain release blockers; do not treat this
working-tree check as a 1.0 candidate.

## Working tree has moved past the frozen candidates

The sections below are the historical record of the frozen `0.9.0-beta.1`
candidate (2026-08-23) and of the incremental redeployments up to 2026-10-17.
The current candidate is **`0.9.0-beta.2`**, rebuilt on 2026-10-17 after the
independent release audit summarized under *Thirteenth round* below; its
fingerprints are in *Currently deployed pair (0.9.0-beta.2)*.

Current working-tree checks (full suite 2026-10-18; soak/P1 2026-10-19/20):

```text
backend    python -m pytest -q                      533 passed
coverage   --cov=backend --cov-fail-under=75        88.01% (gate 75)
frontend   node --test frontend/tests/*.mjs          21 passed
syntax     node --check (17 files)                   clean
lint       python -m ruff check backend tools        clean
mirror     tools/check_release_mirror.py             CLEAN, 155 files
rust       cargo fmt/clippy/test                     clean, 23 passed
build      PyInstaller onedir, pinned env            backend.exe A1BAACAA…
nsis       install/launch/uninstall                   pass; Add/Remove entry
                                                      blocked by sandbox,
                                                      normal-desktop check pending
```

Current working-tree checks after the UI/A11y pass, read-only registries (B1),
runtime/defaults wiring (B2), draft/offline work (B3), guardrails (B4), M1
polish and the maintainability split + model grouping (2026-10-21):

```text
backend    python -m pytest backend/tests -q          615 passed
coverage   --cov=backend --cov-fail-under=75          85.67% (gate 75)
frontend   node --test runtime + i18n + chat          38 passed
syntax     node --check (all frontend js/mjs)          clean
lint       python -m ruff check backend tools          clean
a11y       Lighthouse snapshot                       100 accessibility on
                                                      dashboard, models,
                                                      runtimes, param editor
                                                      (light + dark)
browser    isolated 17641 audit                       runtime switch, model
                                                      defaults provenance,
                                                      preset runtime restore,
                                                      history runtime column,
                                                      panel drawer at 800px,
                                                      settings dirty guard,
                                                      model grouping;
                                                      no unexpected console errors
mirror     tools/check_release_mirror.py              CLEAN, 188 files
```

**B2 is wired into launching.** The parameter editor and dashboard can choose a
registered runtime; schema, capabilities, recipes, command preview, preflight
and `POST /api/instances` all carry `runtime_id`. Presets can pin a runtime;
instances and history persist `runtime_id`/`runtime_label` (old SQLite databases
migrate in place). `core/config_resolver.py` implements global < manifest < user
defaults with per-key source tags; the UI shows provenance and applies defaults
only on an explicit button. The launch transport is unchanged: local
`llama-server.exe` + argv + loopback REST/WebSocket.

**B3 and the offline contract are also in place (2026-10-21).** Draft-model
compatibility reads only local GGUF metadata: total vocabulary size plus the
first 256 token strings, cached by path/mtime/size; a definite mismatch blocks
in preflight, an unreadable signature is an informational `unknown`, never a
false `match`. Model manifests declaring `min_llama_cpp_build` are checked
against the selected runtime's version line. `backend/tests/test_offline_contract.py`
pins the absence of remote frontend assets/imports, the loopback + Tauri-IPC Tauri CSP,
and the fact that only user-clicked documentation links leave the machine; see
`docs/OFFLINE.md`. There is no update checker or telemetry.

**B4 is complete.** The parameter editor can save the current differences as
user model defaults and clear them again; it never stores the whole default
dictionary and filters managed keys in the UI as well as the backend. Manifest
`customFields` with boolean/string defaults and `setJinjaVariable` effects are
merged into `chat_template_kwargs` as manifest defaults. `launch_guardrail`
(`off` / `warn` / `strict`) controls warning friction; errors always block.
Instances and history persist `model_key` (manifest key or `local:<path>`) and
`variant_key` (filename stem); old databases migrate in place. New builds that
support `--load-mode` get `--load-mode mmap` instead of the deprecated
`--mmap` for the default memory-map intent.

**M1 polish is complete.** Static analysis removed 23 unused CSS classes and
reduced raw px in `style.css` from 529 to 241; padding/margin/gap/font-size/
border-radius now use `tokens.css` exclusively, pinned by
`test_design_tokens.py`. Settings now asks before discarding unsaved changes
(X/Esc/backdrop), Logs auto-selects a running instance, shows a live
“shown / total” line count and removes the empty state when the first line
arrives, and Chat shows the runtime label plus an `aria-busy` loading state.
At 900px and below the secondary context panel becomes a focus-managed left
drawer instead of disappearing, so model/preset/log instance lists remain
reachable; it auto-closes on selection, ESC/backdrop, and leaves no inert state
behind when the window widens.

The shell was also split for maintainability: `modal.mjs` owns the dialog
stack/focus trap/confirm dialogs, `html.mjs` owns escaping, and
`param_editor_view.mjs` owns pure parameter/group HTML rendering. The model
library now asks the scan for manifests; when a manifest declares `model:`, a
“group by model” toggle groups files under that logical key, with unkeyed files
kept as “no manifest”. No behavior changes for directories without manifests.
Lighthouse on the parameter editor in light and dark remained 100 for
Accessibility, Best Practices, SEO and Agentic Browsing.

### Real runtime validation (2026-10-21, local machine)

The sandbox now has real CUDA 13.3 binaries and models, so the previously open
success path was exercised:

- Runtimes registered and probed successfully: official llama.cpp `b11698`
  (`D:\AI-tools\llama.cpp\build\bin`), prism `b10709`, prism `b10777`.
  All report `status=ok`; options 415 / 408 / 409 respectively.
- Official `build\bin` has a neutral path but ships `ggml-cuda.dll`; backend
  inference now falls back to sibling backend DLLs, so it reports CUDA instead
  of unknown.
- Real Qwen3.5-4B Q4_K_M launched on both prism b10777 and official b11698
  (RTX 5060 Laptop 8 GB), reached `running`, served `/health` and
  `/v1/models`, and completed `/v1/chat/completions` (about 92 tok/s on the
  prism build for a short prompt).
- Chat page loaded the real llama-server WebUI in the iframe; the Logs page
  received the real WebSocket stream. Logs now auto-selects the first running
  instance instead of showing “no instance”.
- Real draft check: Qwen3.5-4B + gemma MTP draft ⇒ `draft_vocab_mismatch`
  (gpt2 vs gemma4); Gemma-4-E4B + `mtp-gemma-4-E4B-it.gguf` ⇒ match, and a
  real Gemma+MTP launch reached `running` with the draft model loaded.
- Offline/proxy robustness: with `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`
  pointed at a dead local port, runtime probing and a real model launch still
  succeeded (backend health uses `trust_env=False`; probe/launch are local).
- Purely upstream observation: the llama-server WebUI requests `/tools` and
  receives 403 when tools are disabled; it does not affect launcher operation.
- Final B4 real run: the official build launched with `--load-mode mmap` and no
  deprecated `--mmap` warning; `model_key` / `variant_key` appeared in the
  instance and history API, and the history detail showed them.
- CORS hardening: the launcher now sends `--cors-origins localhost` by default.
  A real request with `Origin: http://evil.example` received no
  `Access-Control-Allow-Origin`; a `localhost` Origin was reflected. The Chat
  iframe (same origin) still loaded. `cors_origins` is a managed key for model
  defaults, so a manifest/user default cannot weaken it.

### Native chat and product chrome (2026-10-21)

The Chat page is no longer only an iframe around llama-server's WebUI. Its
default mode is a native streaming chat backed by:

- `core/chat_store.py` — local SQLite `chats.db` (bounded conversations,
  messages, params and stats; no network).
- `/api/chats*` CRUD and `POST /api/instances/{id}/chat/completions`, a
  loopback SSE proxy that keeps API keys server-side and avoids webview CORS.
- `markdown.mjs` (escaped, dependency-free rendering), `chat_stream.mjs`
  (cross-chunk SSE parsing), conversation sidebar, stop/regenerate, system
  prompt/temperature/max tokens, editable chat names, token/speed stats,
  reasoning disclosure, code copy, Markdown export and a WebUI fallback.

**Chat advanced (2026-10-21).** Editing a user turn truncates every later
message and regenerates the reply; an auto-generated conversation title follows
the edited first message, while a manually renamed title is left alone. Any
saved message can fork a new conversation up to that point, copying params,
model binding and stats without touching the source thread. The composer shows
a context meter from the latest `total_tokens` (or prompt/generated counters)
against the instance's `ctx_size`, turning amber near the limit. A prompt
library offers six built-in prompts plus locally saved prompts and insertion at
the composer caret; saved prompts can be edited in place or deleted; prompts
live in `chats.db`, so in-app backups and `Backup-UserData.ps1` carry them. Real Qwen3.5-4B b11698 run in Chromium against
an isolated backend: 2.1K/4.1K context meter after a 2103-token reply,
edit → truncate → regenerated reply
(1941 tokens), branch conversation created with one copied turn, prompt saved,
inserted and deleted; no console errors.

**Local text attachments (2026-10-21).** The native composer accepts text files
through the paperclip, a browser file picker or drag/drop; the webview reads
them with the File API and nothing is uploaded. An allow-list covers source,
Markdown, JSON/YAML/CSV/log and similar text extensions; NUL-containing binary
files and oversized files are rejected. Bounds are 200,000 characters per file,
five files and 500,000 characters per message, 2,000,000 characters per
conversation (recomputed server-side). Attachments live in `chats.db`
`attachments` and are rendered as chips with a bounded preview/copy dialog;
`buildMessagePayloadContent` wraps their full text in explicit file delimiters
for llama-server, while the visible message stays clean. Attachment-only turns
are allowed and use the file name for the automatic title; branches copy
attachments, Markdown export includes their content, and both backup paths carry
them inside `chats.db`. Real Qwen3.5-4B run: a local `secret-notes.md` containing
`LUNARSHROUD_ATTACHMENT_CODE: 7391` produced `7391` (286 tokens), a dropped
attachment-only `drop-test.txt` was included in the next turn, preview/copy,
branch copy, reload persistence and an unsupported-`.png` warning all passed;
console stayed clean.

**Conversation search (2026-10-21).** The Chat sidebar now searches conversation
titles, message text, attachment names and attachment content through
`GET /api/chats/search`. Queries are bounded to 200 characters and 30/100
results, use parameterized SQLite `LIKE` with `%`/`_` escaped, and return a
short snippet plus match type (`title`/`message`/`attachment`) without sending
whole attachments. The UI debounces input, preserves focus/caret across list
refreshes, highlights matches in titles/snippets, supports Enter to open the
first result and ArrowUp/Down navigation, and clears with Escape/×. Ctrl+F
focuses and selects the conversation search box while Chat is in native mode. Verified in
Chromium against the isolated backend: `7391` found two attachment hits with
name/meta/snippet highlighting, `GGUF` found message hits, no-match and clear
states worked, and the active conversation loaded from a result; console clean.

**Packaged regression (2026-10-21, no installer).** The current source was
rebuilt as a PyInstaller onedir backend plus an optimized Tauri binary
(`tauri build --ci --no-bundle`), then launched as a standard-user process with
isolated `APPDATA/LOCALAPPDATA/TEMP/USERPROFILE` and a synthetic `chats.db`.
Windows UI Automation against the real WebView2 verified: Chat route, search for
`7391` returning three attachment hits with names/snippets, prompt-library edit
persisting (`Packaged prompt`), attachment preview content, conversation branch,
message edit with attachment retained, and clean window-close/backend cleanup.
The optimized release binary repeated Chat/search and prompt-library checks
against the same isolated data. Backend logs stayed clean.

The release binary then launched real official llama.cpp b11698 + Qwen3.5-4B
Q4_K_M through the launch modal/preset. A real streamed chat answered
`打包态流式成功` (`218 tokens · 84.6 tok/s · 3.0s`, context meter
`237 / 4.1K`), and the packaged attachment picker attached a local
`packaged-attachment.txt`; the next answer was `8642` (`352 tokens · 84.6 tok/s
· 4.6s`, context `409 / 4.1K`). Stopping the instance left no `llama-server`
process, closing the window left no launcher/backend process or listener, and
the isolated `chats.db` retained the user message, attachment row and assistant
reply. Evidence: `tmp/packaged-check-20261021-chat/packaged-real-chat.png`,
`packaged-real-attachment.png`.

WebView2 155 no longer exposes its CDP endpoint here even with
`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=...` (an upstream
153+ regression), so packaged interaction used UI Automation rather than CDP;
the source Chromium run remains the console-error check. The release candidate
pair is `src-tauri/target/release/lunarshroud.exe` `5C8BA375…` (4,165,120 B) and
`backend/backend.exe` `28136B22…` (9,950,217 B) with 718 product files. It is not
deployed: the repo-root portable pair remains `0E778DD0…` / `A1BAACAA…`, and no
installer or signing work was performed.

Ctrl+K now opens a command palette for pages, new chat, model picker, parameter
editor, settings, theme and (in Tauri) an explicit quit. Instance cards on the
dashboard have a direct Chat action. Model grouping also falls back to the
containing folder when no manifest exists but that folder has multiple models;
manifest keys still take priority. `Backup-UserData.ps1` now includes `chats.db`
and its WAL/SHM files.

**Model manifest editor (2026-10-21).** Model detail → 模型清单 → 编辑清单 opens a
raw YAML/JSON editor with templates, live validation/preview, save and delete.
The backend only writes beside an existing `.gguf`; it validates with the same
parser used by scanning, refuses to create a second format beside an existing
manifest, writes atomically, and copies the old file to
`DATA_DIR/manifest-backups/` before overwrite/delete. The UI shows parsed model
key/warnings before saving; GGUF probe data remains authoritative. Verified in
real WebView2: template validation, save (`demo-org/editor-test`), automatic
backup on overwrite, delete with backup, and the card/grouping refresh.

**In-app backups and delayed restore (2026-10-21).** Settings → 数据备份 lists,
creates, verifies, deletes and restores local backups. Backups contain
`config.json`, SQLite snapshots of `history.db`/`chats.db` (sqlite3 backup API,
including saved prompts), and `presets/`; model files are never copied. A restore is checksum-verified,
staged in `restore-pending.json`, and applied on the next backend start before
history/chat databases are opened — never under a live process. A pre-restore
backup is written first; stale WAL/SHM sidecars are removed; presets are
replaced rather than merged. The API rejects restore while instances are
running, validates manifest paths against traversal, and the UI offers
“quit now to apply”. End-to-end: created a backup, changed the theme and added a
preset, staged restore, quit/relaunched, and verified the old theme/preset were
restored and the new preset removed.

**Desktop lifecycle (2026-10-21).** The Tauri shell now owns a tray icon with
Show/Quit. `close_to_tray` (Settings → General, default **off**) makes the close
button hide the window while the backend and llama-server instances keep
running; the tray Quit menu or Ctrl+K → “退出 LunarShroud” performs a full
shutdown. The setting is read from the same `config.json` as the backend and
can be toggled at runtime through the `set_close_to_tray` Tauri command. The
second-instance path uses EnumWindows (not FindWindowW) to restore a hidden
window. Close-to-tray disabled, close-to-tray enabled, runtime toggle, hidden
window restore and explicit quit were all verified in a real WebView2 session;
backend Job cleanup left no orphan processes.

### Real WebView2 validation (2026-10-21)

The current debug Tauri binary was run against the real WebView2 runtime with
an isolated data directory and a CDP audit over the Tauri boundary. Baseline
DPR was 1.545 at 1243x797 (1657x1036 screen); every route rendered without
horizontal overflow. At 800px/DPR2 the context panel became a working drawer
(288x852, backdrop, ESC restores focus to the toggle); at 1280px/DPR2 it
returned to a permanent panel. Settings exposed `role=dialog`, `aria-modal`, a
labelledby target and initial focus inside the dialog.

Two real defects were found and fixed:

- **CSP blocked Tauri IPC on every invoke.** `get_backend_session` was reported
  as a CSP violation and Tauri silently fell back to `postMessage`, logging an
  error each time. `connect-src` now includes Tauri's local
  `ipc:`/`http://ipc.localhost` bridge; the offline-contract allowlist was
  tightened to those exact entries.
- **Settings falsely reported unsaved changes on open.** The dirty check
  compared backend `config` with rendered values; the theme radio reflects the
  frontend preference (`system`) while the backend default is `dark`. Settings
  now records a per-group `baseline` after first render, so only actual edits
  are dirty. After the fix the modal closes directly and the console has zero
  warnings/errors.

End-to-end through the UI: official b11698 + real Qwen3.5-4B Q4_K_M launched
from the dashboard, served the real WebUI in Chat, streamed real logs over the
WebSocket, and after stopping, history showed
`local:...Qwen3.5-4B-Q4_K_M.gguf · Qwen3.5-4B-Q4_K_M`. The repo-root portable
pair predates these fixes and was not overwritten.

Native chat was exercised in the same real WebView2 + official b11698 session:
a streamed answer completed with `2716 tokens · 85.6 tok/s · 32.2s`, stop saved
a partial `已中断 · 3.5s` message, regenerate replaced it, reload restored the
conversation from `chats.db`, a fenced Python block rendered with a copy
control, chat settings persisted the system prompt, WebUI switched back and
forth, export produced the `已导出对话` action, and Ctrl+K navigated to Logs by
filter. Directory-only grouping produced `目录 · Qwen3.5-4B-GGUF` for two
hardlinked models without a manifest. Screenshots:
`tmp/feature-completion-20261021/native-chat*.png`.

### Fresh packaged-pair validation (2026-10-21)

A release binary built from the current source was run with a freshly built
PyInstaller onedir, isolated `%APPDATA%`/`%LOCALAPPDATA%`/`%TEMP%` and a fresh
WebView2 profile. The same audit passed: every route, DPR 1.545, 800px/DPR2
drawer with ESC focus restore, 1280px/DPR2 desktop, Settings dialog semantics,
and **zero console warnings/errors**. A real Qwen3.5-4B launch through the
packaged UI reached `running`, Chat loaded the real WebUI, logs streamed, and
history recorded the launch.

That fresh environment exposed a packaged-only defect: `backend.spec` excluded
PyYAML even though `model_manifest._load_yaml` imports it lazily and it is a
locked runtime dependency. In the frozen backend every `model.yaml` silently
became "not found", so manifest grouping and the manifest card were dead —
source-mode tests could not see it. The spec now collects `yaml` submodules and
no longer excludes PyYAML; a new `test_packaging_contracts.py` pins it. After
rebuilding the onedir, a real `model.yaml` beside hardlinked Qwen files produced
the group `demo-org/qwen-demo`, the detail manifest card, and history identity
`demo-org/qwen-demo · qwen-a`. The repo-root pair was still not overwritten.

## Post-takeover hardening (2026-10-18, deployed)

A read-only, three-way review of the beta.2 tree (backend lifecycle, Tauri
shell, silent failures/test gaps) re-derived the release state instead of
trusting the previous session's claims. It found no P1, but several P2
defects that could reproduce the reported instability. All were fixed,
tested red-green and rebuilt into the deployed pair:

- **Port reservation ownership.** A stale monitor from a stopped generation
  could release the *new* generation's reservation for the same reused port;
  the next auto-assigned instance could then bind the same port. Reservations
  are now released only when no other starting/running/stopping instance owns
  the key and the process is confirmed dead. A monitor that wakes while the
  stop path is still terminating no longer fabricates `STOPPED` or releases
  the port. Tests: `test_port_reservation_survives_the_old_generations_stale_release`,
  `test_release_port_keeps_reservation_while_the_process_is_alive`.
- **System tools never fall back to bare names.** Python and Rust now resolve
  `taskkill.exe`/`nvidia-smi.exe` from the real system directory
  (`GetSystemDirectoryW` in Rust) and disable the helper when it cannot be
  resolved; `which_on_path` skips relative `PATH` entries. Tests:
  `test_system_tools_are_disabled_when_the_system_dir_is_unavailable`,
  `test_which_on_path_ignores_relative_path_entries`,
  `system_tools_are_resolved_inside_system32`, and the missing-tool Rust test.
- **Capability probes cannot execute CWD files.** `probe_server` resolves
  relative `llama-server` names only against the application directory/PATH;
  an unresolved name returns a failed probe instead of `Popen`-ing a same-named
  file in the current directory. `_warm_server_probe` skips unresolved paths.
  Test: `test_probe_server_never_executes_a_relative_name_from_cwd`.
- **Incomplete `--help` no longer claims unsupported flags.** Missing options
  are called unsupported only when the probe is complete; an unparsed
  `--spec-type` block reports unknown instead of "all values unsupported".
- **Shell/backend data directory alignment.** The launcher reproduces the
  backend's writable-candidate order (including `<launcher>\backend\data`)
  and strips a UTF-8 BOM, so a `launcher_port` saved in a restricted-environment
  fallback directory is honored. Verified end-to-end: with every normal user
  directory pointed at a file, the second start listened on the edited
  `17531`, not the default.
- **Tauri lifecycle/diagnostics.** Single-instance creation failure no longer
  masquerades as "already running"; rapid crash loops back off up to 30 s;
  WebView2 falls back from the exe directory to `%LOCALAPPDATA%`; the
  readiness response is capped at 8 KiB and does not follow redirects;
  shutdown waits briefly for the backend and always logs the exit; the child
  stderr log falls back to the runtime temp directory; launcher log writes are
  serialized.
- **Client/API robustness.** Non-idempotent requests are no longer retried on
  network failure (duplicate instance/preset creation); WS parse/handler
  errors are logged; log-feed drops are surfaced as `logs_dropped`; late
  stdout lines cannot reopen a finalized instance log; an unreadable config is
  never overwritten with defaults; `/api/models/vram-estimate` samples GPUs
  off the event loop; missing static assets return 404 instead of
  `index.html` (with an explicit favicon link, so the browser no longer
  requests a missing `/favicon.ico`); deleting an unknown instance returns
  404; NVML's `NVML_VALUE_NOT_AVAILABLE` sentinel is reported as unknown.
- **Release mirror gate.** Missing `github/` now fails closed (opt-in
  `--allow-missing-mirror` for the public mirror itself); `--apply` runs the
  privacy/forbidden checks *before* writing anything; runtime directories
  created beside the exe (`webview2-data/`, `logs/`, `runtime-temp/`,
  `appdata/`) and local AI session directories (`dsh-session-*`, `CLAUDE.md`)
  are excluded at any depth.
- **Portable deployer.** An incomplete onedir build (no `_internal`) is
  rejected before touching the destination; launcher, backend and runtime are
  swapped together and restored on failure.

Verification for this revision:

```text
pytest        533 passed, coverage 88.01%
frontend      21 passed, 17 JS files syntax-clean
rust          fmt/clippy clean, 22 tests
mirror        CLEAN, 155 files
packaged      tmp/release_smoke.py: 22/22 (isolated APPDATA, dead proxy,
              HMAC readiness, token rotation, Job reaping, clean-shutdown log)
broken TEMP   launcher + backend reach ready with TEMP/TMP pointing at a
              nonexistent path
single inst.  second launch exits 0; exactly one launcher/backend remain
fallback      all normal user-data env vars unusable: backend uses
              <app>\backend\data; edited launcher_port=17531 is honored
real model    deployed launcher + Qwen3.5-4B Q4_K_M: RUNNING, raw
              /completion text, /v1/chat/completions content "OK", stop,
              port release, history stopped, no residue; isolated APPDATA
              and the real user data byte-identical before/after
embedding     nomic-embed-text-v1.5 Q4_K_M (space-containing model path):
              RUNNING, /v1/embeddings returned a 768-dim vector, stop,
              port release and clean launcher exit
soak          4-hour real-model run (2026-10-19): 7186/7186 requests, 0 failures,
              min/avg/p50/p95/p99/max 0.203/0.248/0.25/0.266/0.281/2.078 s;
              llama-server RSS 3178.8 -> 2795.0 MB (slope -2.053 MB/min),
              handles 221 -> 271, threads 41 -> 39, GPU ~3004 MB steady;
              stop/port/orphan/real-data checks all pass. The automated report
              verdict is FAIL only on the AC precondition (plugged=False at
              12:00:07); Kernel-Power event 105 shows power changes at
              11:59:24 and 12:02:40, consistent with a ~3-minute unplug
              window around start, current battery 96% on AC, no sleep/resume,
              max sample gap 61.2 s. Product soak criteria pass; see the
              4-hour soak section below. Earlier 15-minute run: 402/402,
              0 failures, RSS ~3.18 GB flat.
```

A follow-up independent verification (R8, 2026-10-18) reproduced the deployed
pair and the gates, and flagged three issues that are now fixed: Rust tests
depending on inherited `%TEMP%` (now hermetic; 22/22 with `TEMP`/`TMP` pointing
at a nonexistent path), an external temp-file residue in `backend/`
(`jy4s_b4h`; removed, with a narrow gate rule for 8-character tempfile-style
residue), and the NVML device-memory sentinel call site (now both `used` and
`total` go through `_nvml_memory_mb`, pinned by a call-site test). See
`RISK_ASSESSMENT.md` for the follow-up section.

P1 NSIS install/uninstall was re-run on 2026-10-20; see "NSIS P1 re-run"
below. Packaged screenshots were not re-run. The 4-hour soak was run on
2026-10-19; see "4-hour real-model soak". The browser check used the DevTools
protocol (DOM snapshot + console) because screenshot capture timed out in this
headless session; the dashboard and the parameter-editor modal rendered with
zero console errors and zero 404s.

## 4-hour real-model soak (2026-10-19)

The deployed root pair was exercised for 14,405 s through the launcher API in an
isolated `APPDATA`/`LOCALAPPDATA`/`USERPROFILE`/`TEMP` sandbox
(`tmp/soak-4h-20261019/`). Qwen3.5-4B Q4_K_M ran with `ctx_size=8192`,
`cache_ram=0`, auto-assigned port.

- Requests: **7,186 / 7,186 succeeded, 0 failures**; latency min/avg/p50/p95/
  p99/max **0.203 / 0.248 / 0.25 / 0.266 / 0.281 / 2.078 s**; throughput
  0.499 req/s.
- Resources: RSS **3,178.8 MB -> 2,795.0 MB** (slope -2.053 MB/min), max
  3,180.4 MB; handles 221 -> 271; threads 41 -> 39; GPU memory 3,004 MB steady,
  max 3,246 MB.
- Lifecycle: instance STOPPED, port 8080 released, launcher exited 0,
  management port 17520 released, no orphan LunarShroud/backend/llama-server
  process.
- Integrity: real `%APPDATA%\LunarShroud` and `%LOCALAPPDATA%\LunarShroud`
  byte-identical before/after; launcher/backend/installer hashes match the
  deployed pair; scanned logs contain no traceback and no supervisor restart.
- Environment deviation: the preflight AC check read `plugged=False` at
  12:00:07 (battery 99%). Windows Kernel-Power event 105 shows power changes at
  11:59:24 and 12:02:40, consistent with a roughly three-minute unplug window
  around start; at 16:03 the battery was 96% on AC. There were no sleep/resume events and the maximum
  sample gap was 61.2 s, so the run was continuous. The automated report's
  aggregate verdict is therefore **FAIL solely on the AC precondition**, not on
  a product check; the product soak criteria pass. The generated
  `report.json`/`report.md` are preserved unchanged, and this classification is
  the human review.

## NSIS P1 re-run (2026-10-20)

`python tmp\nsis_install_test.py` was run directly against the documented
installer `4C439483…` (the same installer hash as the deployed pair). Results:

- Silent install exited 0; installed `_internal` has 757 entries; backend hash
  `A1BAACAA…` matches. The installed launcher is `87B0B2E1…`, the NSIS bundle
  variant. The root portable launcher is `0E778DD0…`; the two differ only in the
  three-byte `BUNDLE_TYPE_VAR` marker (`NSS` vs `UNK`). The test now pins the
  NSIS variant so a portable-marker build cannot be shipped by mistake.
- Installed app reached backend ready on 17520; unauthenticated config returned
  401; wrong readiness session returned 404; the readiness proof matched
  HMAC-SHA256; the response contained no token; a valid token read config (200).
- WM_CLOSE exited the installed app; no backend remained; port 17520 was
  released.
- Silent uninstall exited 0; the install directory (including WebView2/log
  runtime artifacts) was removed; real `%APPDATA%`/`%LOCALAPPDATA%` data and
  desktop/Start-Menu shortcuts were restored.

The Add/Remove entry and shortcuts are still unobservable in this session. A
controlled minimal NSIS probe (not LunarShroud) ran through its full wizard:
`.onInit` wrote `ran.txt`, the install section created `probe.txt` and
`uninstall.exe`, but it created no Desktop/Start-Menu shortcut and no HKCU
`Software\NSISProbe` or Uninstall key. `ran.txt` reports a per-user
`...\Temp\Low` temporary path; the Start Menu ACL grants the sandbox group only
`ReadAndExecute`. This is a low-integrity / restricted-token restriction of the
current Codex session, not a LunarShroud-specific NSIS bug. The same restriction explains why the
LunarShroud installer copies files but cannot create shortcuts or the
Add/Remove entry here.

A candidate installer `860FC8FC…` adds an explicit HKCU `POSTINSTALL` hook in
`src-tauri/nsis/hooks.nsh` as belt-and-braces; it embeds the same NSIS launcher
`87B0B2E1…` and backend `A1BAACAA…`. The old `4C439483…` installer is
superseded for release. P1 remains **environment-blocked**: install/launch/
uninstall/cleanup pass, but Add/Remove and shortcuts must be confirmed on a
normal non-sandboxed Windows desktop before release. The portable zip remains
usable without an installer.

The fresh-environment row in the older block below is not a re-run in the
developer tree: a copy with no `data/`, `tmp/`, `.venv`, `backend/dist`,
`backend/build` or `src-tauri/target` was bootstrapped by `setup.ps1` under
**Windows PowerShell 5.1** earlier on 2026-10-17. The `0.9.0-beta.2` rebuild was
produced in this workspace with the pinned commands; see *Thirteenth round*.

The `css` row is not reproducible from a clean checkout on its own: the cascade
probe runs inside a page served by the backend, so it needs a running,
authenticated launcher (`tools/ui_preview.py --probe @tools/css_cascade_probe.js`).
Without a session token the backend answers 401, the stylesheet never loads, and
the probe reports `NO_SHEET` rather than a selector count. Treat `NO_SHEET` as
"the check did not run", not as a pass. The unique-selector count grows whenever
CSS is added, so read the first line of the probe output rather than memorizing a
number.

Modules added since the candidate: `core/gguf_probe.py`,
`core/capabilities.py`, `core/server_probe.py`, `core/vram_estimate.py`,
`core/formatting.py`; `frontend/js/i18n.mjs` with `locales/`, `models.js`,
`panel_views.mjs`, `model_picker.mjs`; `frontend/css/tokens.css`.

Endpoints added: `GET /api/system/capabilities`,
`GET /api/models/vram-estimate`, `GET /api/models/scan?with_capabilities=`.

Release hygiene added: `tools/check_release_mirror.py` gates the public mirror
(`github/`) against drift, forbidden paths and local-path leakage, and
`.gitignore` now covers `tmp/`, `.claude/` and `*.bak*`.

This section is informational only; test counts grow over time and the gate is
zero failures. The release blockers below were re-reviewed on 2026-10-17.
[CHANGELOG.md](../CHANGELOG.md)
carries the user-visible change record and [HISTORY.md](HISTORY.md) the dated
incident timeline; between them they are the published record of the evolution.

## Thirteenth round: independent release audit (2026-10-17)

Before calling this tree open-source-ready, an independent audit re-derived the
release claims from the source and reproduced the failure modes instead of
trusting `SECURITY_MODEL.md`/this document. It found one still-open P1, one P2
and several P2/P3 defects that the previous rounds had not pinned; all of the
security/data-integrity ones were fixed and given red-green regression tests.

**P1 — the readiness "identity proof" was forgeable.** `/api/system/ready`
accepted any peer that echoed the `session_id` the shell had just sent in the
probe URL. A local process that binds `127.0.0.1:<launcher_port>` before the
backend can therefore answer the very first probe and receive the management
token from the WebView; the token allows `PUT /api/system/config` to repoint
`llama_server_path` → arbitrary code execution as the victim. Verified by
protocol analysis and a PoC replaying the exact client predicate. The probe is
now a challenge-response: the shell picks a fresh nonce and accepts only
`HMAC-SHA256(token, "lunarshroud-ready-v1:" + nonce)`, which a squatter cannot
compute; the token never appears in the request or response
(`backend/main.py`, `src-tauri/src/lib.rs`).

**P1 — the capability probe handed the token to the user-selected
llama-server.** `server_probe._run` spawned `--help` / `--version` without
`env=`, so a configured `.bat`/third-party build inherited
`LUNARSHROUD_SESSION_TOKEN`. All subprocess paths (probe, `nvidia-smi`,
`taskkill`, instance launch) now use `sanitized_child_env()`.

**P1/P2 — restart token replay.** The supervisor restarted a crashed backend
with the *same* token and the frontend reconnected to the same port; a port
squatter could win the restart gap and capture a still-valid token. Each
generation now gets a new `session_id`+token, `get_backend_session` returns
nothing until the new generation passes the HMAC check, and the packaged
frontend no longer falls back to `127.0.0.1:17520` when no verified session
exists.

**P2 — stop could hang forever.** If `taskkill` was blocked (security software,
group policy, restricted sandbox), the return code was ignored and
`_finalize_process_resources` closed the stdout pipe while the reader thread
was blocked inside it: the stop request and monitor thread deadlocked. Reproduced
in this environment (first test-suite run hung; faulthandler stacks showed both
threads at `stream.close()`). `taskkill` failures now fall back to a psutil tree
kill, the reader owns closing, and finalize keeps a live handle.

**P2 — data-page corruption bypassed the history quarantine.** A valid DB with
one corrupted 4 KiB data page passed `CREATE TABLE`, then killed the lifespan
(`mark_unfinished_as_interrupted`) or made `/api/history` return 500 forever.
`init_db` now runs `PRAGMA quick_check` before caching; a red-green test corrupts
page 2 of a real 40-row DB.

**P2 — external links were dead in the packaged app.** WebView2/wry deny
unhandled `window.open`/`target="_blank"` requests, so the Getting-started
llama.cpp link and Chat's "open in browser" did nothing. The main window now
handles new-window requests and opens only `http`/`https` URLs through
`ShellExecuteW`; other schemes are refused and logged.

**P3 fixes:** failed termination keeps the live handle/port and can be retried;
`OSError` from `Popen` returns 400 instead of a bare 500; huge
`ctx_size`/`parallel` are rejected by bounds; non-ASCII tokens/session values are
compared as bytes (401/404, not 500); `0.0.0.0:P` and `127.0.0.1:P` reservations
conflict; a single oversized log line is truncated; loopback detection requires
a full IPv4 literal; double-escaped toast messages were corrected; the runtime
lock now includes `colorama`.

Verification evidence for this revision:

```text
backend    python -m pytest -q                         508 passed
coverage   --cov=backend --cov-fail-under=75           88.41%
frontend   node frontend/tests/runtime.test.mjs         13 passed
           node frontend/tests/i18n.test.mjs            7 passed
syntax     node --check (17 files)                      clean
lint       python -m ruff check backend tools           clean
rust       cargo fmt --check / clippy / test            14 passed
build      PyInstaller x2 (SOURCE_DATE_EPOCH/PYTHONHASHSEED)
           backend.exe                                854F95A7… (identical)
```

Red-green for the new regressions: reverting the quick-check makes
`test_data_page_corruption_is_quarantined_not_just_non_sqlite_files` fail;
reverting the finalize handle guard makes
`test_finalize_never_closes_a_pipe_owned_by_the_reader_thread` fail; reverting
`taskkill` return-code handling makes
`test_taskkill_nonzero_exit_falls_back_and_kills_the_whole_tree` fail (2 failed
on the reverted tree, 2 passed after restore).

Not verified in this round: a real-browser click-through of the new external-link
handler (headless WebView2 automation is unavailable here; the scheme allowlist
has a Rust unit test), an 8-hour soak (the 4-hour soak passed on 2026-10-19),
multi-GPU/Windows 10 coverage, and packaged-mode screenshot evidence. Those
remain outstanding as before.

### Deployed `0.9.0-beta.2` pair (2026-10-18)

Built in this workspace with `SOURCE_DATE_EPOCH=1790000000`,
`PYTHONHASHSEED=0` and the `RUSTFLAGS` cargo-registry remap, then deployed with
`tools/Publish-PortableRuntime.ps1 -Destination . -SkipDesktopShortcut`:

```text
launcher  0E778DD0C7AFDB1C0DAA17A7D24FFEC3498DB1BE0E61B75AD9FA820627DA3DD8
backend   A1BAACAA21FD6CDC49CBF8E7DA79556AA21D44056630627E87BF1A59ABD93B5E
installer 4C43948374E8F6F14BE2ECB323D0BA4181E2ACE390576A6C4DF9774649AC0657
          (historical installer; superseded)
candidate 860FC8FC9FDCDAA11BDBCC4010D50DBA9E863CE815E2A71C19C421F1D2647619
          (explicit HKCU POSTINSTALL hook; pending normal-desktop validation)
          (src-tauri/target/release/bundle/nsis/LunarShroud_0.9.0-beta.2_x64-setup.exe)
```

The earlier `854F95A7` build was run twice and produced the same `backend.exe`
hash both times. The 2026-10-18 hardening rebuild (`A1BAACAA…`) was built once;
byte-reproducibility of this round's launcher is not claimed. The NSIS installer
is still not byte-reproducible (makensis metadata).

**NSIS install/uninstall re-verified (2026-10-18, authorized).** Silent install
into a temporary directory exited 0; the installed pair contained the matching
backend (`A1BAACAA…`), 757 `_internal` entries and an installer-patched launcher.
The installed app reached `backend ready`, answered 401/404 and the HMAC
readiness proof, accepted the token, closed via WM_CLOSE, and left no
backend/port. Silent uninstall exited 0; the new `NSIS_HOOK_PREUNINSTALL`
(`src-tauri/nsis/hooks.nsh`) removed the app-created `webview2-data/`,
`runtime-temp/`, `logs/`, `launcher-boot.log`, `debug.log` and
`backend/backend-child.log`, so the install directory was removed. User data in
`%APPDATA%`/`%LOCALAPPDATA%` was byte-identical before/after, and the
desktop/Start Menu shortcuts were restored.

The Add/Remove Programs registry entry could **not** be observed in this
session: a minimal NSIS installer also fails to write `HKCU` here while a Python
probe in the same task succeeds, so this is an environment restriction rather
than a confirmed product defect. The 2026-10-17 round observed the entry on a
normal desktop; verify it once more in a normal interactive session before
release.

**The sidecar is now PyInstaller `onedir` (2026-10-18).** The first beta.2
deployment still failed on the user's machine with PyInstaller's
"Could not create temporary directory!" even though the same pair started from
the maintainer shell; the onefile bootloader extracts ~20 MB to `%TEMP%` before
any application code runs, so the launcher could not attribute the failure. The
backend is now built as `backend/dist/backend/backend.exe` + `_internal/`
(no per-launch extraction). `src-tauri/build.rs`, `tauri.conf.json` resources
and `Publish-PortableRuntime.ps1` copy/verify the whole directory; deploying
only the exe is a broken pair. Evidence:

- rebuilding the onedir backend twice produced the same `backend.exe`
  (`854F95A7…`);
- the packaged pair reaches `backend ready` even with inherited
  `TEMP`/`TMP` pointing at a nonexistent path;
- the same onedir backend also starts under the beta.1 launcher with a broken
  `TEMP`, so the fix does not depend on the user having the newest shell;
- the generated NSIS script contains `backend\_internal` file entries;
- a deployer regression test copies a nested `_internal/` tree with real
  PowerShell 5.1 and verifies every file hash;
- the rebuilt NSIS installer was silently installed into a temp directory
  (`/S /D=…`): exit 0, installed launcher, `backend\backend.exe` (hash
  `854F95A7…`, matches the build) and **716 `_internal` files**; the installed
  app pair is therefore complete. Silent uninstall removed the install
  directory, uninstall entry and shortcuts; the desktop shortcut was restored
  to the workspace root afterwards, and the real `%APPDATA%\LunarShroud` data
  hashes/timestamps were unchanged.

The earlier dedicated-temp hardening remains (it protects any future
onefile/child process and cleans stale `_MEI*`); it also removed 167 stale
directories (~2.2 GB) that forced-kill tests had left in the real `%TEMP%`.

**Restricted-environment data-dir fallback (2026-10-18).** A user run with the
new launcher produced `exit code: 3` *and left neither `launcher.log` nor
`backend.log` updated*. The real `%APPDATA%\LunarShroud\logs` ACL carried an
inherited `LUNARVEIL\CodexSandboxUsers:(RX)` entry: a process started through
that sandbox can read but not write the normal data directory, so
`configure_file_logging` / the history DB fail before the API is ready. Both
sides now treat persistence as best-effort and relocate it:

- frozen backend: probe `%APPDATA%` → `%LOCALAPPDATA%` → `%USERPROFILE%` → the
  application directory for writability, use the first writable one, and do not
  abort the lifespan if only the log file cannot be opened;
- launcher: `launcher.log` falls back from `%APPDATA%` to `exe\logs\`, backend
  temp falls back to `exe\runtime-temp\`, and a dependency-free
  `launcher-boot.log` (version, pid, APPDATA/LOCALAPPDATA/TEMP/USERPROFILE) is
  written next to the exe;
- the backend's stderr is captured into `backend/backend-child.log` (1 MB
  rotation) so uvicorn's startup traceback survives even when application
  logging cannot be created.

A probe that sets `APPDATA`, `LOCALAPPDATA`, `USERPROFILE`, `TEMP` and `TMP` all
to a **file path** (every normal directory unusable) still reaches
`backend ready`, writes `app\logs\launcher.log`, `app\backend\data\config.json`
and the boot/child logs — verified with a copy of the portable pair. A normal
desktop launch with writable `%APPDATA%` keeps using the existing user data.

**Duplicate launches produced the "exit code: 3" dialog (2026-10-18).** The
user's own `launcher-boot.log` showed two launches 26 seconds apart. The first
one succeeded (`backend ready pid=21856 port=17520`, the first app window was
created); the second one's `backend/backend-child.log` contains
`[Errno 10048] ... bind ... 17520` and exits 3. The error dialog the user saw
was therefore the *second* instance, not a broken installation. The launcher now
takes a per-session named mutex (`Local\LunarShroud-SingleInstance`); a second
launch calls `ShowWindow(SW_RESTORE)` + `SetForegroundWindow` on the existing
window and exits instead of spawning another backend. Window creation and
visibility are logged in `launcher-boot.log` (`hwnd=... visible_before/after`),
and `setup` reports a failed window build instead of leaving an invisible
process in the event loop. Verified with the packaged pair: first launch logs
`setup: main window created`; a second launch leaves exactly one
`LunarShroud.exe`/`backend.exe` pair and no port-17520 error. (Window visibility
cannot be asserted through `EnumWindows` from this headless DSH session, hence
the in-app log line.)

**Root cause of the invisible window: WebView2 data directory not writable
(2026-10-18).** A limited-token run (Windows Task Scheduler `/RL LIMITED`, the
same kind of token the user's desktop shell hands to launched apps) reproduced
the failure exactly: the backend reached `backend ready`, but `setup` logged
`window creation failed: 拒绝访问 (os error 5)` because Tauri's default WebView2
user-data folder (`%LOCALAPPDATA%\<identifier>`) could not be created by the
restricted token. The process stayed in the event loop with **no window**, so the
user double-clicked again and hit the second-instance port conflict described
above. The window builder now passes an explicit
`WebviewWindowBuilder::data_directory(exe_dir/webview2-data)`; under the same
limited token the boot log records:

```
setup: creating main window
setup: main window created
setup: hwnd=0xe1294 visible_before=true visible_after=true
```

and the backend child log shows the frontend actually connecting
(`WebSocket /ws/instances [accepted]`). The frozen backend also seeds
`config.json`/`presets/` from the real `%APPDATA%` into the writable fallback
directory, so a restricted run keeps the user's settings instead of showing an
empty configuration.

Packaged-pair smoke (`tmp/release_smoke.py`, isolated `%APPDATA%` and
`%LOCALAPPDATA%`, dead `HTTP_PROXY`/`HTTPS_PROXY` pointing at
`127.0.0.1:1`) — all checks passed:

- launcher reached `backend ready`, created the dedicated backend temp dir and
  bound the management port despite the dead proxy (the `.no_proxy()` path);
- unauthenticated `/api/system/config` → 401, wrong readiness session → 404;
- `/api/system/ready?session&nonce` returned the expected
  `HMAC-SHA256(token, "lunarshroud-ready-v1:"+nonce)` and did not contain the
  token;
- hard-killing the backend generation made the supervisor restart it; the new
  generation had a different token, the old token got 401 and the new one 200;
- no file under the isolated `%APPDATA%` contained the token;
- after the launcher was terminated, the Job Object reaped both backend
  processes and the port was released.

Not exercisable in this session: the graceful window-close path. The packaged
app runs in a headless session where WebView2 never creates a titled top-level
window (`EnumWindows` only showed tao's message windows), so `WM_CLOSE` had no
target; the launcher was force-terminated and the Job Object cleanup was verified
instead. The maintainer's desktop run in the earlier rounds remains the only
window-close evidence.

The public mirror was refreshed with `tools/check_release_mirror.py --apply`,
re-checked **CLEAN (154 files, 0 problems)**, committed as the mirror's current
`HEAD`, and its
unreachable preparation objects were purged (`git gc --prune=now`;
`git fsck --unreachable` empty).

## Verified state

### Clean-room build validated this round (2026-10-17, not deployed)

Built from the fresh copy described above with the pinned commands from
`BUILDING.md` (`PyInstaller backend.spec --clean -y`, then
`cargo tauri build --ci -- --locked` with the `RUSTFLAGS` path remap):

```text
launcher  00a64ad81c623a839e058a8e7faf7f0e74b668ac62b0765567b7b5f24d982b0c
backend   c0d1d0f6cc79e948a88d7b1444aabc0daade682611306849f558ddecec83fafd
installer 850a1af2570a30c6068983d4f506289ea22f8ff7dfd19bbaea57177b9f708a5b
          (src-tauri/target/release/bundle/nsis/LunarShroud_0.9.0-beta.1_x64-setup.exe)
```

These are the **final** artifacts: they include the stop/monitor exit-code race
fix and were built after the Linux-only `rustls` 0.23.46 security bump.
`cargo tree --target x86_64-pc-windows-msvc -e normal,build,dev -i rustls` (and
`-i glib`) returns nothing, so neither crate is part of the Windows graph.
The portable deployment copies were byte-identical to the two build outputs
(`Publish-PortableRuntime.ps1` verifies both hashes). Start/close checks used an
isolated `%APPDATA%`, so the
maintainer's real configuration was not touched:

- With a deliberately dead `HTTP_PROXY`/`HTTPS_PROXY` (`127.0.0.1:1`, no
  `NO_PROXY`), the packaged pair bound the management port within 1 s. Without
  the `.no_proxy()` fix the shell's readiness probe would have gone through the
  dead proxy and reported a timeout.
- Unauthenticated `/api/system/health` and `/api/system/config` returned 401;
  `/api/system/ready` without the session id (and with a wrong one) returned
  404.
- Closing the window exited the launcher and both PyInstaller processes and
  released the port; `launcher.log` recorded `backend ready` then
  `backend exited during shutdown`.
- Artifact scan: raw ASCII/UTF-16LE search **plus** decompression of the
  PyInstaller archive (1,112 modules, `co_filename` and constants) found zero
  hits for the user name, workspace name, machine name or local model paths.
  The launcher carries the expected 317 neutral `cargo-registry` prefixes.
- Real model through the launcher's own API: llama.cpp `0.7.0-dev (build 11698)`
  with `nomic-embed-text-v1.5.Q4_K_M.gguf`; `/v1/embeddings` returned a
  768-dimensional vector; stop and history persistence worked. The stop response
  and the history row both carried `exit_code=1`, exercising the stop/monitor
  race fix end to end. The backend process ran with the dead proxy in its
  environment, exercising the `trust_env=False` fix end to end.
- Short stability check (not a substitute for the 4–8 h soak): 120 + 60
  embedding requests, zero failures; launcher backend RSS stayed flat at
  83.0 MB across the first run. The second run measured 3 ms min / 9.6 ms avg /
  22 ms max. The first run contained one ~6-minute server-side stall that
  coincided with a parallel Rust release build on the same machine; it did not
  reproduce in the quiet run.
- Browser (headless Chrome over CDP, no console errors on any page): dashboard,
  parameter editor with the embedding recipe applied (preview contained
  `--embeddings`), model library with GGUF capability values, history with the
  new 已中断 badge after a hard-killed backend, monitor (RTX 5060 Laptop GPU),
  settings, and the embedded llama-server WebUI iframe rendered.

Not done in the clean-room round: NSIS install/uninstall (done later on
2026-10-17 after authorization; see the onboarding section above and the raw
sixth-round replay), a packaged-mode screenshot (foreground capture was
unreliable on the shared desktop), the 4–8 h soak and the multi-GPU/model
matrix.

### Onboarding and diagnostics rebuild (2026-10-17, workspace build)

A later build the same day added the in-app Getting started page, pre-launch
checks and failure attribution. It was produced in the workspace with the pinned
commands after the clean-room build above, then **deployed to the workspace root
on 2026-10-17** after the fifth-round verification. The NSIS installer was also
installed silently, started, closed, and uninstalled; the install directory,
HKCU uninstall entry and Start Menu/Desktop shortcuts were removed.

```text
launcher  85c5cc57abd61c4cfc238109fae7eb477bca576f6fdc2662134da6cc499c1c49
backend   6e94321bfeaf167df8a211fc6857246949bb77f8d7c15a79147b17253189c809
installer b38443868466ae3f9ad90a1b908b8810abf907cd0a17dbfcd734965ae4af08f5
```

- Packaged smoke with an isolated `%APPDATA%` and a dead `HTTP_PROXY`: listener
  on 18091, unauthenticated `/health` → 401, wrong session → 404, graceful
  window close exited the launcher and released the port.
- Browser validation (headless Chrome/CDP, no console errors):
  - Getting started page: 7 sections, 5 steps, 15 troubleshooting rows, dynamic
    checklist (missing server/model → red/amber; after configuring the real
    llama.cpp build and model folder → green).
  - Dashboard setup banner linked to the guide.
  - Model library showed the 嵌入模型 badge; direct launch of
    nomic-embed-text-v1.5 produced a command containing `--embeddings`
    automatically and reached RUNNING.
  - A deliberately failing server (`CUDA error: out of memory`) produced
    `out_of_memory` diagnosis; the dashboard's “为什么没启动？” modal showed the
    localized cause and fix.
  - Launching with a nonexistent model showed the blocking pre-flight modal
    (`model_missing`) with 关闭 / 查看新手指南 and did not create an instance.
- NSIS installer end-to-end (authorized 2026-10-17): with the stale audit install
  key removed, a silent install placed `lunarshroud.exe`, `backend\backend.exe`
  and `uninstall.exe` under `%LOCALAPPDATA%\LunarShroud` and registered the
  uninstall entry; the installed app started, returned 401/404, and closed
  cleanly; the silent uninstaller removed the directory, uninstall entry and
  Start Menu/Desktop shortcuts. The installer patches the launcher binary with
  bundle metadata, so the installed launcher hash differs from the portable
  candidate while `backend.exe` matches. A stale
  `HKCU\Software\lunarshroud\LunarShroud` install-location key is preserved by
  design when “delete app data” is not selected; it makes a reinstall reuse the
  previous directory (removed manually for the clean-registry test).
- Sixth-round replay (2026-10-17) captured the raw NSIS evidence that the
  previous round only narrated. Silent install exited 0 in 2.55 s and produced
  three files (24,997,085 bytes): installed launcher `076577a5…` (bundle-patched,
  differs from the portable candidate), `backend.exe` `6e94321b…` (matches), and
  `uninstall.exe` `069d6145…`. The installed app bound 17520, returned 401 for an
  unauthenticated config request and 404 for a wrong readiness session, then
  closed cleanly with no installed process left. Silent uninstall exited 0 in
  1.36 s and removed the install directory, uninstall entry and Start
  Menu/Desktop shortcuts; the install-location key remained by design and was
  removed to restore the pre-test state. The real `%APPDATA%\LunarShroud`
  config/history SHA-256 values were unchanged before and after. Raw logs and
  registry snapshots are in maintainer-local
  `tmp/risk_assessment_20261017/nsis/` (not published).
- Partial soak (not a replacement for the 4–8 h gate): the deployed packaged
  backend served a real Qwen3.5-4B Q4_K_M chat model for 610 s. 99 of 100
  sequential chat requests succeeded (the last failed only because cleanup
  stopped the backend mid-request); average 992 ms, max 2.0 s, 6,336 completion
  tokens. Backend RSS stayed ~64 MB. llama-server RSS grew from ~5.2 GB to
  ~11.4 GB and then plateaued, consistent with the default 8 GiB `--cache-ram`
  prompt cache rather than an unbounded leak; GPU use was ~2.98 GB of 8 GB.
  A full 4–8 h soak is still required.
- `tools/verify_checksums.py` now verifies a whole `SHA256SUMS.txt` in one
  command (fail-closed); release reports should quote its output instead of
  retyping digests (fifth-round P1).

### Remaining risk assessment (2026-10-17)

The full register, with probability/impact/evidence/residual risk, is in
[RISK_ASSESSMENT.md](RISK_ASSESSMENT.md). This round added four real-process
checks that were still open:

- **`cache_ram` memory behavior:** with the default `--cache-ram 8192`, the real
  4B chat run grew llama-server RSS from 5.2 GB to 11.4 GB and then plateaued;
  with `cache_ram: 0` the same model stayed at 3.21 GB over 40 requests (214 s),
  backend RSS 65.2 MB, GPU 2980/8151 MiB. The growth is llama.cpp's host-side
  prompt cache, not a launcher leak. Guidance added to GETTING_STARTED: lower
  the value or set 0 on 16 GB machines, at the cost of prompt-cache reuse.
- **Supervisor crash restart:** killing the packaged backend tree produced
  `backend exited unexpectedly … restarting` followed by `backend restarted
  pid=21476` on the same 17520 port in about 2 s; the window stayed alive, and a
  normal window close exited the launcher, released the port and left no new
  backend. The first generation's bootloader/child PIDs were 40104/35536; the
  restarted pair was 21476/39932.
- **Credential scan:** across 19 isolated `%APPDATA%` files (ASCII + UTF-16LE),
  the session token had zero hits. The per-launch `session_id` appears once in
  `backend.log`; it is not the management token and is invalidated on restart.
- **Rollback backup boots:** the previous pair in
  `tmp/portable-backups/20261017-174210` (`83634d…` / `e4fae5…`) started in an
  isolated `%APPDATA%` on 17520 and closed cleanly, so the backup is a real
  rollback point rather than a hash-only record.
- Repeated risk checks kept passing: management API bound only to
  `127.0.0.1:18097`; a non-ASCII/space model path reached RUNNING and returned
  768-dimensional embeddings; 10 consecutive launch/stop cycles reused port 8080
  with zero failures and no leftover `llama-server`.

The 8-hour soak (the 4-hour soak passed on 2026-10-19), multi-hardware matrix,
code signing and build provenance remain open; the risk document states that
beta publication is acceptable with those disclosed, but 1.0 is not.

### Seventh-round hardening (2026-10-17, deployed)

A rebuild after the sixth-round review carries four backend/frontend fixes and
two operational tools:

```text
launcher  67ab9f22b4a7a3de659ffb70aa8320fbff6c13e24e21f68552a4f5700c6c4aab
backend   30d908756d233560fdea54f2c6918899f7f86bcb40c025621b7d70bcb631f9c6
installer bba806827f4bb7344c2ddde4edea28469843b604a02e4e4327fdf2ce9f7c61f5
```

- **Corrupt `history.db` no longer kills startup.** Fault injection produced
  `sqlite3.DatabaseError: file is not a database` and exit 3. `init_db()` now
  quarantines the main DB plus WAL/SHM as `history.db.corrupt*` and rebuilds;
  lock/permission errors are still re-raised rather than misclassified.
- **>260-character paths are blocked before launch.** A valid 315-character
  GGUF path was accepted by preflight, then llama-server failed with
  `failed to open GGUF (No such file or directory)` and the diagnosis blamed
  format/download/architecture. New `path_too_long` / `path_near_limit` checks
  cover model, draft and mmproj paths, with bilingual title/fix text.
- **Truncated GGUF metadata is visible in scan summaries.** The model list now
  returns `truncated`/`stopped_early`; the library shows a `元数据截断` warning
  badge. Malformed probes still degrade to `probed: false` rather than 500.
- **Startup logs no longer contain `session_id`.** `backend.log` now records
  only `backend started pid=…`; a regression test asserts the session value is
  absent.
- **User-data backup/restore tools** (`tools/Backup-UserData.ps1`,
  `tools/Restore-UserData.ps1`): SHA-256 manifest, tamper/traversal rejection,
  pre-restore snapshot. During testing, PowerShell 5.1 launched from a
  PowerShell 7 environment could not load `Get-FileHash` because of the
  inherited `PSModulePath`; the tools now use .NET SHA-256.
- **Deployer relative-path fix:** `-Destination .` resolved through .NET's
  current directory and once deployed into `src-tauri\` while the workspace root
  kept the old pair. `Publish-PortableRuntime.ps1` now resolves relative
  `-Destination`/`-BackupDirectory` against `Get-Location`, with a regression
  test where PowerShell location and process cwd differ.
- **Reproducible builds:** with `SOURCE_DATE_EPOCH=1790000000` and
  `PYTHONHASHSEED=0`, two consecutive builds produced identical `backend.exe`
  (`30d90875…`) and `lunarshroud.exe` (`67ab9f22…`) hashes. The NSIS installer
  still changes between builds because makensis writes its own metadata
  (`bba80682…` for this release); installed backend/launcher hashes are verified
  separately.
- **Fault/security probe:** 34/34 against the rebuilt deployed backend
  (authentication, CORS/WS, traversal, `extra_args`, command injection,
  redaction, 413, malformed GGUF, child env without `LUNARSHROUD_*`, logs
  without token).
- **Short model matrix:** Qwen3.5-4B, Qwen3.5-9B IQ4_NL, Gemma-4-E4B, MiMo-9B,
  nomic-embed (768-dim) and Qwen3.5-4B CPU-only all reached RUNNING and answered
  one request; no llama-server left after each stop.
- **Concurrency:** 8 instances started/stopped concurrently; 8/8 running,
  ports 8080–8087 unique, 8/8 embedding requests succeeded, no leftover process.
- **NSIS replay (r3):** silent install exit 0; installed launcher `1cb283d2…`
  (bundle-patched), backend `30d90875…` (matches candidate); installed app bound
  17520, returned 401/404, closed with no residue; silent uninstall removed the
  directory, uninstall entry and shortcuts. Raw logs are in maintainer-local
  `tmp/risk_assessment_20261017/nsis-r3/`.

Gates at this revision: 497 pytest, 88.53% coverage, ruff clean, 19 frontend
tests, 17 JavaScript syntax checks, 10 Rust tests, mirror CLEAN.

### Currently deployed pair (2026-10-17)

Root `LunarShroud.exe` with the adjacent `backend\backend.exe`, deployed by
`tools/Publish-PortableRuntime.ps1` and confirmed byte-identical to
`src-tauri/target/release/lunarshroud.exe` / `backend/dist/backend.exe`:

```text
launcher  67ab9f22b4a7a3de659ffb70aa8320fbff6c13e24e21f68552a4f5700c6c4aab
backend   30d908756d233560fdea54f2c6918899f7f86bcb40c025621b7d70bcb631f9c6
installer bba806827f4bb7344c2ddde4edea28469843b604a02e4e4327fdf2ce9f7c61f5
          (src-tauri/target/release/bundle/nsis/LunarShroud_0.9.0-beta.1_x64-setup.exe)
```

This pair carries the llama.cpp v0.7.0 alignment round on top of everything in
the 2026-10-14/16 pairs: use-case recipes (`/api/params/recipes` plus the recipe
row in the parameter editor), nine new capability-driven parameters (pooling,
embedding normalization, prompt caching, router-mode directory and limit,
thinking mode, reasoning format, chat-template kwargs, RPC), argument-position
allowed-value parsing (`{a,b,c}` / `[a|b|c]`), build-aware select validation, the
annotated numeric-default fix (`--moe-cache-mib` no longer parses as
`0,disabled`), and crash translation for `GGML_ASSERT` lines.

Verified at the symptom level: window start with the backend bound to 17520,
HTTP 401 for unauthenticated requests (including the new recipes route), HTTP 404
for `/api/system/ready` without a session, and a **graceful window close** that
reclaimed the whole backend process tree and released the port (no residue in
`tasklist`/`netstat`, no orphaned `llama-server`). All three artifacts passed the
builder-identity byte scan (ASCII + UTF-16LE: zero hits for the user name, the
workspace-directory name, the machine name, and the user-profile path prefix; the
launcher carries the expected 317 neutral `cargo-registry` panic-location
prefixes). `SHA256SUMS.txt` beside the installer was regenerated from this build.

Backend gates at this revision: 464 pytest, ruff clean, 19 frontend node tests,
10 cargo tests, release mirror CLEAN at 145 files. Real-model evidence against
llama.cpp `0.7.0-dev (build 11698)`, all through the launcher's own API: an
embedding instance serving 768-dimensional `/v1/embeddings`; a 4 B chat instance
with `--reasoning off` and the read-only tool set answering in 0.2 s with
`/tools` reporting `write: false` for all four tools; router mode
(`--models-dir` + `--models-max 1`) reaching RUNNING with the router-shaped
`/v1/models`; and the upstream single-slot embedding assertion reproduced,
bisected and worked around both ways. What was **not** done at this revision: a
real-browser click-through of the recipe row and of the embedded v0.7.0 Web UI
(browser automation was not authorized this round), packaged NSIS
install/uninstall, and the 4–8 h soak.

### 上一对部署（historical, 2026-10-14）

```text
launcher  5bff4affbce90a23ac80530b30f11ffb9fe99ddf610b79c6196a5d16ec3dc114
backend   4149e2bc96a9701b76c65a5503b2a084cb848cde70fa87b3164ac9045b185cbc
installer b3112d83398864fbdf0d468fc9aad9a2a29bee6f927063f0e73419b107519f50
```

That pair carried the independent-audit fixes (probe-hang/thread-starvation H1,
restart launch-history M1, instance-log rotation/cleanup L1, BOM config L2), the
AI tools & MCP parameter group, the official-build (11630) alignment batch, and
the second-verification fixes N1/N2/N3.

### 上一对部署（historical, 2026-09-25）

These three values go stale on **every** rebuild. Any change to this section
must be produced by re-running the commands in [RELEASE.md](RELEASE.md) steps
6–8 (build, byte-scan for builder identity, recompute checksums) and regenerating
`SHA256SUMS.txt` beside the installer — not by copying the previous block
forward. Do not record a fingerprint for a pair that was not deployed and
start/close-verified together.

### Frozen `0.9.0-beta.1` candidate (2026-08-23)

The candidate this document was originally written against:

```text
launcher  4475C4F60DC26FDFCB9450E874168DE2CC50CAE3FCB9F8715961E6B559027925
backend   8479F8A6EEB3E8183811C898F7DD031F785464597556D5E8F96F7C6DAC4B46EF
```

On 2026-08-22 this pair opened a `LunarShroud` window, bound the management
service on port 17520, returned HTTP 401 to an unauthenticated request, exited
with code 0 after window close, and released the port. The Desktop shortcut was
rewritten to target this root launcher. (Pair refreshed twice on 2026-08-23;
see HISTORY.md for the resilience and security changes each rebuild carried.)

Current source checks passed: 38 Python tests with 76.85% product-code
coverage (75% gate), 6 frontend runtime tests, syntax checks for 11 JavaScript
modules, Rust formatting, Clippy with warnings denied except linker messages,
and 7 Rust tests. The public-source scan found 86 reviewed files with no
forbidden files/directories, source-parity mismatch, local-path match, or
secret-pattern match at the time of the check.

On 2026-08-23, frequent terminal flashes were traced to Windows child-process
creation for `nvidia-smi` polling and model-server launches. Both now receive
the no-console creation flag. The rebuilt packaged backend passed all 36 Python
tests (76.67% coverage) and an actual launcher start/close check with no
residual process or listener.

Later on 2026-08-23, backend-crash resilience was added end to end: the Tauri
shell supervises the sidecar and restarts it (reclaiming the crashed
generation's llama-server children via its Job Object). This historical entry
used the same token; the current supervisor rotates session ID and token each
generation, and the frontend reacquires it after the readiness HMAC check. The
frontend refreshes the session and retries once on 401/network
failure, reconnects both WebSockets with exponential backoff, de-duplicates
replayed log history, and shows an explicit "backend disconnected" header
state; instance lifecycle transitions are now written to the backend file log,
and launcher supervision events to `launcher.log` beside it.

The rebuilt matched pair (launcher SHA-256 `4475C4F60DC2…`, backend
`8479F8A6EEB3…`) was verified live: healthy start with window, port 17520
listener, and HTTP 401 without a token; force-killing the entire backend
process tree was followed by an automatic restart within ~1s (port re-bound,
auth intact, exit+restart recorded in `launcher.log`); a graceful window close
exited the launcher with no residual process or listener. After the
security-audit fixes, the full 22-probe attack battery was re-run against the
rebuilt packaged backend: 22/22 passed, including the chunked-transfer body
now returning 413.

## Remaining release blockers

The probability/impact/evidence view of these blockers and the accepted beta
risks is [RISK_ASSESSMENT.md](RISK_ASSESSMENT.md).

- No code-signing certificate; SmartScreen may warn and users cannot
  independently establish installer authenticity.
- No completed 4–8 hour real-workload soak test or representative GPU/model
  hardware matrix. A 610 s real 4B chat-model soak ran (99/100 requests, see
  above), but the full gate remains open.
- No GitHub build-provenance attestation (audit F16 residual). Releases publish
  `SHA256SUMS.txt` and put the digest in the release notes, but do not emit an
  `actions/attest-build-provenance` statement.
- Auto-assigned instance ports still have a probe → release → child-bind TOCTOU
  window: an unrelated process can take the port between selection and
  `llama-server` binding. The launch then fails with a clear bind error and a
  retry assigns a different port. The test double now retries binding briefly;
  the product-side race is documented, not eliminated.
- FastAPI test-client/httpx deprecation warning remains for the next dependency
  update; it is not a known packaged-runtime defect.
- `cargo audit` reports zero vulnerabilities after the 2026-10-17 `rustls`
  0.23.46 bump; seven warnings remain — five unmaintained `unic-*` crates
  inherited through Tauri/urlpattern, unmaintained `proc-macro-error`, and the
  Linux-only `glib` unsoundness advisory. Neither `rustls` nor `glib` is in the
  Windows dependency graph; track upstream Tauri/urlpattern updates.

Resolved since the previous list: Python dependencies are now hash-locked for
the tested CPython 3.11/3.12 Win64 wheels (`backend/requirements*.lock`) and
both CI and `setup.ps1` install from the lock with `--require-hashes`.

## Deployment rule

Never copy only one executable into a portable runtime. Deploy the matching
launcher/backend pair with:

```powershell
.\tools\Publish-PortableRuntime.ps1 -Destination . -BackupDirectory .\tmp\portable-backups
```

The script verifies both copied SHA-256 values and then updates the Desktop
shortcut. It is the canonical repair for a stale root EXE or shortcut.

## Deliberate no-touch areas

- `data\` is local runtime and user data in its entirety — `config.json`,
  `history.db` (plus its `-wal`/`-shm` sidecars while the backend runs),
  `logs\`, `presets\`, and any `config.json.corrupt` written when a damaged
  config is rebuilt. None of it is source or a release input. Do not publish,
  delete, or overwrite it during source cleanup.
- `src-tauri\target\` and `backend\dist\` are local build output. The
  beta installer remains in `src-tauri\target\release\bundle\nsis\`.
- `tmp\` is maintainer-local: debug scripts, screenshots, probe output, and
  recoverable archives moved out of the root on 2026-08-23. It is not part of
  the published tree, so nothing in it may be cited as evidence or instruction
  by a document that ships. `.claude\` and `.mimosa\` are likewise local tool
  state and record machine-specific absolute paths.

See [HANDOVER.md](HANDOVER.md) for commands and [HISTORY.md](HISTORY.md) for
the incident timeline.
