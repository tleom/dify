# Dify Agent 工作台生产切换记录

2026-09-10 17:50（北京时间）完成切换，目标主机为 SSH 别名 `237`。

## 入口与数据

- 生产工作台：<http://10.16.9.237:3100/>。刷新页面后，使用原 Dify 邮箱和密码重新登录。
- 原 Dify 管理入口：<http://10.16.9.237:8080/>。
- 工作台直接连接原生产 `dify` 数据库，复用原插件服务、模型凭据及文件存储。当前两个真实成员均已开放。
- 资源模板使用生产已发布的 `test` Agent。当前目录包含 90 个支持工具调用的模型、63 个工具。该生产模板尚未绑定 Skills 和知识库，这两项暂为空；管理员可在原 Dify 中绑定并发布后提供给工作台。
- 开发数据库、测试账号、历史会话和沙盒卷保留在独立开发栈，没有合并进入生产。开发前端 `wb-web` 已停止，占用的 `3100` 端口由生产工作台接管。

## 变更

原 `api`、`api_websocket`、`worker`、`worker_beat` 使用 `dify-workbench-api:1.17.0-20260910.1`；原 `agent_backend` 使用 `dify-workbench-agent:1.17.0-20260910.1`。新增生产专用 `workbench_control`、`workbench_manager`、`workbench_redis`、`workbench_web`。

数据库仅执行新增工作台表及修订字段的迁移，版本为 `wb20260910b`。没有导入开发库，也没有修改现有账号密码。

生产用户沙盒采用 `dify-wb-prod` 前缀，使用独立网络 `dify-workbench-production-sandboxes` 和持久卷。运行票据及前端登录会话使用生产专用 Redis；原 Redis 已启用的 AOF 调整为 `appendfsync always` 并写回配置文件。单用户并发为 2，全局为 10。

原执行 Worker 同时接收原业务队列及 `workbench`，并发设为 11；单独的控制 Worker 并发为 3。主数据库角色连接池预算为 API 8、WebSocket 2、执行 Worker 32、Beat 2、控制 Worker 4、原 Plugin 10，共 58，低于角色上限 60。全库 `max_connections` 保持 100。

持久配置：

- `/home/jrgx/apps/dify/docker/docker-compose.workbench.yaml`
- `/home/jrgx/apps/dify/docker/.env` 中的 `COMPOSE_FILE` 指向原两个 Compose 文件及新增覆盖文件。
- `/home/jrgx/apps/dify-workbench/workbench/production/` 保存私有环境配置、运行状态、备份和验证记录；目录权限为 0700，机密文件为 0600，并已排除出 Git 和 Docker 构建上下文。

## 本轮验证

- 两个现有成员的身份接口及生产资源目录读取成功；使用短时服务端签发的验证会话，未读取或修改用户密码。
- Kimi 实际完成个人沙盒 Shell、`current_time` 插件及只读 `yuandian_list_apis` MCP 调用，运行状态为 `completed`。
- 相同发送键返回同一个任务；另一成员读取验收会话返回 404。
- 验收会话删除接口实际返回 204；数据库确认已软删除，个人最近选择恢复为验收前的选择。验收脚本最初误把删除成功码写为 200，已纠正，并独立核实实际删除结果。
- 工作台首页 200、未登录身份接口 401、原 Dify 登录页 200、原系统功能接口 200。
- Edge 新页面显示正常生产登录页。密码登录由用户重新登录；本轮不把服务端签发会话等同于人工密码登录验收。
- 验收后两次采样：`dify_app` 分别为 12/60、13/60，全库分别为 45/100、46/100。这些值是即时采样，不是生产并发压测峰值。

此前五用户十任务、停止/重启恢复、共享依赖及知识检索等完整验收在隔离开发环境完成，详见原交付报告；本次没有在生产重做五用户压测，也没有运行完整 Docker CI。

## 备份与回退

备份目录：`/home/jrgx/apps/dify-workbench/workbench/production/backup-20260910T094340Z`。

包含原 `.env`、Compose 配置、容器信息、Redis 配置、MCP 修补文件、应用文件存储及三个数据库的自定义格式备份；三个数据库备份均通过 `pg_restore --list` 检查。

回退脚本保存在 `/home/jrgx/apps/dify-workbench/workbench/production/rollback_production_cutover.py`。先停止新请求并等待原业务任务和工作台任务结束，再运行该脚本。脚本会拒绝仍有工作台未完成任务的回退，恢复原 API/Agent 镜像和开发前端，保留新增表与生产用户持久卷。由于新增迁移记录仍保留，旧镜像通过 `MIGRATION_ENABLED=false` 启动，避免旧迁移代码无法识别新增版本。

常规维护在 `/home/jrgx/apps/dify/docker` 使用 `docker compose`；更新前先排空任务。不要删除数据库表或 Docker 卷来回退。

验证明细见同目录 `production-verification.json`。
