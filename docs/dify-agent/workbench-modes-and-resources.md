# 工作台模式、任务清单与个人资源

## 使用方式

| 功能 | 入口 | 行为 |
| --- | --- | --- |
| 目标模式 | `/goal 完整目标` | 记录目标并启动执行。单轮结束后，只要目标仍有效、会话没有待处理输入且没有其他任务占用，就继续下一轮。 |
| 查看、暂停、恢复目标 | `/goal`、`/goal pause`、`/goal resume` | 查看状态不调用模型；暂停停止后续自动轮次，正在运行的工具到安全停点后结束。 |
| 修改、移除目标 | `/goal edit 新目标`、`/goal clear` | 修改生成新的目标版本，旧执行不能回写新版本；移除目标保留聊天记录。 |
| 计划模式 | `/plan`、`/plan 需要规划的任务` | 调研并形成完整计划。通过计划卡片选择“开始执行”或“继续规划”，可同时填写修改意见。 |
| 退出计划模式 | `/plan off` | 在没有待审计划时退出；已有待审计划时通过计划卡片作出选择。 |
| 手动压缩 | `/compact`、`/compact 要重点保留的信息` | 在任务静止时生成摘要，保留最近消息和完整工具调用关系。保存成功后才替换工作上下文，聊天记录仍可查看。 |
| 任务清单 | 对话顶部任务面板 | 模型通过 `todo_write` 整体更新清单，状态为待处理、进行中、已完成，同一时刻最多一项进行中。 |
| 个人记忆 | “记忆与技能” → “记忆” | 编辑 `/workspace/memory.md`，供同一用户的各个会话使用。模型请求开始时重新读取；用户明确要求记住、修改或忘记时可以更新文件。 |
| 个人技能 | “记忆与技能” → “个人技能” | 导入 `SKILL.md`、技能 ZIP 或文件夹，启用或停用技能；技能位于 `/workspace/skills/<name>`。 |
| 管理员资源 | “记忆与技能” → “全局资源” | 查看管理员发布的工具、知识库、技能与文件。个人入口不能修改这些定义。 |
| 文件空间 | 右上角“文件空间” | 在当前对话和个人空间间切换，按需展开目录，上传、建目录、添加附件、下载和预览文件。 |
| 显示主题 | 右上角主题菜单 | 浅色、深色、跟随系统；Markdown 表格、代码、数学公式、引用和任务列表随主题显示。 |

个人技能示例：

```text
my-skill/
├── SKILL.md
├── scripts/
└── references/
```

`SKILL.md` 必须包含 YAML 文件头：

```markdown
---
name: my-skill
description: 说明技能完成什么工作，以及什么时候使用。
---
# 操作步骤

按需读取 references 中的资料，再执行 scripts 中的脚本。
```

个人技能名使用小写字母、数字和连字符，最多 64 个字符。单个导入包最多 200 个文件、20 MiB，`SKILL.md` 和个人记忆各不超过 64 KiB。同名导入会先展示确认，使用版本比较防止覆盖并发修改，并保留旧包备份。

## 沙盒和资源边界

每个 Dify 租户与账号组合有独立工作空间。模型的可写范围是本人的 `/workspace`，默认工作目录仍是当前会话目录。没有明确要求时，任务文件留在当前会话；跨会话文件使用明确的 `/workspace/...` 路径。

管理员技能和文件来自发布时冻结的配置，由管理进程安装到 `/opt/workbench-global/<内容版本>/`，目录和文件由 root 持有，普通沙盒用户只有读取权限。个人技能与管理员技能使用显式 `personal`、`global` 命名空间，同名个人技能不能替换管理员包。工具凭据和知识库配置继续由服务端管理。

文件操作使用工作空间目录描述符定位，并拒绝符号链接、路径穿越和越界归档。另一账号的会话、模式状态、记忆、技能和文件不能通过传入账号或会话 ID 访问。文件交付链接仍由服务端签发，执行层按完整工作空间路径核验，不按文件名猜测。

