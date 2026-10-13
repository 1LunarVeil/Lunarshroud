# 前端功能契约（FRONTEND_CONTRACT）

**这份文件的作用**：前端最大的风险不是"写不好看"，而是**改着改着功能没了**。
整文件重写、忘记某个模块还绑着某个 DOM id、漏掉一条事件绑定 —— 这类损失
在 diff 里看不出来，只在用户点击时才暴露。

所以：**改动前端之前先读这里，改完对照本文件逐项确认。** 新增/删除任何
导出、DOM id、`data-*` 属性、端点调用，都必须同步更新本文件。

---

## 1. 硬性规则

1. **禁止整文件重写。** 单次改动尽量小；若某个文件需要改超过约 1/3 的内容，先拆成多次提交。
2. **改一个 class 或 id 之前先查引用**（LSP `findReferences`），不要靠文本 grep 猜。
3. **改完必须跑**（全部必须绿）：
   ```powershell
   node --test frontend/tests/runtime.test.mjs frontend/tests/i18n.test.mjs
   Get-ChildItem frontend\js -File | Where-Object Extension -in '.js','.mjs' | ForEach-Object { node --check $_.FullName }
   py -m pytest -q
   ```
4. **颜色/间距/圆角/字号/阴影/动效只能引用 `css/tokens.css` 的变量**，不得写死 hex / rgba / px。
   （`style.css` 当前已清零写死颜色；新增规则请保持。）
5. **视觉改动要用证据验收**，不能只靠肉眼：
   ```powershell
   py --% -X utf8 tools/ui_preview.py --out tmp/shots/x.png --page param_editor `
       --expand speculative --compare tmp/shots/baseline.png --compare-skip-top 60 `
       --compare-image tmp/shots/x_diff.png
   ```
   注意：同一状态两次渲染之间本身有约 0.1% 的抗锯齿噪声（实测已验证），
   所以"差异像素不为 0"不等于有回归 —— 要看 `--compare-image` 的标红位置，
   并核对每段差异的最大通道差（1/255 量级即噪声）。

---

## 2. 页面模块契约

每个页面模块（`frontend/js/*.js`）必须导出：

| 导出 | 类型 | 说明 |
|---|---|---|
| `id` | string | 唯一标识，同时是 `data-page` 值、`page-<id>` 元素 id、i18n 键 `nav.<id>` |
| `title` | string | 回退标题（实际显示走 `t('nav.' + id)`） |
| `init(el, appCtx)` | function | 只调用一次；负责填充 `el` 并绑定事件。**模态型模块不要导出它** |
| `onShow()` | function（可选） | 每次切到该页时调用；订阅/刷新放这里 |
| `onHide()` | function（可选） | 离开该页时调用；**必须在这里解绑订阅** |
| `renderPanel(el, appCtx)` | function（可选） | 提供二级左栏（上下文面板）内容。未导出 → 该页面板整列隐藏，顶栏开关置灰 |
| `kind: 'modal'` | string（可选） | 声明该模块是**浮层而非路由目的地**，见下 |
| `open(appCtx)` | function | 模态型模块的入口（取代 `init`） |

### 模态型模块（`kind === 'modal'`）

现有两个：`settings` 与 `param_editor`。它们不是"目的地"，而是随时召唤的覆盖层，所以：

- `app.js` 的 `initPages()` **跳过**它们，不创建 `#page-<id>` 容器；
- `navigateTo()` 遇到它就打开浮层并 **立即返回**，当前路由不变
  （实测：点「设置」/「参数编辑器」后 `.sidebar-item.active` 仍是原来的页面）；
- 因此它永远不会被标记为 `active`，也没有 `data-page` 之外的状态；
- 浮层内容自己创建节点（`#settings-nav` / `#settings-pane`；`#pe-nav` / `#param-groups` 等），
  生命周期随 `showModal`。

`param_editor` 模态化（2026-09-18）的三个特殊点，改它之前先想清楚：

1. **状态常驻模块级**：`currentValues` 等都活在模块闭包里，关掉浮层再打开不会丢
   （`Ctrl+L` 重开选择器换个模型 = 在原参数上换模型，不是清零重来）。
2. **二级交互一律 inline-sheet**：`showModal` 单实例、开头先 `closeModal()`，编辑器
   自己再弹任何窗都会把浮层顶掉（包括 `showConfirm`）。启动确认 / 保存预设 /
   重置确认共用浮层底部命令条上方的 `#pe-sheet`，同一时刻只开一个。
3. **底部命令条接管了 `modal-footer`**：`open()` 传空 actions，再整体重写 footer
   内容（摘要行 / `#cmd-suppressed` / 保存预设 / 立即启动）。不要往这里塞
   `data-idx` 按钮 —— `showModal` 的按钮机制已经不在这条 footer 上。

