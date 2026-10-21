# HTML eBay: контракт парсера та межі перевірки

Дата дослідження: 21 жовтня 2026 року. Початковий HTTP-клієнт отримував 403. Порівняльна перевірка показала, що браузерний User-Agent у httpx іноді дає 200, але працює нестабільно. Безкоштовний `curl_cffi` із профілем Chrome без проксі завантажив [живу видачу Sony Walkman](https://www.ebay.com/sch/i.html?_nkw=sony+walkman&_sop=10&_ipg=240&_pgn=1&_dmd=1&_curr=USD): парсер розпізнав **240 товарів у розмітці `s-card`**. Також завантажено та розібрано одну сторінку товару з 5 доступними характеристиками. На ній точна дата публікації не була доступна. Отже, `s-card` і detail перевірені на реальному HTML; `s-item` залишається сумісністю, перевіреною на синтетичному прикладі. Успіх цих запитів не гарантує доступності будь-якого іншого запиту чи постійного доступу.

Наступна перевірка через Playwright: headless Chromium отримував 403; звичайний Chromium на Xvfb зі збереженим профілем прочитав 10 оголошень збереженого користувацького пошуку та 6 характеристик товару. Постійний worker з автоматичним Xvfb також завершив повний прохід: оновлено подробиці всіх 10 товарів, помилка очищена. Повторні діагностичні запуски часом теж отримували 403. Браузерна перевірка підтверджує працездатність, але не гарантує безперервного доступу. Точна дата публікації у перевірених сторінках усе ще відсутня, тому правила новизни залишаються консервативними.

| Дані | `s-card` | `s-item` / detail |
|---|---|---|
| Контейнер | `li.s-card` | `li.s-item` |
| Основна видача | `#srp-river-results`, `.srp-river-results` | аналогічно |
| Назва | `.s-card__title .su-styled-text.primary`, fallback `.s-card__title` | `.s-item__title` |
| Посилання | `a.s-card__link[href*='/itm/']` | `a.s-item__link[href*='/itm/']` |
| Ціна | `.s-card__price` без `s`, `del`, `.strikethrough`, `.STRIKETHROUGH` | `.s-item__price` |
| Стан / підзаголовок | `.s-card__subtitle` | `.SECONDARY_INFO`, `.s-item__subtitle` |
| Фото | `.s-card__image img`, `img.s-card__image`; `data-src` / `data-defer-load` перед `src` | `.s-item__image-wrapper img` |
| Інші рядки | `.s-card__attribute-row`, `.s-card__footer--row` | `.s-item__shipping`, `.s-item__location`, `.s-item__seller-info-text` |
| Наступна сторінка | `a.pagination__next[href]` | `a[aria-label='Go to next search page'][href]` |
| Характеристики | detail `.ux-labels-values__labels` + `.ux-labels-values__values` | `dl dt/dd`, старі `td.attrLabels` |
| Structured data | detail `script[type='application/ld+json']` | Product, additionalProperty, offers availability |

ID отримується з канонічного `/itm/<id>` або `/itm/<slug>/<id>`, а не з внутрішнього tracking `data-listingid`. Tracking-параметри видаляються, дозволено тільки HTTPS www.ebay.com. Плейсхолдер «Shop on eBay», дублікати та розпізнані секції рекомендацій відсікаються. Незнайома сторінка без явного маркера нульових результатів — помилка, а не успішний порожній список. Розпізнавання секцій рекомендацій залежить від їхньої розмітки й також потребує живої перевірки.

Запити формуються через URL encoding: `_nkw`, `_sop=10` (нові першими), `_ipg=240`, `_pgn`, `_curr`, `_udlo`, `_udhi`, `LH_ItemCondition`, `LH_BIN` / `LH_Auction`, `LH_FS`, `_sacat`. eBay може нормалізувати ці параметри, валюту або локацію; локальні перевірки фільтрів обов’язкові. Локація, seller, виключення й Item specifics перевіряються локально. Видача може містити не всі оголошення або використовувати персоналізований регіон. Рекламний маркер зберігається у вихідному тексті; алгоритм не використовує його як доказ новизни.

Дату новизни беремо лише з явного `datePublished` Product JSON-LD, `meta[itemprop=datePublished]`, `meta[property=product:published_time]`, `time[itemprop=datePublished]` або JSON-вузла з **тим самим** `itemId` і `startTime`/`listingDate`. Не використовуємо `dateModified`, дату завершення аукціону, цінове `validFrom`, порядок у видачі чи локальний first-seen. ISO-дата без часу/часового поясу не підходить. Немає підтвердженого універсального джерела `datePublished` для всіх оголошень eBay — тому невідомі дати не спричиняють сповіщення.

За замовчуванням Playwright виконує JavaScript eBay і передає BeautifulSoup поточний DOM після завантаження основного вмісту. Приватні API безпосередньо не викликаються; додаткові iframe опису окремо не обходяться парсером. Альтернативи `curl_cffi`/`httpx` виконують лише HTTP-запит без JavaScript. Поле `raw` містить витягнуті доступні дані, а не повну копію усіх даних сервера eBay. Браузер і HTTP-транспорти мають один парсер та однакові правила новизни.

Після зміни HTML збережіть публічний зразок без cookies/токенів, перевірте `scripts/inspect_html.py`, оновіть селектори й додайте regression-fixture. Під час 403/429/503 та сторінок challenge діє спільна пауза HTTP-клієнта на 15 хвилин плюс backoff пошуку; довший Retry-After враховується. Наявні результати та неініціалізований стан зберігаються. Для обмеженої живої діагностики: `python -m app.diagnostics --detail`. curl_cffi підтримує браузерні TLS/HTTP-профілі ([документація](https://curl-cffi.readthedocs.io/en/latest/)); це HTTP-клієнт, який не запускає JavaScript і не розв’язує CAPTCHA.