## 源码研究与实现对应

参考 DeepSeek Harness 的 MIT 许可源码，基准提交为 `0d1f50007f9bca3f52b06e1c3074fa14d5fb0720`：[源码仓库](https://github.com/deepseek-ai/deepseek-harness)。前端借鉴其留白、排版、颜色和流式块缓存方式；相关许可证保留在前端仓库 `docs/third-party/deepseek-harness.txt`。

| 参考机制 | 本项目实现 | 适配原因 |
| --- | --- | --- |
| Goal 的目标 ID、版本比较和回合驱动 | `protocol/workbench_control.py`、API `services/workbench/control.py` | 将单进程激活状态改为持久化状态；通过现有会话锁、执行票据和调度恢复机制避免分布式重复执行。默认上限为 256 轮。 |
| Plan 的请求边界生效、完整计划提交 | `layers/workbench_control.py`、`runtime/workbench_control.py` | 模式指令放入 SDK 动态指令槽，避免污染历史；`exit_plan_mode` 使用现有可恢复的人类输入流程。无回答不会自动批准。 |
| Todo 整体替换和单一进行中步骤 | 协议校验、`todo_write`、`WorkbenchControls.vue` | 清单属于会话持久化状态；普通新用户轮次正式开始时重置，续跑与恢复保留。 |
| 摘要加近期历史、失败不破坏原上下文 | `runtime/manual_compaction.py` | 使用已安装的原生 Pydantic AI Harness 压缩接口，先保存检查点，再确认成功；无实际缩减时保留原历史。 |
| Markdown 尾部增量渲染 | `WorkbenchMarkdown.vue`、`lib/markdown.ts`、`markdown.css` | 使用 Vue、remark/GFM、KaTeX 与语法高亮；缓存已稳定的块，尾部重新解析；脚注和引用按整篇解析保证编号正确。 |
| 按需加载的侧栏目录与分类预览 | `WorkbenchFileTree.vue`、`WorkbenchFilePreview.vue` | 目录自然排序；Markdown 源码/预览、代码、图片、PDF、HTML、音频和视频各用相应视图；其他二进制格式提供下载。 |

个人资源参考 [Deep Agents memory middleware](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/memory.py) 的持续载入机制，以及 [skills middleware](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/skills.py) 的元信息目录加按需读取完整技能。此处增加了明确的用户归属和不可写的管理员资源目录，个人与管理员同名技能分别保留。

## 状态与恢复约定

- 目标完成必须由 `update_goal` 提交匹配的目标 ID 和版本，并确认任务清单全部完成；一段结束语不会自动完成目标。
- 用户跟进消息、待审计划、环境更新、暂停以及恢复中的任务优先于目标自动续跑。
- 计划模式与 DSH 一样是模型行为模式。模型仍具备调查所需工具，计划指令要求批准前不实施；它不是一个额外的操作系统只读沙盒。
- 上下文压缩不会删除历史聊天。检查点保存失败、模型失败、取消或压缩没有减少上下文时，保留原工作历史并显示结果。
- 控制写入绑定当前运行票据，旧执行和重放不能更新当前状态。周期调度公平轮转活跃目标；服务重启后继续处理已授权的目标。
- 个人技能停用后不再出现在模型目录，也不能经 `read_skill` 加载；本人仍可在个人文件空间查看这些文件。

## 升级与回滚

迁移 `wb20260916control` 在 `wb20260914events` 之后新增 `workbench_controls` 和 `workbench_commands`。先备份数据库，再执行迁移并更新 API、worker、控制调度、Agent backend 和 sandbox manager，最后更新 GXZS 网关及前端。

本次接口是增量扩展。回滚应用镜像时保留新增表和个人文件，旧版本可继续读取原有会话。旧 Agent 会重新使用较窄的会话写入范围；如需继续使用新个人资源功能，应重新部署匹配版本。
