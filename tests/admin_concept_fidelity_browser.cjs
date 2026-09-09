/* Visual acceptance against the approved concept, using the real template and
 * isolated, read-only API responses. Run render_admin_fixture.py first.
 * ADMIN_REFERENCE_HTML optionally captures the approved gallery beside it.
 * No business service, database, message send or other mutation is contacted. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const fixtureDir = process.env.ADMIN_FIXTURES || '/tmp/mi-admin-review';
const outputDir = path.join(fixtureDir, 'fidelity');
fs.mkdirSync(outputDir, {recursive: true});

const user = {id: 700000001, username: 'demo_client', email: 'client@example.test', telegram_id: 700000001, is_active: true, expire_at: '18.09.2026', days_left: 9};
const history = [
    {id: 'fixture-payment-1', payment_id: 'fixture-payment-1', created_at: '01.09.2026 20:51', system: 'YooKassa', tariff: 'Подписка · 1 месяц', amount: 299, currency: 'RUB', status: 'canceled', success: false},
    {id: 'fixture-payment-2', payment_id: 'fixture-payment-2', created_at: '19.08.2026 20:39', system: 'YooKassa', tariff: 'Подписка · 1 месяц', amount: 299, currency: 'RUB', status: 'succeeded', success: true},
];
const client = {user, history, autopay: {yk: true, wata: false}, ltv: 1663, first_seen: '03.09.2025'};
const traffic = {available: true, used_traffic_bytes: 292 * 1024 ** 3, lifetime_used_traffic_bytes: 292 * 1024 ** 3, traffic_limit_bytes: 0, status: 'ACTIVE', hwid_devices: 4, first_connected: '01.12.2025 08:08'};
const referrals = {user, summary: {referrals: 0, paid_referrals: 0, bonus_days: 0}, referrals: [], referral_block: {blocked: false}, account_block: {blocked: false}};
const promos = [
    {id: 1, code: 'ISLAND7', promo_type: 'days', value: 7, is_active: true, first_purchase_only: false, used_count: 427, max_uses: 0, comment: 'Подарочные дни', valid_until: '11.09.2026', deep_link: 'https://example.test/promo/ISLAND7'},
    {id: 2, code: 'RETURN20', promo_type: 'discount', value: 20, is_active: true, first_purchase_only: false, used_count: 60, max_uses: 100, comment: 'Возврат клиентов', valid_until: '11.09.2026', deep_link: 'https://example.test/promo/RETURN20'},
];
const configTemplates = [
    {id: 1, name: 'DEFAULT', is_active: true, entry_name: 'proxy', enable_dialer_proxy: true, dialer_proxy_name: 'wl-in', profile_update_interval: 1, additional_headers: {'ping-type': 'proxy-head'}, template_json: {log: {loglevel: 'warning'}}, support_url: 'https://example.test/support'},
    {id: 2, name: 'Direct Out', is_active: false, entry_name: 'proxy', enable_dialer_proxy: false, profile_update_interval: 1, additional_headers: {}, template_json: {log: {loglevel: 'warning'}}},
];
const servers = [
    {id: 1, name: 'br-1', country_code: 'BR', hostname: 'br-1.example.test', online: true, rx_bps: 9500000, tx_bps: 13000000, effective_limit_mbps: null, detected_link_speed_mbps: null, utilization_pct: null, tcp_connections: 331, ips: {active: 1}, domains: ['br.example.test'], last_seen_age: 5},
    {id: 2, name: 'ca-1', country_code: 'CA', hostname: 'ca-1.example.test', online: true, rx_bps: 27000000, tx_bps: 19000000, effective_limit_mbps: 300, detected_link_speed_mbps: 1000, utilization_pct: 9, tcp_connections: 832, ips: {active: 2, reserve: 1}, domains: ['ca.example.test'], last_seen_age: 2},
    {id: 3, name: 'de-1', country_code: 'DE', hostname: 'de-1.example.test', online: false, rx_bps: 0, tx_bps: 0, effective_limit_mbps: 1000, utilization_pct: null, tcp_connections: null, ips: {active: 1}, domains: ['de.example.test'], last_seen_age: 900},
];
const checks = ['uk', 'nl', 'de'].map((country, i) => ({id: i + 1, target_ip: `192.0.2.${i + 1}`, port: 443, sni: `${country}.example.test`, is_enabled: true, alerts_enabled: true, api_key_name: 'Test key', interval_minutes: 0, last_run: null}));
const buckets = Array.from({length: 7}, (_, i) => ({label: `${i + 1}.09`, date: `2026-09-0${i + 1}`, revenue: 2500 + i * 1000, payments: 8 + i * 3, unique_paying_users: 6 + i * 2, tariffs: [{name: '1 месяц', count: 8 + i * 3, revenue: 2500 + i * 1000}]}));
const stats = {totals: {subscriptions: 427, connections: 320, unique_paying_users: 90, payments: 104, revenue: 35000, connection_conversion: 74.9, payment_conversion: 21.1, referrals: 25, referral_traffic: 20, referral_purchase: 8}, sources: [], tariffs: [{name: '1 месяц', count: 104}], sales_series: {mode: 'cohort', granularity: 'day', tariff_names: ['1 месяц'], buckets}};
const script = {key: 'install.sh', label: 'install.sh', active_version: 2, has_active: true, active: {version: 2, content: '#!/usr/bin/env bash\nset -euo pipefail\n'}, versions: [{id: 2, version: 2, is_active: true, comment: 'Проверка соединения', created_by: 'admin', created_at: '2026-09-08'}]};
const patterns = {current_hour_msk: 11, typical: {p25: Array.from({length: 24}, (_, i) => i * 1600), p50: Array.from({length: 24}, (_, i) => i * 2100), p75: Array.from({length: 24}, (_, i) => i * 2600)}, today: Array.from({length: 24}, (_, i) => i * 2200), heatmap: Array.from({length: 168}, (_, i) => ({dow: Math.floor(i / 24) + 1, hr: i % 24, rub: 1000 + (i % 24) * 100, payments: 3 + i % 8}))};

(async () => {
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
    const errors = [], mutations = [], failures = [], captures = [];
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
        if (api.includes('api_stats/')) payload = {result: stats};
        else if (api.includes('api_user_payments/')) payload = {result: client};
        else if (api.includes('api_user_traffic/')) payload = {result: traffic};
        else if (api.includes('api_referrals/')) payload = {result: referrals};
        else if (api.includes('api_user_timeline/')) payload = {result: {items: [{ts: '01.09.2026 20:51', category: 'Платежи', title: 'Создан счёт', details: {tariff: 'month'}}, {ts: '01.09.2026 20:50', category: 'Установка', title: 'install_on_android_clicked', details: {}}]}};
        else if (api.includes('api_rwms_sync/')) payload = {result: {user, panel: {expire_at: user.expire_at, status: 'ACTIVE'}, diffs: []}};
        else if (api.includes('api_direct_message/')) payload = {result: []};
        else if (api.includes('api_promocodes/')) payload = {result: {codes: promos, batches: []}};
        else if (api.includes('api_segments/')) payload = {result: [{key: 'active', label: 'Активные платившие', count: 5603}, {key: 'expired', label: 'Истекли за последние 7 дней', count: 3198}]};
        else if (api.includes('api_broadcasts/')) payload = {result: [{id: 1, title: 'Возврат клиентов', text: 'Вам доступна скидка на продление подписки.', created_at: '07.09.2026 14:31', created_by: 'admin', segment_label: 'Неактивные клиенты', status: 'done', progress_pct: 100, sent: 4727, total: 7069, funnel: {claims: 60, buyers: 10, revenue: 2517}, buttons: [{type: 'claim_promo', code: 'RETURN20'}]}]};
        else if (api.includes('api_config_templates/')) payload = {templates: configTemplates};
        else if (api.includes('api_ua_rules/')) payload = {rules: [{id: 1, match_substring: 'Happ', variable_name: 'CLIENT', value: 'happ', priority: 100, is_active: true}]};
        else if (api.includes('api_node_scripts/')) payload = {result: {scripts: [script]}};
        else if (api.includes('api_node_provision/')) payload = {result: {bootstrap_domains_configured: true, rwms_available: true, scripts: [script], scripts_ready: {'install.sh': true}, nodes: [], requests: []}};
        else if (api.includes('api_censor_checks/')) payload = {checks, keys: [{id: 1, name: 'Test key', is_default: true, public_measurements: false}], has_default_key: true};
        else if (api.includes('api_infra_servers/')) payload = {result: {servers, totals: {count: 3, online: 2}, settings: {infra_load_threshold_pct: 85}}};
        else if (api.includes('api_acquisition/') && url.searchParams.get('section') === 'patterns') payload = {result: patterns};
        if (payload) return route.fulfill({json: {status: 'ok', ...payload}});
        return route.fulfill({status: 503, json: {status: 'error', message: 'Изолированная проверка: данные раздела недоступны'}});
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    const check = (label, callback) => { try { callback(); } catch (error) { failures.push(`${label}: ${error.message}`); } };
    async function settle() { await page.evaluate(() => document.fonts.ready); await page.waitForTimeout(90); }
    async function shot(name, width) {
        await settle();
        await page.evaluate(() => window.scrollTo(0, 0));
        const file = `${name}-${width}.png`;
        await page.screenshot({path: path.join(outputDir, file), fullPage: true, animations: 'disabled'});
        captures.push(file);
        const pageOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
        check(`${name}/${width} page overflow`, () => assert.equal(pageOverflow, false));
    }
    async function inspectPatternHour(hour) {
        const canvas = page.locator('#acq-today-chart');
        await canvas.scrollIntoViewIfNeeded();
        const position = await canvas.evaluate((element, selected) => {
            const box = element.getBoundingClientRect(), meta = element.__acqMeta;
            return {x: box.left + meta.padL + (selected + .5) * meta.step, y: box.top + box.height / 2};
        }, hour);
        await page.mouse.move(position.x, position.y);
        await page.locator('.acquisition-chart-tooltip').waitFor({state: 'visible'});
        return page.locator('.acquisition-chart-tooltip').evaluate(element => ({
            heading: element.querySelector('.acq-tooltip-head').textContent,
            rows: [...element.querySelectorAll('.acq-tooltip-row')].map(row => [row.querySelector('.acq-tooltip-name').textContent, row.querySelector('.acq-tooltip-value').textContent.replace(/\s/g, ' ')]),
        }));
    }
    for (const width of [1440, 390]) {
        await page.setViewportSize({width, height: width === 390 ? 844 : 1000});
        await page.goto('http://admin.test/admin.html');
        await page.locator('#stats-result .metrics-grid > .metric').first().waitFor();
        await settle();
        if (width === 1440) {
            const actual = await page.evaluate(() => {
                const css = selector => getComputedStyle(document.querySelector(selector));
                const title = document.querySelector('#panel-stats .admin-title');
                const kpi = css('#stats-result > .metrics-grid > .metric');
                return {sidebar: document.querySelector('.sidebar').getBoundingClientRect().width,
                    contentX: document.querySelector('#panel-stats').getBoundingClientRect().left,
                    titleX: title.getBoundingClientRect().left, font: css('body').fontFamily,
                    bg: css('.page-bg').backgroundColor, surface: css('#stats-form').backgroundColor,
                    titleSize: css('#panel-stats .admin-title').fontSize, titleWeight: css('#panel-stats .admin-title').fontWeight,
                    inputSize: css('#stats-form .input').fontSize, navIcon: css('.sidebar .tab-btn i').fontSize,
                    kpiBackground: kpi.backgroundColor, kpiBorder: kpi.borderTopWidth,
                    kpiShadow: kpi.boxShadow};
            });
            fs.writeFileSync(path.join(outputDir, 'computed-desktop.json'), JSON.stringify(actual, null, 2));
            check('sidebar', () => assert.equal(actual.sidebar, 202));
            check('content origin', () => assert.equal(actual.contentX, 228));
            check('heading has no decorative column', () => assert.equal(actual.titleX, actual.contentX));
            check('body font', () => assert.match(actual.font, /^"?Manrope"?[, ]/));
            check('page color', () => assert.equal(actual.bg, 'rgb(16, 18, 21)'));
            check('approved query surface color', () => assert.equal(actual.surface, 'rgb(21, 24, 29)'));
            check('heading typography', () => assert.deepEqual([actual.titleSize, actual.titleWeight], ['30px', '650']));
            check('input size', () => assert.equal(actual.inputSize, '14px'));
            check('nav icon size', () => assert.equal(actual.navIcon, '17px'));
            check('unboxed summary metrics', () => assert.deepEqual([actual.kpiBackground, actual.kpiBorder, actual.kpiShadow], ['rgba(0, 0, 0, 0)', '0px', 'none']));
        }
        // Every existing top-level/subtab view is captured without hiding controls.
        const tabs = await page.locator('.sidebar .tab-btn').evaluateAll(elements => elements.map(element => element.dataset.tab));
        for (const tab of tabs) {
            await page.locator(`.sidebar [data-tab="${tab}"]`).evaluate(element => element.click());
            const panel = page.locator(`#panel-${tab}`);
            await panel.waitFor({state: 'visible'});
            const subs = await panel.locator(':scope > .subtabs [data-subtab]').evaluateAll(elements => elements.map(element => element.dataset.subtab));
            for (const sub of subs.length ? subs : [null]) {
                if (sub) await panel.locator(`[data-subtab="${sub}"]`).click();
                if (tab === 'stats' && sub === 'overview') await page.locator('#stats-result .metric').first().waitFor();
                if (sub === 'acq-patterns') {
                    await page.locator('#acq-heatmap td[title]').first().waitFor();
                    const observed = await inspectPatternHour(11);
                    check(`patterns current hour/${width}`, () => assert.deepEqual(observed, {heading: 'к 11:59 МСК (накопленная выручка)', rows: [['коридор p25', '17 600 ₽'], ['коридор p75', '28 600 ₽'], ['Медиана', '23 100 ₽'], ['Сегодня', '24 200 ₽']]}));
                    const future = await inspectPatternHour(17);
                    check(`patterns future hour/${width}`, () => assert.deepEqual(future, {heading: 'к 17:59 МСК (накопленная выручка)', rows: [['коридор p25', '27 200 ₽'], ['коридор p75', '44 200 ₽'], ['Медиана', '35 700 ₽']]}));
                    await inspectPatternHour(11);
                }
                if (sub === 'inf-tspu') await page.locator('[data-censor-select]').first().waitFor();
                if (sub === 'inf-configs') await page.locator('[data-config-template-id]').first().waitFor();
                if (sub === 'inf-servers') {
                    await page.locator('.infra-server-card').first().waitFor();
                    assert.equal(await page.locator('.infra-set-limit').count(), 1);
                    assert.equal(await page.locator('.infra-server-card').count(), 3);
                }
                await shot(`real-${tab}${sub ? '-' + sub : ''}`, width);
            }
            if (tab === 'user-payments') {
                await page.locator('#user-payments-form [name="q"]').fill('demo_client');
                await page.locator('#user-payments-form').evaluate(form => form.requestSubmit());
                await page.locator('.client-summary-card').waitFor();
                await page.waitForFunction(() => document.querySelector('[data-client-traffic-value]')?.textContent.includes('292'));
                assert.match(await page.locator('.client-summary-card').textContent(), /Без лимита/);
                for (const section of ['overview', 'payments', 'referrals', 'timeline', 'rwmssync', 'message']) {
                    await page.locator(`[data-client-subtab="${section}"]`).click();
                    if (section === 'timeline') await page.locator('.client-timeline-row').first().waitFor();
                    if (section === 'rwmssync') await page.locator('.client-sync-ok').waitFor();
                    await shot(`real-user-${section}`, width);
                }
            }
        }
    }
    if (process.env.ADMIN_REFERENCE_HTML) {
        const reference = await context.newPage();
        reference.on('pageerror', error => errors.push(`reference: ${error.message}`));
        for (const width of [1440, 390]) {
            await reference.setViewportSize({width, height: width === 390 ? 844 : 1000});
            await reference.goto('http://admin.test/reference.html');
            await reference.locator('#mi-admin-picker option').first().waitFor({state: 'attached'});
            await reference.evaluate(() => document.fonts.ready);
            for (const name of ['overview', 'acq-patterns', 'broadcasts', 'promocodes', 'inf-tspu', 'inf-servers', 'inf-configs', 'users']) {
                await reference.locator('#mi-admin-picker').evaluate((select, value) => {select.value = value; select.dispatchEvent(new Event('change', {bubbles: true}));}, name);
                await reference.waitForTimeout(90);
                const file = `reference-${name}-${width}.png`;
                await reference.screenshot({path: path.join(outputDir, file), fullPage: true, animations: 'disabled'});
                captures.push(file);
            }
        }
    }
    fs.writeFileSync(path.join(outputDir, 'report.json'), JSON.stringify({captures, failures, errors, mutations}, null, 2));
    await browser.close();
    assert.deepEqual(mutations, [], 'No attempted mutations');
    assert.deepEqual(errors, [], 'No uncaught JavaScript errors');
    assert.deepEqual(failures, [], 'Concept fidelity and viewport checks');
    console.log(`PASS: concept shell, type, palette and unboxed KPIs; ${captures.length} screenshots; populated customer, infrastructure, broadcast and promo fixtures; zero mutations.`);
})().catch(error => {console.error(error); process.exit(1);});
