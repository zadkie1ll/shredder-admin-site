/* Local production-template fixtures only; no real measurements or deletions. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const dir=process.env.ADMIN_FIXTURES||'/tmp/mi-admin-review';
const checks=Array.from({length:3},(_,i)=>({id:i+1,target_ip:`192.0.2.${i+1}`,port:443,sni:`node${i+1}.example.test`,is_enabled:true,alerts_enabled:true,api_key_name:'Test key',interval_minutes:0,last_run:null}));
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'chrome'});
 const context=await browser.newContext({reducedMotion:'reduce'});
 let mutations=0;const errors=[];
 await context.route('**/*',async route=>{
  const u=new URL(route.request().url());
  if(u.hostname!=='admin.test'){
   if(/^(cdnjs.cloudflare.com|fonts.googleapis.com|fonts.gstatic.com)$/.test(u.hostname))return route.continue();
   return route.abort();
  }
  if(route.request().method()!=='GET'){mutations++;return route.fulfill({status:403,body:''});}
  if(u.pathname.startsWith('/static/')){
   const file=path.join(root,'engine/static',u.pathname.slice(8));
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  }
  if(u.pathname==='/admin.html')return route.fulfill({path:path.join(dir,'admin.html'),contentType:'text/html'});
  if(u.pathname.includes('api_censor_checks'))return route.fulfill({json:{status:'ok',checks,keys:[],has_default_key:true}});
  return route.fulfill({status:503,json:{status:'error',message:'Isolated fixture'}});
 });
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 for(const width of [320,390,640,641,760,761,1024,1440]){
  await page.setViewportSize({width,height:1000});
  await page.goto('http://admin.test/admin.html');
  await page.locator('[data-tab=infrastructure]').evaluate(e=>e.click());
  await page.locator('[data-censor-select]').first().waitFor({state:'attached'});
  await page.evaluate(()=>document.fonts.ready);
  const all=page.locator('[data-censor-select-all]');
  const items=page.locator('[data-censor-select]');
  const label=page.locator('.censor-check-row.table-head .censor-pick');
  for(const theme of ['dark','light']){
   await page.evaluate(t=>applyAdminTheme(t),theme);
   await label.scrollIntoViewIfNeeded();
   const box=await label.evaluate(e=>{
    const cell=e.getBoundingClientRect();const input=e.querySelector('input').getBoundingClientRect();
    const node=[...e.childNodes].find(n=>n.nodeType===Node.TEXT_NODE&&n.textContent.trim());
    const range=document.createRange();range.selectNodeContents(node);const text=range.getBoundingClientRect();
    return {cell:{left:cell.left,right:cell.right,top:cell.top,bottom:cell.bottom},input:{left:input.left,right:input.right,top:input.top,bottom:input.bottom,width:input.width},text:{left:text.left,right:text.right,top:text.top,bottom:text.bottom},nextLeft:e.nextElementSibling.getBoundingClientRect().left};
   });
   assert.equal(box.input.width,16,`checkbox cannot shrink: ${width}/${theme}`);
   assert(box.text.left>=box.input.right+6,`label separated from checkbox: ${width}/${theme}`);
   assert(box.text.right<=box.cell.right+1,`label contained in selection column: ${width}/${theme}`);
   assert(box.text.top<box.input.bottom&&box.input.top<box.text.bottom,'checkbox and label share one line');
   if(width>760){
    assert.equal(Math.round(box.cell.right-box.cell.left),64);
    assert(box.nextLeft>=box.text.right+8,'IP column stays separate');
    assert(Math.abs((await items.first().boundingBox()).x-box.input.left)<1,'header and row checkboxes align');
   }
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'table scroll remains local');
  }
  await label.click();
  assert.equal(await items.evaluateAll(es=>es.filter(e=>e.checked).length),3);
  assert.equal(await all.isChecked(),true);
  await items.first().uncheck();
  assert.equal(await all.evaluate(e=>e.indeterminate),true);
  assert.match(await page.locator('[data-censor-bulk-count]').textContent(),/2/);
  await label.click();
  assert.equal(await all.isChecked(),true);
  assert.equal(await all.evaluate(e=>e.indeterminate),false);
  await label.click();
  assert.equal(await items.evaluateAll(es=>es.filter(e=>e.checked).length),0);
  assert.equal(await page.locator('[data-censor-bulkbar]').isVisible(),false);
  if(width===390||width===1440){
   await page.evaluate(()=>applyAdminTheme('dark'));
   await label.scrollIntoViewIfNeeded();
   await page.locator('#censor-checks-result').screenshot({path:path.join(dir,`tspu-selection-${width}.png`),animations:'disabled'});
  }
 }
 assert.equal(mutations,0);assert.deepEqual(errors,[]);
 console.log('PASS: TSPU selection layout at 8 widths / 2 themes; select all, partial, clear; zero mutations.');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