### 分类型表单必须保住未保存的输入

`settings` 的左导航是"一次只渲染一个分类"。切换分类会重建 DOM，
**未保存的输入会被下一次渲染丢掉**。所以这类模块必须：

1. 维护一个 `draft`（初值 `{...config}`）；
2. 在**切分类前**和**保存前**都调一次 `collect()`，把当前 DOM 收进 `draft`；
3. 渲染时读 `draft[key]` 而不是 `config[key]`。

### 二级左栏（上下文面板）

- 位置：`#context-panel`，夹在图标轨道与 `#main-content` 之间，宽度 `--panel-width`。
- 渲染时机：`navigateTo()` 每次切页、以及页面主动调用 `appCtx.refreshPanel()` 时。
- 收起状态存在 `localStorage.lunarshroud_panel_collapsed`，跨会话保持。
- 页面若在异步数据到达后才具备面板内容，**必须**在数据就绪后调用
  `appCtx.refreshPanel()`，否则面板会停留在"加载中"
  （`param_editor` 模态化后已无二级左栏，此规则对 `models` / `presets` / `logs` 仍适用）。
- 面板内若有订阅/观察者（如 `IntersectionObserver`），**必须**在 `onHide()` 中断开。

#### 共享面板视图 `panel_views.mjs`

| 导出 | 用途 |
|---|---|
| `renderList(el, ctx, {items, activeKey, onSelect, emptyText})` | 通用可选中列表（`.panel-list-*`）；item 形如 `{key, label, meta, metaHtml, badge, raw}` |
| `renderInstanceList(el, ctx, {instances, activeId, onSelect, emptyText, onlyRunning})` | 实例列表（运行中在前）；`logs` / `monitor` / `chat` 共用 |
| `instanceSignature(instances, activeId)` | 状态签名，用于判断"要不要真的重绘" |

item 里三个文本/徽标字段的分工（`models` 页踩过一遍才知道）：

| 字段 | 位置 | 用途 |
|---|---|---|
| `badge` | **标签行**右侧 | 短徽标（`statusBadge` 的"运行中"这种） |
| `meta` | 元信息行 | 纯文本，会被转义 |
| `metaHtml` | 元信息行 | 原始 HTML —— **需要放徽标时用它** |

徽标放在标签行会被长文件名挤压（实测 `内置 MTP` 在 252px 面板里折成两行）；
元信息行有整行宽度，所以"模型能力徽标"这类放在那里。CSS 侧对应
`.panel-list-meta` 是 flex，徽标 `flex: 0 0 auto`、文字负责截断 —— 反过来会把徽标挤扁。

两条硬性约束：

1. **面板视图不持有状态**。状态留在页面模块里，面板只读它。P0 那个
   "前后端各持一份字典"的病根就是两份数据源，面板绝不能重蹈覆辙。
2. **高频推送的页面必须用 `instanceSignature` 去重**。WS 每次推送都重绘面板，
   会把用户滚到一半的面板位置顶回顶部。

#### 统一空态 `emptyStateHtml(ctx, {icon, title, sub})`

居中细线 SVG 图标（`clock` / `cpu` / `file` / `layers` / `bookmark` 五种内置）+
主文案 + 引导副文案。纯函数，产 HTML 不持状态。已落地：logs 主区（无实例/无日志，
深色面板内自动切 code 系配色）、presets 详情、models 列表（区分「无匹配」与
「目录未配置」两种引导）、history 无记录、monitor 无 GPU。**新页面的空态一律用它**，
不要再手写 emoji + 文案的组合。

页面注册顺序 = 侧边栏顺序，见 `app.js` 的 `PAGES`：

`dashboard, models, chat, param_editor(模态), presets, logs, monitor, history, settings(模态)`

### `appCtx` 形状（`app.js::initPages` 注入）

```
Api, showToast, showModal, closeModal, showConfirm,
escapeHtml, formatDuration, formatTime, formatBytes, basename, statusBadge,
copyToClipboard, setTheme, getTheme, subscribeInstances, t,
refreshPanel, navigate
```

`init()` 与 `renderPanel()` 收到的是**同一个** ctx 对象（`app.js::buildAppContext`）。

新增页面所需能力 → 加到这里，不要在页面模块里直接 import `app.js`（会成环）。

`ctx.navigate(pageId)` 是页面间跳转的**唯一**入口。此前模块靠
`document.querySelector('.sidebar-item[data-page=…]').click()` 模拟点击，
既依赖 DOM 结构、又被复制到了两处；再加一处就是第三份。

**跳转传参**沿用既有约定：发送方先写 `window.__pendingXxx`，接收方在 `onShow()` 里读取并清空
（`__pendingLogsInstanceId`、`__pendingModelPath`）。这是跨页传参的既有模式，不是新设计。

