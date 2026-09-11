# Аудит Monkey Island Website и план исправлений

Дата: 10 сентября 2026. Проверена текущая рабочая копия сайта; контрольный commit к концу проверки — `7e4ea2284bead9f5a26faa53db0a3abf16f0b43e`. На старте были пользовательские незакоммиченные изменения; они включены в проверенную реализацию. Во время составления отчёта появились дополнительные правки реферальных текстов, select рассылки и тестов; их diff проверен, описанные ниже дефекты он не устраняет. Размеры файлов приведены для момента замера, а не непрерывно меняющегося дерева. Код приложения не исправлялся на момент аудита; актуальный статус реализации приведён ниже.

В плане **63 отдельные задачи**: **2 P0**, **21 P1**, **40 P2**. Это общий реестр багов, укрепления защиты и направлений ускорения; не все пункты являются воспроизведёнными инцидентами. Дополнительные проверки среды вынесены в конец.

На момент аудита самые срочные проблемы были связаны с доступом к аккаунтам и корректностью оплаченных подписок. Оплата на чужой email могла дать доступ к этому аккаунту; статус платежа мог быть взят из другого заказа/шлюза того же пользователя. Были найдены пути сохранённого XSS в админке, рассогласования email с Remnawave, зависания web-потоков и ошибки установки нод. На маленьком мобильном экране воспроизводилась блокировка кнопки онбординга.

«Все баги» нельзя честно гарантировать одним аудитом. Здесь разделены воспроизведённые дефекты, доказанные ветки кода, структурные издержки и риски, которые ещё требуют измерений/проверки среды. Отсутствие пункта о функции не означает доказанное отсутствие багов. Скорость каждой функции в production без трассировки, данных и нагрузки не измерена; обещания «ускорить на X%» не даются.

## Статус реализации на 11 сентября 2026

После повторной проверки исправлены R01–R08: Telegram payload, устойчивое
сопоставление платежей и восстановление неизвестного исхода, подтверждённый вход
перед анонимной оплатой, одноразовая смена email с очередью синхронизации,
пагинация/ETag поддержки и сетевые ошибки. Растры и nginx из R09 проверены;
полная проверка видео остаётся. Браузер подтвердил фокус диалогов и возврат фокуса.
Добавлены пакетная агрегация, ограниченный по времени отчёт, кэш аналитики,
бюджет maintenance, кэшируемые assets и SQL timing. Это не закрывает все 63 задачи.

Актуальная детализация, ограничения и порядок миграции/релиза:
[исправления после повторной проверки](implementation-fixes-2026-09-11.md).
Исторические доказательства до исправлений сохранены в
[повторной проверке](implementation-recheck-2026-09-11.md).

В этой итерации прошли 617 изолированных Python-тестов website, 5 JS-тестов,
3 теста payment и 5 тестов bot; 51 класс fixtures БД исключён. Живые БД и
провайдеры не использовались. Новые ORM-таблицы требуют пользовательской
Alembic-миграции до запуска website/worker и совместимого rollout payment/bot.

Приложения: [индекс 772 функций](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docs/audits/2026-09-10/function-index.json), [сводка выполненных проверок](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docs/audits/2026-09-10/verification-summary.json). Индекс содержит координаты функций и синтаксические I/O-признаки; обёртки и косвенные вызовы требуют трассировки.

## Что проверено и как читать план

- Пользовательские сценарии: лендинги VPN/VPS/direct sale, login/OAuth/Telegram, онбординг, кабинет, устройства, email, рефералы, поддержка, checkout/payment status, PWA, mobile API.
- Админские сценарии: права сотрудников, аналитика/реклама/когорты, поддержка, инфраструктура, node bootstrap/provisioning, телеметрия, фоновые проверки, Cloudflare и RWMS.
- Эксплуатация: Django/Gunicorn, SQLAlchemy sessions/pool, timeouts, nginx edge/origin, кэш/статика, зависимости, наблюдаемость.
- Инвентаризированы **772 Python-функции** приложения и common без тестов/миграций. Синтаксический разбор **183 Python-файлов** прошёл без ошибок. Это инвентаризация и проверка синтаксиса, а не 772 выполненных функциональных теста.
- Прочитаны связанные участки соседних payment, RWMS, Telegram bot, mobile app, ip-guard; проверены потребители общих моделей в user-notify, email, rw-cleaner, ym-stat, yk-recurrent. Это проверка контрактов в затронутых областях, не полный независимый аудит каждого микросервиса.
- Выполнены **8 существующих чистых template guards — все прошли**, локальные проверки функций с mocks, реальный protobuf-конструктор и браузерные проверки HTML-фикстур. Полный Django suite не запускался: часть тестов создаёт таблицы/данные. Реальная БД, Remnawave, DNS, счета и отправка писем не использовались.
- Автодетектор интерфейса выдал 452 эвристических сигнала; он не разрешает Django `{% static %}` и часть динамической разметки. Эти сигналы **не считаются 452 багами**. В план вошли проверенные в контексте проблемы.

**Приоритеты:** P0 — немедленно закрыть возможность чужого доступа; P1 — серьёзный дефект доступа/оплаты/доступности или блокировка основного сценария; P2 — функциональная ошибка, эксплуатационный риск или оптимизация следующей итерации. Приоритет означает порядок исправления, а не доказательство эксплуатации в production.

У каждой задачи ниже указаны текущая реализация/условие ошибки, место в коде, правильное исправление, приёмочные проверки и сервисы. Буквенные ID сохраняются для создания отдельных задач. Пересекающиеся пункты объединять в один PR там, где указан общий механизм, не реализовывать независимые конкурирующие fixes.

## Очерёдность работ

| Этап | Что делать | Почему в таком порядке | Готовность этапа |
|---|---|---|---|
| 1. Доступ и блокирующий мобильный сценарий | B01+B02; B03+AI-01; OPS-02; мобильная кнопка онбординга и приватный PWA cache | Закрывает чужой вход, сохранённый JS в admin origin и невозможность начать вход на малом экране | Негативные auth/XSS тесты; после оплаты чужого email нет сессии; no private CacheStorage; CTA кликается на 320×568 |
| 2. Подписки и платежи | B04/B05/B08/B09/B10/B23; B06+B07 совместно; AI-02/04/05 | Исправление данных/платёжной идемпотентности должно предшествовать автоматическим повторам и ускорению запросов | UUID/URLs и paid expiry сохраняются; один intent→один счёт; потерянный ответ восстанавливается; bootstrap conflict безопасен |
| 3. Доступность при отказах | OPS-01/04/05; AI-03; B11/B22; AP-07 | Таймауты и жизненный цикл session защищают весь сайт от исчерпания ресурсов | Зависший провайдер не блокирует лёгкие запросы; worker возвращается после сбоя; UI различает недоступность и неверный код |
| 4. Загрузка и отзывчивость | Фронтенд: responsive images, cacheable CSS/JS, lazy modules, polling/модальные окна; OPS-06/07/08; AP-01/03/04/05; AI-06/09/10 | Убирает наиболее дорогую повторную работу и ошибки конкурирующих ответов | Cold/warm measurements, bounded payload/query count, максимум один poll, актуальная карточка совпадает с действием |
| 5. Корректность аналитики и масштабирование | AI-07/08; B14/B20; AP-02/06/08; остальные P2 | После устранения опасных багов можно менять тяжёлые агрегаты и согласовывать read models | Метрики совпадают с эталоном, недели с нулевой выручкой видны, 24ч данные действительно ограничены окном |
| 6. Закрепление | OPS-09/10; функциональные, контрактные и mobile browser tests; эксплуатационный baseline | Без измерений и воспроизводимого deploy ускорение легко потерять | CI на production Python, dashboard latency/error metrics, readiness, rollout/rollback-план |

Этапы 1–3 частично можно вести параллельно, но B06+B07 и изменения account/token identity должны иметь одного владельца решения. Большой рефакторинг `views.py` или переписывание frontend framework не является предпосылкой исправлений. Размер файла сам по себе не доказывает медленную работу.

## Карта скорости по пользовательским функциям

Вместо произвольной оптимизации арифметики измерять цепочки, которые ждёт человек. Мелкие чистые функции не оптимизировать без CPU-профиля. Полный индекс Python-функций и прямых статических I/O-признаков приложен отдельно; это навигация для профилирования, не оценка миллисекунд.

| Сценарий / функции | Что сейчас формирует задержку | Правильный целевой путь | Что замерять |
|---|---|---|---|
| `index`, `render_vps_direct_sale`, `render_login`, `get_runtime_tariffs` | Несколько чтений settings, тяжёлые изображения/внешние ресурсы, JS онбординга | Настройки одним snapshot, responsive LCP image, lazy assets следующих слайдов | TTFB, FCP/LCP, transferred bytes, время до первого нажатия |
| `send_magic_link`, `auth_email_request` | SQL/создание trial до проверки email, синхронная доставка письма | Валидированный challenge, квоты, durable отправка; provision в согласованной точке | Время HTTP-ответа и фактической отправки отдельно, SQL/lock wait |
| `auth_by_*`, `authorize_user_session`, `exchange_code` | Проверки личности, transaction/RWMS и гонки одноразовых кодов | Атомарное подтверждение, правильная session lifecycle, ограниченный внешний I/O | p95 auth, число ошибок по истинной причине, locks |
| `dashboard` | Последовательные counts/settings/support history, повторно открытая session, RPC, код всех вкладок | Быстрый summary; закрыть DB до сети; вторичные вкладки загружать по открытию | SQL count/time, pool wait, RPC, template render, LCP/INP |
| `cabinet_devices`, `_panel_hwid_fallback_limit` | RPC пользователя/устройств/settings плюс клиентское обновление счётчика | Единый запрос в работе, bounded deadline, retry UI; общие panel settings кратко кэшировать | Время открытия панели, RPC count, stale-response handling |
| `pay`, `create_*_payment_sync`, `save_wata_invoice` | Длинная SQL transaction, сеть, письмо перед ответом, повтор POST | Durable intent → идемпотентный provider create → быстрый статус; письмо независимо | Каждый этап checkout, API latency/error, orphan/double-intent count |
| `get_purchase_payment_status`, `active_wata_status_for_token` | Повтор SQL/внешней проверки; локальный throttle не общий | Точный order match, shared single-flight/check budget, backoff polling | Requests/order/min, время webhook→активация→UI |
| `update_email`, `confirm_email`, bind Telegram | Проверка ownership и синхронизация между сервисами | Одноразовый versioned challenge, корректный proto, observable retry | Время convergence DB↔panel, replay/concurrency errors |
| `support_*`, `load_support_messages_with_attachments` | Вся история на каждый poll, hidden polling и полный redraw | Cursor/incremental payload, visible-only poll, keyed DOM, media offload | Bytes/poll, SQL rows, no-op cost, воспроизведение видео |
| Mobile `/me`, `authenticate` | Write last_seen на каждый GET, транзакция во время RPC | Ограничить служебные writes, DTO до RPC, block/revocation без stale cache | Writes/user/min, DB checkout duration, latency/degradation |
| Admin stats/cohorts/acquisition | Повтор исторических CTE/окон, лишняя загрузка, stale fetch | Только активный раздел, latest-wins, report cache; read model по профилю | p95 cold/warm, EXPLAIN, memory/rows, applied-filter consistency |
| Infra detail/telemetry/who_connects | Тяжёлая аналитика блокирует live показатели, cache stampede | Раздельный live snapshot и lazy analytics, shared single-flight | Время первого полезного состояния, пересчёты/ключ, chart latency |
| `aggregate_telemetry`, worker maintenance | SELECT/upsert на bucket, unbounded backlog, последовательные внешние шаги | Batches, watermarks, bounded work, monotonic due-time, recoverable worker | Tick duration/lag, SQL round trips, RSS, heartbeat age |
| Node traffic/provisioning | Сотни profile RPC; параллельные stale details и claim races | Видимая страница, ограниченный batch; актуальный ID; атомарные переходы | RPC/report, active requests, selected/rendered/mutated ID |

## Как измерять выигрыш

Размеры исходных шаблонов на момент снятия baseline: `dashboard.html` — **444 956 байт**, `admin_dashboard.html` — **1 284 911 байт**, `login.html` — **40 083 байта**. Для ориентира gzip исходников даёт 71 222, 235 666 и 9 000 байт соответственно. Это **не размер реального ответа**: рендеринг условий, контекст, роли и настройки nginx меняют результат. CSS `admin_dashboard.css` — 140 901 байт до сжатия. Браузерные fixture-измерения и размеры изображений приведены в разделе интерфейса.

1. Снять baseline по VPN, VPS, direct-sale и cabinet domain; гость/активная/истекающая/истёкшая/заблокированная подписка, Telegram WebView и standalone PWA; mobile320/390/430 и desktop1440.
2. Отделить cold/warm browser cache и cold/warm server cache; CPU throttling, медленная мобильная сеть, сценарий зависшей внешней зависимости. Записывать median/p75/p95 и payload, не один удачный запуск на localhost.
3. Разложить TTFB на auth, pool wait, SQL, RPC/provider, rendering. Для медленных ORM запросов получить read-only EXPLAIN на релевантном объёме; не создавать индексы «на глаз». EXPLAIN ANALYZE исполняет запрос: тяжёлые запросы сначала на согласованной реплике/изолированном стенде и только SELECT.
4. Проектные стартовые цели для клиентских страниц: LCP ≤2,5с, INP ≤200мс, CLS ≤0,1 на выбранном мобильном профиле. Это цели приёмки, не текущие измерения и не обещание результата. API budgets установить после baseline, отдельно учитывать SLA внешнего провайдера.
5. Считать оптимизацию завершённой только при сохранении результатов/прав доступа и измеримом улучшении. Для основных пользовательских действий — отсутствие зависаний, немедленный понятный feedback, безопасный повтор.

## Межсервисные ограничения плана

