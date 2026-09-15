# Workbench Office 沙箱

此目录保存用户沙箱的 Office、PDF、图片、数据分析和浏览器运行环境。基础镜像固定到 digest，Python 和 Node 依赖分别使用 `requirements.lock.txt` 和 `package-lock.json` 安装。

## 内置能力

| 工作                             | 工具与库                                                                                                               |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Word 生成、模板填充、合并        | python-docx、docxtpl、docxcompose、Node docx、Mammoth、LibreOffice Writer                                              |
| Excel 读写、样式、图表、公式重算 | openpyxl、XlsxWriter、pandas、python-calamine、xlrd、ExcelJS、LibreOffice Calc                                         |
| 演示文稿                         | python-pptx、PptxGenJS、LibreOffice Impress                                                                            |
| PDF 提取、合并、分页、渲染       | PyMuPDF、pypdf、pdfplumber、pikepdf、ReportLab、Poppler、qpdf                                                          |
| 数据分析与计算                   | NumPy、SciPy、pandas、DuckDB、PyArrow、numpy-financial、SQLite                                                         |
| 图表及关系图                     | Matplotlib、Seaborn、Plotly + Kaleido、Altair + vl-convert-python、ECharts、D3、NetworkX、Graphviz、pydot、Mermaid CLI |
| SVG、图片与缩略图                | Pillow、CairoSVG、svglib、resvg、Sharp、librsvg、ImageMagick                                                           |
| HTML、Markdown 和排版打印        | Chrome、Playwright、WeasyPrint、BeautifulSoup、lxml、html5lib、cssselect、Cheerio、Markdown-it、highlight.js、KaTeX    |
| 前端页面构建                     | React、TypeScript、esbuild、Tailwind CSS CLI、Sass                                                                     |
| 其他办公材料                     | extract-msg、striprtf、cn2an、python-magic、Pandoc、antiword、7-Zip、JSZip、ExifTool                                   |

实际版本可读取 `/opt/office/requirements.lock.txt`、`/opt/office/node/package.json`、`/opt/office/system-packages.txt`。用户自己的 Python/Node 环境仍优先使用，内置依赖作为共享的只读基础环境。

Mermaid CLI 单独安装在 `/opt/office/mermaid`，依赖由该目录的 `package-lock.json` 固定，以兼容其传递依赖要求的 Playwright 版本；应用使用的 Playwright 保留在 `/opt/office/node`。两者均使用系统 Chrome。

Word 的 Python 导入名称是 `docx`，例如 `from docx import Document`。合并 Word 可使用 `from docxcompose.composer import Composer`；`docxtpl` 用于现有模板填充。金额大写可使用 `cn2an.an2cn('1234.56', 'rmb')`。

Excel 写入公式后使用 `python /opt/office/office.py recalc input.xlsx calculated.xlsx` 得到真实计算值。`openpyxl` 本身只读写公式，不执行公式计算。

`mmdc -i flow.mmd -o flow.svg` 可直接导出流程图，启动器使用镜像中的 Chrome 和固定浏览器配置。Plotly 的 `write_image()` 可生成 PNG/SVG/PDF；Altair 的 `save('chart.html', inline=True)` 可生成包含运行依赖的独立 HTML。

`weasyprint input.html output.pdf` 适合 A4 分页、页码和印刷排版。交互式页面由 Chrome/Playwright 渲染；HTML、脚本、字体与图片应使用当前会话内的相对路径，离线交付时将所需资源一起打包。

## 构建和验证

```sh
docker build -t workbench-sandbox:office ./workbench/sandbox-office
docker run --rm --network none --shm-size 1g \
  --entrypoint /opt/office/python/bin/python \
  workbench-sandbox:office /opt/office/smoke.py /tmp/office-smoke
```

当前镜像安装 Google Chrome 的 AMD64 软件包，构建平台为 `linux/amd64`。系统软件安装时会获取软件源当前版本；实际版本记录在镜像的 `/opt/office/system-packages.txt` 和 `/opt/office/chrome-version.txt`。自备字体按 `fonts/README.md` 单独提供。

扩展能力的真实文件检查入口为：

```sh
python /opt/office/capabilities-smoke.py ./office-capabilities-check
```

该检查生成中文 Word、带图表和重算公式的 Excel、PNG/SVG/PDF 图表、Mermaid 流程图、分页 PDF，以及可交互的 React HTML 包。部署验证还需通过实际 shellctl 会话执行该入口，确认只允许当前会话目录和私有临时目录写入时仍可使用。

将沙箱管理器的 `WORKBENCH_SANDBOX_IMAGE` 配置为构建后的镜像。运行中的用户容器继续完成当前任务；停止的旧容器在下一次启动时保留为备份，并复用该用户已有的 home、files、env 数据卷创建新容器。

Office、浏览器和环境使用说明由管理员在 Agent 的配置文件中维护并发布，运行时通过配置文件能力读取。`office.py` 和 `uno_worker.py` 提供格式转换、预览、公式重算和接受修订；每次 LibreOffice 操作使用独立 profile，输出文件不得覆盖源文件或已有文件。

沙箱管理器和文件操作的回归检查位于 `../tests/`，应在 Linux 环境运行：

```sh
python -m pytest workbench/tests/test_file_ops.py \
  workbench/tests/test_file_directories.py \
  workbench/tests/test_manager_migration.py
```
