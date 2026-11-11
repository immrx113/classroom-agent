/* Real local frontend + real local API/data. No request interception or fixtures. */
const fs = require('fs');
const path = require('path');
const { chromium } = require('C:/Users/11322/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root = path.resolve(__dirname, '../..');
const state = JSON.parse(fs.readFileSync(path.join(root, 'backend/runtime/classroom-trial-state.json'), 'utf8'));
const out = path.join(root, 'docs/classroom-agent/acceptance/evidence-classroom-live-20261030/browser-final');
fs.mkdirSync(out, {recursive: true});
const results = [], requests = [], errors = [];
function record(check, passed, detail) {
  results.push({check, passed, detail});
  console.log(JSON.stringify(results.at(-1)));
}
async function main() {
  const browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true, args: ['--autoplay-policy=no-user-gesture-required']});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', req => {
      const u = new URL(req.url());
      if (u.pathname.startsWith('/api/')) requests.push({method: req.method(), path: u.pathname});
    });
    await page.goto(`http://localhost:5173/notes/${state.note_id}`);
    await page.getByText('要点与引用', {exact: false}).first().waitFor();
    await page.waitForFunction(() => document.querySelector('[data-testid="note-audio"]')?.readyState >= 1);
    const cite = page.getByRole('button', {name: '回听来源'}).first();
    await cite.waitFor();
    const [evResponse] = await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/evidence/') && r.status() === 200), cite.click()
    ]);
    const ev = await evResponse.json();
    await page.waitForFunction(start => {
      const a = document.querySelector('[data-testid="note-audio"]');
      return a && !a.paused && a.currentTime > start / 1000 + 0.1;
    }, ev.start_ms);
    const audio = await page.locator('[data-testid="note-audio"]').evaluate(a => ({
      src: new URL(a.currentSrc).pathname, time: a.currentTime, duration: a.duration, paused: a.paused
    }));
    record('真实笔记引用解析及固定资产定位播放', ev.source_audio_asset_id === state.asset_id &&
      audio.src === `/api/audio-assets/${state.asset_id}/content` && Math.abs(audio.duration - 45.28) < 0.1 && !audio.paused,
      {source_asset: ev.source_audio_asset_id, start_ms: ev.start_ms, ...audio});
    await page.locator('[data-testid="note-audio"]').evaluate(a => a.pause());
    await page.screenshot({path: path.join(out, 'live-note.png'), fullPage: true});

    await page.goto('http://localhost:5173/retrieval');
    await page.locator('select').first().selectOption(state.course_id);
    await page.waitForFunction(id => Array.from(document.querySelectorAll('select')[1]?.options || []).some(o => o.value === id), state.lecture_id);
    await page.locator('select').nth(1).selectOption(state.lecture_id);
    await page.getByPlaceholder('输入关键词检索（课程/课时范围内）...').fill('牛顿第二定律');
    const [searchResponse] = await Promise.all([
      page.waitForResponse(r => r.url().includes('/api/retrieval/search') && r.status() === 200),
      page.getByRole('button', {name: '搜索', exact: true}).click()
    ]);
    const found = await searchResponse.json();
    record('真实页面范围检索', found.status === 'ok' && found.total > 0 && found.results.every(r =>
      r.source_course_id === state.course_id && (!r.source_lecture_id || r.source_lecture_id === state.lecture_id)),
      {status: found.status, total: found.total, sources: found.results.map(r => r.source_type)});
    const openDoc = page.getByRole('button', {name: '打开原文'}).first();
    await openDoc.waitFor();
    const [blocksResponse] = await Promise.all([
      page.waitForResponse(r => r.url().includes(`/api/documents/${state.document_id}/blocks`) && r.status() === 200),
      openDoc.click()
    ]);
    const blocks = await blocksResponse.json();
    const shown = page.locator('p.whitespace-pre-wrap').first();
    await shown.waitFor();
    const text = await shown.textContent();
    record('真实课件原文打开', blocks.some(b => b.text === text), {block_count: blocks.length, text_matches_source: blocks.some(b => b.text === text)});
    await page.screenshot({path: path.join(out, 'live-retrieval.png'), fullPage: true});
    record('浏览器无页面脚本错误', errors.length === 0, {count: errors.length});
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify({results, requests, errors, fixture: false}, null, 2));
  }
  if (results.some(r => !r.passed)) process.exitCode = 1;
}
main().catch(error => { fs.writeFileSync(path.join(out, 'fatal.txt'), error.stack); console.error(error.message); process.exitCode = 2; });
