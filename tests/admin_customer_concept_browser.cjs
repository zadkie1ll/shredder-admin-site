/* Customer-card acceptance against the approved cuSummary/cuOverview concept.
 * Run tests/render_admin_fixture.py first. This executes the real renderer with
 * GET-only fixtures: no database, subscription, payment, message or admin action.
 * ADMIN_REFERENCE_HTML overrides the approved local reference when necessary.
 * Screenshots, measured geometry and every mismatch are saved under
 * ADMIN_FIXTURES/customer-concept (default /tmp/mi-admin-review).
 */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const fixtureDir = process.env.ADMIN_FIXTURES || '/tmp/mi-admin-review';
const referenceFile = process.env.ADMIN_REFERENCE_HTML || '/Users/apugachev/.codex/visualizations/2026/09/06/01a078e9-c4b8-7850-bdae-76187677853b/admin-concept-pattern-hover.html';
const outputDir = path.join(fixtureDir, 'customer-concept');
fs.mkdirSync(outputDir, {recursive: true});

// The reference contains only fictional values. Keep the fixture identical so
// wrapping, glyph widths and typography are directly comparable.
const user = {id: 700000001, username: 'demo_client', email: 'client@example.com', telegram_id: 700000001, is_active: true, expire_at: '18.09.2026', days_left: 9};
const history = [299, 299, 249, 599, 249, 249, 10, 8, 299, 249, 299].map((amount, index) => ({
    id: `demo-payment-${11 - index}`, payment_id: `demo-payment-${11 - index}`,
    date: `${index ? '19.08.2026' : '01.09.2026'} 20:39`,
    system: index === 2 ? 'Wata' : 'YooKassa', tariff: `Подписка · ${index === 3 ? '3 месяца' : '1 месяц'}`,
    amount, currency: 'RUB', status: index > 0 && index < 8 ? 'succeeded' : 'canceled', success: index > 0 && index < 8,
}));
const client = {user, history, autopay: {yk: true, wata: false}, ltv: 1663, first_seen: '03.09.2025'};
const traffic = {available: true, used_traffic_bytes: 292 * 1024 ** 3, lifetime_used_traffic_bytes: 292 * 1024 ** 3, traffic_limit_bytes: 0, status: 'ACTIVE', hwid_devices: 4, first_connected: '01.12.2025 08:08'};
const referrals = {user, summary: {referrals: 0, paid_referrals: 0, bonus_days: 0}, referrals: [], referral_block: {blocked: false}, account_block: {blocked: false}};
const scenarios = {
    active: {client, traffic},
    expired: {client: {...client, user: {...user, is_active: false, expire_at: '01.09.2026', days_left: -8}, autopay: {yk: false, wata: false}}, traffic: {...traffic, status: 'EXPIRED'}},
    managed: {client, traffic: {...traffic, traffic_limit_bytes: 10 * 1024 ** 3, managed_limit: {kind: 'managed', marker: true, label: 'Управляемый лимит: пробный период'}}},
    manual: {client, traffic: {...traffic, traffic_limit_bytes: 100 * 1024 ** 3, managed_limit: {kind: 'manual', label: 'Ручной лимит владельца панели'}}},
    limited: {client, traffic: {...traffic, traffic_limit_bytes: 10 * 1024 ** 3, status: 'LIMITED', is_limited: true, managed_limit: {kind: 'managed', marker: true, label: 'Управляемый лимит: пробный период'}}},
    unavailable: {client, traffic: {available: false}},
};
const expectedActions = ['extend', 'set_trial_hour', 'stop_autopay', 'apply_trial_limit', 'remove_traffic_limit', 'temp_ban', 'block_account'];
const expectedTabs = ['overview', 'payments', 'referrals', 'timeline', 'rwmssync', 'message'];
const actionsToConcept = {extend: 'extend', set_trial_hour: 'refund', stop_autopay: 'autopay', apply_trial_limit: 'limit', remove_traffic_limit: 'unlimit', temp_ban: 'tempban', block_account: 'block'};

