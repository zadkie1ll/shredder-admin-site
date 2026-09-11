// Поведенческие тесты фронтенда по ревью аудита 2026-09-10 (группа L3).
// Реальный JS из шаблонов и статики выполняется в node:vm с фиктивным DOM:
// без браузера, сети и БД.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const read = (relativePath) => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

// Вырезает вызов `<marker>(...)` по балансу скобок; строки и комментарии пропускаются.
function extractCall(source, marker) {
    const start = source.indexOf(marker);
    assert.notEqual(start, -1, `marker not found: ${marker}`);
    let depth = 0;
    let quote = null;
    let index = start + marker.length - 1;
    for (; index < source.length; index += 1) {
        const char = source[index];
        const next = source[index + 1];
        if (quote) {
            if (char === '\\') index += 1;
            else if (char === quote) quote = null;
            continue;
        }
        if (char === '/' && next === '/') { index = source.indexOf('\n', index); continue; }
        if (char === '/' && next === '*') { index = source.indexOf('*/', index) + 1; continue; }
        if (char === '\'' || char === '"' || char === '`') { quote = char; continue; }
        if (char === '(' || char === '{' || char === '[') depth += 1;
        if (char === ')' || char === '}' || char === ']') {
            depth -= 1;
            if (depth === 0) break;
        }
    }
    return `${source.slice(start, index + 1)};`;
}

/* ── F7 / F3: запуск оплаты на лендингах и в кабинете ── */

const PAYMENT_TEMPLATES = [
    'engine/templates/index_vpn.html',
    'engine/templates/index_vps.html',
    'engine/templates/index_vps_direct_sale.html',
    'engine/templates/dashboard.html',
];
const NEUTRAL_PAYMENT_ERROR = 'Не удалось открыть оплату. Обновите страницу и попробуйте ещё раз';

const headers = (contentType) => ({
    get: (name) => (String(name).toLowerCase() === 'content-type' ? contentType : null),
});
const htmlResponse = (status) => ({
    ok: false,
    status,
    headers: headers('text/html; charset=utf-8'),
    json: async () => { throw new SyntaxError('Unexpected token \'<\', "<!doctype "... is not valid JSON'); },
});
const plainTextResponse = (status, body) => ({
    ok: false,
    status,
    headers: headers('text/html; charset=utf-8'),
    json: async () => { throw new SyntaxError(`Unexpected token '${body[0]}', "${body.slice(0, 10)}"... is not valid JSON`); },
});
const jsonResponse = (status, payload) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: headers('application/json'),
    json: async () => payload,
});

function setupPaymentForm(templatePath, fetchImpl, {withAbortController = true, timers = null} = {}) {
    const block = extractCall(read(templatePath), "document.querySelectorAll('[data-payment-form]').forEach(");
    const messages = [];
    const fetchCalls = [];
    let submitHandler = null;
    const submitButton = {disabled: false};
    const paymentWindow = {
        closed: false,
        opener: {},
        location: {href: ''},
        document: {write() {}},
        close() { this.closed = true; },
    };
    const form = {
        action: '/pay/',
        dataset: {tariffId: 'month', tariffPrice: '299'},
        addEventListener(type, handler) { if (type === 'submit') submitHandler = handler; },
        querySelector(selector) { return selector === 'button[type="submit"]' ? submitButton : null; },
    };
    const trackedFetch = (url, options) => {
        fetchCalls.push({url, options});
        return fetchImpl(url, options);
    };
    function FormData() {}
    const location = {href: 'https://site.test/'};
    const window = {
        fetch: trackedFetch,
        FormData,
        open: () => paymentWindow,
        alert: (message) => messages.push(String(message)),
        location,
        // timers: записываем задержки вместо реальных таймеров (55 с не ждём).
        setTimeout: timers ? (fn, ms) => { timers.push(ms); return timers.length; } : setTimeout,
        clearTimeout: timers ? () => {} : clearTimeout,
    };
    const context = {
        window,
        document: {querySelectorAll: (selector) => (selector === '[data-payment-form]' ? [form] : [])},
        fetch: trackedFetch,
        FormData,
        sendGoal() {},
        showToast: (message) => messages.push(String(message)),
    };
    // В vm-контексте нет web-API: без явной передачи AbortController недоступен,
    // как в старых Safari/WebView.
    if (withAbortController) context.AbortController = AbortController;
    vm.createContext(context);
    vm.runInContext(block, context);
    assert.equal(typeof submitHandler, 'function', `${templatePath}: submit handler is not attached`);
    return {
        submit: () => submitHandler({preventDefault() {}}),
        messages,
        fetchCalls,
        paymentWindow,
        submitButton,
        location,
    };
}

