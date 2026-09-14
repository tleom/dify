# Dify Agent 工作台

当前维护基线为 Dify `1.17.1`，定制分支为 `main`。本目录维护 Dify 工作台 API、任务调度、Agent 和沙盒管理能力。升级过程见 [1.17.1 升级记录](UPGRADE-1.17.1.md)。

## 入口与身份

此入口依赖 [公信签名身份接入](https://github.com/tleom/dify/pull/1)。部署前须先包含该变更并验证 gxzs 登录用户能通过 `/ai/workbench/**` 读取自己的会话，再应用本目录的独立网页移除配置。仅使用旧版 Dify console-session 鉴权的 API 不满足此部署前提。

用户从 gxzs 的智能问答页面使用工作台。Vue 页面调用 gxzs `/ai/workbench/**`，gxzs 后端核对登录状态、租户与权限后签署请求，Dify 根据签名身份访问当前用户的聊天和文件。

独立网页已于 2026-09-13 卸载，源码保存在 [GitHub 归档仓库](https://github.com/tleom/webapp-conversation)。站内前端由 [gxzs-frontend](https://github.com/tleom/gxzs-frontend) 维护，身份代理由 [gxzs-backend](https://github.com/tleom/gxzs-backend) 维护。Dify 管理入口继续用于配置模型、Agent、工具与知识库。

## 模块与数据

| 模块                                   | 职责                                   |
| -------------------------------------- | -------------------------------------- |
| `api/controllers/console/workbench.py` | 工作台请求、响应及 OpenAPI             |
| `api/services/workbench`               | 权限、配置、文件路由、历史与公平队列   |
| `api/tasks/workbench_tasks.py`         | 任务执行和恢复                         |
| `dify-agent`                           | 模型、工具、技能、知识检索与任务运行   |
| `workbench/sandbox-manager`            | 按工作区和账号管理容器、目录和共享环境 |

聊天、修订和任务分别保存在 `workbench_chats`、`workbench_revisions`、`workbench_runs`。新一轮按当前配置重建运行层并恢复历史；暂停续跑使用原配置。已排队任务保留入队时的配置快照。

用户容器不挂载 Docker socket。共享目录、会话目录、Home 和依赖环境按用户隔离。删除会话会清理其绑定及会话目录，个人共享文件和依赖继续保留。依赖安装由 `update_shared_environment` 协调任务暂停、环境验证、切换和失败恢复。

工作台保留已发布 Agent 的 CLI、环境声明和配置文件；选择相同模型时继承其参数，再应用用户明确设置的参数。切换模型时使用目标模型的参数。环境用法可复制 [环境提示词](AGENT_ENVIRONMENT_PROMPT.md) 后由管理员维护；共享依赖更新及禁止私有替代环境的约束仍由系统层提供。

知识库检索沿用数据库中的 Top-K、重排序、阈值、权重和元数据过滤配置，底层搜索方式继续由知识库决定。选择知识库后允许先读取附件、准备查询、调用其他工具或询问用户；最终结论前须尝试使用所选知识库。访问失败作为明确的失败结果交给 Agent，不作为空结果，也不强迫重复调用。

搜索返回命中的完整内容，超过单页长度时通过 `knowledge_base_read_results` 继续读取同一次结果（保留本轮最近五次搜索）。需要全文或完整列表时，使用 `knowledge_base_list_documents` 和 `knowledge_base_read_document` 分页读取当前授权、符合过滤条件的索引内容。每页重新检查任务归属与访问权限，并报告未完成索引的内容。完整读取索引不等于确认原始附件已全部解析；外部知识库没有文档枚举接口时会明确说明限制。

## 配置与部署

`compose.yaml` 是应用服务示例，包含 API、Worker、Control、Beat、Agent 和 Manager。数据库、Redis、Plugin、存储、私有 env 文件及外部网络须单独准备。现有部署使用自己的 Compose 叠加配置，更新前核对实际镜像、挂载、环境与活动任务。

私有配置文件为 `api.env`、`worker.env`、`control.env`、`beat.env`、`agent.env`、`manager.env`。环境文件、账号密码、模型凭据和数据库导出不得提交到仓库。

| 配置                                                                           | 说明                            |
| ------------------------------------------------------------------------------ | ------------------------------- |
| `WORKBENCH_ENABLED`                                                            | API 与 Celery 服务一致配置      |
| `WORKBENCH_AGENT_TEMPLATES`                                                    | 工作区到已发布公共 Agent 的映射 |
| `WORKBENCH_ALLOWED_ACCOUNTS`                                                   | 工作区灰度账号列表              |
| `WORKBENCH_TOOL_PARAMETERS`                                                    | 可配置工具参数的 JSON Schema    |
| `WORKBENCH_PER_USER_RUNS` / `WORKBENCH_GLOBAL_RUNS`                            | 用户及全局运行额度              |
| `WORKBENCH_MAX_ACTIVE_USERS`                                                   | 同时运行任务的用户数上限        |
| `WORKBENCH_SANDBOX_MANAGER_URL` / `WORKBENCH_SANDBOX_MANAGER_TOKEN`            | 内网管理服务及鉴权              |
| `DIFY_AGENT_WORKBENCH_MANAGER_ENDPOINT` / `DIFY_AGENT_WORKBENCH_MANAGER_TOKEN` | Agent 使用的管理服务            |
| `WORKBENCH_RUNTIME_CONTAINER`                                                  | 固定单进程执行器容器名          |
| `WORKBENCH_SANDBOX_CPUS` / `WORKBENCH_SANDBOX_MEMORY`                          | 用户容器资源限制                |

Agent 使用的 Redis、任务记录、数据库和用户持久卷属于执行服务。维护时保留这些数据，按目标版本核对连接池预算、并发额度和迁移顺序。

更新前暂停新增派发并等待活动任务完成，再更新 API、Worker 与 Agent；采用固定镜像并记录镜像 ID。构建入口为 `workbench/Dockerfile.api`、`workbench/Dockerfile.agent` 和 `workbench/sandbox-manager/Dockerfile`。

## 验证与维护

范围匹配的检查应覆盖身份和权限、任务额度、票据幂等、停止与恢复、文件路径与版本隔离，以及变更影响的 Agent 行为。后端命令使用 `uv run --project api`；完整集成测试由 CI 执行。

故障回退先停止新增派发，等待活动任务结束，再恢复已记录的服务镜像与配置。保留新增表、Redis 持久数据、管理状态和用户卷。监控排队时长、活动租约、任务失败或中断、共享环境更新、数据库连接数和模型限流。

本目录带日期的界面、并发、交互与生产切换文档记录当时的验收结果，不作为当前运行入口或镜像清单。
