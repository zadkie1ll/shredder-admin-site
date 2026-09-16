/* Мобильный кабинет: экраны как в приложении (главная → аккаунт → разделы),
   покупка, установка, устройства, промокоды, перевыпуск ключа.
   Работает поверх глобалов основного скрипта dashboard.html: newSetupApps,
   appleRecommendedApp, appleSubscriptionUrl, INCY_*, showToast,
   openAutopaySheet, openPaymentsHistorySheet, MiNetwork. Синтаксис — ES2018
   (без optional chaining и nullish coalescing): старые WebView Android и iOS. */
(function cabinetMobile() {
    var root = document.getElementById('cm-root');
    if (!root) return;

    var PLATFORMS = [
        { key: 'ios', label: 'iOS', icon: 'fab fa-apple' },
        { key: 'android', label: 'Android', icon: 'fab fa-android' },
        { key: 'macos', label: 'macOS', icon: 'fab fa-apple' },
        { key: 'windows', label: 'Windows', icon: 'fab fa-windows' },
        { key: 'linux', label: 'Linux', icon: 'fab fa-linux' },
        { key: 'androidtv', label: 'Android TV', icon: 'fas fa-tv' },
        { key: 'appletv', label: 'Apple TV', icon: 'fas fa-tv' },
    ];
    var STORE_WORDS = { ios: 'App Store', macos: 'App Store', appletv: 'App Store', android: 'Google Play' };

    var state = {
        page: 'home',
        platform: null,
        sheet: null,
        devices: null,
        discount: parseInt(root.getAttribute('data-cm-discount') || '0', 10) || 0,
        plainUrl: root.getAttribute('data-cm-plain-url') || '',
    };
    var pages = {};
    Array.prototype.forEach.call(root.querySelectorAll('[data-cm-page]'), function (el) {
        pages[el.getAttribute('data-cm-page')] = el;
    });

    function esc(v) {
        return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }
    function csrfToken() {
        var input = root.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? match[1] : '';
    }
    function toast(message) {
        if (typeof window.showToast === 'function') window.showToast(message);
    }
    function copyText(value) {
        return new Promise(function (resolve) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(value).then(function () { resolve(true); }, function () { resolve(legacyCopy(value)); });
                return;
            }
            resolve(legacyCopy(value));
        });
    }
    function legacyCopy(value) {
        try {
            var ta = document.createElement('textarea');
            ta.value = value;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            var ok = document.execCommand('copy');
            ta.remove();
            return ok;
        } catch (e) {
            return false;
        }
    }
    function isMobileMode() {
        return document.body.classList.contains('tg-webapp') || window.matchMedia('(max-width: 1024px)').matches;
    }
    function tgBackButton() {
        var tg = window.Telegram && window.Telegram.WebApp;
        return tg && tg.BackButton ? tg.BackButton : null;
    }

    // ----- Навигация по экранам -----
    function showPage(page) {
        if (!pages[page]) page = 'home';
        Object.keys(pages).forEach(function (key) {
            pages[key].classList.toggle('is-active', key === page);
        });
        state.page = page;
        var container = document.getElementById('app-container');
        if (container && container.scrollTo) container.scrollTo(0, 0);
        window.scrollTo(0, 0);
        if (page === 'devices') loadDevices(true);
        if (page === 'install') renderInstall();
        if (page === 'buy') updateTotal();
        if (page === 'promo') {
            var input = document.getElementById('cm-promo-code');
            if (input) window.requestAnimationFrame(function () { input.focus(); });
        }
        if (typeof window.updateTgBackButton === 'function') window.updateTgBackButton();
    }
    function go(page) {
        if (page === state.page) return;
        history.pushState({ cmPage: page }, '');
        showPage(page);
    }
    function back() {
        if (state.sheet) { closeSheet(); return true; }
        if (state.page !== 'home') { history.back(); return true; }
        return false;
    }
    window.addEventListener('popstate', function (event) {
        if (state.sheet) { closeSheet(false); return; }
        var target = event.state && event.state.cmPage ? event.state.cmPage : 'home';
        showPage(target);
    });
    if (!(history.state && history.state.cmPage)) history.replaceState({ cmPage: 'home' }, '');
    // Точки интеграции с TG BackButton основного скрипта.
    window.cmBackNeeded = function () { return isMobileMode() && (state.page !== 'home' || !!state.sheet); };
    window.cmHandleBack = function () { return isMobileMode() ? back() : false; };
    window.cmGo = go;

    // ----- Шторки -----
    var overlay = document.getElementById('cm-overlay');
    function openSheet(id) {
        var sheet = document.getElementById(id);
        if (!sheet) return;
        if (state.sheet) closeSheet(false);
        state.sheet = sheet;
        sheet.hidden = false;
        overlay.hidden = false;
        history.pushState({ cmPage: state.page, cmSheet: id }, '');
        window.requestAnimationFrame(function () {
            sheet.classList.add('is-open');
            overlay.classList.add('is-open');
            var focusable = sheet.querySelector('button, [href], input');
            if (focusable) focusable.focus();
        });
        if (id === 'cm-qr-sheet') renderQr();
        if (typeof window.updateTgBackButton === 'function') window.updateTgBackButton();
    }
    function closeSheet(popHistory) {
        var sheet = state.sheet;
        if (!sheet) return;
        state.sheet = null;
        sheet.classList.remove('is-open');
        overlay.classList.remove('is-open');
        setTimeout(function () { if (!state.sheet) { sheet.hidden = true; overlay.hidden = true; } }, 220);
        if (popHistory !== false && history.state && history.state.cmSheet) history.back();
        if (typeof window.updateTgBackButton === 'function') window.updateTgBackButton();
    }
    overlay.addEventListener('click', function () { closeSheet(); });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && state.sheet) { event.preventDefault(); closeSheet(); }
    });

    // Делегирование кликов: переходы, назад, шторки.
    root.addEventListener('click', function (event) {
        var goEl = event.target.closest('[data-cm-go]');
        if (goEl) { event.preventDefault(); go(goEl.getAttribute('data-cm-go')); return; }
        if (event.target.closest('[data-cm-back]')) { event.preventDefault(); back(); return; }
        var sheetEl = event.target.closest('[data-cm-sheet]');
        if (sheetEl) { event.preventDefault(); openSheet(sheetEl.getAttribute('data-cm-sheet')); return; }
        if (event.target.closest('[data-cm-sheet-close]')) { event.preventDefault(); closeSheet(); }
    });
    var tgBack = tgBackButton();
    if (tgBack && tgBack.onClick) {
        tgBack.onClick(function () { if (window.cmBackNeeded()) back(); });
    }

    // На мобильных «Продлить» из любого старого места ведёт на экран покупки.
    var legacyShowTariffs = window.showTariffs;
    window.showTariffs = function () {
        if (isMobileMode()) go('buy'); else if (typeof legacyShowTariffs === 'function') legacyShowTariffs();
    };

    // ----- Установка -----
    function detectPlatform() {
        var ua = navigator.userAgent || '';
        if (/iPhone|iPad|iPod/i.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return 'ios';
        if (/Android/i.test(ua)) return 'android';
        if (/Mac OS X|Macintosh/i.test(ua)) return 'macos';
        if (/Windows/i.test(ua)) return 'windows';
        if (/Linux/i.test(ua)) return 'linux';
        return 'ios';
    }
    function platformLabel(key) {
        for (var i = 0; i < PLATFORMS.length; i++) if (PLATFORMS[i].key === key) return PLATFORMS[i].label;
        return key;
    }
    function isApple(key) { return key === 'ios' || key === 'macos'; }
    function incyAvailable(key) {
        return isApple(key) && typeof appleRecommendedApp !== 'undefined' && appleRecommendedApp === 'incy'
            && typeof appleSubscriptionUrl !== 'undefined' && !!appleSubscriptionUrl;
    }
    function appFor(key) {
        var catalog = typeof newSetupApps !== 'undefined' ? newSetupApps : {};
        var cfg = catalog[key] || {};
        if (incyAvailable(key)) {
            return {
                name: 'INCY',
                installUrl: key === 'macos' ? INCY_MACOS_ARM_URL : INCY_IOS_APPSTORE_URL,
                fallbackText: key === 'macos' ? 'macOS — Intel' : null,
                fallbackUrl: key === 'macos' ? INCY_MACOS_INTEL_URL : null,
                subscriptionUrl: appleSubscriptionUrl,
                copyValue: appleSubscriptionUrl,
                alternative: cfg.appName ? { name: cfg.appName, installUrl: cfg.installUrl, subscriptionUrl: cfg.subscriptionUrl } : null,
            };
        }
        return {
            name: cfg.appName || 'приложение',
            installUrl: cfg.installUrl || '',
            fallbackText: cfg.fallbackText || null,
            fallbackUrl: cfg.fallbackUrl || null,
            subscriptionUrl: cfg.subscriptionUrl || state.plainUrl,
            copyValue: cfg.copyValue || cfg.subscriptionUrl || state.plainUrl,
            alternative: null,
        };
    }
    function renderInstall() {
        var tabs = document.getElementById('cm-install-tabs');
        var steps = document.getElementById('cm-install-steps');
        if (!tabs || !steps) return;
        if (!state.platform) state.platform = detectPlatform();
        tabs.innerHTML = PLATFORMS.map(function (pl) {
            return '<button type="button" role="tab" class="cm-tab' + (pl.key === state.platform ? ' is-active' : '') + '" data-cm-platform="' + pl.key + '" aria-selected="' + (pl.key === state.platform) + '"><i class="' + pl.icon + '" aria-hidden="true"></i>' + esc(pl.label) + '</button>';
        }).join('');
        Array.prototype.forEach.call(tabs.querySelectorAll('[data-cm-platform]'), function (btn) {
            btn.addEventListener('click', function () {
                state.platform = btn.getAttribute('data-cm-platform');
                renderInstall();
                var active = tabs.querySelector('.is-active');
                if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest', inline: 'center' });
            });
        });
        var app = appFor(state.platform);
        var store = STORE_WORDS[state.platform] || 'официального сайта';
        var addLabel = 'Добавить в ' + app.name;
        steps.innerHTML =
            '<div class="cm-step"><span class="cm-step-num">1</span><div><p>Установите ' + esc(app.name) + ' из ' + esc(store) + '.</p>' +
                (app.installUrl ? '<a class="cm-btn cm-btn-mini" href="' + esc(app.installUrl) + '" target="_blank" rel="noopener">' + esc(store === 'официального сайта' ? 'Скачать ' + app.name : store) + '</a>' : '') +
                (app.fallbackUrl ? ' <a class="cm-btn cm-btn-mini is-ghost" href="' + esc(app.fallbackUrl) + '" target="_blank" rel="noopener">' + esc(app.fallbackText || 'Другой установщик') + '</a>' : '') +
            '</div></div>' +
            '<div class="cm-step"><span class="cm-step-num">2</span><div><p>Добавьте подписку кнопкой «' + esc(addLabel) + '» — или скопируйте ссылку ниже и добавьте её в приложении.</p>' +
                '<a class="cm-btn cm-btn-mini" href="' + esc(app.subscriptionUrl) + '">' + esc(addLabel) + '</a>' +
                (app.alternative ? '<p class="cm-note">Альтернатива — <a href="' + esc(app.alternative.installUrl) + '" target="_blank" rel="noopener">' + esc(app.alternative.name) + '</a>: <a href="' + esc(app.alternative.subscriptionUrl) + '">добавить подписку</a>.</p>' : '') +
            '</div></div>' +
            '<div class="cm-step"><span class="cm-step-num">3</span><div><p>Для подключения нажмите большую кнопку по центру.</p></div></div>';
        var copyBtn = document.getElementById('cm-install-copy');
        if (copyBtn) copyBtn.setAttribute('data-cm-copy-value', app.copyValue || '');
    }
    var installCopy = document.getElementById('cm-install-copy');
    if (installCopy) installCopy.addEventListener('click', function () {
        var value = installCopy.getAttribute('data-cm-copy-value') || state.plainUrl;
        copyText(value).then(function (ok) { toast(ok ? 'Ссылка скопирована' : 'Не удалось скопировать — выделите ссылку вручную'); });
    });
    function renderQr() {
        var box = document.getElementById('cm-qr-box');
        if (!box) return;
        box.innerHTML = '';
        if (!state.plainUrl) { box.innerHTML = '<p class="cm-note">Ссылка подписки сейчас недоступна — обновите страницу чуть позже.</p>'; return; }
        if (typeof QRCode !== 'function') { box.textContent = state.plainUrl; return; }
        var size = Math.min(240, Math.max(160, window.innerWidth - 120));
        new QRCode(box, { text: state.plainUrl, width: size, height: size, colorDark: '#101113', colorLight: '#ffffff', correctLevel: QRCode.CorrectLevel.M });
    }
    var autoBtn = document.getElementById('cm-install-auto');
    if (autoBtn) {
        var detected = detectPlatform();
        var label = document.getElementById('cm-install-auto-label');
        if (label) label.textContent = 'Скачать для ' + platformLabel(detected);
        autoBtn.addEventListener('click', function () { state.platform = detected; go('install'); });
    }

    // ----- Устройства -----
    function deviceIcon(d) {
        var raw = ((d.platform || '') + ' ' + (d.device_model || '') + ' ' + (d.user_agent || '')).toLowerCase();
        if (/iphone|ipad|ios/.test(raw)) return 'fas fa-mobile-screen';
        if (/android/.test(raw)) return 'fab fa-android';
        if (/mac/.test(raw)) return 'fab fa-apple';
        if (/windows/.test(raw)) return 'fab fa-windows';
        if (/linux/.test(raw)) return 'fab fa-linux';
        if (/tv/.test(raw)) return 'fas fa-tv';
        return 'fas fa-laptop';
    }
    function deviceTitle(d) { return d.device_model || d.platform || 'Устройство'; }
    function deviceOs(d) {
        var parts = [];
        if (d.platform) parts.push(d.platform);
        if (d.os_version) parts.push(d.os_version);
        return parts.join(' · ');
    }
    function deviceSeen(d) {
        var updated = d.updated_at ? new Date(d.updated_at).getTime() : NaN;
        if (!isFinite(updated)) return { text: 'Нет данных об активности', online: false };
        var diffMin = Math.max(0, Math.round((Date.now() - updated) / 60000));
        if (diffMin < 10) return { text: 'В сети сейчас', online: true };
        if (diffMin < 60) return { text: 'Был в сети ' + diffMin + ' мин назад', online: false };
        if (diffMin < 1440) return { text: 'Был в сети ' + Math.round(diffMin / 60) + ' ч назад', online: false };
        return { text: 'Был в сети ' + Math.round(diffMin / 1440) + ' дн назад', online: false };
    }
    function updateDevicesCount(data) {
        var count = document.getElementById('cm-devices-count');
        if (!count) return;
        if (!data) { count.textContent = '—'; return; }
        count.textContent = data.total + ' / ' + (data.limit ? data.limit : '∞');
    }
    function renderDevices(data) {
        var list = document.getElementById('cm-devices-list');
        var note = document.getElementById('cm-devices-note');
        if (!list) return;
        if (note) {
            note.textContent = data.total
                ? 'Использовано ' + data.total + (data.limit ? ' из ' + data.limit : '') + '. Нажмите на устройство, чтобы увидеть, из какого приложения оно добавлено.'
                : 'Пока ни одно устройство не добавило подписку.';
        }
        if (!data.devices.length) {
            list.innerHTML = '<div class="cm-empty">Подключите первое устройство — оно появится здесь автоматически.</div>';
            return;
        }
        list.innerHTML = data.devices.map(function (d) {
            var seen = deviceSeen(d);
            var os = deviceOs(d);
            return '<details class="cm-device"><summary class="cm-row">' +
                '<i class="' + deviceIcon(d) + '" aria-hidden="true"></i>' +
                '<span>' + esc(deviceTitle(d)) + (os ? '<small>' + esc(os) + '</small>' : '') + '<small class="cm-device-seen' + (seen.online ? ' is-online' : '') + '">' + esc(seen.text) + '</small></span>' +
                '<svg class="cabinet-icon cm-chevron" aria-hidden="true"><use href="#cabinet-chevron"></use></svg></summary>' +
                '<div class="cm-device-body">' +
                    '<div class="cm-device-field"><span>User-Agent</span><code class="cm-ua">' + esc(d.user_agent || 'не передан приложением') + '</code></div>' +
                    (d.hwid ? '<div class="cm-device-field"><span>HWID</span><code class="cm-ua">' + esc(d.hwid) + '</code></div>' : '') +
                    '<p class="cm-note">Удаление освободит место в лимите. Саму подписку это не отменит.</p>' +
                    '<button type="button" class="cm-btn cm-btn-danger" data-cm-hwid="' + esc(d.hwid) + '">Удалить устройство</button>' +
                '</div></details>';
        }).join('');
        Array.prototype.forEach.call(list.querySelectorAll('[data-cm-hwid]'), bindDelete);
    }
    function bindDelete(btn) {
        var original = btn.textContent;
        btn.addEventListener('click', function () {
            if (!btn.classList.contains('is-armed')) {
                btn.classList.add('is-armed');
                btn.textContent = 'Точно удалить?';
                setTimeout(function () { if (document.body.contains(btn) && !btn.disabled) { btn.classList.remove('is-armed'); btn.textContent = original; } }, 3500);
                return;
            }
            btn.disabled = true;
            btn.textContent = '…';
            var body = new URLSearchParams({ hwid: btn.getAttribute('data-cm-hwid') });
            MiNetwork.requestJSON('/api/cabinet/devices/delete/', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrfToken() },
                body: body,
            }).then(function (payload) {
                if (payload.status !== 'ok') throw new Error(payload.message || 'fail');
                state.devices = payload;
                renderDevices(payload);
                updateDevicesCount(payload);
                toast('Устройство удалено');
            }).catch(function (e) {
                btn.disabled = false;
                btn.classList.remove('is-armed');
                btn.textContent = original;
                toast(e && e.status ? e.message : 'Не удалось удалить. Обновите список и попробуйте снова.');
            });
        });
    }
    var devicesPromise = null;
    function loadDevices(render) {
        if (render && state.devices) renderDevices(state.devices);
        if (devicesPromise) return devicesPromise;
        devicesPromise = MiNetwork.requestJSON('/api/cabinet/devices/', { credentials: 'same-origin' })
            .then(function (payload) {
                if (payload.status !== 'ok') throw new Error(payload.message || 'fail');
                state.devices = payload;
                updateDevicesCount(payload);
                if (state.page === 'devices') renderDevices(payload);
                return payload;
            })
            .catch(function () {
                updateDevicesCount(null);
                if (state.page === 'devices' && !state.devices) {
                    var list = document.getElementById('cm-devices-list');
                    if (list) list.innerHTML = '<div class="cm-empty">Не удалось загрузить устройства.<button type="button" class="cm-link" data-cm-devices-retry>Повторить</button></div>';
                }
                return null;
            })
            .then(function (payload) { devicesPromise = null; return payload; });
        return devicesPromise;
    }
    root.addEventListener('click', function (event) {
        if (event.target.closest('[data-cm-devices-retry]')) loadDevices(true);
    });
    loadDevices(false);

    // ----- Покупка -----
    function formatRub(value) { return Math.round(value).toLocaleString('ru-RU') + ' ₽'; }
    function updateTotal() {
        var checked = root.querySelector('input.cm-tariff-radio:checked');
        if (!checked) return;
        var price = parseInt(checked.getAttribute('data-price') || '0', 10) || 0;
        var total = price;
        if (state.discount > 0 && state.discount < 100) total = Math.max(1, Math.round(price * (100 - state.discount) / 100));
        var saved = document.getElementById('cm-total-saved');
        if (saved) saved.textContent = '−' + formatRub(price - total);
        var totalEl = document.getElementById('cm-total-price');
        if (totalEl) totalEl.textContent = formatRub(total);
        var submitPrice = document.getElementById('cm-buy-submit-price');
        if (submitPrice) submitPrice.textContent = formatRub(total);
    }
    Array.prototype.forEach.call(root.querySelectorAll('input.cm-tariff-radio'), function (input) {
        input.addEventListener('change', updateTotal);
    });
    updateTotal();

    // ----- Промокод -----
    var promoForm = document.getElementById('cm-promo-form');
    var promoInput = document.getElementById('cm-promo-code');
    var promoSubmit = document.getElementById('cm-promo-submit');
    var promoResult = document.getElementById('cm-promo-result');
    function showPromoResult(text, ok) {
        if (!promoResult) return;
        promoResult.hidden = false;
        promoResult.textContent = text;
        promoResult.classList.toggle('is-ok', !!ok);
        promoResult.classList.toggle('is-error', !ok);
    }
    if (promoInput && promoSubmit) {
        promoInput.addEventListener('input', function () { promoSubmit.disabled = !promoInput.value.trim(); });
    }
    if (promoForm) promoForm.addEventListener('submit', function (event) {
        event.preventDefault();
        var code = promoInput ? promoInput.value.trim() : '';
        if (!code) return;
        promoSubmit.disabled = true;
        promoSubmit.textContent = 'Проверяем…';
        var body = new URLSearchParams({ code: code });
        MiNetwork.requestJSON(promoForm.getAttribute('action'), {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrfToken() },
            body: body,
        }, 15000).then(function (payload) {
            if (payload.status !== 'ok') throw Object.assign(new Error(payload.message || 'Не удалось активировать промокод'), { status: 400 });
            if (payload.promo_type === 'days') {
                showPromoResult('Готово: к подписке добавлено ' + payload.value + ' дн. Обновляем данные…', true);
                setTimeout(function () { window.location.reload(); }, 1800);
                return;
            }
            state.discount = payload.value;
            root.setAttribute('data-cm-discount', String(payload.value));
            updateTotal();
            showPromoResult('Готово: скидка ' + payload.value + '% на следующую оплату. Она применится на экране покупки.', true);
            promoInput.value = '';
        }).catch(function (e) {
            showPromoResult(e && e.message ? e.message : 'Сервис временно недоступен, попробуйте позже', false);
        }).then(function () {
            promoSubmit.textContent = 'Активировать';
            promoSubmit.disabled = !(promoInput && promoInput.value.trim());
        });
    });

    // ----- Перевыпуск ключа -----
    var reissueBtn = document.getElementById('cm-reissue-confirm');
    if (reissueBtn) reissueBtn.addEventListener('click', function () {
        reissueBtn.disabled = true;
        reissueBtn.textContent = 'Перевыпускаем…';
        MiNetwork.requestJSON('/api/cabinet/subscription/reissue/', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'X-CSRFToken': csrfToken() },
        }, 20000).then(function (payload) {
            if (payload.status !== 'ok') throw new Error(payload.message || 'fail');
            reissueBtn.textContent = 'Готово';
            toast('Ключ перевыпущен. Обновляем ссылки…');
            setTimeout(function () { window.location.reload(); }, 1200);
        }).catch(function (e) {
            reissueBtn.disabled = false;
            reissueBtn.textContent = 'Перевыпустить';
            toast(e && e.status ? e.message : 'Сервис временно недоступен, попробуйте позже');
        });
    });

    // ----- Автопродление -----
    var autopayToggle = document.getElementById('cm-autopay-toggle');
    if (autopayToggle) autopayToggle.addEventListener('change', function () {
        if (!autopayToggle.checked) {
            // Отключение — только через подтверждение (как в боте); тумблер
            // вернётся в «выкл» после перезагрузки страницы.
            autopayToggle.checked = true;
            if (typeof window.openAutopaySheet === 'function') window.openAutopaySheet();
        }
    });

    // ----- Кнопки «Скопировать» (мобильный кабинет и десктопная карточка рефералов) -----
    Array.prototype.forEach.call(document.querySelectorAll('[data-mi3-copy]'), function (btn) {
        var original = btn.innerHTML;
        var restoreTimer = null;
        btn.addEventListener('click', function () {
            copyText(btn.getAttribute('data-mi3-copy') || '').then(function (ok) {
                btn.innerHTML = ok ? '<i class="fas fa-check"></i> Скопировано' : '<i class="far fa-copy"></i> Не удалось';
                clearTimeout(restoreTimer);
                restoreTimer = setTimeout(function () { btn.innerHTML = original; }, 2000);
            });
        });
    });

    // ----- Поделиться ссылкой -----
    var share = document.getElementById('cm-ref-share');
    if (share) share.addEventListener('click', function () {
        var url = share.getAttribute('data-cm-share') || '';
        if (navigator.share) {
            navigator.share({ title: 'Monkey Island VPN', text: 'Подключайся к Monkey Island VPN по моей ссылке', url: url }).catch(function () {});
            return;
        }
        copyText(url).then(function (ok) { toast(ok ? 'Ссылка скопирована' : 'Не удалось скопировать'); });
    });
})();