for (const templatePath of PAYMENT_TEMPLATES) {
    const name = path.basename(templatePath);

    test(`${name}: HTML/plain-text answer from /pay/ shows a neutral message instead of SyntaxError`, async () => {
        for (const response of [htmlResponse(403), htmlResponse(502), plainTextResponse(400, 'Выбранный тариф не найден')]) {
            const page = setupPaymentForm(templatePath, async () => response);
            await page.submit();
            assert.deepEqual(page.messages, [NEUTRAL_PAYMENT_ERROR]);
            assert.equal(page.paymentWindow.closed, true);
            assert.equal(page.submitButton.disabled, false);
            assert.equal(page.fetchCalls.length, 1);
        }
    });

    test(`${name}: JSON error shows payload.message from the server`, async () => {
        const page = setupPaymentForm(templatePath, async () => jsonResponse(403, {
            status: 'error',
            message: 'Аккаунт заблокирован. Напишите в поддержку.',
        }));
        await page.submit();
        assert.deepEqual(page.messages, ['Аккаунт заблокирован. Напишите в поддержку.']);
        assert.equal(page.submitButton.disabled, false);
    });

    test(`${name}: broken JSON body or network error falls back to the neutral message`, async () => {
        const brokenJson = {
            ok: true,
            status: 200,
            headers: headers('application/json'),
            json: async () => { throw new SyntaxError('Unexpected end of JSON input'); },
        };
        const failures = [async () => brokenJson, async () => { throw new TypeError('Failed to fetch'); }];
        for (const fetchImpl of failures) {
            const page = setupPaymentForm(templatePath, fetchImpl);
            await page.submit();
            assert.deepEqual(page.messages, [NEUTRAL_PAYMENT_ERROR]);
            assert.equal(page.submitButton.disabled, false);
        }
    });

    test(`${name}: launch deadline is above the /pay/ server budget and below the 60 s proxy timeout`, async () => {
        // FE-FINAL-01: RWMS 8 с + провайдер с ретраями + письмо укладываются
        // до ~51 с; nginx proxy_read_timeout и gunicorn --timeout — 60 с.
        const timers = [];
        const page = setupPaymentForm(templatePath, async () => jsonResponse(200, {
            status: 'ok',
            payment_url: 'https://pay.example/checkout',
            payment_status_url: '/payment/status/token/',
        }), {timers});
        await page.submit();
        assert.equal(timers.length, 1);
        assert.ok(timers[0] >= 50000 && timers[0] < 60000, `payment launch deadline ${timers[0]} ms`);
    });

    test(`${name}: timeout keeps its own message`, async () => {
        const page = setupPaymentForm(templatePath, async () => {
            throw Object.assign(new Error('The operation was aborted'), {name: 'AbortError'});
        });
        await page.submit();
        assert.deepEqual(page.messages, ['Сервис оплаты отвечает слишком долго. Попробуйте ещё раз.']);
    });

    test(`${name}: success opens provider tab and status page with and without AbortController`, async () => {
        for (const withAbortController of [true, false]) {
            const page = setupPaymentForm(templatePath, async () => jsonResponse(200, {
                status: 'ok',
                payment_url: 'https://pay.example/checkout',
                payment_status_url: '/payment/status/token/',
            }), {withAbortController});
            await page.submit();
            assert.deepEqual(page.messages, []);
            assert.equal(page.paymentWindow.location.href, 'https://pay.example/checkout');
            assert.equal(page.location.href, '/payment/status/token/');
            assert.equal('signal' in page.fetchCalls[0].options, withAbortController);
            assert.equal(page.fetchCalls[0].options.headers['X-Payment-Launch'], 'new-tab');
        }
    });

    test(`${name}: confirmation_required still redirects the current tab`, async () => {
        const page = setupPaymentForm(templatePath, async () => jsonResponse(200, {
            status: 'ok',
            payment_url: '/login/',
            payment_status_url: '/login/',
            confirmation_required: true,
        }));
        await page.submit();
        assert.deepEqual(page.messages, []);
        assert.equal(page.location.href, '/login/');
        assert.equal(page.paymentWindow.closed, true);
        assert.equal(page.paymentWindow.location.href, '');
    });
}

