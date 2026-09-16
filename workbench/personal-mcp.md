# 个人 MCP

个人 MCP 与个人 Skill 一样，以当前账号沙盒中的文件为配置来源。管理员在 Dify 后台配置的全局插件继续通过现有工具链调用；个人 MCP 通过账号自己的沙盒连接服务，并合并到该账号的插件目录。

## 使用

在工作台的插件选择面板中点击“管理个人 MCP”，添加配置或导入 JSON，保存后测试连接。测试成功后可以查看工具参数、启停、置顶、编辑、打开文件夹、删除或在当前对话中点名使用。

导入接受单个配置对象，或包含 `mcpServers` 的配置文件。后者可选择一个服务导入；重复标识通过原条目的编辑入口更新。标识为 1–64 位小写字母、数字或连字符。

```text
/workspace/
├── mcp/<id>/mcp.json
├── .mcp-settings.json
├── .mcp-pins.json
├── .mcp-cache/<id>.json
└── .mcp-backups/
```

可直接编辑 `mcp.json`。配置内容改变后，旧工具缓存失效，需重新测试。界面中的“已验证”表示这份配置已取得工具声明，不代表服务当前持续在线。目录读取使用缓存，不会自动启动新配置或访问外部服务。

### 远程 Streamable HTTP

```json
{
  "name": "文档检索",
  "description": "查询个人文档索引",
  "transport": "streamable-http",
  "url": "https://mcp.example.com/mcp",
  "headers": {},
  "timeout": 60
}
```

`headers` 可填写服务要求的认证请求头。兼容旧 SSE 服务时，将 `transport` 设置为 `sse`，填写其 SSE 地址；`http` 是 `streamable-http` 的别名。当前认证方式为静态请求头，不含 OAuth 授权流程。

### 本地 stdio

```json
{
  "name": "文档工具",
  "transport": "stdio",
  "command": "python",
  "args": ["/workspace/mcp/document-tools/server.py"],
  "env": {},
  "cwd": "/workspace",
  "timeout": 60
}
```

命令通过参数数组直接启动；需要的包先安装到个人共享环境。命令查找优先使用个人 Python 和 Node 环境，然后使用 Office 环境和系统命令。`cwd` 必须在 `/workspace` 内，`timeout` 为 5–300 秒。协议 SDK 位于镜像内独立的 `/opt/workbench-mcp`，不随个人依赖更新而改变。

配置中的 `headers` 和 `env` 属于该用户自己的文件，文件空间和同一账号的 Shell 可以读取。资源目录、模型工具声明和常规错误信息不带这些值。这里提供账号之间的隔离；文件权限并不使凭据对同一账号的其他程序保密。

## 运行规则

- 新任务冻结当时已启用且已验证的工具声明。排队任务与暂停续跑沿用各自快照；新增工具在新任务中生效。
- 每次调用检查租户、账号、应用、工作台任务、当前执行身份和所属会话绑定，并重新核对启用状态、配置版本、工具声明版本与参数 schema。API 读取目录后再检查执行；Manager 在等待该 MCP 的调用锁后、连接准备完成后，分别向 API 确认执行仍然有效。授权不可用时不执行工具。
- 全局插件和个人 MCP 使用不同的资源标识；同名服务不合并。点名个人 MCP 后，沿用工作台已有的工具组访问要求。
- `isError`、`structuredContent` 和内容块保留到 Agent 工具结果；长结果继续受现有工作台输出机制管理。单次协议结果上限为 8 MiB。
- 同一配置的连接在用户容器内复用，空闲 10 分钟退出。同一个 MCP 的工具调用串行，不同 MCP 可并行；调用排队最多等待 20 秒，超出时返回未执行。账号共用锁仅保护短时容器和文件操作，不覆盖外部工具响应等待。
- 编辑、删除、停用和容器回收会取消受影响的排队及活动调用，并关闭连接。停止会话会取消该会话的 MCP 请求，其他会话保持独立。运行器同时监听 Manager 连接关闭，在工具执行中也能取消等待和清理本地进程。已提交到远程服务的操作仍需核实外部结果。
- 业务调用中断或超时返回“执行结果未知”，不自动重放。Manager 在调用前将执行标识摘要写入 `/state/mcp-journal.sqlite3`。结果最多保留 24 小时，每账号结果上限 64 MiB、全局 256 MiB；超限先清理最早的结果。结果清理后保留已受理标记，同一请求返回结果已不可用或未知，不重新执行。
- 去重记录上限为 100,000 条。达到上限时拒绝新增调用并提示管理员归档，既有记录仍用于阻止重放。归档需要先排空活动任务并明确旧执行的保留边界；不能直接删除记录来释放容量。旧版 `mcp-*` 文件会压缩为最小去重记录，原请求继续拒绝重放。清理每 30 秒运行，并在调用写入结果时执行容量检查。
- 删除将配置及同目录文件移入 `.mcp-backups`；编辑前保存旧配置。备份沿用用户卷生命周期，由用户按需要管理。