| Сервис | Почему затрагивается | Что проверить перед выпуском |
|---|---|---|
| Website | Основная реализация всех найденных сценариев | Негативные auth/permissions, mobile UI, query/latency budget |
| Common / PostgreSQL | Модели token/intent/outbox/агрегатов и shared RPC client | Только добавочные совместимые изменения; ORM; миграции генерирует и применяет пользователь своим скриптом |
| Payment | Order mapping, callbacks, подтверждение оплаты, dedup | Ранний/повторный/запоздалый webhook, recovery после обрыва; ровно одно продление |
| YK recurrent | Расписание после платежа, отмена/повтор | Нет двойного расписания/списания; сохранён контракт scheduled_payment |
| RWMS / Remnawave | Email/squads, регистрации/adoption, bootstrap node, deadlines | Не смешивать NOT_FOUND и unavailable; сохранить существующие UUID/URL/squads/paid expiry |
| Telegram bot | Bind/merge, выдача mobile-кодов и purchase links, цены | Совместимый rollout token format, atomic consume, перенос FK/платежей без потерь |
| Mobile app | Цены, 429/503/401, auth token/URL, повторы | Верный error contract и retry, old-client compatibility |
| User-notify / Email | Отправка magic-link, trial/events, письма/уведомления | Один владелец delivery, durable dedup, корректные события после изменения момента provision |
| IP-guard | Writer телеметрии и lifetime hits | Не менять семантику детектора; новая временная статистика отдельно |
| RW cleaner | Опирается на состояние пользователя/срок/платежи | Те же запреты удаления active/trial/paid/recently-expired; ambiguity→skip; dry-run сохраняется |
| YM-stat | Аналитические события регистрации/платежа и атрибуция | Согласованный момент событий, без дублей/исчезновения конверсий |
| Custom-config | Existing subscription URLs, squads, user identity | Старые клиентские конфиги действуют после email/bind/payment fixes |
| Edge/origin/deploy | Trusted IP, uploads, static cache, readiness | Проверить всю цепочку proxy; не открыть private media/static cache для cabinet HTML |

Ни один пункт не требует автоматической очистки users или подписок. Существующие данные сначала анализируются; неоднозначный результат внешней мутации восстанавливается по устойчивой идентичности. Добавочные protobuf-методы допустимы, удаление/перенумерация/переименование существующих полей — нет. При реализации каждого изменения обновлять README затронутого проекта и добавлять тесты на настоящий отказ/гонку, а не только на happy path.

## Что уже сделано правильно и надо сохранить

Есть строгий RWMS lookup с различением NOT_FOUND и unavailable и деградация кабинета по локальному сроку; одноразовый magic-link уже потребляется атомарным UPDATE/RETURNING; действуют ownership guards при adoption; статика использует content hash и HTML/JSON GZip; Tailwind скомпилирован заранее; в node traffic ограничена параллельность. Новая панель устройств имеет focus/inert/Back/Escape. Эти механизмы надо распространить на оставшиеся ветки, не заменяя их небезопасными «быстрыми» fallback.

Далее — подробные карточки задач. Порядок внутри технических разделов не заменяет этапы приоритизации выше.



## Реестр задач

Объединённые номера: F03 входит в B06/B07; B15 — в OPS-05; B16 — в OPS-07. Поэтому пропуски ID намеренны.

