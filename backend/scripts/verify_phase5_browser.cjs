/* 阶段 5「入口切换与整体回归」A7 浏览器实测（真实本地前端 Vite + 真实本地后端，无请求拦截/夹具）。
 *
 * 从默认首页走完整链路：默认首页(=课程列表) → 新建课程 → 新建课时 → 上传录音+课件 →
 * 转写（FakeASRProvider）→ 整理（FakeOrganizeModel）→ 草稿确认 → 发布 → 问答 → 引用回跳（打开笔记）。
 * 课程/课时由页面真实创建，不预置种子业务数据（对齐 A7「用户全程可经课堂页面完成任务」）。
 *
 * 后端：scripts/serve_phase5_browser.py（真实 app.main:app + 独立种子库 + 后台派发循环，无收费模型）。
 * 前端：Vite dev server :5173，/api 代理到 :8787。
 * 样例：scripts/seed_phase5_browser.py 生成的 8s WAV + 带标记 Markdown 课件。
 *
 * 运行（backend 目录，Node，需先启动后端 8787 与前端 5173）：
 *   node scripts/verify_phase5_browser.cjs
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('C:/Users/11322/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const ROOT = path.resolve(__dirname, '../..');
const OUT = path.join(ROOT, 'docs/classroom-agent/reset-v1/evidence/phase5/browser');
fs.mkdirSync(OUT, { recursive: true });

const BASE = 'http://localhost:5173';
const state = JSON.parse(fs.readFileSync(path.join(ROOT, 'backend/runtime/phase5-browser-state.json'), 'utf8'));
const MARKERS = state.markers;

const results = [];
const requests = [];
const errors = [];

function record(id, check, passed, detail) {
  results.push({ id, check, result: passed ? '通过' : '不通过', detail });
  console.log(JSON.stringify(results[results.length - 1]));
}

async function waitForTestId(page, testid, timeout = 30000) {
  await page.waitForSelector(`[data-testid="${testid}"]`, { timeout });
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

    // ═══ 0. 默认首页 = 课程列表（入口切换） ═══
    await page.goto(BASE + '/', { waitUntil: 'networkidle' });
    const homeIsCourse = (await page.getByRole('heading', { name: '课程管理' }).count()) >= 1;
    record('A7-browser-0', '默认首页为课堂课程列表（入口切换）', homeIsCourse, {
      url: page.url(), heading_course_list: homeIsCourse,
    });
    await page.screenshot({ path: path.join(OUT, 'item0-home.png'), fullPage: true });

    // ═══ 1. 新建课程 ═══
    const courseTitle = '阶段五验收课程';
    await page.getByRole('button', { name: '新建课程' }).click();
    await page.fill('#course-title', courseTitle);
    await page.getByRole('button', { name: '创建', exact: true }).click();
    await page.getByText(courseTitle, { exact: true }).waitFor({ timeout: 15000 });
    const courseCard = page.locator('a', { hasText: courseTitle }).first();
    await courseCard.click();
    await page.waitForURL(/\/courses\/[0-9a-f-]{36}$/, { timeout: 15000 });
    const courseId = page.url().split('/').pop();
    record('A7-browser-1', '新建课程（页面创建，进入课程详情）', !!courseId && courseId.length === 36, {
      course_title: courseTitle, course_id: courseId, url: page.url(),
    });

    // ═══ 2. 新建课时 ═══
    const lectureTitle = '第 1 讲 加法';
    await page.getByRole('button', { name: '新建课时' }).click();
    await page.fill('#lecture-title', lectureTitle);
    await page.getByRole('button', { name: '创建', exact: true }).click();
    await page.getByText(lectureTitle, { exact: true }).waitFor({ timeout: 15000 });
    await page.locator('a', { hasText: lectureTitle }).first().click();
    await page.waitForURL(/\/lectures\/[0-9a-f-]{36}$/, { timeout: 15000 });
    const lectureId = page.url().split('/').pop();
    record('A7-browser-2', '新建课时（页面创建，进入课时详情）', !!lectureId && lectureId.length === 36, {
      lecture_title: lectureTitle, lecture_id: lectureId, url: page.url(),
    });

    // ═══ 3. 上传录音 → 转写（FakeASRProvider，产生带标记片段） ═══
    await page.setInputFiles('input[type="file"][accept*=".wav"]', state.wav);
    await page.getByRole('button', { name: '上传并转写' }).click();
    await page.waitForSelector('[data-testid="transcript-segment"]', { timeout: 60000 });
    const segCount = await page.locator('[data-testid="transcript-segment"]').count();
    const segText = await page.locator('[data-testid="transcript-segment"]').allTextContents();
    const transcriptOk = segCount >= 1 && segText.some((t) => t.includes(MARKERS.example));
    record('A7-browser-3', '上传录音并转写（片段含例题标记）', transcriptOk, {
      segment_count: segCount, contains_example_marker: transcriptOk,
      segments: segText.slice(0, 6),
    });

    // ═══ 4. 上传课件（Markdown，带已知标记） ═══
    await page.setInputFiles('input[type="file"][accept*=".md"]', state.markdown);
    await page.waitForSelector('[data-testid="course-doc"]', { timeout: 30000 });
    const docTitles = await page.locator('[data-testid="course-doc"]').allTextContents();
    const docOk = docTitles.length >= 1;
    record('A7-browser-4', '上传课件（页面出现课件条目）', docOk, {
      doc_count: docTitles.length, docs: docTitles.slice(0, 5),
    });
    await page.screenshot({ path: path.join(OUT, 'item1-upload.png'), fullPage: true });

    // ═══ 5. 整理本课 → 草稿 ═══
    await page.getByTestId('organize-button').click();
    await page.waitForSelector('[data-testid="note-status"]', { timeout: 60000 });
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-testid="note-status"]');
      return el && el.textContent.includes('草稿');
    }, { timeout: 60000 });
    const notePreview = await page.getByTestId('note-preview').textContent();
    const draftOk = notePreview.includes(MARKERS.example) && notePreview.includes('2+3=5');
    record('A7-browser-5', '整理产出草稿（草稿状态 + 例题可核对，未发布）', draftOk, {
      note_status: await page.getByTestId('note-status').textContent(),
      note_preview: notePreview.slice(0, 400),
    });
    await page.screenshot({ path: path.join(OUT, 'item2-organize.png'), fullPage: true });

    // ═══ 6. 草稿确认 → 发布 ═══
    await page.getByTestId('publish-button').click();
    await page.waitForFunction(() => {
      const el = document.querySelector('[data-testid="note-status"]');
      return el && el.textContent.includes('已发布');
    }, { timeout: 60000 });
    const publishedOk = (await page.getByTestId('note-status').textContent()).includes('已发布');
    record('A7-browser-6', '草稿确认并发布（状态变为已发布）', publishedOk, {
      note_status: await page.getByTestId('note-status').textContent(),
    });
    await page.screenshot({ path: path.join(OUT, 'item3-publish.png'), fullPage: true });

    // ═══ 7. 问答 + 引用回跳（课堂问答页，选择课程 → 提问 → 打开笔记） ═══
    // 发布仅创建 IndexIntent(pending)，由后台派发循环（约 1s/轮）异步构建+激活索引；
    // 此处等待其完成，避免问答早于索引就绪（不使用任何测试后门，仅等待正常异步索引）。
    await page.waitForTimeout(4000);
    await page.goto(BASE + '/classroom-qa', { waitUntil: 'networkidle' });
    await page.locator('select').first().waitFor({ timeout: 15000 });
    await page.waitForFunction((cid) => {
      const s = document.querySelector('select');
      return s && Array.from(s.options).some((o) => o.value === cid);
    }, courseId, { timeout: 15000 });
    await page.locator('select').first().selectOption(courseId);

    await page.getByPlaceholder('输入问题，Enter 发送...').fill(MARKERS.example);
    await page.getByRole('button', { name: '提问' }).click();
    await page.waitForSelector('[data-testid="qa-answer"]', { timeout: 30000 });
    const answer = await page.getByTestId('qa-answer').last().textContent();
    const noteBtn = page.getByRole('button', { name: '打开笔记' }).first();
    await noteBtn.waitFor({ timeout: 30000 });
    const answerCitesNote = answer.includes(MARKERS.example) && answer.includes('笔记');
    await noteBtn.click();
    await page.waitForFunction((mk) => {
      const el = document.querySelector('[data-testid="qa-note-content"]');
      return el && el.textContent.includes(mk);
    }, MARKERS.example, { timeout: 30000 });
    const openedNote = await page.getByTestId('qa-note-content').textContent();
    const citationOk = answerCitesNote && openedNote.includes(MARKERS.example) && openedNote.includes('2+3=5');
    record('A7-browser-7', '问答检索已发布笔记并引用回跳（打开笔记正文与回答引用一致）', citationOk, {
      course_id: courseId, lecture_id: lectureId, marker: MARKERS.example,
      answer_cites_note: answerCitesNote,
      opened_note_contains_marker: openedNote.includes(MARKERS.example),
      opened_note_contains_derivation: openedNote.includes('2+3=5'),
      answer: answer.slice(0, 300),
      opened_note: openedNote.slice(0, 300),
    });
    await page.screenshot({ path: path.join(OUT, 'item4-qa.png'), fullPage: true });

    // ═══ 8. 无页面脚本错误 ═══
    record('A7-browser-8', '浏览器无页面脚本错误', errors.length === 0, { count: errors.length });
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({
      started: new Date().toISOString(), finished: new Date().toISOString(),
      markers: state.markers, results, requests, errors, fixture: false,
      note: '真实本地前端 Vite + 真实本地后端（独立种子库 yuan_resetv1_p5_browser）；课程/课时由页面真实创建，转写 FakeASRProvider + 整理 FakeOrganizeModel + 确定性嵌入 + 确定性摘录答案，无请求拦截/夹具，无收费模型调用。',
    }, null, 2));
  }
  if (results.some((r) => r.result === '不通过')) process.exitCode = 1;
}

main().catch((error) => {
  fs.writeFileSync(path.join(OUT, 'fatal.txt'), error.stack || String(error));
  console.error(error);
  process.exitCode = 2;
});
