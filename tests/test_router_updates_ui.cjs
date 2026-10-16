const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve('.test-output/router-updates');
fs.mkdirSync(root, {recursive: true});
const defaultRepo = execFileSync(process.env.TEST_PYTHON || 'python', ['-c', String.raw`
import importlib.util, os, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as state:
    os.environ['OWRT_REMOTE_STATE_DIR']=state
    spec=importlib.util.spec_from_file_location('preview_updates','vps/owrt-remote-hub.py')
    hub=importlib.util.module_from_spec(spec);sys.modules[spec.name]=hub;spec.loader.exec_module(hub)
    out=Path('.test-output/router-updates')
    out.joinpath('updates.html').write_text(hub.router_updates_html('test-csrf'),encoding='utf-8')
    out.joinpath('dashboard.html').write_text(hub.dashboard_html([],'owner',auth_bootstrap={'username':'owner','is_owner':True}),encoding='utf-8')
    print(hub.RAW_REPO_BASE)
`], {env:{...process.env,PYTHONIOENCODING:'utf-8'},encoding:'utf-8'}).trim();
let job = null, routers = [], starts = [], polls = 0;
function data() {return {ok:true,job,routers};}
function send(res, data) {res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));}
const server = http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/api/router-updates') {polls++;return send(res,data());}
  if(url.pathname==='/api/router-updates/refresh') {
    assert.equal(req.headers['x-router-update-csrf'],'test-csrf');
    let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
      const payload=JSON.parse(body);
      if(job&&job.id===payload.id&&job.state!=='running')job=null;
      send(res,data());
    });return;
  }
  if(url.pathname==='/api/router-updates/start') {
    assert.equal(req.headers['x-router-update-csrf'],'test-csrf');
    let body='';req.on('data',data=>body+=data);req.on('end',()=>{
      const payload=JSON.parse(body);starts.push(payload);
      job={id:'test-task',request_id:payload.request_id,state:'running',cancel_requested:false,items:[{id:'node-5',name:'АНТОН ВПН',state:'running',message:'Установка выполняется',output:'one\ntwo'}]};
      send(res,data());
    });return;
  }
  if(url.pathname==='/api/router-updates/cancel') {
    job.cancel_requested=true;job.state='finished';job.items[0].state='cancelled';
    job.items[0].message='Остановка на роутере подтверждена';return send(res,data());
  }
  if(url.pathname==='/api/auth/meta')return send(res,{username:'owner',is_owner:true,users:[],sessions:[],captcha:{mode:'digits'}});
  if(url.pathname.startsWith('/api/'))return send(res,{});
  res.setHeader('Content-Type','text/html; charset=utf-8');
  res.end(fs.readFileSync(path.join(root,url.pathname==='/'?'dashboard.html':'updates.html')));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,channel:'chrome'});
  try {
    for(const mobile of [false,true]) {
      job=null;
      routers=[{id:'node-5',name:'АНТОН ВПН',online:true,last_seen:Math.floor(Date.now()/1000),xray:'running'}];
      const page=await browser.newPage({viewport:mobile?{width:390,height:844}:{width:1280,height:900},isMobile:mobile,hasTouch:mobile});
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      const base=`http://127.0.0.1:${server.address().port}`;
      await page.goto(base+'/router-updates/');
      await page.waitForFunction(()=>!document.getElementById('start').disabled);
      assert.equal(await page.locator('.router strong').textContent(),'АНТОН ВПН');
      assert.equal(await page.locator('.router small').textContent(),'ID: node-5');
      assert.equal(await page.locator('#countTotal').textContent(),'1');
      assert.equal(await page.locator('#countSuccess').textContent(),'0');
      assert.equal(await page.locator('.live-badge').textContent(),'● Онлайн');
      assert.equal(await page.locator('#command').inputValue(),'wget -O - "'+defaultRepo+'/install.sh?v=$(date +%s)" | sh');
      const command=mobile?'https://another.example/updates/router.sh?v=2&from=hub':'wget -O - "https://repo.example/install.sh?v=$(date +%s)" | sh';
      await page.locator('#command').fill(command);
      await page.locator('#sshPassword').fill('test-password');
      await page.locator('#start').click();
      await page.waitForFunction(()=>document.getElementById('status').textContent.includes('Обновление выполняется'));
      assert.equal(starts.at(-1).command,command);assert.equal(starts.at(-1).ssh_password,'test-password');
      assert.equal(await page.locator('#sshPassword').inputValue(),'');
      assert.equal(await page.locator('#start').isDisabled(),true);
      assert.equal(await page.locator('#cancel').isEnabled(),true);
      assert.equal(await page.locator('#countActive').textContent(),'1');
      await page.locator('summary').click();
      job.items[0].output+='\nupdated';
      await page.waitForFunction(()=>document.querySelector('pre').textContent.includes('updated'));
      assert.equal(await page.locator('details').evaluate(node=>node.open),true);
      await page.reload();
      await page.waitForFunction(()=>document.getElementById('cancel').disabled===false);
      const count=starts.length;
      assert.equal(await page.locator('#start').isDisabled(),true);
      routers[0].online=false;routers[0].xray='stopped';
      await page.locator('#refreshRouters').click();
      await page.waitForFunction(()=>document.getElementById('status').textContent.includes('Статусы обновлены'));
      assert.equal(job.state,'running');
      assert.equal(starts.length,count);
      assert.equal(await page.locator('.live-badge').textContent(),'● Офлайн');
      assert.equal(await page.locator('#countActive').textContent(),'1');
      assert.equal(await page.locator('#start').isDisabled(),true);
      if(mobile) {
        await page.locator('#cancel').click();
        await page.waitForFunction(()=>document.querySelector('.router strong').textContent==='Остановлено — АНТОН ВПН');
        assert.equal(job.cancel_requested,true);
        assert.equal(await page.locator('#cancel').isDisabled(),true);
        assert.equal(await page.locator('#cancel').textContent(),'Остановить всё');
      }
      job.state='finished';job.items[0].state='success';job.items[0].message='Установка успешно завершена, rc=0';
      await page.waitForFunction(()=>document.getElementById('status').textContent.includes('Очередь завершена'));
      await page.waitForFunction(()=>document.querySelector('.router strong').textContent==='Успешно — АНТОН ВПН');
      assert.equal(await page.locator('.router strong').textContent(),'Успешно — АНТОН ВПН');
      assert.equal(await page.locator('#countSuccess').textContent(),'1');
      assert.equal(await page.locator('#queueProgress').getAttribute('aria-valuenow'),'100');
      await page.locator('#command').fill(command);
      assert.equal(starts.length,count);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      assert.equal(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}).includes('test-password')),false);
      await page.screenshot({path:path.join(root,mobile?'mobile.png':'desktop.png'),fullPage:true});
      // Match the user's example exactly, including all finished outcomes.
      job.items=Array.from({length:35},(_,i)=>({id:'node-'+(i+1),name:'Роутер '+(i+1),state:i<21?'success':i<25?'error':i<26?'unknown':i<27?'cancelled':'skipped',message:'Итог установки'}));
      await page.waitForFunction(()=>document.getElementById('countTotal').textContent==='35');
      for(const [key,value] of Object.entries({Success:21,Error:4,Unknown:1,Cancelled:1,Skipped:8,Total:35,Active:0,Queued:0})) {
        assert.equal(await page.locator('#count'+key).textContent(),String(value));
      }
      assert.equal(await page.locator('#queueProgress').getAttribute('aria-valuenow'),'100');
      await page.locator('.queue-dashboard').screenshot({path:path.join(root,mobile?'mobile-summary.png':'desktop-summary.png')});
      routers=[{id:'node-5',name:'АНТОН ВПН',online:true,last_seen:Math.floor(Date.now()/1000),xray:'running'},{id:'node-36',name:'Новый роутер',online:false,last_seen:0,xray:'unknown'}];
      await page.locator('#refreshRouters').click();
      await page.waitForFunction(()=>document.getElementById('status').textContent.includes('Можно запустить новую очередь'));
      assert.equal(job,null);
      assert.equal(starts.length,count,'Refresh must never execute an update');
      assert.equal(await page.locator('.router').count(),2);
      assert.equal(await page.locator('.router strong').first().textContent(),'АНТОН ВПН');
      assert.equal(await page.locator('#countTotal').textContent(),'2');
      assert.equal(await page.locator('#countSuccess').textContent(),'0');
      assert.equal(await page.locator('#start').isEnabled(),true);
      assert.equal(await page.locator('#command').inputValue(),command);
      await page.reload();
      await page.waitForFunction(()=>document.getElementById('countTotal').textContent==='2');
      assert.equal(await page.locator('#countSuccess').textContent(),'0');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      assert.deepEqual(errors,[]);
      await page.close();
    }
    const dashboard=await browser.newPage();
    await dashboard.goto(`http://127.0.0.1:${server.address().port}/`);
    const link=dashboard.locator('#routerUpdatesGroup a');
    assert.equal(await link.getAttribute('target'),'_blank');
    assert.equal(await link.getAttribute('href'),'/router-updates/');
    assert.match(await link.getAttribute('rel'),/noopener/);
    await dashboard.close();
    console.log('Router updates desktop/mobile dashboard, live statuses, refresh, polling, reload, privacy and new-tab link: PASS');
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
