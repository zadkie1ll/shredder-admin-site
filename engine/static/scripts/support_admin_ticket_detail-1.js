const chatError = document.getElementById('admin-chat-error');
        function showChatError(message) {
            chatError.textContent = message || '';
            chatError.hidden = !message;
        }
        const adminChat = document.getElementById('admin-chat-scroll');

        function escapeHtml(value) {
            return String(value || '')
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;')
                .replaceAll("'", '&#039;');
        }

        function attachmentHtml(attachment) {
            const url = escapeHtml(attachment.url);
            const fileName = escapeHtml(attachment.file_name);

            if (attachment.is_image) {
                return `
                    <a href="${url}" target="_blank" class="attachment" title="${fileName}">
                        <img src="${url}" alt="${fileName}">
                    </a>
                `;
            }

            if (attachment.is_video) {
                return `
                    <a href="${url}" target="_blank" class="attachment" title="${fileName}">
                        <video src="${url}" controls preload="metadata"></video>
                    </a>
                `;
            }

            return `
                <a href="${url}" target="_blank" class="attachment" title="${fileName}">
                    <div class="p-3 text-xs font-bold text-gray-300">
                        <i class="fas fa-paperclip mr-2 text-[#ffc700]"></i>${fileName}
                    </div>
                </a>
            `;
        }

        function messageHtml(message) {
            const senderLabel = message.is_user ? 'Пользователь' : 'Поддержка';
            const senderClass = message.is_user ? 'text-gray-400' : 'text-[#ffc700]';
            const bubbleClass = message.is_user ? 'msg-user' : 'msg-support';
            const attachments = message.attachments && message.attachments.length
                ? `<div class="attachment-grid">${message.attachments.map(attachmentHtml).join('')}</div>`
                : '';

            return `
                <div class="msg ${bubbleClass}" data-message-id="${message.id}">
                    <div class="flex items-center justify-between gap-3 mb-2">
                        <span class="text-xs font-bold ${senderClass}">${senderLabel}</span>
                        <span class="text-[10px] text-gray-500 font-bold">${escapeHtml(message.created_at_full || message.created_at)}</span>
                    </div>
                    <p class="text-sm text-white/90 font-bold leading-relaxed whitespace-pre-wrap">${escapeHtml(message.message)}</p>
                    ${attachments}
                </div>
            `;
        }

        let knownMessageIds = new Set();
        let messagesRefreshInFlight = false;
        let soundReady = false;
        let audioContext = null;

        async function unlockSound(playTest = false) {
            soundReady = true;
            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) return;

            audioContext = audioContext || new AudioContextClass();
            if (audioContext.state === 'suspended') {
                await audioContext.resume().catch(() => {});
            }
            const button = null;
            if (button) button.remove();
            if (playTest) playMessageSound();
        }

        function installSoundButton() {
            return;
        }

        function playMessageSound() {
            if (!soundReady) return;

            const AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) return;

            const context = audioContext || new AudioContextClass();
            audioContext = context;
            const now = context.currentTime;
            const gain = context.createGain();
            gain.connect(context.destination);
            gain.gain.setValueAtTime(0.0001, now);
            gain.gain.exponentialRampToValueAtTime(0.08, now + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.28);

            [784, 1046].forEach((frequency, index) => {
                const oscillator = context.createOscillator();
                oscillator.type = 'sine';
                oscillator.frequency.setValueAtTime(frequency, now + index * 0.09);
                oscillator.connect(gain);
                oscillator.start(now + index * 0.09);
                oscillator.stop(now + index * 0.09 + 0.16);
            });
        }

        function renderMessages(messages) {
            if (!adminChat) return;

            const shouldStickToBottom = adminChat.scrollTop + adminChat.clientHeight >= adminChat.scrollHeight - 80;
            if (!messages.length) {
                if (!adminChat.querySelector('[data-message-id]')) {
                    adminChat.innerHTML = `
                        <div data-empty-messages class="h-full flex items-center justify-center text-gray-600 font-bold">
                            Сообщений пока нет.
                        </div>
                    `;
                }
                return;
            }

            const unseenMessages = messages.filter((message) => (
                !knownMessageIds.has(String(message.id))
            ));
            if (!unseenMessages.length) return;

            const emptyState = adminChat.querySelector('[data-empty-messages]');
            if (emptyState) emptyState.remove();
            adminChat.insertAdjacentHTML(
                'beforeend',
                unseenMessages.map(messageHtml).join(''),
            );
            if (shouldStickToBottom) {
                adminChat.scrollTop = adminChat.scrollHeight;
            }
        }

        async function refreshMessages() {
            const form = document.getElementById('admin-support-message-form');
            if (!form || !form.dataset.messagesUrl || document.hidden || messagesRefreshInFlight) return;

            messagesRefreshInFlight = true;
            try {
                const payload = await MiNetwork.requestJSON(form.dataset.messagesUrl, {
                    headers: {'X-Requested-With': 'XMLHttpRequest'},
                    cache: 'no-store',
                });
                showChatError('');
                if (payload.status === 'ok') {
                    const messages = payload.messages || [];
                    const hasNewUserMessage = messages.some((message) => (
                        message.is_user && !knownMessageIds.has(String(message.id))
                    ));
                    renderMessages(messages);
                    knownMessageIds = new Set(messages.map((message) => String(message.id)));
                    if (hasNewUserMessage) {
                        playMessageSound();
                    }
                }
            } catch (error) {
                showChatError('Не удалось обновить сообщения. Повторим попытку автоматически.');
            } finally {
                messagesRefreshInFlight = false;
            }
        }

        function setupAdminMessageForm() {
            const form = document.getElementById('admin-support-message-form');
            if (!form) return;

            form.addEventListener('submit', async (event) => {
                event.preventDefault();
                const submitButton = form.querySelector('button[type="submit"]');
                const originalDisabled = submitButton ? submitButton.disabled : false;

                if (submitButton && submitButton.disabled) return;
                if (submitButton) submitButton.disabled = true;
                showChatError('');
                try {
                    // Ответ с вложениями (видео до 25 МиБ) грузится дольше 10 с
                    // таймаута requestJSON по умолчанию: даём 180 с на отправку.
                    // Опрос сообщений (refreshMessages) остаётся с коротким таймаутом.
                    await MiNetwork.requestJSON(form.action, {
                        method: 'POST',
                        body: new FormData(form),
                        headers: {'X-Requested-With': 'XMLHttpRequest'},
                    }, 180000);

                    form.reset();
                    await refreshMessages();
                    const textarea = form.querySelector('textarea[name="message"]');
                    if (textarea) textarea.focus();
                } catch (error) {
                    showChatError(error.status ? error.message : 'Ответ не получен. Проверьте сообщения перед повторной отправкой.');
                } finally {
                    if (submitButton) submitButton.disabled = originalDisabled;
                }
            });
        }

        function setupQuickReplies() {
            const quickReplies = document.getElementById('quick-replies');
            const toggle = document.getElementById('quick-replies-toggle');
            const form = document.getElementById('admin-support-message-form');
            const textarea = form ? form.querySelector('textarea[name="message"]') : null;
            if (!quickReplies || !toggle || !textarea) return;

            toggle.addEventListener('click', (event) => {
                event.stopPropagation();
                quickReplies.classList.toggle('open');
            });

            quickReplies.querySelectorAll('.quick-reply-item').forEach((button) => {
                button.addEventListener('click', () => {
                    const bodyNode = button.querySelector('.quick-reply-body');
                    const body = (bodyNode && bodyNode.textContent) || '';
                    const current = textarea.value.trim();
                    textarea.value = current ? `${current}\n\n${body}` : body;
                    textarea.focus();
                    quickReplies.classList.remove('open');
                });
            });

            document.addEventListener('click', (event) => {
                if (!event.target.closest('#quick-replies')) {
                    quickReplies.classList.remove('open');
                }
            });
        }

        if (adminChat) {
            adminChat.scrollTop = adminChat.scrollHeight;
            adminChat.querySelectorAll('[data-message-id]').forEach((node) => {
                knownMessageIds.add(String(node.dataset.messageId));
            });
        }
        setupAdminMessageForm();
        setupQuickReplies();
        setInterval(refreshMessages, 7000);
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) refreshMessages();
        });
