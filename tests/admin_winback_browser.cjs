/* Isolated Win-back editor contract and reference geometry.
 * Render tests/render_admin_fixture.py first. Every API, including writes, is
 * intercepted; no production database, notification or payment service is used.
 */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const fixtures = process.env.ADMIN_FIXTURES || '/tmp/mi-admin-review';
const output = path.join(fixtures, 'winback');
const referencePath = process.env.ADMIN_REFERENCE_HTML || '/Users/apugachev/.codex/visualizations/2026/09/06/01a078e9-c4b8-7850-bdae-76187677853b/admin-concept-pattern-hover.html';
const keys = ['winback_enabled', 'winback_price_month', 'winback_price_threemonths', 'winback_price_year', 'winback_send_hour_start', 'winback_send_hour_end', 'winback_offer_ttl_hours', 'winback_grace_hours'];
const values = ['', '', '449', '1499', '9', '21', '24', '48'];
const baseline = keys.map((key, index) => ({key, type: index ? 'int' : 'bool', value: values[index], display_value: values[index] || 'env/default', is_set: !!values[index], sensitive: false, updated_at: '09.09.2026 12:30', description: `Описание ${key}: учитывается действующая настройка сервиса.`}));
const clone = value => JSON.parse(JSON.stringify(value));
function hold() {
    let requested, release;
    return {ready: new Promise(resolve => {requested = resolve;}), wait: new Promise(resolve => {release = resolve;}), requested: () => requested(), release: () => release()};
}
const id = key => `#winback-${key}`;
const formSelector = '#subpanel-sys-winback [data-winback-form]';
const detailsSelector = '#subpanel-sys-winback [data-winback-details]';
fs.mkdirSync(output, {recursive: true});

function multipart(request) {
    const type = request.headers()['content-type'] || '';
    assert.match(type, /^multipart\/form-data; boundary=/, 'Keep the existing FormData contract');
    const boundary = type.split('boundary=')[1].replace(/^"|"$/g, '');
    const fields = {};
    for (const part of request.postData().split(`--${boundary}`)) {
        const name = part.match(/name="([^"]+)"/);
        if (name) fields[name[1]] = part.slice(part.indexOf('\r\n\r\n') + 4).replace(/\r\n$/, '');
    }
    return fields;
}
function geometry(selector) {
    const element = document.querySelector(selector), css = getComputedStyle(element), box = element.getBoundingClientRect();
    return {width: box.width, height: box.height, padding: css.padding, gap: css.columnGap, columns: css.gridTemplateColumns, font: css.fontSize, weight: css.fontWeight, line: css.lineHeight, radius: css.borderRadius};
}

