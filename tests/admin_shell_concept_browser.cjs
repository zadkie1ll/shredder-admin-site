/* Compare the real shell with the approved HTML. All application requests are
 * local fixtures and every non-GET request is blocked. No service credentials. */
const {chromium}=require('playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),dir=process.env.ADMIN_FIXTURES||'/tmp/mi-admin-review';
const reference=process.env.ADMIN_REFERENCE_HTML;
assert(reference,'Set ADMIN_REFERENCE_HTML to the approved concept');
const out=path.join(dir,'shell-concept');fs.mkdirSync(out,{recursive:true});
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const context=await browser.newContext({reducedMotion:'reduce'}),errors=[],mutations=[];
 await context.route('**/*',async route=>{
  const req=route.request(),u=new URL(req.url());
  if(req.method()!=='GET'){mutations.push(req.url());return route.fulfill({status:403,body:''});}
  if(u.hostname!=='admin.test')return /^(fonts.googleapis.com|fonts.gstatic.com|cdnjs.cloudflare.com)$/.test(u.hostname)?route.continue():route.abort();
  if(u.pathname==='/reference')return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet"><style>body{margin:0}</style></head><body>'+fs.readFileSync(reference,'utf8')+'</body></html>'});
  if(u.pathname==='/admin')return route.fulfill({path:path.join(dir,'admin.html'),contentType:'text/html'});
  if(u.pathname.startsWith('/static/')){const file=path.join(root,'engine/static',u.pathname.slice(8));return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});}
  return route.fulfill({status:503,json:{status:'error',message:'Изолированная проверка оболочки'}});
 });
 const page=await context.newPage(),ref=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 const comparison=[];
 const inspect=(p,selector)=>p.locator(selector).evaluate(e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return {width:r.width,height:r.height,font:s.fontFamily,size:s.fontSize,line:s.lineHeight,weight:s.fontWeight,padding:s.padding,color:s.color,background:s.backgroundColor,border:s.borderWidth,radius:s.borderRadius,gap:s.gap};});
 for(const width of [390,700,701,760,1024,1440]){
  await page.setViewportSize({width,height:1000});await ref.setViewportSize({width,height:1000});
  await page.goto('http://admin.test/admin');await ref.goto('http://admin.test/reference');
  await ref.addScriptTag({path:require.resolve('lucide/dist/umd/lucide.js')});await ref.evaluate(()=>lucide.createIcons());
  await Promise.all([page.evaluate(()=>document.fonts.ready),ref.evaluate(()=>document.fonts.ready)]);
  const hasSidebar=width>700;
  assert.equal(await page.locator('.sidebar').evaluate(e=>e.getBoundingClientRect().right>0),hasSidebar);
  assert.equal(await page.locator('.mobile-topbar').isVisible(),!hasSidebar);
  if(hasSidebar){
   assert.equal((await page.locator('.sidebar').boundingBox()).width,(await ref.locator('aside').boundingBox()).width);
   for(const [real,concept,count] of [['stats','stats',3],['acquisition','acquisition',9],['system','system',10],['infrastructure','infra',5]]){
    await page.locator(`[data-tab="${real}"]`).click();await ref.locator(`[data-group="${concept}"]`).click();
    await page.waitForFunction(id=>document.querySelector('.sidebar .active')?.dataset.tab===id,real);
    // Transitions are disabled only for measurement, never in the application.
    await page.addStyleTag({content:'.sidebar .tab-btn,.sidebar .tab-btn i{transition:none!important}'});
    const actual=await inspect(page,`.sidebar [data-tab="${real}"]`),expected=await inspect(ref,`.mi-nav[data-group="${concept}"]`);
    comparison.push({width,real,actual,expected});
    assert.deepEqual(actual,expected,`${width}/${real}: exact navigation geometry and colors`);
    assert.equal(await page.locator(`[data-tab="${real}"] .admin-nav-count`).textContent(),String(count));
    assert.equal(await page.locator(`[data-tab="${real}"] i`).evaluate(e=>getComputedStyle(e).color),actual.color);
   }
  } else {
   await page.locator('#mobile-burger').click();await page.locator('[data-tab="infrastructure"]').click();
   await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=0);
  }
  for(const section of ['stats','acquisition','system','infrastructure']){
   await page.locator(`[data-tab="${section}"]`).evaluate(e=>e.click());
   const tabs=page.locator(`#panel-${section} > .subtabs > [data-subtab]`);
   for(let i=0;i<await tabs.count();i++){
    await tabs.nth(i).click();
    assert.equal(await page.locator(`#panel-${section} > .panel-header .admin-title`).textContent(),(await tabs.nth(i).textContent()).trim());
   }
  }
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${width}: no page overflow`);
  if(width===1440){await page.locator('[data-tab="acquisition"]').click();await ref.locator('[data-group="acquisition"]').click();await page.locator('.sidebar').screenshot({path:path.join(out,'real-sidebar.png')});await ref.locator('aside').screenshot({path:path.join(out,'reference-sidebar.png')});}
 }
 fs.writeFileSync(path.join(out,'comparison.json'),JSON.stringify(comparison,null,2));
 assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
 await browser.close();console.log('PASS: shell at six widths, exact reference navigation, all section headings, drawer, no overflow or mutations.');
})().catch(e=>{console.error(e);process.exit(1)});
