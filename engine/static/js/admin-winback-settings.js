/* Win-back's unified editor uses the existing per-key runtime API.
 * There is no batch transaction: acknowledge each successful key, stop on
 * failure, and retain the remaining draft. Never infer effective defaults.
 */
(() => {
    'use strict';
    const editors = new WeakMap();
    const labels = {
        winback_enabled: 'Win-back',
        winback_price_month: 'Цена на 1 месяц, ₽',
        winback_price_threemonths: 'Цена на 3 месяца, ₽',
        winback_price_year: 'Цена на 12 месяцев, ₽',
        winback_send_hour_start: 'Начало окна отправки, час МСК',
        winback_send_hour_end: 'Конец окна отправки, час МСК',
        winback_offer_ttl_hours: 'Срок предложения, часов',
        winback_grace_hours: 'Грейс-период, часов',
    };
    const isHour = key => key === 'winback_send_hour_start' || key === 'winback_send_hour_end';

    function getEditor(container) {
        let editor = editors.get(container);
        if (!editor) {
            editor = {container, draft: new Map(), busy: false, revision: 0, loadRequest: 0};
            editors.set(container, editor);
        }
        return editor;
    }

    function currentLoad(editor, token) {
        return !token || (token.revision === editor.revision && token.request === editor.loadRequest);
    }

    function collectDraft(editor) {
        editor.container.querySelectorAll('[data-winback-key]').forEach(row => {
            const key = row.dataset.winbackKey;
            if (editor.draft.get(key)?.action === 'delete') return;
            const input = row.querySelector('[data-setting-control]');
            if (input.value !== input.dataset.initialValue) {
                editor.draft.set(key, {action: 'save', value: input.value});
            } else editor.draft.delete(key);
        });
    }

    function markDraft(editor) {
        editor.container.querySelectorAll('[data-winback-key]').forEach(row => {
            const draft = editor.draft.get(row.dataset.winbackKey);
            const indicator = row.querySelector('[data-winback-dirty]');
            indicator.hidden = !draft;
            indicator.textContent = draft?.action === 'delete' ? 'Сброс после сохранения' : 'Не сохранено';
        });
    }

    function message(editor, text, error = false) {
        const status = editor.container.querySelector('[data-winback-status]');
        status.textContent = text;
        status.classList.toggle('is-error', error);
    }

    function draw(editor) {
        const {container, rows, options, draft} = editor;
        const esc = options.escapeHtml;
        const label = key => labels[key] || key;
        const detailsOpen = container.querySelector('[data-winback-details]')?.open;
        container.innerHTML = `<form data-winback-form novalidate aria-busy="false">
            <section class="card system-card system-settings-card winback-settings-card">
                <h2 class="system-card-title">Предложения для возвращения</h2>
                <fieldset class="winback-fields" aria-label="Настройки Win-back">
                    ${rows.map(setting => `<div class="winback-setting" data-winback-key="${esc(setting.key)}">
                        <div class="winback-setting-label"><label for="winback-${esc(setting.key)}">${esc(label(setting.key))}</label>
                            <span data-winback-dirty hidden></span></div>
                        <div class="winback-setting-value">${options.controlHtml(setting)}</div>
                    </div>`).join('')}
                </fieldset>
            </section>
            <div class="winback-settings-footer">
                <span data-winback-status role="status" aria-live="polite"></span>
                <button class="btn btn-primary" type="submit" data-winback-save>Сохранить изменения</button>
            </div>
            <details class="winback-settings-details" data-winback-details ${detailsOpen ? 'open' : ''}>
                <summary>Описание и источники настроек</summary>
                <p>Сброс вернёт настройку к env/default после сохранения изменений.</p>
                <p>Окно отправки действует на все проактивные уведомления: Win-back и напоминания о подписке. Часы указаны по МСК; конец не включён. Одинаковые часы разрешают отправку круглосуточно, начало позже конца — через полночь.</p>
                ${rows.map(setting => `<div class="winback-setting-details">
                    <div><strong>${esc(label(setting.key))}</strong>
                        <p id="winback-description-${esc(setting.key)}">${esc(setting.description || setting.type)}</p>
                        <code>${esc(setting.key)}</code> <span>${esc(setting.type)}</span>
                        <div class="setting-row-origin">${setting.is_set ? 'БД' : 'env/default'}${setting.is_set && setting.updated_at ? ` · ${esc(setting.updated_at)}` : ''}</div>
                    </div>
                    <button class="btn" type="button" data-winback-reset="${esc(setting.key)}" ${setting.is_set || draft.get(setting.key)?.action === 'delete' ? '' : 'disabled'}>${draft.get(setting.key)?.action === 'delete' ? 'Отменить сброс' : 'Сбросить к env/default'}</button>
                </div>`).join('')}
            </details>
        </form>`;
        container.querySelectorAll('[data-winback-key]').forEach(row => {
            const key = row.dataset.winbackKey;
            const input = row.querySelector('[data-setting-control]');
            input.id = `winback-${key}`;
            input.setAttribute('aria-label', label(key));
            input.setAttribute('aria-describedby', `winback-description-${key}`);
            if (input.type === 'number') {
                input.min = isHour(key) ? '0' : '1';
                if (isHour(key)) input.max = '23';
            }
            const pending = draft.get(key);
            if (pending) input.value = pending.value;
            input.disabled = pending?.action === 'delete';
            // An absent DB value means fallback, not false or zero.
            const blank = input.querySelector('option[value=""]');
            if (blank) blank.textContent = 'env/default';
        });
        markDraft(editor);
        const form = container.querySelector('[data-winback-form]');
        const onEdit = event => {
            if (!event.target.matches('[data-setting-control]')) return;
            event.target.setCustomValidity('');
            collectDraft(editor);
            markDraft(editor);
            message(editor, editor.draft.size ? 'Изменения ещё не сохранены' : '');
        };
        form.addEventListener('input', onEdit);
        form.addEventListener('change', onEdit);
        form.addEventListener('submit', event => { event.preventDefault(); save(editor); });
        form.addEventListener('click', event => {
            const button = event.target.closest('[data-winback-reset]');
            if (!button || editor.busy) return;
            collectDraft(editor);
            const key = button.dataset.winbackReset;
            const input = document.getElementById(`winback-${key}`);
            const previous = draft.get(key);
            if (previous?.action === 'delete') {
                if (previous.value !== input.dataset.initialValue) draft.set(key, {action: 'save', value: previous.value});
                else draft.delete(key);
            } else draft.set(key, {action: 'delete', value: input.value});
            draw(editor);
            message(editor, draft.size ? 'Изменения ещё не сохранены' : '');
            container.querySelector(`[data-winback-reset="${key}"]`).focus();
        });
    }

    async function persist(editor, key, change) {
        const body = new FormData();
        body.set('key', key);
        body.set('action', change.action);
        if (change.action === 'save') body.set('value', String(Number(change.value)));
        let rejected = false;
        try {
            const response = await fetch(editor.options.url, {
                method: 'POST', body,
                headers: {'X-CSRFToken': editor.options.csrfToken, 'X-Requested-With': 'XMLHttpRequest'},
            });
            const payload = await response.json();
            if (!response.ok || payload.status !== 'ok') {
                rejected = true;
                throw new Error(payload.message || `Не удалось сохранить «${labels[key] || key}»`);
            }
            if (change.action === 'save' && payload.setting?.key !== key) throw new Error('Сервер не подтвердил новое значение настройки');
            return payload.setting;
        } catch (error) {
            if (rejected) throw error;
            // A lost response does not prove that the server rejected the write.
            // Reconcile only this key, without reloading or discarding drafts.
            try {
                const response = await fetch(editor.options.url, {cache: 'no-store', headers: {'X-Requested-With': 'XMLHttpRequest'}});
                const payload = await response.json();
                const setting = payload.settings?.find(row => row.key === key);
                const matches = change.action === 'delete' ? setting?.is_set === false
                    : setting?.is_set && String(setting.value) === String(Number(change.value));
                if (response.ok && matches) return setting;
            } catch (_) { /* Keep the draft when reconciliation is unavailable. */ }
            throw new Error(`Не удалось подтвердить сохранение «${labels[key] || key}». Проверьте соединение и повторите сохранение.`);
        }
    }

    async function save(editor) {
        if (editor.busy) return;
        collectDraft(editor);
        if (!editor.draft.size) { message(editor, 'Нет изменений для сохранения'); return; }
        // Validate every changed value before making the first per-key request.
        for (const [key, change] of editor.draft) {
            if (change.action === 'delete') continue;
            const input = document.getElementById(`winback-${key}`);
            const n = Number(change.value);
            const valid = key === 'winback_enabled' ? ['0', '1'].includes(change.value)
                : change.value.trim() !== '' && Number.isSafeInteger(n) && (isHour(key) ? n >= 0 && n <= 23 : n > 0);
            input.setCustomValidity(valid ? '' : isHour(key) ? 'Укажите целый час от 0 до 23' : 'Укажите целое число больше нуля');
            if (!input.reportValidity()) { message(editor, `Проверьте поле «${labels[key] || key}»`, true); return; }
        }
        // Disable first when requested, enable (or restore its fallback) last.
        // Do not expose a newly enabled chain to an unfinished price edit.
        const order = ([key, change]) => key !== 'winback_enabled' ? 0 : change.action === 'save' && change.value === '0' ? -1 : 1;
        const changes = [...editor.draft].sort((a, b) => order(a) - order(b));
        editor.revision++;
        editor.busy = true;
        editor.container.querySelector('[data-winback-form]').setAttribute('aria-busy', 'true');
        editor.container.querySelector('fieldset').disabled = true;
        editor.container.querySelectorAll('button').forEach(button => { button.disabled = true; });
        const saveButton = editor.container.querySelector('[data-winback-save]');
        saveButton.textContent = 'Сохраняем…';
        let saved = 0, failure = '';
        try {
            for (const [key, change] of changes) {
                message(editor, `Сохраняем ${saved + 1} из ${changes.length}…`);
                const setting = await persist(editor, key, change);
                const index = editor.rows.findIndex(row => row.key === key);
                if (change.action === 'delete') {
                    editor.rows[index] = {...editor.rows[index], value: '', display_value: '', is_set: false, updated_at: ''};
                } else {
                    editor.rows[index] = setting;
                }
                editor.draft.delete(key);
                saved++;
            }
        } catch (error) {
            failure = error instanceof TypeError ? 'Не удалось подтвердить сохранение. Проверьте соединение.' : error.message;
        } finally {
            editor.revision++;
            editor.busy = false;
            draw(editor);
            message(editor, failure ? `Сохранено ${saved} из ${changes.length}. ${failure} Несохранённые изменения остались в форме.` : 'Изменения сохранены', Boolean(failure));
            editor.container.querySelector('[data-winback-save]').focus({preventScroll: true});
        }
    }

    window.AdminWinbackSettings = {
        beginLoad(container) {
            const editor = getEditor(container);
            collectDraft(editor);
            return {revision: editor.revision, request: ++editor.loadRequest};
        },
        loadError(container, token, text) {
            const editor = editors.get(container);
            if (!editor) return false;
            if (editor.busy || !currentLoad(editor, token)) return true;
            if (!container.querySelector('[data-winback-form]')) return false;
            message(editor, text, true);
            return true;
        },
        render(container, rows, options) {
            const editor = getEditor(container);
            // Also reject GETs that began before or during a completed save.
            if (editor.busy || !currentLoad(editor, options.loadToken)) return;
            collectDraft(editor);
            editor.rows = rows.map(row => ({...row}));
            editor.options = options;
            if (!rows.length) { container.innerHTML = '<div class="info-card muted">Настроек нет.</div>'; return; }
            draw(editor);
            if (editor.draft.size) message(editor, 'Изменения ещё не сохранены');
        },
    };
})();
