# CC-Worker

[English](README.md) | [简体中文](README_zh.md)

一个将实质性编码和测试委派给本地 Claude Code CLI 的 Codex skill。
Codex 负责探索源码、设计任务、审查真实 diff 和集成结果；worker 使用已经配置好的模型
完成实现，并承载详细文件读取与执行日志。支持 Anthropic、DeepSeek 等兼容服务。

## 能力与分工

- 按完整功能边界派单，明确允许修改的文件，不限制固定文件数量。
- 多个独立任务在各自 Git worktree 中并行执行。
- 从当前代码建立快照，保留用户的暂存和未暂存修改。
- 记录准确的 session ID，修正时续接对应任务，避免串错会话。
- 提供 worker 与检查命令超时、真实检查退出码、精简 JSON 结果及完整本地日志。
- Codex 审查后应用 worker 增量补丁，保留原暂存区和分支历史。

目标是减少进入 Codex 上下文的实现细节、重复读文件和冗长日志。
Codex 仍需阅读关键源码并验收结果；不承诺固定 Token 或成本节省比例。
provider 返回的用量数据也不能直接衡量 Codex 的节省量。

## 环境与安装

支持 macOS、Linux 和 WSL，需要 Bash、Python 3.9+、Git 2.25+、Codex，以及已配置好、
在 PATH 中可执行的 [Claude Code CLI](https://code.claude.com/docs/en/overview)。
运行时无需额外 Python 包或 Agent SDK。

### 使用 Skills CLI 快速安装

通过 [Skills CLI](https://github.com/vercel-labs/skills) 直接从 GitHub 安装，
无需手动克隆仓库。`npx` 命令需要 Node.js 22.20+：

```bash
npx --yes skills add siray-code/cc-worker-skill --skill cc-worker --agent codex --global --yes
```

使用 pnpm 或 Bun 的等价命令：

```bash
pnpm dlx skills add siray-code/cc-worker-skill --skill cc-worker --agent codex --global --yes
bunx skills add siray-code/cc-worker-skill --skill cc-worker --agent codex --global --yes
```

以上命令任选其一。`--global` 表示用户级安装，所有项目均可使用；
如需项目级安装，在项目根目录执行并去掉 `--global`。
`--agent codex` 指定安装到 Codex，`--yes` 跳过 Skills CLI 的确认提示。
Node.js 和包执行工具仅用于这种安装方式，运行 skill 时不需要。

### 从本地仓库安装

克隆仓库并执行自带安装脚本：

```bash
git clone https://github.com/siray-code/cc-worker-skill.git
cd cc-worker-skill
./install.sh
```

默认将 `cc-worker/` 软链接到 `~/.agents/skills/cc-worker`，即当前
[Codex 用户级技能目录](https://learn.chatgpt.com/docs/build-skills)。
安装可重复执行，不覆盖已有真实目录或其他软链接。软链接安装后需保留仓库目录。

项目级安装或明确使用旧版目录的环境可以指定目标：

```bash
./install.sh --target /path/to/project/.agents/skills
# 也可以：./install.sh --target "$HOME/.codex/skills"
```

建议只安装到一个发现目录，避免重复条目；也支持将完整 `cc-worker/` 目录复制过去。
在 Codex 中通过 `/skills` 查找或用 `$cc-worker` 调用；未出现时重启 Codex。

## 在 Codex 中使用

```text
$cc-worker 给用户服务增加 CSV 导出并补充回归测试。
先理解现有接口，再派给 worker 实现，最后审查补丁。
```

```text
用 cc-worker 处理这两个独立修复，在隔离 worktree 中并行执行，
分别审查后集成，再运行必要的组合检查。
```

源码探索和架构决策由 Codex 完成。实质性编码默认派单，简单修改和少量收尾可以直接完成。
共享接口、锁文件、存在实现依赖的任务，以及依赖共享可变服务的测试，需要顺序执行。

## 配置模型服务

已配置好的 Claude Code 可直接使用。执行器继承其环境和配置，不强制切换模型，
也不修改全局 Claude 设置。

如果希望单独为 worker 配置 DeepSeek，可创建 `~/.cc-worker.env`：

```bash
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export ANTHROPIC_AUTH_TOKEN="replace-with-your-deepseek-api-key"
export ANTHROPIC_MODEL="deepseek-flash[1m]"
```

模型名和可选别名配置以当前
[DeepSeek Claude Code 集成文档](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)
为准。用 `chmod 600 ~/.cc-worker.env` 限制文件权限。
该文件作为可信 shell 代码在执行器子进程内加载，其中的赋值覆盖继承的环境变量。
`CC_WORKER_ENV_FILE=/path/to/file` 可指定其他文件，`CC_WORKER_ENV_FILE=''` 可禁用加载。
`--model` 优先于模型配置。

本地诊断不会调用模型，也不显示凭据值：

```bash
RUNNER="$HOME/.agents/skills/cc-worker/scripts/run-cc.sh"
"$RUNNER" doctor
```

Claude CLI 需支持 JSON 输出、追加系统指令、`acceptEdits` 和工具选择。
缺少必要能力时明确报错，不会自动回退到跳过权限检查。相关机制见
[Claude Code 自动化文档](https://code.claude.com/docs/en/headless)。

## 直接调用执行器

通常由 Codex 创建任务文件。手动调用时，将 JSON 放在目标仓库之外，
并将以下示例路径和检查命令换成实际项目内容：

```json
{
  "id": "csv-export",
  "prompt": "Implement CSV export with standard escaping and an empty-result header. Preserve existing public APIs and cover these behaviors in the existing tests.",
  "read_files": ["src/types/user.ts"],
  "write_files": ["src/services/user.ts", "tests/user.test.ts"],
  "checks": [["npm", "run", "typecheck"], ["npm", "test", "--", "user.test.ts"]]
}
```

路径为相对仓库根目录的具体文件。检查命令使用参数数组，直接执行，不隐式经过 shell。
worker 结束后由执行器运行检查；`checks` 为空表示未执行 runner 检查。

```bash
"$RUNNER" run --task /tmp/task.json --workdir /path/to/project --isolated
```

并行清单采用 `{"tasks": [...]}`。任务之间的写入/写入、写入/读取路径重叠会在派单前被拒绝；
隐藏的语义依赖仍需 Codex 判断。

```bash
"$RUNNER" batch --manifest /tmp/tasks.json --workdir /path/to/project --jobs 2
```

保存返回的 `run_id`，检查补丁和验证结果后再集成：

```bash
"$RUNNER" inspect --run <run-id>
"$RUNNER" inspect --run <run-id> --patch
# 完成代码和验证证据审查后再应用。
"$RUNNER" apply --run <run-id>
# 集成完成或决定放弃后再清理。
"$RUNNER" cleanup --run <run-id>
```

需要修正时，将针对性指令写入文本文件并续接准确会话：

```bash
"$RUNNER" resume --run <run-id> --prompt-file /tmp/correction.txt
```

每个运行最多两轮 worker 执行，不会自动重试。某个并行任务失败不影响其他成功任务独立审查。
无法续接或第二轮仍失败时由 Codex 接管。多个结果集成后，需运行必要的组合检查。

`run` 不带 `--isolated` 时在原目录串行执行。旧调用
`run-cc.sh "<task prompt>" [workdir]` 保留文本输出，但没有结构化文件范围和 runner 检查。
原目录中的结果需要直接审查，不要再次应用补丁。

## 默认值、隔离与恢复

| 设置 | 默认值 |
| --- | --- |
| 并发 worker | 2，可通过 `--jobs` 调整 |
| worker 超时 | 每轮 900 秒，`--timeout` |
| 检查超时 | 每条命令 300 秒，`--check-timeout` |
| 轮次限制 | CLI 声明支持时默认 30，`--max-turns` |
| 模型 | 继承 Claude 配置，可用 `--model` 覆盖 |
| 状态目录 | 系统临时目录内的 `cc-worker-<uid>/`，权限 700 |

通过 `CC_WORKER_STATE_DIR` 指定目标仓库外的私有目录，可长期保留运行记录；
后续查看、续接、集成和清理时应使用相同配置。默认临时文件可能被系统清理。
完整结果保存在 `result.json`；每轮保留任务、CLI 输出、stderr 和检查日志。
日志可能包含项目源码及 provider 错误细节，不要提交到 Git。

快照通过临时索引和私有 Git commit 对象生成，不移动原 HEAD、不更新原暂存区，也不向
用户分支提交。包含当前已跟踪内容；未跟踪文件仅带入明确声明的输入与输出。
忽略文件、凭据和依赖目录不会复制。子模块需要单独工作流。
执行器不安装依赖、不共享可写依赖目录；需要时按项目已授权的流程准备 worktree 环境。

worktree 隔离文件，但共享 Git 元数据，不能隔离 shell、外部服务或任意忽略文件。
范围检查覆盖已跟踪和未被忽略的文件。worker 仅获得编码工具和声明检查命令对应的 Bash 允许规则；
现有 Claude 设置与 hooks 仍然生效。执行器以调用方现有权限运行检查，请仅使用可信仓库和已授权命令。

没有首个提交或非 Git 目录时，明确提示后降为原目录顺序执行，不自动创建首个提交。

应用补丁前检查原文件与声明参考是否变化、worker 文件是否在验证后变化、是否越界及补丁是否被改动，
再预检完整补丁。冲突时停止，不暂存、重置或覆盖用户修改。
失败任务保留 worktree；显式 `cleanup` 只移除本次运行拥有的 worktree，保留补丁与日志。
快照和集成期间应避免其他编辑器同时修改目标文件；执行器锁只协调自身命令。

退出码：成功为 `0`，任务或检查失败为 `1`，输入无效或操作被拒绝为 `2`，中断为 `130`。
完整接口和状态说明见 [dispatch reference](cc-worker/references/dispatch.md)。

## 开发验证

```bash
bash -n install.sh cc-worker/scripts/run-cc.sh
python3 -m unittest discover -s tests -v
```

测试使用临时 Git 仓库和模拟 `claude`，不访问网络、不消耗模型用量。
CI 在 macOS、Linux 的 Python 3.9 和 3.13 上运行同一套测试。
可安装的 skill 包为自包含的 `cc-worker/`，其中全部使用英文；测试与仓库文档放在包外。

[MIT License](LICENSE)
