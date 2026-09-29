# TI-game — project memory

Workspace: `C:\Users\L\workspace_ccstheia\TI-game`
Remote: `https://github.com/mizy06/TI-Game.git` (origin)

## Repository

2026TI杯 赛道一 参赛项目。C / SysConfig 工程（`empty.c`, `empty.syscfg`, CCS 构建），
核心算法在 `waveform_analyzer.*` 与 `waveform_cascade.c`。

| Branch | Purpose |
|---|---|
| `main` | 集成/发布线 |
| `algorithm/precision-waveform-analysis` | 主力开发分支 |
| `hardware-serial-dma` | DMA 固件 |
| `codex/hardware-serial-dma` | 与上一分支同提交（Codex CLI 产生的重复分支） |

## 环境陷阱：git 无法写 refs/remotes

本机 git **无法在 `refs/remotes/**` 下留下 ref 文件**，也**无法在 `refs/heads/`、`refs/remotes/` 下新建子目录**：

- `git fetch` 会打印 `[new branch] x -> origin/x`，`git update-ref` 返回 exit 0，但 ref 随后不存在，
  `git rev-parse` 报 `fatal: Needed a single revision`。
- reflog（`.git/logs/refs/remotes/origin/*`）**正常写入**，所以 reflog 有记录、ref 却没有。
- `git branch -vv` 会显示 `[origin/x: gone]`。
- 直接子节点（`refs/heads/foo`）、`refs/tags/*`、新建命名空间（`refs/foo/bar`）都正常。

**绕过方法**：fetch 后用 shell 直接写 ref 文件（会正常持久化，git 也能读到）：

```bash
mkdir -p .git/refs/remotes/origin/algorithm
printf '%s\n' <sha> > .git/refs/remotes/origin/main
printf 'ref: refs/remotes/origin/main\n' > .git/refs/remotes/origin/HEAD
```

新建本地分支同理（`git branch` 会静默失败）。根因未确认，疑似沙箱/后台进程
（曾观察到非本会话触发的 `update-server-info` 重写 `.git/info/refs`）。
详见 skill `ti-game-git-branch-sync`。

## 注意事项

- 工作区经常有**未跟踪、未提交的测试产物**（`Serial Debug *.txt`、`tests/results/`、
  `tools/strict_serial_noise_test.py`）。**不要** `git clean` / `git checkout .`。
- `tests/` 下有大量测试结果（csv/json/jsonl/bin/log），是本项目的主要数据资产。
- 串口调试日志（`Serial Debug *.txt`）用于回归对比，文件名带时间戳，勿删。