---

## 3. 模块导出清单（改动后请核对）

| 文件 | 导出 |
|---|---|
| `api.js` | `tauriInvoke`, default `Api` |
| `app.js` | `showToast`, `showModal`, `closeModal`, `showConfirm`, `escapeHtml`, `formatDuration`, `formatTime`, `formatBytes`, `basename`, `statusBadge`, `getTheme`, `setTheme`, `subscribeInstances` |
| `i18n.mjs` | `LOCALE_NAMES`, `detectLocale`, `getLocale`, `setLocale`, `t`, `localizeBackendError` |
| `runtime.mjs` | `createSingleFlight`, `createReconnectDelays`, `boundedAppend`, `normalizeHttpUrl`, `isLoopbackHost`, `isParamEnabled`, `shouldAutoAssignPort`, `mergeUpstreamOptions` |
| `panel_views.mjs` | `renderList`, `renderInstanceList`, `instanceSignature`, `emptyStateHtml` |
| `model_picker.mjs` | `open(ctx)`（**不是页面模块**：没有 `id`/`init`，不参与路由，由 `app.js` 与 chat 按需调用） |
| `chat.js` | `id`, `title`, `init`, `onShow`, `onHide` |
| `dashboard.js` | `id`, `title`, `init`, `onShow`, `onHide` |
| `models.js` | `id`, `title`, `init`, `onShow`, `onHide`, `renderPanel` |
| `param_editor.js` | `id`, `title`, `kind`(`'modal'`), `open` |
| `presets.js` | `id`, `title`, `init`, `onShow`, `onHide`, `renderPanel` |
| `logs.js` | `id`, `title`, `init`, `onShow`, `onHide`, `renderPanel` |
| `monitor.js` | `id`, `title`, `init`, `onShow`, `onHide` |
| `history.js` | `id`, `title`, `init`, `onShow`, `onHide` |
| `settings.js` | `id`, `title`, `kind`(`'modal'`), `open` |
| `locales/zh-CN.mjs` | `ZH_CN` |
| `locales/en-US.mjs` | `EN_US` |

> `zh-CN` 与 `en-US` 的键集合必须完全一致，由 `frontend/tests/i18n.test.mjs` 守护。

---

## 4. DOM 契约

### 4.1 `index.html` 静态节点（改动前先确认没有 JS 依赖）

`sidebar`, `main-content`, `sidebar-backdrop`, `toast-container`,
`header-status`, `header-status-dot`, `header-status-text`,
`btn-sidebar-toggle`, `btn-theme`, `btn-lang`, `btn-about`,
`context-panel`, `context-panel-title`, `context-panel-body`,
`btn-panel-toggle`（顶栏开关，无面板的页面置灰而非隐藏）, `btn-panel-collapse`（面板头部收起）

### 4.2 各模块动态生成的 id

| 模块 | id |
|---|---|
| `dashboard` | `instance-grid`, `btn-launch`, `launch-preset`, `launch-port`, `launch-model`, `launch-name` |
| `models` | `models-subtitle`, `btn-models-rescan`, `model-detail`, `model-caps-body`, `btn-copy-model-path`, `btn-use-in-editor`, `btn-launch-model`, `model-search`, `model-show-all`, `model-list-body` |
| `chat` | `chat-instance-select`, `chat-instance-info`, `chat-frame-container`, `chat-iframe`（运行时创建）, `chat-empty`, `chat-goto-dashboard`, `btn-chat-refresh`, `btn-chat-external` |
| `param_editor` | 浮层内动态创建（无 `page-param_editor` 容器）：`pe-nav`（左分组导航）, `model-caps-card`（顶部能力条，空时不占位）, `vram-estimate`（能力条内显存预估行，空时不显示）, `btn-apply-recommended`, `param-groups`（参数区，一次只渲染当前分组）, `pe-cmd-toggle` / `pe-cmd-summary`（底部命令条摘要行）, `cmd-preview`（命令全文，默认收起）, `cmd-suppressed`（被丢弃参数的 **chip 组**容器，预置但 hidden，有内容才显示；每参数一枚 `.supp-chip`，`data-tip` 放「全名 + flag」）, `pe-sheet`（inline-sheet 容器，默认 hidden）, `load-preset-select` / `btn-reset-defaults`（浮层标题栏右侧）, `btn-copy-cmd`, `btn-save-preset`, `btn-launch-now`, `preset-name` / `preset-desc` / `preset-tags` / `launch-name`（均在 inline-sheet 内） |
| 顶栏 / 模型选择器 | `btn-model-picker`（静态 HTML，文案由 `app.js` 注入）, `picker-search`, `picker-list`, `picker-foot`（后三个由 `model_picker.mjs` 生成在模态里；foot 是左侧 `.model-picker-kbd` 快捷键提示 + 右侧计数的两段结构） |
| `chat` | `chat-instance-select`, `chat-instance-info`, `chat-frame-container`, `chat-empty`, `chat-reload-last`, `chat-external-only`, `chat-open-external`, `chat-goto-dashboard` |
| `presets` | `preset-detail`, `btn-new`, `btn-import`, `btn-diff`, `btn-copy-params`, `i-json`, `i-file`, `e-name`, `e-desc`, `e-tags`, `e-params`, `d-a`, `d-b`, `d-result` |
| `logs` | `logs-main`, `logs-view`, `log-search`, `log-level`, `log-pause`, `log-clear`, `log-download`, `log-conn-state` |
| `monitor` | `metric-canvas`, `gpu-table-wrap`, `proc-table-wrap`, `monitor-refresh`, `monitor-subtitle` |
| `history` | `history-table`, `history-pagination`, `btn-refresh`, `btn-clear`, `pg-prev`, `pg-next` |
| `settings` | 浮层内动态创建：`settings-nav`, `settings-pane`（无 `page-settings` 容器） |
| 全局（`app.js`） | `app-modal-overlay`（`showModal` 创建）, `debug-banner`（可选，index.html 未定义时静默跳过） |

