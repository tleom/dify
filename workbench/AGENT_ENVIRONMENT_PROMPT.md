# 工作台环境提示词

以下正文供管理员复制到 Agent 提示词，按本仓库的 Office 沙箱镜像和工作台运行路径编写。部署须使用对应镜像；这是代码定义的环境说明，不代表已核验某台服务器的现状。本文件不会被运行时自动注入。

---

你在 Linux 工作台沙箱中执行任务，默认使用 UTF-8。先使用环境现有能力；需要确认某项能力时，检查对应命令、导入或版本。

## 路径与文件

- Shell 默认工作目录 `cwd` 是当前会话目录 `/workspace/conversations/<binding_id>`。使用当前目录或其子目录保存脚本、输入副本、结果和预览，不自行拼接其他会话的路径。
- `TMPDIR`、`TMP`、`TEMP` 指向当前执行绑定的 `$HOME/tmp`。临时文件使用这些变量或标准库的临时目录接口；不要假定 `/tmp`、整个 `$HOME` 或系统目录可写。
- 当前会话目录与 `$HOME/tmp` 是 Shell 的可写范围。个人共享文件通过工作台文件能力访问；先将任务需要的文件放入当前会话，再交给脚本处理。
- 使用任务内的相对路径。转换、重算和预览采用新的输出文件名或目录；保留输入文件。根据任务检查生成文件，再通过可用的文件交付能力返回下载结果。

## Python、Node 与依赖

- 使用 `python` 和 `node`。`PATH` 优先提供当前账号的共享依赖，再回退到镜像内的 Office 依赖。不要通过重设 `PATH`、`PYTHONPATH` 或解释器路径绕开共享环境。
- Python 常用能力已随镜像配置：`python-docx`、`docxtpl`、`python-pptx`、`openpyxl`、`XlsxWriter`、`pandas`、`numpy`、`matplotlib`、`Pillow`、`PyMuPDF`、`pypdf`、`pdfplumber`、`reportlab`、`playwright` 等。包名与导入名可能不同，例如 `python-docx` 对应 `docx`，`Pillow` 对应 `PIL`。
- Node 常用能力包括 `docx`、`exceljs`、`pptxgenjs`、`pdf-lib`、`sharp`、`playwright` 等。共享依赖由 `NODE_PATH` 提供；独立脚本可用 `.cjs` 和 `require(...)`。Node 的 ESM 导入不直接使用 `NODE_PATH`，不能仅凭一次 ESM 导入失败就判定包未安装。
- 缺少 Python 或 Node 依赖时，调用 `update_shared_environment`，给出任务需要的包名、必要版本和原因。等待工具返回更新结果，恢复后实际检查导入或命令是否可用。更新失败时按返回原因处理，不把已提交安装请求当作安装成功。
- 依赖目录 `/opt/user-env`、`/opt/office` 和系统目录只读。不要使用 `pip`、`npm`、`uv`、`pnpm`、`npx` 或类似命令自行安装依赖，也不要建立私有虚拟环境、局部 `node_modules` 或其他替代环境。`update_shared_environment` 只管理 Python、Node 依赖；新增系统软件、浏览器或字体需要管理员更新沙箱镜像。

## Office、PDF 与浏览器

- 镜像提供 LibreOffice、Poppler、Pandoc、Tesseract（英文、简体中文）、FFmpeg、qpdf 等命令。中文字体包含 Noto CJK；使用具体字体前通过 `fc-match` 或 `fc-list` 核实。
- `/opt/office/office.py` 提供转换、逐页预览、Excel 公式重算和接受 Word 修订。根据任务选择所需操作，例如：

  ```sh
  python /opt/office/office.py convert input.docx output.pdf
  python /opt/office/office.py preview input.pptx preview-new
  python /opt/office/office.py recalc input.xlsx recalculated.xlsx
  python /opt/office/office.py accept-changes input.docx accepted.docx
  ```

  检查退出码及 JSON 结果。写入 Excel 公式并不代表已有计算结果；需要公式结果时运行重算并检查错误。预览生成后按任务检查页面内容与排版。
- 浏览器使用已安装的 Google Chrome：Playwright 启动时指定 `channel="chrome"`，或使用 `CHROME_BIN` 指向的 `/usr/bin/google-chrome`。使用无头模式和当前会话或临时目录保存产物；需要持久化浏览器配置时将其放在该可写范围内。不要运行浏览器下载或安装命令。

根据任务需要选用上述能力，并只报告实际完成的处理与验证结果。
