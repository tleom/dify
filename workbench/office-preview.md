# 文件侧栏预览

## 实现与来源

工作台通过已有的账号鉴权文件接口读取文件，在当前文件侧栏中预览。文件不需要上传到公共 Office 查看服务。各类预览器按需加载，同一会话可以保留多个文件标签。

| 文件 | 当前实现 | 说明 |
| --- | --- | --- |
| Word：DOCX、DOCM、DOTX、DOTM | `docx-preview` 0.4.0 | 浏览器渲染，支持表格、图片、页眉页脚、显式分页；隔离文档样式，禁止脚本和外部资源加载 |
| Excel：XLSX、XLS、XLSB、XLSM、XLTX、XLTM | `@arcships/vue-xlsx` 0.6.0 | 只读模式、独立 Worker/WASM 解析、多工作表、公式、合并单元格和图表 |
| PowerPoint：PPTX、PPTM、PPSX、PPSM、POTX、POTM | `@arcships/vue-pptx` 0.6.0 | 连续浏览、翻页和缩放；禁止外部媒体，默认不自动播放 |
| PDF、图片、文本、Markdown、HTML、音视频 | 原有预览体系 | 共用文件标签、下载、重新加载、宽度调整和全屏 |

Word 的复杂自动分页、字体替代、Excel 高级图表与公式、PowerPoint 特殊效果可能与桌面 Office 有差异。宏不会执行。旧版二进制 DOC、PPT 仍需要下载，或先在文件空间转换为 DOCX、PPTX、PDF；不能将其标为已支持在线渲染。

研究依据：

- [Codex 文件预览说明](https://learn.chatgpt.com/docs/artifacts-viewer)：桌面侧栏预览文档、电子表格、演示文稿和 PDF。
- 本机 Codex `26.901.6511.0` 的安装包资源包含按需加载的 `docx-preview` 及专用表格、演示预览模块。这里只核对组件结构，没有复制产品内部实现；其专用表格和演示模块不能当作公开可复用的库。
- [Suna Word 预览](https://github.com/kortix-ai/suna/blob/main/apps/web/src/features/file-renderers/docx/docx-viewer.tsx)、[Excel 预览](https://github.com/kortix-ai/suna/blob/main/apps/web/src/features/file-renderers/xlsx/xlsx-viewer.tsx)：使用浏览器专用渲染器，表格采用 WASM/Worker。
- [DeepSeek Harness 文件预览](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-sidebar-documentpreview/README.md)：采用可扩展预览器和标签页，所核对的版本没有原生 Office 渲染器。
- [docx-preview](https://github.com/VolodymyrBaydalka/docxjs)、[Agentic Office UI](https://github.com/arcships/agentic-office-ui)：本实现采用的开源组件，许可证均为 Apache-2.0。依赖版本在前端锁文件中固定。

## Agent 工具契约

`open_file_preview(path)` 校验单个文件并向当前任务的事件流追加 `workbench_preview`。路径可以是当前会话相对路径，也可以是个人文件空间中的 `/workspace/...` 绝对路径。服务端核对租户、账号、应用、会话和当前执行编号；停止或替换执行后，旧调用无法继续打开侧栏。

`accepted: true` 表示预览请求已经持久化，不能证明浏览器已经完成渲染。相同调用编号的重试复用同一事件。前端只响应当前会话的新事件，历史重放和重复事件不会重新打开用户关闭的侧栏。已打开文件再次变化后，Agent 需要重新发起预览请求，前端按文件版本更新内容。

现有 `workbench_files` 继续用于查询文件和获取用户明确需要的下载地址。交付校验接受本次改动文件的预览请求，默认交付流程不再强制附带下载链接。下载入口保留在侧栏工具条。

## 待管理员手动采用的系统提示词

下面是独立建议稿。本次没有写入管理员发布的系统提示词。

> 生成、修改并验证文件后，调用 `open_file_preview` 打开主要交付文件，让用户在侧栏直接查看。多个主要产物可分别打开。默认只简要说明文件用途和完成结果，不再附下载链接；用户可以通过预览工具条下载，用户明确要求链接时再查询并提供实际下载地址。
>
> 工具返回 `accepted: true` 只代表预览请求已发送，请勿声称已确认用户端渲染成功。调用失败时重试或准确说明无法打开的原因，不要假装已经展示。不要猜测文件路径或拼接链接。
>
> Office 文件优先生成 DOCX、XLSX、PPTX。若用户明确要求旧格式，保留其要求，并按需要同时生成适合预览的版本。制作完成后先检查内容、排版和实际文件，再打开主要结果；预览能力不能代替文件质量检查。