### 模型能力卡片与参数编辑器浮层（`param_editor`）

`param_editor` 是模态型浮层（`modal-xl`）：顶部能力条 + 左分组导航 + 右参数网格 +
底部命令条。`#model-caps-card` 按当前 `model` 字段的值渲染，数据来自
`/api/system/capabilities?model=…`，把探测到的事实（原生上下文、层数、MTP 形态、
同目录配套文件）和一键应用放在一起；无模型时能力条不占位（`:empty` 隐藏）。

**挂在 `refreshAllControls()` 上**：它是"值变化后的统一出口"——载入预设、重置默认、
浏览选择、字段编辑全汇聚到它，卡片只要跟住这一处就不会漏。浮层标题里的模型文件名
（`加载模型 · <文件名>`）也挂同一个防抖出口。

四个必须遵守的点：

1. **探测请求要防抖**。`model` 是文本框，逐字符输入都会走到 `refreshAllControls`；
   不防抖就变成每敲一个字发一次 HTTP（冷后端上每次还要跑一次 `llama-server --help`）。
2. **`ctx_size` 的 `max` 要在每次 `renderGroups()` 之后重新收紧**。收紧到模型原生上限
   （探测不到时回落到 schema 的通用天花板 `1048576`）。原生上限是**状态不是说明**：
   `.native-ctx-note` 作为控件下方的独立内联 note，只在**接近（≥90%）或超过**上限时
   出现，不进 ? 提示圈。超过原生上限**不阻止提交**（有些模型配合 RoPE 缩放确实会用
   更大的值），只把提示切成 `.warn`。
3. **静态 help 与动态状态分流**。参数说明收进 label 行尾的 `?` 提示圈
   （`.param-help-tip`，hover/键盘聚焦 1s 延迟浮现、消失无延迟、带 8px 箭头、
   ESC 只收 tooltip；tooltip 首行是参数全名 —— 网格里 label 可能被截断）；
   构建不支持提示（`.param-field-warn`）、ctx 超限 note 这类**随值变化的状态保持内联**。
   数字参数的单位不是 label：`controlHtml` 把它放进输入框内右缀
   （`.input-unit-wrap` + `.input-unit`），label 行只留名字与徽标
   （label 支持 wrap、name 两行 clamp + 8ch 保底 —— 名字永不截到不可读）。
4. **参数网格按控件类型分跨度**。分组内容是 `auto-fill minmax(300px,1fr)` 网格；
   `file/dir/password/text/多选卡片` 加 `.param-field-wide` 独占整行，其余进格子
   （开关类是 `.param-field-switch` 单行紧凑形态；多选卡片容器内部自成
   `minmax(260px,1fr)` 网格）。左导航是**切换式**：一次只渲染当前分组，靠
   `currentValues` 即时写回保证切组不丢输入 —— 不要改回"全量渲染 +
   IntersectionObserver"那套。

### 键盘快捷键（都要能被解绑；注册位置随宿主生命周期）

| 组合 | 作用 | 注册位置 | 边界 |
|---|---|---|---|
| `Ctrl+Enter` | 参数编辑器：打开启动确认（inline-sheet） | `param_editor` 浮层 `open()` 时绑在 overlay 上，随浮层销毁自动解绑（焦点在浮层内才触发） | 其他页面 / 其他浮层不抢键；再按一次同 kind = 收起 |
| 模型选择器 `↑`/`↓` | 在结果列表移动选中行（`scrollIntoView nearest` 跟随） | `model_picker.mjs` 搜索框 keydown（随浮层销毁） | 只在搜索框聚焦时生效 |
| `Ctrl+L` | 打开模型选择器 | `app.js` 的 `initLayout`（与 ESC 同一处，键盘处理只有一份） | `preventDefault()` 否则会选中地址栏；编辑器浮层开着时按它 = 选择器替换编辑器（选完回来，参数不丢） |
| `Ctrl+R` | Chat 空态：重载上次使用的模型 | `chat.onShow` / `onHide` 解绑 | **只在空态**——有实例可用时抢它会把"刷新页面"变成"再拉一个模型" |

