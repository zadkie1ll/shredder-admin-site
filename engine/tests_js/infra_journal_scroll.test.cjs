// Журнал сервера в карточке инфраструктуры: внутренняя прокрутка переживает
// фоновую перерисовку карточки (раз в 5 секунд) и ручное обновление.
// Реальный JS из admin_dashboard.html исполняется в node:vm с фиктивным DOM.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..', '..');
const TEMPLATE = 'engine/templates/admin_dashboard.html';
const read = (relativePath) => fs.readFileSync(path.join(ROOT, relativePath), 'utf8');

// Вырезает объявление по балансу скобок, начиная с последнего символа маркера.
function extractBlock(source, marker) {
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

function loadScrollHelpers() {
    const source = read(TEMPLATE);
    const context = vm.createContext({});
    vm.runInContext([
        extractBlock(source, 'const INFRA_SCROLL_PANE_SELECTORS = ['),
        extractBlock(source, 'function infraSnapshotScrollPanes(container) {'),
        extractBlock(source, 'function infraRestoreScrollPanes(container, snapshot) {'),
    ].join('\n'), context);
    return context;
}

const pane = (scrollTop, scrollHeight = 2400, clientHeight = 520) => ({scrollTop, scrollHeight, clientHeight});
const container = (panes) => ({querySelectorAll: (selector) => panes[selector] || []});

test('infra detail: журнал сервера сохраняет прокрутку при перерисовке', () => {
    const context = loadScrollHelpers();
    context.before = container({'.infra-log-list': [pane(860)], '.node-provision-log': [pane(120)]});
    context.snapshot = vm.runInContext('infraSnapshotScrollPanes(before)', context);

    // Перерисовка: панели создаются заново и стоят в начале.
    const journal = pane(0);
    const provisionLog = pane(0);
    context.after = container({'.infra-log-list': [journal], '.node-provision-log': [provisionLog]});
    vm.runInContext('infraRestoreScrollPanes(after, snapshot)', context);

    assert.equal(journal.scrollTop, 860);
    assert.equal(provisionLog.scrollTop, 120);
});

test('infra detail: укоротившийся журнал не перематывается за пределы содержимого', () => {
    const context = loadScrollHelpers();
    context.before = container({'.infra-log-list': [pane(860)]});
    context.snapshot = vm.runInContext('infraSnapshotScrollPanes(before)', context);

    // Событий стало меньше: доступная прокрутка всего 180px.
    const shortened = pane(0, 700, 520);
    context.after = container({'.infra-log-list': [shortened]});
    vm.runInContext('infraRestoreScrollPanes(after, snapshot)', context);

    assert.equal(shortened.scrollTop, 180);
});

test('infra detail: позиция одной панели не утекает в другую', () => {
    const context = loadScrollHelpers();
    // На момент снимка лога установки ноды на странице не было.
    context.before = container({'.infra-log-list': [pane(430)]});
    context.snapshot = vm.runInContext('infraSnapshotScrollPanes(before)', context);

    const journal = pane(0);
    const provisionLog = pane(0);
    context.after = container({'.infra-log-list': [journal], '.node-provision-log': [provisionLog]});
    vm.runInContext('infraRestoreScrollPanes(after, snapshot)', context);

    assert.equal(journal.scrollTop, 430);
    assert.equal(provisionLog.scrollTop, 0);
});

test('infra detail: перерисовка снимает позицию до рендера и возвращает после', () => {
    const body = extractBlock(read(TEMPLATE), 'async function refreshInfraDetail(full) {');
    const snapshotAt = body.indexOf('infraSnapshotScrollPanes(container)');
    const renderAt = body.indexOf('renderInfraDetail(container');
    const restoreAt = body.indexOf('infraRestoreScrollPanes(container');

    assert.notEqual(snapshotAt, -1, 'снимок прокрутки не вызывается');
    assert.notEqual(restoreAt, -1, 'восстановление прокрутки не вызывается');
    assert.ok(snapshotAt < renderAt, 'снимок должен делаться до перерисовки');
    assert.ok(renderAt < restoreAt, 'восстановление должно идти после перерисовки');
});
