/* Production template, isolated API fixtures. Never contacts business services. */
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const dir=process.env.ADMIN_FIXTURES||'/tmp/mi-admin-review';
const script={key:'install.sh',label:'install.sh',active_version:2,has_active:true,active:{version:2,content:'#!/usr/bin/env bash\nset -euo pipefail\n'},versions:[{id:2,version:2,is_active:true,comment:'Обновлена проверка соединения',created_by:'admin',created_at:'2026-09-08'}]};
const provisioning={bootstrap_domains_configured:true,rwms_available:true,scripts:[script],scripts_ready:{'install.sh':true},nodes:[{uuid:'fixture-node',name:'Германия',has_config_profile:true,is_connected:true}],requests:[]};
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 let mutations=0,checked=0; const errors=[];
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 await context.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.hostname!=='admin.test') {
   if(/^(cdnjs.cloudflare.com|fonts.googleapis.com|fonts.gstatic.com)$/.test(u.hostname)) return route.continue();
   return route.abort();
  }
  if(route.request().method()!=='GET') {mutations++;return route.fulfill({status:403,json:{status:'error',message:'Read-only fixture'}});}
  if(u.pathname.startsWith('/static/')){
   const file=path.join(root,'engine/static',u.pathname.slice(8));
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  }
  const file=path.join(dir,u.pathname.slice(1));
  if(fs.existsSync(file)&&fs.statSync(file).isFile())return route.fulfill({path:file,contentType:'text/html'});
  if(u.pathname.includes('api_node_scripts')) return route.fulfill({json:{status:'ok',result:{scripts:[script]}}});
  if(u.pathname.includes('api_node_provision')) return route.fulfill({json:{status:'ok',result:provisioning}});
  if(u.pathname.includes('api_stats/')&&!u.pathname.includes('source')) return route.fulfill({json:{status:'ok',result:{totals:{subscriptions:427,connections:320,unique_paying_users:90,payments:104,revenue:35000},sources:[],tariffs:[]}}});
  if(u.pathname.includes('api_acquisition')&&u.searchParams.get('section')==='patterns')return route.fulfill({json:{status:'ok',result:{current_hour_msk:11,typical:{p25:Array.from({length:24},(_,i)=>i*1600),p50:Array.from({length:24},(_,i)=>i*2100),p75:Array.from({length:24},(_,i)=>i*2600)},today:Array.from({length:24},(_,i)=>i*2200),heatmap:[]}}});
  return route.fulfill({status:503,json:{status:'error',message:'Тестовое состояние недоступности сервиса'}});
 });
 const page=await context.newPage();page.on('pageerror',e=>{errors.push(e.message);console.error('PAGE ERROR',e.message);});
 async function open(role,control,width){
  await page.setViewportSize({width,height:1000});
  await page.goto(`http://admin.test/${role}${control?'-control':''}.html`);
  await page.waitForFunction(()=>typeof renderNodeScripts==='function');
 }
 async function controls(){return page.evaluate(()=>[...document.querySelectorAll('.main input,.main select,.main textarea,.main button,.main a,.main summary')].filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden').map(e=>[e.tagName,e.id,e.getAttribute('name'),e.getAttribute('type'),e.id==='infra-refresh'?'':(e.textContent||'').replace(/\s+/g,' ').trim()]).sort((a,b)=>JSON.stringify(a).localeCompare(JSON.stringify(b))));}
 // Compare all main/subtab controls in both themes and at mobile/desktop widths.
 for(const role of ['admin','marketer','support'])for(const width of [390,1440]){
  const baseline=new Map();
  for(const control of [true,false]){
   await open(role,control,width);
   const tabs=await page.locator('.sidebar .tab-btn').evaluateAll(es=>es.map(e=>e.dataset.tab));
   assert.equal(tabs.length,role==='admin'?11:role==='marketer'?8:4);
   for(const tab of tabs){
    await page.locator(`.sidebar [data-tab="${tab}"]`).evaluate(e=>e.click());
    const panel=page.locator(`#panel-${tab}`);if(!(await panel.count())){assert.equal(role+':'+tab,'marketer:acquisition','Only the documented pre-existing missing marketer panel is allowed');continue;}await panel.waitFor({state:'visible',timeout:2000}).catch(async e=>{console.error(role,width,control,tab,await page.locator('.tab-panel.active').evaluateAll(es=>es.map(e=>[e.id,e.offsetHeight,getComputedStyle(e).display])));throw e;});
    const subs=await panel.locator(':scope > .subtabs [data-subtab]').evaluateAll(es=>es.map(e=>e.dataset.subtab));
    for(const sub of (subs.length?subs:[null])){
     if(sub)await panel.locator(`[data-subtab="${sub}"]`).click();
     await page.waitForTimeout(30);
     for(const theme of ['dark','light']){
      await page.evaluate(t=>applyAdminTheme(t),theme);
      const key=[tab,sub,theme].join(':');const signature=await controls();
      if(control)baseline.set(key,signature);else {assert.deepEqual(signature,baseline.get(key),`${role}/${width}/${key}: controls preserved`);checked++;}
     }
    }
   }
  }
 }
 await open('admin',false,1440);
 await page.locator('#stats-result .metrics-grid').waitFor();
 await page.evaluate(()=>document.fonts.ready);
 const hoverTab=page.locator('#panel-stats [data-subtab=cohort]');
 await hoverTab.hover();
 await page.waitForFunction(()=>getComputedStyle(document.querySelector('#panel-stats [data-subtab=cohort]'),'::before').backgroundColor==='rgb(29, 34, 41)');
 assert.deepEqual(await hoverTab.evaluate(e=>{const s=getComputedStyle(e);const bg=getComputedStyle(e,'::before');return [s.paddingLeft,s.paddingRight,s.backgroundColor,bg.backgroundColor,bg.borderRadius,bg.left,bg.right,bg.top,bg.bottom];}),['0px','0px','rgba(0, 0, 0, 0)','rgb(29, 34, 41)','8px','-12px','-12px','-6px','4px'],'Tab hover extends past the text without widening its underline');
 await page.screenshot({path:path.join(dir,'analytics-desktop.png'),fullPage:true,animations:'disabled'});
 await page.locator('[data-tab="infrastructure"]').click();
 await page.locator('[data-subtab="inf-provision"]').click();
 await page.locator('#node-provision-type option').first().waitFor({state:'attached'});
 assert.match(await page.locator('#node-provision-type').textContent(),/install.sh · v2/);
 await page.locator('[data-node-provision-view="scripts"]').click();
 await page.locator('#node-provision-script-comment').waitFor({state:'visible'});
 assert.equal(await page.locator('[data-node-script-editor]').isVisible(),true);
 assert.match(await page.locator('.node-provision-version-meta').textContent(),/Обновлена проверка соединения/);
 await page.screenshot({path:path.join(dir,'script-editor-desktop.png'),fullPage:true,animations:'disabled'});
 await page.locator('[data-subtab="inf-configs"]').click();
 await page.evaluate(()=>openConfigTemplateModal());
 assert.equal(await page.locator('#config-template-json').count(),1);
 await page.screenshot({path:path.join(dir,'config-editor-desktop.png'),fullPage:true,animations:'disabled'});
 await page.evaluate(()=>closeConfigTemplateModal());
 await page.locator('[data-tab="broadcasts"]').click();
 await page.screenshot({path:path.join(dir,'broadcast-desktop.png'),fullPage:true,animations:'disabled'});
 await page.setViewportSize({width:390,height:844});
 assert((await page.locator('#broadcast-test-id').boundingBox()).width>=150,'Telegram ID field remains usable');
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Mobile page has no horizontal overflow');
 assert.equal(await page.locator('#broadcast-test-id').evaluate(e=>getComputedStyle(e,'::placeholder').color),'rgb(174, 182, 193)');
 await page.screenshot({path:path.join(dir,'broadcast-mobile.png'),fullPage:true,animations:'disabled'});
 await page.locator('#mobile-burger').click();
 assert.equal(await page.locator('.sidebar [data-tab="promocodes"]').isVisible(),true);
 await page.locator('.sidebar [data-tab="promocodes"]').click();
 assert.equal(await page.locator('#panel-promocodes').isVisible(),true);
 await page.screenshot({path:path.join(dir,'promocodes-mobile.png'),fullPage:true,animations:'disabled'});
 assert.equal(mutations,0,'No mutations during navigation and editor review');
 assert.deepEqual(errors,[],'No uncaught JS errors');
 console.log(`PASS: ${checked} page/theme/role/width comparisons; scripts, versions, config modal, mobile drawer; zero mutations.`);
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