判断"是否空态"用 `#chat-empty` 是否在 DOM 里，而不是自己数实例列表：列表来自
WS 推送的缓存，刚停止实例时会短暂滞后，用缓存判断会让快捷键莫名其妙失效。

### 顶栏模型选择器（`model_picker.mjs`）

居中按钮（`.header-center` 用绝对定位，实测居中偏差 **0px**；`flex:1` 占位方案会让
"居中"取决于两侧内容宽度）。选择器是**独立模块而不是页面模块**：它没有 `id`/`init`，
不参与路由，只是 `open(ctx)`。数据源与模型库页相同（`/api/models/scan?with_capabilities=true`），
但只做"敲两个字母就选中"——不显示配套文件、不做筛选标签。

交互约定：**点行 = 选中并直接切换到参数编辑器浮层**（主按钮「配置并加载」/ 搜索框
Enter / `↑↓`+Enter 走同一条路）。键盘全流程在搜索框上：`↑`/`↓` 移动选中行
（只更新 `.selected` 类 + `scrollIntoView nearest`，不整表重绘）、Enter 打开、
Esc 走全局关闭。选中行有左缘 3px 指示条（与 `.panel-nav-item.active` 同一语言），
底部 foot 左侧常驻快捷键提示（`picker.kbdHint`）。
动作是 `window.__pendingModelPath = path; ctx.closeModal(); ctx.navigate('param_editor')`
（沿用 `models.js` 的跨模块传参约定；`param_editor` 是模态型模块，`navigate` 打开浮层）。
取消按钮是唯一的纯退出路径。

### Chat 的降级：非回环实例不嵌 iframe

`isLoopbackHost()`（`runtime.mjs`）判 `browserHost`；非回环时渲染 `#chat-external-only`
说明原因 + `#chat-open-external` 外开，而不是嵌一个必然空白的 iframe。两条原因都不是
我们能绕开的：CSP 只允许 `frame-src http://127.0.0.1:*`，且非回环实例强制要 API Key
（iframe 无法注入）。

### 显存预估（`#vram-estimate`）

**显存预估**来自 `/api/models/vram-estimate`，按**当前参数值**算，因此挂在
`refreshAllControls()` 的统一出口上（和模型能力卡片同一个防抖，350 ms）。

它是**估算**：llama.cpp 的真实占用还含随 batch 变化的 compute buffer、驱动预留。
所以界面上必须带"估算值，仅供参考"。它同时用于**容量对照**（见下方 `fit`），
但那只到"告警"一级，绝不允许拿来阻断启动。

返回里的 `kv_source` 有三档，对应三种提示，别混成一个 `partial`：

| `kv_source` | 含义 | 提示 |
|---|---|---|
| `declared` | 用的是 GGUF 里声明的 KV 头数与维度 | 不提示 |
| `derived` | 缺 KV 头数，按 `attention.head_count` 估算（GQA 下偏大） | "偏保守" |
| `unavailable` | 连层数/KV 维度都没有 | "只估了权重" |

返回里还有 `fit`，是后端把估算值与**本机显卡实际容量**对照后的分级结论
（估算器和 `gpu_monitor` 各自都对，此前没人把两者接到一起，于是界面上只有一
个裸数字，用户无从判断自己的卡够不够）：

| `fit.level` | 含义 | 渲染 |
|---|---|---|
| `ok` | 估算低于当前可用显存 | 不提示 |
| `tight` | 总量够，但当前可用量不够（别的进程占着） | `.warn`（黄） |
| `over` | 估算已超过显卡**总量**，物理上装不下 | `.danger`（红） |
| `unknown` | 拿不到显卡数据 | **不显示任何告警**，宁可漏报也不误报 |

`over` 这一档值得单独说：它预测的现象是"**加载成功、就绪通过、第一次推理才崩**"。
最贵的那块 compute buffer 要到计算图真正建立时才申请，所以任何只在就绪之后
才做的检查都救不了；界面上必须把这句话说明白，否则用户会以为显存够
（实测资源管理器在加载后还显示约 1 GB 盈余，而崩的那一刻要的比这多）。

多卡时容量按**合计**算（llama.cpp 会分摊），不要拿单卡上限去误报。

### 先绑行为、再取数据

