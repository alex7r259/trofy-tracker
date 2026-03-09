# Trophy Raid Server — оффлайн-система трекинга

## Структура

```
trophy-server/
├── server.py           # Основной сервер
├── download_tiles.py   # Скачивание карт (запускать дома)
├── dashboard.html      # UI для судей (положить сюда)
├── trophy_raid.db      # БД (создаётся автоматически)
└── tiles/              # Оффлайн-карта
    ├── 8/
    ├── 9/
    ├── ...
    └── 16/
```

## Подготовка дома (нужен интернет)

### 1. Скачать тайлы карты

```bash
# По координатам центра + радиус
python download_tiles.py --lat 59.419 --lon 56.834 --radius 30 --zoom 8-16

# По GPX-файлу маршрута
python download_tiles.py --gpx route.gpx --buffer 5 --zoom 8-16

# По прямоугольнику координат
python download_tiles.py --bbox 55.5,37.0,56.0,38.0 --zoom 8-16

# Сначала посмотреть сколько тайлов (без скачивания)
python download_tiles.py --lat 55.75 --lon 37.62 --radius 30 --zoom 8-16 --dry-run
```

Типичные размеры:
- 20 км, зум 8-15: ~3000 тайлов, ~80 МБ
- 50 км, зум 8-16: ~30000 тайлов, ~800 МБ
- 100 км, зум 8-14: ~5000 тайлов, ~130 МБ

### 2. Подготовить UI

Положить `dashboard.html` (мобильный дашборд) в папку рядом с `server.py`.

В dashboard.html изменить URL тайлов с OSM на локальный сервер:

```javascript
// Было (онлайн):
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', ...);

// Стало (оффлайн):
L.tileLayer('/tiles/{z}/{x}/{y}.png', {maxZoom: 16});
```

### 3. Проверить что всё работает

```bash
python server.py --gateway http://127.0.0.1 --port 8080
# Открыть http://localhost:8080
```

## На соревнованиях

### Схема подключения

```
[30 трекеров] ---LoRa mesh---> [Gateway ESP32]
                                     |
                                  Wi-Fi
                                     |
                               [Ноутбук]
                            server.py + SQLite
                            раздаёт Wi-Fi (AP)
                                /    |    \
                          судья1  судья2  хронометр
                          телефон планшет  ноутбук
```

### Запуск

1. Gateway ESP32 поднимает Wi-Fi AP (или подключается к роутеру)
2. Ноутбук подключается к той же сети
3. Ноутбук раздаёт свой Wi-Fi (точка доступа) для судей

```bash
# Gateway на 192.168.4.1 (AP режим ESP32)
python server.py --gateway http://192.168.4.1 --port 8080

# Gateway на другом IP (через роутер)
python server.py --gateway http://192.168.1.100 --port 8080

# Изменить интервал опроса
python server.py --gateway http://192.168.4.1 --poll 1 --port 8080
```

4. Судьи подключаются к Wi-Fi ноутбука
5. Открывают в браузере `http://<ip-ноутбука>:8080`

## API

| Метод  | Путь                         | Описание                          |
|--------|------------------------------|-----------------------------------|
| GET    | /                            | UI (dashboard.html)               |
| GET    | /tiles/{z}/{x}/{y}.png       | Оффлайн тайлы карты              |
| GET    | /api/status                  | Статус сервера и шлюза           |
| GET    | /api/nodes                   | Все узлы (текущие данные)        |
| GET    | /api/node/{id}               | Данные конкретного узла          |
| GET    | /api/tracks                  | Сводка треков                    |
| GET    | /api/tracks/{id}             | Трек узла (JSON)                 |
| GET    | /api/tracks/{id}/gpx         | Трек узла (GPX файл)            |
| GET    | /api/participants            | Все участники                    |
| POST   | /api/participants            | Добавить/обновить участника      |
| DELETE | /api/participants/{dev_id}   | Удалить участника                |
| GET    | /api/checkpoints             | Все контрольные точки            |
| POST   | /api/checkpoints             | Добавить/обновить КТ             |
| POST   | /api/checkpoints/import-gpx  | Импорт КТ из GPX                |
| DELETE | /api/checkpoints/{id}        | Удалить КТ                       |
| GET    | /api/categories              | Категории                        |
| POST   | /api/categories              | Добавить категорию               |
| DELETE | /api/categories/{id}         | Удалить категорию                |
| GET    | /api/cp-log                  | Лог прохождения КТ              |
| GET    | /api/scores                  | Таблица очков                    |

## Зависимости

Только стандартная библиотека Python 3.8+. Ничего устанавливать не нужно.

## Бэкап

БД — один файл `trophy_raid.db`. Копировать периодически на флешку.
Тайлы — папка `tiles/`. Не меняется во время гонки.
