/* Real section navigation, isolated API responses. Never changes business data. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const dir=process.env.ADMIN_FIXTURES||'/tmp/mi-admin-review';
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const context=await browser.newContext({reducedMotion:'reduce'});
 const errors=[],mutations=[];
 await context.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(request.method()!=='GET'){mutations.push(url.pathname);return route.fulfill({status:403,body:''});}
  if(url.hostname!=='admin.test')return /^(cdnjs.cloudflare.com|fonts.googleapis.com|fonts.gstatic.com)$/.test(url.hostname)?route.continue():route.abort();
  if(url.pathname.startsWith('/static/')){
   const file=path.join(root,'engine/static',url.pathname.slice(8));
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  }
  if(url.pathname==='/admin.html')return route.fulfill({path:path.join(dir,'admin.html'),contentType:'text/html'});
  return route.fulfill({status:503,json:{status:'error',message:'Изолированная проверка вкладок'}});
 });
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 let layouts=0;
 for(const width of [320,390,760,1024,1440]){
  await page.setViewportSize({width,height:1000});await page.goto('http://admin.test/admin.html');
  await page.evaluate(()=>document.fonts.ready);
  for(const theme of ['dark','light']){
   await page.evaluate(t=>applyAdminTheme(t),theme);
   for(const [section,count] of [['stats',3],['acquisition',9],['system',10],['infrastructure',5]]){
    await page.locator(`.sidebar [data-tab="${section}"]`).evaluate(e=>e.click());
    const nav=page.locator(`#panel-${section} > .subtabs`),tabs=nav.locator(':scope > [data-subtab]');
    assert.equal(await tabs.count(),count);
    assert.deepEqual(await nav.evaluate(e=>{const s=getComputedStyle(e);return[s.flexWrap,s.gap,s.overflowX,s.backgroundColor];}),['wrap','16px','visible','rgba(0, 0, 0, 0)']);
    for(let i=0;i<count;i++){
     const tab=tabs.nth(i);await tab.click();
     assert.equal(await page.locator(`#subpanel-${await tab.getAttribute('data-subtab')}`).isVisible(),true);
     if(theme==='dark')await page.waitForFunction(selector=>getComputedStyle(document.querySelector(selector)).color==='rgb(255, 207, 50)',`#panel-${section} > .subtabs > .subtab.active`);
     const actual=await tab.evaluate(e=>{
      const s=getComputedStyle(e),box=e.getBoundingClientRect(),range=document.createRange();
      range.selectNodeContents([...e.childNodes].find(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim()));
      return {width:box.width,textWidth:range.getBoundingClientRect().width,height:box.height,
       padding:[s.paddingTop,s.paddingRight,s.paddingBottom,s.paddingLeft],size:s.fontSize,weight:s.fontWeight,
       border:s.borderBottomWidth,color:s.color,background:s.backgroundColor,icon:getComputedStyle(e.querySelector('i')).display};
     });
     assert(Math.abs(actual.width-actual.textWidth)<1,'Underline matches text without icon or side padding');
     assert.equal(actual.height,32);assert.deepEqual(actual.padding,['0px','0px','12px','0px']);
     assert.deepEqual([actual.size,actual.weight,actual.border,actual.icon,actual.background],['12px','600','2px','none','rgba(0, 0, 0, 0)']);
     if(theme==='dark')assert.equal(actual.color,'rgb(255, 207, 50)');
    }
    assert.equal(await tabs.first().evaluate(e=>getComputedStyle(e).fontWeight),'400');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'No page overflow');
    if(width<=760&&section==='acquisition')assert(await tabs.evaluateAll(es=>new Set(es.map(e=>e.getBoundingClientRect().top)).size)>1,'Long navigation wraps');
    if(theme==='dark'&&section==='acquisition'&&[390,760,1024,1440].includes(width)){
     await tabs.filter({hasText:'Когорты'}).click();await page.mouse.move(0,0);
     await nav.screenshot({path:path.join(dir,`section-tabs-${width}.png`),animations:'disabled'});
    }
    layouts++;
   }
  }
 }
 await page.setViewportSize({width:1440,height:1000});
 await page.locator('.sidebar [data-tab="acquisition"]').click();
 const tab=page.locator('[data-subtab="acq-cohorts"]');await tab.focus();await tab.press('Enter');
 assert.equal(await page.locator('#subpanel-acq-cohorts').isVisible(),true);
 assert.deepEqual(await tab.evaluate(e=>{const s=getComputedStyle(e);return[e.matches(':focus-visible'),s.outlineStyle,s.outlineWidth];}),[true,'solid','2px']);
 assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);await browser.close();
 console.log(`PASS: ${layouts} tab layouts; text-width underlines, wrapping, all section switches and keyboard focus; zero mutations.`);
})().catch(e=>{console.error(e);process.exit(1)});