/* ── F3: mi-network.js на старых браузерах ── */

test('mi-network.js works without AbortController: no signal and no timer', async () => {
    const fetchOptions = [];
    const timers = [];
    const context = vm.createContext({
        window: {},
        setTimeout: (...args) => { timers.push(args); return 1; },
        clearTimeout() {},
        fetch: async (url, options) => {
            fetchOptions.push(options);
            return {ok: true, status: 200, json: async () => ({status: 'ok'})};
        },
    });
    vm.runInContext(read('engine/static/mi-network.js'), context);
    const payload = await vm.runInContext(
        "window.MiNetwork.requestJSON('/status', {headers: {'X-Requested-With': 'XMLHttpRequest'}})",
        context,
    );
    assert.equal(payload.status, 'ok');
    assert.equal('signal' in fetchOptions[0], false);
    assert.equal(fetchOptions[0].headers['X-Requested-With'], 'XMLHttpRequest');
    assert.equal(timers.length, 0);
});

test('mi-network.js forwards an already aborted external signal and JSON error message', async () => {
    const {requestJSON} = require('../static/mi-network.js');
    const original = global.fetch;
    const external = new AbortController();
    external.abort();
    let forwardedAborted = null;
    global.fetch = async (url, options) => {
        forwardedAborted = options.signal.aborted;
        return {ok: false, status: 400, json: async () => ({message: 'Тариф не найден'})};
    };
    try {
        await assert.rejects(
            requestJSON('/pay', {signal: external.signal}),
            (error) => error.status === 400 && error.message === 'Тариф не найден',
        );
        assert.equal(forwardedAborted, true);
    } finally {
        global.fetch = original;
    }
});

/* ── SUP-04 / F2: страница тикета в админке поддержки ── */

function renderTicketMessages(template, ids) {
    const loop = template.match(/{%\s*for message in support_messages\s*%}([\s\S]*?){%\s*empty\s*%}([\s\S]*?){%\s*endfor\s*%}/);
    assert.ok(loop, 'support_messages loop not found in template');
    const strip = (html) => html.replace(/{%[\s\S]*?%}/g, '').replace(/{{[\s\S]*?}}/g, '');
    return {
        messagesHtml: ids.map((id) => strip(loop[1].replace(/{{\s*message\.id\s*}}/g, String(id)))).join(''),
        emptyHtml: strip(loop[2]),
    };
}

function makeChat(initialHtml) {
    return {
        innerHTML: initialHtml,
        scrollTop: 0,
        clientHeight: 100,
        scrollHeight: 100,
        querySelectorAll(selector) {
            assert.equal(selector, '[data-message-id]');
            return Array.from(this.innerHTML.matchAll(/data-message-id="([^"]*)"/g), (match) => ({dataset: {messageId: match[1]}}));
        },
        querySelector(selector) {
            if (selector === '[data-message-id]') return this.querySelectorAll(selector)[0] || null;
            if (selector === '[data-empty-messages]' && this.innerHTML.includes('data-empty-messages')) {
                const chat = this;
                return {remove() { chat.innerHTML = chat.innerHTML.replace(/<div data-empty-messages[\s\S]*?<\/div>/, ''); }};
            }
            return null;
        },
        insertAdjacentHTML(position, html) {
            assert.equal(position, 'beforeend');
            this.innerHTML += html;
        },
    };
}

const ticketMessage = (id, isUser = true) => ({
    id,
    is_user: isUser,
    message: `Сообщение ${id}`,
    created_at: '11.09.2026 10:00',
    attachments: [],
});
const renderedMessageIds = (chat) => Array.from(chat.innerHTML.matchAll(/data-message-id="([^"]*)"/g), (match) => match[1]);

