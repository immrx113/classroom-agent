/* 阶段 4「问答/检索闭环」A5 浏览器实测证据（真实本地前端 Vite + 真实本地后端，无请求拦截/夹具）。
 *
 * 后端：scripts/serve_phase4_browser.py（app.main:app + 独立种子库 + 确定性 embedding，不触发收费模型）。
 * 前端：Vite dev server :5173，/api 代理到 :8787。
 * 种子：scripts/seed_phase4_browser.py（真实可解码 WAV 音频 + 已发布例题笔记 + 已解析课件页）。
 *
 * 覆盖 A5 浏览器项：
 *   1. 笔记来源定位：问结构化例题 → 点击「打开笔记」→ 打开正文与回答引用一致。
 *   2. 转写来源回听：问转写标记 → 点击「回听来源」→ 记录音频播放位置 + 对应原句。
 *   3. 课件来源定位：问课件标记 → 点击「打开原文」→ 记录页码 + 原文块。
 *   三项截图与作业/课程/课时/来源标识一并写入同一份 results.json。
 *
 * 运行（backend 目录，Node，需先启动后端 8787 与前端 5173）：
 *   node scripts/verify_phase4_browser.cjs
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('C:/Users/11322/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const ROOT = path.resolve(__dirname, '../..');
const OUT = path.join(ROOT, 'docs/classroom-agent/reset-v1/evidence/phase4/browser');
fs.mkdirSync(OUT, { recursive: true });

const BASE = 'http://localhost:5173';
const state = JSON.parse(fs.readFileSync(path.join(ROOT, 'backend/runtime/phase4-browser-state.json'), 'utf8'));

const results = [];
const requests = [];
const errors = [];

function record(id, check, passed, detail) {
  results.push({ id, check, result: passed ? '通过' : '不通过', detail });
  console.log(JSON.stringify(results[results.length - 1]));
}

async function prepare(page) {
  await page.goto(BASE + '/classroom-qa', { waitUntil: 'networkidle' });
  await page.locator('select').first().waitFor({ timeout: 15000 });
  await page.waitForFunction((cid) => {
    const s = document.querySelector('select');
    return s && Array.from(s.options).some((o) => o.value === cid);
  }, state.course_id, { timeout: 15000 });
  await page.locator('select').first().selectOption(state.course_id);
  await page.waitForFunction((lid) => {
    const ss = document.querySelectorAll('select');
    return ss.length >= 2 && Array.from(ss[1].options).some((o) => o.value === lid);
  }, state.lecture_id, { timeout: 15000 });
  await page.locator('select').nth(1).selectOption(state.lecture_id);
}

async function ask(page, query) {
  const qbox = page.getByPlaceholder('输入问题，Enter 发送...');
  await qbox.fill(query);
  await page.getByRole('button', { name: '提问' }).click();
}

async function main() {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true,
    args: ['--autoplay-policy=no-user-gesture-required'],
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
    page.on('request', (req) => {
      const u = new URL(req.url());
      if (u.pathname.startsWith('/api/')) requests.push({ method: req.method(), path: u.pathname });
    });

    // ═══ 1. 笔记来源：结构化例题 → 打开笔记 → 正文与引用一致 ═══
    await prepare(page);
    await ask(page, state.markers.example);
    const noteBtn = page.getByRole('button', { name: '打开笔记' }).first();
    await noteBtn.waitFor({ timeout: 15000 });
    const answerNote = await page.getByTestId('qa-answer').last().textContent();
    const noteSnippet = (await page.getByTestId('qa-source-content').allTextContents()).find((t) => t.includes(state.markers.example)) || '';
    await noteBtn.click();
    await page.waitForFunction((mk) => {
      const el = document.querySelector('[data-testid="qa-note-content"]');
      return el && el.textContent.includes(mk);
    }, state.markers.example, { timeout: 15000 });
    const noteContent = await page.getByTestId('qa-note-content').textContent();
    const noteOk = answerNote.includes(state.markers.example) && answerNote.includes('笔记')
      && noteContent.includes(state.markers.example) && noteContent.includes('计算 2+3') && noteContent.includes('2+3=5')
      && noteSnippet.includes(state.markers.example);
    record('A5-browser-1', '笔记来源定位：点击笔记引用打开正文与回答引用一致（例题可核对）', noteOk, {
      course_id: state.course_id, lecture_id: state.lecture_id, source_type: 'note',
      marker: state.markers.example,
      answer_cites_marker: answerNote.includes(state.markers.example),
      answer_cites_note_label: answerNote.includes('笔记'),
      opened_note_contains_marker: noteContent.includes(state.markers.example),
      opened_note_contains_derivation: noteContent.includes('2+3=5'),
      note_snippet_contains_marker: noteSnippet.includes(state.markers.example),
      answer: answerNote.slice(0, 200),
      opened_note: noteContent.slice(0, 300),
    });
    await page.screenshot({ path: path.join(OUT, 'item1-note.png'), fullPage: true });

    // ═══ 2. 转写来源：回听 → 记录播放位置 + 对应原句 ═══
    await prepare(page);
    await ask(page, state.markers.transcript);
    const replayBtn = page.getByRole('button', { name: '回听来源' }).first();
    await replayBtn.waitFor({ timeout: 15000 });
    const answerTrans = await page.getByTestId('qa-answer').last().textContent();
    const transSnippet = (await page.getByTestId('qa-source-content').allTextContents()).find((t) => t.includes(state.markers.transcript)) || '';
    await replayBtn.click();
    await page.waitForFunction((ms) => {
      const a = document.querySelector('[data-testid="qa-audio"]');
      return a && a.readyState >= 1 && !a.paused && a.currentTime > ms / 1000 + 0.1;
    }, state.expect.transcript_start_ms, { timeout: 15000 });
    const audio = await page.locator('[data-testid="qa-audio"]').evaluate((a) => ({
      src: new URL(a.currentSrc).pathname, time: a.currentTime, paused: a.paused, duration: a.duration,
    }));
    const transOk = answerTrans.includes(state.markers.transcript) && answerTrans.includes('转写')
      && transSnippet.includes(state.markers.transcript)
      && audio.src === `/api/audio-assets/${state.audio_asset_id}/content`
      && !audio.paused && audio.time > state.expect.transcript_start_ms / 1000;
    record('A5-browser-2', '转写来源回听：点击回听来源定位播放，记录音频位置与原句', transOk, {
      course_id: state.course_id, lecture_id: state.lecture_id, source_type: 'transcript',
      marker: state.markers.transcript,
      answer_cites_marker: answerTrans.includes(state.markers.transcript),
      answer_cites_transcript_label: answerTrans.includes('转写'),
      original_sentence: transSnippet.slice(0, 120),
      expected_start_ms: state.expect.transcript_start_ms,
      audio,
    });
    await page.locator('[data-testid="qa-audio"]').evaluate((a) => a.pause());
    await page.screenshot({ path: path.join(OUT, 'item2-transcript.png'), fullPage: true });

    // ═══ 3. 课件来源：打开原文 → 记录页码 + 原文块 ═══
    await prepare(page);
    await ask(page, state.markers.document);
    const docBtn = page.getByRole('button', { name: '打开原文' }).first();
    await docBtn.waitFor({ timeout: 15000 });
    const answerDoc = await page.getByTestId('qa-answer').last().textContent();
    const docSnippet = (await page.getByTestId('qa-source-content').allTextContents()).find((t) => t.includes(state.markers.document)) || '';
    await docBtn.click();
    await page.waitForFunction((mk) => {
      const ps = Array.from(document.querySelectorAll('p.whitespace-pre-wrap'));
      return ps.some((p) => p.textContent.includes(mk));
    }, state.markers.document, { timeout: 15000 });
    const pageShowsPage1 = (await page.getByText('第 1 页', { exact: true }).count()) >= 1;
    const docBlocksReq = requests.filter((r) => r.path.includes(`/api/documents/${state.document_id}/blocks`)).length;
    const docOk = answerDoc.includes(state.markers.document) && answerDoc.includes('课件')
      && docSnippet.includes(state.markers.document) && pageShowsPage1 && docBlocksReq >= 1;
    record('A5-browser-3', '课件来源定位：点击打开原文显示页码与原文块', docOk, {
      course_id: state.course_id, lecture_id: state.lecture_id, source_type: 'document',
      marker: state.markers.document,
      answer_cites_marker: answerDoc.includes(state.markers.document),
      answer_cites_document_label: answerDoc.includes('课件'),
      original_block: docSnippet.slice(0, 120),
      page_no: 1, page_shown: pageShowsPage1, blocks_requests: docBlocksReq,
    });
    await page.screenshot({ path: path.join(OUT, 'item3-document.png'), fullPage: true });

    record('A5-browser-4', '浏览器无页面脚本错误', errors.length === 0, { count: errors.length });
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({
      started: new Date().toISOString(), finished: new Date().toISOString(),
      course_id: state.course_id, lecture_id: state.lecture_id, note_id: state.note_id,
      document_id: state.document_id, audio_asset_id: state.audio_asset_id,
      markers: state.markers, expect: state.expect,
      results, requests, errors,
      fixture: false,
      note: '真实本地前端 Vite + 真实本地后端（独立种子库 yuan_resetv1_p4_browser）；确定性 embedding + 确定性摘录答案，无请求拦截/夹具，无收费模型调用。',
    }, null, 2));
  }
  if (results.some((r) => r.result === '不通过')) process.exitCode = 1;
}

main().catch((error) => {
  fs.writeFileSync(path.join(OUT, 'fatal.txt'), error.stack || String(error));
  console.error(error);
  process.exitCode = 2;
});
