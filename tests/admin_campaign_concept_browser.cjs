/* Compare real bc/pc/ca workspaces with the pinned approved HTML at equal widths.
 * Render tests/render_admin_fixture.py first. Only local GET fixtures are used;
 * submitting campaigns, creating codes and editing business data are blocked.
 */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const fixtures = process.env.ADMIN_FIXTURES || '/tmp/mi-admin-review';
const output = path.join(fixtures, 'campaign-concept');
const referencePath = process.env.ADMIN_REFERENCE_HTML || '/Users/apugachev/.codex/visualizations/2026/09/06/01a078e9-c4b8-7850-bdae-76187677853b/admin-concept-pattern-hover.html';
fs.mkdirSync(output, {recursive: true});
const codes = [
    {id: 1, code: 'DEMO50', promo_type: 'discount', value: 50, used_count: 0, max_uses: 0, first_purchase_only: true, valid_until: '2026-09-11', is_active: true},
    {id: 2, code: 'DEMO20', promo_type: 'discount', value: 20, used_count: 60, max_uses: 0, first_purchase_only: false, valid_until: '2026-09-11', comment: 'Ушедшим платившим', is_active: true},
    {id: 3, code: 'DEMO3', promo_type: 'days', value: 3, used_count: 427, max_uses: 0, first_purchase_only: true, comment: 'Возврат пользователей', is_active: true},
    {id: 4, code: 'DEMO30', promo_type: 'days', value: 30, used_count: 1, max_uses: 100, first_purchase_only: false, comment: 'Подарок', is_active: true},
];
const segments = ['Все пользователи', 'Активная подписка', 'Активный триал (не платили)', 'Активные платившие', 'Истекают в ближайшие 3 дня', 'Истекли за последние 7 дней', 'Истекли за последние 30 дней', 'Неактивны >30 дней, платили', 'Неактивны >30 дней, не платили, но пользовались', 'Ни разу не подключались (0 трафика)', 'Подключились, но меньше 100 МБ', 'Никогда не платили', 'Платили хотя бы раз', 'С автоплатежом', 'Платили, но без автоплатежа', 'Пришли по реферальной ссылке', 'Прямые (не рефералы)', 'Зарегистрировались за 7 дней'].map((label, index) => ({key: `segment-${index}`, label, count: [240000, 18750, 13147, 5603, 2483, 3198, 11979, 7115, 88252, 37000, 18000, 209168, 30832, 4900, 25932, 34000, 206000, 3200][index]}));
const records = [{id: 1, title: 'Возврат плательщиков №1', segment_label: segments[7].label, status: 'done', sent: 4727, total: 7069, progress_pct: 100, created_at: '07.09.2026', created_by: 'admin', text: 'Вам доступна скидка на продление подписки.', funnel: {claims: 60, buyers: 10, revenue: 2517}, buttons: [{type: 'claim_promo', code: 'DEMO20'}]}];
const cohort = {subject: {label: 'DEMO3', kind: 'promo', promo_type: 'days', value: 3}, cohort: {activations: 427, buyers: 1, repeat_buyers: 0, revenue: 599, payments: 1, active_paid: 1, active_without_purchase: 426, expired_without_purchase: 0, returned_then_churned: 0, blocked: 0, avg_days_to_purchase: '0,8', with_autopay: 0, pending_discounts: 0, revenue_per_activation: 1}, tariffs: [{name: '3 месяца', payments: 1, revenue: 599, share_pct: 100}], users: [{user_id: 700000001, telegram_id: 700000001, activated_at: '04.09.2026 14:10', status: 'active', payments: 1, revenue: 599, first_paid_at: '06.09.2026 12:00', expire_at: '18.09.2026', has_autopay: false}], broadcasts: [], activations_by_day: [], first_payments_by_day: []};