(async () => {
    const browser = await chromium.launch({channel: 'chrome', headless: true});
    const context = await browser.newContext({viewport: {width: 1480, height: 1100}, reducedMotion: 'reduce'});
    const errors = [], writes = [], blocked = [], failures = [], comparisons = [], screenshots = [];
    let rows = clone(baseline), failure = null, normalize = null, nextGet = null, nextPost = null, activeWrites = 0, maxConcurrent = 0;
    await context.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        if (url.hostname !== 'admin.test') {
            if (request.method() !== 'GET') {blocked.push(request.url()); return route.abort();}
            return ['cdnjs.cloudflare.com', 'fonts.googleapis.com', 'fonts.gstatic.com', 'cdn.jsdelivr.net'].includes(url.hostname) ? route.continue() : route.abort();
        }
        if (url.pathname.includes('api_runtime_settings/')) {
            if (request.method() === 'GET') {
                const settings = clone(rows), paused = nextGet; nextGet = null;
                if (paused) {paused.requested(); await paused.wait;}
                return route.fulfill({json: {status: 'ok', settings}});
            }
            assert.equal(request.method(), 'POST');
            const fields = multipart(request);
            assert.ok(keys.includes(fields.key), 'Only Win-back keys may be written');
            assert.ok(['save', 'delete'].includes(fields.action));
            assert.ok(Object.keys(fields).every(key => ['action', 'key', 'value', 'csrfmiddlewaretoken'].includes(key)));
            assert.ok(request.headers()['x-csrftoken'], 'CSRF header preserved');
            writes.push(fields); activeWrites++; maxConcurrent = Math.max(maxConcurrent, activeWrites);
            const paused = nextPost; nextPost = null;
            if (paused) {paused.requested(); await paused.wait;}
            await new Promise(resolve => setTimeout(resolve, 30));
            activeWrites--;
            if (failure?.key === fields.key) {
                const kind = failure.kind; failure = null;
                if (kind === 'lost_response') {
                    const setting = rows.find(row => row.key === fields.key);
                    Object.assign(setting, fields.action === 'delete' ? {value: '', is_set: false} : {value: fields.value, is_set: true});
                    return route.abort('failed');
                }
                if (kind === 'network') return route.abort('failed');
                return route.fulfill({status: 400, json: {status: 'error', message: 'Ошибка локальной фикстуры'}});
            }
            const setting = rows.find(row => row.key === fields.key);
            if (fields.action === 'delete') {
                Object.assign(setting, {value: '', display_value: 'env/default', is_set: false});
                return route.fulfill({json: {status: 'ok'}}); // Delete does not disclose the effective fallback.
            }
            const value = normalize?.key === fields.key ? normalize.value : fields.value;
            Object.assign(setting, {value, display_value: value, is_set: true, updated_at: '09.09.2026 12:31'});
            return route.fulfill({json: {status: 'ok', setting: clone(setting)}});
        }
        if (request.method() !== 'GET') {blocked.push(`${request.method()} ${url.pathname}`); return route.fulfill({status: 403, body: 'Unexpected write blocked'});}
        if (/^\/(admin|marketer|support)(-control)?\.html$/.test(url.pathname)) return route.fulfill({path: path.join(fixtures, url.pathname.slice(1)), contentType: 'text/html'});
        if (url.pathname === '/reference.html') return route.fulfill({contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet"><style>body{margin:0}</style></head><body>' + fs.readFileSync(referencePath, 'utf8') + '</body></html>'});
        if (url.pathname.startsWith('/static/')) {
            const file = path.join(root, 'engine/static', url.pathname.slice(8));
            return fs.existsSync(file) ? route.fulfill({path: file}) : route.fulfill({status: 404, body: ''});
        }
        return route.fulfill({status: 503, json: {status: 'error', message: 'Unrelated local API'}});
    });
    const page = await context.newPage(), reference = await context.newPage();
    for (const tab of [page, reference]) tab.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(5000);
    async function open({role = 'admin', reset = true} = {}) {
        if (reset) {rows = clone(baseline); failure = normalize = null;}
        await page.goto(`http://admin.test/${role}.html`);
        if (role !== 'admin') return;
        await page.evaluate(() => applyAdminTheme('dark'));
        await page.locator('.sidebar [data-tab="system"]').evaluate(element => element.click());
        await page.locator('[data-subtab="sys-winback"]').click();
        await page.locator(`${formSelector} [data-winback-key]`).first().waitFor();
        await page.evaluate(() => document.fonts.ready);
    }
    async function set(key, value) {
        const control = page.locator(id(key));
        if (key === 'winback_enabled') await control.selectOption(value); else await control.fill(value);
    }
    async function settled() {
        await page.waitForFunction(selector => document.querySelector(selector)?.getAttribute('aria-busy') !== 'true', formSelector);
        await page.waitForTimeout(60);
    }
    async function save() {await page.locator('[data-winback-save]').click(); await settled();}
    async function technical() {
        const details = page.locator(detailsSelector);
        if (!(await details.evaluate(element => element.open))) await details.locator('summary').click();
    }
    async function screenshot(selector, name) {
        await page.mouse.move(0, 0);
        await page.locator(selector).screenshot({path: path.join(output, `${name}.png`), animations: 'disabled', style: '.admin-topbar{visibility:hidden!important}'});
        screenshots.push(`${name}.png`);
    }
    const check = (label, callback) => {try {callback();} catch (error) {failures.push(`${label}: ${error.message}`);}};

    // Structure and row/control geometry come from the accepted settings view;
    // its four demo rows are not substituted for the eight real service keys.
    for (const width of [1480, 390]) {
        await page.setViewportSize({width, height: 1100}); await reference.setViewportSize({width, height: 1100});
        await open(); await reference.goto('http://admin.test/reference.html');
        await reference.locator('#mi-admin-picker').evaluate(select => {select.value = 'sys-winback'; select.dispatchEvent(new Event('change', {bubbles: true}));});
        await reference.evaluate(() => document.fonts.ready);
        assert.deepEqual(await page.locator(`${formSelector} [data-winback-key]`).evaluateAll(elements => elements.map(element => element.dataset.winbackKey)), keys);
        assert.equal(await page.locator(`${formSelector} form`).count(), 0, 'No nested or per-key forms');
        assert.equal(await page.locator('[data-winback-save]').count(), 1);
        assert.equal(await page.locator('.winback-settings-card [data-winback-save]').count(), 0, 'One footer Save outside the card');
        assert.equal(await page.locator(`${formSelector} [data-winback-save]`).count(), 1);
        assert.equal(await page.locator('.winback-settings-card h2').innerText(), 'Предложения для возвращения');
        const positions = await page.evaluate(() => ({card: document.querySelector('.winback-settings-card').getBoundingClientRect().bottom, save: document.querySelector('[data-winback-save]').getBoundingClientRect().top, details: document.querySelector('[data-winback-details]').getBoundingClientRect().top}));
        assert.ok(positions.save >= positions.card && positions.details > positions.save, 'Technical details follow the unified footer');
        for (const key of keys) {
            const label = page.locator(`label[for="winback-${key}"]`), control = page.locator(id(key));
            assert.equal(await label.count(), 1); assert.ok((await label.innerText()).trim());
            assert.equal(await control.getAttribute('data-setting-control'), '');
            assert.equal(await control.getAttribute('data-initial-value'), baseline.find(row => row.key === key).value);
        }
        for (const key of keys.slice(0, 2)) assert.equal(await page.locator(id(key)).inputValue(), '', 'Unset values are not guessed');
        assert.equal(await page.locator(id('winback_price_month')).getAttribute('placeholder'), 'env/default');
        const unchanged = writes.length; await save(); assert.equal(writes.length, unchanged, 'Unchanged form sends nothing');
        for (const [label, actualSelector, expectedSelector, properties] of [
            ['card', '.winback-settings-card', '.mi-panel', ['width', 'padding', 'radius']],
            ['heading', '.winback-settings-card h2', '.mi-panel h2', ['font', 'weight', 'line']],
            ['row', '[data-winback-key="winback_enabled"]', '.mi-setting', ['width', 'padding', 'columns', 'gap']],
            ['control', id('winback_price_month'), '.mi-setting input', ['width', 'height', 'font', 'radius']],
        ]) {
            const actual = await page.evaluate(geometry, actualSelector), expected = await reference.evaluate(geometry, expectedSelector);
            comparisons.push({width, label, actual, expected});
            for (const property of properties) check(`${width}.${label}.${property}`, () => typeof expected[property] === 'number'
                ? assert.ok(Math.abs(actual[property] - expected[property]) < 1.1, `${actual[property]} != ${expected[property]}`)
                : assert.equal(actual[property], expected[property]));
        }
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
        await screenshot(formSelector, `real-${width}`);
        await reference.locator('.mi-panel').screenshot({path: path.join(output, `reference-card-${width}.png`)});
        await technical();
        for (const row of baseline) {
            const help = await page.locator(detailsSelector).innerText();
            assert.ok(help.includes(row.key) && help.includes(row.description), `Technical key and description preserved: ${row.key}`);
            const reset = page.locator(`[data-winback-reset="${row.key}"]`);
            assert.equal(await reset.getAttribute('type'), 'button');
        }
        assert.match(await page.locator(detailsSelector).innerText(), /env\/default/);
        assert.match(await page.locator(detailsSelector).innerText(), /БД/);
        await screenshot(formSelector, `technical-${width}`);
        await page.evaluate(() => applyAdminTheme('light')); await screenshot(formSelector, `light-${width}`);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    }
    await page.setViewportSize({width: 1480, height: 1100});

    // No autosave; changed-only writes and response canonical values.
    await open(); let start = writes.length;
    await set('winback_price_month', '0250'); await page.locator('.winback-settings-card h2').click();
    await page.waitForTimeout(120); assert.equal(writes.length, start);
    normalize = {key: 'winback_price_month', value: '251'}; await save();
    assert.deepEqual(writes.slice(start).map(({action, key, value}) => ({action, key, value})), [{action: 'save', key: 'winback_price_month', value: '250'}]);
    assert.equal(await page.locator(id('winback_price_month')).inputValue(), '251');
    assert.equal(await page.locator(id('winback_price_month')).getAttribute('data-initial-value'), '251');
    start = writes.length; await save(); assert.equal(writes.length, start, 'Canonical response clears the dirty state');
    await open(); start = writes.length;
    await set('winback_price_month', '1e2'); await set('winback_price_threemonths', '200.0'); await save();
    assert.deepEqual(writes.slice(start).map(row => row.value), ['100', '200'], 'Validated integer input uses canonical POST values');

    // Validate the entire draft before any network write, even when an earlier
    // row is valid. Test positive integers, fractional values and hour bounds.
    for (const [key, value] of [['winback_price_year', '0'], ['winback_price_year', '-1'], ['winback_offer_ttl_hours', '1.5'], ['winback_grace_hours', '0'], ['winback_send_hour_start', '-1'], ['winback_send_hour_end', '24']]) {
        await open(); start = writes.length;
        await set('winback_price_month', '250'); await set(key, value); await save();
        assert.equal(writes.length, start, `Invalid ${key}=${value} blocks the complete batch`);
        assert.equal(await page.locator(id('winback_price_month')).inputValue(), '250');
    }
    await open(); start = writes.length;
    await set('winback_send_hour_start', '0'); await set('winback_send_hour_end', '23'); await save();
    assert.deepEqual(writes.slice(start).map(row => row.value), ['0', '23']);

    // Operational ordering and a stop-on-first-failure retry.
    await open(); start = writes.length;
    await set('winback_enabled', '1'); await set('winback_price_month', '250'); await set('winback_grace_hours', '72'); await save();
    assert.deepEqual(writes.slice(start).map(row => row.key), ['winback_price_month', 'winback_grace_hours', 'winback_enabled']);
    start = writes.length; await set('winback_enabled', '0'); await set('winback_price_year', '1599'); await save();
    assert.deepEqual(writes.slice(start).map(row => row.key), ['winback_enabled', 'winback_price_year']);
    start = writes.length; await technical(); await page.locator('[data-winback-reset="winback_enabled"]').click();
    await set('winback_price_month', '260'); await save();
    assert.deepEqual(writes.slice(start).map(({action, key}) => ({action, key})), [{action: 'save', key: 'winback_price_month'}, {action: 'delete', key: 'winback_enabled'}], 'Restoring the enabled fallback occurs last');
    await open(); start = writes.length;
    await set('winback_price_month', '250'); await set('winback_price_threemonths', '499'); await set('winback_price_year', '1599');
    failure = {key: 'winback_price_threemonths', kind: 'server'}; await save();
    assert.deepEqual(writes.slice(start).map(row => row.key), ['winback_price_month', 'winback_price_threemonths']);
    assert.equal(await page.locator(id('winback_price_month')).getAttribute('data-initial-value'), '250');
    assert.equal(await page.locator(id('winback_price_threemonths')).inputValue(), '499');
    assert.equal(await page.locator(id('winback_price_threemonths')).getAttribute('data-initial-value'), '449');
    assert.equal(await page.locator(id('winback_price_year')).inputValue(), '1599');
    start = writes.length; await save();
    assert.deepEqual(writes.slice(start).map(row => row.key), ['winback_price_threemonths', 'winback_price_year'], 'Do not resend successful rows');

    // Reset is staged and can be undone; a successful delete leaves the local
    // override empty, because the endpoint does not return its fallback value.
    await open(); await technical(); start = writes.length;
    const reset = page.locator('[data-winback-reset="winback_price_year"]');
    await reset.click(); assert.equal(writes.length, start); await reset.click();
    assert.equal(await page.locator(id('winback_price_year')).inputValue(), '1499');
    await save(); assert.equal(writes.length, start, 'Undo reset leaves no pending request');
    await reset.click(); await save();
    assert.deepEqual(writes.slice(start).map(({action, key}) => ({action, key})), [{action: 'delete', key: 'winback_price_year'}]);
    assert.equal(await page.locator(id('winback_price_year')).inputValue(), '');
    assert.equal(await page.locator(id('winback_price_year')).getAttribute('data-initial-value'), '');
    const afterReset = writes.length; await save(); assert.equal(writes.length, afterReset, 'Successful reset clears the draft');

    // Both network failure and fresh runtime GET retain unsaved work.
    await open(); start = writes.length; await set('winback_price_month', '250');
    failure = {key: 'winback_price_month', kind: 'network'}; await save();
    assert.equal(await page.locator(id('winback_price_month')).inputValue(), '250');
    assert.equal(await page.locator(id('winback_price_month')).getAttribute('data-initial-value'), '');
    await save(); assert.equal(writes.length, start + 2);
    await open(); start = writes.length;
    await set('winback_price_month', '260'); await set('winback_price_threemonths', '499');
    failure = {key: 'winback_price_month', kind: 'lost_response'}; await save();
    assert.deepEqual(writes.slice(start).map(row => row.key), ['winback_price_month', 'winback_price_threemonths'], 'GET reconciliation allows the batch to continue without repeating a committed write');
    assert.equal(await page.locator(id('winback_price_month')).getAttribute('data-initial-value'), '260');
    await technical(); start = writes.length; await page.locator('[data-winback-reset="winback_price_year"]').click();
    failure = {key: 'winback_price_year', kind: 'lost_response'}; await save();
    assert.equal(writes.length, start + 1); assert.equal(await page.locator(id('winback_price_year')).inputValue(), '', 'GET can also confirm an ambiguously acknowledged reset');
    await open(); await set('winback_price_month', '270'); await technical(); await page.locator('[data-winback-reset="winback_price_year"]').click();
    rows.find(row => row.key === 'winback_grace_hours').value = '60';
    await page.evaluate(() => loadRuntimeSettings());
    assert.equal(await page.locator(id('winback_price_month')).inputValue(), '270');
    assert.equal(await page.locator(id('winback_grace_hours')).inputValue(), '60');
    start = writes.length; await save();
    assert.deepEqual(writes.slice(start).map(({action, key}) => ({action, key})), [{action: 'save', key: 'winback_price_month'}, {action: 'delete', key: 'winback_price_year'}]);

    // A GET captures its old settings when requested, then deliberately arrives
    // after Save. Cover requests begun both before and during the write batch.
    for (const duringSave of [false, true]) {
        await open(); await set('winback_price_month', '280');
        const stale = hold(); let saving, refresh;
        if (duringSave) {
            const pausedWrite = hold(); nextPost = pausedWrite;
            saving = save(); await pausedWrite.ready;
            assert.equal(await page.locator('[data-winback-save]').isDisabled(), true);
            assert.equal(await page.locator(id('winback_price_month')).isDisabled(), true);
            nextGet = stale; refresh = page.evaluate(() => loadRuntimeSettings()); await stale.ready;
            pausedWrite.release(); await saving;
        } else {
            nextGet = stale; refresh = page.evaluate(() => loadRuntimeSettings()); await stale.ready;
            await save();
        }
        stale.release(); await refresh; await settled();
        assert.equal(await page.locator(id('winback_price_month')).inputValue(), '280');
        assert.equal(await page.locator(id('winback_price_month')).getAttribute('data-initial-value'), '280', `Stale GET started ${duringSave ? 'during' : 'before'} Save cannot restore the old value`);
    }

    rows = clone(baseline).map(row => ({...row, value: row.value || (row.key === 'winback_enabled' ? '1' : '199'), is_set: true}));
    await open({reset: false}); await screenshot(formSelector, 'populated-1480');
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({path: path.join(output, 'page-1480.png'), fullPage: true, animations: 'disabled'});
    screenshots.push('page-1480.png');

    // The module must not introduce an editor for restricted roles.
    for (const role of ['marketer', 'support']) {
        await open({role});
        assert.equal(await page.locator('[data-winback-form]').count(), 0);
        assert.equal(await page.locator('[data-subtab="sys-winback"]').count(), 0);
        await page.goto(`http://admin.test/${role}-control.html`);
        assert.equal(await page.locator('[data-subtab="sys-winback"]').count(), 0);
    }
    assert.equal(maxConcurrent, 1, 'Settings are saved sequentially');
    assert.deepEqual(blocked, [], 'No writes to unrelated APIs'); assert.deepEqual(errors, [], 'No uncaught JavaScript errors');
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({comparisons, failures, writes, blocked, errors, screenshots}, null, 2));
    await browser.close();
    assert.deepEqual(failures, [], 'Reference geometry differences');
    console.log(`PASS: eight Win-back keys, reference geometry, role restrictions, validations, modified-only sequential saves, retry/reset/GET draft preservation. ${writes.length} intercepted writes; no real API calls.`);
})().catch(error => {console.error(error); process.exit(1);});
