# 发布操作手册（RELEASE_GUIDE）

> **要发版、或改动 `.github/workflows/release.yml` 之前，先读完本文件。**
> 2026-11-05 首次发布 v1.0.0 连续失败三次，每次都是本文件里记过的一类问题。
> 发布链路是**平时不跑**的代码，它的缺陷不会在 CI 日常红点里出现。

---

## 0. 发布是什么（本仓库只有一个通道）

| 通道 | 产物 | 状态 |
|---|---|---|
| **便携包** | `LunarShroud-<版本>-portable-x64.zip` | 唯一通道 |
| ~~安装包（NSIS）~~ | ~~`*-setup.exe`~~ | **已彻底移除**（2026-11-05） |

安装包不是"本次不发"，是**通道整体删除**：`src-tauri/nsis/` 已删、
`bundle.active: false` 且无 `targets`、工作流用 `--no-bundle`，且产物里出现
任何 `.exe` 会**主动失败**。要恢复需要同时具备：代码签名 + 干净机器上的
安装/卸载往返验收 + 卸载钩子。三者缺一不要重新引入。

---

## 1. 发版的完整流程

```bash
# 前提：main 已推、CI 全绿、本地三条门禁 CLEAN

# 1) 版本号：只改一处，其它由测试逼着同步
#    src-tauri/tauri.conf.json 的 "version"
#    CHANGELOG.md 加对应小节的日期

# 2) 合并 CHANGELOG 的 [Unreleased] 到正式版本小节
#    （本项目约定：没公开发布过的修复直接并入当前版本，不为了凑 1.0.1 而升号）

# 3) 提交并推送
cd <仓库根>/github
git add -A && git commit -F /tmp/commit_msg.txt
git push origin main

# 4) 打 tag —— 这一步才真正触发发布
git tag v1.0.0
git push origin v1.0.0
```

**tag 必须和要发布的提交完全一致。** 如果发布失败后修了代码要重发，
必须**先删旧 tag 再重打**（见 §4）。

---

## 2. 发布流水线做了什么（步骤 5–18）

```
5  Install and test Python backend    ← 全量测试 + 覆盖率 ≥75%
6  Lint (ruff)
8  Frontend unit tests
9  Frontend syntax check
10 Build Python backend               ← PyInstaller onedir
11 Install pinned Rust and Tauri CLI  ← 最慢，约 10–15 分钟
12 Rust formatting, lint and tests    ← fmt + clippy -D warnings + test
13 Build the desktop launcher         ← tauri build --no-bundle
14 Stage the portable runtime         ← Publish-PortableRuntime.ps1
15 Build portable zip                 ← package_portable.py
16 Verify the portable zip is self-contained  ← 必须有 _internal/（≥50 条）
17 Create checksums                   ← release/dist/SHA256SUMS.txt
18 upload-artifact
── publish-release job（ubuntu，无 checkout）──
   download artifacts → 拒绝不完整 → gh release create/upload
```

全程约 **25–35 分钟**。第 11 步占一半。

---

## 3. 发布链路特有的四个坑（全部实测踩过）

### 3.1 `$LASTEXITCODE` 在全新会话里是 `$null`

工作流在**同一个 pwsh 会话**里调部署脚本，再检查 `$LASTEXITCODE`。而它只由
**原生命令**或 `exit` 设置。`Publish-PortableRuntime.ps1` 在 `-SkipSigning`
路径下不运行任何原生命令 → `$null -ne 0` 为 **True** → 脚本成功、工作流报
"deployment failed"（步骤只耗时 1.7 秒，成功消息之后 0.2 秒就 throw）。

**修法**：脚本结尾显式 `exit 0`。**本地测试抓不到**——测试用
`powershell.exe -File` 看**进程**退出码，正常结束就是 0。
回归测试：`test_deployer_sets_lastexitcode_for_session_based_callers`
（用 `-Command` + 同会话检查复刻 CI 调用方式）。

### 3.2 发布 job 没有 checkout，`gh` 推不出仓库名

`publish-release` 跑在 ubuntu 上且**故意不 checkout**（只需要产物）。`gh` 默认
从当前目录的 git 仓库推断 owner/repo → `fatal: not a git repository`。

**每条 `gh` 都必须带 `-R "$GITHUB_REPOSITORY"`**，包括 `gh release view`。
后者的失败被 `>/dev/null 2>&1` 吞掉，缺 `-R` 时永远判"release 不存在"，
重跑就会撞 "release already exists"。

回归测试：`test_every_gh_command_in_the_publish_job_names_the_repository`（按行扫描）。

### 3.3 `npx playwright install` 装错版本

见 `GIT_PUSH_GUIDE.md` §6。必须走 `node node_modules/playwright-core/cli.js`。

### 3.4 便携包必须自带 onedir 运行时

`backend/backend.exe` 与 `backend/_internal/` 是一个整体。第 16 步会检查
`_internal` 条目数 ≥50。只装 exe 的包在用户机器上一启动就"后端启动失败"。

---

## 4. 发布失败后怎么重发

**不要**直接再 push 一次 tag —— 旧 tag 指向失败的提交。必须挪：

```bash
cd <仓库根>/github
git tag -d v1.0.0                      # 删本地
git push origin :refs/tags/v1.0.0      # 删远程
git tag v1.0.0 <新的提交SHA>            # 重打到修好的提交
git push origin v1.0.0
```

如果 Release 已经建出来只是附件不对，重跑工作流会走 `gh release upload --clobber`
覆盖附件，不会报错。

---

## 5. 发完之后怎么验证（构建方的责任到这里为止）

只看"发出了"是不够的。做这三步：

```bash
# 1) Release 资产存在且大小合理
curl -s "https://api.github.com/repos/1LunarVeil/Lunarshroud/releases" | python -c "
import json,sys
for r in json.load(sys.stdin):
    print(r['tag_name'], r['published_at'])
    for a in r['assets']: print('  ', a['name'], a['size'])
"

# 2) 下载 zip，算哈希与 SHA256SUMS.txt 对得上
# 3) 展开 zip，确认 LunarShroud.exe + backend/backend.exe + backend/_internal/ 都在
```

**Release 正文不写"请核对哈希"之类的提示。** 校验和作为附件（`SHA256SUMS.txt`）
发布即可——是否校验是使用者的判断，构建方不逐条叮嘱。正文只写
"这是什么、怎么用"。

---

## 6. 发版前检查清单

```
□ main 已推、CI 全绿
□ pytest / ruff / 镜像门禁 三条 CLEAN
□ 版本号只改 tauri.conf.json；CHANGELOG 日期已改
□ [Unreleased] 已并入正式版本小节
□ 没有未提交改动（git status 干净）
□ tag 打在要发布的那次提交上
□ 发布后用 API 核对资产 + 下载验证哈希 + 检查 zip 结构
```

---

## 7. 相关文档

- `GIT_PUSH_GUIDE.md` —— 提交与推送（**先读那份**）
- `docs/RELEASE.md` —— 面向发布的校验清单（用户/维护者视角）
- `docs/BUILDING.md` —— 构建与本地跑门禁
- `AGENTS.md` §4 —— 产品边界（为什么只有便携包一个通道）
- `AGENTS.md` §2.35 —— CI 是"开发机进不去的状态"的唯一环境