const mappings = {
    broadcasts: [
        ['panel', '.broadcast-compose-card', '.bc-concept > .mi-panel', ['width', 'paddingTop', 'paddingRight', 'borderRadius']],
        ['layout', '.broadcast-composer', '.bc-layout', ['width', 'gridTemplateColumns', 'columnGap']],
        ['heading', '.broadcast-card-heading h2', '.bc-head h2', ['fontSize', 'fontWeight', 'lineHeight']],
        ['editor', '.broadcast-form', '.bc-editor', ['width', 'paddingTop', 'paddingLeft']],
        ['fields', '.broadcast-primary-fields', '.bc-two', ['width', 'gridTemplateColumns', 'columnGap']],
        ['title', '#broadcast-title', '#bc-title', ['width', 'height', 'paddingTop', 'fontSize', 'fontWeight', 'lineHeight', 'borderRadius']],
        ['text', '#broadcast-text', '#bc-text', ['width', 'height', 'paddingTop', 'fontSize', 'lineHeight', 'borderRadius']],
        ['previewPane', '.broadcast-preview-pane', '.bc-preview', ['width', 'paddingTop', 'paddingLeft', 'paddingRight']],
        ['preview', '#broadcast-preview', '.bc-telegram', ['width', 'minHeight', 'paddingTop', 'borderRadius', 'backgroundColor']],
        ['bubble', '#broadcast-preview-text', '#bc-preview-text', ['width', 'minHeight', 'paddingTop', 'fontSize', 'lineHeight']],
        ['bubbleSurface', '.broadcast-preview-bubble', '#bc-preview-text', ['backgroundColor', 'borderRadius']],
        ['history', '.broadcast-history-card', '.bc-concept > .mi-panel:last-child', ['width', 'paddingTop', 'borderRadius']],
        ['record', '.broadcast-record', '.bc-record', ['width', 'paddingTop', 'borderRadius']],
        // История перерисована по концепту B «Воронка» (10.09.2026): мок
        // .bc-stats больше не эталон, сравнивается только наличие воронки
        ['recordMetrics', '.broadcast-funnel', '.bc-stats', []],
    ],
    promocodes: [
        ['panel', '.promo-compose-grid', '.pc-concept > .mi-panel', ['width', 'paddingTop', 'paddingRight', 'borderRadius']],
        ['heading', '.promo-builder-card h2', '.pc-head h2', ['fontSize', 'fontWeight', 'lineHeight']],
        ['form', '.promo-builder-card > .promo-form', '[data-pc-form="single"]', ['width']],
        ['fields', '.promo-builder-card .promo-fields-row', '[data-pc-form="single"] .pc-three', ['width', 'gridTemplateColumns', 'columnGap']],
        ['code', '#promo-code', '[data-pc-form="single"] [data-pc-field="code"]', ['width', 'height', 'paddingTop', 'fontSize', 'lineHeight', 'borderRadius']],
        ['effect', '.promo-builder-card .promo-type-option', '[data-pc-form="single"] .pc-option', ['width', 'paddingTop', 'columnGap', 'borderRadius', 'backgroundColor']],
        ['audience', '.promo-builder-card .promo-audience', '[data-pc-form="single"] .pc-audience', ['width', 'paddingTop', 'gridTemplateColumns', 'columnGap', 'backgroundColor']],
        ['aside', '.promo-live-card', '.pc-layout > aside', ['width', 'paddingTop', 'paddingLeft', 'paddingRight']],
        ['hint', '#promo-live-effect', '#pc-summary-single .pc-hint', ['width', 'paddingTop', 'borderRadius', 'backgroundColor']],
        ['hintTitle', '#promo-live-title', '#pc-summary-single h3', ['fontSize', 'fontWeight', 'lineHeight']],
        ['registry', '.promo-registry-card', '.pc-concept > .mi-panel:last-child', ['width', 'paddingTop', 'borderRadius']],
        ['registryMetrics', '.promo-summary', '.pc-totals', ['width', 'gridTemplateColumns', 'columnGap']],
        ['registryValue', '.promo-summary-item b', '.pc-totals b', ['fontSize', 'fontWeight', 'lineHeight']],
        ['registryRow', '.promo-code-row', '.pc-row', ['width', 'gridTemplateColumns', 'columnGap', 'paddingTop']],
        ['registryAction', '[data-promo-copy="DEMO50"]', '.pc-actions button', ['height', 'paddingTop', 'fontSize', 'fontWeight', 'lineHeight']],
    ],
    'promo-cohorts': [
        ['panel', '#promo-cohort-card', '.ca-concept > .mi-panel:nth-child(2)', ['width', 'paddingTop', 'paddingRight', 'borderRadius']],
        ['heading', '#promo-cohort-title', '.ca-head h2', ['fontSize', 'fontWeight', 'lineHeight']],
        ['filter', '#promo-cohort-select', '#ca-select', ['width', 'height', 'paddingTop', 'fontSize', 'lineHeight', 'borderRadius']],
        ['tabs', '.promo-cohort-view-tabs', '.ca-concept .mi-subnav', ['width', 'columnGap']],
        ['tab', '#promo-cohort-tab-overview', '[data-ca-tab="overview"]', ['height', 'paddingTop', 'paddingBottom', 'fontSize', 'fontWeight', 'backgroundColor']],
        ['metrics', '.promo-cohort-metrics', '.ca-metrics', ['width', 'gridTemplateColumns', 'columnGap']],
        ['metric', '.promo-cohort-metric', '.ca-metrics > div', ['width', 'paddingTop', 'borderRadius', 'backgroundColor']],
        ['metricValue', '.promo-cohort-metric > b', '.ca-metrics b', ['fontSize', 'fontWeight', 'lineHeight', 'color']],
        ['overview', '.promo-cohort-overview-grid', '.ca-overview', ['width', 'gridTemplateColumns', 'columnGap']],
        ['box', '.promo-cohort-panel', '.ca-box', ['width', 'paddingTop', 'borderRadius', 'backgroundColor']],
        ['track', '.promo-cohort-funnel-track', '.ca-track', ['width', 'height', 'backgroundColor']],
        ['funnelValue', '.promo-cohort-funnel-value b', '.ca-funnel b', ['fontSize', 'fontWeight', 'lineHeight']],
    ],
};
function measure({selector, properties}) {
    const element = document.querySelector(selector), css = getComputedStyle(element), box = element.getBoundingClientRect();
    return Object.fromEntries(properties.map(property => [property, property === 'width' || property === 'height' ? box[property] : css[property]]));
}

