/* Exercise the real localhost UI against isolated config/SQLite files. */
const { chromium } = require('playwright');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
(async () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'contact-ui-'));
  const config = path.join(temp, 'config.json');
  fs.writeFileSync(config, JSON.stringify({whitelist: [], media: {}}));
  const port = await new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
  const base = `http://127.0.0.1:${port}`;
  const server = spawn(path.join(root, '.venv/bin/python'), ['-m', 'src.core.assistant.web', '--config', config, '--db', path.join(temp, 'state.db'), '--port', String(port)], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
  let output = ''; server.stderr.on('data', d => output += d);
  let browser;
  try {
    let ready = false;
    for (let i = 0; i < 100; i++) { try { if ((await fetch(base+'/api/state')).ok) { ready = true; break; } } catch {} await new Promise(r => setTimeout(r, 50)); }
    assert(ready, output);
    browser = await chromium.launch({executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true});
    const page = await browser.newPage({viewport: {width:1440, height:1000}});
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.goto(base);
    await page.getByText('先添加你的第一位联系人').waitFor();
    for (const [name, conversation, sender] of [['交互验收联系人一','test-conversation-one','test-sender-one'], ['交互验收联系人二','test-conversation-two','test-sender-two']]) {
      await page.getByRole('button', {name:'＋ 添加联系人'}).click();
      await page.locator('#contact-form [name=name]').fill(name);
      await page.locator('#contact-form [name=conversation]').fill(conversation);
      await page.locator('#contact-form [name=sender]').fill(sender);
      await page.locator('#contact-form button[type=submit]').click();
      await page.locator('#add-dialog').waitFor({state:'hidden'});
    }
    assert.equal(JSON.parse(fs.readFileSync(config)).whitelist.length, 0);
    await page.getByRole('checkbox', {name:'允许回复 交互验收联系人一', exact:true}).check();
    await page.getByRole('button', {name:'保存选择', exact:true}).click();
    await page.getByText('已保存选择。').waitFor();
    assert.equal(JSON.parse(fs.readFileSync(config)).whitelist.length,1);
    await page.reload();
    await page.getByRole('checkbox', {name:'允许回复 交互验收联系人一', exact:true}).waitFor();
    assert(await page.getByRole('checkbox', {name:'允许回复 交互验收联系人一',exact:true}).isChecked());
    assert(!(await page.getByRole('checkbox', {name:'允许回复 交互验收联系人二',exact:true}).isChecked()));
    await page.getByRole('textbox', {name:'搜索联系人'}).fill('联系人二');
    assert.equal(await page.locator('.contact-row').count(),1);
    await page.getByRole('checkbox', {name:'选择当前筛选结果',exact:true}).check();
    await page.getByRole('textbox', {name:'搜索联系人'}).fill('');
    assert.equal(await page.locator('.contact-row.selected').count(),2);
    await page.getByRole('button', {name:'重置'}).click();
    await page.getByText('已恢复保存的选择。').waitFor();
    assert.equal(await page.locator('.contact-row.selected').count(),1);
    await page.getByRole('button', {name:'管理 交互验收联系人一'}).click();
    await page.getByText('还没有有效记忆。可以在左侧添加，或通过入站消息明确记住。').waitFor();
    await page.locator('#memory-form [name=key]').fill('回复风格');
    await page.locator('#memory-form [name=value]').fill('简短中文');
    await page.getByRole('button', {name:'保存记忆', exact:true}).click();
    await page.locator('#memory-list').getByText('简短中文', {exact:true}).waitFor();
    await page.locator('#memory-list').getByRole('button', {name:'删除', exact:true}).click();
    await page.getByText('还没有有效记忆。可以在左侧添加，或通过入站消息明确记住。').waitFor();
    await page.getByRole('button', {name:'附件', exact:true}).click();
    await page.getByText('还没有已解析的资料。通过 attachment-add 导入文件后，在这里查看。').waitFor();
    await page.getByRole('button', {name:'联系人', exact:true}).click();
    const out = path.join(root, 'logs/contact-ui-check'); fs.mkdirSync(out, {recursive:true});
    await page.screenshot({path:path.join(out,'desktop.png'), fullPage:true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.setViewportSize({width:390,height:844});
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    await page.waitForTimeout(200);
    await page.screenshot({path:path.join(out,'mobile.png'), fullPage:true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(out,'receipt.json'), JSON.stringify({status:'pass', scenarios:['empty catalog','manual add without auto-allow','checkbox save','reload persistence','search and filtered select-all','discard unsaved selection','memory add/delete','attachment empty state','desktop/mobile no overflow'], viewport:[1440,1000,390,844], browserErrors:errors, realSends:0},null,2));
    console.log('contact UI browser smoke: PASS; actual HTTP + config + SQLite; no browser errors; real sends=0');
  } finally { if (browser) await browser.close(); server.kill('SIGINT'); await new Promise(r => { server.once('exit',r); setTimeout(r,3000); }); fs.rmSync(temp, {recursive:true,force:true}); }
})().catch(error => { console.error(error); process.exitCode = 1; });
