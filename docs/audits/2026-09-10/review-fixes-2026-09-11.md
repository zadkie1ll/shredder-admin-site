# Ревью правок ИИ, исправления и план деплоя — 11 сентября 2026

Состояние на вечер 11 сентября, после финального ревью и его исправлений. Правки
лежат в рабочих копиях `monkey-island-website` (вместе с submodule `common`) и
`monkey-island-vpn-bot`, ничего не закоммичено и не выкачено. Правки ИИ в
`monkey-island-payment` откатаны, payment в релиз не входит.

Этот документ заменяет [deployment-recheck-2026-09-11.md](deployment-recheck-2026-09-11.md)
и [implementation-fixes-2026-09-11.md](implementation-fixes-2026-09-11.md). Детали
реализации описаны в README сайта (раздел «Защита и производительность критических
сценариев (2026-09-10)»), README бота («Привязка аккаунтов и правила merge»,
«Операции сайта и merge»), `common/README.md`, `docker/website/PRODUCTION.md` и
`docker/edge/PRODUCTION.md`.

При ревью и исправлениях не использовались БД (любая), alembic, SSH и живые API
провайдеров, Telegram и RWMS.

## 1. Что нашло ревью и что исправлено

13 областей, 85 находок: 1 critical, 19 high, 30 medium, 35 low. Перепроверку прошли
50 находок: 25 подтверждены, 23 подтверждены частично (обычно завышены серьёзность или
сценарий), 2 опровергнуты. Одна и та же проблема часто всплывала в нескольких
областях: доверенные прокси, например, в шести.

Опровергнуты две находки. Фильтр `orderId` у Wata `GET /links` задокументирован
(payment PAY-04). Выключенный legacy-скан RWMS не регрессия: в HEAD он не мог найти
подписку (AUTH-06).

| Кластер и находки | Что было не так и чем опасно | Что сделано |
| --- | --- | --- |
| Попытки оплаты: checkout PAY-01 (critical), payment PAY-02/03, F1, OPS-5 | `prepared` и `review` переиспользовались бессрочно, `ready` — 15 минут без проверки исхода, воркер брал одну попытку за 31 с. Короткий сбой провайдера навсегда блокировал покупку тарифа, и блокировка переходила к выжившему при merge. Повтор после отказа карты вёл на отменённый платёж ЮKassa | Окна: `prepared` до 20 минут, `ready` до 15 минут и только с нетерминальным исходом; `review` и `failed` не блокируют. В отпечатке цена и промо. Воркер за цикл: 20 просроченных, 1 Wata, 20 ЮKassa |
| Ошибки провайдера: checkout PAY-02/07/08 | Однозначный отказ 4xx считался неизвестным исходом и не логировался. Ошибка commit в воркере зацикливала одну попытку. Оплата Wata без `wata_invoices` была не видна | `ProviderRejected` → `failed` и прежний 502; валидация SDK ЮKassa до коммита; сбойная попытка откладывается отдельной транзакцией; ALERT об оплате Wata без invoice |
| Автоплатёж и email чека: payment PAY-01, checkout PAY-04 | Сайт перестал писать email чека в `users.email`, но ЮKassa сохраняла карту: yk-recurrent не смог бы списать автоплатёж. Email, введённый Telegram-пользователем, не привязывался | `save_payment_method=bool(user.email)`. Аккаунту без email после счёта уходит письмо подтверждения, адрес привязывается только по ссылке |
| Прямая покупка с лендинга: AUTH-03, checkout PAY-06/09, F1, TR-01 | Анонимная оплата заменена входом: email терялся, тариф жил только в сессии браузера, после оплаты кнопка входа была с пустым href. Удар по основной воронке | Прямая покупка возвращена (решение владельца): аккаунт по email без trial, браузеру только `pstatus_`, ссылка входа только письмом, лимиты `PAYMENT_ANON_*`. Страница успеха без сессии ведёт на `/login/` |
| Старые ссылки: checkout PAY-05, AUTH-04, TR-02 | Постоянные purchase-ссылки и старые ссылки статуса молча перестали бы работать | Legacy-вход без обращения к БД показывает понятный текст и не авторизует; старая ссылка статуса 7 дней показывает только статус |
| IP клиента: AUTH-01, IP-1, SUP-01, MOB-01, OPS-1, INFRA-02 | Дефолтный `TRUSTED_PROXY_NETWORKS` не включал публичные IP edge. Все клиенты получали IP edge: общий лимит входа, неверный аудит и «Ваш IP», нода в Remnawave с адресом edge | `ORIGIN_ALLOWED_PROXY_CIDRS` всегда добавляется к доверенным сетям; claim ноды с приватным или доверенным IP → 409 без RWMS |
| Лимиты: AUTH-02/05, RL-1/2, SUP-02, OPS-8, TR-06 | Отклонённые запросы расходовали все бакеты, и один IP блокировал magic-link всем. Гонка в Redis оставляла ключ без TTL, бакет блокировался навсегда. Лимит админки считал успешные входы и общий бакет `shared`, это анонимный DoS входа сотрудников. Redis без таймаутов подвешивал вход. Django-код лежал в common | Проверка бакетов останавливается на первом превышенном; `window_id` и `touch`. Админка считает только неудачи по «аккаунт+IP», по аккаунту только ALERT. Таймаут Redis 0,5 с. Модули перенесены в `engine/` |
| Дедлайны RWMS: RWMS-1/2, payment PAY-07, TR-05 | Общий дедлайн 8 с в common менял бюджеты всех сервисов при bump (user-notify 30 → 8 с). Пустая env роняла импорт. Таймаут legacy-скана трактовался как «подписки нет» | В common по умолчанию нет дедлайна, есть `timeout=` на вызов и безопасный разбор env. Сайт задаёт через settings 8 с для обычных вызовов и 30 с для тяжёлых, мобильный API — 8 с. Legacy-скан без ответа откладывает регистрацию |
| Смена email и merge: EMAIL-01…07, TG-BIND-3, F4 | `users FOR UPDATE` держался через два RPC (до ~16 с), а порядок блокировок был обратным merge бота, отсюда deadlock. Без новых таблиц merge падал уже после погашения токена. Невалидный email ретраился вечно, почтовые сканеры сжигали ссылку. Воркер падал на idle-in-transaction и игнорировал SIGTERM | CAS-синк без блокировки `users`, `SKIP LOCKED`. Merge бота первым берёт `users FOR UPDATE`, на каждую таблицу свой savepoint. Повторный переход по ссылке идемпотентен. Лидер на AUTOCOMMIT-соединении, SIGTERM обрабатывается |
| Привязка Telegram: TG-BIND-1/2/4/6, F2, OPS-9, TR-03 | Bind-токен хэшировался как токен входа: непогашенный bind-токен после привязки становился бессрочной ссылкой входа на сайт. Кабинет показывал почти истёкшую ссылку. GET кабинета писал в БД и падал 500, в том числе с `DetachedInstanceError` | Отдельный префикс `telegram-bind:` на сайте и в боте (round-trip проверен), переиспользование ссылки 5 минут, выпуск в отдельной транзакции, маскирование токена в логах бота, понятный ответ при сбое RWMS после погашения. Окно несовместимости учтено в плане |
| Деплой и воркер: OPS-2/6/7, payment PAY-06, MOB-04, F5, TR-04 | Ничто не мешало выкатить сайт без миграции, и тогда все оплаты и страницы статуса отдают 500. Лидер держал транзакцию во время сна. Критичные файлы (`runtime_tariffs.py`, статика, `package-lock.json`) не в git | `check_common_schema` в `deploy.sh` до `up` с возвратом тега; страница статуса без таблицы не падает; воркер ждёт схему; `init: true` и `stop_grace_period`; список файлов для коммита — в плане |
| Поддержка и диагностика: SUP-03…07, OPS-3/4 | X-Accel с не-ASCII именем файла отдавал 404. Переписка дублировалась. Битый GIF давал 500. ETag не срабатывал за gzip. Сбой БД разлогинивал операторов. `Server-Timing` выдавал, зарегистрирован ли email | Percent-кодирование пути, `data-message-id`, отказ 400 на битом изображении, `parse_etags`, 503 без сброса сессии, `Server-Timing` только по флагу |
| Фронтенд: F2/3/4/6/7, AUTH-08, TR-07 | ES2020 в inline-скрипте ломал вход на старых браузерах. Вложения обрывались через 10 с. На VPS-доменах онбординг говорил про белые списки и YouTube без рекламы, это риск рекламной модерации. Атрибут `hidden` не скрывал кнопку. Пользователь видел текст SyntaxError. Скрипт template guards падал | ES2019 и гард, таймаут 180 с, 5 нейтральных слайдов и нейтральные картинки для VPS, `[hidden]`, ответ разбирается как JSON только при `application/json`, скрипт починен |
| infra и mobile: INFRA-01, MOB-02 | Под нагрузкой OFFLINE-детект шёл реже, чем в HEAD. Сбой отправки кода аннулировал прежний код и тратил квоту, повтор получал 429 | OFFLINE-проверка первой, короткая пауза после обрезанного тика. Недоставленный код удаляется, прежние коды восстанавливаются |
| payment/common: F3, payment PAY-05 | Правка сделана поверх устаревшего 98a11e8: риск потерять `client_snis` и получить две головы alembic. Фолбэк почти недостижим, но делал payment зависимым от незакоммиченного common | Откатано, см. раздел 3 |