// This function is serialized into each browser page. All rectangles are
// relative to the summary, never the browser's unrelated shell/header position.
function measureCustomer(reference) {
    const find = selector => document.querySelector(selector);
    const card = find(reference ? '.cu-summary' : '.client-summary-card');
    const origin = card.getBoundingClientRect();
    const properties = ['display', 'gridTemplateColumns', 'columnGap', 'rowGap', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'borderTopWidth', 'borderRadius', 'fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'color', 'backgroundColor', 'textTransform'];
    function snapshot(element) {
        if (!element) return null;
        const rect = element.getBoundingClientRect(), css = getComputedStyle(element);
        return {
            text: element.textContent.replace(/\s+/g, ' ').trim(),
            box: {x: rect.x - origin.x, y: rect.y - origin.y, width: rect.width, height: rect.height},
            style: Object.fromEntries(properties.map(property => [property, css[property]])),
        };
    }
    const identity = card.querySelector(reference ? '.cu-identity' : '.client-summary-identity');
    const copy = identity.querySelector(reference ? ':scope > div' : '.client-summary-identity-copy');
    const facts = [...card.querySelectorAll(reference ? '.cu-fact' : '.client-summary-stat')].map(element => ({
        ...snapshot(element),
        label: snapshot(element.querySelector(reference ? 'small' : '.client-summary-label')),
        value: snapshot(element.querySelector(reference ? 'strong' : '.client-summary-value')),
        note: snapshot(element.querySelector(reference ? 'span' : '.client-summary-meta, .client-status-pill')),
    }));
    const nav = find(reference ? '.cu-tabs' : '.client-subtabs');
    const overview = find(reference ? '#cu-section' : '.client-overview-workspace');
    const columns = find(reference ? '.cu-columns' : '.client-actions-grid');
    return {
        card: snapshot(card), identity: snapshot(identity),
        avatar: snapshot(identity.querySelector(reference ? '.cu-avatar' : '.client-summary-avatar')),
        identityCopy: [...copy.children].map(snapshot), facts,
        nav: snapshot(nav), tabs: [...nav.querySelectorAll('button')].map(snapshot),
        overview: snapshot(overview), columns: snapshot(columns),
        panels: [...columns.querySelectorAll(reference ? '.mi-panel' : '.client-action-panel')].map(snapshot),
        actions: [...overview.querySelectorAll(reference ? '.cu-controls button' : '[data-client-sub-action]')].map(element => ({
            key: reference ? element.dataset.cu : element.dataset.clientSubAction, ...snapshot(element),
        })),
        numbers: [...overview.querySelectorAll('input[type="number"]')].map(snapshot),
        pageOverflow: document.documentElement.scrollWidth > innerWidth + 1,
        cardOrigin: {x: origin.x, y: origin.y},
    };
}

function controlInventory() {
    const card = document.querySelector('#user-payments-result');
    return {
        fullAdmin: document.querySelector('main').dataset.fullAdmin,
        tabs: [...card.querySelectorAll('[data-client-subtab]')].map(element => ({key: element.dataset.clientSubtab, role: element.getAttribute('role')})),
        fields: [...card.querySelectorAll('input[id], textarea[id], select[id]')].map(element => ({id: element.id, type: element.type, min: element.min, step: element.step, value: element.value, label: element.getAttribute('aria-label')})),
        actions: [...card.querySelectorAll('[data-client-sub-action]')].map(element => ({action: element.dataset.clientSubAction, type: element.type, disabled: element.disabled})),
        directMessage: Boolean(card.querySelector('#client-dm-send')),
        referralsLink: Boolean(card.querySelector('[data-client-section-link="referrals"]')),
        tablistRole: card.querySelector('.client-subtabs')?.getAttribute('role'),
        trafficHooks: ['traffic-value', 'traffic-meta', 'traffic-limit-value', 'traffic-limit-meta', 'first-connected-value'].filter(name => card.querySelector(`[data-client-${name}]`)),
    };
}

function lightContrast() {
    const parse = color => {
        const parts = color.match(/[\d.]+/g)?.map(Number) || [];
        return parts.length >= 3 ? [...parts.slice(0, 3), parts.length > 3 ? parts[3] : 1] : [0, 0, 0, 0];
    };
    const over = (front, back) => {
        const alpha = front[3] + back[3] * (1 - front[3]);
        return [0, 1, 2].map(i => (front[i] * front[3] + back[i] * back[3] * (1 - front[3])) / (alpha || 1)).concat(alpha);
    };
    const luminance = color => color.slice(0, 3).map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
    return [...document.querySelectorAll('.client-summary-identity-copy > *, .client-summary-label, .client-summary-value, .client-summary-meta, .client-status-pill, .client-action-copy strong, .client-action-copy > span, [data-client-sub-action], .client-subtabs button')].filter(element => element.getClientRects().length && element.textContent.trim()).map(element => {
        const css = getComputedStyle(element);
        let background = [0, 0, 0, 0];
        for (let node = element; node && background[3] < 1; node = node.parentElement) background = over(background, parse(getComputedStyle(node).backgroundColor));
        background = over(background, [255, 255, 255, 1]);
        const foreground = over(parse(css.color), background);
        const lum = [luminance(foreground), luminance(background)].sort((a, b) => a - b);
        return {text: element.textContent.replace(/\s+/g, ' ').trim().slice(0, 80), color: css.color, background, ratio: (lum[1] + .05) / (lum[0] + .05), minimum: parseFloat(css.fontSize) >= 24 || (parseFloat(css.fontSize) >= 18.66 && Number(css.fontWeight) >= 700) ? 3 : 4.5};
    });
}

(async () => {
    assert.ok(fs.existsSync(referenceFile), `Approved reference must exist: ${referenceFile}`);
    for (const role of ['admin', 'marketer', 'support']) for (const suffix of ['', '-control']) assert.ok(fs.existsSync(path.join(fixtureDir, `${role}${suffix}.html`)), 'Run tests/render_admin_fixture.py before this test');
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
    const errors = [], mutations = [], failures = [], comparisons = [], captures = [], inventories = [], states = [];
    let scenarioName = 'active';
    const allowedExternal = new Set(['cdnjs.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.jsdelivr.net']);
    await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (request.method() !== 'GET') {
            mutations.push(`${request.method()} ${url.pathname}`);
            return route.fulfill({status: 403, json: {status: 'error', message: 'Read-only fixture'}});
        }
        if (url.hostname !== 'admin.test') return allowedExternal.has(url.hostname) ? route.continue() : route.abort();
        if (url.pathname === '/reference.html') return route.fulfill({contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap"><style>body{margin:0}</style></head><body>' + fs.readFileSync(referenceFile, 'utf8') + '</body></html>'});
        if (/^\/(admin|marketer|support)(-control)?\.html$/.test(url.pathname)) return route.fulfill({path: path.join(fixtureDir, url.pathname.slice(1)), contentType: 'text/html'});
        if (url.pathname.startsWith('/static/')) {
            const file = path.join(root, 'engine/static', url.pathname.slice(8));
            return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});
        }
        let result;
        if (url.pathname.includes('api_user_payments/')) result = scenarios[scenarioName].client;
        else if (url.pathname.includes('api_user_traffic/')) result = scenarios[scenarioName].traffic;
        else if (url.pathname.includes('api_referrals/')) result = referrals;
        else if (url.pathname.includes('api_user_timeline/')) result = {items: []};
        else if (url.pathname.includes('api_rwms_sync/')) result = {user, panel: {expire_at: user.expire_at, status: 'ACTIVE'}, diffs: []};
        else if (url.pathname.includes('api_direct_message/')) result = [];
        if (result) return route.fulfill({json: {status: 'ok', result}});
        return route.fulfill({status: 503, json: {status: 'error', message: 'Unrelated section unavailable in isolated fixture'}});
    });
    const page = await context.newPage(), reference = await context.newPage();
    for (const [name, tab] of [['real', page], ['reference', reference]]) tab.on('pageerror', error => errors.push(`${name}: ${error.message}`));
    const check = (label, callback) => {try {callback();} catch (error) {failures.push(`${label}: ${error.message}`);}};
    const near = (label, actual, expected, tolerance = 1.1) => check(label, () => assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} differs from reference ${expected} (±${tolerance}px)`));
    const same = (label, actual, expected) => check(label, () => assert.deepEqual(actual, expected));
    const typeProperties = ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'textTransform'];
    const boxProperties = ['paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'borderTopWidth', 'borderRadius'];
    function compareElement(label, actual, expected, geometry = ['x', 'y', 'width', 'height'], typography = true) {
        if (!actual || !expected) return same(`${label} exists`, Boolean(actual), Boolean(expected));
        for (const key of geometry) near(`${label}.${key}`, actual.box[key], expected.box[key]);
        if (typography) for (const key of typeProperties) {
            // A fallback list cannot change layout when the primary face is
            // loaded. Check the loaded face below and compare that family here.
            const primary = value => value.split(',')[0].trim().replace(/["']/g, '');
            same(`${label}.${key}`, key === 'fontFamily' ? primary(actual.style[key]) : actual.style[key], key === 'fontFamily' ? primary(expected.style[key]) : expected.style[key]);
        }
    }
    async function settle(tab) {await tab.evaluate(() => document.fonts.ready); await tab.waitForTimeout(80);}
    const visibleIcons = locator => locator.evaluateAll(elements => elements.filter(element => {
        const css = getComputedStyle(element);
        return element.getClientRects().length > 0 && css.display !== 'none' && css.visibility !== 'hidden';
    }).length);
    async function openReal(tab, role = 'admin', control = false) {
        await tab.goto(`http://admin.test/${role}${control ? '-control' : ''}.html`);
        await tab.locator('.sidebar [data-tab="user-payments"]').evaluate(element => element.click());
        await tab.locator('#user-payments-form [name="q"]').fill('demo_client');
        await tab.locator('#user-payments-form').evaluate(form => form.requestSubmit());
        await tab.locator('.client-summary-card').waitFor();
        await tab.waitForFunction(() => {
            const value = document.querySelector('[data-client-traffic-value]');
            return value && value.textContent !== 'Загрузка…';
        });
        await tab.evaluate(() => applyAdminTheme('dark'));
        await settle(tab);
    }
    async function capture(tab, selector, name, startAt = null) {
        const file = `${name}.png`;
        // Locator screenshots scroll tall content. Hide only the fixed shell
        // chrome during capture so it cannot cover the component's first row.
        // This is screenshot-only: live UI measurements remain untouched.
        const options = {path: path.join(outputDir, file), animations: 'disabled', style: '.admin-topbar{visibility:hidden!important}'};
        if (startAt) {
            // The reference's explanatory/search strip belongs to its header.
            // Crop both overview artifacts from the actual summary origin.
            const clip = await tab.evaluate(({selector, startAt}) => {
                const end = document.querySelector(selector).getBoundingClientRect();
                const start = document.querySelector(startAt).getBoundingClientRect();
                return {x: start.x + scrollX, y: start.y + scrollY, width: start.width, height: end.bottom - start.top};
            }, {selector, startAt});
            await tab.screenshot({...options, clip, fullPage: true});
        } else await tab.locator(selector).screenshot(options);
        captures.push(file);
    }
    for (const width of [1440, 1100, 390]) {
        const viewport = {width, height: width === 390 ? 844 : 1000};
        await page.setViewportSize(viewport); await reference.setViewportSize(viewport);
        await openReal(page);
        await reference.goto('http://admin.test/reference.html');
        await reference.locator('#mi-admin-picker').evaluate(select => {select.value = 'users'; select.dispatchEvent(new Event('change', {bubbles: true}));});
        await reference.locator('.cu-summary').waitFor(); await settle(reference);
        same(`${width}.real Manrope loaded`, await page.evaluate(() => document.fonts.check('14px Manrope')), true);
        same(`${width}.reference Manrope loaded`, await reference.evaluate(() => document.fonts.check('14px Manrope')), true);
        const actual = await page.evaluate(measureCustomer, false), approved = await reference.evaluate(measureCustomer, true);
        comparisons.push({width, actual, approved});
        same(`${width} no real overflow`, actual.pageOverflow, false);
        same(`${width} no reference overflow`, approved.pageOverflow, false);
        compareElement(`${width}.summary`, actual.card, approved.card, ['width', 'height']);
        for (const property of [...boxProperties, 'display', 'gridTemplateColumns', 'rowGap', 'columnGap', 'color', 'backgroundColor']) same(`${width}.summary.${property}`, actual.card.style[property], approved.card.style[property]);
        compareElement(`${width}.identity`, actual.identity, approved.identity);
        compareElement(`${width}.avatar`, actual.avatar, approved.avatar);
        same(`${width}.avatar text`, actual.avatar.text, approved.avatar.text);
        for (let index = 0; index < approved.identityCopy.length; index++) {
            compareElement(`${width}.identityCopy[${index}]`, actual.identityCopy[index], approved.identityCopy[index]);
            same(`${width}.identityCopy[${index}] text`, actual.identityCopy[index]?.text, approved.identityCopy[index].text);
        }
        same(`${width}.fact count`, actual.facts.length, 7);
        for (let index = 0; index < approved.facts.length; index++) {
            compareElement(`${width}.fact[${index}]`, actual.facts[index], approved.facts[index]);
            for (const element of ['label', 'value', 'note']) {
                compareElement(`${width}.fact[${index}].${element}`, actual.facts[index]?.[element], approved.facts[index][element]);
                // The incumbent status prefix is useful business copy and may
                // stay. It must not change the summary's grid or typography.
                const text = actual.facts[index]?.[element]?.text.replace(/^Статус: /, '');
                same(`${width}.fact[${index}].${element} text`, text, approved.facts[index][element]?.text);
            }
            same(`${width}.fact[${index}].value color`, actual.facts[index]?.value?.style.color, approved.facts[index].value.style.color);
        }
        compareElement(`${width}.tabs`, actual.nav, approved.nav);
        same(`${width}.tab count`, actual.tabs.length, 6);
        for (let index = 0; index < approved.tabs.length; index++) {
            compareElement(`${width}.tab[${index}]`, actual.tabs[index], approved.tabs[index]);
            same(`${width}.tab[${index}] text`, actual.tabs[index]?.text, approved.tabs[index].text);
            for (const property of boxProperties) same(`${width}.tab[${index}].${property}`, actual.tabs[index]?.style[property], approved.tabs[index].style[property]);
        }
        same(`${width}.text-only tabs`, await visibleIcons(page.locator('.client-subtabs i, .client-subtabs svg, .client-subtabs img')), 0);
        // The overview replaced the concept's two columns with the approved
        // action registry (2026-09-09): one card, two groups, six rows, two
        // equal control columns with the primary button always on the right.
        // Row icons are part of that registry, so only the heading stays icon-free.
        same(`${width}.overview heading no decorative icons`, await visibleIcons(page.locator('.client-overview-heading i, .client-overview-heading svg')), 0);
        same(`${width}.panel count`, actual.panels.length, 2);
        const registry = await page.evaluate((width) => {
            const rows = [...document.querySelectorAll('.client-action-registry .client-action-row')];
            const primaries = rows.map(row => row.querySelector('.client-action-button.is-primary')).filter(Boolean);
            const rights = primaries.map(button => Math.round(button.getBoundingClientRect().right));
            const widths = [...document.querySelectorAll('.client-action-registry .client-action-button')].map(button => Math.round(button.getBoundingClientRect().width));
            return {rows: rows.length, columns: rows.map(row => getComputedStyle(row).gridTemplateColumns.split(' ').length), rights: new Set(rights).size, widths: new Set(widths).size, primaries: primaries.length};
        }, width);
        same(`${width}.registry rows`, registry.rows, 6);
        same(`${width}.registry primaries`, registry.primaries, 6);
        same(`${width}.registry row columns`, registry.columns, Array(6).fill(width === 390 ? 2 : 3));
        if (width !== 390) {
            same(`${width}.registry primary buttons aligned`, registry.rights, 1);
            same(`${width}.registry equal button widths`, registry.widths, 1);
        }
        await capture(reference, '.cu-summary', `reference-summary-${width}`);
        await capture(page, '.client-summary-card', `real-summary-${width}`);
        await capture(reference, '.cu-work', `reference-overview-${width}`, '.cu-summary');
        await capture(page, '#user-payments-result > .result-stack', `real-overview-${width}`);
        const inventory = await page.evaluate(controlInventory);
        same(`${width}.action bindings`, inventory.actions.map(action => action.action), expectedActions);
        same(`${width}.tabs preserved`, inventory.tabs.map(tab => tab.key), expectedTabs);
        same(`${width}.tab roles`, inventory.tabs.map(tab => tab.role), expectedTabs.map(() => 'tab'));
        same(`${width}.tablist role`, inventory.tablistRole, 'tablist');
        same(`${width}.traffic hooks`, inventory.trafficHooks.length, 5);
        for (const id of ['client-extend-days', 'client-ban-hours', 'client-dm-text']) check(`${width}.field ${id}`, () => assert.ok(inventory.fields.some(field => field.id === id)));
        same(`${width}.message control`, inventory.directMessage, true);
        same(`${width}.referrals navigation`, inventory.referralsLink, true);
        await page.evaluate(() => applyAdminTheme('light')); await settle(page);
        const contrast = await page.evaluate(lightContrast);
        comparisons[comparisons.length - 1].lightContrast = contrast;
        for (const sample of contrast) check(`${width}.light contrast: ${sample.text}`, () => assert.ok(sample.ratio >= sample.minimum - .02, `${sample.ratio.toFixed(2)}:1 below ${sample.minimum}:1 (${sample.color})`));
        same(`${width}.light no overflow`, await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
        await capture(page, '.client-summary-card', `real-summary-light-${width}`);
    }
    // Guard the existing permissions and action contracts against HEAD's real
    // markup. We inspect controls, but never click a mutating action or send.
    const control = await context.newPage();
    control.on('pageerror', error => errors.push(`control: ${error.message}`));
    await page.setViewportSize({width: 1440, height: 1000});
    for (const role of ['admin', 'marketer', 'support']) {
        await openReal(page, role); await openReal(control, role, true);
        const actual = await page.evaluate(controlInventory), previous = await control.evaluate(controlInventory);
        inventories.push({role, actual, previous});
        const contracts = inventory => ({...inventory, fields: inventory.fields.map(({label, ...field}) => field)});
        same(`${role}.controls and roles unchanged`, contracts(actual), contracts(previous));
        // A new accessible label is compatible with the existing action; any
        // label already present in production must remain intact.
        for (const field of previous.fields.filter(field => field.label)) same(`${role}.${field.id} accessible label preserved`, actual.fields.find(current => current.id === field.id)?.label, field.label);
        if (role !== 'admin') same(`${role}.no management actions`, actual.actions, []);
    }
    // Change GET responses only to exercise real renderer branches. Meaningful
    // expired/limited/managed/manual/unavailable states must survive the new skin.
    for (const name of ['expired', 'managed', 'manual', 'limited', 'unavailable']) {
        scenarioName = name;
        await openReal(page);
        for (const theme of ['dark', 'light']) {
            await page.evaluate(value => applyAdminTheme(value), theme); await settle(page);
            const state = await page.evaluate(() => {
                const card = document.querySelector('.client-summary-card');
                const item = selector => {
                    const element = card.querySelector(selector);
                    return {text: element.textContent.replace(/\s+/g, ' ').trim(), color: getComputedStyle(element).color, classes: element.className};
                };
                const panel = getComputedStyle(document.querySelector('#panel-user-payments'));
                // Resolve each semantic token through the browser so computed
                // rgb output can be compared across both theme palettes.
                const resolveColor = variable => {
                    const sample = document.createElement('span');
                    sample.style.color = panel.getPropertyValue(variable).trim();
                    card.appendChild(sample);
                    const color = getComputedStyle(sample).color;
                    sample.remove(); return color;
                };
                return {
                    subscription: item('.client-summary-subscription'),
                    expiry: item('.client-summary-subscription .client-summary-value'),
                    status: item('.client-status-pill'),
                    autopay: item('.client-summary-stat:last-child .client-summary-value'),
                    traffic: item('[data-client-traffic-value]'),
                    trafficMeta: item('[data-client-traffic-meta]'),
                    limit: item('[data-client-traffic-limit-value]'),
                    limitMeta: item('[data-client-traffic-limit-meta]'),
                    limitClasses: card.querySelector('[data-client-traffic-limit-stat]').className,
                    trafficClasses: card.querySelector('[data-client-traffic-stat]').className,
                    firstConnected: item('[data-client-first-connected-value]'),
                    autopayButtonDisabled: document.querySelector('[data-client-sub-action="stop_autopay"]').disabled,
                    semantic: {success: resolveColor('--workspace-success'), danger: resolveColor('--workspace-danger'), gold: resolveColor('--client-gold'), muted: resolveColor('--admin-text-muted')},
                };
            });
            states.push({name, theme, state});
            if (name === 'expired') {
                same(`${theme}.expired date`, state.expiry.text, '01.09.2026');
                same(`${theme}.expired status`, state.status.text, '● Истекла');
                same(`${theme}.expired color`, state.expiry.color, state.semantic.danger);
                same(`${theme}.expired status color`, state.status.color, state.semantic.danger);
                same(`${theme}.autopay off`, state.autopay.text, 'Отключён');
                check(`${theme}.autopay off is not success`, () => assert.notEqual(state.autopay.color, state.semantic.success));
                same(`${theme}.no active recurring control`, state.autopayButtonDisabled, true);
            }
            if (name === 'managed' || name === 'manual') {
                check(`${theme}.${name} class`, () => assert.ok(state.limitClasses.split(' ').includes(`is-${name}`)));
                check(`${theme}.${name} ownership label`, () => assert.ok(state.limitMeta.text.includes(name === 'managed' ? 'Управляемый лимит' : 'Ручной лимит владельца')));
                same(`${theme}.${name} ownership color`, state.limitMeta.color, state.semantic[name === 'managed' ? 'success' : 'gold']);
                same(`${theme}.${name} limit value`, state.limit.text, name === 'managed' ? '10 ГиБ' : '100 ГиБ');
            }
            if (name === 'limited') {
                check(`${theme}.limited class`, () => assert.ok(state.limitClasses.split(' ').includes('is-limited')));
                check(`${theme}.limited information`, () => assert.ok(state.limitMeta.text.includes('LIMITED — лимит исчерпан')));
                same(`${theme}.limited color`, state.limit.color, state.semantic.danger);
            }
            if (name === 'unavailable') {
                same(`${theme}.unavailable traffic`, state.traffic.text, '—');
                same(`${theme}.unavailable traffic note`, state.trafficMeta.text, 'Нет данных RWMS');
                same(`${theme}.unavailable limit`, state.limit.text, '—');
                same(`${theme}.unavailable first connection`, state.firstConnected.text, '—');
                same(`${theme}.unavailable neutral color`, state.traffic.color, state.semantic.muted);
                check(`${theme}.unavailable class`, () => assert.ok(state.trafficClasses.split(' ').includes('is-unavailable')));
            }
            for (const sample of await page.evaluate(lightContrast)) check(`${name}.${theme} contrast: ${sample.text}`, () => assert.ok(sample.ratio >= sample.minimum - .02, `${sample.ratio.toFixed(2)}:1 below ${sample.minimum}:1`));
        }
    }
    const report = {referenceFile, viewportWidths: [1440, 1100, 390], comparisons, inventories, states, captures, failures, errors, mutations};
    fs.writeFileSync(path.join(outputDir, 'report.json'), JSON.stringify(report, null, 2));
    // A directly comparable pair uses summary-only crops at the same viewport;
    // the longer real business descriptions remain visible in overview captures.
    fs.writeFileSync(path.join(outputDir, 'comparison.html'), '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Customer concept acceptance</title><style>body{margin:24px;background:#101215;color:#edf0f3;font:14px/1.5 system-ui}section{margin-bottom:40px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:20px}figure{margin:0}img{width:100%;display:block}figcaption{padding:10px 0}a{color:#a4cffa}</style><h1>Карточка клиента: концепт и реальный код</h1><p>Одинаковые данные и ширина окна. <a href="report.json">Замеры и расхождения</a>.</p>' + [1440, 1100, 390].map(width => `<section><h2>${width}px</h2><div class="pair"><figure><figcaption>Принятый концепт</figcaption><img src="reference-summary-${width}.png"></figure><figure><figcaption>Реальная карточка</figcaption><img src="real-summary-${width}.png"></figure></div></section>`).join('') + '</html>');
    await browser.close();
    assert.deepEqual(mutations, [], 'No mutation attempted');
    assert.deepEqual(errors, [], 'No uncaught JavaScript errors');
    assert.deepEqual(failures, [], `Customer concept mismatches; see ${path.join(outputDir, 'report.json')}`);
    console.log(`PASS: customer summary geometry/type matches approved concept at 1440/1100/390px; text-only tabs, control sizing, light contrast, three roles and all action bindings preserved; ${captures.length} screenshots; zero mutations.`);
})().catch(error => {console.error(error); process.exit(1);});
