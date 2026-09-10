---
version: 1
slug: "docs-cabinet-desktop-cabinet-concept-html"
primary_target: "docs/cabinet-desktop/cabinet-concept.html"
related_targets: ["engine/templates/dashboard.html"]
---

# Surface: личный кабинет для ПК (концепт)

Scope: статичный концепт docs/cabinet-desktop/cabinet-concept.html; боевой dashboard.html не трогаем до утверждения. Режим: Operate. Аудитория: клиент VPN с активной/истекающей/неоплаченной подпиской, задача — срок, продление, установка, устройства. Данные синтетические, помечены в README, не в самой странице.

## Direction contract

THESIS: Кабинет — продолжение лендинга: та же страница-история, «Ваш доступ» вместо hero; отвергает вкладочный app-shell с сайдбаром.

OWN-WORLD: #090a0f, золото #ffc700 (CTA и статус), мята #38d996 (живое/онлайн), Inter 800 заголовки, JetBrains Mono для дат/ключей/счётчиков, бордеры rgba(233,237,243,.09), карточки 18px, sec-num плашки, чип «ОСТРОВ НА СВЯЗИ», cursor-glow, grid-bg.

STORY: Вижу срок и статус мгновенно → продлеваю в одно золотое действие → ниже устройства, установка, рефералы, поддержка как номерные секции.

FIRST VIEWPORT: Sticky-нав лендинга; слева 2/3 — карточка доступа: чип статуса, дни крупно (mono), прогресс-бар, золотая «Продлить»; справа 1/3 — колонка статов: устройства 3/15, трафик ∞, автоплатёж. Якорная подвкладка-навигация секций.

FORM: «Продолжение лендинга», №3 моего списка; seed ad3bb4c4, code-led.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance.
