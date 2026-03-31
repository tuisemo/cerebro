# Changelog

All notable changes to Cerebro will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- `CHANGELOG.md` — version history tracking

---

## [2.1.0] — 2025-03-31

> **SDK Transport Pilot (Phase 3–4)**

### Added

- **`DROID_TRANSPORT` environment variable** — selects the Cerebro-to-Droid transport layer:
  - `cli` (default): spawns `droid exec` as a subprocess; all tasks use this path.
  - `sdk`:新鲜非文件操作任务走 `droid-sdk` Python 客户端（Phase 3 SDK transport pilot）。

  当 `DROID_TRANSPORT=sdk` 时：
  - 新鲜、非文件操作的对话任务使用 SDK transport。
  - 恢复会话（session resume）继续走 CLI transport（SDK `load_session()` 不重新应用当前配置的模型/自主等级）。
  - 文件操作任务继续走 CLI transport（权限审批 UX 依赖 CLI 事件结构）。

- **`droid-sdk>=0.1.2` dependency** — Python SDK 客户端，用于 SDK transport 路径。

- **SDK Permission Bridging** — SDK 发出的工具权限请求通过 Discord 交互桥接：
  - 高危命令（`rm -rf`、`format` 等）→ Discord 按钮审批。
  - 中等风险命令 → Discord 通知，3 秒后自动继续。
  - SDK 仅允许 `proceed_once` 单次授权选项。
  - 缺少 `proceed_once` 或关键信息时 fail closed（安全拒绝）。

- **SDK ask-user Bridging** — SDK 向用户请求补充信息时，通过 Discord Thread 内直接回复处理：
  - 限单问题请求；多问题请求安全拒绝，要求 Droid 改为逐条提问。
  - 仅任务发起人可回复（可通过配置放宽）。
  - 回复后任务继续执行。

- **`InteractionBridge` dataclass** (`cerebro/runner.py`) — 解耦 transport 与 Discord 交互的桥接层：
  - `request_permission(params) -> dict` — 权限审批回调。
  - `ask_user(params) -> dict` — ask-user 回调。

- **Transport factory** (`get_droid_transport_name()`, `create_droid_transport()`, `normalize_droid_transport_name()`) — 统一管理 transport 选择逻辑，支持未来扩展。

- **`flush_output()` public method** (`cerebro/handler.py`) — 暴露内部 `_flush()` 为公开 API，供外部调用方（如 SDK permission handler）在发送 Discord 消息前刷新缓冲文本。

### Changed

- **`runner.py` 重构** — `DroidTask` 不再直接持有 subprocess；改为持有可插拔的 `BaseDroidTransport` 实例：
  - `CliDroidTransport` — 现有 subprocess 行为（已重构）。
  - `SdkDroidTransport` — Phase 3 SDK transport 实现。
  - `BaseDroidTransport` — 两者共享的抽象基类，定义 `run()`、`kill()`、`is_running` 等接口。

- **`DroidEventHandler._on_completion` / `_on_error`** — 调用 `flush_output()` 替代直接调用私有 `_flush()`，保证缓冲文本在流结束时正确刷新。

- **启动警告日志** — 当 `DROID_TRANSPORT=sdk` 时，日志说明当前生效的 SDK 路径策略（新鲜非文件任务）和仍走 CLI 的场景（resume、文件操作）。

### Deprecated

- `DroidTask` 的旧构造函数 `DroidTask(cwd)` — 已废弃，请使用 `DroidTask(cwd, transport_name, interaction_bridge)`。

### Fixed

- **`_select_transport_for_task()` docstring** — 原写 "Phase 2 SDK PoC"，已更正为 Phase 3 策略说明，含 SDK resume 限制的技术原因（`load_session()` 不重新应用模型配置）。

---

## [2.0.0] — 2025-03-30

### Added
- 多场景任务模式：`repo:`（Git 克隆）、`workspace:`（指定目录）、临时目录、纯问答
- SQLite 任务持久化，支持崩溃恢复和继续对话
- Discord Thread 工作区隔离，防止 Git 冲突
- 智能风险分级审批机制（高危按钮确认 / 中等风险通知）
- 并发任务队列，超额自动排队
- 流式状态面板（TaskDashboard）
- 斜杠命令：`/task`、`/status`、`/new`、`/cleanup`
- Discord Thread 内附件处理（自动保存到工作区）
- 自动清理 24 小时+ 非活跃工作区

### Changed
- 完全重构代码结构，模块化设计
- 从头重写事件流解析和进程控制
- Windows 原生适配（编码容错、PowerShell/CMD 语法提示）

### Fixed
- ProactorEventLoop IOCP 线程安全问题（通过 `_thread_pool` 独立线程运行 subprocess）
- 消息截断问题（超长输出分页发送）
