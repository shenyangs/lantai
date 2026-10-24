/**
 * Real Chromium geometry lock for the PDF renderer, using an in-memory PDF and
 * preload stub (no backend, provider or user document). Start dev:renderer, then:
 * ego-browser nodejs <<'EOF'
 * const task = await taskSpace("PDF ribbon regression");
 * const { runPdfRibbonLayoutSmoke } = await import("/absolute/path/to/this/file.mjs");
 * await runPdfRibbonLayoutSmoke(task.page("p1"), { rendererUrl: "http://127.0.0.1:5186/" });
 * await task.finish({ keep: [] });
 * EOF
 * Resume the same task space for subsequent runs.
 */
import assert from 'node:assert/strict'
import { PDFDocument } from 'pdf-lib'

export async function runPdfRibbonLayoutSmoke(page, { rendererUrl, screenshotPath } = {}) {
  assert.ok(rendererUrl, 'rendererUrl is required')
  const pdf = await PDFDocument.create()
  pdf.addPage([595, 842]).drawText('PDF ribbon layout regression', { x: 48, y: 780, size: 18 })
  const bytes = Array.from(await pdf.save())
  const bootstrap = await page.cdp('Page.addScriptToEvaluateOnNewDocument', {
    source: `(() => {
      const params = new URLSearchParams(location.search);
      Object.defineProperty(navigator, 'platform', { value: 'MacIntel' });
      window.__pdfRibbonErrors = [];
      addEventListener('error', e => window.__pdfRibbonErrors.push(e.message));
      addEventListener('unhandledrejection', e => window.__pdfRibbonErrors.push(String(e.reason)));
      const api = {
        getLanguage: async () => params.get('lang') || 'zh',
        getTheme: async () => params.get('theme') || 'light',
        consumePending: async () => '/layout-smoke/' + params.get('filename'),
        readFile: async () => new Uint8Array(${JSON.stringify(bytes)}).buffer,
        listEditFonts: async () => [],
        getAiSettings: async () => ({ provider: 'openai', model: '', apiKey: '' }),
        setDirty: () => {},
      };
      window.pdfApi = new Proxy(api, { get(target, name) {
        if (String(name).startsWith('on')) return () => () => {};
        return target[name] || (() => { throw new Error('Unexpected PDF IPC: ' + String(name)); });
      }});
    })();`,
  })
  const failures = []
  const measurements = []
  const names = ['文档.pdf', `${'25b45dd72266011e8c53b7a494f05b1370e608268cbe24a9425304e9341c862e'}_当前汽车行业品牌形象与发展趋势及竞争格局深度研究.pdf`]
  try {
    for (const lang of ['zh', 'en']) for (const theme of ['light', 'dark']) {
      for (const filename of names) for (const width of [640, 720, 960, 1440]) {
        await page.cdp('Emulation.setDeviceMetricsOverride', { width, height: 840, deviceScaleFactor: 1, mobile: false })
        const url = new URL(rendererUrl)
        url.search = new URLSearchParams({ mode: 'tab', lang, theme, filename }).toString()
        await page.goto(url.href)
        await page.waitForSelector('.ribbon-file')
        await page.evaluate(() => document.fonts.ready.then(() => new Promise(resolve => requestAnimationFrame(resolve))))
        const result = await page.evaluate(() => {
          const row = document.querySelector('.ribbon-tabs')
          const bounds = row.getBoundingClientRect()
          const controls = [...row.querySelectorAll(':scope > .file-tab-wrap > button, :scope > .qa-btn, :scope > .ribbon-tab, :scope > .lt-office-agent-entry')]
          const issues = []
          const rects = controls.map(el => {
            const r = el.getBoundingClientRect()
            const range = document.createRange()
            range.selectNodeContents(el)
            const textLines = new Set([...range.getClientRects()].filter(rect => rect.width > 0).map(rect => Math.round(rect.top)))
            const label = el.textContent.trim() || el.title
            if (el.matches('.ribbon-tab, .ribbon-tab-file') && textLines.size !== 1) issues.push(label + ' wraps')
            if (el.matches('.qa-btn') && r.width < 29) issues.push(label + ' is squeezed')
            if (r.left < bounds.left || r.right > bounds.right || r.top < bounds.top || r.bottom > bounds.bottom + 1) issues.push(label + ' leaves the menu row')
            return { label, left: r.left, right: r.right }
          })
          for (let i = 1; i < rects.length; i++) if (rects[i].left < rects[i - 1].right - 1) issues.push('controls overlap')
          if (rects[0].left > 24) issues.push('embedded tab reserves native traffic-light space')
          if (row.scrollWidth > row.clientWidth) issues.push('menu row overflows')
          const title = row.querySelector('.ribbon-file')
          return { issues, rowHeight: bounds.height, titleWidth: title.clientWidth, titleScrollWidth: title.scrollWidth, errors: window.__pdfRibbonErrors }
        })
        const scenario = `${lang}/${theme}/${filename === names[0] ? 'short' : 'long'}/${width}`
        measurements.push({ scenario, ...result })
        for (const issue of [...result.issues, ...result.errors]) failures.push(`${scenario}: ${issue}`)
      }
    }
    // Menus must still escape the fixed-height row and the co-creation dock must toggle.
    await page.click('.ribbon-tab-file')
    await page.waitForSelector('.file-menu')
    const menu = await page.evaluate(() => {
      const el = document.querySelector('.file-menu')
      const r = el.getBoundingClientRect()
      return { count: el.querySelectorAll('button').length, hit: el.contains(document.elementFromPoint(r.left + 20, r.bottom - 15)) }
    })
    assert.equal(menu.count, 5)
    assert.equal(menu.hit, true, 'file menu is clipped by the row')
    await page.click('.ribbon-tab-file')
    await page.click('.lt-office-agent-entry')
    await page.waitForFunction(() => document.querySelector('.lt-office-agent-entry').textContent.includes('收起'))
    await page.click('.lt-office-agent-entry')
    await page.waitForFunction(() => !document.querySelector('.lt-office-agent-entry').textContent.includes('收起'))
    // A standalone Mac window still needs clearance for its native buttons.
    const standalone = new URL(rendererUrl)
    standalone.search = new URLSearchParams({ lang: 'zh', theme: 'light', filename: names[1] }).toString()
    await page.goto(standalone.href)
    await page.waitForSelector('.ribbon-tab-file')
    assert.ok(await page.evaluate(() => document.querySelector('.ribbon-tab-file').getBoundingClientRect().left >= 80), 'standalone Mac traffic-light clearance was lost')
    const finalUrl = new URL(standalone)
    finalUrl.searchParams.set('mode', 'tab')
    await page.goto(finalUrl.href)
    await page.waitForSelector('.ribbon-file')
    if (screenshotPath) await page.screenshot({ path: screenshotPath })
    console.log(JSON.stringify({ scenarios: measurements.length, failures, measurements }))
    assert.deepEqual(failures, [], 'PDF ribbon geometry regressed')
    return measurements
  } finally {
    await page.cdp('Page.removeScriptToEvaluateOnNewDocument', { identifier: bootstrap.identifier })
  }
}
