# Dify Agent 工作台

当前维护基线为 Dify `1.17.1`（`8387590ace4a094de812b7847fc6a4c3a27cd52b`）。升级合并、数据库迁移和后续同步方式见 [1.17.1 升级记录](UPGRADE-1.17.1.md)。定制分支为 `workbench/main`；独立前端位于 [tleom/webapp-conversation](https://github.com/tleom/webapp-conversation/tree/workbench/main)。下文保留首次部署的实现说明和历史记录。

2026-09-11 并发、会话菜单、上传与语音功能更新见 [最新更新记录](CAPACITY-20260911.md)。

2026-09-11 界面更新已上线，当前使用方式与验证见 [界面更新记录](UI-20260911.md)。原隔离开发环境已清理，下文开发部署内容作为历史记录。

当前 `http://10.16.9.237:3100` 已接入原生产 Dify，原管理入口为 `http://10.16.9.237:8080`。请刷新并重新登录。当前生产配置、资源范围和回退方式以 [生产切换记录](PRODUCTION.md) 为准；以下保留首次隔离开发部署及实现说明。

基线：Dify `1.17.0`（`09a855dc`），前端 `tleom/webapp-conversation`（`33085b6608fe7174e0fb75e46c220348863b1c19`）。前端是独立仓库；本目录包含 Dify 扩展、沙盒管理服务及针对性测试。

## 使用

当前入口为 `http://10.16.9.237:3100`。使用原 Dify 邮箱和密码登录并新建会话。顶部选择模型，输入框工具栏选择技能、插件和知识库；多选浮层支持搜索，选择自动保存到当前会话。失效资源自动清理，新会话继承仍然可用的个人选择。Enter 发送，Shift+Enter 换行；文件空间入口位于右上角。输入框旁“文件”按钮直接上传到共享目录并附加到输入框。会话右侧菜单支持置顶、重命名、删除。

文件面板提供共享文件与会话目录，支持上传、下载、删除及选作消息附件。单文件上限 20 MiB；上传和删除检查内容版本并原子替换。任意 Shell 同时修改同一路径仍需协调。每人最多运行两个任务，超过后排队；同一会话同时只能运行一个任务。

安装共享依赖时，请让 Agent 使用 `update_shared_environment`。任务暂停并释放执行额度，同用户的其他任务结束后才安装。新环境验证成功后原子切换，失败保留原环境，再恢复原任务。Python 和 Node 依赖在同用户会话间共享。

## 模块与数据

- Next.js：`app/workbench` 为界面，`app/api/auth` 复用 Dify 身份，`app/api/workbench` 为白名单网关，`lib/workbench/session.ts` 将 Dify 令牌加密存入 Redis；浏览器只持有 HttpOnly 会话 Cookie。
- Dify：`api/controllers/console/workbench.py` 定义请求、响应及 OpenAPI；`api/services/workbench` 管理权限、配置、文件路由和公平队列；`api/tasks/workbench_tasks.py` 执行和恢复任务。
- 数据库：新增 `workbench_chats`、`workbench_revisions`、`workbench_runs`；迁移为 `wb20260910`、`wb20260910b`、`wb20260911`。会话的基础 Agent binding 版本与选择的公共资源版本分别保存。
- Agent：新一轮重建模型、工具、技能、知识层并保留历史；暂停续跑保留原配置。工作台引用的 Skills 固定到已发布归档，取消选择后清理该会话旧的技能缓存。
- 沙盒：`sandbox-manager` 按工作区和账号分配容器；用户容器不挂载 Docker socket。共享目录 `/workspace/shared`，会话目录 `/workspace/conversations/<binding>`，会话 Home `/home/dify/<binding>`。共享环境 `/opt/user-env` 在普通 Shell 中只读。

删除会话会退役其绑定、清理会话 Home 和工作目录，保留个人共享文件及依赖。用户容器空闲 30 分钟后停止，使用时恢复；初始限制 2 CPU、4 GiB、512 个进程。

## 配置与部署

本次仅部署独立开发栈。原 Dify 服务不接入新数据库或新沙盒卷。开发栈位于 `/home/jrgx/apps/dify-workbench`，前端运行目录 `/home/jrgx/apps/webapp-workbench`；机密配置位于 `workbench/dev`，该目录同时被 Git 和 Docker 构建上下文排除。

开发数据库是原库的隔离快照，包含克隆时已有的账号和资源；原库后续新增或变更不会自动同步到开发库。

`compose.yaml` 描述应用服务，依赖已准备好的 `wb-postgres`、`wb-redis`、`wb-plugin`、原行为兼容用的 `wb-local-sandbox` 及两个外部 Docker 网络。它用于后续受控切换；本次运行容器由部署脚本创建，不能在有活动任务时直接用 Compose 接管。

私有配置文件为 `api.env`、`worker.env`、`control.env`、`beat.env`、`agent.env`、`manager.env`、`frontend.env`。继续保留当前 Dify 的存储解密密钥和已存在的 MCP 服务兼容修补文件；不要把环境文件、测试账号密码或数据库导出提交到仓库。

关键配置：

| 配置 | 说明 |
| --- | --- |
| `WORKBENCH_ENABLED` | 默认关闭，API 与 Celery 服务一致配置 |
| `WORKBENCH_AGENT_TEMPLATES` | JSON：工作区 ID → 已发布的公共 Agent ID |
| `WORKBENCH_ALLOWED_ACCOUNTS` | JSON：工作区 ID → 灰度账号列表；空列表或未配置表示该工作区全部成员 |
| `WORKBENCH_TOOL_PARAMETERS` | JSON：工作区 → 工具 ID → 参数 → JSON Schema；默认不开放任何工具参数 |
| `WORKBENCH_PER_USER_RUNS` / `WORKBENCH_GLOBAL_RUNS` | 每人同时 2 个任务、全局同时 20 个任务；工具调用不单独计数 |
| `WORKBENCH_MAX_ACTIVE_USERS` | 默认同时运行任务的用户最多 10 人，超出人数或任务数自动排队 |
| `WORKBENCH_SANDBOX_MANAGER_URL` / `WORKBENCH_SANDBOX_MANAGER_TOKEN` | 仅内部网络使用；API、Agent、Manager 的密钥一致 |
| `DIFY_AGENT_WORKBENCH_MANAGER_ENDPOINT` / `DIFY_AGENT_WORKBENCH_MANAGER_TOKEN` | Agent 的管理服务配置 |
| `WORKBENCH_RUNTIME_CONTAINER` | 固定为实际单进程执行器容器名，本次为 `wb-agent` |
| `WORKBENCH_SANDBOX_CPUS` / `WORKBENCH_SANDBOX_MEMORY` | 默认 `2` / `4g` |
| `DIFY_CONSOLE_API_URL` | 前端服务端访问地址，本次 `http://wb-api:5001/console/api` |
| `WORKBENCH_PUBLIC_ORIGIN` | 浏览器访问的完整 Origin；正式入口使用 HTTPS |
| `WORKBENCH_REDIS_URL` / `WORKBENCH_SESSION_KEY` | 前端会话 Redis 地址及 32 字节 Base64 加密密钥 |

开发 PostgreSQL `max_connections=80`、`dify_app CONNECTION LIMIT 60`。配置预算为 API 8、执行 Worker 32、控制 Worker 4、Beat 2、当前 Plugin 10，总计 56；不得按进程扩容后仍沿用这份预算。五用户验收采样峰值为 38。后续新建 Plugin 配置可收紧至 6，总预算 52。

Redis 使用独立持久卷及 AOF，`appendfsync always`。队列、执行票据、会话令牌使用不同 DB/命名空间。执行票据保留，任务结果到期也不会自动重做。执行器失联时，管理服务核对容器启动代次，终止该会话的遗留 Shell 后释放租约；无法确认停止时保留占位。此实现面向一个固定名称、单进程的 Agent 执行器，扩展多实例前需要调整执行器所有权协议。

正常更新先暂停 Beat/控制队列派发，等待活动任务完成，再更新 API/Worker/Agent；不要强制终止用户任务。迁移在启动新版 API 前执行。使用固定镜像标签并记录镜像 ID，见交付目录的镜像清单。公共模板更新后，旧会话可保存新的资源选择，已入队任务继续使用原快照。

标准构建入口：

```sh
docker build -f workbench/Dockerfile.api -t dify-workbench-api:1.17.0-20260910.1 .
docker build -f workbench/Dockerfile.agent -t dify-workbench-agent:1.17.0-20260910.1 .
docker build -t dify-workbench-manager:20260910.1 workbench/sandbox-manager
# 在前端仓库执行
docker build -t dify-workbench-web:33085b66-20260910 .
```

本机 Docker Hub 基础镜像下载较慢，前端和 Manager 的已验证镜像使用 `Dockerfile.cached`，基于已缓存的固定 Dify 沙盒镜像；Manager 挂载宿主 Docker CLI。标准 Dockerfile 同时保留。

## 验证与回退

针对性检查包括真实 Redis 队列和幂等票据、文件版本/路径隔离、配置编译、原 Agent 请求构建与技能层回归、OpenAPI 响应契约。前端分别执行 `pnpm typecheck`、`pnpm lint`、`pnpm build`，已移除忽略类型/lint 错误的构建设置。仓库完整 Docker 集成测试仍由 CI 运行，本次不声称 CI 已通过。

真实环境验收覆盖双账号各两个会话、模型/插件/MCP/Skill/向量知识检索、取消选择、共享 Python/Node 依赖及失败回滚、两并发加第三个排队、定向停止、执行器硬重启、重复发送、五用户十任务、跨账号/工作区与文件路径拒绝、删除会话后保留共享文件。详细结果与日志摘要见交付报告。

本次为首次独立开发部署。回退时先停止新增派发并等待活动任务结束，关闭工作台入口和 `WORKBENCH_ENABLED`，继续使用保持原镜像运行的原 Dify 服务。后续工作台版本升级时，将本次 `images.json` 记录的镜像作为上一版恢复。保留新增表、Redis 持久卷、`manager-state` 以及所有 `dify-wb-dev-<workspace>-{home,files,env}` 卷。关闭功能开关不会删除数据；不要通过删除卷完成回退。

监控：排队时长、每用户/全局活动租约、过期但未释放的租约、`workbench_runs` 的失败/中断状态、共享环境更新失败、PostgreSQL 角色连接数和总连接数、上游模型限流、用户容器 CPU/内存。历史向量 `403` 在本轮直接 Embedding 和真实知识检索中均未复现；没有替换或修改上游模型密钥。