function setupAdminTicket(initialHtml, serverMessages) {
    const requests = [];
    let submitHandler = null;
    const chat = makeChat(initialHtml);
    const form = {
        action: '/support-admin/tickets/1/messages/',
        dataset: {messagesUrl: '/support-admin/tickets/1/messages.json'},
        button: {disabled: false},
        addEventListener(type, handler) { if (type === 'submit') submitHandler = handler; },
        querySelector(selector) { return selector === 'button[type="submit"]' ? this.button : null; },
        reset() {},
    };
    const nodes = {
        'admin-chat-error': {textContent: '', hidden: true},
        'admin-chat-scroll': chat,
        'admin-support-message-form': form,
    };
    const context = vm.createContext({
        window: {},
        document: {hidden: false, getElementById: (id) => nodes[id] || null, addEventListener() {}},
        setInterval() {},
        FormData: function FormData() {},
        MiNetwork: {
            requestJSON: async (url, options, timeoutMs) => {
                const method = (options && options.method) || 'GET';
                requests.push({url, method, timeoutMs});
                return method === 'POST' ? {status: 'ok'} : {status: 'ok', messages: serverMessages()};
            },
        },
    });
    vm.runInContext(read('engine/static/scripts/support_admin_ticket_detail-1.js'), context);
    return {
        chat,
        requests,
        refresh: () => vm.runInContext('refreshMessages()', context),
        submit: () => submitHandler({preventDefault() {}}),
    };
}

test('support admin ticket: first refresh does not duplicate server-rendered messages', async () => {
    const template = read('engine/templates/support_admin_ticket_detail.html');
    const {messagesHtml} = renderTicketMessages(template, [1, 2]);
    let serverMessages = [ticketMessage(1), ticketMessage(2, false)];
    const page = setupAdminTicket(messagesHtml, () => serverMessages);

    await page.refresh();
    assert.deepEqual(renderedMessageIds(page.chat), ['1', '2']);

    serverMessages = serverMessages.concat(ticketMessage(3));
    await page.refresh();
    assert.deepEqual(renderedMessageIds(page.chat), ['1', '2', '3']);
});

test('support admin ticket: empty state is replaced by the first message', async () => {
    const template = read('engine/templates/support_admin_ticket_detail.html');
    const {emptyHtml} = renderTicketMessages(template, []);
    assert.ok(emptyHtml.includes('Сообщений пока нет'));
    const page = setupAdminTicket(emptyHtml, () => [ticketMessage(7)]);

    await page.refresh();
    assert.equal(page.chat.innerHTML.includes('Сообщений пока нет'), false);
    assert.deepEqual(renderedMessageIds(page.chat), ['7']);
});

test('support admin ticket: reply with attachments gets 180 s, polling keeps the short default', async () => {
    const template = read('engine/templates/support_admin_ticket_detail.html');
    const {messagesHtml} = renderTicketMessages(template, [1]);
    const page = setupAdminTicket(messagesHtml, () => [ticketMessage(1)]);

    await page.submit();
    const posts = page.requests.filter((request) => request.method === 'POST');
    const polls = page.requests.filter((request) => request.method === 'GET');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].timeoutMs, 180000);
    assert.ok(polls.length >= 1);
    polls.forEach((request) => assert.equal(request.timeoutMs, undefined));
    assert.deepEqual(renderedMessageIds(page.chat), ['1']);
});

/* ── PAY-09 / F6: страница статуса оплаты ── */

