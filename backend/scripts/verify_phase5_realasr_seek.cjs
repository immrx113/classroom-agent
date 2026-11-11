/* 阶段 5 最终验收补证 · A2「真实录音 + 真实 ASR + 转写段点击与原句对应」浏览器实测。
 *
 * 背景：A2 冻结判据要求「真实 ASR 产生有序时间段，播放器定位到对应原句」。
 *   阶段 2 已在活跃库 yuan_classroom_local 用真实录音 newton-trial.wav + 真实 DashScope
 *   ASR（provider=real，provider_task_id 持久化）产出 5 个有序转写段，证据落盘于
 *   evidence/phase2/real_results.json。本脚本在「最终页面版本」（当前前端路由 = 课程列表入口、
 *   lecture-detail-page 含转写段点选回听 + 全课整理与发布卡片）上，复用该真实课时，重新执行
 *   「点选转写段 → 播放器 seek 到对应原句时间」这一浏览器行为，产出最终页面版本的定位证据。
 *
 * 复用 vs 重新执行：
 *   - 真实录音：复用 newton-trial.wav（已存为 assets/88e501fd….wav，22.5kHz 单声道 45.28s）。
 *   - 真实 ASR：复用阶段 2 的 DashScope 真实结果（provider=real，5 段，未重新计费调用）。
 *   - 转写段点击定位：本脚本在最终页面版本上重新执行（新证据）。
 *
 * 前置：真实后端（uvicorn app.main:app，连 yuan_classroom_local）+ 真实前端（Vite :5173）已起。
 * 运行（backend 目录，Node）：
 *   node scripts/verify_phase5_realasr_seek.cjs
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('C:/Users/11322/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const ROOT = path.resolve(__dirname, '../..');
const OUT = path.join(ROOT, 'docs/classroom-agent/reset-v1/evidence/phase5/realasr');
fs.mkdirSync(OUT, { recursive: true });

const BASE = 'http://localhost:5173';
const API = 'http://localhost:8787';

// 活跃库中真实课时：第 1 讲 牛顿第二定律（真实录音 + 真实 DashScope ASR 5 段）
const REAL_COURSE_ID = 'fcc5ec19-36bb-402e-a9df-b430f9d1ac40';
const REAL_LECTURE_ID = '4b141489-551f-4aa6-8653-b2d0790a71e8';

const results = [];
const errors = [];

function record(check, passed, detail) {
  results.push({ check, passed, detail });
  console.log(JSON.stringify(results[results.length - 1]));
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
    const api = ctx.request;

    // 先经真实 HTTP 读取后端所见的真实转写段（provider=real 证据基线）
    const lectResp = await api.get(`${API}/api/lectures/${REAL_LECTURE_ID}`);
    const lecture = await lectResp.json();
    const segResp = await api.get(`${API}/api/lectures/${REAL_LECTURE_ID}/transcript`);
    const segs = await segResp.json();
    if (!lecture.active_transcript || lecture.active_transcript.provider !== 'real') {
      throw new Error(`真实课时无 real 转写：${JSON.stringify(lecture.active_transcript)}`);
    }
    if (segs.length < 5) throw new Error(`真实转写段不足 5 段（实际 ${segs.length}）`);

    // 页面：最终版本 lecture-detail-page
    await page.goto(`${BASE}/courses/${REAL_COURSE_ID}/lectures/${REAL_LECTURE_ID}`, { waitUntil: 'networkidle' });
    await page.locator('[data-testid="transcript-segment"]').first().waitFor({ timeout: 15000 });
    await page.waitForFunction(() => document.querySelector('[data-testid="lecture-audio"]')?.readyState >= 1, null, { timeout: 15000 });

    // 读取页面上展示的转写段文本（原句）与时间区间，与后端真实 ASR 段逐一对齐
    const pageSegs = await page.locator('[data-testid="transcript-segment"]').evaluateAll((els) =>
      els.map((e) => ({
        text: (e.querySelector('p')?.textContent || '').trim(),
        badge: (e.querySelector('span')?.textContent || '').trim(),
      })),
    );
    const segCountShown = pageSegs.length;

    // 点选转写段 → 播放器 seek 到对应原句时间（与阶段 2 相同的判定窗口）
    async function clickAndRead(index, lo, hi) {
      await page.locator('[data-testid="transcript-segment"]').nth(index).click();
      await page.waitForFunction((rng) => {
        const a = document.querySelector('[data-testid="lecture-audio"]');
        return a && !a.paused && a.currentTime > rng.lo && a.currentTime < rng.hi;
      }, { lo, hi }, { timeout: 6000 });
      return page.locator('[data-testid="lecture-audio"]').evaluate((a) => a.currentTime);
    }

    // 依次点第 3 段(13610ms) / 第 1 段(0ms) / 第 4 段(23200ms)
    const seg2 = segs[2];
    const seg0 = segs[0];
    const seg3 = segs[3];
    const thirdT = await clickAndRead(2, seg2.start_ms / 1000 - 0.3, seg2.start_ms / 1000 + 0.7);
    const firstT = await clickAndRead(0, -0.1, 1.0);
    const fourthT = await clickAndRead(3, seg3.start_ms / 1000 - 0.3, seg3.start_ms / 1000 + 0.7);

    const passSeek = segCountShown === 5
      && Math.abs(thirdT - seg2.start_ms / 1000) < 0.4
      && firstT < 1.0
      && Math.abs(fourthT - seg3.start_ms / 1000) < 0.4;

    // 页面转写段文本与后端真实 ASR 原句一致（原句对应）
    const textsMatch = pageSegs.length === segs.length && pageSegs.every((p, i) => p.text === segs[i].content);

    record('真实课时最终页面·点选转写段定位到对应原句', passSeek, {
      provider: lecture.active_transcript.provider,
      segment_count_shown: segCountShown,
      expected: { seg2_ms: seg2.start_ms, seg3_ms: seg3.start_ms },
      actual: { third_s: Number(thirdT.toFixed(3)), first_s: Number(firstT.toFixed(3)), fourth_s: Number(fourthT.toFixed(3)) },
      page_texts: pageSegs.map((p) => ({ badge: p.badge, text: p.text })),
    });

    record('页面转写段文本与真实 ASR 原句逐段一致', textsMatch, {
      backend_segments: segs.map((s) => ({ seq: s.seq_index, start_ms: s.start_ms, end_ms: s.end_ms, text: s.content })),
      page_segments: pageSegs,
    });

    // 截图：点回第 3 段，展示「原句 + 时间徽标 + 播放器」同屏
    await page.locator('[data-testid="transcript-segment"]').nth(2).click();
    await page.waitForFunction((rng) => {
      const a = document.querySelector('[data-testid="lecture-audio"]');
      return a && !a.paused && a.currentTime > rng.lo && a.currentTime < rng.hi;
    }, { lo: seg2.start_ms / 1000 - 0.3, hi: seg2.start_ms / 1000 + 0.7 }, { timeout: 6000 });
    await page.locator('[data-testid="lecture-audio"]').evaluate((a) => a.pause());
    await page.screenshot({ path: path.join(OUT, 'item-seek.png'), fullPage: false });

    record('浏览器无页面脚本错误', errors.length === 0, { count: errors.length });
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(OUT, 'results.json'), JSON.stringify({
      results,
      errors,
      fixture: false,
      recording: 'newton-trial.wav（复用阶段 2 真实录音，22.5kHz 单声道 45.28s，已存为 assets/88e501fd-….wav）',
      asr: '真实 DashScope（provider=real，复用阶段 2 真实结果，未重新计费调用）',
      note: '真实本地后端（uvicorn app.main:app 连 yuan_classroom_local）+ 真实 Vite 前端（最终页面版本）；无请求拦截/夹具',
    }, null, 2));
  }
  if (results.some((r) => !r.passed)) process.exitCode = 1;
}

main().catch((error) => {
  fs.writeFileSync(path.join(OUT, 'fatal.txt'), error.stack);
  console.error(error.message);
  process.exitCode = 2;
});