## 2. Финальное ревью и исправления

Второй круг ревью шёл по итоговым рабочим копиям в шести областях: платежи,
авторизация и лимиты, email и привязка в боте, фронтенд, common/infra/деплой,
межсервисная безопасность. Найдено 19 проблем: 1 critical, 2 high, 4 medium, 12 low.
Переиспользование попытки ЮKassa после merge нашли две области (ZONE-BIND-01 и
XSVC-01).

Перепроверку прошли 7 находок, опровергнутых нет. Полностью подтверждены FINAL-PAY-01
и ZONE-BIND-02. У XSVC-01 один проверяющий подтвердил high, второй — medium.
AUTHZ-F1, ZONE-BIND-01, FE-FINAL-01 и XSVC-02 подтверждены частично, у трёх из них
серьёзность понижена: ZONE-BIND-01 и XSVC-01 срабатывают только при
`PAYMENT_GATEWAY=yookassa` (по `PRODUCTION.md` и `.env.example` сайт на Wata), а
условие AUTHZ-F1 (AAAA у доменов edge) в репозиториях не подтверждено.

| Находка | Что было не так и чем опасно | Что сделано |
| --- | --- | --- |
| XSVC-01, ZONE-BIND-01: попытка ЮKassa после merge | Бот при merge переносит попытки оплаты проигравшего на выжившего, а отпечаток попытки от пользователя не зависит. Выживший в течение 15 минут (`prepared` — 20) получал ссылку ЮKassa с `metadata.username` проигравшего. Оплата проходила, payment не находил пользователя (CRITICAL, после трёх повторов `failed/`), срок не продлевался. Для Wata проблемы нет: владелец ищется по `wata_invoices.user_id` | `find_reusable_attempt(..., username=)` отдаёт попытку ЮKassa, только если `request_payload.metadata.username` совпадает с текущим пользователем; чужая пропускается с WARNING, берётся следующая или создаётся новая. В metadata ЮKassa добавлено опциональное `email` = `users.email` аккаунта (не email чека): payment уже ищет по нему после username и telegram_id, поэтому оплата старой вкладки проигравшего находит выжившего. Payment и бот не менялись |
| FINAL-PAY-01: покупка с лендинга при недоступном RWMS | Новый email проходил strict-поиск в RWMS, и любой сбой панели давал 503, хотя в HEAD аккаунт создавался и оплата проходила. Во время инцидента с панелью новые клиенты с рекламы не могли заплатить | Как в HEAD: при `RwmsUnavailableError` анонимная покупка создаёт локальный аккаунт без подписки и пишет `ERROR ALERT: RWMS unavailable during anonymous purchase …`; подписку создаст или продлит задача payment по тому же username. Вход по magic link, OAuth, Telegram и любая регистрация при `SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=true` по-прежнему получают 503 |
| FINAL-PAY-02: письмо при неизвестном исходе | При таймауте или 5xx провайдера письмо со ссылкой входа не уходило, а после оплаты страница писала «ссылка отправлена» | Письмо с `plogin_` уходит сразу в ветке неизвестного исхода тем же шаблоном: ссылка авторизует только после подтверждённой оплаты. Текст успеха без сессии дополнен: «Если письма нет, войдите по email на странице входа.» |
| FINAL-PAY-03: анонимная покупка существующего аккаунта | Ветка не брала `users FOR UPDATE`: два одновременных POST на один email и тариф могли создать два платежа | Пользователь читается `with_for_update()`, как в авторизованной ветке |
| FE-FINAL-01: дедлайн запуска оплаты | Браузер обрывал `/pay/` через 15 с и закрывал вкладку оплаты, а сервер успевал создать платёж и отправить письмо | 55 с на трёх лендингах и в кабинете, ниже 60 с nginx и gunicorn |
| FE-FINAL-02: старая ссылка статуса | Legacy-ссылка без строки или старше 7 дней бесконечно опрашивала API раз в 2,5 с и показывала «Ожидаем подтверждение» | JSON содержит `terminal: true`; страница переходит в `data-status="info"`, скрывает ожидание, останавливает опрос и ведёт на вход |
| ZONE-EMAIL-01: письма подтверждения из `/pay/` | Каждый новый email чека у аккаунта без email давал письмо «Подтвердите email» на произвольный адрес | Не больше 3 писем на аккаунт в час независимо от адреса; пропуски лимит не тратят, при превышении ссылка не перевыпускается |
| AUTHZ-F1: IPv6-клиенты edge | При AAAA у доменов edge и docker userland-proxy на `[::]:443` все IPv6-клиенты приходят как шлюз bridge `172.x.0.1` и делят один IP-бакет magic-link, покупки без входа, админки и mobile exchange | IP-бакет не читается и не расходуется, если IP клиента непубличный, `unknown` или доверенный; WARNING `client IP unresolved (proxy chain fully trusted)…` не чаще раза в 10 минут на процесс. Бакеты email, общий и «аккаунт+IP» работают. Корень в инфраструктуре: предпроверка в шаге 0 плана |
| AUTHZ-F4: опечатка в CIDR | Запись, которую принимает nginx, но не Python (`203.0.113.010/32`), молча отбрасывалась: edge работал, но не был доверен, и все его клиенты делили IP edge | ERROR `invalid TRUSTED_PROXY_NETWORKS/ORIGIN_ALLOWED_PROXY_CIDRS entry … ignored` один раз на значение |
| ZONE-COMMON-F1: отчёт «Трафик нод» | `GetNodeUsersUsage` резался дедлайном 8 с, крупные ноды попадали в «Не ответили ноды» | Дедлайн `RWMS_BULK_RPC_TIMEOUT_SECONDS`, но не больше остатка бюджета отчёта (20 с) и не меньше 1 с |
| ZONE-BIND-02 (бот): снимок в merge | Merge и первичный bind писали `expire_at` и email из снимка, прочитанного до strict-чтения RWMS. Оплата или смена email, закоммиченные в этом окне, затирались | После `FOR UPDATE` строки перечитываются и сверяются со снимком (`expire_at`, `email`, `telegram_id`). При расхождении транзакция откатывается до записей, пользователь получает `BIND_DATA_CHANGED_TEXT`. Staged-файл RWMS пишется внутри транзакции последним шагом перед commit, поэтому откат его не оставляет |
| ZONE-BIND-03 (бот): savepoint merge | Проверка «таблицы нет» по тексту глотала и 42703 `column … of relation … does not exist` | Пропускается только 42P01 (из `sqlstate`/`pgcode`) или SQLite «no such table» |
| B-BIND-AWARE-TS (бот, было в HEAD с 05ce51b): первичный bind | Первичный bind (в БД только site-строка) писал в `users.expire_at` (`TIMESTAMP` без tz) aware datetime. asyncpg такое значение не принимает, bind падал с общей ошибкой; тесты на SQLite этого не видели | В БД пишется `_naive_utc(new_expire_at)`, как в ветке merge; в RWMS-задаче остаётся тот же момент времени. Тест проверяет присваиваемое ORM значение (`tzinfo is None`) и совпадение с `expire_at` задачи |

