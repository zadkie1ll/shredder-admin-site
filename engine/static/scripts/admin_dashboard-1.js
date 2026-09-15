// ===== Вкладка «Привлечение» (аналитика привлечения клиентов) =====
    (function () {
        const mainEl = document.querySelector('main.main');
        if (!mainEl || !mainEl.dataset.acquisitionUrl) return;
        // График «Когорты промокодов» в Аналитике переиспользует канвас-рендер
        // этой вкладки (оси, тултипы, ретина, перерисовка при смене темы).
        window.acqDraw = acqDraw;
        const API = mainEl.dataset.acquisitionUrl;
        const SPENDS_API = mainEl.dataset.adSpendsUrl;
        const loaded = {};
        const fmtRub = (v) => Math.round(v).toLocaleString('ru-RU');

        // Значение серии для тултипа: разделители тысяч везде (в том числе для
        // штучных метрик) и явная единица измерения — ₽ или %.
        function acqFmtValue(series, value) {
            if (value == null) return '—';
            if (series.fmt === 'pct') return `${Number(value).toFixed(1)}%`;
            if (series.fmt === 'rub') return `${fmtRub(value)} ₽`;
            return fmtRub(value);
        }


        // --- общий тултип для всех canvas-графиков вкладки ---
        const tipEl = document.createElement('div');
        tipEl.className = 'sales-chart-tooltip acquisition-chart-tooltip';
        tipEl.style.cssText = 'position:fixed;z-index:9999;display:none;pointer-events:none;max-width:min(320px, calc(100vw - 24px));';
        document.body.appendChild(tipEl);

        function acqBindTooltip(canvas) {
            if (canvas.__acqTipBound) return;
            canvas.__acqTipBound = true;
            canvas.addEventListener('mousemove', (ev) => {
                const m = canvas.__acqMeta;
                if (!m || !m.step) return;
                const rect = canvas.getBoundingClientRect();
                const i = Math.floor((ev.clientX - rect.left - m.padL) / m.step);
                if (i < 0 || i >= m.labels.length) {
                    if (canvas.__acqHover != null) { canvas.__acqHover = null; m.render(null); }
                    tipEl.style.display = 'none';
                    return;
                }
                if (canvas.__acqHover !== i) { canvas.__acqHover = i; m.render(i); }
                const rows = m.series
                    .filter((sr) => sr.label && sr.data[i] != null)
                    .map((sr) => `<div class="acq-tooltip-row"><span class="acq-tooltip-dot" style="background:${adminChartSeriesColor(sr)}"></span><span class="acq-tooltip-name">${sr.label}</span><b class="acq-tooltip-value">${acqFmtValue(sr, sr.data[i])}</b></div>`)
                    .join('');
                tipEl.innerHTML = `<div class="acq-tooltip-head">${m.fullLabels ? m.fullLabels[i] : m.labels[i]}</div>${rows}`;
                tipEl.style.display = 'block';
                const tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
                let tx = ev.clientX + 14, ty = ev.clientY - th - 10;
                if (tx + tw > window.innerWidth - 8) tx = ev.clientX - tw - 14;
                if (ty < 8) ty = ev.clientY + 16;
                tipEl.style.left = tx + 'px'; tipEl.style.top = ty + 'px';
            });
            canvas.addEventListener('mouseleave', () => {
                const m = canvas.__acqMeta;
                canvas.__acqHover = null;
                if (m && m.render) m.render(null);
                tipEl.style.display = 'none';
            });
        }

        // Раскладывает легенду по строкам под ширину канвы. Возвращает массив
        // строк, каждая — список {series, x} с уже посчитанной позицией.
        function acqLegendRows(ctx, legend, availableWidth) {
            ctx.font = '12px Manrope, system-ui, sans-serif';
            const rows = [];
            let row = [];
            let x = 6;
            legend.forEach((series) => {
                const itemWidth = 12 + ctx.measureText(series.label).width + 14;
                if (row.length && x + itemWidth > availableWidth) {
                    rows.push(row);
                    row = [];
                    x = 6;
                }
                row.push({series, x});
                x += itemWidth;
            });
            if (row.length) rows.push(row);
            return rows;
        }

        function acqSetupCanvas(canvas) {
            const dpr = window.devicePixelRatio || 1;
            canvas.width = canvas.clientWidth * dpr;
            canvas.height = canvas.clientHeight * dpr;
            const ctx = canvas.getContext('2d');
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            return {ctx, w: canvas.clientWidth, h: canvas.clientHeight};
        }

        // Универсальный график: stacked-бары (левая ось) + линии (правая ось).
        // render(hi) перерисовывает всё; при hi != null подсвечивает колонку:
        // вертикальная направляющая, точки-чекпоинты на линиях, подсветка бара.
        function acqDraw(canvas, labels, barSeries, lineSeries, opts = {}) {
            const meta = {
                labels,
                fullLabels: opts.fullLabels || null,
                series: [...barSeries, ...lineSeries],
            };
            meta.render = function (hi) {
                const {ctx, w, h} = acqSetupCanvas(canvas);
                const n = labels.length;
                // hideLegend: легенду рисует сама вкладка (чипы/подпись), в
                // тултипе подписи серий остаются.
                const legend = opts.hideLegend ? [] : [...barSeries, ...lineSeries].filter((sr) => sr.label);
                // Легенда переносится по строкам: в одну строку она не влезает на
                // узких экранах и раньше просто уезжала за правый край канвы.
                const legendRowH = 18;
                const legendRows = acqLegendRows(ctx, legend, Math.max(80, w - 12));
                const lineOnly = opts.lineOnly && !barSeries.length && lineSeries.length;
                const padL = 54, padR = lineSeries.length && !lineOnly && !opts.sharedAxis ? 54 : 14;
                const padT = 6 + legendRows.length * legendRowH + 6, padB = 26;
                const plotW = w - padL - padR, plotH = Math.max(1, h - padT - padB);
                meta.padL = padL;
                meta.step = n ? plotW / n : 0;
                if (!n) { ctx.fillStyle = adminChartUiColor('empty'); ctx.font = '12px Manrope, system-ui, sans-serif'; ctx.fillText('Нет данных', padL, h / 2); return; }
                const barTotals = labels.map((_, i2) => barSeries.reduce((acc, sr) => acc + (sr.data[i2] || 0), 0));
                const maxBar = Math.max(1, ...barTotals);
                // sharedAxis: линии в тех же единицах, что столбики (скользящее
                // среднее выручки) — одна шкала, правая ось не рисуется.
                const maxLine = opts.sharedAxis
                    ? Math.max(maxBar, ...lineSeries.flatMap((sr) => sr.data.filter((v) => v != null)))
                    : Math.max(1, ...lineSeries.flatMap((sr) => sr.data.filter((v) => v != null)));
                const barFmt = barSeries[0]?.fmt || 'raw';
                const lineFmt = lineSeries[0]?.fmt || 'raw';
                const step = meta.step, barW = Math.max(2, step * 0.62);
                // Подложка диапазона бакетов (например, «прошлое — оценка»):
                // рисуется под сеткой, чтобы линии и подписи оставались сверху.
                if (opts.shade && opts.shade.to > opts.shade.from) {
                    ctx.fillStyle = adminChartUiColor('highlight');
                    ctx.fillRect(padL + opts.shade.from * step, padT, (opts.shade.to - opts.shade.from) * step, plotH);
                }
                if (hi != null && hi >= 0 && hi < n) {
                    ctx.fillStyle = adminChartUiColor('highlight');
                    ctx.fillRect(padL + hi * step, padT, step, plotH);
                }
                ctx.fillStyle = adminChartUiColor('label');
                ctx.font = '11px Manrope, system-ui, sans-serif'; ctx.textAlign = 'right';
                for (let g = 0; g <= 4; g++) {
                    const y = padT + plotH - (plotH * g) / 4;
                    // Нулевая линия заметнее прочих: от неё считываются высоты баров.
                    ctx.strokeStyle = adminChartUiColor(g === 0 ? 'gridStrong' : 'grid');
                    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
                    ctx.fillStyle = adminChartUiColor('label');
                    ctx.fillText(adminChartAxisLabel(((lineOnly || opts.sharedAxis ? maxLine : maxBar) * g) / 4, lineOnly ? lineFmt : barFmt), padL - 6, y + 3);
                    if (lineSeries.length && !lineOnly && !opts.sharedAxis) { ctx.textAlign = 'left'; ctx.fillText(adminChartAxisLabel((maxLine * g) / 4, lineFmt), w - padR + 6, y + 3); ctx.textAlign = 'right'; }
                }
                barSeries.length && labels.forEach((_, i2) => {
                    let y = padT + plotH;
                    const bx = padL + i2 * step + (step - barW) / 2;
                    barSeries.forEach((sr) => {
                        const v = sr.data[i2] || 0;
                        const bh = (v / (opts.sharedAxis ? maxLine : maxBar)) * plotH;
                        ctx.fillStyle = adminChartSeriesColor(sr);
                        ctx.fillRect(bx, y - bh, barW, bh);
                        // Тонкий разрез между сегментами стека: без него соседние
                        // сегменты сливаются в одну полосу.
                        if (barSeries.length > 1 && bh > 1.5 && y < padT + plotH) {
                            ctx.fillStyle = adminChartUiColor('barSeparator');
                            ctx.fillRect(bx, y - 0.5, barW, 1);
                        }
                        y -= bh;
                    });
                });
                // Optional range fill uses the same series and scale as the
                // lines/tooltip. Missing values break the band, never invent data.
                if (opts.rangeBand) {
                    const lower = lineSeries[opts.rangeBand[0]]?.data || [];
                    const upper = lineSeries[opts.rangeBand[1]]?.data || [];
                    let segment = [];
                    const fillSegment = () => {
                        if (segment.length > 1) {
                            ctx.beginPath();
                            segment.forEach((i2, index) => {
                                const x = padL + (i2 + .5) * step;
                                const y = padT + plotH - lower[i2] / maxLine * plotH;
                                if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                            });
                            segment.slice().reverse().forEach((i2) => ctx.lineTo(padL + (i2 + .5) * step, padT + plotH - upper[i2] / maxLine * plotH));
                            ctx.closePath();
                            ctx.fillStyle = adminChartUiColor('highlight');
                            ctx.fill();
                        }
                        segment = [];
                    };
                    labels.forEach((_, i2) => {
                        if (Number.isFinite(lower[i2]) && Number.isFinite(upper[i2])) segment.push(i2);
                        else fillSegment();
                    });
                    fillSegment();
                }
                lineSeries.forEach((sr) => {
                    ctx.strokeStyle = adminChartSeriesColor(sr); ctx.lineWidth = sr.width || 2;
                    if (sr.dash) ctx.setLineDash(sr.dash); else ctx.setLineDash([]);
                    ctx.beginPath();
                    let started = false;
                    sr.data.forEach((v, i2) => {
                        if (v == null) return;
                        const x = padL + i2 * step + step / 2;
                        const y = padT + plotH - (v / maxLine) * plotH;
                        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
                    });
                    ctx.stroke(); ctx.setLineDash([]);
                });
                // Маркер бакета (сплошная вертикаль с подписью сверху) — «сегодня».
                if (opts.marker && opts.marker.index >= 0 && opts.marker.index < n) {
                    const mx = padL + opts.marker.index * step + step / 2;
                    ctx.strokeStyle = adminChartSeriesColor({tone: 'amber'}); ctx.lineWidth = 1.5;
                    ctx.setLineDash([]);
                    ctx.beginPath(); ctx.moveTo(mx, padT); ctx.lineTo(mx, padT + plotH); ctx.stroke();
                    if (opts.marker.label) {
                        ctx.font = '600 11px Manrope, system-ui, sans-serif';
                        const tw = ctx.measureText(opts.marker.label).width;
                        const alignRight = mx + tw + 10 > w - padR;
                        ctx.textAlign = alignRight ? 'right' : 'left';
                        ctx.fillStyle = adminChartSeriesColor({tone: 'amber'});
                        ctx.fillText(opts.marker.label, alignRight ? mx - 6 : mx + 6, padT + 12);
                        ctx.textAlign = 'right';
                    }
                }
                if (hi != null && hi >= 0 && hi < n) {
                    const x = padL + hi * step + step / 2;
                    ctx.strokeStyle = adminChartUiColor('guide'); ctx.lineWidth = 1;
                    ctx.setLineDash([3, 3]);
                    ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, padT + plotH); ctx.stroke();
                    ctx.setLineDash([]);
                    lineSeries.forEach((sr) => {
                        const v = sr.data[hi];
                        if (v == null) return;
                        const y = padT + plotH - (v / maxLine) * plotH;
                        ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2);
                        ctx.fillStyle = adminChartSeriesColor(sr); ctx.fill();
                        ctx.lineWidth = 2; ctx.strokeStyle = adminChartUiColor('pointBorder'); ctx.stroke();
                    });
                }
                ctx.fillStyle = adminChartUiColor('label'); ctx.textAlign = 'center'; ctx.font = '11px Manrope, system-ui, sans-serif';
                // Шаг подписей считается по реальной ширине текста, а не по
                // фиксированному «каждая n/14»: иначе на узком экране даты слипаются.
                const widestLabel = labels.reduce((acc, lb) => Math.max(acc, ctx.measureText(String(lb)).width), 0);
                const every = Math.max(1, Math.ceil((widestLabel + 10) / Math.max(1, step)));
                labels.forEach((lb, i2) => { if (i2 % every === 0) ctx.fillText(lb, padL + i2 * step + step / 2, h - 8); });
                ctx.textAlign = 'left'; ctx.font = '12px Manrope, system-ui, sans-serif';
                legendRows.forEach((row, rowIndex) => {
                    const ly = 6 + rowIndex * legendRowH;
                    row.forEach((item) => {
                        ctx.fillStyle = adminChartSeriesColor(item.series);
                        ctx.fillRect(item.x, ly, 8, 8);
                        ctx.fillStyle = adminChartUiColor('legend');
                        ctx.fillText(item.series.label, item.x + 12, ly + 8);
                    });
                });
            };
            canvas.__acqMeta = meta;
            canvas.__acqHover = null;
            meta.render(null);
            acqBindTooltip(canvas);
        }

        const acqHtml = (html) => ({trustedHtml: String(html)});
        const acqCellHtml = (value) => {
            if (value && typeof value === 'object' && Object.prototype.hasOwnProperty.call(value, 'trustedHtml')) {
                return value.trustedHtml;
            }
            return escapeHtml(value == null ? '' : String(value));
        };

        function acqTable(headers, rows) {
            const th = headers.map((x) => `<th style="text-align:left;padding:6px 10px;color:rgba(255,255,255,.5);font-size:11px;white-space:nowrap;">${escapeHtml(String(x))}</th>`).join('');
            const trs = rows.map((r) => '<tr>' + r.map((c) => `<td style="padding:6px 10px;font-size:12px;border-top:1px solid rgba(255,255,255,.06);white-space:nowrap;">${acqCellHtml(c)}</td>`).join('') + '</tr>').join('');
            return `<table style="border-collapse:collapse;min-width:100%;">${'<thead><tr>' + th + '</tr></thead>'}<tbody>${trs}</tbody></table>`;
        }

        const acqFetchState = new Map();

        async function acqFetch(section, params = {}, options = {}) {
            const previous = acqFetchState.get(section);
            if (previous) previous.controller.abort();
            const controller = new AbortController();
            const generation = (previous?.generation || 0) + 1;
            acqFetchState.set(section, {controller, generation});
            // Таймаут — понятной ошибкой, а не «signal is aborted without reason».
            const timeoutMs = options.timeoutMs || 20000;
            const timeoutId = setTimeout(() => controller.abort(new DOMException(`Сервер не ответил за ${Math.round(timeoutMs / 1000)} с`, 'TimeoutError')), timeoutMs);
            const q = new URLSearchParams({section, ...params});
            try {
                const resp = await fetch(`${API}?${q}`, {
                    headers: {'X-Requested-With': 'XMLHttpRequest'},
                    signal: controller.signal,
                });
                const payload = await resp.json();
                const current = acqFetchState.get(section);
                if (!current || current.generation !== generation) {
                    throw new DOMException('Устаревший ответ', 'AbortError');
                }
                if (!resp.ok || payload.status !== 'ok') throw new Error(payload.message || 'error');
                return payload.result;
            } finally {
                clearTimeout(timeoutId);
                if (acqFetchState.get(section)?.generation === generation) {
                    acqFetchState.delete(section);
                }
            }
        }

        const dayLabel = (iso) => iso.slice(8, 10) + '.' + iso.slice(5, 7);

        // ===== Окончания и продления по тарифам =====
        // Состояние вкладки: последний ответ и скрытые тарифы — перерисовка
        // графика и таблицы без повторного запроса.
        // Пробные скрыты по умолчанию: их в разы больше платных, и на общей
        // шкале линии тарифов прижимаются к нулю. Чип включает их обратно.
        const expiryState = {res: null, hidden: new Set(['trial']), group: 'day'};
        const EXPIRY_TONES = {trial: 'slate', oneday: 'sky', threedays: 'pink', oneweek: 'orange', month: 'amber', threemonths: 'indigo', sixmonths: 'violet', year: 'green', other: 'slate'};
        const expiryTone = (key) => EXPIRY_TONES[key] || 'slate';
        const expiryPct = (part, whole) => whole ? `${Math.round(100 * part / whole)}%` : '—';

        function expiryIsoShift(days) {
            const d = new Date(Date.now() + 3 * 60 * 60 * 1000);
            d.setUTCDate(d.getUTCDate() + days);
            return d.toISOString().slice(0, 10);
        }

        function setExpiryPreset(name) {
            const mskNow = new Date(Date.now() + 3 * 60 * 60 * 1000);
            let start, end;
            if (name === 'next14') { start = expiryIsoShift(0); end = expiryIsoShift(14); }
            else if (name === 'month') {
                const y = mskNow.getUTCFullYear(), m = mskNow.getUTCMonth();
                start = `${y}-${String(m + 1).padStart(2, '0')}-01`;
                end = new Date(Date.UTC(y, m + 1, 0)).toISOString().slice(0, 10);
            }
            else if (name === 'quarter') { start = expiryIsoShift(0); end = expiryIsoShift(91); }
            // По умолчанию 60 дней назад: при окне продления 30 дней у первого
            // месяца окно уже закрыто и доля продлений — финальная.
            else { start = expiryIsoShift(-60); end = expiryIsoShift(30); }
            setAcqDateField('acq-expiry-start', start);
            setAcqDateField('acq-expiry-end', end);
            document.querySelectorAll('[data-expiry-preset]').forEach((b) => b.classList.toggle('active', b.dataset.expiryPreset === name));
        }

        function expiryBucketLabel(row, group) {
            if (group !== 'week') return row.label;
            const [y, m, d] = row.key.split('-').map(Number);
            const endDay = new Date(Date.UTC(y, m - 1, d + 6));
            return `${row.label}–${String(endDay.getUTCDate()).padStart(2, '0')}.${String(endDay.getUTCMonth() + 1).padStart(2, '0')}`;
        }

        function renderExpiryChips(res) {
            const box = document.getElementById('acq-expiry-tariffs');
            if (!box) return;
            box.innerHTML = res.tariffs.map((t) => {
                const total = res.totals.by_tariff[t.key] || {ending: 0};
                const on = !expiryState.hidden.has(t.key);
                return `<button type="button" class="acq-expiry-chip" data-expiry-tariff="${escapeHtml(t.key)}" aria-pressed="${on}"><span class="acq-expiry-chip-dot" style="background:${chartTone(expiryTone(t.key))}"></span>${escapeHtml(t.label)}<b>${fmtRub(total.ending)}</b></button>`;
            }).join('');
            box.querySelectorAll('[data-expiry-tariff]').forEach((btn) => btn.addEventListener('click', () => {
                const key = btn.dataset.expiryTariff;
                if (expiryState.hidden.has(key)) expiryState.hidden.delete(key); else expiryState.hidden.add(key);
                btn.setAttribute('aria-pressed', String(!expiryState.hidden.has(key)));
                renderExpiryCards(expiryState.res);
                renderExpiryChart(expiryState.res);
                renderExpiryTransitions(expiryState.res);
                renderExpiryTable(expiryState.res);
            }));
        }

        // Плитки считаются на клиенте по видимым тарифам — они обязаны
        // совпадать с графиком и таблицей, иначе скрытые пробные (их в разы
        // больше платных) делают итоги нечитаемыми.
        function expiryVisibleTotals(res) {
            const visible = res.tariffs.filter((t) => !expiryState.hidden.has(t.key)).map((t) => t.key);
            const t = {ending: 0, endingPast: 0, endingFuture: 0, renewed: 0, pending: 0, autopayFuture: 0, endingClosed: 0, renewedClosed: 0, subscriptions: 0};
            visible.forEach((key) => { t.subscriptions += (res.totals.by_tariff[key] || {}).subscriptions || 0; });
            res.buckets.forEach((row) => {
                const sum = (bag) => visible.reduce((acc, key) => acc + (bag[key] || 0), 0);
                const ending = sum(row.ending), renewed = sum(row.renewed);
                t.ending += ending;
                t.renewed += renewed;
                t.pending += sum(row.pending);
                if (row.is_past || row.is_current) t.endingPast += ending; else { t.endingFuture += ending; t.autopayFuture += sum(row.autopay); }
                if (row.window_closed) { t.endingClosed += ending; t.renewedClosed += renewed; }
            });
            return t;
        }

        function renderExpiryCards(res) {
            const box = document.getElementById('acq-expiry-cards');
            if (!box || !res) return;
            const t = expiryVisibleTotals(res);
            const card = (label, value, meta) => `<div><div>${label}</div><div>${value}</div>${meta ? `<small>${meta}</small>` : ''}</div>`;
            const openRenewed = t.renewed - t.renewedClosed;
            box.innerHTML = [
                card('Окончаний за период', fmtRub(t.ending), `у ${fmtRub(t.subscriptions)} подписок · ${fmtRub(t.endingFuture)} впереди · ${fmtRub(t.endingPast)} уже прошли`),
                card('Продлились', t.endingClosed ? `${fmtRub(t.renewedClosed)}<span class="acq-expiry-card-pct">${expiryPct(t.renewedClosed, t.endingClosed)}</span>` : '—', t.endingClosed ? `из ${fmtRub(t.endingClosed)} с закрытым окном ${res.window_days} дн.${openRenewed ? ` · ещё ${fmtRub(openRenewed)} в открытом окне` : ''}` : `нет дней с закрытым окном ${res.window_days} дн. — расширьте период влево`),
                card('Ещё могут продлиться', fmtRub(t.pending), 'закончились недавно, окно ещё открыто'),
                card('С автоплатежом впереди', fmtRub(t.autopayFuture), `${expiryPct(t.autopayFuture, t.endingFuture)} будущих окончаний`),
            ].join('');
        }

        function renderExpiryChart(res) {
            const canvas = document.getElementById('acq-expiry-chart');
            if (!canvas || !res) return;
            const rows = res.buckets;
            const visible = res.tariffs.filter((t) => !expiryState.hidden.has(t.key));
            const todayIndex = rows.findIndex((r) => r.is_current);
            const pastEnd = rows.filter((r) => r.is_past).length;
            const lines = [];
            visible.forEach((t) => {
                lines.push({label: `${t.label}: заканчиваются`, tone: expiryTone(t.key), data: rows.map((r) => r.ending[t.key] || 0), fmt: 'raw', width: 2});
                lines.push({label: `${t.label}: продлились`, tone: expiryTone(t.key), dash: [5, 4], width: 1.5, fmt: 'raw',
                    data: rows.map((r) => (r.is_past || r.is_current) ? (r.renewed[t.key] || 0) : null)});
            });
            acqDraw(canvas, rows.map((r) => r.label), [], lines, {
                lineOnly: true,
                hideLegend: true,
                fullLabels: rows.map((r) => `${expiryBucketLabel(r, res.group)}${r.is_current ? ' · сегодня' : r.is_past ? ' · оценка' : ''}`),
                marker: todayIndex >= 0 ? {index: todayIndex, label: 'сегодня'} : null,
                shade: {from: 0, to: pastEnd},
            });
            const summary = document.getElementById('acq-expiry-summary');
            if (summary) {
                const t = expiryVisibleTotals(res);
                const groupWord = res.group === 'week' ? 'неделям' : 'дням';
                summary.innerHTML = rows.length
                    ? `По ${groupWord}, ${res.start.slice(8, 10)}.${res.start.slice(5, 7)}.${res.start.slice(0, 4)} — ${res.end.slice(8, 10)}.${res.end.slice(5, 7)}.${res.end.slice(0, 4)}, по выбранным тарифам. Левее «сегодня» — оценка по цепочке оплат, правее — реальные сроки подписок. Считаются <b>окончания периодов</b>: подписка на 1 день за месяц даёт до 30 окончаний, поэтому окончаний больше, чем подписок (<b>${fmtRub(t.subscriptions)}</b>). Всего окончаний <b>${fmtRub(t.ending)}</b>, из уже прошедших продлилось <b>${fmtRub(t.renewed)}</b>. Данные пересобираются раз в ${Math.round((res.cache_ttl || 300) / 60)} мин.`
                    : 'Нет данных за выбранный период.';
            }
        }

        function renderExpiryTable(res) {
            const box = document.getElementById('acq-expiry-table');
            if (!box || !res) return;
            const visible = res.tariffs.filter((t) => !expiryState.hidden.has(t.key));
            const rows = res.buckets;
            const cell = (row, key) => {
                const ending = row.ending[key] || 0;
                if (!ending) return '<td class="is-empty">—</td>';
                if (row.is_past || row.is_current) {
                    const renewed = row.renewed[key] || 0, pending = row.pending[key] || 0;
                    // Доля — только когда окно закрыто; иначе она занижена.
                    const meta = row.window_closed ? expiryPct(renewed, ending) : (pending ? `ещё ${fmtRub(pending)} в окне` : 'окно открыто');
                    return `<td><span class="acq-expiry-cell-end">${fmtRub(ending)}</span><span class="acq-expiry-cell-arrow" aria-hidden="true">→</span><span class="acq-expiry-cell-ren">${fmtRub(renewed)}</span><small>${meta}</small></td>`;
                }
                const autopay = row.autopay[key] || 0;
                return `<td><span class="acq-expiry-cell-end">${fmtRub(ending)}</span><small>${autopay ? `автоплатёж ${fmtRub(autopay)}` : 'без автоплатежа'}</small></td>`;
            };
            const totalCell = (row) => {
                if (!row.total_ending) return '<td class="is-empty">—</td>';
                if (row.is_past || row.is_current) {
                    const meta = row.window_closed ? expiryPct(row.total_renewed, row.total_ending) : (row.total_pending ? `ещё ${fmtRub(row.total_pending)} в окне` : 'окно открыто');
                    return `<td><span class="acq-expiry-cell-end">${fmtRub(row.total_ending)}</span><span class="acq-expiry-cell-arrow" aria-hidden="true">→</span><span class="acq-expiry-cell-ren">${fmtRub(row.total_renewed)}</span><small>${meta}</small></td>`;
                }
                return `<td><span class="acq-expiry-cell-end">${fmtRub(row.total_ending)}</span><small>${row.total_autopay ? `автоплатёж ${fmtRub(row.total_autopay)}` : ''}</small></td>`;
            };
            const head = `<tr><th scope="col">${res.group === 'week' ? 'Неделя' : 'День'}</th>${visible.map((t) => `<th scope="col"><span class="acq-expiry-chip-dot" style="background:${chartTone(expiryTone(t.key))}"></span>${escapeHtml(t.label)}</th>`).join('')}<th scope="col">Всего</th></tr>`;
            const body = rows.map((r) => `<tr class="${r.is_current ? 'is-today' : r.is_past ? 'is-past' : 'is-future'}"><th scope="row">${expiryBucketLabel(r, res.group)}${r.is_current ? '<small>сегодня</small>' : r.is_past && !r.window_closed ? '<small class="is-open">окно открыто</small>' : ''}</th>${visible.map((t) => cell(r, t.key)).join('')}${totalCell(r)}</tr>`).join('');
            const by = res.totals.by_tariff;
            const foot = `<tr><th scope="row">Итого</th>${visible.map((t) => { const x = by[t.key] || {ending: 0, renewed: 0, pending: 0, autopay: 0}; return `<td><span class="acq-expiry-cell-end">${fmtRub(x.ending)}</span><span class="acq-expiry-cell-arrow" aria-hidden="true">→</span><span class="acq-expiry-cell-ren">${fmtRub(x.renewed)}</span><small>${x.autopay ? `автоплатёж ${fmtRub(x.autopay)}` : ''}</small></td>`; }).join('')}<td><span class="acq-expiry-cell-end">${fmtRub(res.totals.ending)}</span><span class="acq-expiry-cell-arrow" aria-hidden="true">→</span><span class="acq-expiry-cell-ren">${fmtRub(res.totals.renewed)}</span></td></tr>`;
            box.innerHTML = rows.length
                ? `<table class="acq-expiry-table"><thead>${head}</thead><tbody>${body}</tbody><tfoot>${foot}</tfoot></table>`
                : '<p class="acq-ads-summary-note">Нет данных за выбранный период.</p>';
        }

        // Матрица переходов: строки — тариф закончившегося периода (только
        // видимые чипами), колонки — тариф следующей оплаты (все, куда
        // кто-то ушёл) + «не продлились». Диагональ выделена: остались на своём.
        function renderExpiryTransitions(res) {
            const box = document.getElementById('acq-expiry-transitions');
            if (!box || !res) return;
            const transitions = res.totals.transitions || {};
            const churned = res.totals.churned || {};
            const labelOf = Object.fromEntries(res.tariffs.map((t) => [t.key, t.label]));
            const order = res.tariffs.map((t) => t.key);
            const fromKeys = order.filter((k) => !expiryState.hidden.has(k) && ((transitions[k] && Object.keys(transitions[k]).length) || churned[k]));
            const toSet = new Set();
            fromKeys.forEach((k) => Object.keys(transitions[k] || {}).forEach((to) => toSet.add(to)));
            const toKeys = [...order.filter((k) => toSet.has(k)), ...[...toSet].filter((k) => !order.includes(k))];
            if (!fromKeys.length) {
                box.innerHTML = '<p class="acq-ads-summary-note">Пока нет дней с закрытым окном продления по выбранным тарифам — расширьте период влево.</p>';
                return;
            }
            const dot = (key) => `<span class="acq-expiry-chip-dot" style="background:${chartTone(expiryTone(key))}"></span>`;
            const head = `<tr><th scope="col">Закончился</th>${toKeys.map((k) => `<th scope="col">${dot(k)}→ ${escapeHtml(labelOf[k] || k)}</th>`).join('')}<th scope="col">Не продлились</th><th scope="col">Всего</th></tr>`;
            const body = fromKeys.map((from) => {
                const row = transitions[from] || {};
                const renewed = Object.values(row).reduce((a, b) => a + b, 0);
                const lost = churned[from] || 0;
                const total = renewed + lost;
                const cells = toKeys.map((to) => {
                    const n = row[to] || 0;
                    if (!n) return '<td class="is-empty">—</td>';
                    return `<td class="${to === from ? 'is-same' : 'is-move'}"><span class="acq-expiry-cell-end">${fmtRub(n)}</span><small>${expiryPct(n, total)}</small></td>`;
                }).join('');
                return `<tr><th scope="row">${dot(from)}${escapeHtml(labelOf[from] || from)}</th>${cells}<td class="is-lost"><span class="acq-expiry-cell-end">${fmtRub(lost)}</span><small>${expiryPct(lost, total)}</small></td><td><span class="acq-expiry-cell-end">${fmtRub(total)}</span><small>${expiryPct(renewed, total)} продлились</small></td></tr>`;
            }).join('');
            box.innerHTML = `<table class="acq-expiry-table acq-expiry-matrix"><thead>${head}</thead><tbody>${body}</tbody></table>`;
        }

        async function loadExpiry() {
            const startEl = document.getElementById('acq-expiry-start');
            const endEl = document.getElementById('acq-expiry-end');
            if (!startEl || !endEl) return;
            if (!startEl.value || !endEl.value) setExpiryPreset('around30');
            const body = document.getElementById('acq-expiry-body');
            body?.classList.add('is-loading');
            // Явный индикатор: первая сборка на большой базе идёт десятки
            // секунд, и без крутилки пустой блок читается как «сломалось».
            const loading = document.getElementById('acq-expiry-loading');
            const loadingText = document.getElementById('acq-expiry-loading-text');
            if (loading) {
                loading.hidden = false;
                if (loadingText) loadingText.textContent = expiryState.res
                    ? 'Пересчитываем по новому периоду…'
                    : 'Собираем периоды по всем оплатам — первый раз это может занять до минуты, дальше быстро.';
            }
            if (!expiryState.res) {
                const table = document.getElementById('acq-expiry-table');
                if (table) table.innerHTML = '';
            }
            try {
                // Первая сборка на большой базе может занять десятки секунд
                // (дальше отдаётся из кэша сервера) — ждём дольше обычного.
                const res = await acqFetch('expirations', {
                    start: startEl.value,
                    end: endEl.value,
                    group: expiryState.group,
                    window: document.getElementById('acq-expiry-window')?.value || '30',
                }, {timeoutMs: 120000});
                expiryState.res = res;
                // Скрытые тарифы, которых больше нет в ответе, забываем.
                expiryState.hidden = new Set([...expiryState.hidden].filter((k) => res.tariffs.some((t) => t.key === k)));
                renderExpiryCards(res);
                renderExpiryChips(res);
                renderExpiryChart(res);
                renderExpiryTransitions(res);
                renderExpiryTable(res);
            } catch (error) {
                // Запрос вытеснен более новым (сменили шаг/период) — не ошибка,
                // новый сам отрисует всё.
                if (error?.name === 'AbortError') return;
                const table = document.getElementById('acq-expiry-table');
                if (table) table.innerHTML = `<p class="acq-ads-summary-note is-error">Не удалось загрузить данные: ${escapeHtml(error.message || String(error))}</p>`;
                throw error;
            } finally {
                body?.classList.remove('is-loading');
                const loadingEl = document.getElementById('acq-expiry-loading');
                if (loadingEl) loadingEl.hidden = true;
            }
        }

        // ===== Выручка по дням =====
        const revenueState = {res: null, weekly: null, mode: 'tariff'};
        const REVENUE_MODE_LABELS = {tariff: 'по тарифам', newrep: 'новые и повторные', autopay: 'автоплатёж и вручную', provider: 'ЮKassa и Wata'};
        const WEEKDAYS_SHORT = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
        const fullDay = (iso) => dayLabel(iso) + '.' + iso.slice(0, 4);
        const deltaHtml = (current, previous) => {
            if (previous == null || current == null) return '<span class="acq-delta is-flat">—</span>';
            if (!previous) return current ? '<span class="acq-delta is-up">новое</span>' : '<span class="acq-delta is-flat">—</span>';
            const pct = Math.round(100 * (current - previous) / previous);
            const cls = pct > 0 ? 'is-up' : pct < 0 ? 'is-down' : 'is-flat';
            return `<span class="acq-delta ${cls}">${pct > 0 ? '+' : ''}${pct}%</span>`;
        };

        function setRevenuePreset(name) {
            const mskNow = new Date(Date.now() + 3 * 60 * 60 * 1000);
            const y = mskNow.getUTCFullYear(), m = mskNow.getUTCMonth();
            let start, end;
            if (name === 'month') { start = `${y}-${String(m + 1).padStart(2, '0')}-01`; end = expiryIsoShift(0); }
            else if (name === 'prev_month') {
                start = new Date(Date.UTC(y, m - 1, 1)).toISOString().slice(0, 10);
                end = new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10);
            }
            else { const days = Number(name) || 45; start = expiryIsoShift(-(days - 1)); end = expiryIsoShift(0); }
            setAcqDateField('acq-revenue-start', start);
            setAcqDateField('acq-revenue-end', end);
            document.querySelectorAll('[data-revenue-preset]').forEach((b) => b.classList.toggle('active', b.dataset.revenuePreset === name));
        }

        // Плитки: сегодня/вчера/7/30 дней с дельтой к сопоставимому периоду —
        // считаются по своему запросу за 61 день, чтобы не зависеть от
        // выбранного на графике диапазона.
        function renderRevenueKpis(res) {
            const box = document.getElementById('acq-revenue-kpis');
            if (!box || !res) return;
            const days = res.days;
            const byDay = Object.fromEntries(days.map((d) => [d.day, d]));
            const todayIso = res.today;
            const shift = (iso, n) => { const d = new Date(`${iso}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
            const sum = (from, to, field = 'revenue') => days.filter((d) => d.day >= from && d.day <= to).reduce((a, d) => a + (d[field] || 0), 0);
            const today = byDay[todayIso] || {revenue: 0, payments: 0};
            const yesterday = byDay[shift(todayIso, -1)] || {revenue: 0, payments: 0};
            const weekAgo = byDay[shift(todayIso, -7)] || {revenue: 0};
            const last7 = sum(shift(todayIso, -6), todayIso), prev7 = sum(shift(todayIso, -13), shift(todayIso, -7));
            const last30 = sum(shift(todayIso, -29), todayIso), prev30 = sum(shift(todayIso, -59), shift(todayIso, -30));
            const pays30 = sum(shift(todayIso, -29), todayIso, 'payments'), paysPrev30 = sum(shift(todayIso, -59), shift(todayIso, -30), 'payments');
            const check30 = pays30 ? Math.round(last30 / pays30) : null, checkPrev30 = paysPrev30 ? Math.round(prev30 / paysPrev30) : null;
            const auto30 = sum(shift(todayIso, -29), todayIso, 'autopay_rub');
            const card = (label, value, delta, meta) => `<div><div>${label}</div><div>${value}${delta}</div><small>${meta}</small></div>`;
            box.innerHTML = [
                card('Сегодня', `${fmtRub(today.revenue)} ₽`, deltaHtml(today.revenue, yesterday.revenue), `${fmtRub(today.payments)} оплат · день ещё идёт`),
                card('Вчера', `${fmtRub(yesterday.revenue)} ₽`, deltaHtml(yesterday.revenue, weekAgo.revenue), `${fmtRub(yesterday.payments)} оплат · к тому же дню неделей раньше`),
                card('7 дней', `${fmtRub(last7)} ₽`, deltaHtml(last7, prev7), `к предыдущим 7 дням (${fmtRub(prev7)} ₽)`),
                card('30 дней', `${fmtRub(last30)} ₽`, deltaHtml(last30, prev30), `к предыдущим 30 дням (${fmtRub(prev30)} ₽)`),
                card('Средний чек, 30 дн.', check30 == null ? '—' : `${fmtRub(check30)} ₽`, deltaHtml(check30, checkPrev30), `${fmtRub(pays30)} оплат за 30 дней`),
                card('Автоплатёж, 30 дн.', `${fmtRub(auto30)} ₽`, `<span class="acq-delta is-flat">${last30 ? Math.round(100 * auto30 / last30) : 0}%</span>`, 'доля выручки со списаний ЮKassa'),
            ].join('');
        }

        function revenueSeries(res) {
            const days = res.days;
            const mode = revenueState.mode;
            if (mode === 'newrep') return [
                {label: 'Повторные, ₽', tone: 'indigo', data: days.map((d) => d.repeat_rub), fmt: 'rub'},
                {label: 'Новые, ₽', tone: 'amber', data: days.map((d) => d.new_rub), fmt: 'rub'},
            ];
            if (mode === 'autopay') return [
                {label: 'Автоплатёж, ₽', tone: 'violet', data: days.map((d) => d.autopay_rub), fmt: 'rub'},
                {label: 'Вручную, ₽', tone: 'amber', data: days.map((d) => d.manual_rub), fmt: 'rub'},
            ];
            if (mode === 'provider') return [
                {label: 'ЮKassa, ₽', tone: 'sky', data: days.map((d) => d.yk_rub), fmt: 'rub'},
                {label: 'Wata, ₽', tone: 'orange', data: days.map((d) => d.wata_rub), fmt: 'rub'},
            ];
            return res.tariffs.filter((t) => t.key !== 'trial').map((t) => ({
                label: `${t.label}, ₽`, tone: expiryTone(t.key), fmt: 'rub',
                data: days.map((d) => (d.by_tariff[t.key] || {}).rub || 0),
            }));
        }

        function renderRevenueChart(res) {
            const canvas = document.getElementById('acq-revenue-chart');
            if (!canvas || !res) return;
            const days = res.days;
            const ma = days.map((_, i) => {
                if (i < 6) return null;
                let acc = 0; for (let k = i - 6; k <= i; k++) acc += days[k].revenue;
                return Math.round(acc / 7);
            });
            acqDraw(canvas, days.map((d) => d.label), revenueSeries(res),
                [{label: 'Среднее за 7 дней, ₽', tone: 'neutral', width: 1.5, dash: [4, 4], data: ma, fmt: 'rub'}],
                {sharedAxis: true, fullLabels: days.map((d) => `${fullDay(d.day)}, ${WEEKDAYS_SHORT[d.weekday]}${d.day === res.today ? ' · сегодня' : ''}`)});
            const total = days.reduce((a, d) => a + d.revenue, 0);
            const newRub = days.reduce((a, d) => a + d.new_rub, 0);
            const pays = days.reduce((a, d) => a + d.payments, 0);
            const best = days.reduce((a, d) => (d.revenue > (a?.revenue || 0) ? d : a), null);
            const summary = document.getElementById('acq-revenue-summary');
            if (summary) summary.innerHTML = days.length
                ? `${fullDay(res.start)} — ${fullDay(res.end)}, ${REVENUE_MODE_LABELS[revenueState.mode]}: всего <b class="chart-tone-strong">${fmtRub(total)} ₽</b> за <b>${fmtRub(pays)}</b> оплат (чек <b>${pays ? fmtRub(total / pays) : '—'} ₽</b>), новые <b class="chart-tone-amber">${fmtRub(newRub)} ₽</b> (${total ? Math.round(100 * newRub / total) : 0}%), повторные <b class="chart-tone-indigo">${fmtRub(total - newRub)} ₽</b>.${best ? ` Лучший день — <b class="chart-tone-green">${fullDay(best.day)}</b>: ${fmtRub(best.revenue)} ₽.` : ''} Пунктир — среднее за 7 дней. Данные пересобираются раз в ${Math.round((res.cache_ttl || 300) / 60)} мин.`
                : 'Нет оплат за выбранный период.';
        }

        function renderRevenueTable(res) {
            const box = document.getElementById('acq-revenue-table');
            if (!box || !res) return;
            const days = res.days;
            const byDay = Object.fromEntries(days.map((d) => [d.day, d]));
            const tariffs = res.tariffs.filter((t) => t.key !== 'trial');
            const shift = (iso, n) => { const d = new Date(`${iso}T00:00:00Z`); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
            const head = `<tr><th scope="col">День</th><th scope="col">Выручка</th><th scope="col">Δ неделя</th><th scope="col">Оплат</th><th scope="col">Чек</th><th scope="col">Новые</th><th scope="col">Повторные</th><th scope="col">Автоплатёж</th><th scope="col">ЮKassa / Wata</th>${tariffs.map((t) => `<th scope="col"><span class="acq-expiry-chip-dot" style="background:${chartTone(expiryTone(t.key))}"></span>${escapeHtml(t.label)}</th>`).join('')}</tr>`;
            const cell = (main, sub = '') => `<td><span class="acq-expiry-cell-end">${main}</span>${sub ? `<small>${sub}</small>` : ''}</td>`;
            const body = days.slice().reverse().map((d) => {
                const weekAgo = byDay[shift(d.day, -7)];
                const isToday = d.day === res.today;
                const isWeekend = d.weekday >= 5;
                return `<tr class="${isToday ? 'is-today' : ''}${isWeekend ? ' is-weekend' : ''}"><th scope="row">${d.label}<small>${WEEKDAYS_SHORT[d.weekday]}${isToday ? ' · сегодня' : ''}</small></th>`
                    + cell(`${fmtRub(d.revenue)} ₽`)
                    + `<td>${deltaHtml(d.revenue, weekAgo ? weekAgo.revenue : null)}</td>`
                    + cell(fmtRub(d.payments))
                    + cell(d.avg_check == null ? '—' : `${fmtRub(d.avg_check)} ₽`)
                    + cell(`${fmtRub(d.new_rub)} ₽`, `${fmtRub(d.new_payers)} чел.`)
                    + cell(`${fmtRub(d.repeat_rub)} ₽`, `${fmtRub(d.repeat_payers)} чел.`)
                    + cell(`${fmtRub(d.autopay_rub)} ₽`, `${fmtRub(d.autopay_count)} шт. · ${d.revenue ? Math.round(100 * d.autopay_rub / d.revenue) : 0}%`)
                    + cell(`${fmtRub(d.yk_rub)} / ${fmtRub(d.wata_rub)}`)
                    + tariffs.map((t) => { const x = d.by_tariff[t.key]; return x ? cell(`${fmtRub(x.count)} · ${fmtRub(x.rub)} ₽`) : '<td class="is-empty">—</td>'; }).join('')
                    + '</tr>';
            }).join('');
            const total = (f) => days.reduce((a, d) => a + (d[f] || 0), 0);
            const tTotal = (key, f) => days.reduce((a, d) => a + ((d.by_tariff[key] || {})[f] || 0), 0);
            const foot = `<tr><th scope="row">Итого</th>${cell(`${fmtRub(total('revenue'))} ₽`)}<td></td>${cell(fmtRub(total('payments')))}${cell(total('payments') ? `${fmtRub(total('revenue') / total('payments'))} ₽` : '—')}${cell(`${fmtRub(total('new_rub'))} ₽`, `${fmtRub(total('new_payers'))} чел.`)}${cell(`${fmtRub(total('repeat_rub'))} ₽`, `${fmtRub(total('repeat_payers'))} чел.`)}${cell(`${fmtRub(total('autopay_rub'))} ₽`, `${fmtRub(total('autopay_count'))} шт.`)}${cell(`${fmtRub(total('yk_rub'))} / ${fmtRub(total('wata_rub'))}`)}${tariffs.map((t) => cell(`${fmtRub(tTotal(t.key, 'count'))} · ${fmtRub(tTotal(t.key, 'rub'))} ₽`)).join('')}</tr>`;
            box.innerHTML = days.length ? `<table class="acq-expiry-table acq-revenue-table"><thead>${head}</thead><tbody>${body}</tbody><tfoot>${foot}</tfoot></table>` : '<p class="acq-ads-summary-note">Нет оплат за выбранный период.</p>';
        }

        // Недельный блок «почему выручка такая»: новые/повторные столбиками,
        // доля продлений (единое определение: оплата ≤ 30 дней после конца
        // периода) линией, таблица с чеком и активной базой.
        function renderWeekly(res) {
            const canvas = document.getElementById('acq-weekly-chart');
            const box = document.getElementById('acq-weekly-table');
            if (!canvas || !box || !res) return;
            const wk = res.weeks;
            acqDraw(canvas, wk.map((w) => w.label),
                [
                    {label: 'Повторные, ₽', tone: 'indigo', data: wk.map((w) => w.revenue_repeat), fmt: 'rub'},
                    {label: 'Новые, ₽', tone: 'amber', data: wk.map((w) => w.revenue_new), fmt: 'rub'},
                ],
                [{label: `Продлились, % (окно ${res.window_days} дн., правая ось)`, tone: 'green', data: wk.map((w) => w.renewal_pct), fmt: 'pct'}],
                {fullLabels: wk.map((w) => `неделя с ${fullDay(w.week)}${w.is_current ? ' · текущая' : ''}${w.renewal_pct == null ? ' · окно продления ещё не закрыто' : ''}`)});
            const head = '<tr><th scope="col">Неделя</th><th scope="col">Выручка</th><th scope="col">Δ</th><th scope="col">Новые</th><th scope="col">Повторные</th><th scope="col">Новых покупателей</th><th scope="col">Оплат</th><th scope="col">Чек</th><th scope="col">Продлились</th><th scope="col">Платная база</th></tr>';
            const cell = (main, sub = '') => `<td><span class="acq-expiry-cell-end">${main}</span>${sub ? `<small>${sub}</small>` : ''}</td>`;
            const body = wk.slice().reverse().map((w, i, arr) => {
                const prev = arr[i + 1];
                return `<tr class="${w.is_current ? 'is-today' : ''}"><th scope="row">${w.label}${w.is_current ? '<small>текущая</small>' : ''}</th>`
                    + cell(`${fmtRub(w.revenue)} ₽`) + `<td>${deltaHtml(w.revenue, prev ? prev.revenue : null)}</td>`
                    + cell(`${fmtRub(w.revenue_new)} ₽`, `${w.revenue ? Math.round(100 * w.revenue_new / w.revenue) : 0}%`)
                    + cell(`${fmtRub(w.revenue_repeat)} ₽`, `${w.revenue ? Math.round(100 * w.revenue_repeat / w.revenue) : 0}%`)
                    + cell(fmtRub(w.new_payers)) + cell(fmtRub(w.payments))
                    + cell(w.avg_check == null ? '—' : `${fmtRub(w.avg_check)} ₽`)
                    + (w.renewal_pct == null ? '<td class="is-empty">окно открыто</td>' : cell(`${w.renewal_pct}%`, `${fmtRub(w.renewed_closed)} из ${fmtRub(w.ending_closed)}`))
                    + cell(fmtRub(w.base), 'на конец недели')
                    + '</tr>';
            }).join('');
            box.innerHTML = `<table class="acq-expiry-table"><thead>${head}</thead><tbody>${body}</tbody></table>`;
            const closed = wk.filter((w) => w.renewal_pct != null);
            const last = closed[closed.length - 1];
            const summary = document.getElementById('acq-weekly-summary');
            if (summary) summary.innerHTML = last
                ? `Последняя неделя с закрытым окном — <b>${fullDay(last.week)}</b>: закончилось <b>${fmtRub(last.ending_closed)}</b> платных периодов, продлилось <b class="chart-tone-green">${last.renewal_pct}%</b>, отвал <b class="chart-tone-pink">${(100 - last.renewal_pct).toFixed(1)}%</b>; платная база на конец недели — <b>${fmtRub(last.base)}</b>.`
                : 'Ещё нет недель с закрытым окном продления.';
        }

        async function loadRevenue() {
            const startEl = document.getElementById('acq-revenue-start');
            const endEl = document.getElementById('acq-revenue-end');
            if (!startEl || !endEl) return;
            if (!startEl.value || !endEl.value) setRevenuePreset('45');
            const body = document.getElementById('acq-revenue-body');
            const loading = document.getElementById('acq-revenue-loading');
            const loadingText = document.getElementById('acq-revenue-loading-text');
            body?.classList.add('is-loading');
            if (loading) { loading.hidden = false; if (loadingText) loadingText.textContent = revenueState.res ? 'Пересчитываем…' : 'Собираем оплаты и периоды — первый раз до минуты, дальше быстро.'; }
            try {
                const [res, kpiRes, weekly] = await Promise.all([
                    acqFetch('revenue_days', {start: startEl.value, end: endEl.value}, {timeoutMs: 120000}),
                    acqFetch('revenue_kpis', {start: expiryIsoShift(-60), end: expiryIsoShift(0)}, {timeoutMs: 120000}),
                    acqFetch('summary', {weeks: 12}, {timeoutMs: 120000}),
                ]);
                revenueState.res = res;
                revenueState.weekly = weekly;
                renderRevenueKpis(kpiRes);
                renderRevenueChart(res);
                renderRevenueTable(res);
                renderWeekly(weekly);
            } catch (error) {
                if (error?.name === 'AbortError') return;
                const table = document.getElementById('acq-revenue-table');
                if (table) table.innerHTML = `<p class="acq-ads-summary-note is-error">Не удалось загрузить данные: ${escapeHtml(error.message || String(error))}</p>`;
                throw error;
            } finally {
                body?.classList.remove('is-loading');
                if (loading) loading.hidden = true;
            }
        }

        async function loadNewRepeat() {
            const start = document.getElementById('acq-newrep-start').value;
            const end = document.getElementById('acq-newrep-end').value;
            const params = (start && end)
                ? {start, end}
                : {days: document.getElementById('acq-newrep-days').value};
            const res = await acqFetch('new_repeat', params);
            const rows = res.days;
            acqDraw(document.getElementById('acq-newrep-chart'),
                rows.map((r) => dayLabel(r.day)),
                [
                    {label: 'Повторные, ₽', tone: 'indigo', data: rows.map((r) => r.repeat_rub), fmt: 'rub'},
                    {label: 'Новые, ₽', tone: 'amber', data: rows.map((r) => r.new_rub), fmt: 'rub'},
                ],
                [{label: 'Новых покупателей (правая ось)', tone: 'green', data: rows.map((r) => r.new_payers), fmt: 'raw'}],
                {fullLabels: rows.map((r) => r.day)});
            const nSum = rows.reduce((a, r) => a + r.new_rub, 0), rSum = rows.reduce((a, r) => a + r.repeat_rub, 0);
            const nPayers = rows.reduce((a, r) => a + r.new_payers, 0);
            const fullDay = (iso) => dayLabel(iso) + '.' + iso.slice(0, 4);
            document.getElementById('acq-newrep-summary').innerHTML = rows.length
                ? `За период <b>${fullDay(rows[0].day)} — ${fullDay(rows[rows.length - 1].day)}</b>: новые <b class="chart-tone-amber">${fmtRub(nSum)} ₽</b> (${Math.round(100 * nSum / Math.max(1, nSum + rSum))}%), повторные <b class="chart-tone-indigo">${fmtRub(rSum)} ₽</b>, итого <b>${fmtRub(nSum + rSum)} ₽</b>; новых покупателей: <b>${fmtRub(nPayers)}</b>. Повторные — эхо продаж прошлых месяцев; рекламу оценивайте по жёлтой части.`
                : 'Нет данных за выбранный период.';
        }

        async function loadFunnel() {
            const res = await acqFetch('funnel', {weeks: 12});
            const wk = res.weeks;
            acqDraw(document.getElementById('acq-funnel-chart'),
                wk.map((r) => dayLabel(r.week)),
                [],
                [
                    {label: 'Подписки', tone: 'indigo', data: wk.map((r) => r.trials), fmt: 'raw'},
                    {label: 'Подключения', tone: 'indigoSoft', data: wk.map((r) => r.connected), fmt: 'raw'},
                    {label: '100 МБ', tone: 'amberSoft', data: wk.map((r) => r.mb100), fmt: 'raw'},
                    {label: 'Инвойсы', tone: 'amber', data: wk.map((r) => r.invoice_clicks), fmt: 'raw'},
                    {label: 'Новые покупатели', tone: 'green', data: wk.map((r) => r.new_payers), fmt: 'raw'},
                ],
                {fullLabels: wk.map((r) => 'неделя с ' + r.week)});
            const funnelPct = (num, den) => den ? (100 * num / den).toFixed(1) + '%' : '—';
            document.getElementById('acq-funnel-table').innerHTML = acqTable(
                ['Неделя', 'Подписки', 'Подключения', '5 МБ', '100 МБ', 'Инвойсы', 'Оплат всего', 'Новых покупателей',
                 'Подписка→подкл.', 'Подкл.→100 МБ', 'Подписка→покупатель', 'Инвойс→Оплата', 'Выручка новых'],
                wk.map((r) => [r.week, r.trials, r.connected, r.mb5, r.mb100, r.invoice_clicks, r.payments, r.new_payers,
                    funnelPct(r.connected, r.trials),
                    funnelPct(r.mb100, r.connected),
                    funnelPct(r.new_payers, r.trials),
                    funnelPct(r.payments, r.invoice_clicks),
                    fmtRub(r.new_rub) + ' ₽']));
            await loadTrials();
        }

        async function loadTrials() {
            const windowDays = document.getElementById('acq-trials-window')?.value || '10';
            const tr = await acqFetch('trials', {days: 60, window: windowDays});
            const win = tr.window_days || windowDays;
            acqDraw(document.getElementById('acq-trials-chart'),
                tr.days.map((r) => dayLabel(r.day)),
                [{label: 'Подписок создано', tone: 'indigoSoft', data: tr.days.map((r) => r.trials), fmt: 'raw'}],
                [{label: `Оплатили в течение ${win} дней, %`, tone: 'green', data: tr.days.map((r) => r.conv_pct), fmt: 'pct'}],
                {fullLabels: tr.days.map((r) => r.day)});
        }

        const fmtCost = (v) => v == null ? '—' : v.toFixed(2) + ' ₽';
        const fmtInt = (v) => v == null ? '—' : v.toLocaleString('ru-RU');

        function updateAdsDailyTableFrame(group, rowCount) {
            const scroller = document.getElementById('acq-ads-daily-table');
            const shell = document.getElementById('acq-ads-data-shell');
            const title = document.getElementById('acq-ads-data-title');
            const count = document.getElementById('acq-ads-data-count');
            const hint = document.getElementById('acq-ads-scroll-hint');
            const directions = document.getElementById('acq-ads-scroll-directions');
            if (!scroller || !shell || !hint || !directions) return;

            const groupedByMonth = group === 'month';
            const periodLabel = groupedByMonth ? 'месяцам' : 'дням';
            if (title) title.textContent = `Данные графиков по ${periodLabel}`;
            if (count) count.textContent = `Показано периодов: ${rowCount}`;
            scroller.setAttribute(
                'aria-label',
                `Данные двух графиков рекламы по ${periodLabel}. Прокручиваемая таблица.`,
            );

            const refreshScrollCues = () => {
                const canScrollY = scroller.scrollHeight > scroller.clientHeight + 2;
                const canScrollX = scroller.scrollWidth > scroller.clientWidth + 2;
                const atBottom = !canScrollY || scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
                const atRight = !canScrollX || scroller.scrollLeft + scroller.clientWidth >= scroller.scrollWidth - 2;
                shell.classList.toggle('has-overflow-y', canScrollY);
                shell.classList.toggle('has-overflow-x', canScrollX);
                shell.classList.toggle('is-at-bottom', atBottom);
                shell.classList.toggle('is-at-right', atRight);
                hint.classList.toggle('is-active', canScrollY || canScrollX);
                const availableDirections = [
                    canScrollY ? 'вниз ↕' : '',
                    canScrollX ? 'в сторону ↔' : '',
                ].filter(Boolean);
                directions.textContent = availableDirections.length
                    ? availableDirections.join(' · ')
                    : 'все строки и столбцы видны';
            };

            scroller.__refreshScrollCues = refreshScrollCues;
            if (!scroller.dataset.scrollCueBound) {
                scroller.dataset.scrollCueBound = '1';
                scroller.addEventListener('scroll', refreshScrollCues, {passive: true});
            }
            requestAnimationFrame(refreshScrollCues);
        }

        async function loadAdsDaily() {
            const group = document.getElementById('acq-ads-group').value;
            const days = document.getElementById('acq-ads-days').value;
            const res = await acqFetch('ads_daily', {group, days});
            if (res.needs_migration) return;
            const rows = res.rows;
            const label = (p) => group === 'month' ? p : dayLabel(p);
            acqDraw(document.getElementById('acq-ads-daily-chart'),
                rows.map((r) => label(r.period)),
                [{label: 'Расход, ₽', tone: 'amberSoft', data: rows.map((r) => r.spend), fmt: 'rub'}],
                [
                    {label: 'Подписки', tone: 'indigo', data: rows.map((r) => r.subs), fmt: 'raw'},
                    {label: 'Подключения', tone: 'green', data: rows.map((r) => r.conns), fmt: 'raw'},
                    {label: 'Продажи', tone: 'pink', data: rows.map((r) => r.sales), fmt: 'raw'},
                ],
                {fullLabels: rows.map((r) => r.period)});
            acqDraw(document.getElementById('acq-ads-cost-chart'),
                rows.map((r) => label(r.period)),
                [],
                [
                    {label: 'CPC', tone: 'amber', data: rows.map((r) => r.cpc), fmt: 'rub'},
                    {label: 'Цена подписки', tone: 'indigo', data: rows.map((r) => r.cost_per_sub), fmt: 'rub'},
                    {label: 'Цена подключения', tone: 'green', data: rows.map((r) => r.cost_per_conn), fmt: 'rub'},
                    {label: 'Цена продажи', tone: 'pink', data: rows.map((r) => r.cost_per_sale), fmt: 'rub'},
                ],
                {fullLabels: rows.map((r) => r.period)});
            document.getElementById('acq-ads-daily-table').innerHTML = acqTable(
                [group === 'month' ? 'Месяц' : 'День', 'Расход, ₽', 'Показы', 'Клики', 'CPC',
                 'Подписки', 'Цена подписки', 'Подключения', 'Цена подключения', 'Продажи', 'Цена продажи'],
                rows.slice().reverse().map((r) => [r.period, fmtRub(r.spend), fmtInt(r.impressions), fmtInt(r.clicks),
                    fmtCost(r.cpc), r.subs, fmtCost(r.cost_per_sub),
                    r.conns, fmtCost(r.cost_per_conn), r.sales, fmtCost(r.cost_per_sale)]));
            updateAdsDailyTableFrame(group, rows.length);
        }

        async function loadAds() {
            const res = await acqFetch('ads', {weeks: 12});
            document.getElementById('acq-ads-migration').style.display = res.needs_migration ? 'block' : 'none';
            renderAdAccounts(res.accounts || []);
            document.getElementById('acq-spends-table').innerHTML = acqTable(
                ['Дата', 'Канал', 'Аккаунт', 'Сумма, ₽', 'Показы', 'Клики', 'Комментарий', ''],
                res.spends.map((r) => [r.day, r.channel, r.account || 'default', fmtRub(r.amount_rub),
                    fmtInt(r.impressions), fmtInt(r.clicks), r.comment,
                    acqHtml(`<button data-spend-del="${escapeHtml(String(r.id))}" style="background:none;border:0;color:rgba(255,120,80,.8);cursor:pointer;">удалить</button>`)]));
            document.getElementById('acq-ads-table').innerHTML = acqTable(
                ['Неделя', 'Траты, ₽', 'Подписки', 'Цена подписки', 'Подключения', 'Цена подключения', 'Новых', 'Цена продажи', 'Выручка новых', 'ДРР', 'ROMI'],
                res.weeks.map((r) => [r.week, fmtRub(r.spend), r.subs,
                    fmtCost(r.cost_per_sub),
                    r.conns, fmtCost(r.cost_per_conn),
                    r.new_payers,
                    r.cost_per_sale == null ? '—' : fmtRub(r.cost_per_sale) + ' ₽',
                    fmtRub(r.new_rub) + ' ₽',
                    r.drr == null ? '—' : r.drr + '%',
                    r.romi == null ? '—' : ('x' + r.romi.toFixed(2))]));
            document.querySelectorAll('[data-spend-del]').forEach((btn) => btn.addEventListener('click', async () => {
                const body = new FormData();
                body.append('action', 'delete'); body.append('id', btn.dataset.spendDel);
                await fetch(SPENDS_API, {method: 'POST', body, headers: {'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest'}});
                loadAcqSection('acq-ads', true);
            }));
            await loadAdsDaily();
        }

        async function loadCohorts() {
            const res = await acqFetch('cohorts', {months: 14});
            document.getElementById('acq-ltv-table').innerHTML = acqTable(
                ['Когорта (месяц 1-й оплаты)', 'Покупателей', '1-й мес', '+1 мес', '+2 мес', '+3 мес', '+4 мес', '+5 мес', '+6 мес'],
                res.ltv.map((r) => [r.cohort, r.size, ...r.ltv_per_user.map((v) =>
                    v ? fmtRub(v) + ' ₽' : '—')]));
            const retCell = (r, h) => {
                const v = r['r' + h] + '%';
                return r['r' + h + '_matured']
                    ? v
                    : acqHtml(`<span style="color:rgba(255,255,255,.35);" title="Окно ${h} дней ещё не истекло для всей когорты — процент дорастёт">${v} <i>(идёт)</i></span>`);
            };
            document.getElementById('acq-retention-table').innerHTML = acqTable(
                ['Когорта', 'Покупателей', 'Купили 2-й раз ≤30 дней', '≤60 дней', '≤90 дней', '≤180 дней', '≤365 дней'],
                res.retention.map((r) => [r.cohort, r.size,
                    retCell(r, 30), retCell(r, 60), retCell(r, 90), retCell(r, 180), retCell(r, 365)]));
        }

        async function loadPushes() {
            const res = await acqFetch('pushes', {days: 30});
            const days = {};
            res.revenue_days.forEach((r) => { days[r.day] = {rub: r.rub, selling: 0}; });
            res.days.forEach((r) => { (days[r.day] = days[r.day] || {rub: 0}).selling = r.selling; });
            const keys = Object.keys(days).sort();
            acqDraw(document.getElementById('acq-pushes-chart'),
                keys.map(dayLabel),
                [{label: 'Продающих пушей', tone: 'indigoSoft', data: keys.map((k) => days[k].selling || 0), fmt: 'raw'}],
                [{label: 'Выручка, ₽ (правая ось)', tone: 'amber', data: keys.map((k) => days[k].rub || 0), fmt: 'rub'}],
                {fullLabels: keys});
            const wbAmounts = (r) => {
                const items = (r.amounts || []);
                const top = items.slice(0, 3).map((a) => `${fmtRub(a.amount)}₽×${a.count}`).join(' · ');
                const rest = items.length > 3 ? ` <span style="color:rgba(255,255,255,.4);">+${items.length - 3}</span>` : '';
                return top ? acqHtml(top + rest) : '—';
            };
            document.getElementById('acq-winback-table').innerHTML = res.winback.length
                ? acqTable(['Тип пуша', 'Отправлено', 'Оплатили ≤72ч', 'Конверсия', 'Новые', 'Повторные', 'Медиана до оплаты', 'Выручка', 'Чеки'],
                    res.winback.map((r) => [r.type, r.sent, r.paid_72h, r.conv_pct + '%',
                        r.new_payers, r.repeat_payers,
                        r.median_hours == null ? '—' : r.median_hours + ' ч',
                        fmtRub(r.rub) + ' ₽', wbAmounts(r)]))
                : '<div style="color:var(--muted);font-size:12px;">Пока нет данных — события notification_sent копятся после деплоя бота.</div>';
        }

        async function loadPatterns() {
            const days = document.getElementById('acq-patterns-days').value;
            const res = await acqFetch('patterns', {days});
            const hrs = [...Array(24).keys()];
            const cutoff = res.current_hour_msk;
            acqDraw(document.getElementById('acq-today-chart'),
                hrs.map((h) => h + ':00'),
                [],
                [
                    {label: 'коридор p25', tone: 'neutralSoft', data: res.typical.p25, dash: [4, 3], width: 1, fmt: 'rub'},
                    {label: 'коридор p75', tone: 'neutralSoft', data: res.typical.p75, dash: [4, 3], width: 1, fmt: 'rub'},
                    {label: 'Медиана', tone: 'neutral', data: res.typical.p50, fmt: 'rub'},
                    {label: 'Сегодня', tone: 'amber', width: 3, data: res.today.map((v, h) => (h <= cutoff ? v : null)), fmt: 'rub'},
                ],
                {fullLabels: hrs.map((h) => `к ${h}:59 МСК (накопленная выручка)`), lineOnly: true, rangeBand: [0, 1]});
            const cell = {};
            let maxRub = 1;
            res.heatmap.forEach((r) => { cell[`${r.dow}-${r.hr}`] = r; maxRub = Math.max(maxRub, r.rub); });
            const dows = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
            let html = '<table style="border-collapse:collapse;">';
            html += '<tr><td></td>' + hrs.map((h) => `<td style="font-size:9px;color:rgba(255,255,255,.4);padding:2px 3px;text-align:center;">${h}</td>`).join('') + '</tr>';
            for (let d = 1; d <= 7; d++) {
                html += `<tr><td style="font-size:10px;color:rgba(255,255,255,.6);padding:2px 6px;">${dows[d - 1]}</td>`;
                hrs.forEach((h) => {
                    const r = cell[`${d}-${h}`];
                    const a = r ? Math.min(1, 0.08 + 0.92 * r.rub / maxRub) : 0.03;
                    const title = r ? `${dows[d - 1]} ${h}:00–${h}:59 — ${r.payments} оплат, ${fmtRub(r.rub)} ₽ за 60 дней` : 'нет оплат';
                    html += `<td title="${title}" style="width:22px;height:18px;background:rgba(255,199,0,${a.toFixed(2)});border:1px solid rgba(0,0,0,.35);cursor:default;"></td>`;
                });
                html += '</tr>';
            }
            document.getElementById('acq-heatmap').innerHTML = html + '</table>';
        }

        const TARIFF_LABELS = {
            month: '1 месяц', threemonths: '3 месяца', sixmonths: '6 месяцев',
            year: '12 месяцев', oneday: '1 день', threedays: '3 дня (промо)',
            oneweek: '1 неделя', '?': 'не определён',
        };
        const tariffLabel = (t) => TARIFF_LABELS[t] || t;
        // Полоска-бар внутри ячейки таблицы: доля pct от максимума maxPct.
        const pctBar = (pct, maxPct) => {
            const w = maxPct ? Math.max(1, Math.round(100 * pct / maxPct)) : 0;
            return acqHtml(`<div style="display:flex;align-items:center;gap:8px;min-width:180px;"><div style="height:10px;width:${w}%;max-width:100%;background:rgba(255,199,0,.82);border-radius:3px;"></div><span>${pct.toFixed(1)}%</span></div>`);
        };

        async function loadJourney() {
            const timing = await acqFetch('trial_timing', {days: document.getElementById('acq-timing-days').value});
            document.getElementById('acq-timing-summary').innerHTML =
                `Подписок в когорте: <b class="chart-tone-strong">${timing.trials.toLocaleString('ru-RU')}</b> · ` +
                `оплатили: <b class="chart-tone-strong">${timing.converted.toLocaleString('ru-RU')}</b> · ` +
                `конверсия в оплату: <b class="chart-tone-strong">${timing.conversion_pct}%</b>`;
            const maxPct = Math.max(1, ...timing.buckets.map((b) => b.pct));
            document.getElementById('acq-timing-table').innerHTML = acqTable(
                ['День первой оплаты', 'Людей', 'Доля оплативших'],
                timing.buckets.map((b) => [b.label, b.users.toLocaleString('ru-RU'), pctBar(b.pct, maxPct)]));

            const ladder = await acqFetch('renewal_ladder', {months: 12});
            const step = (cur, prev) => prev
                ? acqHtml(`${(100 * cur / prev).toFixed(1)}% <span style="color:rgba(255,255,255,.4);">(${cur.toLocaleString('ru-RU')})</span>`)
                : '—';
            const ladderRow = (r, name) => [name, r.attracted.toLocaleString('ru-RU'),
                step(r.paid1, r.attracted), step(r.paid2, r.paid1),
                step(r.paid3, r.paid2), step(r.paid4, r.paid3)];
            document.getElementById('acq-ladder-table').innerHTML = acqTable(
                ['Когорта', 'Привлечено', 'Оплатил', '2-я оплата', '3-я оплата', '4-я оплата'],
                [...ladder.cohorts.map((r) => ladderRow(r, r.cohort)),
                 ladderRow(ladder.totals, acqHtml('<b>Итого</b>'))]);

            await loadTariffPaths();
        }

        async function loadTariffPaths() {
            const res = await acqFetch('tariff_paths', {months: document.getElementById('acq-paths-months').value});
            const fromCell = (r) => r.from.length
                ? r.from.map((f) => `${tariffLabel(f.tariff)} × ${f.users.toLocaleString('ru-RU')}`).join(' · ')
                : '—';
            document.getElementById('acq-tariff-paths-table').innerHTML = acqTable(
                ['Тариф', 'Покупателей', 'Оплат', 'Выручка', 'Впервые купили тариф', 'Из них первая покупка', 'Пришли с'],
                res.tariffs.map((r) => [tariffLabel(r.tariff),
                    r.buyers.toLocaleString('ru-RU'), r.payments.toLocaleString('ru-RU'),
                    fmtRub(r.rub) + ' ₽', r.adopters.toLocaleString('ru-RU'),
                    `${r.first_purchase.toLocaleString('ru-RU')} (${r.first_purchase_pct}%)`,
                    fromCell(r)]));
        }

        // Карточка-плитка со значением для MRR/здоровья платежей.
        const acqCard = (label, value, tone = '') =>
            `<div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:12px 16px;min-width:150px;">` +
            `<div style="font-size:11px;color:var(--muted);margin-bottom:4px;">${label}</div>` +
            `<div class="${tone}" style="font-size:20px;font-weight:800;">${value}</div></div>`;

        async function loadMrr() {
            const months = document.getElementById('acq-mrr-months').value;
            const res = await acqFetch('mrr', {months});
            const m = res.mrr;
            const monthLabel = (iso) => iso.slice(5, 7) + '.' + iso.slice(0, 4);
            const currentMonth = new Date().toISOString().slice(0, 7);
            const lastClosed = m.filter((r) => r.month.slice(0, 7) < currentMonth).slice(-1)[0];
            document.getElementById('acq-mrr-cards').innerHTML =
                acqCard('MRR последнего закрытого месяца', lastClosed ? fmtRub(lastClosed.mrr) + ' ₽' : '—', 'chart-tone-gold') +
                acqCard('Прогноз MRR по живым рекуррентам', fmtRub(res.live.mrr_forecast) + ' ₽', 'chart-tone-green') +
                acqCard('Активных рекуррентов сейчас', res.live.recurrents_total.toLocaleString('ru-RU')) +
                res.live.by_tariff.map((t) => acqCard(t.tariff, `${t.count} шт · ${fmtRub(t.mrr)} ₽/мес`)).join('');
            acqDraw(document.getElementById('acq-mrr-chart'),
                m.map((r) => monthLabel(r.month)),
                [{label: 'MRR по покрытию, ₽', tone: 'amberSoft', data: m.map((r) => r.mrr), fmt: 'rub'}],
                [{label: 'Клиентов с покрытием', tone: 'indigo', data: m.map((r) => r.covered_users), fmt: 'raw'}],
                {fullLabels: m.map((r) => monthLabel(r.month))});
            const d = res.dynamics;
            acqDraw(document.getElementById('acq-mrr-churn-chart'),
                d.map((r) => monthLabel(r.month)),
                [{label: 'Активная рекуррентная база', tone: 'indigoSoft', data: d.map((r) => r.active_recurrents), fmt: 'raw'}],
                [
                    {label: 'Отмены автоплатежа', tone: 'pink', data: d.map((r) => r.cancels), fmt: 'raw'},
                    {label: 'Churn, % (правая ось)', tone: 'amber', data: d.map((r) => r.churn_pct), fmt: 'pct'},
                ],
                {fullLabels: d.map((r) => monthLabel(r.month))});
            document.getElementById('acq-mrr-table').innerHTML = acqTable(
                ['Месяц', 'Активная база', 'Списаний (людей)', 'Autopay-фейлов', 'Отмен', 'Churn'],
                d.slice().reverse().map((r) => [monthLabel(r.month), r.active_recurrents,
                    r.autopay_success_users, r.autopay_failures, r.cancels,
                    r.churn_pct == null ? '—' : r.churn_pct + '%']));
        }

        async function loadPayHealth() {
            const days = document.getElementById('acq-payhealth-days').value;
            const group = document.getElementById('acq-payhealth-group').value;
            const res = await acqFetch('payment_health', {days, group});
            const rate = (v) => v == null ? '—' : v + '%';
            const toneFor = (v) => v == null ? '' : (v >= 50 ? 'chart-tone-green' : 'chart-tone-pink');
            document.getElementById('acq-payhealth-cards').innerHTML =
                acqCard('ЮКасса: оплачено/создано', `${res.yookassa.totals.paid}/${res.yookassa.totals.created} · ${rate(res.yookassa.totals.rate)}`, toneFor(res.yookassa.totals.rate)) +
                acqCard('WATA: оплачено/создано', `${res.wata.totals.paid}/${res.wata.totals.created} · ${rate(res.wata.totals.rate)}`, toneFor(res.wata.totals.rate)) +
                acqCard('Автосписания: успех', `${res.autopay.totals.success}/${res.autopay.totals.success + res.autopay.totals.failure} · ${rate(res.autopay.totals.rate)}`, toneFor(res.autopay.totals.rate));
            const periods = [...new Set([
                ...res.yookassa.series.map((r) => r.period),
                ...res.wata.series.map((r) => r.period),
                ...res.autopay.series.map((r) => r.period),
            ])].sort();
            const byPeriod = (series) => Object.fromEntries(series.map((r) => [r.period, r]));
            const yk = byPeriod(res.yookassa.series), wa = byPeriod(res.wata.series), ap = byPeriod(res.autopay.series);
            acqDraw(document.getElementById('acq-payhealth-chart'),
                periods.map((p) => dayLabel(p)),
                [],
                [
                    {label: 'ЮКасса, % успеха', tone: 'indigo', data: periods.map((p) => yk[p]?.rate ?? null), fmt: 'pct'},
                    {label: 'WATA, % успеха', tone: 'amber', data: periods.map((p) => wa[p]?.rate ?? null), fmt: 'pct'},
                    {label: 'Автосписания, % успеха', tone: 'green', data: periods.map((p) => ap[p]?.rate ?? null), fmt: 'pct'},
                ],
                {fullLabels: periods});
            document.getElementById('acq-payhealth-table').innerHTML = acqTable(
                ['Период', 'ЮК создано', 'ЮК оплачено', 'ЮК %', 'WATA создано', 'WATA оплачено', 'WATA %', 'Autopay успех', 'Autopay фейл', 'Autopay %'],
                periods.slice().reverse().map((p) => [p,
                    yk[p]?.created ?? 0, yk[p]?.paid ?? 0, rate(yk[p]?.rate ?? null),
                    wa[p]?.created ?? 0, wa[p]?.paid ?? 0, rate(wa[p]?.rate ?? null),
                    ap[p]?.success ?? 0, ap[p]?.failure ?? 0, rate(ap[p]?.rate ?? null)]));
        }

        const loaders = {
            'acq-revenue': loadRevenue, 'acq-newrep': loadNewRepeat, 'acq-expiry': loadExpiry, 'acq-funnel': loadFunnel, 'acq-ads': loadAds,
            'acq-cohorts': loadCohorts, 'acq-pushes': loadPushes, 'acq-patterns': loadPatterns,
            'acq-journey': loadJourney, 'acq-mrr': loadMrr, 'acq-payhealth': loadPayHealth,
        };

        // [ads-summary] Сводка рекламы за диапазон дат: расход по аккаунтам,
        // CPC и цены подписки/подключения/продажи за одни и те же даты.
        function adsSummaryField(id) {
            return document.getElementById(id)?.closest('[data-date-picker]');
        }

        function setAdsSummaryPreset(days) {
            const today = new Date();
            const start = addDays(today, -(Number(days) - 1));
            const startField = adsSummaryField('acq-ads-sum-start');
            const endField = adsSummaryField('acq-ads-sum-end');
            if (!startField || !endField) return;
            setDateFieldValue(startField, isoFromLocalDate(start));
            setDateFieldValue(endField, isoFromLocalDate(today));
            document.querySelectorAll('[data-ads-summary-preset]').forEach((button) => {
                button.classList.toggle('active', button.dataset.adsSummaryPreset === String(days));
            });
        }

        function renderAdsSummary(res) {
            const body = document.getElementById('acq-ads-summary-body');
            if (!body) return;
            if (res.needs_migration) {
                body.innerHTML = '<p class="acq-ads-summary-note">Нужна миграция таблицы <code>ad_spends</code> — сводка появится после неё.</p>';
                return;
            }
            const fullDay = (iso) => dayLabel(iso) + '.' + iso.slice(0, 4);
            const daysWord = (n) => {
                const mod10 = n % 10, mod100 = n % 100;
                if (mod10 === 1 && mod100 !== 11) return 'день';
                if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'дня';
                return 'дней';
            };
            const tile = (label, value, meta, tone = '') => `
                <div class="acq-ads-summary-tile${tone ? ` is-${tone}` : ''}">
                    <span class="acq-ads-summary-tile-label">${label}</span>
                    <strong class="acq-ads-summary-tile-value">${value}</strong>
                    <span class="acq-ads-summary-tile-meta">${meta}</span>
                </div>`;
            const rubOrDash = (v) => v == null ? '—' : `${fmtRub(v)} ₽`;
            const tiles = [
                tile('Расход на рекламу', `${fmtRub(res.spend)} ₽`, res.accounts.length ? `${res.accounts.length} ${res.accounts.length === 1 ? 'аккаунт' : res.accounts.length < 5 ? 'аккаунта' : 'аккаунтов'}` : 'расходов нет', 'spend'),
                tile('CPC', fmtCost(res.cpc), res.clicks == null ? 'показы и клики не загружены' : `${fmtInt(res.impressions)} показов · ${fmtInt(res.clicks)} кликов`),
                tile('Цена подписки', fmtCost(res.cost_per_sub), `${fmtInt(res.subs)} подписок`),
                tile('Цена подключения', fmtCost(res.cost_per_conn), `${fmtInt(res.conns)} подключений`),
                tile('Цена продажи', fmtCost(res.cost_per_sale), `${fmtInt(res.sales)} новых покупателей`, 'sale'),
                tile('Выручка новых', rubOrDash(res.new_rub), res.drr == null ? 'ДРР и ROMI не считаются без расходов и выручки' : `ДРР ${res.drr}% · ROMI x${res.romi.toFixed(2)}`),
            ].join('');
            const accountRows = res.accounts.map((a) => [
                a.account, `${fmtRub(a.spend)} ₽`, a.share == null ? '—' : `${a.share}%`,
                fmtInt(a.impressions), fmtInt(a.clicks), fmtCost(a.cpc),
            ]);
            if (res.accounts.length > 1) {
                accountRows.push([acqHtml('<b>Итого</b>'), acqHtml(`<b>${fmtRub(res.spend)} ₽</b>`), acqHtml('<b>100%</b>'),
                    acqHtml(`<b>${fmtInt(res.impressions)}</b>`), acqHtml(`<b>${fmtInt(res.clicks)}</b>`), acqHtml(`<b>${fmtCost(res.cpc)}</b>`)]);
            }
            body.innerHTML = `
                <p class="acq-ads-summary-range">За <b>${res.days} ${daysWord(res.days)}</b>: ${fullDay(res.start)} — ${fullDay(res.end)}</p>
                <div class="acq-ads-summary-tiles">${tiles}</div>
                <div class="acq-ads-summary-accounts">
                    <div class="acq-ads-summary-accounts-title">Расход по аккаунтам</div>
                    ${res.accounts.length
                        ? acqTable(['Аккаунт', 'Расход, ₽', 'Доля', 'Показы', 'Клики', 'CPC'], accountRows)
                        : '<p class="acq-ads-summary-note">За выбранные даты расходов не записано — загрузите CSV Директа или добавьте траты вручную.</p>'}
                </div>`;
        }

        async function loadAdsSummary() {
            const start = document.getElementById('acq-ads-sum-start')?.value;
            const end = document.getElementById('acq-ads-sum-end')?.value;
            if (!start || !end) setAdsSummaryPreset(30);
            const params = {
                start: document.getElementById('acq-ads-sum-start').value,
                end: document.getElementById('acq-ads-sum-end').value,
            };
            const body = document.getElementById('acq-ads-summary-body');
            body?.classList.add('is-loading');
            try {
                renderAdsSummary(await acqFetch('ads_summary', params));
            } catch (error) {
                if (body) body.innerHTML = `<p class="acq-ads-summary-note is-error">Не удалось загрузить сводку: ${escapeHtml(error.message || String(error))}</p>`;
                throw error;
            } finally {
                body?.classList.remove('is-loading');
            }
        }

        (() => {
            const original = loaders['acq-ads'];
            loaders['acq-ads'] = () => Promise.all([original(), loadAdsSummary()]);
            document.querySelectorAll('[data-ads-summary-preset]').forEach((button) => button.addEventListener('click', () => {
                setAdsSummaryPreset(button.dataset.adsSummaryPreset);
                loadAdsSummary().catch(console.error);
            }));
            document.getElementById('acq-ads-summary-apply')?.addEventListener('click', () => {
                document.querySelectorAll('[data-ads-summary-preset]').forEach((button) => button.classList.remove('active'));
                loadAdsSummary().catch(console.error);
            });
            ['acq-ads-sum-start', 'acq-ads-sum-end'].forEach((id) => document.getElementById(id)?.addEventListener('input', () => {
                document.querySelectorAll('[data-ads-summary-preset]').forEach((button) => button.classList.remove('active'));
            }));
        })();
        // [/ads-summary]

        function loadAcqSection(name, force = false) {
            const loader = loaders[name];
            if (!loader || (loaded[name] && !force)) return;
            loaded[name] = true;
            loader().catch((e) => { loaded[name] = false; console.error('acquisition load failed', name, e); });
        }

        document.querySelectorAll('#panel-acquisition .subtab').forEach((btn) => {
            btn.addEventListener('click', () => loadAcqSection(btn.dataset.subtab));
        });
        setupSubtabs('panel-acquisition');

        document.querySelector('.tab-btn[data-tab="acquisition"]')?.addEventListener('click', () => {
            loadAcqSection(document.querySelector('#panel-acquisition .subtab.active')?.dataset.subtab || 'acq-revenue');
        });
        if (location.hash === '#acquisition') loadAcqSection('acq-revenue');

        document.getElementById('acq-mrr-months')?.addEventListener('change', () => loadAcqSection('acq-mrr', true));
        document.getElementById('acq-payhealth-days')?.addEventListener('change', () => loadAcqSection('acq-payhealth', true));
        document.getElementById('acq-payhealth-group')?.addEventListener('change', () => loadAcqSection('acq-payhealth', true));

        // Календарные поля периода — кастомные date-field (как в «Аналитике»):
        // программная установка значения идёт через setDateFieldValue, чтобы
        // обновлялся и лейбл кнопки-триггера.
        function setAcqDateField(id, iso) {
            const input = document.getElementById(id);
            if (!input) return;
            const field = input.closest('[data-date-picker]');
            if (field) setDateFieldValue(field, iso);
            else input.value = iso;
        }
        document.getElementById('acq-newrep-days')?.addEventListener('change', () => {
            setAcqDateField('acq-newrep-start', '');
            setAcqDateField('acq-newrep-end', '');
            const monthSel = document.getElementById('acq-newrep-month');
            if (monthSel) monthSel.value = '';
            loadNewRepeat().catch(console.error);
        });
        document.getElementById('acq-newrep-apply')?.addEventListener('click', () => loadNewRepeat().catch(console.error));
        // Выручка: пресеты, разрез столбиков, ручной период.
        document.querySelectorAll('[data-revenue-preset]').forEach((button) => button.addEventListener('click', () => {
            setRevenuePreset(button.dataset.revenuePreset);
            loadRevenue().catch(console.error);
        }));
        document.querySelectorAll('[data-revenue-mode]').forEach((button) => button.addEventListener('click', () => {
            revenueState.mode = button.dataset.revenueMode;
            document.querySelectorAll('[data-revenue-mode]').forEach((b) => b.classList.toggle('active', b === button));
            if (revenueState.res) renderRevenueChart(revenueState.res);
        }));
        document.getElementById('acq-revenue-apply')?.addEventListener('click', () => {
            document.querySelectorAll('[data-revenue-preset]').forEach((b) => b.classList.remove('active'));
            loadRevenue().catch(console.error);
        });
        ['acq-revenue-start', 'acq-revenue-end'].forEach((id) => document.getElementById(id)?.addEventListener('input', () => {
            document.querySelectorAll('[data-revenue-preset]').forEach((b) => b.classList.remove('active'));
        }));
        window.addEventListener('resize', () => {
            if (!document.getElementById('subpanel-acq-revenue')?.classList.contains('active')) return;
            if (revenueState.res) renderRevenueChart(revenueState.res);
            if (revenueState.weekly) renderWeekly(revenueState.weekly);
        });
        // Окончания и продления: пресеты периода, шаг, окно, ручной период.
        document.querySelectorAll('[data-expiry-preset]').forEach((button) => button.addEventListener('click', () => {
            setExpiryPreset(button.dataset.expiryPreset);
            loadExpiry().catch(console.error);
        }));
        document.querySelectorAll('[data-expiry-group]').forEach((button) => button.addEventListener('click', () => {
            expiryState.group = button.dataset.expiryGroup === 'week' ? 'week' : 'day';
            document.querySelectorAll('[data-expiry-group]').forEach((b) => b.classList.toggle('active', b === button));
            loadExpiry().catch(console.error);
        }));
        document.getElementById('acq-expiry-window')?.addEventListener('change', () => loadExpiry().catch(console.error));
        document.getElementById('acq-expiry-apply')?.addEventListener('click', () => {
            document.querySelectorAll('[data-expiry-preset]').forEach((b) => b.classList.remove('active'));
            loadExpiry().catch(console.error);
        });
        ['acq-expiry-start', 'acq-expiry-end'].forEach((id) => document.getElementById(id)?.addEventListener('input', () => {
            document.querySelectorAll('[data-expiry-preset]').forEach((b) => b.classList.remove('active'));
        }));
        window.addEventListener('resize', () => { if (expiryState.res && document.getElementById('subpanel-acq-expiry')?.classList.contains('active')) renderExpiryChart(expiryState.res); });
        // Переключатель по месяцам: заполняем список последних месяцев и по выбору
        // подставляем границы месяца в календарные поля периода.
        (function () {
            const monthSel = document.getElementById('acq-newrep-month');
            if (!monthSel) return;
            const MONTH_NAMES = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
            const now = new Date();
            for (let i = 0; i < 18; i++) {
                const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
                const opt = document.createElement('option');
                opt.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
                opt.textContent = `${MONTH_NAMES[d.getMonth()]} ${d.getFullYear()}`;
                monthSel.appendChild(opt);
            }
            // setDateFieldValue шлёт input-событие; во время программной
            // подстановки границ месяца сброс селектора надо подавлять.
            let settingMonthBounds = false;
            monthSel.addEventListener('change', () => {
                if (!monthSel.value) return;
                const [y, m] = monthSel.value.split('-').map(Number);
                const lastDay = new Date(y, m, 0).getDate();
                settingMonthBounds = true;
                setAcqDateField('acq-newrep-start', `${monthSel.value}-01`);
                setAcqDateField('acq-newrep-end', `${monthSel.value}-${String(lastDay).padStart(2, '0')}`);
                settingMonthBounds = false;
                loadNewRepeat().catch(console.error);
            });
            // Ручная правка календарных дат снимает выбор месяца
            ['acq-newrep-start', 'acq-newrep-end'].forEach((id) => {
                document.getElementById(id)?.addEventListener('input', () => {
                    if (!settingMonthBounds) monthSel.value = '';
                });
            });
        })();
        document.getElementById('acq-patterns-days')?.addEventListener('change', () => loadPatterns().catch(console.error));
        document.getElementById('acq-timing-days')?.addEventListener('change', () => loadJourney().catch(console.error));
        document.getElementById('acq-paths-months')?.addEventListener('change', () => loadTariffPaths().catch(console.error));
        // ===== Рекламные аккаунты: селектор общий для CSV-импорта и ручного ввода =====
        // Список собирается из данных (DISTINCT account) + добавленные вручную в
        // этой сессии; после первой загрузки с новым аккаунтом он попадает в БД
        // и дальше приходит из данных.
        const adAccountsAdded = new Set();
        let adAccountsFromData = [];
        function currentAdAccount() {
            return document.getElementById('acq-account-select')?.value || 'default';
        }
        function renderAdAccounts(fromData) {
            if (fromData) adAccountsFromData = fromData;
            const sel = document.getElementById('acq-account-select');
            if (!sel) return;
            const selected = sel.value || 'default';
            const all = Array.from(new Set(
                ['default', ...adAccountsFromData, ...adAccountsAdded]));
            const options = all.map((a) =>
                `<option value="${a.replace(/"/g, '&quot;')}">${a}</option>`).join('');
            sel.innerHTML = options;
            sel.value = all.includes(selected) ? selected : 'default';
            // Дублирующий селектор в форме импорта CSV: человек явно видит,
            // в какой аккаунт лягут данные. Синхронизирован с верхним.
            const csvSel = document.getElementById('acq-csv-account');
            if (csvSel) {
                csvSel.innerHTML = options;
                csvSel.value = sel.value;
            }
        }
        // Двусторонняя синхронизация селекторов аккаунта (верхний ↔ CSV-форма)
        document.getElementById('acq-account-select')?.addEventListener('change', () => {
            const csvSel = document.getElementById('acq-csv-account');
            if (csvSel) csvSel.value = currentAdAccount();
        });
        document.getElementById('acq-csv-account')?.addEventListener('change', (ev) => {
            const sel = document.getElementById('acq-account-select');
            if (sel) sel.value = ev.target.value;
        });
        // Кнопка выбора файла: показываем имя выбранного файла на самой кнопке
        document.getElementById('acq-csv-file-input')?.addEventListener('change', (ev) => {
            const nameEl = document.getElementById('acq-csv-filename');
            const label = document.getElementById('acq-csv-file-label');
            const file = ev.target.files && ev.target.files[0];
            if (nameEl) nameEl.textContent = file ? file.name : 'Выбрать CSV-файл';
            if (label) label.classList.toggle('active', Boolean(file));
        });
        document.getElementById('acq-account-add')?.addEventListener('click', () => {
            const name = (prompt('Название аккаунта (например, direct-2):') || '')
                .trim().slice(0, 64);
            if (!name) return;
            adAccountsAdded.add(name);
            renderAdAccounts(null);
            const sel = document.getElementById('acq-account-select');
            if (sel) sel.value = name;
        });
        // Переименование задним числом: данные, залитые до мульти-аккаунтов,
        // лежат под «default» — их можно объявить конкретным кабинетом.
        document.getElementById('acq-account-rename')?.addEventListener('click', async () => {
            const src = currentAdAccount();
            const dst = (prompt(`Новое имя для аккаунта «${src}»:`) || '')
                .trim().slice(0, 64);
            if (!dst || dst === src) return;
            const statusEl = document.getElementById('acq-account-status');
            statusEl.textContent = 'переименование…';
            const body = new FormData();
            body.append('action', 'rename_account');
            body.append('from', src);
            body.append('to', dst);
            try {
                const resp = await fetch(SPENDS_API, {method: 'POST', body, headers: {'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest'}});
                const payload = await resp.json();
                if (payload.status === 'ok') {
                    statusEl.textContent = `переименовано строк: ${payload.renamed}`;
                    adAccountsAdded.delete(src);
                    adAccountsAdded.add(dst);
                    loadAcqSection('acq-ads', true);
                } else {
                    statusEl.textContent = payload.message || 'ошибка переименования';
                }
            } catch (e) {
                statusEl.textContent = 'ошибка переименования';
            }
        });

        document.getElementById('acq-spend-form')?.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            const body = new FormData(ev.target);
            body.append('action', 'upsert');
            body.append('account', currentAdAccount());
            const resp = await fetch(SPENDS_API, {method: 'POST', body, headers: {'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest'}});
            const payload = await resp.json();
            if (payload.status === 'ok') { ev.target.reset(); loadAcqSection('acq-ads', true); }
            else alert(payload.message || 'Ошибка сохранения');
        });
        document.getElementById('acq-ads-group')?.addEventListener('change', () => loadAdsDaily().catch(console.error));
        document.getElementById('acq-ads-days')?.addEventListener('change', () => loadAdsDaily().catch(console.error));
        window.addEventListener('resize', () => document.getElementById('acq-ads-daily-table')?.__refreshScrollCues?.());
        document.getElementById('acq-trials-window')?.addEventListener('change', () => loadTrials().catch(console.error));
        document.getElementById('acq-csv-form')?.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            const statusEl = document.getElementById('acq-csv-status');
            const fileInput = document.getElementById('acq-csv-file-input');
            // Инпут скрыт (стилизованная кнопка), браузерная валидация required
            // на нём не работает — проверяем вручную.
            if (!fileInput || !fileInput.files || !fileInput.files.length) {
                statusEl.textContent = 'сначала выберите CSV-файл';
                return;
            }
            const account = currentAdAccount();
            statusEl.textContent = `загрузка в аккаунт «${account}»…`;
            const body = new FormData(ev.target);
            body.append('action', 'import_csv');
            body.append('account', account);
            try {
                const resp = await fetch(SPENDS_API, {method: 'POST', body, headers: {'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest'}});
                const payload = await resp.json();
                if (payload.status === 'ok') {
                    statusEl.textContent = `импортировано в «${account}» дней: ${payload.imported} (${payload.from} — ${payload.to})`;
                    ev.target.reset();
                    const nameEl = document.getElementById('acq-csv-filename');
                    if (nameEl) nameEl.textContent = 'Выбрать CSV-файл';
                    document.getElementById('acq-csv-file-label')?.classList.remove('active');
                    // form.reset() сбросил селектор аккаунта в форме на первый
                    // option — возвращаем синхронизацию с верхним селектором.
                    const csvSel = document.getElementById('acq-csv-account');
                    if (csvSel) csvSel.value = account;
                    loadAcqSection('acq-ads', true);
                } else {
                    statusEl.textContent = payload.message || 'ошибка импорта';
                }
            } catch (e) {
                statusEl.textContent = 'ошибка импорта';
            }
        });
    })();
