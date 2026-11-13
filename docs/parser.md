# HTML eBay: контракт парсера та межі перевірки

Поточний режим: **один HTML першої сторінки пошуку на перевірку**, усі картки з нього, без переходів на наступні сторінки чи сторінки товарів. Публічна видача `s-card` перевірялася на реальному eBay; `s-item` підтримується також через fixtures. Кількість карток визначає eBay; тест перевіряє обробку 56 карток з одного виклику транспорту.

| Дані | `s-card` | `s-item` |
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

ID отримується з канонічного `/itm/<id>` або `/itm/<slug>/<id>`, а не з внутрішнього tracking `data-listingid`. Tracking-параметри видаляються, дозволено тільки HTTPS www.ebay.com. Плейсхолдер «Shop on eBay», дублікати та розпізнані секції рекомендацій відсікаються. Незнайома сторінка без явного маркера нульових результатів — помилка, а не успішний порожній список. Розпізнавання секцій рекомендацій залежить від їхньої розмітки й також потребує живої перевірки.

Нативні фільтри: `_nkw`, `_sop=10`, `_pgn=1`, `_curr`, `_udlo`, `_udhi`, `LH_ItemCondition`, `LH_BIN` / `LH_Auction`, `LH_FS`, `_sacat`, `_sasl` / `_saslop`, `_ex_kw`, `_salic` / `LH_LocatedIn`, а також item-specific параметри з `_dcat`. Поля й значення країн звірені з [Advanced Search eBay](https://www.ebay.com/sch/ebayadvsearch) 22 жовтня 2026 року. Розмір сторінки стандартний, без `_ipg=240`.

Назви й значення item-specific фільтрів кодуються додатковим рівнем URL encoding. Зарезервовані пошукові параметри не можна передавати як характеристики. Відомі суперечності у картці відкидаються; значення нативних фільтрів не копіюються у характеристики товару як начебто отримані факти. Довільна локація та сума з доставкою перевіряються за доступними полями картки.

Ідентичність для повідомлень — нормалізовані назва, ціна з валютою та локація (`app/listing_identity.py`). Індекс унікальний для користувача, спільний між його пошуками й зберігається незалежно від ID eBay. Перший успішний прохід кожного пошуку лише запам'ятовує видачу. Дати публікації не впливають на новизну.

За замовчуванням Playwright виконує JavaScript eBay і передає BeautifulSoup поточний DOM після завантаження основного вмісту. Приватні API безпосередньо не викликаються; HTML вкладених iframe, зображення, медіа та шрифти блокуються браузером. Альтернативи `curl_cffi`/`httpx` виконують лише HTTP-запит без JavaScript. Поле `raw` містить витягнуті доступні дані, а не повну копію усіх даних сервера eBay. Браузер і HTTP-транспорти мають один парсер та однакові правила новизни.

Після зміни HTML збережіть публічний зразок без cookies/токенів, перевірте `scripts/inspect_html.py`, оновіть селектори й додайте regression-fixture. Під час 403/429/503 та сторінок challenge діє спільна пауза HTTP-клієнта на 15 хвилин плюс backoff пошуку; довший Retry-After враховується. Наявні результати та неініціалізований стан зберігаються. Для обмеженої живої діагностики: `python -m app.diagnostics --watch-id 1`. curl_cffi підтримує браузерні TLS/HTTP-профілі ([документація](https://curl-cffi.readthedocs.io/en/latest/)); це HTTP-клієнт, який не запускає JavaScript і не розв’язує CAPTCHA.

## Buyee і Kleinanzeigen

Перевірено на публічній видачі 13 листопада 2026 року:

| Платформа | Запит, який використовує застосунок | Контейнер та стабільний ідентифікатор |
| --- | --- | --- |
| Buyee | `/item/search?query=walkman&sort=end&order=d&lang=en`; бюджет `aucminprice` / `aucmaxprice` | `.auctionSearchResult li.itemCard`, посилання в `.itemCard__itemName` |
| Kleinanzeigen | `/s-suchanfrage.html?keywords=sony+walkman&sortingField=SORTING_DATE&adType=OFFER`; бюджет `minPrice` / `maxPrice` | `#srchrslt-adtable article[data-adid]`, посилання в `data-href` |

Параметри Buyee звірені з формою та з `SORT_OPTIONS_DATA` живої видачі: `end/d` відповідає «Time: Newly Listed». Ціна береться з `.g-priceDetails .g-price` у JPY, а конвертована ціна поруч ігнорується. Явний нульовий результат має `.bidNotfound_middle_tit` і може прийти з HTTP 404. Підтримані картки JDirectItems Auction і JDirectItems Flea Market; Mercari повертав захисну відповідь, тому його парсер не включено.

Kleinanzeigen перенаправляє форму в канонічний URL із `sortierung:neuste` та `anzeige:angebote`. З картки зберігаються заголовок, ціна в EUR, місто та доступна позначка доставки; лічильник `#srp-breadcrumb-summary` відрізняє явну порожню видачу від помилки.

Обидва адаптери читають тільки першу сторінку. Перенаправлення дозволені тільки на пошукові маршрути того самого сайту. Скорочені HTML-фікстури в `tests/fixtures` зберігають потрібні елементи DOM для повторної перевірки без мережі.