Не исправлялись, перенесены в разделы 4 и 5: AUTHZ-F2 (скрытая блокировка magic-link
по email), AUTHZ-F3 (exit-IP VPN-нод), ZONE-BIND-04 (рост `telegram_login_tokens`),
ZONE-DEPLOY-F2 (перевыпуск сертификата во время деплоя), XSVC-02 (автоплатёж
TG-пользователей без email), XSVC-03 (deadlock checkout/merge).

Совместимость. Схема БД, ORM-модели, protobuf и контракты RWMS не менялись, новых
env и миграций нет. Затронуты website и vpn-bot. Payment читает новое
`metadata.email` как опциональное поле (`Metadata.email: str | None`, офлайн-проверка
`Metadata.model_validate` с email и без), правки там не нужны. Remnawave: только
прежний read-only `GetNodeUsersUsage` с более длинным дедлайном, подписки не
удаляются и не пересоздаются.

## 3. Что откатано

- **`monkey-island-payment`.** Откатаны фолбэк `_get_user_info_by_order_id` через
  `WebsitePaymentAttempt` в `wata_webhook_handler.py`, раздел README и тест
  `tests/test_website_checkout_mapping.py`; модели и README в `payment/common`
  возвращены к 98a11e8. `git status` payment пуст, тесты payment проходят.
  Фолбэк не нужен: сайт пишет `wata_invoices` в одной транзакции с
  `confirmation_url`, поэтому ссылка на оплату не уходит без строки invoice. Для
  `review` без ссылки есть ALERT.
