# iiko Read-Only CLI

Небольшой Bash-инструмент для безопасного чтения данных iiko. Он поддерживает существующее подключение `retro-protagonist` через iikoWeb (логин, пароль и ID ресторана), а при наличии отдельного `apiLogin` — также iikoCloud. Скрипт не умеет изменять данные iiko.

## Что потребуется

- Bash 4 или новее;
- `curl`;
- `jq`;
- логин/пароль iikoWeb или локальная сессионная cookie;
- для Cloud-команд номенклатуры — отдельный `apiLogin` iikoCloud API.

Режим iikoWeb повторяет read-only OLAP-подключение из `retro-protagonist`: авторизуется, получает список ресторанов и читает отчёты. Значения пароля и cookie никогда не входят в архив — они хранятся только в локальном `.env`.

## Быстрый старт

```bash
cp config.env.example .env
```

Откройте `.env` и заполните:

```dotenv
IIKO_MODE=web
IIKO_SERVER_URL=https://retro3158.iikoweb.ru
IIKO_STORE_ID=82907
IIKO_LOGIN=ваш_логин_iikoweb
IIKO_PASSWORD=ваш_пароль_iikoweb
```

Файл `.env` уже добавлен в `.gitignore`. Не отправляйте его коллегам и не добавляйте в Git. Каждому пользователю следует хранить ключ локально либо передавать его через переменную окружения.

Проверьте подключение:

```bash
./iiko.sh doctor
```

## Команды iikoWeb

Показать доступные рестораны/кассы:

```bash
./iiko.sh stores
./iiko.sh stores --json
```

Получить выручку по типам оплаты:

```bash
./iiko.sh revenue --date 2026-09-13
./iiko.sh revenue --date 2026-09-13 --output exports/revenue.json
```

Получить количество и выручку по каждому блюду:

```bash
./iiko.sh dish-sales --from 2026-09-01 --to 2026-09-13
./iiko.sh dish-sales --from 2026-09-01 --to 2026-09-13 --output exports/dish-sales.json
```

`dish-sales` возвращает для каждой позиции `productId`, `name`,
`accountingCategory`, `quantity` и `revenue`. Идентификаторы полей соответствуют
iikoWeb OLAP (`DishId`, `DishName`, `DishCategory.Accounting`), поэтому результат
можно связывать с другими выгрузками по `productId`.

Без дат используется вчерашний день.

## Дополнительные команды iikoCloud

Показать доступные организации:

```bash
./iiko.sh organizations
./iiko.sh organizations --json
```

Если организаций несколько, скопируйте нужный UUID в `IIKO_ORGANIZATION_ID` или передавайте его явно:

```bash
./iiko.sh nomenclature --organization 11111111-1111-1111-1111-111111111111
```

Выгрузить всю номенклатуру:

```bash
./iiko.sh nomenclature --output exports/nomenclature.json
```

Получить только карточки, относящиеся к Яндекс Доставке:

```bash
./iiko.sh yandex-menu --format json --output exports/yandex-menu.json
./iiko.sh yandex-menu --format csv --output exports/yandex-menu.csv
```

Без `--output` результат выводится в терминал, что удобно для агентов:

```bash
./iiko.sh yandex-menu --format json | jq '.[] | {article, name, price, isDeleted}'
```

## Формат `yandex-menu`

JSON содержит массив объектов со стабильными полями:

- `organizationId`;
- `groupId`, `groupName`;
- `productId`, `article`, `name`, `type`, `measureUnit`;
- `categoryId`, `categoryName`;
- `price` — первая текущая цена, которую вернул API, либо `null`;
- `isDeleted` — признак удалённой/архивной карточки.

Фильтр регистронезависимо ищет `Яндекс` или `Yandex` в названии группы и карточки. Удалённые карточки намеренно остаются в результате, чтобы аудит их не потерял.

## Инструкции для Claude и Codex

Можно дать агенту такой запрос:

```text
Используй только ./iiko.sh. Сначала выполни ./iiko.sh doctor, затем получай
JSON командами ./iiko.sh revenue или ./iiko.sh dish-sales. Если настроен
iikoCloud API_LOGIN, также можно использовать nomenclature и yandex-menu.
Не обращайся к iiko напрямую, не изменяй .env и не выполняй операции записи.
```

Для iikoWeb разрешены только авторизация и retrieval-вызовы:

- `POST /api/auth/login`;
- `GET /api/auth`;
- `GET /api/stores/list`;
- `POST /api/olap/init`;
- `POST /api/olap/fetch/{id}/grouped-table`.

Для iikoCloud допускаются только три retrieval-вызова:

- `POST /access_token`;
- `POST /organizations`;
- `POST /nomenclature`.

В iiko эти POST-запросы только получают данные. Другие адреса блокируются локальным allowlist до обращения к сети. Команды создания заказов, изменения цен, перемещения и удаления отсутствуют.

## Переменные окружения

| Переменная | Обязательна | Назначение |
|---|---:|---|
| `IIKO_MODE` | Нет | `web`, `cloud` или автоматический выбор |
| `IIKO_SERVER_URL` | Для iikoWeb | Адрес ресторана iikoWeb |
| `IIKO_STORE_ID` | Для iikoWeb | Числовой ID ресторана |
| `IIKO_LOGIN` | Для входа без cookie | Логин iikoWeb; также поддерживается `IIKO_USERNAME` |
| `IIKO_PASSWORD` | Для входа без cookie | Пароль iikoWeb |
| `IIKO_COOKIE` | Альтернатива логину | Cookie или JWT текущей сессии |
| `IIKO_API_LOGIN` | Для Cloud-команд | API-логин iikoCloud |
| `IIKO_ORGANIZATION_ID` | При нескольких организациях | Организация по умолчанию |
| `IIKO_API_BASE_URL` | Нет | Базовый API URL; разрешён только HTTPS |
| `IIKO_TIMEOUT` | Нет | Таймаут запросов от 1 до 300 секунд |

Переменные, заданные в оболочке, имеют приоритет над `.env`:

```bash
IIKO_API_LOGIN='...' ./iiko.sh organizations --json
```

## Типичные ошибки

- `Set IIKO_COOKIE or both IIKO_LOGIN...` — задайте iikoWeb-логин и пароль либо cookie.
- `This command requires iikoCloud` — команда номенклатуры требует отдельный `apiLogin`; используйте `revenue`/`dish-sales` для существующего iikoWeb-подключения.
- HTTP 401/403 — API-логин неверен, отключён или не имеет доступа к организации.
- HTTP 429 — превышен лимит API; повторите запрос позже.
- HTTP 5xx — временная ошибка iikoCloud.
- `Select one with --organization` — доступно несколько организаций; укажите UUID.
- `Required command not found: jq` — установите `jq` через пакетный менеджер системы.

## Тестирование

Тесты полностью автономны: используют локальные ответы API и не подключаются к iiko.

```bash
bash -n iiko.sh tests/fake-curl.sh tests/test_iiko.sh
bash tests/test_iiko.sh
```

Для реальной проверки достаточно `./iiko.sh doctor`. Токен авторизации хранится только в памяти процесса и не попадает в JSON/CSV.
