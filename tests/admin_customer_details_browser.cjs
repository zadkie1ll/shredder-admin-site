/* Run render_admin_fixture.py first. All five real client detail views use
 * intercepted GET fixtures; no business service or mutation is contacted.
 * Optional ADMIN_REFERENCE_HTML captures the accepted cuBody beside them.
 */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const fixtureDir = process.env.ADMIN_FIXTURES || '/tmp/mi-admin-review';
const outputDir = path.join(fixtureDir, 'client-details');
fs.mkdirSync(outputDir, {recursive: true});

const user = {id: 700000001, username: 'demo_client', email: 'client@example.test', telegram_id: 700000001, is_active: true, expire_at: '18.09.2026'};
const history = [
    {id: 'fixture-payment-1', date: '01.09.2026 20:51', system: 'YooKassa', tariff: '1 месяц', amount: 299, currency: 'RUB', status: 'canceled', success: false},
    {id: 'fixture-payment-2', date: '19.08.2026 20:39', system: 'Wata', tariff: '3 месяца', amount: 15, currency: 'USD', status: 'succeeded', success: true},
    {id: 'fixture-payment-3', date: '09.09.2026 12:30', system: 'YooKassa', tariff: '1 месяц', amount: 10, currency: 'EUR', status: 'waiting_for_capture', success: false},
];
const client = {user, history, autopay: {yk: true, wata: false}, ltv: 1663, first_seen: '03.09.2025'};
const referrals = {user, summary: {referrals: 0, paid_referrals: 0, bonus_days: 0}, referrals: [], referral_block: {blocked: false}, account_block: {blocked: false}};
const populatedReferrals = {...referrals,
    summary: {referrals: 1, paid_referrals: 1, bonus_days: 7},
    referrals: [{user: {id: 700000002, username: 'invited_client', email: 'invited@example.test'}, paid: true, bonus_days: 7, payments_count: 2, children_count: 1, bonuses: [{created_at: '08.09.2026', type: 'first_payment', days: 7}]}],
    graph: {nodes: [{id: 1, root: true, label: 'demo_client'}, {id: 2, label: 'invited_client'}], edges: [{from: 1, to: 2}]},
};
const timeline = [{ts: '01.09.2026 20:51', category: 'Платежи', title: 'Создан счёт', details: {tariff: 'month', event_type: 'create_invoice', nested: {retry: 2}}}, {ts: '01.09.2026 20:50', category: 'Установка', title: 'install_on_android_clicked', details: {event_type: 'install_on_android_clicked'}}];

