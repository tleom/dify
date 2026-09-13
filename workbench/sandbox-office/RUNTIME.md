# 用户沙盒常用环境

默认 `python`、`python3` 和 `node` 已可使用以下库，先检查导入再决定是否安装其他依赖。个人扩展库优先于基础库，同一用户的多个会话共用，用户之间隔离。

| 用途              | 已装工具与库                                                                                              |
| ----------------- | --------------------------------------------------------------------------------------------------------- |
| Word              | python-docx、docxtpl、docx、mammoth、docx-preview                                                         |
| Excel / 数据分析  | openpyxl、XlsxWriter、xlrd、pandas、numpy、scipy、exceljs                                                 |
| PowerPoint        | pptxgenjs、python-pptx、Font Awesome                                                                      |
| Office 预览       | LibreOffice Writer / Calc / Impress，先转 PDF，再用 Poppler 或 PyMuPDF 转图片                             |
| PDF               | PyMuPDF、pypdf、pdfplumber、pdf2image、reportlab、pdf-lib、pdfjs-dist、qpdf、Ghostscript                  |
| 图片 / 图表 / OCR | Pillow、matplotlib、seaborn、sharp、resvg、Tesseract（简体中文和英文）                                    |
| 文本提取          | MarkItDown（docx、xlsx、xls、pptx、pdf）、Pandoc、BeautifulSoup、lxml                                     |
| 浏览器            | Google Chrome，Python 和 Node Playwright                                                                  |
| 字体              | Noto CJK、Noto Emoji、DejaVu、Liberation、Carlito、Caladea，可选的用户自备字体（需在构建前放入 `fonts/`） |
| 扫描件与证据文件  | OCRmyPDF、pikepdf、ExifTool、qrcode、python-barcode                                                       |
| 批量比对与清洗    | RapidFuzz、diff-match-patch、jieba、dateparser、DuckDB、PyArrow、python-calamine、odfpy、tabulate         |
| 音视频与压缩包    | FFmpeg、pydub、soundfile、7-Zip、unrar-free、zip / unzip                                                  |

## Office 预览

统一脚本为每次转换建立独立的 LibreOffice profile：

```sh
python /opt/office/office.py convert source.docx output.pdf
python /opt/office/office.py preview source.docx preview --dpi 120
python /opt/office/office.py recalc workbook.xlsx recalculated.xlsx
python /opt/office/office.py accept-changes reviewed.docx accepted.docx
```

同样适用于 `xlsx` 和 `pptx`。输出文件不能覆盖输入或已有文件。重算检查公式错误与缓存；返回空字符串的有效公式不会误报为缺少缓存。输出分页和字体效果以渲染结果为准。

自备字体按实际许可单独安装。使用 `fc-match -f '%{family}: %{style}\n' '字体名'` 查看实际匹配结果；排版前核对所需字体是否命中。

## Chrome 自动化

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True,
                                args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page()
    page.goto("https://example.com")
    page.screenshot(path="page.png", full_page=True)
    browser.close()
```

浏览器运行在已隔离的用户容器中，不需要桌面服务。不要使用宿主机浏览器资料目录。Node 版本可使用 `require('playwright').chromium.launch({ channel: 'chrome', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] })`。

精确安装版本记录在 `/opt/office/requirements.lock.txt`、`/opt/office/node/package-lock.json`、`/opt/office/chrome-version.txt` 和 `/opt/office/system-packages.txt`。