| ID | Приоритет | Проблема / направление |
|---|---|---|
| [B01](#b01) | P0 | оплата чужого email даёт вход в чужой аккаунт |
| [B02](#b02) | P0 | несвязанный платёж того же пользователя подтверждает другой purchase token |
| [B03](#b03) | P1 | сохранённый SVG выполняется на origin сайта/админки |
| [B04](#b04) | P1 | email не синхронизируется с RWMS из-за неправильного типа squads |
| [B05](#b05) | P1 | dashboard повторно открывает уже закрытую SQLAlchemy session и не закрывает её |
| [B06](#b06) | P1 | повтор POST создаёт новый счёт, идемпотентность только внутри одного вызова провайдера |
| [B07](#b07) | P2 | внешняя Wata-ссылка создаётся до сохранения durable order mapping |
| [B08](#b08) | P1 | после неопределённого результата AddUser создаётся «успешный» локальный аккаунт без подписки |
| [B09](#b09) | P1 | mobile email login выдаёт token и subscription URL заблокированному аккаунту |
| [B10](#b10) | P1 | одноразовый mobile device code не потребляется атомарно |
| [B11](#b11) | P2 | лимит отправки mobile email-кодов обходится конкурентными запросами |
| [B12](#b12) | P1 | аккаунт и пробная подписка создаются до проверки владения email |
| [B13](#b13) | P2 | mobile endpoints дают 500 на валидном JSON неправильного типа |
| [B14](#b14) | P2 | mobile paywall показывает устаревшие статические цены |
| [B17](#b17) | P2 | recovery одной личности скачивает список всех подписок |
| [B18](#b18) | P2 | ручной login не ротирует session id и CSRF token |
| [B19](#b19) | P2 | ссылка подтверждения смены email повторно применима и не связана с актуальной операцией |
| [B20](#b20) | P2 | API /me выполняет write на каждое чтение статуса и держит transaction через RWMS |
| [B21](#b21) | P2 | Telegram bind token имеет лишь 32 бита и не истекает |
| [B22](#b22) | P2 | корректный mobile email-code показывается как неверный при ошибке provision |
| [B23](#b23) | P1 | pay обходит обязательное подтверждение привязки email |
| [OPS-01](#ops-01) | P1 | Сетевые операции способны занять все потоки сайта без конечного срока |
| [OPS-02](#ops-02) | P1 | Отключение сотрудника, смена роли и пароля не отзывают его сессию |
| [OPS-03](#ops-03) | P2 | Персональный вход зависит от общего пароля; после выхода остаётся чужое имя в аудите |
| [OPS-04](#ops-04) | P1 | Для входа сотрудников и magic-link нет серверного ограничения частоты |
| [OPS-05](#ops-05) | P1 | Ошибка отправки письма показывается пользователю как успешная отправка |
| [OPS-06](#ops-06) | P2 | Лимиты вложений расходятся; крупные файлы держатся в RAM и выдаются через web-потоки |
| [OPS-07](#ops-07) | P2 | На каждом запросе повторяются мелкие чтения настроек и пользователя |
| [OPS-08](#ops-08) | P2 | Сжатые статические файлы подготовлены, но nginx-конфигурация не включает их выдачу |
| [OPS-09](#ops-09) | P2 | Зависимости и запуск воспроизводятся неполностью |
| [OPS-10](#ops-10) | P2 | Нет измерений времени отдельных этапов и раннего обнаружения деградации |
| [F01](#f01) | P1 | Онбординг перекрывает обязательную кнопку входа на коротких экранах — воспроизведено |
| [F02](#f02) | P1 | Service worker сохраняет приватный кабинет под ключом /login/ и показывает его offline |
| [F04](#f04) | P2 | Кабинет бесконечно опрашивает полностью скрытый чат каждые 2 секунды |
| [F05](#f05) | P2 | Новый мастер теряет альтернативные установщики; для INCY на Intel Mac выдаёт ARM |
| [F06](#f06) | P2 | Неизвестный ?tab= оставляет полностью пустой кабинет — воспроизведено |
| [F07](#f07) | P2 | Старые bottom sheets не имеют доступного жизненного цикла диалога |
| [F08](#f08) | P2 | У нового мастера фокус исчезает после выбора платформы/шага — воспроизведено |
| [F09](#f09) | P2 | Вход запрещает увеличение и onboarding оставляет фон интерактивным |
| [F10](#f10) | P2 | Нейтральные домены снова называют продукт VPN на /login/ и в manifest |
| [F11](#f11) | P2 | Платёжные страницы грузят 2.56 MB декоративного PNG |
| [F12](#f12) | P2 | Кабинет пересылает большой общий код/стили с каждым приватным HTML |
| [F13](#f13) | P2 | Внешние ресурсы блокируют критический рендер |
| [F14](#f14) | P2 | Loading состояния fetch могут висеть неограниченно |
| [F15](#f15) | P2 | Загрузка устройств не защищена от устаревшего ответа |
| [AI-01](#ai-01) | P1 | Stored XSS в рекламе позволяет маркетологу исполнять код в сессии полного администратора |
| [AI-02](#ai-02) | P1 | Подделка IP в bootstrap и admin audit через X-Forwarded-For |
| [AI-03](#ai-03) | P1 | Сбой подключения к БД навсегда останавливает infra worker |
| [AI-04](#ai-04) | P1 | Bootstrap принимает чужую существующую node как результат создания и может показать ложный READY |
| [AI-05](#ai-05) | P1 | Claim bootstrap token не атомарен |
| [AI-06](#ai-06) | P2 | Карточка установки отображает другую заявку, а кнопка отзыва действует на выбранную |
| [AI-07](#ai-07) | P2 | Недельная рекламная аналитика скрывает недели с расходами и нулём оплат |
| [AI-08](#ai-08) | P2 | «Активность за 24 часа» и геодоли используют накопительные hits за всё время жизни наблюдения |
| [AI-09](#ai-09) | P2 | Аналитические формы принимают запоздалый ответ и оставляют вечную загрузку после сетевого сбоя |
| [AP-01](#ap-01) | P1 | Разделить быстрые текущие данные infra и тяжёлую аналитику |
| [AP-02](#ap-02) | P2 | Аггрегация телеметрии делает SELECT и запись на каждый bucket |
| [AP-03](#ap-03) | P2 | Админка отправляет всем разделам большой HTML/JS и грузит редактор до открытия |
| [AP-04](#ap-04) | P2 | Список support тикетов не пагинирован |
| [AP-05](#ap-05) | P2 | Отчёт трафика синхронно обогащает до500 клиентов отдельными RWMS RPC |
| [AP-06](#ap-06) | P2 | Cloudflare DNS client теряет connection pooling и повторяет отрицательный поиск zone |
| [AP-07](#ap-07) | P2 | Таймаут шага infra ограничивает отдельный SQL, но не длительность обслуживания |
| [AP-08](#ap-08) | P2 | Аналитика повторно сканирует историю и создаёт временную cohort table на каждый запрос |
| [AI-10](#ai-10) | P2 | Администратору каждые7с сбрасываются видео и выделение текста в support чате |



## Публичный backend, аккаунты, оплата и mobile API


<a id="b01"></a>

### B01 — P0: оплата чужого email даёт вход в чужой аккаунт

**Подтверждено кодом и существующим тестом.** [engine/views.py:9603](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:9603) анонимный POST /pay разрешает произвольный email уже существующего незаблокированного User; `9657–9667` создаёт bearer purchase token, `9841–9846` отдаёт его в payment_status_url инициатору. `2510–2569` auth_by_purchase_link после статуса succeeded авторизует User без подтверждения почты или привязки к ранее авторизованной сессии. Оплата подтверждает платёж, но не владение существующим кабинетом. Человек, оплативший минимальный тариф на чужой известный email, получает подписку, историю, обращения и управление аккаунтом. Тест [engine/tests.py:8852–8935](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/tests.py:8852) проверяет только запрет неоплаченного токена, а оплаченный токен жертвы прямо разрешает.

**Правильное исправление:** разделить token просмотра заказа и token аутентификации. Публичному плательщику возвращать только scoped order-status token; вход в существующий аккаунт требует email magic-link/OAuth/Telegram или уже действующей сессии владельца. Поле email для чека не должно становиться доказательством личности. Для нового клиента завершать проверку email до выдачи постоянного account token. Разобрать совместимость уже выданных permanent links, потребовать подтверждение владельца при миграции; не менять subscription UUID/URLs.

**Тесты:** аноним покупает на email существующего пользователя, платёж succeeded, но сессия не выдаётся; владелец через письмо входит; новая покупка и уже авторизованная покупка работают. **Сервисы:** website; contracts purchase links/email; payment только как источник статуса, подписки не пересоздавать.


<a id="b02"></a>

### B02 — P0: несвязанный платёж того же пользователя подтверждает другой purchase token

**Подтверждено изолированным исполнением.** [engine/views.py:2686–2703](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:2686): при payment_gateway=wata код всё равно ищет YkPayment пользователя с created_at >= token.created_at−5min и первым возвращает succeeded/canceled. Reference конкретного Wata-заказа этот запрос не ограничивает. Для yookassa также существует fallback к свежему WataInvoice (`2616–2630`, `2705+`). У нового неоплаченного Wata token пользователя, который недавно оплатил YooKassa, получается succeeded; auth_by_purchase_link затем выдаёт сессию. Нет верхней границы времени, поэтому и будущие платежи другого шлюза способны подтвердить этот старый токен. Обратный побочный эффект — чужой canceled YooKassa скрывает успешную текущую оплату Wata.

**Исправление:** строго ветвиться по payment_gateway и требовать точную пару gateway+reference+user_id; поиск другого шлюза запрещён. Неопределённый legacy token не использовать для входа по временной эвристике; отправить владельцу email verification, а историю показывать без полномочий входа. Paid по конкретному заказу доминирует над его declined попытками.

**Тесты:** матрица обоих шлюзов, несвязанные succeeded/canceled до/после создания token, корректный reference, token без reference; статус не перескакивает между заказами. **Сервисы:** website, проверка реальных статусов payment; новая proto/schema не обязательна.


<a id="b03"></a>

### B03 — P1: сохранённый SVG выполняется на origin сайта/админки

**Подтверждено цепочкой кода; браузерный exploit не запускался.** [engine/views.py:257](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:257) разрешены все image/*; `565–590` верит uploaded_file.content_type и сохраняет его. SVG с MIME image/svg+xml принимается. `support_attachment:4078` и `support_admin_attachment:9424` отдают FileResponse без as_attachment/CSP sandbox с этим MIME. `engine/templates/support_admin_ticket_detail.html:133,205` открывает вложение отдельной вкладкой. Скрипт SVG при прямом открытии исполняется в origin сайта, в контексте администратора; httpOnly не препятствует authenticated same-origin fetch.

**Исправление:** точный allowlist безопасных растровых форматов, проверка содержимого декодером/перекодирование, отказ от активных SVG; файлы с потенциально активным содержимым отдавать attachment либо на отдельном origin без cookies с sandbox. Проверить уже сохранённые MIME и безопасно изменить способ выдачи, не удаляя пользовательские данные. Добавить nosniff; одного nosniff при корректном SVG MIME недостаточно.

**Тесты:** загрузка SVG и файл со сфальсифицированным image/png, прямое открытие сотрудником без выполнения JS; обычные картинки/видео доступны. **Сервисы:** website/support; соседние микросервисы не нужны.


<a id="b04"></a>

### B04 — P1: email не синхронизируется с RWMS из-за неправильного типа squads

**Подтверждено реальным protobuf-конструктором, сеть не использовалась.** [engine/views.py:3744–3750](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:3744): UpdateUserRequest.active_internal_squads получает `subscription.active_internal_squads`. UserResponse содержит repeated ActiveInternalSquad, UpdateUserRequest — repeated string. Для непустого набора конструктор выдаёт `TypeError: bad argument type for built-in operation` ещё до RPC. Локальный email уже committed (`3711`), exception лишь логируется. В панели сохраняется старый email; recovery ownership начинает конфликтовать, уведомления/поиск могут расходиться.

**Исправление:** предпочтительно передавать только UUID пользователя и новый email. В проверенном RWMS [server.py:525–532](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-rwms/server.py:525) пустой repeated squads означает «не изменять» и исключается из PATCH. Это сохраняет также параллельные изменения squads. `[squad.uuid for squad in subscription.active_internal_squads]` убирает TypeError, но отправляет потенциально устаревший snapshot, поэтому применять такой вариант только если этого требует фактическая версия RWMS. Проверить версию сервера при выпуске; использовать strict lookup и устойчивую повторную синхронизацию email при временной ошибке вместо потери задачи. Не менять UUID/ссылку подписки.

**Тесты:** настоящий proto с 1–2 squads; запрос меняет только email, состав squads в панели не меняется, включая параллельное обновление; ошибка RWMS оставляет наблюдаемую retry-задачу. **Сервисы:** website + RWMS; проверка email/bot ownership contract. Протокол менять не нужно.


<a id="b05"></a>

### B05 — P1: dashboard повторно открывает уже закрытую SQLAlchemy session и не закрывает её

**Подтверждено кодом.** [engine/views.py:3291–3297](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:3291) закрывает session в finally, затем `session.query` для bonus_days открывает новую транзакцию/соединение (Session.close допускает повторное использование по умолчанию). Далее соединение удерживается во время синхронного RWMS (`3308`), шифрования, `build_apple_subscription_link(session,...)` (`3410`) и render; второго finally/close нет. До GC соединение и транзакция остаются заняты, при нагрузке возможны pool exhaustion и рост TTFB. Это не просто «лишний запрос».

**Исправление:** все ORM чтения, включая bonus_days/settings, выполнить внутри одного контекстного блока session, затем materialize DTO и закрыть до внешнего I/O. Не передавать закрытую session в построение Apple-ссылки. Не лечить увеличением pool.

**Тесты:** stub session запрещает любые запросы после close; event counter checkin/checkout в изолированном test harness; исключение RWMS/render не оставляет session; нагрузка подтверждает отсутствие роста checked-out после запроса. **Сервисы:** website/common ORM, бизнес-контракты не меняются.


<a id="b06"></a>

### B06 — P1: повтор POST создаёт новый счёт, идемпотентность только внутри одного вызова провайдера

**Подтверждено кодом.** [engine/payments.py:55](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/payments.py:55) новый uuid4 idempotency_key на каждый create_yk_payment_sync; `109` новый orderId на каждый create_wata_payment_sync. [engine/views.py:9457–9932](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:9457) не принимает/сохраняет payment intent/request id. Повтор запроса после потери ответа создаёт второй действительный платёж. Frontend-agent отдельно нашёл blind form.submit retry после fetch error. Дедуп вебхуков payment идёт по provider payment_id/transaction_id и не объединяет две разные оплаты.

**Исправление:** server-side intent с клиентским request id и ограниченной уникальностью owner/intent; один provider idempotency/order id сохранять до внешнего запроса, последующие retry возвращают прежний результат или статус resolving. Не считать frontend disabled button защитой. Убрать автоматический повтор небезопасного POST; после неопределённого результата проверять intent. Не автоматически возвращать/удалять уже реальные платежи.

**Тесты:** два конкурентных POST с одним request id, обрыв ответа после успешного create, повтор из UI — один provider create и один счёт; новый осознанный платёж получает новый intent. **Сервисы:** website, payment (reconciliation), yk-recurrent (подтвердить отсутствие двойного расписания). Schema если нужна — модели, затем миграцию генерирует/применяет пользователь.

**Клиентский триггер той же проблемы (F03):** [engine/templates/dashboard.html:7755–7780](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:7755), [engine/templates/index_vpn.html:851–872](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/index_vpn.html:851), [engine/templates/index_vps.html:1509–1530](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/index_vps.html:1509), [engine/templates/index_vps_direct_sale.html:1252–1274](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/index_vps_direct_sale.html:1252): catch после fetch POST вызывает form.submit() и повторяет уже отправленный запрос. Исправлять frontend и server-side intent одним согласованным изменением; обычный form fallback допустим до первого отправленного POST, не после потери его ответа.


<a id="b07"></a>

### B07 — P2: внешняя Wata-ссылка создаётся до сохранения durable order mapping

**Подтверждён риск восстановления заказа; потеря оплаченной подписки в обычном браузерном flow не воспроизведена.** [engine/views.py:9685](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:9685) вызывает Wata, лишь затем сохраняет invoice `9701`, commit только `9786`. После успешного API create, но до commit, ошибка/kill теряет invoice и token. [monkey-island-payment/wata_webhook_handler.py:560–579](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-payment/wata_webhook_handler.py:560) при отсутствии WataInvoice не продлевает подписку, сохраняет failed webhook и возвращает ok; автоматическое повторение не восстановит mapping. Потерянный ответ делает исход создания неизвестным; текущий повтор создаёт новый заказ.

**Исправление:** durable payment intent и correlation id фиксировать ДО внешнего вызова; provider result связывать с intent, повторно получать/сверять его после timeout. Для раннего вебхука — bounded durable retry/очередь, а не необратимое завершение без бизнес-обработки. Все действия идемпотентны; возможные orphan invoices сверять read-only, никакого угадывания владельца по email/сумме.

**Тесты:** fault injection в каждом месте между intent commit/API/result commit, webhook приходит раньше сохранения результата, outage сайта после API success; итог один подтверждённый заказ и один продлённый период. **Сервисы:** website + payment; бизнес-процесс значимый, делать совместно с B06.

**Важное условие:** checkout URL возвращается браузеру только после commit ([engine/views.py:9786](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:9786), [engine/views.py:9840–9847](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:9840)). Поэтому сбой до commit обычно оставляет недоступный пользователю внешний неоплаченный счёт. Чтобы такой orphan уже оказался оплаченным/получил ранний Paid webhook, необходимо дополнительное условие получения ссылки или восстановления заказа. Это не доказательство того, что любой сбой commit теряет деньги. Устойчивый intent и reconciliation всё равно нужны и реализуются вместе с B06.


<a id="b08"></a>

### B08 — P1: после неопределённого результата AddUser создаётся «успешный» локальный аккаунт без подписки

**Подтверждено кодом.** В trial flow сначала strict read защищён, но при rwms_client.add_user -> None ([engine/views.py:1519](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:1519)) идёт broad get_all_users lookup, и его отказ/пустой ответ ведёт к create_local_site_user_without_rwms (`1534–1546`). AddUser мог успешно создать панельную подписку, но потерять ответ; DB expire_at=None, маркер trial limit отсутствует. Следующий вход видит существующего User и пропускает recovery. Кабинет считает доступ истёкшим, хотя панель содержит trial, либо обещанный trial вообще не создан. Штатный local-only аккаунт при выключенном trial ([engine/views.py:1411–1449](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:1411)) является намеренным поведением и не входит в этот дефект. Для legacy recovery при выключенном trial требуется отдельная политика.

**Исправление:** после неизвестного AddUser делать strict lookup того же deterministic username; найденное принимать только с ownership guard, недоступное оставлять pending/503 с rollback, подтверждённое отсутствие обрабатывать явным retry flow. Никогда не заменять неуспешное создание обещанного trial завершённой регистрацией без trial. Сохранить детерминированные имена и существующие subscription URLs; не удалять панельные orphan-подписки автоматически.

**Тесты:** AddUser применён, ответ потерян, второй lookup unavailable, затем повтор; должен восстановиться тот же UUID и expiry/маркер, без второго AddUser и без пустого завершённого аккаунта. **Сервисы:** website/RWMS/common; mobile provisioning уже отказывается при create None, сохранить согласованность; user-notify/rw-cleaner должны видеть корректный expiry.


<a id="b09"></a>

### B09 — P1: mobile email login выдаёт token и subscription URL заблокированному аккаунту

**Подтверждено изолированным исполнением.** [mobile_api/views.py:253–285](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/views.py:253) находит existing User и сразу вызывает _issue_access_token; блокировки user_blocks здесь нет. `mobile_api/auth.py:100,202` проверяет блокировку в exchange/authenticate, но не в email verify. Middleware видит анонимный Django request. Возвращается secret subscription URL и долговременный token, хотя последующий /me токен отвергнет. При разблокировке такой токен станет рабочим, если отдельно не отозван.

**Исправление:** единый guard выдачи токенов для всех login flows, проверять UserBlock до токена/внешнего чтения секрета; фиксировать безопасный отказ, не отдавать subscription_url. Проверить policy существующих токенов при блокировке.

**Тесты:** blocked email с верным кодом — token не создаётся, RWMS/URL не возвращается; exchange и email имеют одинаковый результат. **Сервисы:** website/mobile API/mobile app; bot admin block + RWMS блокировка — сверка контрактов.


<a id="b10"></a>

### B10 — P1: одноразовый mobile device code не потребляется атомарно

**Подтверждено конкурентным сценарием по коду; PG нагрузочный тест не запускался.** [mobile_api/auth.py:88–102](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/auth.py:88) SELECT без FOR UPDATE, проверка used_at, присвоение used_at, issue token. Два transaction snapshots читают unused; оба выдают разные долговременные tokens. `auth_exchange` откладывает commit до окончания внешнего RWMS запроса, расширяя окно. Обычный sequential test_code_is_one_time этот дефект не ловит.

**Исправление:** условный UPDATE ... WHERE used_at IS NULL AND expires_at > now RETURNING user_id или lock row + recheck, единая транзакция на consumption/token. Внешний lookup выполнять после durable issuance вне row lock; при сбое выдачи продумать безопасную идемпотентность exchange.

**Тесты:** два одновременно обменивающих один code запроса, только один token; истечение/blocked account; повтор после сети не даёт второй token. **Сервисы:** website/mobile API + Telegram bot как issuer кода; proto менять не нужно.


<a id="b11"></a>

### B11 — P2: лимит отправки mobile email-кодов обходится конкурентными запросами

**Подтверждено отсутствием общей сериализации.** [mobile_api/views.py:189–206](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/views.py:189) сначала rate query, затем register_email_code и письмо без lock_email; у двух первых запросов оба select видят 0 записей. Register инвалидирует старые строки, но пустой набор не блокируется и нет unique active constraint. Verify имеет advisory lock, request его не берёт. Получаем несколько отправок в 60 секунд и потенциально несколько одновременно активных кодов; лишняя стоимость/путаница и расширение brute-force surface.

**Исправление:** тот же email advisory lock ДО проверки лимита и регистрации, время брать после ожидания lock; короткая transaction для durable code/outbox; отправку выполнять после commit с устойчивым delivery state. Добавить распределённые IP/global лимиты, не заменяя email лимит.

**Тесты:** параллельные первые/повторные send requests + одновременный verify, максимум один активный code и одна отправка, корректные TTL/429. **Сервисы:** website; email service/очередь при выносе транспорта.


<a id="b12"></a>

### B12 — P1: аккаунт и пробная подписка создаются до проверки владения email

**Подтверждено кодом; создание панельного trial относится только к конфигурации с включённым site trial.** [engine/views.py:2027–2081](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:2027): email проверяется только на непустоту, нет нормальной валидации адреса/длины, rate limit, captcha/abuse budget. До проверки владения создаётся User и при enabled trial — реальная панельная подписка; затем письмо. То же недостаточное server validation в pay и update_email. Автоматические запросы загрязняют User/RWMS/аналитику, расходуют отправки и удерживают workers. CSRF не является лимитом злоупотребления, а type=email в браузере обходится.

**Исправление:** server-side EmailValidator и длина до ORM/API; пер-email + IP + глобальные лимиты shared cache; для нового trial provision только после подтверждения владения email (pending login challenge вместо User с подпиской). Существующую бизнес-семантику direct sale и Telegram trials сохранить. Никакой массовой очистки текущих users автоматом.

**Тесты:** invalid/oversized email не вызывает SQL writes/RWMS/send; abuse limits между несколькими workers; один подтверждённый challenge создаёт один trial; anti-enumeration response одинаков при нормальной работе. **Сервисы:** website/RWMS/email, аналитика/notify при переносе момента subscription_created.

Серверные квоты описаны в OPS-04; здесь самостоятельная часть работы — валидация email и момент создания бизнес-аккаунта/пробного периода. Не переносить момент trial в боте автоматически.


<a id="b13"></a>

### B13 — P2: mobile endpoints дают 500 на валидном JSON неправильного типа

**Подтверждено изолированным исполнением.** `mobile_api/views.py:112–119,180–185,230–236` ловит только JSONDecodeError, затем body.get и .strip. `[]`, `null`, `{"email":123}`, `{"code":[]}` -> AttributeError; invalid UTF-8 -> UnicodeDecodeError. Ошибочные клиенты превращаются в 500/traceback и лишний шум.

**Исправление:** одна schema validation для JSON object/string fields, ограничить длину code/email, требовать 6 цифр для email code, ловить decode/type errors и отвечать стабильным 400 до БД. **Тесты:** property/table cases по JSON scalar/list/null, неверным типам и Unicode. **Сервисы:** website/mobile API; mobile app сохраняет error contract.


<a id="b14"></a>

### B14 — P2: mobile paywall показывает устаревшие статические цены

**Подтверждено кодом и явным TODO.** [mobile_api/tariffs.py:1–13](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/tariffs.py:1) MOBILE_TARIFFS создан из class defaults; [mobile_api/views.py:344–345](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/views.py:344) возвращает их. Кабинет использует system_settings через [engine/views.py:4777–4805](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:4777). Mobile app реально загружает этот endpoint (`mobile_api_client.dart:186`) и показывает `${tariff.priceRub} ₽ сейчас` (`paywall_sheet.dart:336`), но купить отправляет в бота, где runtime price. При изменении цены в админке UI расходится с реальным счётом.

**Исправление:** вынести runtime tariff resolver в небольшой общий модуль, использовать в site/mobile/bot; один batch запрос по ключам, разумный общий cache/invalidation; сохранить JSON schema/id/period. Персональная скидка — отдельный authenticated quote, не публичная цена.

**Тесты:** runtime override отражается во всех paywalls; ошибка/невалидная настройка даёт согласованный fallback; price/id/period совпадают со счётом. **Сервисы:** website/mobile app/Telegram bot/payment; схема не меняется.


<a id="b17"></a>

### B17 — P2: recovery одной личности скачивает список всех подписок

**Подтверждено алгоритмом.** [engine/views.py:1116–1133](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:1116) find_rwms_user_by_identity вызывает GetAllUsers и линейно сравнивает identities. На trial-disabled регистрации вызывается для каждого нового email. [monkey-island-rwms/server.py:554–565](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-rwms/server.py:554) лишь проксирует offset/count в SDK, website не обходит страницы. Возможны большой payload/CPU/latency и пропуск пользователя за пределами фактически полученной страницы; точную семантику default count следует проверить у установленного SDK, она не подтверждена как отдельный баг.

**Исправление:** strict lookup deterministic username первым, targeted email/telegram lookup через backward-compatible новый RPC/индекс для legacy recovery; broad scan только административной задачей с пагинацией/лимитами. Не выбирать первый совпавший identity при неоднозначном владении; alert/оператор. **Тесты:** legacy пользователь вне первой страницы, дубликаты identity, недоступность панели и большой набор; один пользователь не требует полного списка. **Сервисы:** website/RWMS/common/proto, Telegram bot/mobile при распространении shared client; не менять существующие номера/поля.


<a id="b18"></a>

### B18 — P2: ручной login не ротирует session id и CSRF token

**Подтверждено отсутствием действий в custom login; exploit зависит от доступа к старому session id.** [engine/views.py:1690–1701](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:1690) authorize_user_session просто записывает `_auth_user_id`, backend и пустой hash в текущую session. Стандартный Django login дополнительно cycle_key/flush при смене пользователя и rotate_token. Здесь старый anonymous session id остаётся после email/OAuth/Telegram входа, а данные прошлой учётки остаются при смене аккаунта.

**Исправление:** воспроизвести безопасную session lifecycle без зависимости User._meta: cycle_key для входа из anonymous, flush при смене principal, rotate_token; сохранить только допустимый tracking/return context. Конвертацию User в Django model ради этого не проводить.

**Тесты:** pre-login cookie не даёт auth после входа; CSRF token меняется; вход A→B очищает приватные session values; tracking сохраняется явно. **Сервисы:** website; проверка всех login methods, изменения БД/подписок не нужны.


<a id="b19"></a>

### B19 — P2: ссылка подтверждения смены email повторно применима и не связана с актуальной операцией

**Подтверждено кодом; риск ограничен TTL 15 минут.** [engine/views.py:1986–2005](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:1986) signed payload {user_id,email}, только max_age; `3659–3720` не проверяет pending request, одноразовость или предыдущий email, всегда ставит email и может авторизовать по одному токену. Более старая ссылка подтверждения остаётся пригодна после более новой смены и возвращает старый адрес; одновременно открытые письма дают last-click-wins без контроля версии. После компрометации такого токена смена email в том же окне его не отзывает.

**Исправление:** one-time confirmation challenge с version/current identity binding, atomic consume, supersede старые pending изменения; отдельно определить, нужен ли login при подтверждении email, вместо неявного session switch. Только подтверждённый текущий request синхронизировать в RWMS. **Тесты:** две ссылки в обратном порядке, повтор consumed token, смена email и replay старого token, истечение. **Сервисы:** website/RWMS/email, возможная новая модель и пользовательская миграция.


<a id="b20"></a>

### B20 — P2: API /me выполняет write на каждое чтение статуса и держит transaction через RWMS

**Подтверждено code path, нагрузка не измерялась.** [mobile_api/auth.py:201](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/auth.py:201) authenticate обновляет last_seen_at на каждом запросе; [mobile_api/views.py:305–340](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/views.py:305) читает RWMS перед session.commit, затем сериализует ORM user после commit (expire_on_commit может требовать ещё SELECT). Polling/pull-to-refresh мобильных устройств превращает чтения в writes/WAL и держит ресурсы БД во время внешней сети.

**Исправление:** снять минимальный immutable auth DTO, обновлять last_seen_at не чаще согласованного окна (например 5 минут) условным UPDATE/батчем, commit/close до внешнего I/O; не кэшировать block/revocation настолько долго, чтобы обходить блокировку. Subscription status отдавать с определённой freshness/degradation policy, согласованной с кабинетной «БД — время, панель — существование».

**Тесты/измерения:** N polling GET в окне → ограниченное число updates, отсутствие DB checkout во время RWMS, блокировка и logout применяются немедленно, no stale active on deleted panel user. **Сервисы:** website/mobile app; для convergence проверить payment/RWMS.


<a id="b21"></a>

### B21 — P2, hardening: Telegram bind token имеет лишь 32 бита и не истекает

**Подтверждено обеими сторонами.** [engine/views.py:3210](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:3210) делает md5(...).hexdigest()[:8]. [monkey-island-vpn-bot/handlers/menu.py:1039–1058](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-vpn-bot/handlers/menu.py:1039) проверяет ту же постоянную 8-hex строку, без expiry/nonce/consume. Payload авторизует объединение и привязку бизнес-аккаунтов, поэтому потеря единственной ссылки оставляет долгосрочную возможность привязки. Это не утверждение, что brute-force уже практически выполнен: частоту ограничивает Telegram, лимиты бота требуют отдельного аудита.

**Исправление:** случайный одноразовый token не менее 128 бит, хранить hash, user_id, expiry, used_at и при необходимости target Telegram identity, atomic consume после согласованной проверки; разработать совместимую ограниченную миграцию старых ссылок, не сохранять бессрочный legacy fallback навсегда. Не удалять/пересоздавать подписки ради обновления ссылки.

**Тесты:** replay/expiry/другая identity/parallel bind отвергаются; merge сохраняет paid expiry/все FK и subscription URLs. **Сервисы:** website + Telegram bot/common; очень осторожно с merge, отдельный end-to-end audit.


<a id="b22"></a>

### B22 — P2: корректный mobile email-code показывается как неверный при ошибке provision

**Подтверждено code path.** [mobile_api/views.py:274–276](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/views.py:274): verify_email_code уже вернул True, но если provision_trial_user возвращает None из-за RWMS unavailable/create error, API отвечает 401 invalid_or_expired_code. Транзакция rollback сохраняет код пригодным, но приложение получает ложную причину и может просить новый код/запускать повторную отправку вместо retry. Ownership conflict соседняя ветка корректно возвращает 503.

**Исправление:** типизированный результат временной ошибки provision и 503 temporarily_unavailable с сохранением challenge до TTL; invalid_code только для ошибочного/использованного/истёкшего кода. **Тесты:** валидный code + RWMS outage →503, retry тем же code после восстановления создаёт одного User; неверный code →401. **Сервисы:** website/mobile app/RWMS.


<a id="b23"></a>

### B23 — P1: pay обходит обязательное подтверждение привязки email

**Подтверждено кодом.** В обычной настройке email `update_email` отправляет confirmation token, но [engine/views.py:9565–9584](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:9565) для авторизованного Telegram-only пользователя прямо пишет присланный email в User и flush, без подтверждения владения; затем создание счёта приводит к commit `9786`, даже платить не требуется. Таким способом можно занять свободный чужой email и связать его с Telegram-аккаунтом. Законный владелец почты затем входит по magic-link в чужую Telegram-подписку, а платежи/письма уходят к неверной identity. Это отдельный обход подтверждения от анонимного purchase-token takeover B01.

**Исправление:** email чека хранить в payment intent отдельно, User.email менять исключительно через verification flow. Если оформление требует привязку — завершить email challenge до записи в User, или разрешить оплату авторизованного user_id с receipt_email без изменения auth identity. **Тесты:** Telegram-only pay c новым receipt_email не меняет User.email до подтверждения; занятый/невалидный email и подтверждённая привязка; invoice всегда ссылается на корректный user_id, не ищет заново по неподтверждённому receipt_email (обновить save_wata_invoice contract). **Сервисы:** website + payment + Telegram bot/email, совместимость cabinet bind flows.



## Эксплуатация и сквозные механизмы


<a id="ops-01"></a>

### OPS-01 · P1 · Сетевые операции способны занять все потоки сайта без конечного срока

**Где:** [common/rwms_client_sync.py:27–190](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/common/rwms_client_sync.py:27), [common/rwms_client.py:44–189](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/common/rwms_client.py:44), [engine/payments.py:41–90](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/payments.py:41), [engine/views.py:1919–1991](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:1919), [docker/website/entrypoint.sh:13–16](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/website/entrypoint.sh:13).

**Сейчас и доказательство:** ни один синхронный RPC не передаёт `timeout`. Проверка с mock-stub дала пустые kwargs для GetUserByUsername. В установленной и закреплённой YooKassa 3.10.1 ([yookassa/client.py:76](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/.venv/lib/python3.14/site-packages/yookassa/client.py:76)) `Session.request` тоже вызывается без `timeout`; поле Configuration.timeout используется для задержки повторов, поэтому просто изменить его недостаточно. SMTP получает стандартный `EMAIL_TIMEOUT=None`. Gunicorn настроен на 3 процесса × 4 потока; исходник gthread продолжает heartbeat, пока запросы висят в ThreadPoolExecutor. Его `--timeout 60` и nginx 504 не отменяют отдельный зависший вызов.

**Влияние:** зависание RWMS, SMTP или платёжного провайдера может исчерпать 12 потоков; здоровые запросы кабинета и лендингов встают в очередь. Это подтверждённый дефект ограничений ожидания, а не замер production latency.

**План:** (1) отдельные бюджеты connect/read/overall для каждого провайдера и категории RPC; (2) явный gRPC deadline с обработкой DEADLINE_EXCEEDED как неизвестного результата; (3) SMTP timeout; YooKassa — адаптер транспорта с реальным requests timeout либо проверенное обновление SDK, не одно Configuration.timeout; (4) ограничение одновременных тяжёлых операций, короткие очереди; (5) повторы только безопасных чтений, для мутаций сначала разрешить неоднозначный результат по idempotency key; (6) сохранять текущую деградацию кабинета при недоступности RWMS.

**Проверка:** fake-server принимает соединение и перестаёт отвечать; запрос завершается в установленный бюджет, параллельный лёгкий запрос остаётся доступен. Таймаут AddUser/Payment.create не создаёт повторную подписку/оплату. Тестировать транспорт отдельно от реальных сервисов.

**Сервисы/риск:** website, общий клиент common, RWMS; при изменении common — все его потребители. Нельзя глобально повторять мутации или воспринимать таймаут как отсутствие подписки.


<a id="ops-02"></a>

### OPS-02 · P1 · Отключение сотрудника, смена роли и пароля не отзывают его сессию

**Где:** [engine/views.py:862–935](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:862), [engine/views.py:4113–4120](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:4113), [engine/views.py:14753–14791](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:14753).

**Сейчас:** после входа роль и имя сотрудника копируются в Django session. Все последующие guards читают только сессию. `toggle`, `set_role`, `set_password` меняют AdminAccount, но не активные сессии. В изолированном вызове guard с прежней admin-сессией продолжил разрешать доступ; обращений к аккаунту нет. Стандартная продолжительность сессии — 14 дней, пока её явно не завершили.

**План:** хранить стабильный account_id и проверяемую версию доступа; на запрос проверять active, текущую роль и версию. Смена пароля/выключение немедленно инвалидирует сессии, изменение роли действует до следующего защищённого действия. Для общего аварийного пароля предусмотреть отдельную явно распознаваемую сессию. Вход должен менять session key, выход очищать все admin-ключи. Версия доступа потребует модели common и миграции, которую создаёт/применяет пользователь; минимальный фикс active/role можно сделать без схемы.

**Проверка:** логин → отключение/понижение/смена пароля из второй сессии → прежняя сессия получает отказ, прежде чем выполнить мутацию. Отдельно сохранить аварийный вход.

**Сервисы:** website; common только если добавляется version/revoked_at.


<a id="ops-03"></a>

### OPS-03 · P2 · Персональный вход зависит от общего пароля; после выхода остаётся чужое имя в аудите

**Где:** [engine/views.py:902–913](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:902), [engine/views.py:4140–4158](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:4140), [engine/views.py:4176–4180](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:4176), [engine/views.py:12440–12447](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:12440).

**Сейчас:** персональный AdminAccount успешно авторизуется, но при пустых обоих SUPPORT_*_PASSWORD guard возвращает 503. Logout не удаляет support_admin_account; последующий вход по общему паролю также его не сбрасывает, и журнал приписывает действия предыдущему персональному сотруднику. Оба сценария воспроизведены чистыми функциями.

**План:** отделить доступность персональной авторизации от настройки fallback-паролей; единая функция установки/очистки admin identity, ротация session key, источник входа в audit. Не терять разрешённый запасной вход.

**Проверка:** персональный вход при пустых env-паролях; A → выход → общий пароль → actor=роль/аварийный вход, не A; обычная пользовательская сессия не получает админские права.

**Сервисы:** website.


<a id="ops-04"></a>

### OPS-04 · P1 · Для входа сотрудников и magic-link нет серверного ограничения частоты

**Где:** [engine/views.py:2027–2119](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:2027), [engine/views.py:4087–4173](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:4087); обе nginx-конфигурации не задают limit_req. Mobile exchange использует [mobile_api/views.py:91–103](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/views.py:91), но [web_app/settings.py](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/web_app/settings.py) не настраивает общий cache backend.

**Сейчас:** каждый POST к web magic-link делает работу в БД и отправляет письмо; каждый admin-login проверяет пароль. Ограничение на UI не защищает эти handlers. Mobile exchange считает отдельно в каждом LocMemCache: при трёх процессах квота не единая, перезапуск её сбрасывает. Дополнительно XFF позволяет подменить ключ IP — см. инфраструктурную находку о доверии proxy headers.

**План:** после нормализации доверенного IP добавить атомарные квоты IP+account/email и общий лимит сервиса в Redis; не блокировать всю NAT-группу одним жёстким порогом. Возвращать 429+Retry-After, поддержать состояние ожидания в UI. Защиту от enumeration сохранить одинаковыми ответами, ограничивать отправку до тяжёлых операций. Логировать только нормализованный идентификатор/хеш, без OTP.

**Проверка:** единая квота на нескольких процессах, конкурентные запросы, смена подставленного XFF, legitimate resend, NAT-сценарий, отказ Redis с явно принятой политикой.

**Сервисы:** website, Redis/edge; mobile-app должен корректно обрабатывать 429.


<a id="ops-05"></a>

### OPS-05 · P1 · Ошибка отправки письма показывается пользователю как успешная отправка

**Где:** [engine/views.py:2081](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:2081), [engine/views.py:2111–2119](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:2111); связанные SMTP-настройки [web_app/settings.py:302–309](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/web_app/settings.py:302).

**Сейчас:** generic exception от SMTP/Resend лишь логируется, затем возвращается `{status: ok}`. Человек ждёт письмо, которое не отправлено, и не получает путь восстановления. Токен уже зафиксирован в БД.

**План:** отделить скрытие существования email от технической доступности транспорта; дать нейтральный retryable 503 при недоставке запроса провайдеру. Для очереди — durable outbox/job с idempotency, статус «запрос принят», метрики доставки и повтор; не писать «письмо отправлено» до принятия провайдером. Использовать существующий email-сервис после проверки его формата сообщения и SLA.

**Проверка:** SMTP timeout, 429/5xx Resend, успешный resend, ответ не раскрывает существование аккаунта, токены не попадают в журнал.

**Сервисы:** website; email при переносе доставки в очередь.

**Та же проблема задерживает оплату (объединён B15):** [engine/views.py:9786–9847](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:9786): после фиксации созданного счёта ответ клиенту ждёт send_magic_link_email. В mobile email request ([mobile_api/views.py:206–217](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/mobile_api/views.py:206)) отправка ещё и удерживает DB transaction. Вынести transport из ожидания checkout, а pending challenge/outbox фиксировать до отправки. Fault injection: медленный mail provider не задерживает уже созданный payment response, crash после commit не теряет job, повтор не дублирует письмо.


<a id="ops-06"></a>

### OPS-06 · P2 · Лимиты вложений расходятся; крупные файлы держатся в RAM и выдаются через web-потоки

**Где:** [docker/edge/nginx.conf.template:32](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/edge/nginx.conf.template:32) (10 MiB), [docker/website/nginx.conf.template:41](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/website/nginx.conf.template:41) (50 MiB), `web_app/settings.py:245–249,498–499` (50 MiB на файл/в памяти), `engine/views.py:568–593,4075–4080,9422–9427`.

**Сейчас:** файл 11–49 MiB, разрешённый приложением, отклоняется edge с 413. Файл ровно 50 MiB не помещается в origin body вместе с multipart overhead. Нет согласованного лимита числа файлов/суммарного размера. Порог in-memory upload поднят до 50 MiB; синхронная выдача приватного видео через FileResponse занимает ресурсы приложения.

**План:** определить публичный лимит одного файла, количества и всего запроса; origin/edge выставить чуть выше суммарного multipart budget. Вернуть стандартный небольшой RAM-порог с временными файлами. Обрабатывать 413 и отклонённые файлы явно. После проверки доступа отдавать приватные файлы через nginx internal/X-Accel-Redirect с Range для видео; открыть nginx только read-only media volume. MIME-проверку исправить по отдельной XSS-находке.

**Проверка:** лимит−1/лимит/лимит+1, несколько файлов, 413 в UI, RSS при одновременных upload, чужой attachment ID и прямой internal URL недоступны, Range/перемотка видео.

**Сервисы:** website, edge/origin nginx.


<a id="ops-07"></a>

### OPS-07 · P2 · На каждом запросе повторяются мелкие чтения настроек и пользователя

**Где:** [engine/views.py:4762–4804](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:4762), [engine/views.py:3201–3300](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:3201), [engine/auth_backend.py:7–17](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/auth_backend.py:7), [engine/user_block_middleware.py:19–27](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/user_block_middleware.py:19), `database.py:10`.

**Сейчас:** каждый тариф запрашивает SystemSetting отдельно; кабинет отдельно читает настройки рефералов, количества рефералов и поддержки. Аутентификация и проверка блокировки открывают разные SQLAlchemy sessions на каждый запрос, включая двухсекундный polling. При исключении query в SQLAlchemyBackend session.close не выполняется.

**План:** сначала исправить закрытие сессии try/finally/context manager, включая отдельную ошибку повторного использования session в dashboard. Затем единое чтение необходимых настроек по набору ключей, request-scoped snapshot и агрегированные счётчики. Общий cache — только для несекретных настроек с версией/коротким TTL; в оплате повторная авторитетная проверка цены. Не кэшировать надолго блокировки и платёжный статус. Измерить query count/latency по каждому endpoint, прежде чем выбирать индексы.

**Проверка:** одинаковые результаты до/после на тех же fixture-объектах; query budget не зависит от количества тарифов; исключение не оставляет checked-out соединение; новая цена и блокировка видны своевременно.

**Сервисы:** website; бот/payment/user-notify читают те же runtime settings, поэтому для общего cache нужна согласованная инвалидация.

**Необязательные данные и длинная история (объединён B16):** [engine/views.py:3215–3289](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:3215) грузит progress, recurrent/referral counts, settings, поддержку при каждом входе на главную. [engine/views.py:599–626](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:599) и [engine/views.py:4032](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:4032) читают/отдают всю переписку. На главной оставить subscription summary; referral/support — lazy по открытию. Для переписки cursor after_id и страницы 30–50 + older-history cursor; no-op response через revision/ETag. Приёмка: ответ при 10 и 10 000 сообщениях имеет ограниченный размер, порядок/новые сообщения не теряются. Скрытый polling отдельно удаляется по F04.


<a id="ops-08"></a>

### OPS-08 · P2 · Сжатые статические файлы подготовлены, но nginx-конфигурация не включает их выдачу

**Где:** [web_app/settings.py:397–420](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/web_app/settings.py:397), [docker/website/nginx.conf.template:43–48](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/website/nginx.conf.template:43), [docker/edge/nginx.conf.template:34–58](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/edge/nginx.conf.template:34).

**Наблюдение:** WhiteNoise manifest backend готовит сжатую статику, но production nginx обслуживает /static/ сам. В шаблонах nginx нет gzip_static/gzip_types/Brotli. Django GZipMiddleware сжимает HTML/JSON, но не файлы, выданные nginx. Внешний CDN/базовый nginx.conf может дополнительно сжимать: фактические Content-Encoding ещё нужно проверить.

**План:** read-only проверка Accept-Encoding и заголовков на каждом домене; включить gzip_static или compression MIME для CSS/JS/SVG в реальном слое доставки, Vary: Accept-Encoding; для manifest-хешированных ресурсов долгий immutable cache. Для нехешированных SW/manifest и alias URL — отдельная стратегия обновления. Не применять immutable к приватному HTML/API.

**Проверка:** размер transferred, правильный MIME, no double-compression, свежая версия CSS после deploy, gzip/br поддержка и fallback. Это возможность оптимизации с условием проверки production, не измеренное замедление.

**Сервисы:** website, edge/origin/CDN.


<a id="ops-09"></a>

### OPS-09 · P2 · Зависимости и запуск воспроизводятся неполностью

**Где:** `package.json`, `docker/website/Dockerfile:20–21`, [docker/website/entrypoint.sh:4–5](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/website/entrypoint.sh:4), [docker/website/docker-compose.yml:25–31](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/website/docker-compose.yml:25).

**Наблюдение:** npm lockfile отсутствует, сборка использует npm install, поэтому закреплённая верхняя зависимость не фиксирует весь граф. При каждом старте запускаются migrate и collectstatic; depends_on не проверяет готовность приложения, healthcheck web отсутствует. Это эксплуатационные риски, не доказательство текущего outage. Автоматическое Django migrate в startup не равно Alembic для общей business DB; не смешивать эти базы.

**План:** committed lockfile и npm ci; Python transitive constraints/hashes отдельным проверенным изменением; прогон в Python 3.12 из production-image (локальная .venv имеет 3.14.5). Подготовить статику в release/build-фазе с безопасной публикацией версии, readiness для web, отдельный контролируемый шаг Django migrations. Alembic common по-прежнему генерирует и применяет только пользователь своим скриптом.

**Проверка:** две сборки из одного commit дают одинаковый dependency graph; cold-start/readiness/restart без окна 502; старый и новый HTML получают доступные manifest assets.

**Сервисы:** website/deploy; общая схема не изменяется.


<a id="ops-10"></a>

### OPS-10 · P2 · Нет измерений времени отдельных этапов и раннего обнаружения деградации

**Где:** [web_app/settings.py:438–493](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/web_app/settings.py:438), [web_app/wsgi.py:20–43](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/web_app/wsgi.py:20), [docker/website/docker-compose.yml:1–25](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/website/docker-compose.yml:1); в коде не найдено Server-Timing/PerformanceObserver/web-vitals instrumentation.

**Наблюдение:** журнал FileHandler debug.log не ротируется приложением; endpoint p95/p99, DB pool wait, RPC/provider latency, длина очереди web-потоков и возраст heartbeat фоновых воркеров не наблюдаются штатно. Возможное внешнее наблюдение вне репозитория не проверено. Поэтому обещать точный процент ускорения каждой функции сейчас нельзя.

**План:** добавить request-id, structured timings без query/path-токенов и персональных payload; метрики маршрута, error rate, SQL count/duration, pool checkout, provider/RPC latency, фонового heartbeat. RUM по типу страницы и устройству с LCP/INP/CLS и без subscription_url/email/OTP. Ограничить хранение логов и передавать в существующий сборщик. Отдельно убрать прямой лог magic token в [engine/views.py:2452](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:2452); маршруты login/magic и purchase token должны редактироваться и в access-логах reverse proxy.

**Проверка:** контролируемое замедление становится видно в метриках нужного этапа, секреты отсутствуют, мониторинг не добавляет заметный INP/TTFB. Фактические цели устанавливать после baseline, не выдавать локальный render за production benchmark.

**Сервисы:** website/observability; сквозной request-id согласовать с RWMS/payment/email без breaking protobuf изменений.



## Мобильный интерфейс, загрузка и PWA


Все пункты F относятся к website; межсервисные зависимости указаны отдельно там, где меняются установка/платежи. Проверены 6 страниц × 4 ширины (24 проверки) без горизонтального переполнения. В 20 сочетаниях слайда и короткого viewport кнопка онбординга была перекрыта в 11; F01 содержит условия. Внешние ресурсы заблокированы, использованы fallback fonts. На реальных устройствах Telegram/iOS проверка ещё нужна.

Предварительная оценка технического интерфейса: доступность 2/4, производительность 2/4, адаптивность 2/4, тема 3/4, согласованность реализации 2/4; итого 11/20. Это экспертная оценка проверенной части, не Lighthouse и не сертификат WCAG. Существующая дизайн-система узнаваема; техническая целостность пока не проходит из-за блокирующего onboarding, дублирующихся мастеров и некорректного PWA caching.


<a id="f01"></a>

### F01 [P1] Онбординг перекрывает обязательную кнопку входа на коротких экранах — воспроизведено

**Где:** [engine/templates/login.html:397-445](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/login.html:397), особенно art min-height:260px (:432), slide height:100% (:405), overflow:hidden (:359), margin-bottom:60px (:542).

**Триггер/факт:** 320×568 первый слайд: картинка до y=605, кнопка y=488…546; elementFromPoint в центре кнопки возвращает IMG. Настоящий Playwright locator.click завершился timeout с `img ... intercepts pointer events`. На 320×568 перекрыты центры кнопок всех 5 слайдов; 360×640 первый слайд; 844×390 все слайды (DIV перекрывает кнопку). На 390×844 — работает. Таким образом мобильный пользователь не может завершить обязательный onboarding и перейти ко входу.

**Правильно исправить:** переразметить доступную высоту: отдельные неперекрывающиеся области прогресса/scrollable slide/footer; убрать безусловный min-height:260px у иллюстрации на малой высоте, адаптировать вертикальные отступы, не клипать необходимый контент. Декоративной иллюстрации pointer-events:none как дополнительная защита, не единственное исправление. Сохранить все 5 слайдов и однократность.

**Проверка:** реальные клики на каждом из 5 слайдов при 320×568,360×640,390×844,844×390, увеличенном тексте и safe-area; CTA и legal не пересекаются с картинками; после пятого шага форма доступна, повторное посещение не показывает onboarding.

**Сервисы/риск:** только Website UI, низкий риск при snapshot + функциональных проверках; БД/контракты не меняются.


Браузерный снимок 320×568: иллюстрация перекрывает кнопку.

![Перекрытая кнопка онбординга на 320×568](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docs/audits/2026-09-10/onboarding-320x568.png)


<a id="f02"></a>

### F02 [P1] Service worker сохраняет приватный кабинет под ключом /login/ и показывает его offline

**Где:** [engine/templates/pwa/sw.js:3-15](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/pwa/sw.js:3),35-44; engine/views.py:9433-9442; [dashboard.html:6010-6017](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:6010).

**Сейчас:** SW ставится и из авторизованного кабинета. cache.addAll(['/login/',...]) отправляет запрос с same-origin cookies и следует redirect; login авторизованного пользователя перенаправляет на dashboard. CacheStorage сохраняет весь приватный HTML (email, subscription keys). Logout очищает сессию, но не CacheStorage. На сетевую ошибку любой navigation SW возвращает этот HTML из /login/.

**Доказательство:** локальный синтетический /login/ отдавал реальную dashboard fixture с example@example.test и happ://test, использовался фактический sw.js. cache.match('/login/') содержит оба значения. После отключения сети navigation `/offline-after-logout/` вернул заголовок VPN Monkey Island и HTML с happ://test. Реального logout/production аккаунта не было; неизменность cache при logout подтверждена исходником.

**Исправление:** precache только специально выделенной публичной статической offline page без auth/session/CSRF/subscription данных; никогда не кешировать login redirect/private responses. Выпустить новую версию SW с удалением старого `monkey-island-v2`; ограничить cleanup префиксом собственного приложения (текущий activate:25 удаляет вообще все чужие caches origin). Навигации network-first с безопасной offline fallback; GET static hashed assets можно cache-first; auth/payment/API не кешировать. Один Cache-Control:no-store сам по себе не защищает от явного Cache API put/addAll.

**Проверка:** установка SW как гость и как пользователь A; logout/login B; offline переходы login/dashboard/pay; ни email, ни subscription URL пользователя A не встречаются во всех caches. Existing active keys не регенерировать. Проверить upgrade старого SW и несвязанные caches.

**Сервисы:** Website, клиентское PWA; безопасность подписок не требует менять RWMS/БД.


<a id="f04"></a>

### F04 [P2] Кабинет бесконечно опрашивает полностью скрытый чат каждые 2 секунды

**Где:** [dashboard.html:5231-5246](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:5231),7650-7670,7812-7814. В текущем template вообще отсутствует элемент #support-chat-scroll.

**Триггер:** у клиента есть открытый support_ticket. Форма support-message-form находится в .hidden, но запускает setInterval(refreshSupportMessages,2000) на всех вкладках кабинета. Ответ запрашивает сообщения, затем renderSupportMessages возвращает управление сразу, т.к. chat DOM нет. 30 запросов/минуту на одного такого открытого клиента; 1,000 таких вкладок — до 500 запросов/секунду до browser throttling. Это расчёт кода, не реальная измеренная production нагрузка. setInterval допускает параллельные запросы при ответе дольше 2s.

**Исправление:** если веб-чат заменён Telegram, не запускать polling и не грузить его данные; сохранение API совместимости отдельно. Если веб-чат нужен — возвращать UI осознанно, опрашивать только видимый активный чат + document.visibilityState, single-flight последовательный таймер, backoff, incremental after_id/cursor/ETag. Старую DOM full replacement нельзя переносить в возвращённый чат: она обрывает проигрывание вложений и выделение.

**Проверка:** fixture с открытым ticket и hidden UI: 0 polling calls за 10s; при visible чате нет overlap; hidden tab останавливает polling; восстановление только один request. Соседние Support/Telegram bot API не удалять без проверки потребителей.


<a id="f05"></a>

### F05 [P2] Новый мастер теряет альтернативные установщики; для INCY на Intel Mac выдаёт ARM

**Где:** [dashboard.html:6929-6930](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:6929),6967-6968,7008-7011,8734-8758,8823-8831.

**Сейчас:** конфигурация знает APK Android, .deb Linux, Intel-Mac INCY, но новый mi3 renderConnect выводит только кнопку installUrl и нигде fallbackUrl. Спецветка INCY (:8740) жёстко выбирает ARM DMG для любого macOS и даже не возвращает Intel fallback. Старый мастер умел выводить альтернативу (:7233), но MI_HIDE_SETUP_WIZARD=true (:7152). В браузере Linux шаг 2 содержит лишь основную кнопку; ссылки .deb нет. Пользователь Intel Mac получает несовместимый пакет; пользователь без Google Play не видит APK.

**Исправление:** единый каталог app/platform с альтернативами; показывать Intel/Apple Silicon явным выбором (не угадывать архитектуру по UA), Android APK/.deb как понятные дополнительные ссылки. Обе ветки выбора INCY/Happ используют те же данные. Ссылки необходимо отдельно проверить по официальным release assets при реализации.

**Проверка:** iOS/macOS с happ/incy, Intel/ARM; Android без Play, Linux AppImage/deb; кнопки ведут к нужному installer без изменения subscription deep links. Сверить каталог Telegram bot: UX установки общий, контракт подписки прежний.


<a id="f06"></a>

### F06 [P2] Неизвестный ?tab= оставляет полностью пустой кабинет — воспроизведено

**Где:** [dashboard.html:6020-6041](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:6020),7799-7802.

**Триггер:** stale/manual link `?tab=unknown`: showTab сначала удаляет active у всех tabs, затем не находит target. Браузер: activeTabs=0, main.innerText=''. Спецсимволы в tab также попадают в selector data-tab и могут дать SyntaxError.

**Исправление:** allowlist реально существующих вкладок, проверка target до изменений, fallback home; не интерполировать непроверенный query string в CSS selector. Сохранить реальные deep links tab=settings/support/etc.

**Проверка:** пустой/unknown/старый/с кавычками tab, known tabs, все subscription states; всегда видим осмысленный экран.


<a id="f07"></a>

### F07 [P2] Старые bottom sheets не имеют доступного жизненного цикла диалога

**Где:** [dashboard.html:5406](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:5406) (FAQ),5426 (ref share),5456+ (payments),8291-8315,8355-8368,8459-8472; email sheet:7910+.

**Факт:** Payments History при открытии runtime: role=null, aria-modal=null, background.inert=false, focus=BODY. Эти окна живут вне нового CabinetSheets; show/hide классов не захватывает фокус, не закрывает по Escape/browser Back, не возвращает его. Фокус/скролл могут уходить на фон. Новый CabinetSheets уже реализует большую часть этого (:47-91).

**Исправление:** перевести старые окна на общий lifecycle, добавить role=dialog, aria-modal, labelledby, initial focus, focus trap, inert фон, Escape/Back, восстановление фокуса; сохранить последовательность история → управление автопродлением. Не менять business decision подписки.

**Проверка:** каждый вид диалога мышью, клавиатурой, screen reader, mobile Back/Telegram Back; быстрые повторные open/close; одно окно на экране; после закрытия focus у инициатора.


<a id="f08"></a>

### F08 [P2] У нового мастера фокус исчезает после выбора платформы/шага — воспроизведено

**Где:** [dashboard.html:8778-8797](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:8778),8812-8832; [cabinet-sheets.js:35-43](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/static/js/cabinet-sheets.js:35).

**Сейчас:** renderConnect заменяет весь body через innerHTML при каждом выборе, уничтожая сфокусированную кнопку. После настоящего Playwright click Linux document.activeElement=BODY, хотя dialog открыт. Текущий trap не перехватывает Tab, если focus уже BODY, и выбор не объявляется assistive tech.

**Исправление:** для выбора обновлять состояние существующих кнопок (aria-pressed/checked); при смене шага явно фокусировать заголовок/первый логичный control без прокрутки. Trap должен возвращать focus внутрь, если тот вне active dialog.

**Проверка:** выбрать платформу/приложение только клавиатурой, пройти вперёд/назад; focus всё время внутри и явно виден; screen reader озвучивает шаг. Не требуется переписывать приложение на framework.


<a id="f09"></a>

### F09 [P2] Вход запрещает увеличение и onboarding оставляет фон интерактивным

**Где:** [login.html:6](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/login.html:6) (maximum-scale=1,user-scalable=no),617 (onboarding без role/modal),885-945 (нет управления focus/inert),725 (email без label),729 (ошибка без live region); [tg_webapp.html:5](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/tg_webapp.html:5).

**Влияние:** часть браузеров запрещает пользователю с плохим зрением увеличить форму; Tab уходит в скрытую визуально за onboarding форму; ошибка отправки не объявляется, после success фокус остаётся в скрытом step-input.

**Исправление:** разрешить масштабирование, семантический label и autocomplete=email, ошибки role=alert/aria-describedby, focus на success; onboarding как accessible dialog с inert фоном и фокусом, перевод focus на email после завершения. Пятислайдовый сценарий сохраняется.

**Проверка:** zoom/увеличение текста 200%, keyboard-only, NVDA/VoiceOver; ошибки 400/429/network; повторное открытие формы; low-height кейс F01.


<a id="f10"></a>

### F10 [P2] Нейтральные домены снова называют продукт VPN на /login/ и в manifest

**Где:** [views.py:1667-1670](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:1667) включает один onboarding для vps/vps_direct_sale; [login.html:10](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/login.html:10),15,617,629,661; [views.py:9951-9954](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:9951) neutral `vps` не учтён (только vps_direct_sale получает VPS name).

**Факт:** login subtitle условно скрыт для neutral (:692), но title, app title и 5 слайдов содержат прямое VPN позиционирование без проверки site_role. Это противоречит данному AGENTS требованию для neutral domains.

**Исправление:** role-aware словарь/шаблон onboarding и metadata для vpn, cabinet, vps, vps_direct_sale; 5 преимуществ в neutral формулировках без необоснованных новых обещаний; manifest имя для обеих neutral ролей. Перед правкой copy согласовать существующие формулировки product/README, не менять продукт/тарифы.

**Проверка:** render каждой роли login/index/manifest; neutral не содержит VPN в пользовательских заголовках/доступных именах/метаданных. Кабинетный onboarding не показывается.


<a id="f11"></a>

### F11 [P2, performance] Платёжные страницы грузят 2.56 MB декоративного PNG

**Где:** [payment_status.html:38](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/payment_status.html:38) и [wata_payment.html:34](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/wata_payment.html:34) → static/img/header-monkey-island.png = 2,557,539 bytes; dashboard_collect_email.html:34 → bg10.png=2,030,542 bytes. Это CSS backgrounds, без responsive sizes или lazy-load. Также поддержка использует bg6.png=1,527,219 bytes (админ-аудит).

**Влияние:** изображение оплачивается трафиком и декодированием именно при критичном переходе к оплате/ожиданию активации; не нужно для функции формы. На медленном мобильном соединении конкурирует с рабочими ресурсами. Цифры размеров measured locally; реальный waterfall предстоит снять.

**Исправление:** заменить оптимизированным WebP/AVIF с mobile/desktop размерами через image-set/media либо CSS-фоном достаточного качества; сохранить форму/статусы видимыми до картинки, не preload decorative asset. Для onboarding 5 WebP суммарно 1,176,992 bytes (221,114 +290,968 +239,150 +203,204 +222,556), каждый 720px при display<=340px: подготовить меньшие варианты srcset, не ухудшать первый slide LCP. Не считать весь static=47MB загрузкой страницы: большинство старых PNG не используется!

**Проверка:** network transfer/decode/LCP до/после на 360px/slow 4G, визуальная проверка качества, отсутствия CLS, высоких DPI. Ограничить бюджет на background и onboarding отдельно.


<a id="f12"></a>

### F12 [P2, performance] Кабинет пересылает большой общий код/стили с каждым приватным HTML

**Где:** [dashboard.html:80-4664](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:80) inline CSS,6009-9132 inline JS; installData:6080+; отключённый legacy master:7150-7155, но initialization:7785-7795 выполняется.

**Замеры:** файл 444,956 bytes; gzip самого исходника 71,222 bytes (не production response!), inline CSS 155,821, inline JS181,261 bytes; installData catalogue block 47,406 symbols. tw.css всего30,003 bytes (gzip6,146), cabinet-sheets4,080, cabinet-mobile7,958. Shared JS/CSS нельзя отдельно reuse из browser HTTP cache при переходах login/payment/dashboard; браузер разбирает несколько накопленных UI реализаций, скрытый wizard всё равно рендерится.

**Исправление:** без большого рефактора вынести общие CSS/JS в версионированные immutable static files, динамические значения через безопасный json_script; каталог установщиков отдельно, сохранить функции внешних onclick на переходный период. Удалять только доказанно недостижимые legacy блоки после покрытия сценариев; не инициализировать disabled wizard, QR dependency загружать при открытии QR. Backend response остаётся приватным.

**Проверка:** repeat navigation transfer, parse/eval/long tasks на throttled Android CPU, доступность всех install platforms, expired/paid/trial/Telegram states, referral/email/settings/payment. Bundle/DOM budgets, отсутствие изменения callback/deep-link contracts.


<a id="f13"></a>

### F13 [P2, performance] Внешние ресурсы блокируют критический рендер

**Где:** [dashboard.html:78](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:78) FontAwesome render-blocking CSS; :20 Telegram SDK синхронный только tg_webapp_mode; [tg_webapp.html:8](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/tg_webapp.html:8) синхронный SDK + stylesheet fonts:11; [payment_status.html:630](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/payment_status.html:630) SDK перед контентом. offer/privacy/terms:12 @import Google Fonts, а также blocking FontAwesome. [login.html:19](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/login.html:19) загружает Montserrat помимо Inter:20, хотя финальные CSS:572-582 используют Inter.

**Исправление:** собственный SVG sprite для реально используемых иконок (у кабинета уже есть includes/cabinet_icons.html), необходимые woff2 subset локально/с cache-control, убрать unused Montserrat после проверки fallback; Telegram SDK defer с согласованным deferred initializer/DOMContentLoaded, не просто defer при остающемся раннем inline init. QR при первом использовании. Критичный UI показывает usable shell при CDN failure.

**Проверка:** намеренно задержать/заблокировать telegram.org,fonts.googleapis.com,cdnjs на 10s; форма/платёжный статус доступны, init TG один раз после загрузки. В реальном TG проверить ready/expand/BackButton.


<a id="f14"></a>

### F14 [P2] Loading состояния fetch могут висеть неограниченно

**Где:** [login.html:957-972](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/login.html:957), [dashboard.html:7755-7766](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:7755) (оплата),9055-9075 (devices),8385+ (cancel autopay), [wata_payment.html:210-235](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/wata_payment.html:210) (setInterval active checks).

**Сейчас:** отсутствуют deadlines/AbortController; кнопка login disabled до завершения запроса; устройства бесконечно «Загружаем» при зависшем ответе; активный Wata interval 8s допускает наложения. Payment/status polling retries бесконечно без visibility/backoff/terminal HTTP handling. В flaky mobile сети это функциональная недоступность, хотя соединение не выбросило exception.

**Исправление:** единый fetch helper с явным сроком UI ожидания, single-flight, abort на уходе/отмене, понятная ошибка+повтор; для reads повторы/backoff+jitter+visibility, для payment mutations только стабильный intent F03, для cancel autopay подтвердить состояние после unknown outcome. Не показывать успех по факту timeout. У status polling выделять 401/403/404/expired-token как требующие действия, не маскировать вечным pending.

**Проверка:** запрос не отвечает, connection drop, background/foreground, 401/403/404/429/503, повторные клики; через установленный UX deadline пользователь получает управляемое состояние, нет overlap/duplicate mutations.


<a id="f15"></a>

### F15 [P2] Загрузка устройств не защищена от устаревшего ответа

**Где:** [dashboard.html:9055-9071](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/dashboard.html:9055),9089-9092 и8913-8945.

**Сценарий по коду:** initial home loadDevices + немедленное открытие sheet запускают 2 GET. Нет single-flight/request sequence. Ответы вне порядка перезаписывают devicesCache/домашний список; более старый GET, пришедший после удаления, может вернуть удалённое устройство визуально. Также background API error silently сохраняет старый список, пользователь не знает, что не обновился.

**Исправление:** общий in-flight GET, sequence/version guard и invalidation после mutation; optimistic удаление только после API success, ignore старых результатов. Чёткий stale/error indicator при показе кеша, TTL и обновление при возвращении из клиентского приложения (сейчас home «В сети» может стареть бессрочно).

**Проверка:** задержанные ответ A/B в обратном порядке, GET-start → delete-success → old GET response; UI не восстанавливает удалённое устройство, API GET не дублируется; zero mutations при чтении/открытии.



## Админка и инфраструктура: функциональные дефекты


<a id="ai-01"></a>

### AI-01 / P1. Stored XSS в рекламе позволяет маркетологу исполнять код в сессии полного администратора

- **Место:** [engine/templates/admin_dashboard.html:15576–15578](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/admin_dashboard.html:15576) (`acqTable`), `:15761–15765` (`loadAds`). Запись: [engine/views.py:12267–12284](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:12267), чтение `:11017–11022`; `ANALYTICS_ROLES` `:899` содержит admin+marketer.
- **Сейчас:** `channel`, `account`, `comment` сохраняются как строка с strip/truncate. Ячейки таблицы вставляются непосредственно в `innerHTML`. Маркетолог может вписать HTML в поле комментария/канала/аккаунта. При открытии Ads полным администратором HTML обработчик выполнится в его origin с его ролью, доступом к DOM и CSRF токену. Это не только self-XSS: роли различаются.
- **Подтверждение:** извлечённая из текущего шаблона `acqTable` получила безопасный локальный маркер `<img src=x onerror="window.__auditProof=1">`; результирующий HTML сохранил его исполняемой разметкой. Тест не открывал прод и не отправлял сетевых запросов.
- **Исправление:** сделать ячейки текстовыми по умолчанию (`textContent`/экранирование); HTML кнопок задавать отдельным ограниченным типом/DOM builder. Экранировать данные на output, не «лечить» удалением символов во входных строках. Проверить остальные вызовы `acqTable`/`acqCard` на данные из БД. CSP — дополнительная защита после удаления inline handler, не замена исправлению.
- **Проверка:** браузерный тест marketer сохраняет вредоносный комментарий через mocked API, admin открывает список; текст виден буквально, обработчик не выполнен, кнопки работают. Проверить кавычки, `<`, `&`, кириллицу.
- **Сервисы:** website. Прямых изменений контрактов других сервисов нет; потенциальный ущерб охватывает админские операции сайта.


<a id="ai-02"></a>

### AI-02 / P1. Подделка IP в bootstrap и admin audit через X-Forwarded-For

- **Место:** [engine/views.py:12450–12454](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:12450), `:14854–14872`; [docker/edge/nginx.conf.template:41](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/edge/nginx.conf.template:41), [docker/website/nginx.conf.template:57](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/docker/website/nginx.conf.template:57); [engine/node_provisioning.py:374–411](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/node_provisioning.py:374).
- **Сейчас:** оба nginx добавляют входной `X-Forwarded-For` через `$proxy_add_x_forwarded_for`, а сайт доверяет первому элементу. Его задаёт клиент. Адрес используется не только в журнале: этим IP привязывают bootstrap token и регистрируют Remnawave node. После claim подделка известного bound IP обходит его проверку; наличие самого token по-прежнему требуется.
- **Исправление:** на внешней доверенной границе перезаписывать клиентский заголовок реальным IP; внутри цепочки учитывать только известные proxy hops. В приложении единый trusted-proxy resolver, `ipaddress` нормализация и запрет отсутствующего/непригодного node IP. Нельзя просто везде брать последний XFF без знания схемы edge→website.
- **Проверка:** запрос с подставленным XFF через обе конфигурации даёт фактический адрес клиента; поддельный IP после claim отвергается, нормальный IPv4/IPv6 и доверенные hops работают.
- **Сервисы:** website/edge, RWMS получает корректный адрес без изменения protobuf. Общий аспект rate limiting описан в OPS-04.


<a id="ai-03"></a>

### AI-03 / P1. Сбой подключения к БД навсегда останавливает infra worker

- **Место:** [engine/infra_worker.py:2498–2500](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/infra_worker.py:2498), `:2534–2537`.
- **Триггер:** БД недоступна/пул исчерпан именно во время `engine.connect()` в начале итерации лидерства.
- **Сейчас:** connect расположен до `try`. Исключение выходит из daemon thread. `_started` остаётся True, повторный `start()` ничего не запускает. Пропадают offline алерты, агрегация, обработка аномалий и замены IP до рестарта процесса. В multi-worker схеме другой процесс может продолжить работу, но одновременный сбой connect у всех оставит весь сервис без infra worker.
- **Подтверждение:** AST-selected `_leader_loop` с mock engine raising RuntimeError: ошибка вышла из функции, `connect` вызван 1 раз, `sleep` 0 раз.
- **Исправление:** создание connection внутри защищённого try, `conn=None` и безопасный finally; backoff+jitter на reconnect. Хранить thread и проверять `is_alive()` либо запускать infra отдельным процессом с supervisor. Метрика last successful tick/heartbeat с независимым оповещением, чтобы проверяющий поток не был единственным источником сигнала о себе.
- **Проверка:** первая попытка connect падает, следующая успешна и проходит maintenance; закрытие освобождает advisory lock. Тест на all-workers reconnect; не запускать на действующей БД.
- **Сервисы:** website worker; косвенно мониторинг ip-guard, RIPE Atlas и Cloudflare, без правок подписок/платежей.

**Второй экземпляр того же дефекта:** [engine/censor_worker.py:65](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/censor_worker.py:65) также вызывает engine.connect до try; включить оба фоновых воркера в одну исправляющую задачу и тест reconnect.


<a id="ai-04"></a>

### AI-04 / P1. Bootstrap принимает чужую существующую node как результат создания и может показать ложный READY

- **Место:** [engine/node_provisioning.py:391–411](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/node_provisioning.py:391), `:490–493`; соседний [monkey-island-rwms/server.py:724–735](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-rwms/server.py:724).
- **Триггер:** новую заявку создают с именем существующей ноды, но запускают на другом сервере; либо адрес совпадает с существующей нодой при другом имени/профиле.
- **Сейчас:** RWMS считает совпадение `node.name == request.name OR node.address == request.address` идемпотентным и возвращает старую node. Сайт проверяет только непустой UUID. Заявка связывается со старой node, а после installed старый работающий `is_connected` превращает её в READY. Новый сервер может остаться совершенно неуправляемым панелью, пока админ видит успех.
- **Подтверждение:** AST-selected claim/refresh с mocked RWMS: `claimed_ip=8.8.8.8`, возвращённая node address=1.1.1.1, UUID старый; итог READY.
- **Исправление:** сайт проверяет соответствие name/address/profile/inbounds результата заявки; несовпадение — явный conflict, не принятие успеха. RWMS отличает точный retry от конфликта name/address и возвращает ALREADY_EXISTS/FAILED_PRECONDITION; protobuf поля не переименовывать. При повторе после неопределённого исхода сверять immutable request identity; существующую node не менять и не пересоздавать.
- **Проверка:** совпадает только name, только address, оба совпадают и profile соответствует, иной profile, retry после потерянного ответа. Ложный connected на старом UUID не выдаёт READY.
- **Сервисы:** website + RWMS; безопасность действующих Remnawave node. Пользовательские UUID/URLs не изменять.


<a id="ai-05"></a>

### AI-05 / P1. Claim bootstrap token не атомарен

- **Место:** [engine/node_provisioning.py:318–324](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/node_provisioning.py:318), `:374–411`; commit view [engine/views.py:14872](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:14872).
- **Триггер:** два HTTP POST claim одного ещё CREATED token параллельно с разных серверов или claim одновременно с admin revoke.
- **Сейчас:** SELECT без row lock/условного UPDATE. Обе сессии видят CREATED и проходят без IP-check; внешние RPC выполняются до фиксации binding. Последняя запись claimed_ip побеждает. Revoke также может быть перезаписан результатом уже начавшегося claim. Уникальный token_hash исключает дубли строк, но не гонку состояния одной строки.
- **Исправление:** атомарно захватить заявку и зафиксировать допустимый переход/claimed_ip до выдачи payload, сохранив retry с того же IP и запрет чужого. Простой вариант — ORM `with_for_update` с ограниченными RPC deadlines; более устойчивый — короткий compare-and-set claim reservation и reconciliation внешнего CreateNode. Revoke должен сериализоваться с claim и отрубать выдачу после фиксации. Учитывать AI-04.
- **Проверка:** два независимых ORM session с barrier; ровно один первый IP получает успех, второй 403; повтор того же IP идемпотентен; revoke-vs-claim не оживляет revoked. Для этого нужен изолированный disposable test DB, не local dev DB. В текущем аудите гонка подтверждена анализом контроля конкурентного доступа, не воспроизводилась записью в БД.
- **Сервисы:** website + согласованная идемпотентность RWMS.


<a id="ai-06"></a>

### AI-06 / P2. Карточка установки отображает другую заявку, а кнопка отзыва действует на выбранную

- **Место:** [engine/templates/admin_dashboard.html:11664–11693](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/admin_dashboard.html:11664), `:11716–11719`.
- **Триггер:** открыть заявку A, затем B при медленном ответе A. Ответ B приходит первым, затем A.
- **Сейчас:** `nodeProvisionDetailId` уже B, но ответ A без проверки актуальности заменяет заголовок/лог. Revoke берёт глобальный B. Таким образом видно A, отзывается B. В отличие от infra detail, здесь нет sequence guard. Polling каждые 5 секунд без in-flight guard и timeout может дополнительно наслаивать GetNodes RPC. `openNodeProvisionDetail` также вновь запускает timer после ответа terminal-state, несмотря на stop внутри refresh.
- **Подтверждение:** локальный Node VM с извлечённой функцией и управляемыми обещаниями дал `selected request:2`, `displayed:#1 · node-1 installed`.
- **Исправление:** захватывать ID+generation запроса, принимать только совпадающее состояние, отменять прежний fetch; disable mutation до совпадения rendered ID и selected ID. Следующий poll после завершения текущего через setTimeout, skip document.hidden, deadline/backoff; terminal state не запускать timer повторно.
- **Проверка:** искусственно обратный порядок ответов A/B, закрытие/смена вкладки, ready в первом ответе, медленнее 5с, transport error; на экране и в POST один ID, максимум один poll.
- **Сервисы:** website; снижается нагрузка read-only GetNodes в RWMS.


<a id="ai-07"></a>

### AI-07 / P2. Недельная рекламная аналитика скрывает недели с расходами и нулём оплат

- **Место:** [engine/views.py:10991–11012](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:10991).
- **Сейчас:** `_acq_ads` строит итог `for r in pays`; расход, регистрации и подключения присоединяются только к неделям, в которых есть хотя бы какой-то платёж. Неделя расхода с нулевыми продажами не показана, хотя именно она важна для выявления неэффективной рекламы.
- **Подтверждение:** mock rows: расход1000₽, 10 подписок, 4 подключения, платежей0 → `spends` содержит строку, `weeks=[]`.
- **Исправление:** календарная сетка недель выбранного интервала либо объединение ключей всех рядов, явные нули для оплат/выручки. Политику ROMI/CAC при нулевых знаменателях зафиксировать, не показывать отсутствие недели.
- **Проверка:** неделя только расходов, только оплат, только регистраций, полностью пустая неделя, границы MSK/UTC.
- **Сервисы:** website analytics; финансовые записи и платежные сервисы не менять.


<a id="ai-08"></a>

### AI-08 / P2. «Активность за 24 часа» и геодоли используют накопительные hits за всё время жизни наблюдения

- **Место:** [engine/infra.py:952–960](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/infra.py:952), `:1028–1037`, `:1096–1115`; подпись UI [engine/templates/admin_dashboard.html:14699](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/admin_dashboard.html:14699). Модель [common/models/db.py:663–696](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/common/models/db.py:663); writer соседнего [monkey-island-ip-guard/main.py:270–283](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-ip-guard/main.py:270).
- **Сейчас:** запись единственная на `(username,ip,node)`, каждый ingest делает `hits = hits + new_hits`, пока адрес остаётся активным запись не очищается. Сайт фильтрует `last_seen >= now-24h`, но суммирует весь накопленный hits. У адреса100000 подключений за месяц и1 сегодня получается100001 «за24ч». Геодоли и top ranking смещены к долго существующим IP. `unique_ips_24h` по последней активности при этом корректна.
- **Исправление:** безопасная краткая правка — точно подписать lifetime-значение («накопленные обращения активных за сутки адресов») и не трактовать как24ч. Для действительной24ч статистики — отдельные интервальные агрегаты hits с retention, наполняемые ip-guard; existing observation/detector semantics не менять. Изменения модели оформить common, миграцию генерирует и применяет пользователь своим скриптом.
- **Проверка:** старые hits+один свежий ingest, long-running IP, retention, независимые username на одном IP, суммы геодоли.
- **Сервисы:** website + ip-guard + common при полноценной временной статистике; правила детектора не менять.


<a id="ai-09"></a>

### AI-09 / P2. Аналитические формы принимают запоздалый ответ и оставляют вечную загрузку после сетевого сбоя

- **Место:** [engine/templates/admin_dashboard.html:7829–7839](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/admin_dashboard.html:7829) (`loadStats`), `:7382–7401` (`loadCohortStats`), `:15581–15587` (`acqFetch`).
- **Сейчас:** повторная смена периода запускает параллельный fetch без cancel/sequence; старый медленный запрос может перезаписать новый отчёт под уже новыми фильтрами. В `loadStats` нет catch, поэтому reject fetch/не-JSON после setLoading оставляет spinner навсегда. Для heavy SQL это одновременно лишняя серверная работа.
- **Исправление:** общий loader с AbortController+generation, проверкой HTTP и schema/status, try/catch/finally, состоянием ошибки и retry, disabled submit или latest-wins. Отмена браузером сама по себе не останавливает SQL: серверу отдельный deadline/cancellation policy. Снимок применённых фильтров рядом с результатом.
- **Проверка:** искусственная задержка первого/второго периода, fetch reject, HTML502, bad JSON, повтор после ошибки, долгий запрос с новым выбором.
- **Сервисы:** website analytics.


<a id="ai-10"></a>

### AI-10 / P2. Администратору каждые7с сбрасываются видео и выделение текста в support чате

- **Место:** [engine/templates/support_admin_ticket_detail.html:309](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/support_admin_ticket_detail.html:309), `:317–334`, `:412`; video element создаётся `:214`.
- **Сейчас:** каждый успешный polling, даже с полностью неизменёнными сообщениями, вызывает `renderMessages`, который заменяет `adminChat.innerHTML` целиком. Старый `<video>` удаляется, воспроизведение/currentTime и text selection теряются; media metadata запрашивается повторно. `knownMessageIds` применяется только к звуку, не к diff DOM. Видимость страницы и in-flight guard уже проверяются, их не считать отсутствующими.
- **Исправление:** keyed DOM по message.id, append только новых/patch изменённых сообщений, skip no-op response; сохранить media elements и выделение. Для длинной истории cursor pagination/incremental since_id endpoint. Scroll-to-bottom только если пользователь уже внизу/отправил своё сообщение.
- **Проверка:** playing video и выделенный текст переживают два одинаковых poll; приходит новое сообщение — старый video DOM node тот же, currentTime не сброшено, scroll не прыгает; new-message sound ровно один раз.
- **Сервисы:** website support UI/API. Не путать с клиентским cabinet: там текущий шаблон скрывает support chat, поэтому аналогичный «сброс видео на каждые2с» для кабинета не заявляется.



## Админка и инфраструктура: ускорение


<a id="ap-01"></a>

### AP-01 / P1. Разделить быстрые текущие данные infra и тяжёлую аналитику

- [engine/infra.py:695–831](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/infra.py:695) каждый detail включает `who_connects`; `:849–872` cache per-process с lock только get/set, вычисление вне lock; `:940–981` несколько агрегаций наблюдений, `:1028–1035` полный GROUP BY по IP + GeoIP Python loop. При cold cache/границе минут каждая параллельная вкладка/worker считает всё заново. UI `:13865–13871` ещё ждёт detail+telemetry через Promise.all, поэтому медленный history блокирует показ live state.
- План: быстрый endpoint snapshot/state; отдельный lazy analytics endpoint с cached timestamp, stale-while-revalidate и shared single-flight lock; общий GROUP BY/conditional counts вместо повторных сканов там, где EXPLAIN подтверждает пользу. Detail и chart рисовать независимо. Сохранить уже добавленный `(node,last_seen)` индекс; не предлагать его повторно как отсутствующий.
- Приёмка: p50/p95 отдельно cold/warm cache; одновременные10 запросов одного server дают1 пересчёт analytics; быстрые показатели показываются при задержанном chart; render не теряет dirty fields.
- Сервисы: website, БД ip-guard read-only; writer только при AP/AI-08 redesign.


<a id="ap-02"></a>

### AP-02 / P2. Аггрегация телеметрии делает SELECT и запись на каждый bucket

- [engine/infra.py:2655–2669](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/infra.py:2655), `:2678–2712`, `:2766–2833`: все servers, сырьё `.all()`, Python grouping, `_upsert_agg` SELECT для каждого bucket. При backlog/new node стоимость растёт с числом минут и нод, а весь aggregate шаг одна транзакция. `statement_timeout` не задаёт лимита всей Python функции.
- План: query профилинг; ORM/SQLAlchemy expressions для set-based aggregate и PostgreSQL dialect upsert batches; bounded backlog chunks per node; watermark+пересчёт небольшого окна задержанных samples. Сохранить rounding, NULL, byte integration, 60/900 periods и идемпотентность. Не писать raw migrations.
- Приёмка: сравнить результат с эталоном для gaps/NULL/late samples, число SQL round trips на фиксированный backlog, RSS, время каждого тика; корректно догоняет после downtime.
- Сервисы: website worker, shared telemetry writer ip-guard; schema unchanged вариант предпочтителен.


<a id="ap-03"></a>

### AP-03 / P2. Админка отправляет всем разделам большой HTML/JS и грузит редактор до открытия

- Шаблон [engine/templates/admin_dashboard.html](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/admin_dashboard.html) на диске1,284,911 байт; style начинается:29 и идёт до3264, основной JS5637–15340 и далее. Указанные размеры — source template, не network transfer: условия ролей и gzip меняют итог. CodeMirror CSS/CDN JS грузятся всем на:26–27 и5633–5636; отдельный CSS admin_dashboard140,901 bytes. `:15335` `loadStats()` стартует даже когда текущий раздел не Analytics.
- План: измерить rendered HTML/transfer size, parsing/evaluation и long tasks на мобильном CPU; вынести стабильные JS/CSS в cacheable versioned assets, lazy-load CodeMirror при открытии редактора, heavy panel markup/loaders по активной вкладке, не запускать stats на чужой вкладке. Сохранять доступность всей навигации, deep-links и роли. Избегать большой визуальной переделки.
- Приёмка: cold/warm LCP/INP/transfer на support и infra отдельно, отсутствие запросов stats/CodeMirror до открытия, тесты роли full/marketer/support, mobile navigation.
- Сервисы: website + его static delivery.


<a id="ap-04"></a>

### AP-04 / P2. Список support тикетов не пагинирован

- [engine/views.py:4699–4721](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:4699) `.all()` всех open/closed/all ticket rows + full User ORM, затем payload; [engine/templates/admin_dashboard.html:11458–11475](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/templates/admin_dashboard.html:11458) повторная полная выгрузка и `innerHTML` каждые15с. История all/closed растёт неограниченно; начальная admin HTML загружает тикеты даже при открытии другой вкладки `support_admin_tickets:4188–4193`.
- План: cursor pagination по `(updated_at,id)`, проекция необходимых колонок, lazy first page; отдельный compact counters/changed-since endpoint или revision/ETag; patch изменённых строк вместо полного redraw, не терять фокус и scroll.
- Приёмка: тысячи тикетов на синтетическом fixture, размер ответа ограничен, новые/перемещённые/закрытые тикеты не теряются и не дублируются, no-op poll почти пустой.
- Сервисы: website; lifecycle тикетов/support не менять.


<a id="ap-05"></a>

### AP-05 / P2. Отчёт трафика синхронно обогащает до500 клиентов отдельными RWMS RPC

- [engine/node_traffic.py:136–144](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/node_traffic.py:136), `:178–189`: after bounded node fetch concurrency3, каждый selected user получает GetUserByUuid, concurrency8; max500. Уже есть разумная ограниченная параллельность, увеличивать её вслепую нельзя. Один slow RPC задерживает весь executor/report. OPS-01 рассматривает отсутствие RPC timeout в common client.
- План: быстрый aggregate response, карточки/детали по запросу для видимой страницы, короткий cache user summary, bounded request-wide deadline; fallback partial states, sharing work между identical reports. Если нужен batch RPC — только additive protobuf endpoint и совместимый rollout.
- Приёмка: N=50/500, вызовов profile лишь на видимые строки, unavailable RWMS даёт ограниченное ожидание и partial report, цифры трафика не меняются.
- Сервисы: website + RWMS при bulk method; существующие RPC сохранить.


<a id="ap-06"></a>

### AP-06 / P2. Cloudflare DNS client теряет connection pooling и повторяет отрицательный поиск zone

- [engine/cloudflare_dns.py:44–50](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/cloudflare_dns.py:44): новый `httpx.Client` на каждый HTTP вызов. `:82–92`: cache положительной parent zone не запоминает результат поиска исходного subdomain; каждый последующий lookup `de.example.com` повторяет GET zones name=de.example.com перед попаданием в cached example.com. Много доменов × steps → повторные TLS handshakes и lookup latency.
- План: управляемый long-lived HTTP client на worker; bounded cache domain→resolved zone с TTL/invalidation при404/смене token, осторожно сохранить приоритет дочерней зоны. Retry с backoff для429/5xx; общий deadline DNS step и rate budget. Стейт-машину ADD-before-DELETE сохранить.
- Приёмка: fake transport считает число connect/request на три последовательные операции; child zone discovery не регрессирует; retry mutation idempotent; нет раскрытия token в логах.
- Сервисы: website worker/Cloudflare integration.


<a id="ap-07"></a>

### AP-07 / P2. Таймаут шага infra ограничивает отдельный SQL, но не длительность обслуживания

- [engine/infra_worker.py:2420–2435](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/infra_worker.py:2420): `SET LOCAL statement_timeout` ограничивает каждый statement, не весь `fn`. `run_maintenance:2468–2479` шаги идут последовательно; внешний DNS/RIPE/Telegram и Python loops удлиняют tick сверх интервала. Check count-based cadence (`_ANOMALY_EVERY_TICKS`, `_DNS_WATCH_EVERY_TICKS`) при долгих шагах становится намного реже обещанных минут.
- План: измерять duration/lag каждого шага; monotonic schedule по due timestamp вместо «каждый N тик»; deadline внешних операций и bounded work per tick, сначала быстрые alerts. Выделить worker from web service при доказанной конкуренции за pool/CPU, сохранив один лидер и безопасную передачу ownership.
- Приёмка: медленный mocked DNS/Atlas не отодвигает offline check сверх SLA; backlogs частично завершаются и продолжаются, leadership recoverable.
- Сервисы: website worker, integrations; subscription services не меняются.


<a id="ap-08"></a>

### AP-08 / P2. Аналитика повторно сканирует историю и создаёт временную cohort table на каждый запрос

- [engine/views.py:4997–5067](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:4997), `:6356–6375`: уже устранены9 повторных оконных подзапросов внутри одного отчёта материализацией temp table; это полезное имеющееся улучшение. Но каждый новый `loadStats`, даже на другую вкладку, снова ROW_NUMBER по всем subscription_created до end, temp create/insert/analyze. ACQ handlers отдельно несколько раз строят first-pay CTE по общей истории.
- План: cache готового отчёта по normalised filters+role scope с коротким TTL; запросы запускать только по намерению; read-only EXPLAIN representative ranges; при достаточном объёме инкрементальные first-subscription/first-payment read models с корректным merge user semantics. Не менять единый источник истины и не подменять бизнес-события.
- Приёмка: p95/SQL calls для cold/warm и concurrent identical filters; сравнение cohort результата при повторных create events, merge пользователя, late payment; invalidation корректна.
- Сервисы: website только для caching; при новых read models common и writers event/payment/RWMS зависят от выбранного ingestion.



## Дополнительные проверки, не включённые в число подтверждённых багов

- **Fail-open проверки нового IP:** [engine/infra_worker.py:1694–1734](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/infra_worker.py:1694), `:1756–1768`, `:1907–1933` допускают публикацию кандидата при отсутствии/ошибке контрольных probes. По политике09.09 публикация доменов независимо от отказа SNI сознательна и не ошибка сама по себе (`infra_diagnosis.domains_safe_to_repoint`, `_replacement_step_verifying_names`). Но отсутствие доказательства живого IP — отдельный вопрос. Проверить и документировать fallback policy; безопасный вариант pause/manual_required при unavailable control, preserve старый DNS до подтверждения. Тест — все probes unavailable и no healthy evidence, Cloudflare mutation не вызывается (если выбрана fail-closed политика). Это изменение поведения требует решения владельца, не делать его автоматически в рамках аудита.
- **External side effects vs DB transaction:** process_replacements `:1306–1325` ловит ошибку одной заявки в общей сессии, а `_run_step` коммитит весь набор позднее. После удалённого DNS ADD/DELETE и failed flush состояние может откатиться; операции в основном идемпотентны, это плюс. Нужны fault-injection тесты после каждого external call и до commit, nested transaction/per-item retry и reconciliation при необходимости. Не заявлять «данные уже потеряны» без воспроизведения.
- **Invalid admin input →500:** например `node_provision_detail` [views.py:15383–15385](/Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/engine/views.py:15383) и POST revoke parse `:15280–15283` делают `int` без ValueError handler; аналогично infra IDs. Central bounded input parsing должен давать400 и message. Приоритет ниже критических defects; тесты bad ids/unknown object.
- **Cloudflare pagination/record state:** list_a_records `:103` читает только per_page100; ensure_a_record `:124–132` принимает существующую запись по content независимо от proxied/TTL. При >100 same-name A records или concurrent ручной правке можно получить incomplete/некорректное представление. Пока в репозитории нет доказательства такой конфигурации. Протестировать controlled fixtures, держать fail-safe перед delete.



- **Mobile registration events:** проверить, нужен ли в mobile_api/provisioning.py тот же subscription_created/UserTrafficProgress, что у сайта/бота. При необходимости добавить событие ровно один раз после успешного provision; сверить cohort/notify/ym-stat на одном пользователе. Отсутствие события пока не объявляется багом без контракта аналитики.
- **YooKassa pending при потере webhook:** проверить срок жизни и recovery отменённого платежа; UI может остаться pending при отсутствии callback. Исправление — reconciliation по точному provider ID с известным терминальным статусом, не произвольное объявление failed по таймеру. Проверить late success/cancel и повтор callback.
- **UserBlock fail-open и aborted transaction:** engine/user_block.py ловит исключение, но вызвавшая SQL ошибка могла перевести общую transaction в aborted. Изолировать проверку savepoint/отдельной read session или явно отказывать до мутации; не делать blind rollback уже подготовленной оплаты. Проверка должна имитировать настоящий SQL error и следующую ORM операцию на согласованной тестовой БД. Политику fail-open для обычного просмотра и для оплаты/выдачи секрета определить отдельно.
- **Ошибочные HTTP-методы:** send_magic_link на GET проходит без return, что даёт серверную ошибку вместо 405. Добавить require_POST и тест методов; pay на GET уже redirect, его не считать таким же дефектом. Это небольшое исправление можно включить в B13/OPS-04.

## Критерии выпуска и сохранение результата

Для каждого PR: описать текущий дефект/условие, точное изменение, риск и сервисы; добавить регрессионный тест перед исправлением; провести согласованные сценарии отказов и обновить README. Миграции общей схемы не генерировать и не применять агентом. Проверки с записью — только в отдельно согласованном изолированном стенде; текущая локальная dev-БД должна оставаться эталоном autogenerate.

Минимальная сквозная матрица: гость/владелец/чужой пользователь/заблокированный; активная платная/trial/истёкшая подписка; Wata/YooKassa success/pending/cancel/timeout; новый и существующий аккаунт; mobile email/Telegram/OAuth; роль admin/marketer/support; каждое семейство доменов. Повтор запросов, обратный порядок ответов, restart между внешним действием и commit, неполадки RWMS/email/payment должны проверяться специально.

После исправлений один полный регрессионный проход и сравнение baseline. Для UI уместны технический повторный audit и финальная небольшая polish-проверка; отдельный визуальный редизайн в этот план не входит. Развёртывание — малыми согласованными изменениями с наблюдаемыми метриками и rollback, сохраняющим данные и активные клиентские конфигурации.
