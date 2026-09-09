/* Real admin section fidelity, with GET-only isolated fixtures. Run
 * .venv/bin/python tests/render_admin_fixture.py first. Optional
 * ADMIN_REFERENCE_HTML captures the approved gallery at the same viewport.
 * No database, message delivery, payment or subscription service is contacted. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const fixtures = process.env.ADMIN_FIXTURES || '/tmp/mi-admin-review';
const out = path.join(fixtures, 'sections');
fs.mkdirSync(out, {recursive:true});
// Render the separate ticket route without application or database connections.
// Preserve its real attachment/message markup and POST forms in the fixture.
execFileSync(path.join(root, '.venv/bin/python'), ['-c', `
from pathlib import Path
from datetime import datetime, timezone
import re, sys
from django.conf import settings
from django.urls import path
settings.configure(DEBUG=False, SECRET_KEY='fixture-only', STATIC_URL='/static/', ROOT_URLCONF=__name__, INSTALLED_APPS=[], LANGUAGE_CODE='ru', TIME_ZONE='Europe/Moscow')
import django
django.setup()
from django.template import Engine, Context
root=Path(sys.argv[1]); out=Path(sys.argv[2])
source=(root/'engine/templates/support_admin_ticket_detail.html').read_text()
names=set(re.findall(r"url '([^']+)'", source))
urlpatterns=[path(name+('/<int:pk>/' if re.search(r"url '"+name+r"' (?:ticket|attachment)",source) else '/'),lambda request:None,name=name) for name in names]
engine=Engine(dirs=[root/'engine/templates'],libraries={'static':'django.templatetags.static'})
message={'sender_type':'user','created_at':datetime(2026,9,9,12,30,tzinfo=timezone.utc),'message':'Не получается подключить iPhone','attachments':[{'id':1,'file_name':'connection.txt','is_image':False,'is_video':False}]}
for state in ['open','closed']:
 context=Context({'ticket':{'id':142,'status':state,'subject':'Не получается подключить iPhone'},'ticket_user':{'id':700000001,'email':'client@example.test','telegram_id':700000001},'support_status_open':'open','support_sender_user':'user','support_messages':[message,{**message,'sender_type':'support','message':'Проверьте выбранную локацию.','attachments':[]}],'reply_templates':[{'title':'Проверка','body':'Проверьте выбранную локацию.'}],'csrf_token':'fixture-only'})
 (out/('ticket-'+state+'.html')).write_text(engine.from_string(source).render(context))
`, root, out]);
const source = fs.readFileSync(path.join(root, 'engine/templates/admin_dashboard.html'), 'utf8');
const keys = [...source.matchAll(/\{slug: '(?:tariffs|winback|payment|referral|alerts|general)', keys: \[([^\]]+)\]/g)].flatMap(m => [...m[1].matchAll(/'([^']+)'/g)].map(x => x[1]));
const settings = keys.map((key, i) => ({key, type:key.endsWith('_enabled') ? 'bool' : 'int', value:key.endsWith('_enabled') ? '1' : String(i+7), description:`Настройка ${key}: значение сохраняется отдельно.`, is_set:i%2===0, updated_at:'09.09.2026 12:30', sensitive:false}));
const payment = {id:'fixture-payment-2026-09-09', date:'09.09.2026 12:30', user:'client_example', system:'YooKassa', tariff:'1 месяц', amount:299, currency:'RUB', status:'succeeded', success:true};
const payments = [payment, {...payment,id:'fixture-pending',system:'Wata',currency:'USD',amount:4.99,status:'pending',success:false}, {...payment,id:'fixture-cancelled',status:'canceled',success:false}];
const days = Array.from({length:7}, (_,i) => ({day:`2026-09-0${i+1}`, new_rub:1500+i*200, repeat_rub:3000+i*100, new_payers:5+i}));
const buckets = days.map((r,i) => ({label:`${i+1}.09`,date:r.day,revenue:r.new_rub+r.repeat_rub,payments:10+i,unique_paying_users:8+i,tariffs:[{name:'1 месяц',count:10+i,revenue:r.new_rub+r.repeat_rub}]}));
const stats = {totals:{subscriptions:427,connections:320,unique_paying_users:90,payments:104,revenue:35000,connection_conversion:74.9,payment_conversion:21.1,cohort_size:427,referrals:25,referral_traffic:20,referral_purchase:8},sources:[],tariffs:[{name:'1 месяц',count:104}],sales_series:{mode:'cohort',granularity:'day',tariff_names:['1 месяц'],buckets},cohort:{start:'2026-09-01',end:'2026-09-07'},period:{start:'2026-09-01',end:'2026-09-09'}};
const week = {week:'2026-09-01',trials:400,connected:310,mb5:280,mb100:240,invoice_clicks:180,payments:90,new_payers:50,new_rub:15000,spend:9000,subs:400,conns:310,cost_per_sub:22.5,cost_per_conn:29,cost_per_sale:180,drr:60,romi:1.6};
const ladder = {cohort:'2026-08',attracted:400,paid1:90,paid2:40,paid3:20,paid4:10};
const healthSeries = [{period:'2026-09-01',created:100,paid:70,rate:70},{period:'2026-09-08',created:80,paid:56,rate:70}];
const acq = {
 new_repeat:{days},renew45:{months:[{month:'2026-07-01',payers:100,renew_pct:60,mature:true},{month:'2026-08-01',payers:120,renew_pct:40,mature:false}]},
 funnel:{weeks:[week]},trials:{window_days:10,days:days.map(r => ({day:r.day,trials:30,conv_pct:20}))},
 ads:{needs_migration:false,accounts:['default','РСЯ'],spends:[{id:1,day:'2026-09-01',channel:'yandex-direct',account:'default',amount_rub:9000,impressions:4000,clicks:120,comment:'Fixture'}],weeks:[week]},
 ads_daily:{rows:days.map(r => ({period:r.day,spend:9000,impressions:4000,clicks:120,cpc:75,subs:400,conns:310,sales:50,cost_per_sub:22.5,cost_per_conn:29,cost_per_sale:180}))},
 cohorts:{ltv:[{cohort:'2026-06',size:100,ltv_per_user:[299,450,620,750]}],retention:[{cohort:'2026-06',size:100,r30:60,r60:65,r90:70,r180:75,r365:80,r30_matured:true,r60_matured:true,r90_matured:true,r180_matured:false,r365_matured:false}]},
 pushes:{revenue_days:days.map(r=>({day:r.day,rub:r.new_rub})),days:days.map(r=>({day:r.day,selling:100})),winback:[{type:'winback_7d',sent:300,paid_72h:20,conv_pct:6.7,new_payers:5,repeat_payers:15,median_hours:5,rub:6000,amounts:[{amount:299,count:20}]}]},
 patterns:{current_hour_msk:11,typical:{p25:Array.from({length:24},(_,i)=>i*1600),p50:Array.from({length:24},(_,i)=>i*2100),p75:Array.from({length:24},(_,i)=>i*2600)},today:Array.from({length:24},(_,i)=>i*2200),heatmap:Array.from({length:168},(_,i)=>({dow:Math.floor(i/24)+1,hr:i%24,rub:1000+i*10,payments:5}))},
 trial_timing:{trials:400,converted:90,conversion_pct:22.5,buckets:[{label:'0–1 день',users:50,pct:55.6},{label:'2–7 дней',users:40,pct:44.4}]},
 renewal_ladder:{cohorts:[ladder],totals:ladder},tariff_paths:{tariffs:[{tariff:'month',buyers:100,payments:150,rub:44850,adopters:90,first_purchase:60,first_purchase_pct:66.7,from:[{tariff:'threedays',users:30}]}]},
 mrr:{mrr:[{month:'2026-07-01',mrr:180000,covered_users:640},{month:'2026-08-01',mrr:186400,covered_users:642}],live:{mrr_forecast:190000,recurrents_total:642,by_tariff:[{tariff:'month',count:100,mrr:29900}]},dynamics:[{month:'2026-08-01',active_recurrents:642,cancels:20,churn_pct:3.1,autopay_success_users:300,autopay_failures:12}]},
 payment_health:{yookassa:{totals:{created:180,paid:126,rate:70},series:healthSeries},wata:{totals:{created:180,paid:126,rate:70},series:healthSeries},autopay:{totals:{success:90,failure:10,rate:90},series:[{period:'2026-09-01',success:90,failure:10,rate:90}]}}
};
const templates = [{id:1,title:'Подключение на iPhone',body:'Откройте ссылку подписки и добавьте профиль в приложение.',sort_order:100,is_active:true},{id:2,title:'Проверка соединения',body:'Выберите другую доступную локацию.',sort_order:200,is_active:false}];
const tickets = [{id:142,url:'/support_admin_ticket_detail/142/',updated_at_iso:'2026-09-09T12:30:00',updated_at:'09.09.2026 12:30',subject:'Не получается подключить iPhone',user_id:700000001,email:'client@example.test',telegram_id:700000001,is_open:true}];
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const context=await browser.newContext({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
 const errors=[],mutations=[],failures=[],captures=[],requests=[];
 const external=new Set(['cdnjs.cloudflare.com','fonts.googleapis.com','fonts.gstatic.com','cdn.jsdelivr.net']);
 await context.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url()),api=url.pathname;
  if(req.method()!=='GET'){mutations.push(req.method()+' '+api);return route.fulfill({status:403,json:{status:'error',message:'Read-only fixture'}});}
  if(url.hostname!=='admin.test')return external.has(url.hostname)?route.continue():route.abort();
  if(api==='/reference.html'&&process.env.ADMIN_REFERENCE_HTML)return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet"><style>body{margin:0}</style></head><body>'+fs.readFileSync(process.env.ADMIN_REFERENCE_HTML,'utf8')+'</body></html>'});
  if(api==='/admin.html')return route.fulfill({path:path.join(fixtures,'admin.html'),contentType:'text/html'});
  if(api==='/ticket-open.html'||api==='/ticket-closed.html')return route.fulfill({path:path.join(out,api.slice(1)),contentType:'text/html'});
  if(api.startsWith('/static/')){const file=path.join(root,'engine/static',api.slice(8));return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});}
  requests.push(api+'?'+url.searchParams.toString());
  let payload={status:'ok',result:[]};
  if(api.includes('api_stats/')||api.includes('api_cohort_stats/'))payload.result=stats;
  else if(api.includes('api_acquisition/'))payload.result=acq[url.searchParams.get('section')];
  else if(api.includes('api_runtime_settings/'))payload.settings=settings;
  else if(api.includes('api_reply_templates/'))payload.templates=templates;
  else if(api.includes('api_payments/'))payload.payments=payments;
  else if(api.includes('api_audit_log/'))payload.result=[{created_at:'09.09.2026 12:30',actor:'fixture_admin',action:'extend_subscription',target:'700000001',details:{days:7},ip:'192.0.2.1'}];
  else if(api.includes('api_accounts/'))payload.result=[{id:1,login:'fixture_admin',display_name:'Администратор',role:'full',is_active:true,last_login_at:'09.09.2026 12:30'}];
  else if(api.includes('api_rwms_sync/'))payload.result=[{detected_at:'09.09.2026 12:30',username:'client_example',kind:'expire_diff',details:'Разные даты окончания'}];
  else if(api.includes('tickets_json'))payload={open_count:1,closed_count:0,tickets};
  else if(api.includes('ticket_messages_json'))payload={messages:[]};
  else if(api.includes('api_antiabuse/'))payload.result={};
  else if(api.includes('api_referral_antifraud/'))payload.result={enabled:false,limit:10,window_minutes:1};
  return route.fulfill({json:payload});
 });
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 page.on('console',m=>{if(m.type()==='error'&&m.text().includes('acquisition load failed'))errors.push(m.text());});
 const check=(name,fn)=>{try{fn();}catch(e){failures.push(name+': '+e.message);}};
 const computed=async selector=>page.locator(selector).first().evaluate(el=>{const s=getComputedStyle(el),r=el.getBoundingClientRect();return {padding:s.padding,border:s.borderTopWidth,bg:s.backgroundColor,font:s.fontSize,weight:s.fontWeight,columns:s.gridTemplateColumns,x:r.x,y:r.y,w:r.width,display:s.display};});
 async function capture(name,width){await page.evaluate(()=>document.fonts.ready);await page.waitForTimeout(80);await page.evaluate(()=>scrollTo(0,0));const file=name+'-'+width+'.png';await page.screenshot({path:path.join(out,file),fullPage:true,animations:'disabled'});captures.push(file);const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);check(name+'/'+width+' overflow',()=>assert.equal(overflow,false));}
 const groups=process.env.ADMIN_SECTIONS_CONFIRM ? {system:['sys-tariffs'],'payment-info':[null],'reply-templates':[null]} : {stats:['overview','cohort'],acquisition:['acq-newrep','acq-funnel','acq-ads','acq-cohorts','acq-pushes','acq-patterns','acq-journey','acq-mrr','acq-payhealth'],system:['sys-tariffs','sys-winback','sys-payment','sys-referral','sys-alerts','sys-antiabuse','sys-sync','sys-audit','sys-staff','sys-general'],'payment-info':[null],'bulk-actions':[null],'reply-templates':[null],support:[null]};
 for(const width of [1440,390]){
  await page.setViewportSize({width,height:width===390?844:1000});await page.goto('http://admin.test/admin.html');await page.locator('#stats-result .metric').first().waitFor();
  for(const [tab,subs] of Object.entries(groups)){
   await page.locator(`.sidebar [data-tab="${tab}"]`).evaluate(el=>el.click());
   for(const sub of subs){
    if(sub)await page.locator(`#panel-${tab} > .subtabs [data-subtab="${sub}"]`).click();
    await page.waitForTimeout(130);
    if(tab==='support')await page.evaluate(()=>refreshTickets());
    await capture(sub||tab,width);
    if(sub==='sys-tariffs'){
     const row=page.locator('#subpanel-sys-tariffs [data-setting-form]').first();await row.waitFor();
     const css=await computed('#subpanel-sys-tariffs [data-setting-form]');
     check('setting border/'+width,()=>assert.equal(css.border,'0px'));
     const columns=css.columns.split(' ').map(parseFloat);
     check('setting grid/'+width,()=>width===1440?assert.ok(Math.abs(columns[0]/columns[1]-1/.6)<.01):assert.equal(columns.length,1));
     assert.match(await row.locator('.setting-row-origin').innerText(),/БД|env\/default/);
     assert.equal(await row.locator('[value=save]').isDisabled(),true);
     await row.locator('[data-setting-control]').fill('999');assert.equal(await row.locator('[value=save]').isEnabled(),true);
     assert.equal(await row.locator('[data-setting-dirty]').isVisible(),true);
    }
    if(sub==='acq-funnel'){assert.equal(await page.locator('#acq-funnel-table th').count(),13);assert.match(await page.locator('#acq-funnel-table').innerText(),/280/);const css=await computed('#acq-funnel-table td');check('table typography/'+width,()=>assert.deepEqual([css.font,css.padding],['13px','15px 10px']));}
    if(sub==='acq-mrr'||sub==='acq-payhealth'){const css=await computed(sub==='acq-mrr'?'#acq-mrr-cards > div':'#acq-payhealth-cards > div');check('unboxed KPI/'+sub+'/'+width,()=>assert.deepEqual([css.border,css.bg],['0px','rgba(0, 0, 0, 0)']));}
    if(['acq-newrep','acq-ads','acq-payhealth'].includes(sub)){const css=await computed('#subpanel-'+sub+' > .admin-section-query');check('query surface/'+sub+'/'+width,()=>assert.equal(css.bg,'rgb(21, 24, 29)'));}
    if(tab==='payment-info'){
     await page.locator('#payments-list [data-payment-journal-toggle]').first().click();assert.match(await page.locator('#payments-list .payment-journal-details:visible').innerText(),/fixture-payment-2026-09-09/);assert.match(await page.locator('#payments-list').innerText(),/4\.99 \$/);assert.equal(await page.locator('#payments-list [data-payment-journal-toggle]').count(),3);
     await capture('payments-expanded',width);
    }
    if(tab==='bulk-actions'){assert.equal(await page.locator('#bulk-apply').isDisabled(),true);assert.equal(await page.locator('#bulk-action option').count(),7);}
    if(tab==='reply-templates'){
     await page.locator('[data-template-edit="1"]').click();assert.equal(await page.locator('#reply-template-form [name=title]').inputValue(),templates[0].title);assert.equal(await page.locator('#reply-template-form [name=body]').inputValue(),templates[0].body);
     const list=await computed('.admin-template-library'),form=await computed('#reply-template-form');check('template split/'+width,()=>width===1440?assert.ok(list.x<form.x&&Math.abs(list.y-form.y)<1):assert.ok(list.y<form.y));
     assert.equal(await page.locator('[data-template-delete]').count(),2);await capture('templates-edit',width);
    }
    if(tab==='support'){assert.equal(await page.locator('.ticket-link').count(),1);assert.equal(await page.locator('.ticket-link').getAttribute('href'),tickets[0].url);assert.match(await page.locator('.ticket-link').innerText(),/client@example.test/);}
   }
  }
  for(const state of ['open','closed']){
   await page.goto('http://admin.test/ticket-'+state+'.html');
   assert.equal(await page.locator('.msg').count(),2);assert.equal(await page.locator('.attachment').count(),1);
   assert.equal(await page.locator('.ticket-actions form').count(),2);
   assert.match(await page.locator('.ticket-actions').innerText(),state==='open'?/Закрыть тикет/:/Переоткрыть/);
   await page.locator('#quick-replies-toggle').click();await page.locator('.quick-reply-item').click();
   assert.equal(await page.locator('.composer-textarea').inputValue(),'Проверьте выбранную локацию.');
   const userBubble=await computed('.msg-user'),supportBubble=await computed('.msg-support');
   check('ticket bubble surfaces/'+state+'/'+width,()=>assert.deepEqual([userBubble.bg,supportBubble.bg],['rgb(36, 41, 49)','rgb(41, 39, 25)']));
   assert.equal(await page.locator('.composer [name=attachments]').getAttribute('accept'),'image/*,video/*');
   await capture('ticket-'+state,width);
  }
  if(process.env.ADMIN_REFERENCE_HTML){const ref=await context.newPage();await ref.setViewportSize({width,height:width===390?844:1000});await ref.goto('http://admin.test/reference.html');for(const name of ['overview','acq-newrep','acq-patterns','sys-tariffs','payments','bulk','templates','support']){await ref.locator('.mi-mobile-select').evaluate((el,value)=>{el.value=value;el.dispatchEvent(new Event('change',{bubbles:true}));},name);await ref.evaluate(()=>document.fonts.ready);await ref.screenshot({path:path.join(out,`reference-${name}-${width}.png`),fullPage:true,animations:'disabled'});captures.push(`reference-${name}-${width}.png`);}await ref.close();}
 }
 const report={captures,errors,mutations,failures,requests};fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));await browser.close();assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);assert.deepEqual(failures,[]);console.log(`PASS: ${captures.length} screenshots; section data, controls, geometry; no mutations. ${out}/report.json`);
})().catch(e=>{console.error(e);process.exit(1);});