- **common.** Общий дедлайн RWMS 8 с по умолчанию убран: клиенты работают без
  дедлайна, сайт задаёт свой. `rate_limit.py`, `request_ip.py` и
  `tests/test_rate_limit.py` перенесены из common в `engine/`: в common не должно
  быть Django-кода.
- **Сайт.** Редирект анонимной оплаты на вход заменён прямой покупкой (R03).

## 4. Оставлено сознательно или ждёт решения владельца

| Вопрос | Что сейчас | Что решить |
| --- | --- | --- |
| IPv6 на edge (AUTHZ-F1) | Код только страхует: для IPv6-клиентов за docker-proxy лимиты по IP отключаются с WARNING, «Ваш IP» показывает `172.x.0.1`. Со старым edge-шаблоном такой клиент подставляет свой `X-Forwarded-For` и обходит IP-лимиты. Бакеты email и общий действуют; «аккаунт+IP» у входа в админку для таких клиентов общий | По итогам шага 0 плана убрать AAAA или перевести edge на host-сеть. Подтвердить, что «непубличный» — это RFC 1918, `fc00::/7`, loopback, link-local, unspecified и доверенные сети, а CGNAT `100.64.0.0/10` считается обычным адресом клиента |
| Покупка с лендинга при недоступном RWMS (FINAL-PAY-01) | По умолчанию fail-open, как в HEAD. При `SITE_LEGACY_RWMS_IDENTITY_SCAN_ENABLED=true` остаётся 503: скан при упавшей панели не выполнить, а без него вернулся бы дубль подписки (TR-05). Остаточный риск, как в HEAD: если подписка с этим username уже есть в панели (окно краша), пропускаются проверка email-владельца и backfill маркера лимита trial, и лимит трафика после оплаты может не сняться. Захвата чужого аккаунта нет: занятый username даёт `IntegrityError` и 502 | Подтвердить отступление для включённого legacy-скана; повесить алерт на `ALERT: RWMS unavailable during anonymous purchase` |
| Бакет email у magic-link (AUTHZ-F2) | После 5 запросов на один email за 15 минут с любых IP ответ 200 без письма. Кто знает адрес, может скрыто запереть вход по почте; легитимные повторы подталкивает клиентский таймаут `login.html` 15 с | Ключевать блокирующий бакет парой (email, IP), сделать бакет email мягким с ALERT, показывать «письмо уже отправлено», поднять таймаут входа до 25 с и выше |
| Синхронные письма в `/pay/` (ZONE-EMAIL-01, FE-FINAL-01) | Письмо со ссылкой входа и письмо подтверждения отправляются по SMTP до ответа. Ретраи SDK ЮKassa сами по себе в худшем случае дают около 51 с, вместе со strict-поиском RWMS (8 с) и SMTP ответ может не уложиться в клиентские 55 с. Автоповтора POST после обрыва нет | Отправлять письма после ответа (очередь или поток) |
| Merge бота: что не сверяется под локом (ZONE-BIND-02) | Под `FOR UPDATE` сверяются только `expire_at`, `email`, `telegram_id`. Реферальная история, баны и владелец email у третьего аккаунта считаются по снимку. При откате bind-токен уже погашен, пользователь обновляет кабинет. Вебхук ЮKassa проигравшего, пришедший после лока merge, падает по FK и повторяется, дальше payment ищет по username, telegram_id и `metadata.email` | Нужна ли повторная проверка условий отказа под локом |
| `is_missing_table_error` на сайте | `engine/rwms_helpers.py` использует хелпер common с текстовым фолбэком «relation … does not exist» (контекст `managed_traffic_limits`): 42703 на UPDATE теоретически тоже будет проглочен. В боте хелпер заменён локальным | Править common или сайт отдельной задачей |
| Очередь RWMS-задач бота при пересоздании контейнера | В `docker/vpn` и `docker/vps` бота в volume вынесены только `log` и `locales`, а задачи RWMS лежат в `/app/rwms-tasks` внутри контейнера. `--force-recreate` удаляет необработанные `pending/`, `retry/`, `processing/`, `failed/` и staged-файлы. Проблема старая, но в этом релизе боты пересоздаются | Сверить compose на сервере; вынести `rwms-tasks` в volume отдельной правкой бота. До этого — проверка очереди перед рестартом (шаг 4) |
| Жёсткий 429 общего бакета | magic-link: 9 и больше IP по 60 запросов исчерпывают 500 запросов за 60 с, и вход по ссылке встаёт для всех. То же у `PAYMENT_ANON_GLOBAL_RATE_LIMIT` (600 за 60 с) | Поднять пороги, превратить бакет в алерт или добавить `limit_req` на edge |
| Нейтральный онбординг VPS | 5 новых слайдов и копии картинок. На `locations` и `stable-access` есть мотив замка. Обещания «до 15 устройств», «до 10 Гбит/с», «24/7» взяты из `index_vps.html` | Показать владельцу до выкладки (рекламная модерация) |
| Интеграционные проверки | Не проверялись: реальный PostgreSQL (`FOR UPDATE`, в том числе в анонимной ветке `/pay/`, `SKIP LOCKED`, advisory lock, JSONB, сама миграция), живые ЮKassa и Wata (включая восстановление через `GET /links`), прод-образ на Python 3.12. Были только SQLite, моки и косвенная проверка синтаксиса | Контролируемые сценарии после деплоя (шаг 6 плана) |
| Автоплатёж у аккаунта из покупки с лендинга | `users.email` задан, но не подтверждён, поэтому ЮKassa сохраняет карту, как в HEAD, даже если введён чужой email | Подтвердить бизнес-правило |
| Ответы `/pay/` | Ошибки 400/403 при fetch-запуске теперь JSON с `message`, в том числе у авторизованных. Legacy-ссылка статуса при `succeeded` показывает «войдите по email» | Подтвердить |
| Лимиты без Redis | При сбое кэша лимиты пропускают запросы; без `CACHE_REDIS_URL` каждый воркер gunicorn считает сам | Задать `CACHE_REDIS_URL` в проде |
| Повтор ссылки подтверждения email (EMAIL-07) | Повторный переход в течение 15 минут авторизует открывшего, как в HEAD | Нужен ли строгий вариант с POST-кнопкой |
| Ссылка регистрации magic-link (AUTH-07, не исправлялась) | Многоразовая 15 минут, после создания аккаунта работает как вход; email в подписанном токене читается | Одноразовый nonce, хэш email вместо адреса |
| Бюджет infra_worker на медленной БД | Если все шаги идут по 20–40 с, OFFLINE-проверка выполняется раз в ~60 с, а замены IP реже, чем в HEAD (~331 против 232 с) | При приоритете автозамены поднять `INFRA_MAINTENANCE_BUDGET_SECONDS` до 60–120 |
| Прочее low, не исправлялось | INFRA-03: singleflight `who_connects` без таймаута и без передачи ошибки ожидающим. INFRA-04: общие пулы отчёта трафика, повтор показывает ложное «Не ответили ноды». INFRA-05: повторный one-liner после правки ноды в панели получает 409. MOB-03: мобильное приложение показывает 403/503 verify как сетевую ошибку. Email в открытом виде в логах `send_magic_link` и mobile API. `infra_worker._leader_loop` закрывает соединение без `invalidate` (как в HEAD). `BIND_RWMS_UNAVAILABLE_TEXT` не вынесен в локали бота | Отдельные задачи |