(async () => {
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
    const errors = [], mutations = [], captures = [];
    let variant = 'base';
    const external = new Set(['cdnjs.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.jsdelivr.net']);
    await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (request.method() !== 'GET') {
            mutations.push(`${request.method()} ${url.pathname}`);
            return route.fulfill({status: 403, json: {status: 'error', message: 'Read-only fixture'}});
        }
        if (url.hostname !== 'admin.test') return external.has(url.hostname) ? route.continue() : route.abort();
        if (/^\/(admin|marketer|support)\.html$/.test(url.pathname)) return route.fulfill({path: path.join(fixtureDir, url.pathname.slice(1)), contentType: 'text/html'});
        if (url.pathname === '/reference.html' && process.env.ADMIN_REFERENCE_HTML) {
            return route.fulfill({contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet"><style>body{margin:0}</style></head><body>' + fs.readFileSync(process.env.ADMIN_REFERENCE_HTML, 'utf8') + '</body></html>'});
        }
        if (url.pathname.startsWith('/static/')) {
            const file = path.join(root, 'engine/static', url.pathname.slice(8));
            return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});
        }
        let result;
        if (url.pathname.includes('api_user_payments/')) result = client;
        else if (url.pathname.includes('api_user_traffic/')) result = {available: true, used_traffic_bytes: 292 * 1024 ** 3, lifetime_used_traffic_bytes: 292 * 1024 ** 3, traffic_limit_bytes: 0, status: 'ACTIVE', hwid_devices: 4, first_connected: '01.12.2025 08:08'};
        else if (url.pathname.includes('api_referrals/')) result = variant === 'referrals-populated' ? populatedReferrals : variant === 'referrals-blocked' ? {...referrals, referral_block: {blocked: true, reason: 'Повторные активации', updated_at: '09.09.2026 12:00'}} : referrals;
        else if (url.pathname.includes('api_user_timeline/')) result = {items: timeline};
        else if (url.pathname.includes('api_rwms_sync/')) result = {user, panel: variant === 'syncmissing' ? null : {expire_at: variant === 'syncdiff' ? '19.09.2026' : user.expire_at, status: 'ACTIVE'}, diffs: variant === 'syncdiff' ? [{label: 'Дата окончания отличается', db: user.expire_at, panel: '19.09.2026'}] : variant === 'syncmissing' ? [{label: 'Подписки нет в панели', db: user.expire_at, panel: '—'}] : []};
        else if (url.pathname.includes('api_direct_message/')) result = variant === 'message-history' ? [{created_at: '09.09.2026 14:30', created_by: 'admin', text: 'История сообщения: подписка активна.', deliveries: [{bot: 'main', status: 'sent'}, {bot: 'support', status: 'failed'}]}] : [];
        if (result !== undefined) return route.fulfill({json: {status: 'ok', result}});
        return route.fulfill({status: 503, json: {status: 'error', message: 'Изолированная проверка'}});
    });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    async function shot(name, width) {
        await page.evaluate(() => document.fonts.ready);
        await page.waitForTimeout(80);
        await page.evaluate(() => window.scrollTo(0, 0));
        const file = `${name}-${width}.png`;
        await page.screenshot({path: path.join(outputDir, file), fullPage: true, animations: 'disabled'});
        captures.push(file);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `${name}/${width} has no page overflow`);
    }
    async function openClient(role = 'admin') {
        await page.goto(`http://admin.test/${role}.html`);
        await page.locator('.sidebar [data-tab="user-payments"]').evaluate(element => element.click());
        await page.locator('#user-payments-form [name="q"]').fill('demo_client');
        await page.locator('#user-payments-form').evaluate(form => form.requestSubmit());
        await page.locator('.client-summary-card').waitFor();
        await page.waitForFunction(() => document.querySelector('[data-client-traffic-value]')?.textContent.includes('292'));
    }
    async function tab(section) { await page.locator(`[data-client-subtab="${section}"]`).click(); }
    for (const width of [1440, 390]) {
        variant = 'base';
        await page.setViewportSize({width, height: width === 390 ? 844 : 1000});
        await openClient();
        await tab('payments');
        const payments = page.locator('.client-payment-panel');
        for (const value of ['fixture-payment-1', '01.09.2026', '20:51', 'YooKassa', '15 $', '10 €', 'Ожидает']) assert.ok((await payments.textContent()).includes(value), `payment data: ${value}`);
        await page.locator('[data-payment-journal-toggle]').first().click();
        const details = page.locator('#payment-journal-detail-client-0');
        assert.equal(await details.isVisible(), true);
        assert.match(await details.textContent(), /fixture-payment-1.*YooKassa.*canceled/s);
        assert.equal(await details.locator('[data-payment-copy-id="fixture-payment-1"]').count(), 1);
        assert.equal(await payments.locator('.payment-journal-chevron').count() > 0, true);
        assert.equal(await payments.locator('.payment-journal-columns').evaluate(element => getComputedStyle(element).display), width === 390 ? 'none' : 'grid');
        assert.equal(await payments.locator('.payment-journal-row').first().evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length), width === 390 ? 3 : 5);
        await shot('real-user-payments', width);
        await page.locator('[data-payment-journal-toggle]').first().click();
        assert.equal(await details.isVisible(), false);

        await tab('referrals');
        assert.equal(await page.locator('[data-client-referral-block-action="block"]').count(), 1);
        assert.match(await page.locator('.client-referral-empty').textContent(), /Приглашённые пользователи/);
        assert.equal(await page.locator('.client-detail-metrics .metric').first().evaluate(element => getComputedStyle(element).borderTopWidth), '0px');
        assert.equal(await page.locator('.client-referrals-workspace .client-detail-metrics').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').filter(track => parseFloat(track) > 0).length), width === 390 ? 2 : 3);
        assert.equal(await page.locator('.client-ref-control-actions').evaluate(element => getComputedStyle(element).justifyContent), 'flex-start');
        await shot('real-user-referrals', width);

        await tab('timeline');
        await page.locator('.client-timeline-row').first().waitFor();
        assert.deepEqual(await page.locator('.client-timeline-table th').allTextContents(), ['Дата', 'Категория', 'Событие', 'Детали']);
        const cells = await page.locator('.client-timeline-row').first().locator('td').allTextContents();
        assert.deepEqual(cells.slice(0, 3), [timeline[0].ts, timeline[0].category, timeline[0].title]);
        assert.match(cells[3], /tariff: month.*event_type: create_invoice.*nested: \{"retry":2\}/);
        assert.equal((await page.locator('.client-timeline-row').nth(1).locator('td').last().textContent()).trim(), '—');
        await shot('real-user-timeline', width);

        await tab('rwmssync');
        await page.locator('.client-sync-ok').waitFor();
        assert.equal(await page.locator('.client-detail-metric-note').textContent(), 'ACTIVE');
        assert.equal(await page.locator('[data-rwms-sync-action="push_to_panel"]').isDisabled(), false);
        await shot('real-user-rwmssync', width);

        await tab('message');
        await page.locator('.client-dm-empty').waitFor();
        assert.equal(await page.locator('label[for="client-dm-text"]').count(), 1);
        assert.equal(await page.locator('#client-dm-text').getAttribute('rows'), '5');
        await page.locator('#client-dm-text').fill('Только локальный черновик — отправка не вызывается.');
        await shot('real-user-message', width);

        for (const state of ['syncdiff', 'syncmissing', 'referrals-populated', 'referrals-blocked', 'message-history']) {
            variant = state;
            if (state.startsWith('sync')) {
                await tab('rwmssync');
                await page.locator('[data-rwms-sync-action="refresh"]').click();
                await page.locator('.client-sync-diff').waitFor();
                if (state === 'syncdiff') assert.match(await page.locator('.client-sync-diff').textContent(), /18.09.2026.*19.09.2026/s);
                if (state === 'syncmissing') for (const action of ['push_to_panel', 'pull_from_panel']) assert.equal(await page.locator(`[data-rwms-sync-action="${action}"]`).isDisabled(), true);
            } else if (state.startsWith('referrals')) {
                await openClient();
                await tab('referrals');
                if (state === 'referrals-populated') {
                    assert.equal(await page.locator('#referral-graph .referral-map-card').count(), 1);
                    assert.match(await page.locator('.referral-card').textContent(), /invited_client.*first_payment/s);
                } else {
                    assert.match(await page.locator('.client-referral-manage-card').textContent(), /Повторные активации.*09.09.2026 12:00/s);
                    assert.equal(await page.locator('[data-client-referral-block-action="unblock"]').count(), 1);
                    assert.equal(await page.locator('[data-client-referral-block-action="block"]').count(), 0);
                }
            } else {
                await tab('message');
                await page.locator('.client-dm-item').waitFor();
                assert.match(await page.locator('.client-dm-item').textContent(), /admin.*main.*support.*подписка активна/s);
                assert.equal(await page.locator('.client-dm-delivery').count(), 2);
            }
            await shot(`real-user-${state}`, width);
        }
    }
    for (const role of ['marketer', 'support']) {
        variant = 'referrals-populated';
        await openClient(role);
        await tab('referrals');
        assert.equal(await page.locator('[data-client-referral-block-action]').count(), 0, `${role} cannot modify referral permissions`);
        assert.equal(await page.locator('.referral-card').count(), 1);
    }
    if (process.env.ADMIN_REFERENCE_HTML) {
        const reference = await context.newPage();
        reference.on('pageerror', error => errors.push(`reference: ${error.message}`));
        for (const width of [1440, 390]) {
            await reference.setViewportSize({width, height: width === 390 ? 844 : 1000});
            await reference.goto('http://admin.test/reference.html');
            const selectUsers = () => reference.locator('#mi-admin-picker').evaluate(select => {select.value = 'users'; select.dispatchEvent(new Event('change', {bubbles: true}));});
            await selectUsers();
            await reference.evaluate(() => document.fonts.ready);
            for (const section of ['payments', 'referrals', 'timeline', 'sync', 'message']) {
                await reference.locator(`[data-cu="tab:${section}"]`).click();
                // The accepted demo mistakenly navigates to Patterns on tab
                // clicks. Its selected cuTab persists; restore the users page.
                await selectUsers();
                const file = `reference-user-${section}-${width}.png`;
                await reference.screenshot({path: path.join(outputDir, file), fullPage: true, animations: 'disabled'});
                captures.push(file);
            }
        }
    }
    fs.writeFileSync(path.join(outputDir, 'report.json'), JSON.stringify({captures, errors, mutations}, null, 2));
    await browser.close();
    assert.deepEqual(errors, [], 'No uncaught JavaScript errors');
    assert.deepEqual(mutations, [], 'No attempted mutation requests');
    console.log(`PASS: five client views, payment disclosure/data, timeline fields, sync differences/absence, referral graph/permissions and message history; ${captures.length} screenshots; no mutations.`);
})().catch(error => {console.error(error); process.exit(1);});
