# Dify 1.17.1 工作台升级

官方基线：`1.17.1`，提交 `8387590ace4a094de812b7847fc6a4c3a27cd52b`。

定制分支 `workbench/main` 保留完整官方历史：先记录 1.17.0 上的工作台改动，再合并官方 1.17.1。后续升级继续在该分支合并官方版本标签。

## 合并内容

保留账号隔离、个人沙箱及共享依赖、会话独立配置、并发队列、恢复与停止、附件、语音、模型思考参数、原生消息反馈和重新生成。MCP 管理页接受服务标识与 UUID 的兼容修复已纳入源码。

模板权限检查采用 1.17.1 的 `enforce_rbac_checks`，按 `AgentId` 校验 `AGENT_TEST_AND_RUN`，与上游 Agent 运行权限保持一致。

上游新增会话快照层校验。普通 Agent 会话继续执行该校验；工作台新一轮按当前配置重建运行层并恢复历史，允许上一轮的配置层结构不同。延期工具续跑仍要求原配置层匹配。

迁移节点 `wb20260911u171` 合并已部署的 `wb20260911` 与官方 `c3f1a9b2e6d4`，保留原迁移历史。升级同时执行官方邮箱规范化、旧模型类型迁移和知识库 API Token 范围绑定表迁移。

## 构建与部署

```sh
docker build -f workbench/Dockerfile.api -t dify-workbench-api:1.17.1-20260911.4 .
docker build -f workbench/Dockerfile.agent -t dify-workbench-agent:1.17.1-20260911.1 .
docker build -f workbench/sandbox-manager/Dockerfile.cached -t dify-workbench-manager:1.17.1-20260911.1 workbench/sandbox-manager
```

独立前端在其仓库构建。官方管理前端使用 `langgenius/dify-web:1.17.1`；个人沙箱及官方本地沙箱使用 `langgenius/dify-agent-local-sandbox:1.17.1`。

部署前备份数据库与私有 Compose 配置；在隔离的生产库副本验证迁移。暂停派发并排空执行任务后，停止写入服务、刷新数据库备份、通过新版 API 镜像运行 `uv run --project /app/api --no-sync flask db upgrade`，再切换 API、Worker、Agent 和前端。

私有环境文件、会话令牌、备份和用户卷保持在仓库之外。现有用户卷复用；旧沙箱容器可停止并改名暂存，便于回退。恢复旧镜像时保持自动迁移关闭并保留已升级的表结构。

## 升级验证

隔离镜像测试通过：API 99 项、Agent 27 项。覆盖工作台配置、模板运行权限的允许与拒绝、原生图片及普通附件、停止后历史、消息操作、并发准入、幂等票据、上游请求构建及配置层。

生产备份恢复到临时 PostgreSQL 后，迁移到 `wb20260911u171` 成功；账号、会话、消息、工作台会话/任务/修订和上传文件数量保持一致。规范化邮箱及知识库 Token 绑定表检查通过，再次执行升级成功。

独立前端的类型检查、lint、生产构建和工作台交互单元检查通过。lint 保留原组件中的既有警告。完整上游 CI 未运行。

生产已部署 1.17.1，迁移版本为 `wb20260911u171`。切换时账号、会话、消息和工作台记录数量保持一致。实际验证了停止后继续发送、上下文、反馈、重新生成、账号隔离、Shell、原生代码工具及 Tavily 插件。当前发布模板没有 MCP 工具，本轮没有执行真实 MCP 调用。

图片经账号所属沙箱及文件版本校验后，进入 Dify 原生上传文件与多模态消息通道；普通附件仍使用沙箱路径。重新生成保留原图片引用。浏览器实测即时预览和粘贴阶段无上传，发送时才上传；桌面与移动端缩略图均为 44px 高，上传失败保留草稿。线上模型直接识别测试图片中的颜色、形状和数字，未调用工具读取图片。

原生图片不再追加沙箱路径提示；混合附件仅追加普通文档路径。只发送图片时使用“请描述图片。”作为默认问题。任务的附件记录和文件操作能力保持可用。

使用此前触发 RGB 分析的图片，在新会话以 `qwen3.8-flash` 实测“这是啥”和仅上传图片，两次均直接描述画面，工具调用为 0。测试未添加禁止工具的指令，也未更改账号的模型与工具默认选择。

## 同步下一版官方代码

首次配置远端：

```sh
git remote add upstream https://github.com/langgenius/dify.git
```

每次升级：

```sh
git fetch upstream --tags
git switch workbench/main
git merge <官方版本标签>
```

检查冲突、依赖版本与数据库迁移分支，更新基础镜像版本，执行工作台及受影响的上游测试，预演迁移后构建部署。验证完成后 `git push origin workbench/main`。`main` 保留为官方同步分支，定制改动提交到 `workbench/main`。
