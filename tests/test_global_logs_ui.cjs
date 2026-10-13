// Real desktop/mobile browser: literal log text, scrolling, clear and polling.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve('.test-output/global-logs');
let text = Array.from({length: 150}, (_, i) => `Heartbeat ${i}: OK`).join('\n');
let reads = 0, clears = 0;
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.searchParams.get('action') === 'logs_live') {
    reads++;
    res.setHeader('Content-Type', 'text/plain; charset=utf-8');
    return res.end(text);
  }
  if (url.searchParams.get('action') === 'logs_clear') {
    assert.equal(req.method, 'POST');
    clears++;
    text = '';
    res.setHeader('Content-Type', 'text/plain; charset=utf-8');
    return res.end('Логи Remote очищены.');
  }
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end(fs.readFileSync(path.join(root, 'router.html')));
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    for (const mobile of [false, true]) {
      text = Array.from({length: 150}, (_, i) => `Heartbeat ${i}: OK`).join('\n');
      const page = await browser.newPage({viewport: mobile ? {width: 390, height: 844} : {width: 1280, height: 900}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(`http://127.0.0.1:${server.address().port}/cgi-bin/owrt-remote?key=preview-key`);
      assert.equal(await page.locator('#globalLogsPanel').isVisible(), false);
      const vpn = await page.getByRole('link', {name: 'NetHaven VPN'}).boundingBox();
      const toggle = await page.locator('#globalLogsToggle').boundingBox();
      assert(Math.abs(toggle.y - vpn.y) < 3, 'logs button stays alongside VPN');
      assert(Math.abs(toggle.width - vpn.width) < 1, 'same pill width');
      assert.equal(await page.locator('#globalLogsToggle').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(34, 197, 94)');
      if (mobile) {
        const telegram = await page.getByRole('link', {name: 'Telegram', exact: true}).boundingBox();
        const github = await page.getByRole('link', {name: 'GitHub', exact: true}).boundingBox();
        const banner = await page.locator('.appBanner').boundingBox();
        const back = await page.getByRole('link', {name: 'Вернуться в службы OpenWrt'}).boundingBox();
        assert.equal(telegram.y, github.y, 'social links occupy one row');
        assert.equal(telegram.width, toggle.width, 'all paired buttons have equal width');
        assert.equal(telegram.x, vpn.x, 'left column aligns across header');
        assert.equal(github.x, toggle.x, 'right column aligns across header');
        assert.equal(banner.width, back.width, 'title and return span same width');
        assert.equal(telegram.height, toggle.height, 'header buttons have equal height');
        assert(vpn.y >= telegram.y + telegram.height + 8, 'header rows have spacing');
        assert(back.y >= toggle.y + toggle.height + 8, 'return follows paired buttons');
      }
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
      const beforeOpen = reads;
      await page.locator('#globalLogsToggle').click();
      assert.equal(await page.locator('#globalLogsPanel').isVisible(), true);
      await page.waitForFunction(() => document.querySelector('#globalLogsText').textContent.includes('Heartbeat 149'));
      assert.equal(reads, beforeOpen + 1, 'opening displays existing records immediately');
      assert.equal(await page.locator('#globalLogsLive').textContent(), 'Запустить в реальном времени');
      await page.waitForTimeout(3300);
      assert.equal(reads, beforeOpen + 1, 'snapshot does not start automatic polling');
      await page.locator('#globalLogsLive').click();
      await page.waitForFunction(() => document.querySelector('#globalLogsText').textContent.includes('Heartbeat 149'));
      assert.equal(await page.locator('#globalLogsLive').textContent(), 'Остановить просмотр');
      await page.locator('#globalLogsText').evaluate(el => {el.scrollTop = 40;});
      text += '\nnew event\n<script>window.bad=1</script>';
      await page.waitForFunction(() => document.querySelector('#globalLogsText').textContent.includes('new event'));
      assert.equal(await page.locator('#globalLogsText').evaluate(el => el.scrollTop), 40);
      assert.equal(await page.evaluate(() => window.bad), undefined);
      assert.equal(await page.locator('#globalLogsText script').count(), 0);
      await page.screenshot({path: path.join(root, mobile ? 'mobile.png' : 'desktop.png'), fullPage: true});
      const before = clears;
      await page.locator('#globalLogsClear').click();
      await page.waitForFunction(() => document.querySelector('#globalLogsText').textContent.includes('пуст'));
      assert.equal(clears, before + 1);
      await page.locator('#globalLogsClose').click();
      assert.equal(await page.locator('#globalLogsPanel').isVisible(), false);
      const stoppedAt = reads;
      await page.waitForTimeout(3300);
      assert.equal(reads, stoppedAt, 'closing panel stops polling');
      assert.deepEqual(errors, []);
      await page.close();
      console.log('GLOBAL_LOGS_UI_OK', mobile ? 'mobile' : 'desktop');
    }
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => {console.error(error); server.close(); process.exitCode = 1;});
