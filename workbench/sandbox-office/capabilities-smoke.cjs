const fs = require('node:fs')
const path = require('node:path')
const { execFileSync } = require('node:child_process')
const { chromium } = require('playwright')

async function main() {
  const out = process.argv[2]
  const echarts = require('echarts')
  const chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 640, height: 360 })
  chart.setOption({
    title: { text: '款项统计' },
    xAxis: { data: ['第一笔', '第二笔'] },
    yAxis: {},
    series: [{ type: 'bar', data: [1200, 3400] }],
  })
  fs.writeFileSync(path.join(out, 'echarts.svg'), chart.renderToSVGString())
  chart.dispose()
  fs.writeFileSync(
    path.join(out, 'flow.mmd'),
    'flowchart LR\n A[材料整理] --> B[证据核对] --> C[形成报告]\n',
  )
  execFileSync('mmdc', ['-i', path.join(out, 'flow.mmd'), '-o', path.join(out, 'flow.svg')], {
    timeout: 60000,
    stdio: 'pipe',
  })
  execFileSync('mmdc', ['-i', path.join(out, 'flow.mmd'), '-o', path.join(out, 'flow.png')], {
    timeout: 60000,
    stdio: 'pipe',
  })
  const markdown = require('markdown-it')({
    highlight: (str) => require('highlight.js').highlightAuto(str).value,
  })
  const content = markdown.render('# 中文报告\n\n| 事项 | 金额 |\n|---|---:|\n| 合计 | 4600 |\n')
  const math = require('katex').renderToString('A = P(1+r)^n', { throwOnError: true })
  const $ = require('cheerio').load(content)
  if ($('h1').text() !== '中文报告' || $('td').length !== 2)
    throw new Error('Markdown table parsing failed')
  const style = require('sass').compileString('$accent:#2563eb;h1 {color:$accent}').css
  fs.writeFileSync(
    path.join(out, 'app.tsx'),
    'import React from "react";import {createRoot} from "react-dom/client";createRoot(document.getElementById("app")!).render(<button onClick={e => e.currentTarget.textContent="已核对"}>核对</button>);',
  )
  require('esbuild').buildSync({
    entryPoints: [path.join(out, 'app.tsx')],
    outfile: path.join(out, 'bundle.js'),
    bundle: true,
    nodePaths: ['/opt/office/node/node_modules'],
    jsx: 'automatic',
  })
  fs.writeFileSync(
    path.join(out, 'tailwind.css'),
    '@import "/opt/office/node/node_modules/tailwindcss/index.css";@source inline("text-blue-600 font-bold p-4");',
  )
  execFileSync(
    'tailwindcss',
    ['-i', path.join(out, 'tailwind.css'), '-o', path.join(out, 'utilities.css'), '--minify'],
    { timeout: 60000, cwd: out, stdio: 'pipe' },
  )
  if (!fs.readFileSync(path.join(out, 'utilities.css'), 'utf8').includes('.text-blue-600'))
    throw new Error('Tailwind generation failed')
  fs.writeFileSync(path.join(out, 'amount.ts'), 'const amount: number = 4600;')
  execFileSync(
    'tsc',
    [
      path.join(out, 'amount.ts'),
      '--outDir',
      path.join(out, 'compiled'),
      '--target',
      'ES2022',
      '--skipLibCheck',
    ],
    { timeout: 60000, cwd: out, stdio: 'pipe' },
  )
  if (!fs.readFileSync(path.join(out, 'compiled/amount.js'), 'utf8').includes('4600'))
    throw new Error('TypeScript compilation failed')
  const d3 = await import(require.resolve('d3'))
  if (d3.sum([1200, 3400]) !== 4600) throw new Error('D3 failed')
  fs.copyFileSync(
    '/opt/office/node/node_modules/katex/dist/katex.min.css',
    path.join(out, 'katex.min.css'),
  )
  fs.cpSync('/opt/office/node/node_modules/katex/dist/fonts', path.join(out, 'fonts'), {
    recursive: true,
  })
  fs.writeFileSync(
    path.join(out, 'index.html'),
    `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="katex.min.css"><style>body{font-family:"Noto Sans CJK SC";padding:24px}${style}</style>${content}${math}<div id="app"></div><script src="bundle.js"></script>`,
  )
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  })
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 650 } })
    const errors = []
    page.on('pageerror', (error) => errors.push(String(error)))
    await page.goto('file://' + path.join(out, 'index.html'))
    await page.getByRole('button', { name: '核对' }).click()
    await page.getByRole('button', { name: '已核对' }).waitFor()
    if (errors.length) throw new Error(errors.join('\n'))
    await page.screenshot({ path: path.join(out, 'html-browser.png'), fullPage: true })
    await page.pdf({ path: path.join(out, 'html-browser.pdf'), format: 'A4' })
  } finally {
    await browser.close()
  }
  const JSZip = require('jszip')
  const zip = new JSZip()
  zip.file('index.html', fs.readFileSync(path.join(out, 'index.html')))
  zip.file('bundle.js', fs.readFileSync(path.join(out, 'bundle.js')))
  zip.file('katex.min.css', fs.readFileSync(path.join(out, 'katex.min.css')))
  for (const name of fs.readdirSync(path.join(out, 'fonts')))
    zip.file('fonts/' + name, fs.readFileSync(path.join(out, 'fonts', name)))
  fs.writeFileSync(
    path.join(out, 'html-package.zip'),
    await zip.generateAsync({ type: 'nodebuffer' }),
  )
  fs.writeFileSync(
    path.join(out, 'node-capabilities.json'),
    JSON.stringify(
      {
        status: 'passed',
        echarts: true,
        mermaid: true,
        markdown_math: true,
        react_typescript_bundle: true,
        tailwind_sass: true,
        d3: true,
        browser_interaction: true,
        html_zip: true,
      },
      null,
      2,
    ),
  )
}
main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