`models.js` 的详情页 header 卡片是**同步**渲染的，而精确能力要等一次异步请求
（冷启动时后端要跑一次 `llama-server --help`，约 0.7 s）。第一版把按钮的
`addEventListener` 放在 `await` 之后 —— 那 0.7 s 里按钮**存在但点不动**。

现在改成：立即 `bindActions(el, state)`，需要能力的动作从 `state` 里**延迟读取**；
`#btn-launch-model` 在 `state.pending` 未完成时先 `await` 再启动（否则会用一份
"没有推荐值"的空 caps 去启动，静默丢掉 ctx 与推测解码的推荐）。

> 一般规则：**只要一块 UI 里既有同步渲染的部分、又有异步才就绪的数据，
> 事件绑定就必须放在同步阶段**，异步数据通过可变对象（或 getter）延迟读取。

### 模型能力数据分两层，成本差一百多倍

`models` 页刻意不把两件事混在一个请求里：

| 层 | 来源 | 成本 | 拿得到什么 |
|---|---|---|---|
| 列表 | `/api/models/scan?with_capabilities=true` | 快速探测 0.6~0.9 ms/个 | 原生 ctx、量化、是不是 mmproj/解码头、**配套文件数量** |
| 详情 | `/api/system/capabilities?model=…` | 需逐个探测配套文件头部 | 配套文件的**种类**、可用推测解码模式、推荐项 |

**判断配套文件"是什么"必须读头部**（`gemma4-assistant` 这类架构名才是判据），
那笔钱不该让一个几百项的列表去付。所以列表只报"有几个"，详情才报"是什么"。

> ⚠️ `launch-name` 在 `dashboard` 与 `param_editor` 中都存在，但两者处于不同
> 页面容器内，查询都用模块自己的 `root` 作用域 —— 重构时不要改成全局 `document` 查询。

### 4.3 `data-*` 属性契约

| 属性 | 含义 | 谁设置 / 谁读 |
|---|---|---|
| `data-page` | 侧边栏项 → 页面 id | `app.js` 设置并读 |
| `data-key` | 参数键（同时用于 `.param-field` 与控件容器） | `param_editor` 生成并读；`settings` 用 `data-field` |
| `data-param` | 参数键的别名（`paramHtml` 同时写 `data-param` 与 `data-key`） | `param_editor` |
| `data-group` / `data-group-body` | 折叠分组 key / 其内容容器 | `param_editor` |
| `data-browse` / `data-browse-type` | 浏览按钮 → 目标参数键 / file·dir | `param_editor`, `settings` |
| `data-opt` | 多选卡片里的单个选项值 | `param_editor` |
| `data-multiple` | 标记该控件是多值参数 | `param_editor` |
| `data-count` / `data-clear` | 多选计数显示 / 清空按钮 | `param_editor` |
| `data-action` / `data-id` | 列表行的动作名与记录 id | `dashboard`, `presets`, `history` |
| `data-field` | 设置项键 | `settings` |
| `data-tip` | CSS tooltip 内容（`attr(data-tip)` 机制）：`?` 提示圈向下弹、`.supp-chip` 向上弹 | `param_editor` |
| `data-goto-group` | 参数编辑器浮层左导航项 → 目标参数分组 key（切换式：点击重渲染该分组） | `param_editor` |

**两个"禁用"必须区分开**（都是 `param_editor` 加在 `.param-field` 上的类）：

| 类 | 含义 | 原因 |
|---|---|---|
| `.param-field.disabled` | 当前**不需要**（联动条件不满足） | 值变了就会恢复 |
| `.param-field-unsupported` | 当前**构建不认这个 flag** | 换构建才恢复；配套 `.param-field-warn` 提示条说明原因 |

> `data-key` 与 `data-field` 是**两套数据集**，不要统一：`data-key` = 实例启动参数（64 项，
> 走 `build_argv`），`data-field` = 启动器全局配置（13 项，走 `update_config`）。
> 区分开是有信息量的。

### `extra_args`：唯一一个"原样追加"的参数

`data-key="extra_args"` 是普通 `type: 'text'` 输入框（**前端不为它写任何特殊分支**），
但它在后端是特例：schema 里标了 `append_raw=True`，`cli` 为 `None`。