## 5. Известные ограничения (не блокируют релиз)

Приняты на этот релиз: данные и подписки не портятся, последствия ограничены по
времени или видны в логах.

| Ограничение | Что происходит | Чем смягчено и что можно сделать |
| --- | --- | --- |
| Exit-IP собственных VPN-нод как общий IP для лимитов (AUTHZ-F3) | Клиенты, у которых домены сайта идут через туннель, приходят с IP ноды. Владелец trial-подписки может 60 запросами magic-link за 15 минут или 30 неудачными входами в `/support-admin/` запереть вход всем, кто открывает сайт через эту ноду; 429 до конца окна | Легитимной нагрузки на один exit-IP мало, обход — выключить VPN. Варианты: для адресов нод ключевать бакет парой (IP, email или логин), allowlist IP сотрудников для админки, исключить домены сайта из туннеля в клиентских правилах |
| Рост `telegram_login_tokens` (ZONE-BIND-04) | Кабинет без привязанного Telegram переиспользует bind-ссылку из сессии только 5 минут, дальше каждый рендер вставляет новую строку. Непогашенные bind-строки никто не удаляет, merge обновляет все строки проигравшего | На корректность не влияет. Выпускать ссылку по нажатию кнопки или чистить непогашенные bind-строки старше суток в `reconciliation` |
| Гонка перевыпуска сертификата во время деплоя (ZONE-DEPLOY-F2) | `deploy.sh` вызывает `issue-certs.sh` до `docker load` и проверки схемы. Если сертификат origin отсутствует или истёк, скрипт делает `docker compose up -d` по новому compose со старым тегом `:prod`: `reconciliation` из старого образа падает с `Unknown command` и уходит в рестарт-цикл, а при непрошедшей проверке схемы так и остаётся | Путь редкий, данные не страдают. Перед деплоем проверить срок сертификата (шаг 0). Если сработало — `docker compose -f docker-compose.yml rm -sf reconciliation` и повторный деплой после миграции. Исправление: вызывать `issue-certs.sh` после проверки схемы |
| TG-пользователи без email после оплаты на сайте через ЮKassa (XSVC-02) | Платёж разовый (`save_payment_method=False`), пока email не подтверждён по ссылке. Подтверждение карту задним числом не сохраняет, автоплатёж появится со следующей оплаты. Бот при этом пишет email чека в `users.email` без подтверждения, каналы ведут себя по-разному. §4 оферты это исключение не описывает | Сейчас скрыто: сайт на Wata, автоплатежа там нет и в HEAD. Напоминания user-notify за 1 и 3 дня приходят. Сохранять карту без `users.email` нельзя: user-notify не шлёт напоминаний при рекурренте, а yk-recurrent без email молча помечает списание FAILED |
| Редкий deadlock с merge бота (XSVC-03) | Финальная транзакция `/pay/` и воркер Wata берут строку попытки, затем вставляют `wata_invoices`, `event_logs`, `magic_tokens` (FK-блокировка `users`). Merge берёт `users FOR UPDATE`, затем попытки. payment (refund, отмена автоплатежа) и yk-recurrent пишут «дочерние → users». При совпадении по одному пользователю PostgreSQL прервёт одну транзакцию | Данные не портятся: merge ответит ошибкой без изменений (bind-токен погашен, нужна новая ссылка из кабинета), попытка останется `prepared` и восстановится воркером, вебхук повторится. Восстановленную попытку ЮKassa проигравшего выживший не получит (XSVC-01). Комментарий о порядке блокировок в `lock_users_for_merge` бота неточен |
| Перепривязка site-пользователя к другому Telegram и повторный trial (TG-BIND-5) | В ветке первичного bind бот не проверяет, что у site-пользователя уже есть другой `telegram_id`. Второй свежий bind-токен (15 минут, по токену на браузерную сессию) перепривяжет аккаунт и снова начислит trial; в merge уже привязанный аккаунт может стать проигравшим. В HEAD окно было бессрочным | Отказывать, если `telegram_id` задан и отличается; при погашении отзывать остальные bind-токены пользователя |
| ES2020 в `engine/static/js/cabinet-sheets.js` и админке | На iOS < 13.4 шторки кабинета и админка не работают | Вход, лендинги, оплата, статус и тикет поддержки — на ES2019 с гардом. Переписать или принять |
| `review`-попытки без админ-инструмента | Попытка без ссылки через 15 минут уходит в `review`. Оплату она не блокирует, но в админке не видна | Разбор по логам `checkout needs reconciliation` и ALERT. Нужен ли список в админке |

