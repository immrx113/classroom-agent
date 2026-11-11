/* 阶段 2 复验「浏览器实测证据」（真实本地前端 Vite + 真实本地后端，无请求拦截/夹具）。
 *
 * 后端：scripts/serve_phase2_browser.py（复用 app.main:app + FakeASRProvider 可控替身，
 *       按需派发 POST /__test/dispatch，精确控制「失败 → 重试」时序）。
 * 前端：Vite dev server :5173，/api 代理到 :8787。
 *
 * 覆盖复验项：
 *   1. 课时课件归属（首载/刷新/切换课时）+ 课程共享展示规则
 *   2. 首次点选转写段（播放器加载完成后刷新，首次点第三段 ≈13.61s，再连续跳转）
 *   3. 页面录音/课件上传 → 课件定位原页 → 模拟转写失败 → 刷新重试得有效结果
 *   A1a 浏览器请求无身份头（服务端固定 local-user）请求审计
 *
 * 运行（backend 目录，Node）：
 *   node scripts/verify_phase2_browser.cjs
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('C:/Users/11322/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const ROOT = path.resolve(__dirname, '../..');
const OUT = path.join(ROOT, 'docs/classroom-agent/reset-v1/evidence/phase2/browser');
fs.mkdirSync(OUT, { recursive: true });

const BASE = 'http://localhost:5173';
const API = 'http://localhost:8787';
const PDF = path.join(ROOT, 'docs/classroom-agent/trial-materials/newton-lesson.pdf');

// 活跃库中已知的真实课时（第 1 讲 牛顿第二定律，含真实录音与转写段）
const REAL_COURSE_ID = 'fcc5ec19-36bb-402e-a9df-b430f9d1ac40';
const REAL_LECTURE_ID = '4b141489-551f-4aa6-8653-b2d0790a71e8';

const results = [];
const requests = [];
const errors = [];

function record(check, passed, detail) {
  results.push({ check, passed, detail });
  console.log(JSON.stringify(results[results.length - 1]));
}

function makeWav(seconds = 2, sampleRate = 16000) {
  const dataSize = seconds * sampleRate * 2; // 16-bit mono
  const buf = Buffer.alloc(44 + dataSize);
  buf.write('RIFF', 0, 'ascii');
  buf.writeUInt32LE(36 + dataSize, 4);
  buf.write('WAVE', 8, 'ascii');
  buf.write('fmt ', 12, 'ascii');
  buf.writeUInt32LE(16, 16); // fmt chunk size
  buf.writeUInt16LE(1, 20); // PCM
  buf.writeUInt16LE(1, 22); // mono
  buf.writeUInt32LE(sampleRate, 24);
  buf.writeUInt32LE(sampleRate * 2, 28); // byte rate
  buf.writeUInt16LE(2, 32); // block align
  buf.writeUInt16LE(16, 34); // bits per sample
  buf.write('data', 36, 'ascii');
  buf.writeUInt32LE(dataSize, 40);
  return buf;
}

async function main() {
  const browser = await chromium.launch({
    executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true,
    args: ['--autoplay-policy=no-user-gesture-required'],
  });
  try {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await ctx.newPage();
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('request', (req) => {
      const u = new URL(req.url());
      if (u.pathname.startsWith('/api/')) {
        const h = req.headers();
        requests.push({ method: req.method(), path: u.pathname, hasAuth: !!(h && h.authorization), headerKeys: h ? Object.keys(h) : [] });
      }
    });
    const api = ctx.request;

    // ═══════════ 复验项 1：课时课件归属 + 课程共享展示规则 ═══════════
    const courseTitle1 = '课件归属验收-' + Date.now().toString(36).slice(-6);
    let r = await api.post(`${API}/api/courses`, { data: { title: courseTitle1, teacher: '张老师', term: '2026秋' } });
    const c1 = (await r.json()).id;
    r = await api.post(`${API}/api/courses/${c1}/lectures`, { data: { title: '第 1 讲 甲' } });
    const l1 = (await r.json()).id;
    r = await api.post(`${API}/api/courses/${c1}/lectures`, { data: { title: '第 2 讲 乙' } });
    const l2 = (await r.json()).id;

    async function uploadDoc(lectureId, name, content) {
      const q = lectureId ? `?lecture_id=${lectureId}` : '';
      const resp = await api.post(`${API}/api/courses/${c1}/documents${q}`, {
        multipart: { file: { name, mimeType: 'text/markdown', buffer: Buffer.from(content, 'utf8') } },
      });
      return (await resp.json()).id;
    }
    await uploadDoc(l1, 'courseware-A.md', '# 课件甲\nLECTURE_ONE_MARKER_ALPHA_2026\n');
    await uploadDoc(l2, 'courseware-B.md', '# 课件乙\nLECTURE_TWO_MARKER_BETA_2026\n');
    await uploadDoc(null, 'courseware-shared.md', '# 课程共享课件\nSHARED_MARKER_2026\n');

    async function readDocs(pg) {
      await pg.locator('[data-testid="course-doc"]').first().waitFor({ timeout: 10000 });
      return pg.locator('[data-testid="course-doc"]').evaluateAll((els) => els.map((e) => ({
        title: (e.querySelector('p')?.textContent || '').trim(),
        shared: e.getAttribute('data-shared'),
        lectureId: e.getAttribute('data-lecture-id'),
      })));
    }

    await page.goto(`${BASE}/courses/${c1}/lectures/${l1}`, { waitUntil: 'networkidle' });
    const d1 = await readDocs(page);
    const t1 = d1.map((d) => d.title);
    const shared1 = d1.find((d) => d.title === 'courseware-shared');
    const pass1 = t1.includes('courseware-A') && t1.includes('courseware-shared') && !t1.includes('courseware-B');
    const passShared = !!shared1 && shared1.shared === 'true' && shared1.lectureId === '';

    await page.reload({ waitUntil: 'networkidle' });
    const d1r = await readDocs(page);
    const t1r = d1r.map((d) => d.title);
    const pass1r = t1r.includes('courseware-A') && t1r.includes('courseware-shared') && !t1r.includes('courseware-B');

    await page.goto(`${BASE}/courses/${c1}/lectures/${l2}`, { waitUntil: 'networkidle' });
    const d2 = await readDocs(page);
    const t2 = d2.map((d) => d.title);
    const pass2 = t2.includes('courseware-B') && t2.includes('courseware-shared') && !t2.includes('courseware-A');

    await page.reload({ waitUntil: 'networkidle' });
    const d2r = await readDocs(page);
    const t2r = d2r.map((d) => d.title);
    const pass2r = t2r.includes('courseware-B') && t2r.includes('courseware-shared') && !t2r.includes('courseware-A');

    record('1-课时课件归属(首载/刷新/切换)+课程共享展示规则', pass1 && pass1r && pass2 && pass2r && passShared, {
      courseId: c1, l1, l2,
      lecture1: t1, lecture1_reload: t1r, lecture2: t2, lecture2_reload: t2r,
      shared_doc: shared1,
    });
    await page.screenshot({ path: path.join(OUT, 'item1-lecture1.png'), fullPage: true });
    await page.goto(`${BASE}/courses/${c1}/lectures/${l2}`, { waitUntil: 'networkidle' });
    await page.locator('[data-testid="course-doc"]').first().waitFor({ timeout: 10000 });
    await page.screenshot({ path: path.join(OUT, 'item1-lecture2.png'), fullPage: true });

    // ═══════════ 复验项 2：首次点选转写段（刷新后点第三段） ═══════════
    const segResp = await api.get(`${API}/api/lectures/${REAL_LECTURE_ID}/transcript`);
    const segs = await segResp.json();
    if (segs.length < 4) throw new Error(`真实课时转写段不足 4 段（实际 ${segs.length}）`);
    const seg0 = segs[0];
    const seg2 = segs[2]; // 第三段
    const seg3 = segs[3]; // 第四段

    await page.goto(`${BASE}/courses/${REAL_COURSE_ID}/lectures/${REAL_LECTURE_ID}`, { waitUntil: 'networkidle' });
    await page.locator('[data-testid="transcript-segment"]').first().waitFor({ timeout: 10000 });
    await page.waitForFunction(() => document.querySelector('[data-testid="lecture-audio"]')?.readyState >= 1, null, { timeout: 15000 });

    // 播放器完成加载后刷新，再「首次」点选
    await page.reload({ waitUntil: 'networkidle' });
    await page.locator('[data-testid="transcript-segment"]').first().waitFor({ timeout: 10000 });
    await page.waitForFunction(() => document.querySelector('[data-testid="lecture-audio"]')?.readyState >= 1, null, { timeout: 15000 });

    const segEls = page.locator('[data-testid="transcript-segment"]');
    const segCountShown = await segEls.count();

    async function clickAndRead(index, lo, hi) {
      await segEls.nth(index).click();
      await page.waitForFunction((rng) => {
        const a = document.querySelector('[data-testid="lecture-audio"]');
        return a && !a.paused && a.currentTime > rng.lo && a.currentTime < rng.hi;
      }, { lo, hi }, { timeout: 5000 });
      return page.locator('[data-testid="lecture-audio"]').evaluate((a) => a.currentTime);
    }

    const thirdT = await clickAndRead(2, seg2.start_ms / 1000 - 0.3, seg2.start_ms / 1000 + 0.7);
    const firstT = await clickAndRead(0, -0.1, 1.0);
    const fourthT = await clickAndRead(3, seg3.start_ms / 1000 - 0.3, seg3.start_ms / 1000 + 0.7);

    const passSeek = segCountShown >= 4
      && Math.abs(thirdT - seg2.start_ms / 1000) < 0.4
      && firstT < 1.0
      && Math.abs(fourthT - seg3.start_ms / 1000) < 0.4;
    record('2-首次点选转写段(刷新后第三段13.61s+连续跳转)', passSeek, {
      segCount: segCountShown,
      seg2_expected_ms: seg2.start_ms, seg2_expected_s: (seg2.start_ms / 1000).toFixed(3),
      third_s: Number(thirdT.toFixed(3)), first_s: Number(firstT.toFixed(3)), fourth_s: Number(fourthT.toFixed(3)),
      seg3_expected_ms: seg3.start_ms,
    });
    await page.locator('[data-testid="lecture-audio"]').evaluate((a) => a.pause());
    await page.screenshot({ path: path.join(OUT, 'item2-seek.png'), fullPage: false });

    // ═══════════ 复验项 3：页面录音/课件上传 + 失败重试 ═══════════
    const courseTitle3 = '页面证据验收-' + Date.now().toString(36).slice(-6);
    r = await api.post(`${API}/api/courses`, { data: { title: courseTitle3, teacher: '张老师', term: '2026秋' } });
    const c3 = (await r.json()).id;
    r = await api.post(`${API}/api/courses/${c3}/lectures`, { data: { title: '第 1 讲 页面证据' } });
    const l3 = (await r.json()).id;

    await page.goto(`${BASE}/courses/${c3}/lectures/${l3}`, { waitUntil: 'networkidle' });

    // 3a. 从页面上传带已知页码/标记的 PDF 课件 → 定位原页
    await page.locator('input[type="file"][accept*=".pdf"]').setInputFiles(PDF);
    await page.locator('[data-testid="course-doc"]').first().waitFor({ timeout: 10000 });
    await page.getByRole('button', { name: '查看原文' }).first().click();
    await page.locator('p.whitespace-pre-wrap').first().waitFor({ timeout: 10000 });

    const docList = await (await api.get(`${API}/api/courses/${c3}/documents`)).json();
    const pdfDoc = docList.find((d) => d.title === 'newton-lesson');
    const blocks = await (await api.get(`${API}/api/documents/${pdfDoc.id}/blocks`)).json();
    const p1 = blocks.find((b) => (b.text || '').includes('PAGE_ONE_MARKER_NEWTON_LAW_2026'));
    const p2 = blocks.find((b) => (b.text || '').includes('PAGE_TWO_MARKER_F_EQ_MA_2026'));
    const passPdfLoc = !!p1 && p1.page_no === 1 && !!p2 && p2.page_no === 2;
    const pageShowsPage2 = (await page.getByText('第 2 页').count()) > 0;
    record('3a-页面课件定位到对应原页(已知页码/标记)', passPdfLoc && pageShowsPage2, {
      docId: pdfDoc.id, blockCount: blocks.length, pageShowsPage2,
      page1: p1 && { page_no: p1.page_no, text: p1.text.slice(0, 60) },
      page2: p2 && { page_no: p2.page_no, text: p2.text.slice(0, 60) },
    });
    await page.screenshot({ path: path.join(OUT, 'item3-doc.png'), fullPage: true });

    // 3b. 模拟转写失败：从页面上传录音 → 派发 → 失败可见
    await api.post(`${API}/__test/asr-fail`);
    const audioInput = page.locator('input[type="file"][accept*=".wav"]');
    await audioInput.setInputFiles({ name: 'trial.wav', mimeType: 'audio/wav', buffer: makeWav(5) });
    const uploadBtn = page.getByRole('button', { name: '上传并转写' });
    await uploadBtn.waitFor({ timeout: 5000 });
    const [audioResp] = await Promise.all([
      page.waitForResponse((res) => res.url().includes(`/api/lectures/${l3}/audio`) && res.status() === 201, { timeout: 20000 }),
      uploadBtn.click(),
    ]);
    const audioJson = await audioResp.json();
    const jobId = audioJson.job_id;

    await api.post(`${API}/__test/dispatch`);
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-testid="job-status"]');
      return el && el.textContent.includes('retry_wait');
    }, null, { timeout: 10000 });
    const beforeStatus = await page.locator('[data-testid="job-status"]').textContent();
    const beforeError = await page.locator('[data-testid="job-error"]').textContent();
    const beforeJob = await (await api.get(`${API}/api/jobs/${jobId}`)).json();
    const beforeLect = await (await api.get(`${API}/api/lectures/${l3}`)).json();

    // 3c. 刷新 → 页面恢复最新任务（失败原因 + 重试按钮）
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-testid="job-status"]');
      return el && el.textContent.includes('retry_wait');
    }, null, { timeout: 10000 });
    const afterRefreshStatus = await page.locator('[data-testid="job-status"]').textContent();
    const retryBtn = page.locator('[data-testid="job-retry"]');
    const retryVisible = await retryBtn.isVisible();

    // 3d. 重试 → 有效结果
    await api.post(`${API}/__test/asr-ok`);
    const [retryResp] = await Promise.all([
      page.waitForResponse((res) => res.url().includes(`/api/jobs/${jobId}/retry`) && res.status() === 200, { timeout: 10000 }),
      retryBtn.click(),
    ]);
    const retryJson = await retryResp.json();
    await api.post(`${API}/__test/dispatch`);
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-testid="job-status"]');
      return el && el.textContent.includes('succeeded');
    }, null, { timeout: 15000 });
    await page.locator('[data-testid="transcript-segment"]').first().waitFor({ timeout: 10000 });

    const afterJob = await (await api.get(`${API}/api/jobs/${jobId}`)).json();
    const afterSegs = await (await api.get(`${API}/api/lectures/${l3}/transcript`)).json();
    const afterLect = await (await api.get(`${API}/api/lectures/${l3}`)).json();

    const passRetry = beforeStatus.includes('retry_wait')
      && beforeError.includes('供应商调用失败')
      && beforeJob.output_version === null
      && beforeLect.active_transcript === null
      && afterRefreshStatus.includes('retry_wait')
      && retryVisible
      && afterJob.status === 'succeeded'
      && afterJob.output_version !== null
      && afterSegs.length === 3
      && !!afterLect.active_transcript && afterLect.active_transcript.segment_count === 3;
    record('3bcd-页面录音上传+失败可见+刷新重试得有效结果', passRetry, {
      jobId,
      before: { status: beforeStatus, error: beforeError, jobStatus: beforeJob.status, outputVersion: beforeJob.output_version, activeTranscript: beforeLect.active_transcript },
      afterRefresh: { status: afterRefreshStatus, retryVisible },
      retry: { httpStatus: retryResp.status(), jobStatus: retryJson.status, outputVersion: retryJson.output_version },
      after: { jobStatus: afterJob.status, outputVersion: afterJob.output_version, segments: afterSegs.length, activeTranscript: afterLect.active_transcript },
    });
    await page.screenshot({ path: path.join(OUT, 'item3-retry.png'), fullPage: true });

    // ═══════════ A1a：浏览器请求审计（无身份头，服务端固定 local-user） ═══════════
    const apiReqs = requests.filter((rq) => rq.path.startsWith('/api/'));
    record('A1a-浏览器请求无身份头(服务端固定local-user)', apiReqs.length > 0 && apiReqs.every((rq) => !rq.hasAuth), {
      apiRequestCount: apiReqs.length,
      uniquePaths: [...new Set(apiReqs.map((rq) => rq.path))].slice(0, 20),
      sampleHeaderKeys: apiReqs.slice(0, 3).map((rq) => rq.headerKeys),
    });

    record('浏览器无页面脚本错误', errors.length === 0, { count: errors.length });
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({
      results, requests, errors,
      fixture: false,
      asr: 'FakeASRProvider（可控替身，按需派发，不触发真实 DashScope 收费）',
      note: '真实本地前端 Vite + 真实本地后端；无请求拦截/夹具；失败→重试由 /__test/dispatch 精确控制',
    }, null, 2));
  }
  if (results.some((r) => !r.passed)) process.exitCode = 1;
}

main().catch((error) => {
  fs.writeFileSync(path.join(OUT, 'fatal.txt'), error.stack);
  console.error(error.message);
  process.exitCode = 2;
});
