"""English display of system errors saved by earlier versions."""

LEGACY_ERRORS = {'Недозволене джерело запиту': 'Request origin is not allowed',
 'Додайте TELEGRAM_BOT_TOKEN або tg у .env': 'Telegram sign-in is not configured. Contact the administrator.',
 'Забагато запитів. Зачекайте хвилину перед новим кодом.': 'Too many login requests. Wait at least a minute '
                                                           'before requesting another code.',
 'Код недійсний або закінчився. Створіть новий запит.': 'This code has expired or is no longer valid. '
                                                        'Request a new code.',
 'Спочатку відкрийте бота за посиланням і натисніть Start': 'Open the bot using your sign-in link and press '
                                                            'Start first.',
 'Запит уже використано': 'This sign-in request has already been used.',
 'Невірний код підтвердження': 'Incorrect verification code. Check the six digits in Telegram.',
 'Немає активної сесії': 'No active session',
 'Пошук не знайдено': 'Search not found',
 'Максимум 30 пошуків на користувача': 'You can save up to 30 searches. Delete a search before adding '
                                       'another.',
 'Спочатку відновіть пошук': 'Resume this search first.',
 'Ручна перевірка доступна раз на хвилину': 'You can request a manual check once per minute.',
 'Некоректна пагінація': 'Invalid page parameters',
 'Товар не знайдено': 'Item not found',
 'Відновіть пошук для завантаження характеристик': 'Resume this search to load item details.',
 'Характеристики вже оновлено': 'Item details were updated less than five minutes ago.',
 'Характеристики додано в чергу; зачекайте наступної перевірки': 'Item details queued. They will update '
                                                                 'after the next successful check.',
 'Вкажіть коректний Telegram username без посилання': 'Enter your Telegram username, such as @your_username. '
                                                      'Use your username, not a phone number or display '
                                                      'name.',
 'Ціна від не може перевищувати ціну до': 'Minimum price cannot be higher than maximum price.',
 'Вкажіть назву та значення кожного атрибута (до 100 / 300 символів)': 'Each attribute needs a name (up to '
                                                                       '100 characters) and a value (up to '
                                                                       '300 characters).',
 'Виключення мають містити від 1 до 100 символів': 'Each excluded word or phrase must contain 1 to 100 '
                                                   'characters.',
 'Поле не може бути порожнім': 'This field cannot be blank.',
 'Некоректна адреса EBAY_PROXY': 'Invalid EBAY_PROXY address',
 'Некоректний порт EBAY_PROXY': 'Invalid EBAY_PROXY port',
 'Профіль браузера зайнятий іншим процесом. Зупиніть worker або використайте інший --profile-dir.': 'The '
                                                                                                    'browser '
                                                                                                    'profile '
                                                                                                    'is in '
                                                                                                    'use. '
                                                                                                    'Stop '
                                                                                                    'the '
                                                                                                    'other '
                                                                                                    'worker '
                                                                                                    'or use '
                                                                                                    'a '
                                                                                                    'different '
                                                                                                    '--profile-dir.',
 'Chromium не запустився. Перевірте встановлення браузера та seccomp-профіль Docker у README.': 'Chromium '
                                                                                                'could not '
                                                                                                'start. '
                                                                                                'Check the '
                                                                                                'browser '
                                                                                                'installation '
                                                                                                'and Docker '
                                                                                                'seccomp '
                                                                                                'profile in '
                                                                                                'the README.',
 'Недозволена адреса браузерного запиту': 'Browser request address is not allowed',
 'eBay перенаправив браузер на недозволений домен': 'eBay redirected the browser to a domain that is not '
                                                    'allowed.',
 'Перевищено час завантаження eBay у браузері. Спробуємо пізніше.': 'eBay took too long to load. A retry is '
                                                                    'scheduled automatically.',
 'Помилка браузера eBay. На наступній спробі браузер буде запущено повторно.': 'The eBay browser stopped '
                                                                               'responding. It will restart '
                                                                               'on the next attempt.',
 'eBay заблокував запит або показав сторінку перевірки. Спробуємо пізніше.': 'eBay blocked the request or '
                                                                             'displayed a verification page. '
                                                                             'A retry is scheduled '
                                                                             'automatically.',
 'eBay вимагає перевірку браузера. HTML видачі недоступний.': 'eBay requires browser verification. Search '
                                                              'results are temporarily unavailable.',
 'Не розпізнано HTML видачі eBay. Потрібна перевірка селекторів; початковий знімок не збережено.': 'The eBay '
                                                                                                   'results '
                                                                                                   'format '
                                                                                                   'was not '
                                                                                                   'recognized. '
                                                                                                   'The '
                                                                                                   'parser '
                                                                                                   'needs an '
                                                                                                   'update; '
                                                                                                   'the '
                                                                                                   'initial '
                                                                                                   'snapshot '
                                                                                                   'has not '
                                                                                                   'been '
                                                                                                   'saved.',
 'Не розпізнано сторінку товару eBay; характеристики поки недоступні.': 'The eBay item format was not '
                                                                        'recognized. Item details are '
                                                                        'temporarily unavailable.',
 'Недозволена адреса товару eBay': 'eBay item address is not allowed',
 ' Усі запити до eBay призупинено щонайменше на ': ' All eBay requests are paused for at least ',
 ' хв.': ' min.',
 'Доступ eBay обмежено. Діє спільна пауза для всіх пошуків.': 'eBay access is restricted. All searches are '
                                                              'waiting for the shared retry delay.',
 'Не вдалося з’єднатися з eBay. Перевірте мережу та налаштування HTTP-клієнта.': 'Could not connect to eBay. '
                                                                                 'Check the network and HTTP '
                                                                                 'client settings.',
 'eBay вимагає підтвердження людиною. Автоматичну перевірку призупинено.': 'eBay requires human '
                                                                           'verification. Automatic requests '
                                                                           'are temporarily paused.',
 'eBay обмежив доступ (HTTP ': 'eBay restricted access (HTTP ',
 'eBay вимагає перевірку браузера; сторінка недоступна HTTP-клієнту.': 'eBay requires browser verification; '
                                                                       'the HTTP client cannot access this '
                                                                       'page.',
 'eBay перенаправив запит за межі сторінок товарів і пошуку': 'eBay redirected the request outside item and '
                                                              'search pages.',
 'eBay повернув HTTP ': 'eBay returned HTTP ',
 'Сторінка eBay перевищує допустимий розмір': 'The eBay page exceeds the size limit.',
 'eBay повернув сторінку перевірки замість результатів пошуку.': 'eBay returned a verification page instead '
                                                                 'of search results.',
 'Забагато перенаправлень eBay': 'Too many eBay redirects',
 'Сесія закінчилася. Увійдіть через Telegram.': 'Your session has expired. Sign in with Telegram.',
 'Повторне використання refresh token. Увійдіть ще раз.': 'This session is no longer valid. Sign in again.',
 'Сесію вже оновлено. Повторіть вхід.': 'This session has already been refreshed. Sign in again.',
 'Користувача не знайдено': 'User not found',
 'Увійдіть через Telegram': 'Sign in with Telegram',
 'Access token закінчився або недійсний': 'The access token has expired or is invalid.',
 'Не задано TELEGRAM_BOT_TOKEN (або tg) у .env': 'The Telegram bot is not configured. Contact the '
                                                 'administrator.',
 'Telegram API недоступний; перевірте підключення': 'Telegram is unavailable. Check the network connection.',
 'Telegram 409: зупиніть інший polling-процес або вимкніть webhook цього бота': 'Telegram 409: stop the '
                                                                                'other bot process or '
                                                                                "disable this bot's webhook.",
 'Telegram API відхилив запит (код ': 'Telegram rejected the request (code ',
 'Новий товар на eBay': 'New eBay listing',
 'Доставка не вказана': 'Shipping not specified',
 'Локація не вказана': 'Location not specified',
 'Відкрити на eBay ↗': 'View on eBay ↗',
 'Не встановлено Xvfb. Використайте браузерний Docker-образ або EBAY_BROWSER_HEADLESS=true.': 'Xvfb is not '
                                                                                              'installed. '
                                                                                              'Use the '
                                                                                              'browser '
                                                                                              'Docker image '
                                                                                              'or set '
                                                                                              'EBAY_BROWSER_HEADLESS=true.',
 'Не вдалося запустити віртуальний екран Chromium': 'Could not start the Chromium virtual display.',
 'Віртуальний екран Chromium завершився під час запуску': 'The Chromium virtual display stopped during '
                                                          'startup.',
 'Пошук працює, але характеристики недоступні: ': 'Search results are available, but item details could not '
                                                  'be loaded: ',
 'Внутрішня помилка моніторингу; дивіться логи worker': 'Monitoring encountered an internal error. Check the '
                                                        'worker logs.',
 'Внутрішня помилка бота; дивіться логи': 'The Telegram bot encountered an internal error. Check the bot '
                                          'logs.',
 'Повторимо із затримкою.': 'A retry is scheduled automatically.'}


def display_error(message: str | None) -> str | None:
    if message:
        for old, new in LEGACY_ERRORS.items():
            message = message.replace(old, new)
    return message
