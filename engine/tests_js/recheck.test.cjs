const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {requestJSON} = require('../static/mi-network.js');

test('deadline includes a response body that never arrives', async () => {
    const original = global.fetch;
    global.fetch = async (_, {signal}) => ({ok: true, json: () => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    })});
    try { await assert.rejects(requestJSON('/hung', {}, 10), {name:'AbortError'}); }
    finally { global.fetch = original; }
});

test('HTTP 413 produces a visible actionable error even when nginx returns HTML', async () => {
    const original = global.fetch;
    global.fetch = async () => ({ok:false, status:413, json: async () => {throw Error('HTML');}});
    try { await assert.rejects(requestJSON('/upload'), error => error.status === 413 && error.message.includes('Уменьшите')); }
    finally { global.fetch = original; }
});

test('a failed mutation is sent once', async () => {
    const original = global.fetch;
    let calls = 0;
    global.fetch = async () => { calls++; throw new TypeError('offline'); };
    try { await assert.rejects(requestJSON('/mutation', {method:'POST'})); assert.equal(calls, 1); }
    finally { global.fetch = original; }
});

function makePagination() {
    const nodes = {};
    for (const id of ['tickets-list','tickets-load-more','tickets-previous','tickets-page-status','open-count','closed-count']) {
        nodes[id] = {dataset:{}, hidden:false, isConnected:true, innerHTML:'', addEventListener(){}, focus(){}, contains(){return false;},
            querySelectorAll(){return Array.from(this.innerHTML.matchAll(/data-ticket-id="(\d+)"/g), m => ({dataset:{ticketId:m[1]}, focus(){}}));}};
    }
    nodes['tickets-load-more'].dataset = {nextUpdatedAt:'fixed',nextId:'451'};
    nodes['tickets-list'].innerHTML = Array.from({length:50}, (_,i)=>`<a data-ticket-id="${500-i}"></a>`).join('');
    let fail = false;
    const context = vm.createContext({
        document: {hidden:false, activeElement:null, getElementById:id=>nodes[id]},
        main:{dataset:{ticketsUrl:'/tickets?status=open'}},
        window:{location:{href:'https://site.test/admin'},setTimeout,clearTimeout},
        URL,AbortController,console,activeAdminTab:'support',ticketsRefreshInFlight:false,
        ticketCardHtml:ticket=>`<a data-ticket-id="${ticket.id}"></a>`, showAdminToast(){},
        fetch:async url=>{
            if (fail) throw Error('offline');
            const before=Number(url.searchParams.get('before_id')||501);
            const tickets=Array.from({length:Math.min(50,before-1)},(_,i)=>({id:before-1-i,updated_at_iso:'fixed'}));
            const last=tickets.at(-1)?.id || 0;
            return {ok:true, status:200, headers:{get:()=>`etag-${before}`},json:async()=>({status:'ok', tickets, has_more:last>1, next_cursor:{id:last,updated_at:'fixed'},open_count:500,closed_count:0})};
        }
    });
    const source = fs.readFileSync('engine/templates/admin_dashboard.html','utf8');
    const start=source.indexOf("        const ticketsList = document.getElementById('tickets-list');");
    const end=source.indexOf('        let nodeTrafficNodesLoaded',start);
    vm.runInContext(source.slice(start,end),context);
    return {context,nodes,setFail:value=>fail=value};
}

test('all 500 tickets remain reachable with only 50 rows on a page; previous works', async () => {
    const {context,nodes}=makePagination();
    const ids=new Set(nodes['tickets-list'].querySelectorAll().map(n=>n.dataset.ticketId));
    for(let page=2;page<=10;page++){
        await vm.runInContext('loadMoreTickets(false)',context);
        const rows=nodes['tickets-list'].querySelectorAll();
        assert.equal(rows.length,50);
        rows.forEach(n=>ids.add(n.dataset.ticketId));
        assert.equal(nodes['tickets-page-status'].textContent,`Страница ${page}`);
    }
    assert.equal(ids.size,500);
    assert.equal(nodes['tickets-load-more'].hidden,true);
    await vm.runInContext('refreshTickets()',context);
    assert.equal(nodes['tickets-list'].querySelectorAll()[0].dataset.ticketId,'50');
    for(let page=9;page>=1;page--) await vm.runInContext('loadMoreTickets(true)',context);
    assert.equal(nodes['tickets-list'].querySelectorAll()[0].dataset.ticketId,'500');
    assert.equal(nodes['tickets-previous'].hidden,true);
});

test('failed pagination does not lose current cursor or disable future attempts', async () => {
    const {context,nodes,setFail}=makePagination();
    setFail(true);
    await vm.runInContext('loadMoreTickets(false)',context);
    assert.equal(vm.runInContext('ticketsCursorHistory.length',context),0);
    assert.equal(nodes['tickets-load-more'].disabled,false);
    setFail(false);
    await vm.runInContext('loadMoreTickets(false)',context);
    assert.equal(nodes['tickets-list'].querySelectorAll()[0].dataset.ticketId,'450');
});