(async () => {
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
    const failures = [], errors = [], mutations = [], results = [], screenshots = [], retainedMobileInformation = [];
    await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (request.method() !== 'GET') {mutations.push(`${request.method()} ${url.pathname}`); return route.fulfill({status: 403, body: 'Read-only fixture'});}
        if (url.hostname !== 'admin.test') return ['cdnjs.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.jsdelivr.net'].includes(url.hostname) ? route.continue() : route.abort();
        if (url.pathname === '/reference.html') return route.fulfill({contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap"><style>body{margin:0}</style></head><body>' + fs.readFileSync(referencePath, 'utf8') + '</body></html>'});
        if (url.pathname === '/admin.html') return route.fulfill({path: path.join(fixtures, 'admin.html'), contentType: 'text/html'});
        if (url.pathname.startsWith('/static/')) {const file = path.join(root, 'engine/static', url.pathname.slice(8)); return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});}
        const result = url.pathname.includes('api_promocodes/') ? {codes, batches: []} : url.pathname.includes('api_segments/') ? segments : url.pathname.includes('api_broadcasts/') ? records : url.pathname.includes('api_promo_cohort/') ? cohort : null;
        return result ? route.fulfill({json: {status: 'ok', result}}) : route.fulfill({status: 503, json: {status: 'error', message: 'Unrelated local fixture'}});
    });
    const page = await context.newPage(), reference = await context.newPage();
    for (const tab of [page, reference]) tab.on('pageerror', error => errors.push(error.message));
    const check = (label, callback) => {try {callback();} catch (error) {failures.push(`${label}: ${error.message}`);}};
    const settle = async tab => {await tab.evaluate(() => document.fonts.ready); await tab.waitForTimeout(70);};
    async function shot(tab, selector, name) {
        const file = `${name}.png`;
        await tab.locator(selector).screenshot({path: path.join(output, file), animations: 'disabled', style: '.admin-topbar{visibility:hidden!important}'});
        screenshots.push(file);
    }
    for (const width of [1480, 1100, 390]) {
        await page.setViewportSize({width, height: 1000}); await reference.setViewportSize({width, height: 1000});
        await page.goto('http://admin.test/admin.html'); await page.evaluate(() => applyAdminTheme('dark'));
        await reference.goto('http://admin.test/reference.html');
        for (const name of Object.keys(mappings)) {
            if (name === 'promo-cohorts') {
                await page.locator('.sidebar [data-tab="stats"]').evaluate(element => element.click());
                await page.locator('[data-subtab="promo-cohorts"]').click();
                await page.locator('#promo-cohort-select option[value="promo:3"]').waitFor({state: 'attached'});
                await page.locator('#promo-cohort-select').selectOption('promo:3');
                await page.locator('#promo-cohort-form').evaluate(form => form.requestSubmit());
                await page.locator('.promo-cohort-metrics').waitFor();
            } else {
                await page.locator(`.sidebar [data-tab="${name}"]`).evaluate(element => element.click());
                await page.locator(name === 'promocodes' ? '.promo-code-row' : '.broadcast-record').first().waitFor();
            }
            await reference.locator('#mi-admin-picker').evaluate((select, value) => {select.value = value; select.dispatchEvent(new Event('change', {bubbles: true}));}, name);
            await settle(page); await settle(reference);
            for (const [label, selector, refSelector, properties] of mappings[name]) {
                const actual = await page.evaluate(measure, {selector, properties}), expected = await reference.evaluate(measure, {selector: refSelector, properties});
                results.push({width, name, label, actual, expected});
                for (const property of properties) check(`${width}.${name}.${label}.${property}`, () => {
                    // The pinned shell's generic mobile aside rule also hides
                    // campaign previews/help. Keep real information available.
                    if (width < 700 && property === 'width' && expected[property] === 0 && /preview|bubble|aside|hint/i.test(label)) {
                        assert.ok(actual[property] > 0, 'Mobile preview/help remains visible below the form');
                        retainedMobileInformation.push(`${width}.${name}.${label}`); return;
                    }
                    if (typeof expected[property] === 'number') assert.ok(Math.abs(actual[property] - expected[property]) < 1.1, `${actual[property]} != ${expected[property]}`);
                    else assert.equal(actual[property], expected[property]);
                });
            }
            const overflows = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1);
            check(`${width}.${name}.overflow`, () => assert.equal(overflows, false));
            const realSelector = name === 'broadcasts' ? '.broadcast-compose-card' : name === 'promocodes' ? '.promo-compose-grid' : '#promo-cohort-card';
            const refSelector = name === 'broadcasts' ? '.bc-concept > .mi-panel:first-child' : name === 'promocodes' ? '.pc-concept > .mi-panel:first-of-type' : '.ca-concept > .mi-panel:nth-child(2)';
            await shot(page, realSelector, `real-${name}-${width}`); await shot(reference, refSelector, `reference-${name}-${width}`);
            if (name === 'broadcasts') {
                await page.locator('#broadcast-text').fill('Проверка предпросмотра');
                await page.waitForFunction(() => document.querySelector('#broadcast-preview-text').textContent.includes('Проверка предпросмотра'));
                await page.locator('#broadcast-add-button').click();
                const row = page.locator('.broadcast-button-row').last(); await row.waitFor();
                assert.equal(await row.locator('.broadcast-btn-promo').isVisible(), false, 'Link mode hides promo selector');
                assert.equal(await row.locator('.broadcast-btn-tariffs').isVisible(), false, 'Link mode hides tariff selector');
                await shot(page, realSelector, `real-broadcast-button-${width}`);
                await row.locator('.broadcast-btn-type').selectOption('claim_promo');
                assert.equal(await row.locator('.broadcast-btn-promo').isVisible(), true);
                assert.equal(await row.locator('.broadcast-btn-url').isVisible(), false);
                await page.locator('[name="broadcast-promo-recipients"][value="exclude_activated"]').check();
                await row.locator('.broadcast-btn-type').selectOption('tariffs');
                assert.equal(await row.locator('.broadcast-btn-tariffs').isVisible(), true);
                for (const selector of ['.broadcast-btn-promo', '.broadcast-btn-url', '.broadcast-btn-style', '.broadcast-btn-text']) assert.equal(await row.locator(selector).isVisible(), false);
                assert.equal(await page.locator('[name="broadcast-promo-recipients"]').first().isDisabled(), true);
                await row.locator('.broadcast-btn-remove').click();
                await page.locator('#broadcast-text').fill('');
            }
            if (name === 'promocodes') {
                assert.equal(await page.locator('.promo-summary-item').nth(2).locator('b').innerText(), '488');
                const actions = page.locator('.promo-code-row').first().locator('.promo-row-actions');
                assert.deepEqual(await actions.locator('button,a').allTextContents(), ['Код', 'Ссылка', 'Выключить', 'Удалить']);
                assert.equal(await actions.locator('[data-promo-delete="1"]').getAttribute('data-promo-uses'), '0');
                const off = await page.evaluate(code => renderPromoCodes([{...code, is_active: false}]), codes[0]);
                assert.ok(off.includes('aria-label="Включить промокод DEMO50">Включить</button>'));
                await shot(page, '.promo-batch-card', `real-promo-batch-${width}`);
                await shot(page, '.promo-registry-card', `real-promo-registry-${width}`);
                await shot(reference, '.pc-concept > .mi-panel:last-child', `reference-promo-registry-${width}`);
                await page.locator('[data-promo-type-picker="promo"] [data-value="discount"]').click();
                assert.equal(await page.locator('#promo-type').inputValue(), 'discount');
                await page.locator('#promo-first-only').check();
                await page.waitForFunction(() => document.querySelector('#promo-audience-summary').textContent.includes('ещё не было успешных оплат'));
                await shot(page, realSelector, `real-promocodes-discount-${width}`);
                await page.locator('[data-promo-type-picker="promo"] [data-value="days"]').click(); await page.locator('#promo-first-only').uncheck();
            }
            if (name === 'promo-cohorts') {
                const barPercent = await page.locator('.promo-cohort-funnel-bar').nth(1).evaluate(element => parseFloat(element.style.height));
                assert.ok(Math.abs(barPercent - 100 / 427) < 0.0001, 'Funnel reflects the true share, without a 6% floor');
                const empty = await page.evaluate(() => renderPromoCohort({cohort: {activations: 0}}));
                assert.ok(empty.includes('Активаций пока нет') && !/NaN|Infinity/.test(empty));
            }
            await page.evaluate(() => applyAdminTheme('light')); await settle(page);
            await shot(page, realSelector, `real-${name}-light-${width}`);
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
            await page.evaluate(() => applyAdminTheme('dark'));
        }
    }
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({results, failures, errors, mutations, screenshots, retainedMobileInformation}, null, 2));
    const pairs = [1480, 1100, 390].flatMap(width => Object.keys(mappings).map(name => ({width, name})));
    fs.writeFileSync(path.join(output, 'comparison.html'), `<!doctype html><html lang="ru"><meta charset="utf-8"><title>Реальная админка и принятый концепт</title><style>body{margin:0;padding:24px;background:#101215;color:#edf0f3;font:14px/1.5 system-ui}h1{font-size:24px}h2{margin-top:40px;font-size:18px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}.pair img{width:100%;height:auto;display:block}figure{margin:0}figcaption{margin-bottom:10px;color:#aeb6c1}@media(max-width:700px){.pair{grid-template-columns:1fr}}</style><h1>Сравнение при одинаковой внутренней ширине</h1><p>Слева — принятый HTML, справа — текущая админка с локальными данными. Реальные ограничения, пояснения и состояния сохранены. На мобильном экране предпросмотр и справка остаются ниже формы: глобальное скрытие aside в концепте не повторяется.</p>${pairs.map(({width,name}) => `<h2>${name} · ${width}px</h2><div class="pair"><figure><figcaption>Принятый концепт</figcaption><img src="reference-${name}-${width}.png"></figure><figure><figcaption>Реальная админка</figcaption><img src="real-${name}-${width}.png"></figure></div>`).join('')}</html>`);
    await browser.close();
    assert.deepEqual(errors, [], 'No JavaScript errors'); assert.deepEqual(mutations, [], 'No mutations');
    assert.deepEqual(failures, [], `Compare ${path.join(output, 'report.json')}`);
    console.log(`PASS: bc/pc/ca geometry, typography, mobile layout and local form interactions; ${screenshots.length} screenshots; no mutations.`);
})().catch(error => {console.error(error); process.exit(1);});
