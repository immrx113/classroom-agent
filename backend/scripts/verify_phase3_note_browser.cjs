/* P03-14 only: actual delivered page, valid WAV, frozen three citation cases. */
const fs = require('fs');
const path = require('path');
const http = require('http');
const {spawnSync} = require('child_process');
const {chromium} = require('C:/Users/11322/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=path.resolve(__dirname,'../..');
const frontend=path.join(root,'frontend');
const out=process.env.PHASE3_BROWSER_OUT || path.join(root,'docs/classroom-agent/acceptance/evidence-phase3-recheck-20261029');
fs.mkdirSync(out,{recursive:true});
const results=[];
const requests=[];
function record(check,ok,detail){
  const row={id:'P03-14',check,result:ok?'通过':'不通过',detail};
  results.push(row);console.log(JSON.stringify(row));
}
function wav(freq){
  const samples=16000*8, data=Buffer.alloc(44+samples*2);
  data.write('RIFF',0);data.writeUInt32LE(data.length-8,4);data.write('WAVEfmt ',8);
  data.writeUInt32LE(16,16);data.writeUInt16LE(1,20);data.writeUInt16LE(1,22);
  data.writeUInt32LE(16000,24);data.writeUInt32LE(32000,28);data.writeUInt16LE(2,32);
  data.writeUInt16LE(16,34);data.write('data',36);data.writeUInt32LE(samples*2,40);
  for(let i=0;i<samples;i++)data.writeInt16LE(Math.round(10000*Math.sin(2*Math.PI*freq*i/16000)),44+i*2);
  return data;
}
const audioOld=wav(440),audioNew=wav(880);
let currentMedia='audio-old';
const fixedNote={id:'review-note',lecture_id:'review-lecture',transcript_version_id:'version-old',
  status:'published',pipeline_version:'browser-review',created_at:'2026-10-29T00:00:00Z',
  content:{title:'P03-14 原冻结引用回听',outline:['来源'],coverage:{complete:true,total_chunks:1,succeeded_chunks:1},
    source_version:{transcript_version_id:'version-old'},tasks:[],sections:[{heading:'来源',points:[
      {text:'有时间的末尾事实',status:'confirmed',origin:'teacher_explicit',evidence_ids:['ev-time']},
      {text:'缺时间的原文事实',status:'confirmed',origin:'teacher_explicit',evidence_ids:['ev-no-time']},
      {text:'切换当前版本后仍属旧版的事实',status:'confirmed',origin:'teacher_explicit',evidence_ids:['ev-old']}
    ]}]}};
function evidence(id){return {id,source_type:'transcript',source_version_id:'version-old',source_version:1,
  source_version_status:currentMedia==='audio-old'?'active':'superseded',source_audio_asset_id:'audio-old',
  source_lecture_id:'review-lecture',source_course_id:'review-course',is_current:currentMedia==='audio-old',
  is_valid:true,quote:id==='ev-no-time'?'缺时间的原文事实':'有时间的事实',
  content:[id==='ev-no-time'?'缺时间的原文事实':'有时间的事实'],
  segment_start:0,segment_end:0,start_ms:id==='ev-no-time'?null:3000,end_ms:id==='ev-no-time'?null:5000};}
function json(res,value){res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(value));}
function media(req,res,asset){
  const bytes=asset==='audio-old'?audioOld:audioNew;
  requests.push({type:'audio',path:req.url,asset});
  let start=0,end=bytes.length-1,status=200;
  const match=(req.headers.range||'').match(/^bytes=(\d+)-(\d*)$/);
  if(match){start=Number(match[1]);end=match[2]?Math.min(Number(match[2]),end):end;status=206;}
  const headers={'Content-Type':'audio/wav','Content-Length':end-start+1,'Accept-Ranges':'bytes','Cache-Control':'no-store'};
  if(status===206)headers['Content-Range']=`bytes ${start}-${end}/${bytes.length}`;
  res.writeHead(status,headers);res.end(bytes.subarray(start,end+1));
}
const server=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/api/notes/review-note')return json(res,fixedNote);
  if(url.pathname.startsWith('/api/evidence/')){
    const id=url.pathname.split('/').pop();requests.push({type:'evidence',id,source_asset:'audio-old'});
    return json(res,evidence(id));
  }
  if(url.pathname==='/api/meetings/review-lecture/audio')return media(req,res,currentMedia);
  if(url.pathname==='/api/audio-assets/audio-old/content')return media(req,res,'audio-old');
  if(url.pathname==='/api/audio-assets/audio-new/content')return media(req,res,'audio-new');
  if(url.pathname.startsWith('/api/')){res.writeHead(404);return res.end('{}');}
  let relative=url.pathname==='/'?'index.html':url.pathname.slice(1);
  let file=path.resolve(frontend,'dist',relative);
  if(!file.startsWith(path.resolve(frontend,'dist')+path.sep)||!fs.existsSync(file)||fs.statSync(file).isDirectory())file=path.join(frontend,'dist/index.html');
  const ext=path.extname(file);
  res.writeHead(200,{'Content-Type':({'.html':'text/html','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml'})[ext]||'application/octet-stream'});
  fs.createReadStream(file).pipe(res);
});
async function main(){
  const started=new Date().toISOString();let browser;
  if(!process.argv.includes('--skip-build'))for(const [label,args] of [
    ['typecheck',[path.join(frontend,'node_modules/typescript/bin/tsc'),'-b']],
    ['build',[path.join(frontend,'node_modules/vite/bin/vite.js'),'build']]
  ]){
    const proc=spawnSync(process.execPath,args,{cwd:frontend,encoding:'utf8',windowsHide:true});
    fs.writeFileSync(path.join(out,label+'.log'),(proc.stdout||'')+'\n'+(proc.stderr||''));
    if(proc.status!==0)throw new Error(label+' failed: see log');
  }
  try{
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true,args:['--autoplay-policy=no-user-gesture-required']});
    const page=await browser.newPage({viewport:{width:1280,height:900}});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    const url=`http://127.0.0.1:${server.address().port}/notes/review-note`;
    await page.goto(url);
    await page.getByText('有时间的末尾事实',{exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelector('[data-testid="note-audio"]')?.readyState>=1);
    await page.getByRole('button',{name:'回听来源'}).nth(0).click();
    await page.waitForFunction(()=>{const a=document.querySelector('[data-testid="note-audio"]');return a.currentTime>3.1&&!a.paused;});
    let state=await page.locator('[data-testid="note-audio"]').evaluate(a=>({src:a.currentSrc,time:a.currentTime,paused:a.paused,duration:a.duration,readyState:a.readyState}));
    record('有时间引用在实际浏览器定位并播放有效音频',state.time>=3&&!state.paused&&state.duration===8,{...state,fixture_asset:currentMedia});
    await page.locator('[data-testid="note-audio"]').evaluate(a=>a.pause());
    const beforeTime=await page.locator('[data-testid="note-audio"]').evaluate(a=>a.currentTime);
    await page.getByRole('button',{name:'回听来源'}).nth(1).click();
    await page.getByText('该引用来源缺时间，仅定位原文，不回听',{exact:true}).waitFor();
    state=await page.locator('[data-testid="note-audio"]').evaluate(a=>({time:a.currentTime,paused:a.paused}));
    record('缺时间引用只展示原文不伪造播放位置',state.paused&&Math.abs(state.time-beforeTime)<0.2,state);
    // The exact frozen step: current transcript/audio changes, then reopen the old note.
    currentMedia='audio-new';
    await page.reload();
    await page.getByText('有时间的末尾事实',{exact:true}).waitFor();
    await page.waitForFunction(()=>document.querySelector('[data-testid="note-audio"]')?.readyState>=1);
    await page.getByRole('button',{name:'回听来源'}).nth(2).click();
    await page.getByText('旧版本',{exact:true}).waitFor();
    await page.waitForFunction(()=>{const a=document.querySelector('[data-testid="note-audio"]');return a.currentTime>3.1&&!a.paused;});
    state=await page.locator('[data-testid="note-audio"]').evaluate(a=>({src:a.currentSrc,time:a.currentTime,paused:a.paused,duration:a.duration}));
    const served=requests.filter(r=>r.type==='audio').at(-1);
    record('切换当前转写后旧笔记引用仍播放固定旧音频',served?.asset==='audio-old',
      {expected_asset:'audio-old',actual_asset:served?.asset,requested_path:served?.path,history_badge:true,...state});
    await page.screenshot({path:path.join(out,'P03-14-old-source.png'),fullPage:true});
    fs.writeFileSync(path.join(out,'browser-errors.json'),JSON.stringify(errors,null,2));
  }finally{
    if(browser)await browser.close();
    if(server.listening)await new Promise(resolve=>server.close(resolve));
    fs.writeFileSync(path.join(out,'browser-results.json'),JSON.stringify({started,finished:new Date().toISOString(),fixture:'Actual compiled page and real browser/media decoding; local API fixtures represent note V1, old asset A1 and current asset A2. No paid model or production DB.',results,requests},null,2));
  }
  if(results.some(r=>r.result==='不通过'))process.exitCode=1;
}
main().catch(error=>{fs.writeFileSync(path.join(out,'browser-fatal.txt'),error.stack||String(error));console.error(error);process.exitCode=2;});
