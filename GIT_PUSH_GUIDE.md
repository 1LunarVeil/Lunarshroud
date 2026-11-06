# 提交与推送操作手册（GIT_PUSH_GUIDE）

> **改动任何会被公开的代码/文档、或要 `git push` 之前，先读完本文件。**
> 每一条都是这个仓库真实踩过的坑，不是通用教程。跳过它们不会报错，只会
> 让 CI 变红、让排查多花几轮。

---

## 0. 先搞清楚目录结构（最容易搞错的一点）

```
<仓库根>\                   ← 你编辑的"工作树"，不是 git 仓库
   ├── backend\ frontend\ src-tauri\ tools\  ← 改代码改这里
   ├── data\                          ← 你真实的用户数据，测试绝不能写它
   └── github\                        ← 公开镜像 = 真正的 git 仓库，push 从这里来
```

**改动流程是固定的一条链**：

```
改工作树  →  check_release_mirror.py --apply 同步到 github\  →  在 github\ 里 commit  →  push
```

两条铁律：

1. **不要直接编辑 `github\` 里的文件。** 它由 `--apply` 生成，下次同步会覆盖你的改动。
2. **提交时必须 `cd` 进 `github\`，不要用 `git -C github`。** 如果你已经在 `github\` 里，
   `git -C github` 会去找 `github/github`，报 `fatal: cannot change to 'github'`。

---

## 1. 每次推送前必须跑的三条命令

```bash
cd <仓库根>

python -m pytest backend/tests -q          # 全量后端测试
python -m ruff check backend tools         # lint
python tools/check_release_mirror.py       # 镜像一致性 + 隐私门禁，必须 CLEAN
```

只有第三条是**本仓库特有**的：它逐字节比对工作树与 `github\`，并扫描本机路径/用户名泄漏。
`FAIL` 时不要手动改 `github\`，跑 `--apply` 修。

**基线数字**：`937 passed, 1 skipped`；覆盖率 ≥75%（当前 ~84.8%）。
数字变了要在 `README.md` 与 `README.en.md` 同步改，否则文档漂移。

---

## 2. 提交信息：写"为什么"，不写"改了什么"

**必须用文件传，不要用 `-m` 直接写在命令行里。** 提交信息里一旦有反引号，
Git Bash 会把它当成命令替换并**真的执行**（实测把 `npm run test:browser*` 的
输出塞进了提交信息）。

```bash
# 正确：把提交信息写进一个临时文件（放在仓库外），再用 -F 传入
cat > /tmp/commit_msg.txt <<'EOF'
fix: 一句话说明修了什么

为什么这是缺陷（含实测现象、日志片段、错误码）。
怎么修的，以及为什么这么修而不是别的办法。
验证方式：跑了什么命令、看到了什么结果。
EOF
git commit -F /tmp/commit_msg.txt
```

写清楚"我验证过什么、没验证什么"。**没拿到证据的结论要明说未证实**——
这比给一个看似完整但错误的因果链有价值得多。

---

## 3. 提交前先看清楚要提交什么

```bash
cd <仓库根>/github
git status --porcelain        # 逐个确认，别 -A 一把梭
git diff                      # 至少扫一眼
```

**已经踩过的坑**：

- 跑测试会在 `github\` 里留下 `__pycache__\`、`.pytest_cache\`、`.mimosa\`，
  门禁会报 342 处假红。**不要在 `github\` 里跑 pytest。**
  非跑不可时：`PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider --cache-clear`，
  跑完删掉 `github\.mimosa`。
- `.mimosa\`（AI 工具状态）会持续被写进 `github\`。提交前 `rm -rf .mimosa`。

---

## 4. 推送

```bash
cd <仓库根>/github
git push origin main
```

推送前确认：

```bash
git log --oneline origin/main..HEAD    # 待推的提交
git status -sb                         # ahead N，且无未提交改动
```

---

## 5. 推送后看 CI

```bash
curl -s "https://api.github.com/repos/1LunarVeil/Lunarshroud/actions/runs?per_page=5&branch=main" \
  | python -c "import json,sys;[print(r['name'],r['status'],r['conclusion'],r['head_sha'][:7]) for r in json.load(sys.stdin)['workflow_runs']]"
```

看具体哪一步失败（这是唯一可靠的定位方式）：

```bash
# 匿名读日志会 403，用本地已存的 git 凭据
TOKEN=$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill | sed -n 's/^password=//p')
curl -sL -H "Authorization: Bearer $TOKEN" -o /tmp/logs.zip \
  "https://api.github.com/repos/1LunarVeil/Lunarshroud/actions/runs/<RUN_ID>/logs"
unzip -q /tmp/logs.zip -d /tmp/logs && grep -rn "FAILED\|not ok" /tmp/logs | head
```

**不要打印 token。**

---

## 6. 本地跑浏览器门禁的正确方式（不做就会测出假红）

CI 至少有五种状态是开发机进不去的：**无 GPU、浅色主题、干净数据库、无注册表项、慢机器**。
在这些状态下的缺陷，本地永远测不出来，只能靠 CI 暴露——所以：

```bash
# 1. 后端：隔离数据目录 + 端口必须与测试一致
LUNARSHROUD_ALLOW_INSECURE_DEV=1 LUNARSHROUD_LAUNCHER_PORT=17520 python tools/dev_backend.py

# 2. 浏览器（playwright-core 不带浏览器；npx 会装错版本）
node node_modules/playwright-core/cli.js install chromium

# 3. 门禁
npm run test:browser
```

- 必须用 `tools/dev_backend.py`，否则会写你真实的 `data\`。
- `LUNARSHROUD_LAUNCHER_PORT` 必须等于测试用的端口。`trusted_origins()` 按
  **配置里的**端口派生 WebSocket 白名单，不一致时 HTTP 全 200、每个 WebSocket 都 403。
- 想测空状态/浅色主题分支，在页面里强制它：

```js
await page.evaluate(() => {
  document.body.className = 'theme-light';
  document.querySelectorAll('.hardware-chip').forEach((c) => c.classList.add('is-empty'));
});
```

---

## 7. 判断"是不是真修好了"

1. **红-绿**：先把修复退回去，确认测试变红；再恢复，确认变绿。只"写完就绿"的测试可能什么都测不到。
2. **注入真缺陷**：修完后故意把东西改坏，确认门禁仍会红。只验"变绿"分不清
   "修好了"和"把断言放宽了"。
3. **违规数量在多次运行间变化 = 竞态**，不是固定缺陷。固定缺陷的数量是稳定的。
4. **本地绿不等于 CI 绿**，CI 绿也不等于产品可用。要读存储状态与几何数值，不是读界面文本。

---

## 8. 关于 PR

- **dependabot 的 PR**：批量关掉无损（分支和提交都还在；安全告警是独立开关）。
  本仓库已设 `open-pull-requests-limit: 0`，默认不开，要升级时手工触发。
- **人类贡献者的 PR**：**不要批量关**。写清原因再关，否则会消耗对方动机并丢掉评审记录。

---

## 9. 一句话清单

```
□ 改的是工作树，不是 github\
□ pytest + ruff + 镜像门禁 三条全绿
□ 提交信息用文件传（-f 而非 -m），写清为什么与验证方式
□ git status 逐个确认，没有 .mimosa / __pycache__
□ push 前 git log origin/main..HEAD 确认范围
□ push 后用 API 看 CI 结论
```