| 项 | 约定 |
|---|---|
| 语义 | 值是一行自由文本，按空白拆成 argv 片段，**追加到命令行末尾**（因此能覆盖上面已设的同名参数） |
| 拆分 | 引号感知；**不解释反斜杠转义**（Windows 路径里的 `\` 是字面量，用 shlex 会吃掉） |
| 错误 | 引号没闭合 → 启动前校验直接 `ValueError`，不静默截断 |
| 位置 | 必须是 `PARAM_DEFINITIONS` 的**最后一项**（顺序即语义，有测试守护） |
| 脱敏 | `--api-key=xxx` 是**一个** token，`redact_argv` 已覆盖等号形式 |

> 前端如果要为它做增强（语法高亮、flag 补全），直接读 `schema.params` 里
> `cli === null && type === 'text'` 的条目即可，不要靠参数名硬编码。

---

## 5. 后端接口契约（`api.js` 暴露）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system/config` | 读取全局配置 |
| PUT | `/api/system/config` | 更新配置（仅接受已知键） |
| GET | `/api/system/overview` | 系统资源快照 |
| GET | `/api/params/schema` | 参数 schema（前端表单唯一数据源） |
| GET | `/api/params/defaults` | 默认参数字典 |
| POST | `/api/params/build-command` | 由参数构建命令行预览。返回 `{argv, command, suppressed}` |
| GET/POST | `/api/presets` | 列出 / 新建预设 |
| GET/PUT/DELETE | `/api/presets/{id}` | 预设详情 / 更新 / 删除 |
| POST | `/api/presets/{id}/clone` | 克隆 |
| GET | `/api/presets/{id}/export` | 导出 |
| POST | `/api/presets/import` | 导入 |
| POST | `/api/presets/diff` | 对比两个预设 |
| GET | `/api/models/scan` | 扫描 .gguf |
| GET | `/api/models/browse` | 浏览目录 |
| GET | `/api/models/vram-estimate` | 按**当前参数值**估算显存（`model`/`ctx_size`/`n_gpu_layers`/`cache_type_k`/`cache_type_v`/`parallel`/`mmproj`） |
| GET/POST | `/api/instances` | 列出 / 启动实例 |
| GET/DELETE | `/api/instances/{id}` | 实例详情 / 移除 |
| GET | `/api/instances/{id}/command` | 实例启动命令 |
| POST | `/api/instances/{id}/stop` \| `/restart` | 停止 / 重启 |
| GET | `/api/history` | 分页历史 |
| GET/DELETE | `/api/history/{id}` | 历史详情 / 删除 |
| POST | `/api/history/clear` | 清空历史 |
| POST | `/api/history/{id}/relaunch` | 按历史重启 |
| GET | `/api/chat/instances` | 可用于 Chat 的运行中实例 |

WebSocket：`/ws/system/metrics`、`/ws/instances`、`/ws/instances/{id}/logs`
（统一经 `_createWebSocketWatcher`，带指数退避重连，令牌失效后自动刷新会话）

### `build-command` 的 `suppressed`（新增，前端必须展示）

`suppressed` 是**被丢弃的参数 key 列表**：这些参数用户给了值，但当前
`llama_server_path` 指向的构建**不认这个 flag**，所以没有写进命令行。

llama-server 的路径由用户在设置里自由指定，构建代次之间会有 flag 增删
（实测：`--mlock` / `--mmap` 在 build 10960 被删除，传进去就是
`error: invalid argument`）。后端必须丢弃它们，但**丢弃不能是静默的** ——
否则会出现"UI 里勾了开关、命令行里没有、也不报错"。

前端在渲染命令预览时若 `suppressed` 非空，应显式提示（例如
「本机构建不支持：锁定内存」）。同一份信息也出现在
`/api/system/capabilities` 的 `server.params[key].note == "本机构建不支持该选项"`。

---

## 6. 前端依赖的 schema 字段

`GET /api/params/schema` 的每个 param 至少提供：
`key, label, label_en, cli, group, type, default, min, max, step, options,
option_labels, option_labels_en, placeholder, help, help_en, required,
depends_on, depends_on_any, depends_on_nonempty, multiple, cli_reverse,
cli_no_value, unit, order`

**构建自适应字段**（新增，随当前 `llama_server_path` 指向的构建变化）：

| 字段 | 类型 | 含义 / 前端该怎么做 |
|---|---|---|
| `supported` | `true` / `false` / `null` | 本机构建认不认这个 flag。**`false` 才置灰并提示**（`params.unsupported`）；`null` = 没探测过或探测不完整，**一律不得禁用** |
| `upstream_options` | `string[]` \| `null` | 本机构建声明的合法取值。schema 的 `options` 是静态快照，上游新增的取值（实测 `draft-dspark`）只有靠它才能选到。用 `mergeUpstreamOptions()` 合并 |
| 顶层 `probed` / `probe_complete` | `bool` | 探测是否跑过 / 结果是否完整到可下"不支持"的结论 |

联动语义有**两份实现**，必须保持一致（由 `backend/tests/test_spec_decoding.py`
的不变量测试守护）：

- 后端：`core/param_builder.py::_is_param_active`
- 前端：`frontend/js/runtime.mjs::isParamEnabled`

---

## 7. 跨模块共享状态（隐式契约，重构时最易丢）

