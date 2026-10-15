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
let job = null, starts = [], polls = 0;
function send(res, data) {res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));}
const server = http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/api/router-updates') {polls++;return send(res,{job,routers:[{id:'main',name:'Советская 30'}]});}
  if(url.pathname==='/api/router-updates/start') {
    assert.equal(req.headers['x-router-update-csrf'],'test-csrf');
    let body='';req.on('data',data=>body+=data);req.on('end',()=>{
      const payload=JSON.parse(body);starts.push(payload);
      job={id:'test-task',request_id:payload.request_id,state:'running',cancel_requested:false,items:[{id:'main',name:'Советская 30',state:'running',message:'Установка выполняется',output:'one\ntwo'}]};
      send(res,{ok:true,job});
    });return;
  }
  if(url.pathname==='/api/router-updates/cancel') {job.cancel_requested=true;return send(res,{job});}
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
      const page=await browser.newPage({viewport:mobile?{width:390,height:844}:{width:1280,height:900},isMobile:mobile,hasTouch:mobile});
      const errors=[];page.on('pageerror',error=>errors.push(error.message));
      const base=`http://127.0.0.1:${server.address().port}`;
      await page.goto(base+'/router-updates/');
      await page.waitForFunction(()=>!document.getElementById('start').disabled);
      assert.equal(await page.locator('#command').inputValue(),'wget -O - "'+defaultRepo+'/install.sh?v=$(date +%s)" | sh');
      const command='wget -O - "https://repo.example/install.sh?v=$(date +%s)" | sh';
      await page.locator('#command').fill(command);
      await page.locator('#sshPassword').fill('test-password');
      await page.locator('#start').click();
      await page.waitForFunction(()=>document.getElementById('status').textContent.includes('Обновление выполняется'));
      assert.equal(starts.at(-1).command,command);assert.equal(starts.at(-1).ssh_password,'test-password');
      assert.equal(await page.locator('#sshPassword').inputValue(),'');
      assert.equal(await page.locator('#start').isDisabled(),true);
      assert.equal(await page.locator('#cancel').isEnabled(),true);
      await page.locator('summary').click();
      job.items[0].output+='\nupdated';
      await page.waitForFunction(()=>document.querySelector('pre').textContent.includes('updated'));
      assert.equal(await page.locator('details').evaluate(node=>node.open),true);
      await page.reload();
      await page.waitForFunction(()=>document.getElementById('cancel').disabled===false);
      const count=starts.length;
      assert.equal(await page.locator('#start').isDisabled(),true);
      if(mobile) {
        await page.locator('#cancel').click();
        await page.waitForFunction(()=>document.getElementById('status').textContent.includes('остальные роутеры будут пропущены'));
        assert.equal(job.cancel_requested,true);
        assert.equal(await page.locator('#cancel').isDisabled(),true);
      }
      job.state='finished';job.items[0].state='success';job.items[0].message='Установка успешно завершена, rc=0';
      await page.waitForFunction(()=>document.getElementById('status').textContent.includes('Очередь завершена'));
      assert.equal(await page.locator('.router strong').textContent(),'Успешно — Советская 30');
      assert.equal(starts.length,count);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      assert.equal(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage}).includes('test-password')),false);
      await page.screenshot({path:path.join(root,mobile?'mobile.png':'desktop.png'),fullPage:true});
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
    console.log('Router updates desktop/mobile UI, polling, reload, privacy and new-tab link: PASS');
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
