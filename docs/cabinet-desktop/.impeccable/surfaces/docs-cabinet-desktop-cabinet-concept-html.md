---
version: 1
slug: "docs-cabinet-desktop-cabinet-concept-html"
primary_target: "docs/cabinet-desktop/cabinet-concept.html"
related_targets: ["engine/templates/dashboard.html"]
---

# Surface: личный кабинет для ПК (концепт)

Scope: статичный концепт docs/cabinet-desktop/cabinet-concept.html; боевой dashboard.html не трогаем до утверждения. Режим: Operate. Аудитория: клиент VPN (активная/истекающая/неоплаченная подписка; пришёл с сайта или из бота). Данные синтетические (README). Поддержка — только Telegram+FAQ, тикеты на сайте сознательно не создаются. Уточнения пользователя 2026-09-10: только ПК; контент по вкладкам, не одной страницей; баннеры привязки TG (+7 дней) и email; при истечении — явное «Продлить за 299 ₽» (полоса как ip-topbar); Профиль повторяет мобильный набор (почта со сменой, история платежей с тихой «Отключить автопродление», FAQ, документы).

## Direction contract

THESIS: Кабинет — продолжение лендинга: тот же мир и горизонтальная sticky-навигация, «Ваш доступ» вместо hero; пункты навигации переключают вкладки-панели (не одна длинная страница, не сайдбар).

OWN-WORLD: #090a0f, золото #ffc700 (CTA/статус), мята #38d996 (живое), коралл #ff8a63 (истечение), Inter 800, JetBrains Mono для данных, бордеры rgba(233,237,243,.09), карточки 18px, sec-num-плашки, чип «ОСТРОВ НА СВЯЗИ», cursor-glow, grid-bg.

STORY: Вижу срок → продлеваю в одно золотое действие; привязываю Telegram/email по баннеру; вкладки — устройства, установка, рефералы, поддержка, профиль.

FIRST VIEWPORT (Главная): sticky-нав с вкладками; слева 2/3 карточка доступа (чип, дни mono, прогресс, золотая «Продлить»); справа 1/3 статы; ниже баннер привязки Telegram; финал-призыв. Подпись-интеракция: переключение вкладок tab-in .35s + hash.

FORM: «Продолжение лендинга», №3 списка; seed ad3bb4c4, code-led; вкладочная структура закреплена пользователем поверх выбранной карты.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance. (Ревью и DESIGN.md выполнены; вкладочная ревизия задокументирована.)
