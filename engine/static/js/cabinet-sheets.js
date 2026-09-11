/* Shared lifecycle for cabinet sheets. No subscription or payment mutations. */
class CabinetSheets {
    constructor({ overlay, sheets, background, onChange }) {
        this.overlay = overlay;
        this.sheets = [...sheets];
        this.background = background;
        this.onChange = onChange;
        this.active = null;
        this.returnFocus = null;
        this.historyPending = false;
        this.sheets.forEach(sheet => {
            sheet.inert = true;
            sheet.tabIndex = -1;
            sheet.setAttribute('aria-hidden', 'true');
            let startY = null;
            sheet.addEventListener('touchstart', event => {
                // A list scroll must never dismiss the sheet.
                startY = event.target.closest('.mi3-sheet-grab, .mi3-sheet-top')
                    && !event.target.closest('button') ? event.touches[0].clientY : null;
            }, { passive: true });
            sheet.addEventListener('touchend', event => {
                if (startY !== null && event.changedTouches[0].clientY - startY > 70) this.close();
                startY = null;
            }, { passive: true });
            sheet.addEventListener('touchcancel', () => { startY = null; });
        });
        window.addEventListener('popstate', () => {
            this.historyPending = false;
            this.dismiss();
        });
        document.addEventListener('keydown', event => {
            if (!this.active) return;
            if (event.key === 'Escape') { event.preventDefault(); this.close(); return; }
            if (event.key !== 'Tab') return;
            const nodes = [...this.active.querySelectorAll('button, summary, a[href], input, select, textarea, [tabindex="0"]')]
                .filter(node => !node.disabled && node.getClientRects().length && getComputedStyle(node).visibility === 'visible');
            const first = nodes[0] || this.active;
            const last = nodes[nodes.length - 1] || this.active;
            if (event.shiftKey && (document.activeElement === first || !this.active.contains(document.activeElement) || document.activeElement === this.active)) {
                event.preventDefault(); last.focus();
            } else if (!event.shiftKey && (document.activeElement === last || !this.active.contains(document.activeElement) || document.activeElement === this.active)) {
                event.preventDefault(); first.focus();
            }
        });
    }

    open(sheet) {
        if (this.historyPending || !sheet || this.active === sheet) return;
        if (!this.active) {
            this.returnFocus = document.activeElement;
            this.previousOverflow = this.background.style.overflowY;
            this.previousInert = this.background.inert;
            history.pushState({ ...history.state, cabinetSheet: true }, '');
        } else {
            this.active.classList.remove('is-open');
            this.active.inert = true;
            this.active.setAttribute('aria-hidden', 'true');
        }
        this.active = sheet;
        this.background.style.overflowY = 'hidden';
        this.background.inert = true;
        this.overlay.classList.add('is-open');
        sheet.inert = false;
        sheet.setAttribute('aria-hidden', 'false');
        sheet.classList.add('is-open');
        const first = [...sheet.querySelectorAll('.mi3-sheet-close')].find(node => !node.disabled && node.getClientRects().length && getComputedStyle(node).visibility === 'visible');
        (first || sheet).focus({ preventScroll: true });
        this.onChange();
    }

    close() {
        if (!this.active || this.historyPending) return;
        this.dismiss();
        if (history.state?.cabinetSheet) {
            this.historyPending = true;
            history.back();
        }
    }

    dismiss() {
        if (!this.active) return;
        this.active.classList.remove('is-open');
        this.active.inert = true;
        this.active.setAttribute('aria-hidden', 'true');
        this.active = null;
        this.overlay.classList.remove('is-open');
        this.background.style.overflowY = this.previousOverflow;
        this.background.inert = this.previousInert;
        if (this.returnFocus?.isConnected) this.returnFocus.focus({ preventScroll: true });
        this.onChange();
    }
}
