/* Run after render_cabinet_fixture.py; all APIs intercepted, no real mutations. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const fixtures = process.env.CABINET_FIXTURES || '/tmp/mi-cabinet-test';
const shots = process.env.CABINET_SCREENSHOTS || '/tmp/mi-cabinet-review';
fs.mkdirSync(shots, {recursive:true});
(async () => {
 const browser = await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'chrome'});
 const context = await browser.newContext({viewport:{width:390,height:844}, reducedMotion:'reduce'});
 let mode='normal', mutations=0;
 const devices = Array.from({length:6}, (_,i)=>({hwid:'fixture-'+i,platform:i?'macOS':'iOS',device_model:i?'MacBook Pro '+i:'iPhone 17 Pro Max',created_at:'2026-09-02T12:00:00Z',updated_at:'2026-09-07T12:00:00Z'}));
 await context.addInitScript(() => {
   window.Telegram = { WebApp:{ready(){},expand(){},disableVerticalSwipes(){},platform:'ios',
     isVersionAtLeast(){return false},BackButton:{show(){},hide(){},onClick(fn){window.testTgBack=fn}}} };
 });
 await context.route('**/*', async route => {
   const url=new URL(route.request().url());
   if (url.hostname!=='cabinet.test') {
     if (/fonts.googleapis.com|fonts.gstatic.com|cdnjs.cloudflare.com/.test(url.hostname)) return route.continue();
     return route.abort();
   }
   if (url.pathname.startsWith('/static/')) {
     const file=path.join(root,'engine/static',url.pathname.slice(8));
     if(fs.existsSync(file)) return route.fulfill({path:file});
     return route.fulfill({status:404,body:''});
   }
   if (url.pathname.startsWith('/api/cabinet/devices')) {
     if(route.request().method()==='POST') { mutations++; assert.equal(url.pathname,'/api/cabinet/devices/delete/'); }
     return route.fulfill({status:mode==='error'?503:200,json: mode==='error'?{status:'error'}:{status:'ok',total:mode==='empty'?0:6,limit:25,devices:mode==='empty'?[]:devices}});
   }
   const file=path.join(fixtures,url.pathname.slice(1)||'active.html');
   if(fs.existsSync(file)) return route.fulfill({path:file,contentType:'text/html'});
   return route.fulfill({json:{status:'ok',payments:[]}});
 });
 const page=await context.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(page.url()+"\n"+e.stack));
 await page.goto('http://cabinet.test/active.html');
 await page.waitForFunction(()=>typeof window.mi3OpenDevices==='function');
 await page.locator('#mi3-devices-count').filter({hasText:'6 из 25'}).waitFor();
 assert.equal(await page.locator('.cabinet-home .mi3-device-row').count(),0);
 assert.equal(await page.locator('.cabinet-home .tg-mini-brand-mark img').evaluate(e=>getComputedStyle(e).maskMode),'luminance');
 assert.match(await page.locator('#app-container').evaluate(e=>getComputedStyle(e).backgroundImage),/radial-gradient/);
 for (const width of [320,360,390,430,768,1440]) {
   await page.setViewportSize({width,height:900});
   await page.screenshot({path:path.join(shots,`home-${width}.png`),fullPage:true});
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`overflow ${width}`);
   if(width<1024) {
     const cta=await page.locator('.cabinet-renew').boundingBox();assert(cta.y+cta.height<800);
   }
 }
 await page.setViewportSize({width:390,height:844});
 const trigger=page.locator('.cabinet-action[aria-controls="mi3-devices-sheet"]');
 await trigger.click();
 const sheet=page.locator('#mi3-devices-sheet');
 await sheet.locator('.mi3-device-row').first().waitFor();
 assert.equal(await sheet.locator('.mi3-device-row').count(),6);
 assert.equal(await page.locator('#app-container').evaluate(e=>e.inert),true);
 await page.waitForFunction(()=>[...document.querySelectorAll('#mi3-devices-list svg use')].every(e=>e.getBBox().width>0));
 await page.screenshot({path:path.join(shots,'devices-390.png'),fullPage:true});
 await sheet.locator('summary').first().click();
 assert.equal(await sheet.locator('.mi3-device-del').first().isVisible(),true);
 await sheet.locator('summary').first().click();
 await page.keyboard.press('Shift+Tab');
 assert(await sheet.evaluate(e=>e.contains(document.activeElement)), 'focus stays within sheet');
 await page.keyboard.press('Escape');await page.waitForTimeout(100);
 assert.equal(await sheet.getAttribute('aria-hidden'),'true');
 assert.equal(await trigger.evaluate(e=>e===document.activeElement),true);
 await trigger.click();
 await sheet.locator('.mi3-sheet-top').evaluate(el => {
   const start = new Touch({identifier:1,target:el,clientY:200});
   const end = new Touch({identifier:1,target:el,clientY:300});
   el.dispatchEvent(new TouchEvent('touchstart',{bubbles:true,touches:[start]}));
   el.dispatchEvent(new TouchEvent('touchend',{bubbles:true,changedTouches:[end]}));
 });
 await page.waitForTimeout(100);
 assert.equal(await sheet.getAttribute('aria-hidden'),'true');
 await trigger.click(); await page.goBack();
 assert.equal(await sheet.getAttribute('aria-hidden'),'true');
 await trigger.click();await sheet.locator('.cabinet-sheet-connect').click();
 assert.equal(await page.locator('.mi3-sheet.is-open').count(),1);
 assert.equal(await page.locator('#mi3-connect-sheet').getAttribute('aria-hidden'),'false');
 await page.keyboard.press('Escape');await page.waitForTimeout(100);
 await page.locator('.cabinet-renew').click();
 assert.equal(await page.locator('#tariff-selection-view').isVisible(),true);
 for(const variant of ['recurrent','expiring','expired','telegram']) {
   await page.goto(`http://cabinet.test/${variant}.html`);
   if(variant==='recurrent') assert.match(await page.locator('.cabinet-renew').textContent(),/Продлить заранее/);
   if(variant==='expiring') assert.match(await page.locator('.cabinet-renew').textContent(),/Продлить подписку/);
   if(variant==='expired') assert.equal(await page.locator('.plans-only-view').isVisible(),true);
   if(variant==='telegram') {
     await page.locator('.cabinet-action[aria-controls]').click();await page.evaluate(()=>window.testTgBack());
     assert.equal(await sheet.getAttribute('aria-hidden'),'true');
   }
 }
 for(const state of ['empty','error']) {
   mode=state;await page.goto('http://cabinet.test/active.html');
   await page.locator('.cabinet-action[aria-controls]').click();
   await page.waitForTimeout(100);
   assert.match(await sheet.textContent(),state==='empty'?/Подключите первое/:/Не удалось загрузить/);
   if(state==='error') {mode='normal';await sheet.locator('.cabinet-retry').click();await sheet.locator('.mi3-device-row').first().waitFor();}
 }
 assert.equal(mutations,0,'opening and closing must never delete devices');
 assert.deepEqual(errors,[]);
 console.log('PASS: 6 widths, main CTA, devices sheet, focus restoration, browser/Telegram back, connection handoff, 5 subscription states, empty/error/retry; zero mutations.');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
