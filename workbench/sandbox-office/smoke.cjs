const fs = require('node:fs');
const path = require('node:path');
const PptxGenJS = require('pptxgenjs');
const { chromium } = require('playwright');

async function main() {
  const out = process.argv[2] || '/tmp/office-smoke';
  fs.mkdirSync(out, { recursive: true });
  for (const name of ['docx', 'mammoth', 'docx-preview', 'exceljs', 'pdf-lib', 'sharp', '@resvg/resvg-js', '@fortawesome/fontawesome-svg-core', '@fortawesome/free-solid-svg-icons']) {
    require(name);
  }
  const presentation = new PptxGenJS();
  presentation.layout = 'LAYOUT_WIDE';
  presentation.addSlide().addText('中文预览验证', {
    x: 1, y: 1, w: 10, h: 1, fontSize: 32, fontFace: 'Noto Sans CJK SC',
  });
  await presentation.writeFile({ fileName: path.join(out, 'slides.pptx') });
  const browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage();
    await page.setContent('<html><head><title>中文浏览器验证</title></head><body style="font-family:Noto Sans CJK SC"><h1>中文预览验证</h1><button onclick="this.textContent=\'已点击\'">测试按钮</button></body></html>');
    await page.getByRole('button', { name: '测试按钮' }).click();
    if (await page.getByRole('button').innerText() !== '已点击') throw new Error('Chrome interaction failed');
    await page.screenshot({ path: path.join(out, 'chrome-node.png') });
    await page.pdf({ path: path.join(out, 'chrome-node.pdf') });
    fs.writeFileSync(path.join(out, 'node-result.json'), JSON.stringify({ ok: true, chrome: browser.version(), pptx: true, browserInteraction: true }));
  } finally {
    await browser.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
