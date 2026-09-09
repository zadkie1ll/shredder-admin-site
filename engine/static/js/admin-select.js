/* Admin select: progressive enhancement over native <select>.
 *
 * The native element stays in the DOM (forms, existing `change` listeners,
 * Playwright's selectOption and screen readers keep working) but is made
 * transparent under a styled trigger; the option list is rendered in an
 * admin-styled popover appended to <body>, so cards with overflow:hidden do
 * not clip it. Choosing an option sets select.value and dispatches `input`
 * and `change`, exactly like a native pick. Selects that are populated or
 * re-valued by code are re-synced through a MutationObserver and on the
 * next interaction, so callers never need to know about the widget.
 *
 * Skipped: multiple/size>1 selects, selects inside the date popover, and
 * anything marked data-native-select. On coarse pointers (phones) the native
 * picker is a better control, so the enhancement stays off there too.
 */
(() => {
    'use strict';
    if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) return;

    const registry = new WeakMap();
    let openWidget = null;

    function eligible(select) {
        if (!(select instanceof HTMLSelectElement)) return false;
        if (select.multiple || select.size > 1) return false;
        if (select.hasAttribute('data-native-select')) return false;
        if (select.closest('[data-date-popover], .ui-select')) return false;
        return true;
    }

    function selectedText(select) {
        const option = select.options[select.selectedIndex];
        return option ? option.textContent.trim() : '';
    }

    function syncLabel(widget) {
        const {select, label, wrapper} = widget;
        const text = selectedText(select);
        if (label.textContent !== text) label.textContent = text;
        const placeholder = Boolean(select.selectedIndex >= 0 && select.options[select.selectedIndex].value === '' && select.options[select.selectedIndex].disabled);
        wrapper.classList.toggle('is-placeholder', placeholder);
        wrapper.classList.toggle('is-disabled', select.disabled);
        widget.trigger.disabled = select.disabled;
    }

    function buildMenu(widget) {
        const {select} = widget;
        const menu = document.createElement('div');
        menu.className = 'ui-select-menu';
        menu.setAttribute('role', 'listbox');
        menu.setAttribute('tabindex', '-1');
        const items = [];
        const addOption = (option, index) => {
            const item = document.createElement('div');
            item.className = 'ui-select-option';
            item.setAttribute('role', 'option');
            item.dataset.index = String(index);
            item.textContent = option.textContent.trim();
            if (option.disabled) item.setAttribute('aria-disabled', 'true');
            item.setAttribute('aria-selected', String(index === select.selectedIndex));
            menu.appendChild(item);
            items.push(item);
        };
        let index = 0;
        for (const child of select.children) {
            if (child instanceof HTMLOptGroupElement) {
                const group = document.createElement('div');
                group.className = 'ui-select-group';
                group.textContent = child.label;
                menu.appendChild(group);
                for (const option of child.children) addOption(option, index++);
            } else if (child instanceof HTMLOptionElement) {
                addOption(child, index++);
            }
        }
        widget.menu = menu;
        widget.items = items;
        widget.active = Math.max(0, select.selectedIndex);
        return menu;
    }

    function placeMenu(widget) {
        const {wrapper, menu} = widget;
        const rect = wrapper.getBoundingClientRect();
        const width = Math.max(rect.width, 160);
        menu.style.width = `${Math.round(width)}px`;
        menu.style.left = `${Math.round(Math.min(rect.left, window.innerWidth - width - 8))}px`;
        const height = menu.offsetHeight;
        const below = window.innerHeight - rect.bottom - 8;
        const above = rect.top - 8;
        if (height <= below || below >= above) {
            menu.style.top = `${Math.round(rect.bottom + 4)}px`;
            menu.style.maxHeight = `${Math.max(120, Math.min(360, below))}px`;
            menu.classList.remove('is-above');
        } else {
            menu.style.top = `${Math.round(rect.top - 4 - Math.min(height, Math.min(360, above)))}px`;
            menu.style.maxHeight = `${Math.max(120, Math.min(360, above))}px`;
            menu.classList.add('is-above');
        }
    }

    function highlight(widget, index, scroll = true) {
        if (!widget.items.length) return;
        widget.active = Math.max(0, Math.min(widget.items.length - 1, index));
        widget.items.forEach((item, i) => item.classList.toggle('is-active', i === widget.active));
        if (scroll) widget.items[widget.active].scrollIntoView({block: 'nearest'});
    }

    function step(widget, delta) {
        const {items} = widget;
        let next = widget.active;
        for (let guard = 0; guard < items.length; guard += 1) {
            next = (next + delta + items.length) % items.length;
            if (items[next].getAttribute('aria-disabled') !== 'true') break;
        }
        highlight(widget, next);
    }

    function close(widget, refocus = false) {
        if (!widget || !widget.menu) return;
        widget.menu.remove();
        widget.menu = null;
        widget.items = [];
        widget.wrapper.classList.remove('is-open');
        widget.trigger.setAttribute('aria-expanded', 'false');
        if (openWidget === widget) openWidget = null;
        if (refocus) widget.trigger.focus();
    }

    function commit(widget, index) {
        const {select} = widget;
        const option = select.options[index];
        if (!option || option.disabled) return;
        const changed = select.selectedIndex !== index;
        select.selectedIndex = index;
        syncLabel(widget);
        close(widget, true);
        if (changed) {
            select.dispatchEvent(new Event('input', {bubbles: true}));
            select.dispatchEvent(new Event('change', {bubbles: true}));
        }
    }

    function open(widget) {
        if (widget.select.disabled) return;
        if (openWidget && openWidget !== widget) close(openWidget);
        syncLabel(widget);
        const menu = buildMenu(widget);
        menu.addEventListener('mousedown', (event) => event.preventDefault());
        menu.addEventListener('mousemove', (event) => {
            const item = event.target.closest('.ui-select-option');
            if (item && Number(item.dataset.index) !== widget.active) highlight(widget, Number(item.dataset.index), false);
        });
        menu.addEventListener('click', (event) => {
            const item = event.target.closest('.ui-select-option');
            if (item) commit(widget, Number(item.dataset.index));
        });
        document.body.appendChild(menu);
        widget.wrapper.classList.add('is-open');
        widget.trigger.setAttribute('aria-expanded', 'true');
        openWidget = widget;
        placeMenu(widget);
        highlight(widget, widget.active);
    }

    function typeahead(widget, key) {
        const now = Date.now();
        widget.typed = now - (widget.typedAt || 0) < 700 ? widget.typed + key : key;
        widget.typedAt = now;
        const needle = widget.typed.toLowerCase();
        const start = widget.typed.length === 1 ? widget.active + 1 : widget.active;
        const {items} = widget;
        for (let offset = 0; offset < items.length; offset += 1) {
            const index = (start + offset) % items.length;
            if (items[index].textContent.trim().toLowerCase().startsWith(needle) && items[index].getAttribute('aria-disabled') !== 'true') {
                if (widget.menu) highlight(widget, index); else commit(widget, index);
                return;
            }
        }
    }

    function onKeydown(widget, event) {
        const isOpen = Boolean(widget.menu);
        switch (event.key) {
            case 'ArrowDown':
                event.preventDefault();
                if (!isOpen) open(widget); else step(widget, 1);
                return;
            case 'ArrowUp':
                event.preventDefault();
                if (!isOpen) open(widget); else step(widget, -1);
                return;
            case 'Home':
                if (isOpen) { event.preventDefault(); highlight(widget, 0); }
                return;
            case 'End':
                if (isOpen) { event.preventDefault(); highlight(widget, widget.items.length - 1); }
                return;
            case 'Enter':
            case ' ':
                event.preventDefault();
                if (!isOpen) open(widget); else commit(widget, widget.active);
                return;
            case 'Escape':
                if (isOpen) { event.preventDefault(); event.stopPropagation(); close(widget, true); }
                return;
            case 'Tab':
                if (isOpen) close(widget);
                return;
            default:
                if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
                    event.preventDefault();
                    typeahead(widget, event.key);
                }
        }
    }

    function enhance(select) {
        if (!eligible(select) || registry.has(select)) return;
        const wrapper = document.createElement('span');
        wrapper.className = `ui-select ${select.className}`.trim();
        if (select.getAttribute('style')) wrapper.setAttribute('style', select.getAttribute('style'));
        if (select.id) wrapper.dataset.selectFor = select.id;
        if (select.name) wrapper.dataset.selectName = select.name;
        const trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'ui-select-trigger';
        trigger.setAttribute('aria-haspopup', 'listbox');
        trigger.setAttribute('aria-expanded', 'false');
        const accessibleName = select.getAttribute('aria-label') || select.getAttribute('title') || (select.labels && select.labels[0] ? select.labels[0].textContent.trim() : '');
        if (accessibleName) trigger.setAttribute('aria-label', accessibleName);
        if (select.title) trigger.title = select.title;
        const label = document.createElement('span');
        label.className = 'ui-select-label';
        trigger.appendChild(label);
        select.parentNode.insertBefore(wrapper, select);
        wrapper.appendChild(select);
        wrapper.appendChild(trigger);
        select.tabIndex = -1;
        select.setAttribute('aria-hidden', 'true');
        select.removeAttribute('style');
        select.classList.add('ui-select-native');

        const widget = {select, wrapper, trigger, label, menu: null, items: [], active: 0, typed: '', typedAt: 0};
        registry.set(select, widget);
        syncLabel(widget);

        trigger.addEventListener('click', () => (widget.menu ? close(widget) : open(widget)));
        trigger.addEventListener('keydown', (event) => onKeydown(widget, event));
        trigger.addEventListener('blur', () => { if (widget.menu) close(widget); });
        select.addEventListener('change', () => syncLabel(widget));
        select.addEventListener('input', () => syncLabel(widget));
        new MutationObserver(() => {
            syncLabel(widget);
            if (widget.menu) { close(widget); }
        }).observe(select, {childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'selected', 'value']});
        // Labels pointing at the select should focus the trigger instead.
        if (select.id) {
            document.querySelectorAll(`label[for="${CSS.escape(select.id)}"]`).forEach((node) => node.addEventListener('click', (event) => {
                event.preventDefault();
                trigger.focus();
            }));
        }
    }

    function sync(root) {
        (root || document).querySelectorAll('select').forEach((select) => {
            const widget = registry.get(select);
            if (widget) syncLabel(widget); else enhance(select);
        });
    }

    function init() {
        sync(document);
        new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                for (const node of mutation.addedNodes) {
                    if (!(node instanceof Element)) continue;
                    if (node.matches('select')) enhance(node);
                    else node.querySelectorAll('select').forEach(enhance);
                }
            }
        }).observe(document.body, {childList: true, subtree: true});
        // Programmatic `select.value = …` fires no event; re-read labels on
        // the next interaction so the trigger never shows a stale value.
        document.addEventListener('focusin', () => sync(document), true);
        document.addEventListener('pointerdown', (event) => {
            if (openWidget && !openWidget.wrapper.contains(event.target) && !openWidget.menu.contains(event.target)) close(openWidget);
            sync(document);
        }, true);
        window.addEventListener('resize', () => { if (openWidget) placeMenu(openWidget); });
        window.addEventListener('scroll', (event) => {
            if (openWidget && !(event.target instanceof Element && openWidget.menu.contains(event.target))) placeMenu(openWidget);
        }, true);
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && openWidget) close(openWidget, true);
        });
    }

    window.adminSelect = {sync, enhance};
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