## 6. Проверки

Первый круг, до финального ревью:

- Сайт, `manage.py test engine mobile_api` через ReviewRunner без БД: 1166 OK.
  12 классов `CabinetDevices*` требуют Django test DB и прогнаны отдельно на
  `sqlite://:memory:`: 12 OK.
- `node --test engine/tests_js/`: 37/37. `tests/run_cabinet_template_guards.py`: 8 OK.
  `unittest discover -s common/tests`: 126 OK, 3 skipped.
- Бот: 864 passed, в том числе на дереве с будущим common сайта. Payment после
  отката: 221 passed.
- Контракт bind-токена сайт ↔ бот, round-trip на настоящем коде: 39/39.
  `manage.py check` при `DEBUG` True и False, collectstatic, `bash -n deploy.sh`,
  acorn ES2019 для изменённых шаблонов — OK.
- Все прогоны шли под сетевым guard, реальных подключений к БД, Redis, RWMS и
  провайдерам не было.

Финальный раунд, исправления из раздела 2:

- Сайт, изменённые модули без БД: `engine.tests_recheck` и
  `engine.tests_frontend_review` — 166 OK; `engine.tests_rate_limit`, `mobile_api`,
  `engine.tests_infra`, `engine.tests_node_provisioning` — 372 OK. Режим onlydb на
  `sqlite://:memory:` — 12 OK.
- Полный `manage.py test engine mobile_api` без БД: Ran 1205, OK. Фейки
  `get_node_users_usage` в `engine/tests.py` (`NodeTrafficReportTests`,
  `NodeTrafficDayGranularityTests`) приведены к сигнатуре
  `(self, request, *, timeout=None)`, которую теперь использует
  `engine/node_traffic.py`. Тесты, читающие README и `.env.example`, — OK.
- `node --test engine/tests_js/`: 44/44, в том числе дедлайн запуска оплаты на
  четырёх страницах и terminal-состояние статуса. template guards: 8 OK. acorn
  ES2019 для `payment_status`, трёх лендингов и `dashboard.html`: 0 ошибок.
