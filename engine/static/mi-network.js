/* A deadline includes response-body reading, not just receipt of headers.
   Синтаксис держим на уровне ES2019 (без optional chaining, nullish-оператора
   и глобального объекта из ES2020): скрипт подключается на странице статуса
   оплаты и должен разбираться старыми мобильными браузерами и WebView. */
(function (root) {
    'use strict';
    async function requestJSON(url, options = {}, timeoutMs = 10000) {
        // Без AbortController (старые Safari/WebView) работаем как обычный
        // fetch без таймаута, а не падаем при создании контроллера.
        const controller = typeof AbortController === 'function' ? new AbortController() : null;
        const external = options.signal;
        const abort = () => { if (controller) controller.abort(); };
        const fetchOptions = Object.assign({}, options);
        if (controller) {
            fetchOptions.signal = controller.signal;
            if (external) {
                if (external.aborted) abort();
                external.addEventListener('abort', abort, {once: true});
            }
        }
        const timer = controller ? setTimeout(abort, timeoutMs) : null;
        try {
            const response = await fetch(url, fetchOptions);
            let payload;
            if (!response.ok) {
                const error = new Error('Не удалось выполнить запрос');
                error.status = response.status;
                try { payload = await response.json(); } catch (_) { /* nginx may return HTML */ }
                error.message = (payload && payload.message) || (response.status === 413
                    ? 'Вложения слишком большие. Уменьшите размер файлов.'
                    : response.status === 401 || response.status === 403
                        ? 'Сессия истекла или доступ ограничен. Обновите страницу.'
                        : 'Не удалось выполнить запрос. Попробуйте позже.');
                throw error;
            }
            return await response.json();
        } finally {
            if (timer !== null) clearTimeout(timer);
            if (controller && external) external.removeEventListener('abort', abort);
        }
    }
    root.MiNetwork = {requestJSON};
    if (typeof module !== 'undefined' && module.exports) module.exports = root.MiNetwork;
})(typeof window !== 'undefined' ? window : this);
