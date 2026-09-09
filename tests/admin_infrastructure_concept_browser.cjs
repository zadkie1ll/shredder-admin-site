/* Infrastructure acceptance against the approved concept using the real template
 * and isolated, read-only API responses. Run render_admin_fixture.py first.
 * ADMIN_REFERENCE_HTML points to the accepted gallery; ADMIN_ASSERT_CONCEPT=1
 * enables source-to-render typography/grid assertions after the compare pass.
 * No business service, database, message send or other mutation is contacted. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const fixtureDir = process.env.ADMIN_FIXTURES || '/tmp/mi-admin-review';
const outputDir = path.join(fixtureDir, process.env.ADMIN_INFRA_CASES ? 'infrastructure-fidelity-focused' : 'infrastructure-fidelity');
fs.mkdirSync(outputDir, {recursive: true});

const configTemplates = [
    {id: 1, name: 'DEFAULT', is_active: true, entry_name: 'proxy', enable_dialer_proxy: true, dialer_proxy_name: 'wl-in', profile_update_interval: 1, additional_headers: {'ping-type': 'proxy-head'}, template_json: {log: {loglevel: 'warning'}}, support_url: 'https://example.test/support'},
    {id: 2, name: 'Direct Out', is_active: false, entry_name: 'proxy', enable_dialer_proxy: false, profile_update_interval: 1, additional_headers: {}, template_json: {log: {loglevel: 'warning'}}},
];
const servers = [
    {id: 1, name: 'br-1', country_code: 'BR', hostname: 'br-1.example.test', online: true, rx_bps: 9500000, tx_bps: 13000000, effective_limit_mbps: null, detected_link_speed_mbps: null, utilization_pct: null, tcp_connections: 331, ips: {active: 1}, domains: ['br.example.test'], last_seen_age: 5},
    {id: 2, name: 'ca-1', country_code: 'CA', hostname: 'ca-1.example.test', online: true, rx_bps: 27000000, tx_bps: 19000000, effective_limit_mbps: 300, detected_link_speed_mbps: 1000, utilization_pct: 9, tcp_connections: 832, ips: {active: 2, reserve: 1}, domains: ['ca.example.test'], last_seen_age: 2},
    {id: 3, name: 'de-1', country_code: 'DE', hostname: 'de-1.example.test', online: false, rx_bps: 0, tx_bps: 0, effective_limit_mbps: 1000, utilization_pct: null, tcp_connections: null, ips: {active: 1}, domains: ['de.example.test'], last_seen_age: 900},
];
const checks = ['uk', 'nl', 'de'].map((country, i) => ({id: i + 1, target_ip: `192.0.2.${i + 1}`, port: 443, sni: `${country}.example.test`, is_enabled: true, alerts_enabled: true, api_key_name: 'Test key', interval_minutes: 0, last_run: {id:i+1,status:'done',total:30,ok:i===2?29:30,scheduled:33,created_at:'2026-09-09 12:00',completed_at:'2026-09-09 12:05'}}));
const script = {key: 'install.sh', label: 'install.sh', active_version: 2, has_active: true, active: {version: 2, content: '#!/usr/bin/env bash\nset -euo pipefail\n'}, versions: [{id: 2, version: 2, is_active: true, comment: 'Проверка соединения', created_by: 'admin', created_at: '2026-09-08'}, {id:1, version:1, is_active:false, comment:'Первая версия',created_by:'admin',created_at:'2026-09-07'}]};


(async () => {
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
    const errors = [], mutations = [];
    const allowedExternal = new Set(['cdnjs.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.jsdelivr.net']);
    await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (request.method() !== 'GET') {
            mutations.push(`${request.method()} ${url.pathname}`);
            return route.fulfill({status: 403, json: {status: 'error', message: 'Read-only fixture'}});
        }
        if (url.hostname !== 'admin.test') return allowedExternal.has(url.hostname) ? route.continue() : route.abort();
        if (url.pathname === '/reference.html' && process.env.ADMIN_REFERENCE_HTML) {
            const gallery = fs.readFileSync(process.env.ADMIN_REFERENCE_HTML, 'utf8');
            return route.fulfill({contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet"><style>body{margin:0}</style></head><body>' + gallery + '</body></html>'});
        }
        if (url.pathname.startsWith('/static/')) {
            const file = path.join(root, 'engine/static', url.pathname.slice(8));
            return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});
        }
        if (url.pathname === '/admin.html') return route.fulfill({path: path.join(fixtureDir, 'admin.html'), contentType: 'text/html'});
        let payload;
        const api = url.pathname;
        if (api.includes('api_traffic_nodes/')) payload = {result: [{uuid:'node-nl', name:'Нидерланды — Fornex', is_connected:true}]};
        else if (api.includes('api_node_traffic/')) payload = {result: {start:'2026-09-09 03:00', end:'2026-09-09 15:00', period_hours:1, nodes_total:3, total_bytes:4200*1024**3, users_with_traffic:3, failed_nodes:[], excluded_users:[{username:'SYS-REROUTE',total_bytes:582*1024**3}], users:[{username:'700000001',total_bytes:113.45*1024**3,share_percent:2.7,avg_bytes_per_hour:4.64*1024**3,status:'ACTIVE',expire_at:'2026-10-09',hwid_device_limit:4,top_node:'Нидерланды — Fornex',top_node_share_percent:85}]}};
        else if (api.includes('api_config_templates/')) payload = {templates: configTemplates};
        else if (api.includes('api_ua_rules/')) payload = {rules: [{id: 1, match_substring: 'Happ', variable_name: 'CLIENT', value: 'happ', priority: 100, is_active: true}]};
        else if (api.includes('api_node_scripts/')) payload = {result: {scripts: [script]}};
        else if (api.includes('api_node_provision_detail/')) payload = {result:{request:{id:1,node_name:'Нидерланды — Fornex',status:'ready'},stages:[{stage:'claim',status:'ok'},{stage:'script',status:'ok'},{stage:'connect',status:'ok'}],install_log:'Готово'}};
        else if (api.includes('api_node_provision/')) payload = {result: {bootstrap_domains_configured: true, rwms_available: true, scripts: [script], scripts_ready: {'install.sh': true}, nodes: [{uuid:"node-nl",name:"Нидерланды — Fornex",has_config_profile:true,is_connected:true}], requests: [{id:1,node_name:"Нидерланды — Fornex",script_name:"install.sh",script_version:2,status:"ready",created_at:"2026-09-09 12:00",claimed_ip:"192.0.2.1"}]}};
        else if (api.includes('api_censor_checks/')) payload = {checks, keys: [{id: 1, name: 'Test key', is_default: true, public_measurements: false}], has_default_key: true};
        else if (api.includes('api_infra_servers/')) payload = {result: {servers, totals: {count: 3, online: 2}, settings: {infra_load_threshold_pct: 85}}};
        if (payload) return route.fulfill({json: {status: 'ok', ...payload}});
        return route.fulfill({status: 503, json: {status: 'error', message: 'Изолированная проверка: данные раздела недоступны'}});
    });

    const referencePath = process.env.ADMIN_REFERENCE_HTML;
    assert.ok(referencePath, 'ADMIN_REFERENCE_HTML must point to the accepted HTML');
    const page = await context.newPage(), reference = await context.newPage();
    for (const p of [page, reference]) p.on('pageerror', e => errors.push(e.message));
    const samples = [];
    const properties = ['fontSize','fontWeight','lineHeight','padding','margin','borderRadius','borderWidth','backgroundColor','color','gridTemplateColumns','gap','display'];
    async function sample(p,selector){return p.locator(selector).first().evaluate((e,properties)=>{const c=getComputedStyle(e);return Object.fromEntries(properties.map(k=>[k,c[k]]))},properties)}
    const cases = [
      ['inf-tspu', '#censor-check-form', '#ts-form .ix-grid', '.censor-keys-card > summary', '.ts-block > summary'],
      ['inf-provision', '.node-provision-form-grid','#ix-install .ix-grid','.node-provision-section-title h3','.ix-work .mi-panel > h2'],
      ['inf-scripts','.node-provision-code-editor','.ix-code','.node-provision-editor-title','.ix-work .mi-panel > h2'],
      ['inf-traffic','.node-traffic-form','.ix-filters','.node-traffic-section-copy h3','.ix-work .mi-panel > h2'],
      ['inf-configs','.config-template-list','.ix-config-grid','.config-template-title','.ix-config h3'],
      ['inf-servers','.infra-server-grid','.mi-node-grid','.infra-server-card-name','.mi-node h3'],
    ].filter(([id])=>!process.env.ADMIN_INFRA_CASES || process.env.ADMIN_INFRA_CASES.split(',').includes(id));
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1000}); await reference.setViewportSize({width,height:1000});
      await page.goto('http://admin.test/admin.html'); await reference.goto('http://admin.test/reference.html');
      await page.locator('.sidebar [data-tab="infrastructure"]').evaluate(e=>e.click());
      for(const [id,realGrid,refGrid,realType,refType] of cases){
        const sub=id==='inf-scripts'?'inf-provision':id;
        await page.locator(`#panel-infrastructure [data-subtab="${sub}"]`).click();
        await reference.locator('#mi-admin-picker').evaluate((e,id)=>{e.value=id;e.dispatchEvent(new Event('change',{bubbles:true}))},sub);
        if(id==='inf-scripts'){
          await page.locator('[data-node-provision-view="scripts"]').click();
          await reference.locator('[data-ix="tab:scripts"]').click();
          await page.locator('.node-provision-version-history').evaluate(e=>{e.open=true});
        }
        await page.locator(realGrid).first().waitFor({state:'visible'});await reference.locator(refGrid).first().waitFor({state:'visible'});
        await page.evaluate(()=>document.fonts.ready); await reference.evaluate(()=>document.fonts.ready);
        await page.waitForTimeout(100);
        for(const [label,p] of [['actual',page],['reference',reference]]){
          await p.evaluate(()=>window.scrollTo(0,0));
          await p.screenshot({path:path.join(outputDir,`${label}-${id}-${width}.png`),fullPage:true,animations:'disabled'});
          assert.ok(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${label}/${id}/${width} no page overflow`);
        }
        const result={id,width,grid:{actual:await sample(page,realGrid),reference:await sample(reference,refGrid)},type:{actual:await sample(page,realType),reference:await sample(reference,refType)}};
        samples.push(result);
        if(process.env.ADMIN_ASSERT_CONCEPT==='1'){
          assert.equal(result.type.actual.fontSize,result.type.reference.fontSize,`${id}/${width} concept heading size`);
          assert.equal(result.type.actual.fontWeight,result.type.reference.fontWeight,`${id}/${width} concept heading weight`);
          assert.equal(result.grid.actual.gridTemplateColumns.split(' ').length,result.grid.reference.gridTemplateColumns.split(' ').length,`${id}/${width} concept grid column count`);
        }
        if(id==='inf-tspu')assert.equal(await page.locator('#censor-check-form .infra-concept-field').count(),5);
        if(id==='inf-provision'){
          assert.equal(await page.locator('.node-provision-request-table thead th').count(),5);
          assert.equal(await page.locator('.node-provision-request-row td').count(),5);
          const row=await page.locator('.node-provision-request-row').textContent();
          for(const value of ['Нидерланды — Fornex','#1 · 192.0.2.1','install.sh · v2','09.09.2026','Готова','Открыть']) assert.ok(row.includes(value),`Installation preserves ${value}`);
          await page.locator('[data-open-provision-request="1"]').click();
          await page.locator('#node-provision-log').getByText('Готово',{exact:true}).waitFor({state:'visible'});
          assert.equal(await page.locator('#node-provision-stages .node-provision-stage').count(),3);
          await page.locator('#node-provision-detail-close').click();
        }
        if(id==='inf-traffic'){
          assert.equal(await page.locator('#node-traffic-form .infra-concept-field').count(),4);
          const fields=await page.locator('#node-traffic-form').evaluate(f=>[...new FormData(f).keys()]);
          assert.deepEqual(fields,['node','hours','top','min_gib']);
          const metrics=await sample(page,'.node-traffic-summary-grid');
          assert.equal(metrics.gridTemplateColumns.split(' ').length,width>600?3:2,'Traffic summary keeps a compact metric grid');
        }
        if(id==='inf-scripts'){
          await page.locator('.node-provision-version-history').evaluate(e=>{e.open=true});
          assert.equal(await page.locator('[data-activate-script]').count(),1);
        }
        if(id==='inf-tspu'){
          assert.equal(await page.locator('[data-censor-select]').count(),3);
          const all=page.locator('[data-censor-select-all]');
          await all.check(); assert.equal(await page.locator('[data-censor-select]:checked').count(),3);await all.uncheck();
        }
        if(id==='inf-configs'){
          assert.equal(await page.locator('.config-template-facts dt').count(),8);
          assert.equal(await page.locator('[data-config-template-edit]').count(),2);
        }
        if(id==='inf-traffic')assert.equal(await page.locator('.node-traffic-row.table-head > span').count(),9);
        if(id==='inf-servers'){
          assert.equal(await page.locator('.infra-server-card-head .infra-badge-online').first().evaluate(e=>getComputedStyle(e).color),'rgb(114, 218, 173)','Online status remains green');
          assert.equal(await page.locator('.infra-server-card-head .infra-badge-offline').first().evaluate(e=>getComputedStyle(e).color),'rgb(242, 152, 162)','Offline status remains red');
          assert.equal(await page.locator('.infra-set-limit').count(),1);
          assert.equal(await page.locator('.infra-server-control-field').count(),2);
          const initial=await page.locator('.infra-server-card').count();
          await page.locator('#infra-search').fill('ca-1');
          assert.equal(await page.locator('.infra-server-card').count(),1);
          assert.match(await page.locator('.infra-server-card-host').textContent(),/ca-1.example.test/);
          await page.locator('#infra-search').fill('');
          assert.equal(await page.locator('.infra-server-card').count(),initial);
        }
      }
    }
    fs.writeFileSync(path.join(outputDir,'comparison.json'),JSON.stringify(samples,null,2));
    await browser.close();
    assert.deepEqual(errors,[],'No uncaught JavaScript errors');assert.deepEqual(mutations,[],'No business API mutations');
    console.log(`PASS: ${cases.length} infrastructure views at 1440/390; ${samples.length*2} paired captures; all APIs isolated.`);
})().catch(e=>{console.error(e);process.exit(1)});