## 接口与实现

公开管理操作复用 `/workbench/resources` 的登录身份和 gxzs 现有代理路由。服务端解析所属工作区，客户端不能指定其他用户的沙盒。公开操作包括 `mcp_read`、`mcp_save`、`mcp_delete`、`mcp_toggle`、`mcp_pin`、`mcp_test`。

Agent 使用受内部凭据保护的 `/inner/api/agent/workbench/mcp`，由 `dify.workbench_mcp` 层提供真实工具 schema。该层仅在工作台任务中添加；调用经 API 授权后交给 Sandbox Manager，再通过 `docker exec --user 1000` 在所属沙盒中执行。运行器使用清空环境的固定解释器启动，平台内部令牌不继承到 MCP 进程。

| 文件                                                        | 职责                               |
| ----------------------------------------------------------- | ---------------------------------- |
| `api/services/workbench/personal_mcp.py`                    | 账号目录、任务快照、执行校验与路由 |
| `dify-agent/src/dify_agent/layers/workbench_mcp.py`         | Agent 工具声明与调用               |
| `workbench/sandbox-manager/mcp_ops.py`                      | 配置、开关、缓存与备份             |
| `workbench/sandbox-manager/mcp_runtime.py`、`mcp_worker.py` | 进程生命周期与 MCP 协议            |
| `workbench/sandbox-manager/mcp_journal.py`                  | 有界结果存储和持久化去重记录       |
| `workbench/sandbox-office/mcp-requirements.lock.txt`        | 锁定版本及哈希的运行依赖           |

当前覆盖 MCP tools 的发现和调用。MCP resources、prompts、交互式授权及 OAuth 不在此接口中提供。

Manager 的 `WORKBENCH_MCP_API_URL` 指向可访问的 API 内部地址，默认及 Compose 配置为 `http://wb-api:5001`。最终授权使用 `/inner/api/agent/workbench/mcp/authorize`，复用 Manager 与 API 已有的共享凭据，仅返回授权结果。API 需要能够处理等待工具响应的请求及独立的授权回调。

## 发布与回退

无需修改业务数据库或执行业务数据库迁移。Manager 会在已有持久化状态目录中创建 `mcp-journal.sqlite3`，该文件需随状态目录备份和保留。发布需要更新沙盒镜像、Sandbox Manager、API/Worker/Control、Agent 和 gxzs 前端；gxzs Java 代理无需新增路由。

1. 暂停新增派发并等待活动任务结束，记录现有镜像和配置。
2. 构建包含锁定 MCP SDK 的 `sandbox-office` 镜像，以及包含 `mcp_ops.py`、`mcp_runtime.py`、`mcp_worker.py` 和 `mcp_journal.py` 的 Manager 镜像。
3. 更新 Manager 的目标沙盒镜像，在原账号卷上按现有容器迁移流程重建停止的用户容器。运行中的旧镜像容器会被拒绝，不在其内部注入新运行器。
4. 更新 API、Worker、Control 和 Agent，再发布前端。验证个人配置、测试连接、一次工具调用、停用和另一个账号的不可见性。

回退时停止新增任务，等待当前调用结束，恢复前端和 API/Agent，再恢复 Manager/沙盒镜像。保留用户卷、配置、备份和 Manager 状态目录。回退到旧版本后个人 MCP 不可调用，文件仍保留供后续恢复。

## 验证入口

文件与协议测试在含锁定 SDK 的 Linux 沙盒镜像内运行：

```bash
/opt/workbench-mcp/bin/python -m pytest workbench/tests/test_personal_mcp.py workbench/tests/test_mcp_worker.py workbench/tests/test_mcp_manager.py workbench/tests/test_personal_resources.py
```

`test_mcp_worker.py` 启动临时 stdio、HTTP 和 SSE 服务，检查真实协议、参数、结构化结果、错误、连接复用、直接文件变更、停用、连接断开时取消及超时不重放。`test_mcp_manager.py` 检查停止请求与授权检查交错、排队请求重新授权、不同 MCP 并发，以及容量淘汰和重启后的防重放。API 测试位于 `api/tests/unit_tests/services/workbench/test_personal_mcp.py`；Agent 层测试位于 `dify-agent/tests/local/dify_agent/layers/test_workbench_mcp.py`。测试使用临时目录和示例凭据。