- Бот: 906 passed (было 864: 41 тест на сверку снимка, откат без staged-файла и
  42703, плюс тест naive UTC `expire_at` в первичном bind'е). На `menu.py` до этого
  раунда 35 новых тестов падают.
- Payment не менялся. `Metadata.model_validate` офлайн принимает metadata сайта с
  `email` и без.
- Во всех прогонах в журнале guard только `BLOCKED psycopg2.connect`, реальных
  подключений не было.

## 7. Итоговый план деплоя

### 0. Предусловия

- Владелец просмотрел разделы 4 и 5 (как минимум IPv6 на edge, покупку с лендинга
  при недоступном RWMS, нейтральный онбординг VPS и автоплатёж у аккаунта из покупки
  с лендинга) и этот план.
- Тесты зелёные (раздел 6).
- Поддержка предупреждена: старые ссылки входа из писем об оплате больше не
  действуют (на странице входа понятный текст), а между выкатом ботов и сайта не
  работает привязка Telegram из кабинета.
- IPv6 на edge (AUTHZ-F1). Локально для каждого домена из `EDGE_SERVER_NAMES` всех
  edge и на каждом edge:

  ```bash
  dig +short AAAA <домен>
  ssh <edge> "ss -ltnp 'sport = :443'"
  ```

  Если у домена есть AAAA, а на `[::]:443` слушает `docker-proxy`, IPv6-клиенты
  приходят на сайт как `172.x.0.1`. Лимиты по IP для них отключатся с WARNING
  `client IP unresolved (proxy chain fully trusted)` в логах `app`, «Ваш IP» покажет
  `172.x.0.1`, а со старым edge-шаблоном клиент ещё и подставит свой
  `X-Forwarded-For`. Лучше до деплоя убрать AAAA или перевести edge на host-сеть
  (`docker/edge/PRODUCTION.md`, «IPv6 и docker-proxy»). Если AAAA остаются, edge
  выкатывается не позже сайта (шаг 7).
- Сертификат origin действителен (ZONE-DEPLOY-F2). На origin в
  `/root/website/website`:
  `openssl x509 -checkend 86400 -noout -in letsencrypt/live/<ORIGIN_CERT_NAME>/fullchain.pem`
  должен вывести `Certificate will not expire`. Иначе `deploy.sh` перевыпустит
  сертификат и поднимет стек по новому compose ещё до проверки схемы (раздел 5).

### 1. Коммит common

Submodule `monkey-island-website/common` сейчас в detached HEAD на `f61637b`; это тот
же коммит, что `main` и `origin/main`.

```bash
cd /Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website/common
git switch main
git status --short
#  M README.md
#  M models/db.py
#  M rwms_client.py
#  M rwms_client_sync.py
#  M tests/test_common_rwms_client_lookup.py
# ?? runtime_tariffs.py
# ?? tests/test_common_rwms_client_timeout.py
git add README.md models/db.py rwms_client.py rwms_client_sync.py runtime_tariffs.py \
    tests/test_common_rwms_client_lookup.py tests/test_common_rwms_client_timeout.py
git commit
```

- `runtime_tariffs.py` пока не отслеживается git, но его импортируют
  `engine/views.py` и `mobile_api/tariffs.py`. Без `git add` образ сайта упадёт с
  ImportError.
- В common не должно быть `rate_limit.py`, `request_ip.py` и
  `tests/test_rate_limit.py`: они перенесены в `engine/`.
- Рабочие копии `monkey-island-payment/common` (уже чистая, 98a11e8) и
  `monkey-island-vpn-bot/common` не коммитить. В bot/common лежат те же
  `models/db.py` (совпадает побайтно) и свой вариант README.
- Финальный раунд common не менял.

### 2. Миграция (делает владелец)

- В `common` своим скриптом `./alembic-revision.sh "<сообщение>"`: autogenerate
  против локальной дев-БД на голове `2b3e69407d34`, схему которой руками не трогали.
- До коммита проверить ревизию:
  - `down_revision = "2b3e69407d34"`, у alembic одна голова;
  - в `upgrade()` только `op.create_table("website_email_changes", …)` (PK и FK
    `user_id` → `users.id`, индекс по `next_attempt_at`) и
    `op.create_table("website_payment_attempts", …)` (PK `id`, FK `user_id` →
    `users.id`, `UniqueConstraint("status_token_hash")`, `request_payload` типа
    `postgresql.JSONB`, индексы по `user_id`, `provider_reference`,
    `next_attempt_at`);
  - нет `drop_*`, `alter_column` и других операций над существующими таблицами;
  - ревизия не пустая (инцидент 2026-09-01).
- Закоммитить ревизию в common (вместе с шагом 1 или отдельным коммитом) и
  выполнить `git push origin main`.
- Поднять указатель common:
  - в сайте — `git add common`;
  - в боте — в `monkey-island-vpn-bot/common` отбросить локальные правки
    `models/db.py` и `README.md`, переключить submodule на новый коммит, затем
    `git add common`;
  - в payment указатель не трогать.

### 3. Коммиты website и vpn-bot

website: сверить `git status`, затем `git add -A`. Новые файлы по `git status` на
11 сентября:

- `package-lock.json`: Dockerfile делает `COPY package.json package-lock.json` и
  `npm ci`;
- `engine/checkout_attempts.py`, `engine/email_change.py`, `engine/rate_limit.py`,
  `engine/request_ip.py`, `engine/report_cache.py`,
  `engine/request_timing_middleware.py`, `engine/sql_timing.py`;
- `engine/management/commands/check_common_schema.py`,
  `engine/management/commands/reconcile_website_operations.py`;
- `engine/static/mi-network.js`, `engine/static/pwa/offline.html`,
  `engine/static/scripts/` (3 файла), `engine/static/styles/` (4 файла),
  `engine/static/img/bg6.webp`, `bg10.webp`, `header-monkey-island.webp`,
  `engine/static/img/onboarding/*.webp` (15 файлов);
- тесты: `engine/tests_recheck.py`, `engine/tests_rate_limit.py`,
  `engine/tests_frontend_review.py`, `engine/test_template_source.py`,
  `engine/tests_js/` (3 файла);
- `docs/audits/2026-09-10/*.md`.

Отдельно изменён `.impeccable/config.json` (правила дизайн-линтера): закоммитить
или вернуть на усмотрение владельца.

vpn-bot: изменены `handlers/menu.py`, `handlers/misc.py`,
`tests/test_account_merge.py`, `tests/test_account_merge_fk.py`, `README.md` и
указатель `common`. Новых файлов нет.

Образы собирать из чистого дерева после коммитов: Dockerfile сайта и бота копируют
рабочую копию (`common`, `engine`, `package-lock.json`), а не закоммиченное
состояние.

### 4. Сборка и оба бота

Локально. Образ сайта собирается заранее, чтобы шаг 5 шёл сразу за ботами:

```bash
cd /Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-vpn-bot/docker/vpn
bash build-image-amd64.sh && bash deploy.sh
cd ../vps
bash build-image-amd64.sh && bash deploy.sh
cd /Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website
bash docker/website/build-image-amd64.sh
```

`deploy.sh` бота только загружает tar на `mi.fornex.app`. На сервере сначала VPN-бот:

```bash
ssh mi.fornex.app
cd /srv/monkey-island/vpn-bot
docker compose exec monkey-island-vpn-bot sh -c 'ls -laR rwms-tasks'
docker load -i monkey-island-vpn-bot-amd64.tar
docker compose up -d --no-build --force-recreate monkey-island-vpn-bot
docker compose logs --tail=200 monkey-island-vpn-bot
```

- Очередь RWMS-задач. В compose бота из репозитория в volume вынесены только `log`
  и `locales`, а задачи лежат в `/app/rwms-tasks` внутри контейнера, и
  `--force-recreate` их удалит. Пересоздавайте контейнер, когда `pending/`, `retry/`
  и `processing/` пусты (пауза ретрая до 10 минут). Файлы `pending/*.json.staged`
  разберите до рестарта по README бота («Staged-outbox RWMS-задачи привязки
  аккаунтов»): код бота до этого релиза писал staged до транзакции, и после отката
  файл мог остаться. `failed/` сохраните для ручного разбора:
  `docker compose cp monkey-island-vpn-bot:/app/rwms-tasks ./rwms-tasks-<дата>`.
  Если на сервере `rwms-tasks` смонтирован с хоста, эта проверка не нужна.
- Бот при старте выполняет `alembic upgrade head` (`main.py:85`), здесь миграция и
  накатывается. В логе должна быть строка `Running upgrade 2b3e69407d34 -> <новая
  ревизия>`, затем обычный старт.
- Если миграция упала, бот не стартует: исключение выходит из `main()`, контейнер
  перезапускается. `env.py` common выполняет миграции в транзакции, и PostgreSQL
  откатит DDL целиком, так что схема останется на `2b3e69407d34`. Верните прежний
  образ бота (`monkey-island-vpn-bot-amd64.tar.bak`) и разберите ревизию. Сайт на
  этом этапе ещё старый.
- Затем тем же способом VPS-бот в `/srv/monkey-island/vps-bot` (сервис
  `monkey-island-vps-bot`, с той же проверкой очереди). Если он смотрит в ту же БД,
  `upgrade` для него ничего не делает. Оба бота одновременно не перезапускать.

### 5. Сайт — сразу после ботов

```bash
cd /Users/apugachev/Work/projects/monkeyislandvpn/monkey-island-website
bash docker/website/deploy.sh --skip-build
```

- Скрипт загружает runtime-файлы и tar, обновляет allowlist и сертификат, делает
  `docker load` и ставит тег `:prod`. Затем новым образом запускается
  `docker compose run --rm --no-deps -T --entrypoint python app manage.py check_common_schema`.
  Ожидаемый вывод: `OK: схема common на месте (website_payment_attempts,
  website_email_changes)`. После этого `up -d --no-build --force-recreate` поднимает
  `app`, `reconciliation` и `nginx`.
- Если проверка не прошла, деплой останавливается до пересоздания контейнеров, а
  тег `:prod` возвращается на прежний образ. Исправьте миграцию и повторите с
  `--skip-build`.
- Окно между шагами 4 и 5 ломает только привязку Telegram из кабинета: старый сайт
  выдаёт MD5-ссылки, новый бот их не принимает. Ссылки входа из бота
  (`telegram-login:`) у старого и нового кода одинаковые, а merge нового бота
  работает на уже созданных таблицах.

### 6. Проверки после деплоя

- На origin (`/root/website/website`): `docker compose -f docker-compose.yml ps` —
  `app`, `reconciliation` и `nginx` в состоянии Up. В
  `docker compose -f docker-compose.yml logs --tail=100 reconciliation` есть
  `leadership acquired` и нет `paused: схема common не готова`.
- VPS-лендинг: «Ваш IP» показывает ваш адрес, а не IP edge. Если у доменов edge
  есть AAAA, проверить и с IPv6-клиента (мобильный интернет): там тоже должен быть
  ваш адрес, а не `172.x.0.1`. Проверить до первого bootstrap ноды через домен.
- Покупка с лендинга без входа на контролируемый email: форма оплаты открывается
  сразу, страница статуса остаётся в текущей вкладке, письмо со ссылкой входа
  приходит, после оплаты подписка продлена.
- Продление из кабинета; для аккаунта без email приходит письмо «Подтвердите email».
- Вход по magic link: письмо приходит, 429 нет.
- Привязка Telegram из кабинета в обоих ботах; `/start` и merge на контролируемых
  тестовых аккаунтах. Ответ «Данные подписки изменились во время привязки…» значит,
  что в окне привязки прошла оплата или смена email: обновить кабинет и повторить.
- Вложение поддержки открывается.
- В логах `app` нет `rate limit cache failed`, `website_payment_attempts is
  unavailable`, `Pay error`, `client IP unresolved (proxy chain fully trusted)` и
  `invalid TRUSTED_PROXY_NETWORKS/ORIGIN_ALLOWED_PROXY_CIDRS entry`.
- Алерты настроены на `ALERT: RWMS unavailable during anonymous purchase` и
  `checkout needs reconciliation`.

### 7. Что не деплоить

- `monkey-island-payment`: правка ИИ откатана, образ и указатель common не меняются.
  Payment, user-notify, rwms и остальные сервисы на common `alembic upgrade` при
  старте не выполняют, так что их рестарты новая ревизия не затрагивает. Новое
  `metadata.email` в платежах ЮKassa с сайта payment уже разбирает.
- Edge nginx — по желанию позже, если у доменов edge нет AAAA (шаг 0). В нём две
  правки: `X-Forwarded-For $remote_addr` и `client_max_body_size 32m` вместо 10m.
  Сайт работает и со старым edge: IP IPv4-клиентов разбирается верно. Но вложения
  суммарно больше 10 MiB получат 413 на edge, а при AAAA и `docker-proxy` на
  `[::]:443` IPv6-клиент со старым шаблоном подставляет свой `X-Forwarded-For`,
  поэтому тогда edge выкатывается не позже сайта. Выкатка edge:
  - локально `SSH_HOST=<edge> bash docker/edge/deploy.sh`;
  - на edge `cd /root/edge && docker compose up -d --no-build --force-recreate nginx && docker compose exec nginx nginx -t`.
  Edge `deploy.sh` делает только `up -d`, а шаблон nginx рендерится при старте
  контейнера.

### 8. Откат

- Откатывается только сайт. На origin сначала
  `docker compose -f docker-compose.yml rm -sf reconciliation` (в старом образе этой
  команды нет), затем `deploy.sh` из предыдущего коммита сайта. До повторного
  выката не работает привязка Telegram из кабинета.
- Ботов на образ до миграции не откатывать: `alembic upgrade head` при старте упадёт
  с `Can't locate revision identified by …`, потому что ревизии БД нет в старом
  образе, и бот не поднимется. Старый merge к тому же не знает новых FK. Вернуть
  логику бота можно только новой сборкой старого кода с новым common, но тогда merge
  site-пользователей со строками `website_*` будет падать.
- Новые таблицы и данные не удалять, downgrade не делать.

### 9. Отдельно от релиза

Коммиты 11 сентября уже в `origin/main` своих репозиториев. Указатель common они не
меняют и новые таблицы не читают, поэтому от этого релиза не зависят:

- payment `277e05f`: битый email не роняет оплаченную операцию с панелью, алерт о
  зависшей задаче повторяется;
- payment `68e64ad`: задача RWMS со сроком в прошлом выполняется без обращения к
  панели;
- rwms `f162ba2`: невалидный запрос к панели получает `INVALID_ARGUMENT` вместо
  `INTERNAL`;
- user-notify `5ed187d`: битый email не роняет пересоздание подписки.

Если их ещё нет в проде, они выкатываются своим обычным порядком.
