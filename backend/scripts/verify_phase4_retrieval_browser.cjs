/* P04-15 only: actual delivered retrieval page in a real browser.
 *
 * 覆盖冻结场景：C1/课时范围、PDF 第二页原文、DOCX 段落/表格原文、
 * 固定资产可解码音频定位播放、缺时间仅原文不回听、切 C2 无 C1 残留、历史/旧版本标记。
 *
 * 本地 HTTP 夹具只用于验证 UI 绑定与来源打开；后端范围/权限过滤由
 * `verify_phase4_frozen.py`（真实 API + 隔离库）覆盖，本脚本不替代之。
 */
const fs = require('fs');
const path = require('path');
const http = require('http');
const {spawnSync} = require('child_process');
const {chromium} = require('C:/Users/11322/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const root = path.resolve(__dirname, '../..');
const frontend = path.join(root, 'frontend');
const out = process.env.PHASE4_BROWSER_OUT || path.join(root, 'docs/classroom-agent/acceptance/evidence-phase4-browser-20261029');
fs.mkdirSync(out, {recursive: true});

const results = [];
const requests = [];

function record(check, ok, detail) {
  const row = {id: 'P04-15', check, result: ok ? '通过' : '不通过', detail};
  results.push(row);
  console.log(JSON.stringify(row));
}

function json(res, value) {
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(JSON.stringify(value));
}

// 可解码 8 秒单声道 16kHz WAV（真实浏览器可解码，非伪音频字节）
function wav(freq) {
  const samples = 16000 * 8, data = Buffer.alloc(44 + samples * 2);
  data.write('RIFF', 0); data.writeUInt32LE(data.length - 8, 4); data.write('WAVEfmt ', 8);
  data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(1, 22);
  data.writeUInt32LE(16000, 24); data.writeUInt32LE(32000, 28); data.writeUInt16LE(2, 32);
  data.writeUInt16LE(16, 34); data.write('data', 36); data.writeUInt32LE(samples * 2, 40);
  for (let i = 0; i < samples; i++) data.writeInt16LE(Math.round(10000 * Math.sin(2 * Math.PI * freq * i / 16000)), 44 + i * 2);
  return data;
}
const audio = wav(440);

function media(req, res) {
  requests.push({type: 'audio', path: req.url});
  let start = 0, end = audio.length - 1, status = 200;
  const match = (req.headers.range || '').match(/^bytes=(\d+)-(\d*)$/);
  if (match) { start = Number(match[1]); end = match[2] ? Math.min(Number(match[2]), end) : end; status = 206; }
  const headers = {'Content-Type': 'audio/wav', 'Content-Length': end - start + 1, 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store'};
  if (status === 206) headers['Content-Range'] = `bytes ${start}-${end}/${audio.length}`;
  res.writeHead(status, headers); res.end(audio.subarray(start, end + 1));
}

// ── 夹具数据（仅用于 UI 绑定，非后端权限） ──────────────────────────
const courses = [
  {id: 'course-c1', title: 'C1 课程', lecture_count: 2, created_at: '2026-10-29T00:00:00Z'},
  {id: 'course-c2', title: 'C2 课程', lecture_count: 1, created_at: '2026-10-29T00:00:00Z'},
];
const lectures = {
  'course-c1': [
    {id: 'lecture-l1', course_id: 'course-c1', title: 'L1 课时', status: 'processed'},
    {id: 'lecture-l2', course_id: 'course-c1', title: 'L2 课时', status: 'processed'},
  ],
  'course-c2': [
    {id: 'lecture-l3', course_id: 'course-c2', title: 'L3 课时', status: 'processed'},
  ],
};
const documents = {
  'course-c1': [
    {id: 'doc-pdf-c1', course_id: 'course-c1', title: 'PDF 课件', filename: 'a.pdf', mime: 'application/pdf',
      latest_version: {id: 'dv-pdf-c1', version: 1, status: 'active', parse_status: 'parsed', error_code: null, block_count: 2}},
    {id: 'doc-docx-c1', course_id: 'course-c1', title: 'DOCX 课件', filename: 'b.docx', mime: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      latest_version: {id: 'dv-docx-c1', version: 1, status: 'active', parse_status: 'parsed', error_code: null, block_count: 3}},
  ],
  'course-c2': [],
};
const blocks = {
  'doc-pdf-c1': [
    {block_id: 'pdf-p1', kind: 'text', text: 'PDF 第一页 PDF-C1-PAGE1', heading: null, page_no: 1, paragraph_index: null, table_index: null},
    {block_id: 'pdf-p2', kind: 'text', text: 'PDF 第二页 PDF-C1-PAGE2', heading: null, page_no: 2, paragraph_index: null, table_index: null},
  ],
  'doc-docx-c1': [
    {block_id: 'docx-h1', kind: 'heading', text: 'DOCX 标题 DOCX-C1-HEADING', heading: '第一章', page_no: null, paragraph_index: null, table_index: null},
    {block_id: 'docx-p1', kind: 'paragraph', text: 'DOCX 段落 DOCX-C1-PARA', heading: null, page_no: null, paragraph_index: 0, table_index: null},
    {block_id: 'docx-t1', kind: 'table', text: 'DOCX 表格 DOCX-C1-TABLE', heading: null, page_no: null, paragraph_index: null, table_index: 0},
  ],
};

function result(o) {
  return {
    source_type: 'document', source_version_id: 'dv-1', source_version: 1,
    source_lecture_id: null, source_course_id: null, source_document_id: null,
    source_audio_asset_id: null, is_current: true, block_id: null, page_no: null,
    heading: null, paragraph_index: null, table_index: null, segment_start: null,
    segment_end: null, start_ms: null, end_ms: null, content: [], quote: '',
    score: 0, rerank_score: 0, adjacent: [], note_evidence_ids: [], ...o,
  };
}

const c1Results = [
  result({source_type: 'transcript', source_version_id: 'tv-c1', source_lecture_id: 'lecture-l1', source_course_id: 'course-c1',
    source_audio_asset_id: 'audio-asset-c1', is_current: true, segment_start: 1, segment_end: 1, start_ms: 3000, end_ms: 5000,
    content: ['转写命中 TRANS-C1-TIME'], quote: '转写命中 TRANS-C1-TIME', score: 0.95, rerank_score: 0.95}),
  result({source_type: 'transcript', source_version_id: 'tv-c1', source_lecture_id: 'lecture-l1', source_course_id: 'course-c1',
    source_audio_asset_id: null, is_current: true, segment_start: 2, segment_end: 2, start_ms: null, end_ms: null,
    content: ['转写命中 TRANS-C1-NOTIME'], quote: '转写命中 TRANS-C1-NOTIME', score: 0.9, rerank_score: 0.9}),
  result({source_type: 'transcript', source_version_id: 'tv-c1-old', source_lecture_id: 'lecture-l1', source_course_id: 'course-c1',
    source_audio_asset_id: 'audio-asset-c1', is_current: false, segment_start: 3, segment_end: 3, start_ms: 1000, end_ms: 2000,
    content: ['转写命中 TRANS-C1-OLD'], quote: '转写命中 TRANS-C1-OLD', score: 0.85, rerank_score: 0.85}),
  result({source_type: 'document', source_version_id: 'dv-pdf-c1', source_document_id: 'doc-pdf-c1', is_current: true,
    block_id: 'pdf-p2', page_no: 2, content: ['PDF 第二页 PDF-C1-PAGE2'], quote: 'PDF 第二页 PDF-C1-PAGE2', score: 0.8, rerank_score: 0.8}),
  result({source_type: 'document', source_version_id: 'dv-docx-c1', source_document_id: 'doc-docx-c1', is_current: true,
    block_id: 'docx-p1', paragraph_index: 0, content: ['DOCX 段落 DOCX-C1-PARA'], quote: 'DOCX 段落 DOCX-C1-PARA', score: 0.75, rerank_score: 0.75}),
  result({source_type: 'document', source_version_id: 'dv-docx-c1', source_document_id: 'doc-docx-c1', is_current: true,
    block_id: 'docx-t1', table_index: 0, content: ['DOCX 表格 DOCX-C1-TABLE'], quote: 'DOCX 表格 DOCX-C1-TABLE', score: 0.7, rerank_score: 0.7}),
];
const c2Results = [
  result({source_type: 'document', source_version_id: 'dv-c2', source_course_id: 'course-c2', source_document_id: 'doc-c2',
    is_current: true, block_id: 'c2-b1', content: ['C2 专属 C2-ONLY-ANCHOR'], quote: 'C2 专属 C2-ONLY-ANCHOR', score: 0.9, rerank_score: 0.9}),
  result({source_type: 'transcript', source_version_id: 'tv-c2', source_lecture_id: 'lecture-l3', source_course_id: 'course-c2',
    is_current: true, segment_start: 0, segment_end: 0, start_ms: 500, end_ms: 1500,
    content: ['C2 转写 C2-TRANS-ANCHOR'], quote: 'C2 转写 C2-TRANS-ANCHOR', score: 0.8, rerank_score: 0.8}),
];

function searchResults(p) {
  if (p.course_id === 'course-c1') return c1Results;
  if (p.course_id === 'course-c2') return c2Results;
  return [];
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/api/courses') return json(res, {items: courses});
  if (/^\/api\/courses\/[^/]+\/lectures$/.test(url.pathname)) {
    return json(res, lectures[url.pathname.split('/')[3]] || []);
  }
  if (/^\/api\/courses\/[^/]+\/documents$/.test(url.pathname)) {
    return json(res, documents[url.pathname.split('/')[3]] || []);
  }
  if (/^\/api\/documents\/[^/]+\/blocks$/.test(url.pathname)) {
    const id = url.pathname.split('/')[3];
    requests.push({type: 'blocks', id});
    return json(res, blocks[id] || []);
  }
  if (/^\/api\/audio-assets\/[^/]+\/content$/.test(url.pathname)) {
    return media(req, res);
  }
  if (url.pathname === '/api/retrieval/search') {
    let body = '';
    req.on('data', c => body += c);
    req.on('end', () => {
      let parsed = {};
      try { parsed = JSON.parse(body || '{}'); } catch (e) { parsed = {}; }
      requests.push({type: 'search', body: parsed});
      const rs = searchResults(parsed);
      return json(res, {query: parsed.query, status: rs.length ? 'ok' : 'no_evidence', index_status: 'current',
        index_version_id: 'iv-1', results: rs, total: rs.length, trace: {}});
    });
    return;
  }
  if (url.pathname.startsWith('/api/')) { res.writeHead(404); return res.end('{}'); }
  let relative = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
  let file = path.resolve(frontend, 'dist', relative);
  if (!file.startsWith(path.resolve(frontend, 'dist') + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    file = path.join(frontend, 'dist/index.html');
  }
  const ext = path.extname(file);
  res.writeHead(200, {'Content-Type': ({'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'})[ext] || 'application/octet-stream'});
  fs.createReadStream(file).pipe(res);
});

async function main() {
  const started = new Date().toISOString();
  let browser;
  if (!process.argv.includes('--skip-build')) {
    for (const [label, args] of [
      ['typecheck', [path.join(frontend, 'node_modules/typescript/bin/tsc'), '-b']],
      ['build', [path.join(frontend, 'node_modules/vite/bin/vite.js'), 'build']],
    ]) {
      const proc = spawnSync(process.execPath, args, {cwd: frontend, encoding: 'utf8', windowsHide: true});
      fs.writeFileSync(path.join(out, label + '.log'), (proc.stdout || '') + '\n' + (proc.stderr || ''));
      if (proc.status !== 0) throw new Error(label + ' failed: see log');
    }
  }
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--autoplay-policy=no-user-gesture-required']});
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', msg => { if (msg.type() === 'error') errors.push('console: ' + msg.text()); });

    const base = `http://127.0.0.1:${server.address().port}`;
    await page.goto(base + '/retrieval');

    // 等课程下拉就位（全部课程 + C1 + C2）
    await page.locator('select').first().waitFor();
    await page.waitForFunction(() => { const s = document.querySelector('select'); return s && s.options.length >= 3; });
    const courseOptions = await page.locator('select').first().locator('option').allTextContents();
    record('课程范围可选 C1/C2', courseOptions.some(t => t.includes('C1')) && courseOptions.some(t => t.includes('C2')), {courseOptions});

    // 选择 C1 → 课时范围出现 L1/L2
    await page.locator('select').first().selectOption('course-c1');
    await page.waitForFunction(() => { const ss = document.querySelectorAll('select'); return ss.length >= 2 && ss[1].options.length >= 3; });
    const lectureOptions = await page.locator('select').nth(1).locator('option').allTextContents();
    record('课时范围可选 L1/L2', lectureOptions.some(t => t.includes('L1')) && lectureOptions.some(t => t.includes('L2')), {lectureOptions});

    await page.locator('select').nth(1).selectOption('lecture-l1');
    const qbox = page.getByPlaceholder('输入关键词检索（课程/课时范围内）...');
    await qbox.fill('锚点');
    await page.getByRole('button', {name: '搜索'}).click();
    await page.getByText('TRANS-C1-TIME').waitFor();

    const searchReq = requests.find(r => r.type === 'search');
    record('检索请求携带 C1/L1 范围', !!searchReq && searchReq.body.course_id === 'course-c1' && searchReq.body.lecture_id === 'lecture-l1',
      {body: searchReq && searchReq.body});

    // 缺时间仅原文：该结果不出现「回听来源」按钮
    const notimeCard = page.locator('div.rounded-lg', {hasText: 'TRANS-C1-NOTIME'}).first();
    const notimeBtns = await notimeCard.getByRole('button', {name: '回听来源'}).count();
    const notimeShown = await notimeCard.getByText('TRANS-C1-NOTIME').count();
    record('缺时间仅原文不回听', notimeShown >= 1 && notimeBtns === 0, {notimeShown, notimeBtns});

    // 历史/旧版本标记
    const oldBadge = await page.getByText('旧版本', {exact: true}).count();
    record('历史/旧版本标记', oldBadge >= 1, {oldBadge});

    // PDF 第二页原文（打开原文后展示真实块 + 页码）
    const pdfCard = page.locator('div.rounded-lg', {hasText: 'PDF-C1-PAGE2'}).first();
    await pdfCard.getByRole('button', {name: '打开原文'}).click();
    await page.locator('p.whitespace-pre-wrap', {hasText: 'PDF 第二页 PDF-C1-PAGE2'}).waitFor();
    const pdfPage2 = await page.getByText('第 2 页').count();
    const pdfBlocksReq = requests.filter(r => r.type === 'blocks' && r.id === 'doc-pdf-c1').length;
    record('打开 PDF 第二页原文并定位页码', pdfPage2 >= 1 && pdfBlocksReq >= 1, {pdfPage2, pdfBlocksReq});

    // DOCX 段落原文
    const docxParaCard = page.locator('div.rounded-lg', {hasText: 'DOCX-C1-PARA'}).first();
    await docxParaCard.getByRole('button', {name: '打开原文'}).click();
    await page.locator('p.whitespace-pre-wrap', {hasText: 'DOCX 段落 DOCX-C1-PARA'}).waitFor();
    const paraKind = await page.getByText('paragraph', {exact: true}).count();

    // DOCX 表格原文
    const docxTableCard = page.locator('div.rounded-lg', {hasText: 'DOCX-C1-TABLE'}).first();
    await docxTableCard.getByRole('button', {name: '打开原文'}).click();
    await page.locator('p.whitespace-pre-wrap', {hasText: 'DOCX 表格 DOCX-C1-TABLE'}).waitFor();
    const tableKind = await page.getByText('table', {exact: true}).count();
    const docxBlocksReq = requests.filter(r => r.type === 'blocks' && r.id === 'doc-docx-c1').length;
    record('打开 DOCX 段落/表格原文', paraKind >= 1 && tableKind >= 1 && docxBlocksReq >= 1, {paraKind, tableKind, docxBlocksReq});

    // 固定资产定位播放：转写带时间结果点击「回听来源」→ 音频按 start_ms 定位并播放
    const timeCard = page.locator('div.rounded-lg', {hasText: 'TRANS-C1-TIME'}).first();
    await timeCard.getByRole('button', {name: '回听来源'}).click();
    await page.waitForFunction(() => { const a = document.querySelector('[data-testid="retrieval-audio"]'); return a && a.readyState >= 1; });
    await page.waitForFunction(() => { const a = document.querySelector('[data-testid="retrieval-audio"]'); return a.currentTime > 3.15 && !a.paused; });
    const audioState = await page.locator('[data-testid="retrieval-audio"]').evaluate(a => ({src: a.currentSrc, time: a.currentTime, paused: a.paused, duration: a.duration, readyState: a.readyState}));
    record('固定资产定位播放可解码音频', audioState.time >= 3 && !audioState.paused && audioState.duration === 8 && audioState.src.includes('/api/audio-assets/audio-asset-c1/content'), {...audioState});

    // 暂停后截图，避免切课程卸载播放中的音频产生无害的 play() 中断控制台告警
    await page.locator('[data-testid="retrieval-audio"]').evaluate(a => a.pause());
    await page.screenshot({path: path.join(out, 'P04-15-C1-results.png'), fullPage: true});

    // 切 C2 → 检索 → 无 C1 残留
    await page.locator('select').first().selectOption('course-c2');
    await page.waitForFunction(() => { const ss = document.querySelectorAll('select'); return ss.length >= 2 && ss[1].options.length >= 2; });
    await page.locator('select').nth(1).selectOption('lecture-l3');
    await qbox.fill('锚点');
    await page.getByRole('button', {name: '搜索'}).click();
    await page.getByText('C2-ONLY-ANCHOR').waitFor();
    const c2Shown = await page.getByText('C2-ONLY-ANCHOR').count();
    const c1Residue = await page.getByText('TRANS-C1-TIME').count()
      + await page.getByText('TRANS-C1-NOTIME').count()
      + await page.getByText('PDF-C1-PAGE2').count()
      + await page.getByText('DOCX-C1-PARA').count()
      + await page.getByText('DOCX-C1-TABLE').count();
    const c2SearchReq = requests.filter(r => r.type === 'search').at(-1);
    record('切 C2 无 C1 残留', c2Shown >= 1 && c1Residue === 0 && c2SearchReq && c2SearchReq.body.course_id === 'course-c2',
      {c2Shown, c1Residue, body: c2SearchReq && c2SearchReq.body});

    await page.screenshot({path: path.join(out, 'P04-15-C2-results.png'), fullPage: true});
    fs.writeFileSync(path.join(out, 'browser-errors.json'), JSON.stringify(errors, null, 2));
  } finally {
    if (browser) await browser.close();
    if (server.listening) await new Promise(resolve => server.close(resolve));
    fs.writeFileSync(path.join(out, 'browser-results.json'), JSON.stringify({
      started, finished: new Date().toISOString(),
      fixture: '实际编译产物 + 真实浏览器/音频解码；本地 API 夹具仅验证 UI 绑定与来源打开，后端范围/权限过滤由 verify_phase4_frozen.py 真实 API 覆盖。无收费模型、无正式库。',
      results, requests,
    }, null, 2));
  }
  if (results.some(r => r.result === '不通过')) process.exitCode = 1;
}

main().catch(error => { fs.writeFileSync(path.join(out, 'browser-fatal.txt'), error.stack || String(error)); console.error(error); process.exitCode = 2; });