function setupPaymentStatus(values) {
    const template = read('engine/templates/payment_status.html');
    const scripts = Array.from(template.matchAll(/<script>([\s\S]*?)<\/script>/g), (match) => match[1]);
    assert.equal(scripts.length, 1);
    const source = scripts[0]
        .replace(/{%\s*url\s+["']dashboard["']\s*%}/g, '/dashboard/')
        .replace(/{{\s*([a-z_]+)[^}]*}}/g, (_, name) => String(values[name] || ''));
    const element = (extra = {}) => Object.assign({hidden: false, textContent: '', href: '', dataset: {}, addEventListener() {}}, extra);
    const nodes = {
        'status-shell': element(),
        'status-title': element(),
        'orb-emoji': element(),
        'login-action': element({href: values.login_url || '', hidden: !values.login_url}),
        'login-page-action': element({href: '/login/', hidden: true}),
        'success-redirect-copy': element(),
        'success-login-copy': element(),
        'payment-action': element(),
        'cabinet-action': element(),
        'failed-copy': element(),
        'poll-error': element({hidden: true}),
        'poll-retry': element({hidden: true}),
        'orb-wrap': element(),
    };
    const timers = [];
    const requests = [];
    const schedule = (fn, ms) => { timers.push(ms); return timers.length; };
    const window = {location: {search: '', href: 'https://site.test/payment/status/token/', replace() {}}, setTimeout: schedule};
    const context = vm.createContext({
        window,
        document: {hidden: false, getElementById: (id) => nodes[id] || null, addEventListener() {}},
        setTimeout: schedule,
        clearTimeout() {},
        MiNetwork: {requestJSON: async (url) => { requests.push(url); return {status: 'pending'}; }},
    });
    vm.runInContext(source, context);
    return {nodes, timers, window, context, requests};
}

function assertTerminalStatusView(page, message) {
    assert.equal(page.nodes['status-shell'].dataset.status, 'info');
    assert.equal(page.nodes['status-title'].textContent, message);
    assert.equal(page.nodes['orb-wrap'].hidden, true);
    assert.equal(page.nodes['login-page-action'].hidden, false);
    assert.equal(page.nodes['login-action'].hidden, true);
    assert.equal(page.nodes['payment-action'].hidden, true);
    assert.equal(page.nodes['cabinet-action'].hidden, true);
}

const LEGACY_NEUTRAL_MESSAGE = 'Статус оплаты и подписку можно посмотреть в личном кабинете после входа';

test('payment status: terminal legacy link on first render does not poll and offers the login page', () => {
    const page = setupPaymentStatus({
        initial_status: 'pending',
        initial_message: LEGACY_NEUTRAL_MESSAGE,
        initial_terminal: 'true',
        status_api_url: '/payment/status/legacy-token/json/',
    });
    assertTerminalStatusView(page, LEGACY_NEUTRAL_MESSAGE);
    assert.equal(page.requests.length, 0);
    assert.deepEqual(page.timers, []);
});

test('payment status: polled terminal payload stops polling and leaves the pending state', () => {
    const page = setupPaymentStatus({initial_status: 'pending', login_url: ''});
    assert.equal(page.nodes['status-shell'].dataset.status, 'pending');
    const stopped = vm.runInContext(
        `applyStatus({status: 'pending', terminal: true, login_url: '', payment_url: '', message: ${JSON.stringify(LEGACY_NEUTRAL_MESSAGE)}})`,
        page.context,
    );
    assert.equal(stopped, true);
    assertTerminalStatusView(page, LEGACY_NEUTRAL_MESSAGE);
});

test('payment status: ordinary pending still polls and keeps the waiting view', () => {
    const page = setupPaymentStatus({initial_status: 'pending', status_api_url: '/payment/status/token/json/'});
    assert.equal(page.requests.length, 1);
    assert.equal(page.nodes['status-shell'].dataset.status, 'pending');
    assert.equal(page.nodes['orb-wrap'].hidden, false);
    assert.equal(page.nodes['login-page-action'].hidden, true);
    assert.equal(vm.runInContext("applyStatus({status: 'pending', message: 'Ждем'})", page.context), false);
});

test('payment status: succeeded without login_url offers login page instead of an empty link', () => {
    const page = setupPaymentStatus({initial_status: 'succeeded', login_url: ''});
    assert.equal(page.nodes['login-action'].hidden, true);
    assert.equal(page.nodes['login-page-action'].hidden, false);
    assert.equal(page.nodes['success-login-copy'].hidden, false);
    assert.equal(page.nodes['success-redirect-copy'].hidden, true);
    assert.equal(page.timers.includes(1200), false);
    assert.equal(page.window.location.href, 'https://site.test/payment/status/token/');
});

test('payment status: succeeded with login_url keeps the redirect to the cabinet', () => {
    const page = setupPaymentStatus({initial_status: 'succeeded', login_url: '/dashboard/'});
    assert.equal(page.nodes['login-action'].hidden, false);
    assert.equal(page.nodes['login-action'].href, '/dashboard/');
    assert.equal(page.nodes['login-page-action'].hidden, true);
    assert.equal(page.nodes['success-redirect-copy'].hidden, false);
    assert.equal(page.nodes['success-login-copy'].hidden, true);
    assert.equal(page.timers.includes(1200), true);
});

test('payment status: polled success without login_url switches to the login hint', () => {
    const page = setupPaymentStatus({initial_status: 'pending', login_url: ''});
    const stopped = vm.runInContext("applyStatus({status: 'succeeded', login_url: ''})", page.context);
    assert.equal(stopped, true);
    assert.equal(page.nodes['status-shell'].dataset.status, 'succeeded');
    assert.equal(page.nodes['login-action'].hidden, true);
    assert.equal(page.nodes['login-page-action'].hidden, false);
    assert.equal(page.nodes['success-login-copy'].hidden, false);
});