| 位置 | 用途 |
|---|---|
| `window.__TAURI__` | 判断是否在 Tauri 环境；`api.js` 借此决定是否走 invoke |
| `window.__LLAMA_BACKEND_URL__` | 后端基址（诊断展示用） |
| `window.__LLAMA_LAST_REQUEST_URL__` | 最后一次请求 URL（诊断展示用） |
| `window.__LLAMA_BACKEND_URL_ERROR__` | 会话获取失败原因（诊断展示用） |
| `window.__pendingLogsInstanceId` | `dashboard` → `logs` 的跳转参数：仪表盘点「日志」写入，日志页 `onShow` 读取 |
| `localStorage.lunarshroud_theme` | 主题（**注意**：`data/config.json` 里的 `theme` 目前只写不读，开屏主题完全由它决定） |
| `localStorage.lunarshroud_locale` | 语言 |
| `localStorage.lunarshroud_panel_collapsed` | 二级左栏是否收起 |
| `localStorage.lunarshroud_models_show_all` | 模型库是否显示配套文件（mmproj / 解码头） |
| `window.__pendingModelPath` | 模型库 / 顶栏选择器 → 参数编辑器浮层：要把哪个模型填进 `model` 字段（发送方写入，编辑器 `open()` 消费并清空） |
| `window.__pendingLogsInstanceId` | 仪表盘 / 聊天 → 日志页：要打开哪个实例的日志 |

### 外壳布局（P2 起）

```
.app-header
.app-body
├── .sidebar          一级图标轨道，宽度固定 --sidebar-width，永不改变
├── .context-panel    二级上下文面板，宽度 --panel-width，可收起、按页变化
└── .main-content     页面容器（各页面根节点 #page-<id> 在此，滚动容器）
```

- 轨道**不再** hover 变宽：旧实现 `:hover { width: 60px → 220px }` 会推动整页重排。
- 导航项在轨道态只显示图标，文案靠 `title` 悬浮提示；窄屏抽屉态恢复图标 + 文字。
- `IntersectionObserver` 的 `root` 必须是 `.main-content`（真正的滚动容器），
  用默认视口会导致折叠分组判定不准。

---

## 8. 已知结构性问题（P2 重构时要处理）

1. ~~**`style.css` 末尾有"更精致/更美观"的追加覆盖块**~~ ✅ **已修复（2026-09-16）**：
   原 L1132-1300 的"UI 精致度优化"段（约 123 行）重新声明了 15 个与前面重复的选择器。
   现已把全部声明归位到各自原始规则并删除该段。验证手段不是截图，而是
   `tools/css_cascade_probe.js` 导出的级联声明表——改动前后 **SHA-256 完全一致**，
   且报告由 `DUPLICATES(15)` 变为 `NO_DUPLICATES (259 unique selectors)`。
   `style.css` 从 1302 行降至 1178 行。
2. ~~`settings` 用 `data-field`，其它模块用 `data-key`~~ ✅ **已确认不需要统一**（见 §4.3 说明）。
   已修掉的真问题是：`settings.js` 的 `.param-field` 外层还留着从 `param_editor.js`
   复制来的 `data-key`，而该文件没有任何代码读它 —— 属死标记，已删。
3. **主题持久化双写**：`settings.js` 同时写后端 `config.theme` 与 `localStorage`，但开屏只读
   `localStorage`，后端那份实际是只写字段。要么统一到后端，要么删掉后端字段。
4. ~~**`launcher_host` 可编辑但后端只允许 `127.0.0.1`**，改任何值保存必 400~~
   ✅ **已修复（2026-09-16）**：改为 `type: 'readonly'` 的只读输入框，`collect()` 收到
   `draft.launcher_host` 后由 `save()` 主动 `delete`，不提交。实测保存往返后
   后端配置**逐项零差异**、`launcher_host` 保持 `127.0.0.1`。
   （同时补了帮助文案，明确写出"不可修改"及替代做法。）
5. **Chat 用 iframe 嵌 llama-server 自带 WebUI**，受 CSP `frame-src` 限制，绑定 LAN IP 的实例
   必然空白（`docs/SECURITY_MODEL.md` 已登记为残余风险）。
6. ~~**跨页 CSS 借用**：`presets.js` 的预设列表在用 `logs.js` 的 `.logs-sidebar-item`~~
   ✅ **已修复（2026-09-16）**：`logs` 与 `presets` 的页内列表都迁到了二级左栏，
   统一改用 `panel_views.mjs` 的 `.panel-list-*`；`.logs-layout` / `.logs-sidebar` /
   `.logs-sidebar-item` 三个类已整体删除。
   同期还修掉了两处页内冗余网格（`.logs-layout`、`presets` 的内联 `320px 1fr`）——
   它们都是在"全局左栏"里再套一个"页内左栏"，两级左栏已经是正确的位置。
