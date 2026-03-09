#!/usr/bin/env python3
"""
Trophy Raid Server — оффлайн-сервер для трофи-рейда.

Архитектура:
    [30 трекеров] --LoRa--> [Gateway ESP32] <--Wi-Fi--> [Этот сервер на ноутбуке]
                                                              |
                                                         Wi-Fi AP ноутбука
                                                        /     |      \\
                                                   судья1  судья2  хронометраж

Запуск:
    python server.py --gateway http://192.168.4.1 --port 8080

Сервер:
    - Опрашивает Gateway по HTTP (GET /)
    - Хранит всё в SQLite (trophy_raid.db)
    - Раздаёт оффлайн-тайлы из ./tiles/
    - REST API для UI
    - WebSocket для push-обновлений

Подготовка дома (с интернетом):
    python download_tiles.py --lat 55.75 --lon 37.62 --radius 30 --zoom 8-16
"""

import asyncio
import json
import time
import math
import logging
import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
import threading
import struct
import os

# Минимальный HTTP сервер без зависимостей (только stdlib)

logging.basicConfig(level=logging.WARNING)  # подавляем стандартные логи
log = logging.getLogger('server')

try:
    import console_ui as cui
except ImportError:
    # Fallback если console_ui.py нет рядом
    class _Stub:
        def __getattr__(self, n):
            return lambda *a, **k: None
    cui = _Stub()

# ============ CONFIG ============
DB_PATH = 'trophy_raid.db'
TILE_DIR = Path('tiles')
UI_FILE = Path('dashboard.html')


# ============ DATABASE ============
class Database:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self._init_tables()
    
    def _init_tables(self):
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS nodes (
                dev_id INTEGER PRIMARY KEY,
                lat REAL DEFAULT 0, lon REAL DEFAULT 0, alt REAL DEFAULT 0,
                speed INTEGER DEFAULT 0, heading INTEGER DEFAULT 0,
                battery INTEGER DEFAULT 100, hdop REAL DEFAULT 99,
                flags INTEGER DEFAULT 0, timestamp INTEGER DEFAULT 0,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dev_id INTEGER, lat REAL, lon REAL, alt REAL,
                speed INTEGER, recorded_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trk_dev ON tracks(dev_id);
            CREATE INDEX IF NOT EXISTS idx_trk_time ON tracks(recorded_at);
            
            CREATE TABLE IF NOT EXISTS participants (
                dev_id INTEGER PRIMARY KEY,
                num INTEGER UNIQUE, pilot TEXT, navigator TEXT DEFAULT '',
                car TEXT DEFAULT '', cat_id TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY, name TEXT,
                lat REAL, lon REAL, radius INTEGER DEFAULT 50,
                cp_type TEXT DEFAULT 'cp', points INTEGER DEFAULT 10,
                cat_ids TEXT DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS categories (
                id TEXT PRIMARY KEY, name TEXT, color TEXT DEFAULT '#ffab00'
            );
            CREATE TABLE IF NOT EXISTS cp_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dev_id INTEGER, cp_id TEXT, passed_at TEXT,
                UNIQUE(dev_id, cp_id)
            );
        ''')
        self.conn.commit()
        
        # Категории по умолчанию
        cats = self.conn.execute('SELECT COUNT(*) FROM categories').fetchone()[0]
        if cats == 0:
            for cid, name, color in [('c1','ТР-1','#00e676'),('c2','ТР-2','#29b6f6'),('c3','ТР-3','#ffab00')]:
                self.conn.execute('INSERT INTO categories VALUES (?,?,?)', (cid, name, color))
            self.conn.commit()
        
        log.info(f'DB: {self.path}')
        cui.ok(f'БД инициализирована: {self.path}')
    
    def update_node(self, dev_id, lat, lon, alt, speed, heading, battery, hdop, flags):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute('''
            INSERT INTO nodes (dev_id,lat,lon,alt,speed,heading,battery,hdop,flags,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dev_id) DO UPDATE SET
                lat=?,lon=?,alt=?,speed=?,heading=?,battery=?,hdop=?,flags=?,updated_at=?
        ''', (dev_id,lat,lon,alt,speed,heading,battery,hdop,flags,now,
              lat,lon,alt,speed,heading,battery,hdop,flags,now))
    
    def add_track_point(self, dev_id, lat, lon, alt, speed):
        if lat == 0 and lon == 0:
            return
        now = datetime.now(timezone.utc).isoformat()
        # Не дублируем если та же позиция и < 30с
        last = self.conn.execute(
            'SELECT lat,lon,recorded_at FROM tracks WHERE dev_id=? ORDER BY id DESC LIMIT 1',
            (dev_id,)).fetchone()
        if last and last['lat'] == lat and last['lon'] == lon:
            try:
                dt = datetime.fromisoformat(last['recorded_at'].replace('Z','+00:00'))
                if (datetime.now(timezone.utc) - dt).total_seconds() < 30:
                    return
            except:
                pass
        self.conn.execute(
            'INSERT INTO tracks (dev_id,lat,lon,alt,speed,recorded_at) VALUES (?,?,?,?,?,?)',
            (dev_id, lat, lon, alt, speed, now))
    
    def check_cp(self, dev_id, lat, lon):
        cps = self.conn.execute('SELECT * FROM checkpoints').fetchall()
        for cp in cps:
            existing = self.conn.execute(
                'SELECT 1 FROM cp_log WHERE dev_id=? AND cp_id=?',
                (dev_id, cp['id'])).fetchone()
            if existing:
                continue
            dist = haversine(lat, lon, cp['lat'], cp['lon'])
            if dist <= cp['radius']:
                now = datetime.now(timezone.utc).isoformat()
                self.conn.execute(
                    'INSERT OR IGNORE INTO cp_log (dev_id,cp_id,passed_at) VALUES (?,?,?)',
                    (dev_id, cp['id'], now))
                log.info(f'CP: dev={dev_id} cp={cp["name"]} d={dist:.0f}m pts={cp["points"]}')
                cui.event(f'КТ ВЗЯТА! #{dev_id} → {cp["name"]} (+{cp["points"]} очков)', cui.C.GRN)
    
    def commit(self):
        self.conn.commit()
    
    def get_nodes(self):
        rows = self.conn.execute('SELECT * FROM nodes').fetchall()
        now = datetime.now(timezone.utc)
        result = []
        for r in rows:
            age_ms = 999999
            if r['updated_at']:
                try:
                    dt = datetime.fromisoformat(r['updated_at'].replace('Z','+00:00'))
                    age_ms = int((now - dt).total_seconds() * 1000)
                except:
                    pass
            result.append({
                'id':r['dev_id'],'lat':r['lat'],'lon':r['lon'],'alt':r['alt'],
                'speed':r['speed'],'heading':r['heading'],'battery':r['battery'],
                'hdop':r['hdop'],'flags':r['flags'],'age':age_ms
            })
        return result
    
    def get_tracks_summary(self):
        return [dict(r) for r in self.conn.execute(
            'SELECT dev_id, COUNT(*) as points FROM tracks GROUP BY dev_id').fetchall()]
    
    def get_track(self, dev_id, limit=5000):
        rows = self.conn.execute(
            'SELECT lat,lon,alt,speed,recorded_at FROM tracks WHERE dev_id=? ORDER BY id DESC LIMIT ?',
            (dev_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
    
    def get_track_gpx(self, dev_id):
        rows = self.conn.execute(
            'SELECT lat,lon,alt,recorded_at FROM tracks WHERE dev_id=? ORDER BY id', (dev_id,)).fetchall()
        p = self.conn.execute('SELECT * FROM participants WHERE dev_id=?', (dev_id,)).fetchone()
        name = f'#{p["num"]} {p["pilot"]}' if p else f'Dev#{dev_id}'
        gpx = '<?xml version="1.0"?>\n<gpx version="1.1" creator="TrophyRaid">\n'
        gpx += f'<trk><name>{name}</name><trkseg>\n'
        for r in rows:
            gpx += f'<trkpt lat="{r["lat"]}" lon="{r["lon"]}"><ele>{r["alt"]}</ele><time>{r["recorded_at"]}</time></trkpt>\n'
        gpx += '</trkseg></trk>\n</gpx>'
        return gpx, name
    
    def get_scores(self):
        return [dict(r) for r in self.conn.execute('''
            SELECT p.dev_id, p.num, p.pilot, p.navigator, p.car, p.cat_id,
                   COALESCE(SUM(cp.points),0) as score,
                   COUNT(cl.cp_id) as cps_passed
            FROM participants p
            LEFT JOIN cp_log cl ON p.dev_id=cl.dev_id
            LEFT JOIN checkpoints cp ON cl.cp_id=cp.id
            GROUP BY p.dev_id ORDER BY score DESC, p.num ASC
        ''').fetchall()]
    
    def get_cp_log(self):
        return [dict(r) for r in self.conn.execute('''
            SELECT cl.dev_id, cl.cp_id, cl.passed_at, cp.name as cp_name, cp.points
            FROM cp_log cl JOIN checkpoints cp ON cl.cp_id=cp.id
            ORDER BY cl.passed_at DESC
        ''').fetchall()]


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


# ============ GATEWAY POLLER ============
class GatewayPoller:
    def __init__(self, url, db, interval=2.0):
        self.url = url.rstrip('/')
        self.db = db
        self.interval = interval
        self.running = False
        self.poll_count = 0
        self.last_error = None
        self.connected = False
    
    def start(self):
        self.running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        cui.info(f'Опрос шлюза: {self.url} каждые {self.interval}с')
    
    def _loop(self):
        import urllib.request
        while self.running:
            try:
                req = urllib.request.Request(self.url + '/', headers={'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                
                nodes = data.get('nodes', [])
                for n in nodes:
                    did = n.get('id')
                    if did is None:
                        continue
                    self.db.update_node(did, n.get('lat',0), n.get('lon',0), n.get('alt',0),
                        n.get('speed',0), n.get('heading',0), n.get('battery',100),
                        n.get('hdop',99), n.get('flags',0))
                    self.db.add_track_point(did, n.get('lat',0), n.get('lon',0), n.get('alt',0), n.get('speed',0))
                    self.db.check_cp(did, n.get('lat',0), n.get('lon',0))
                
                self.db.commit()
                self.poll_count += 1
                self.connected = True
                self.last_error = None
                
                if self.poll_count % 50 == 0:
                    nodes_list = self.db.get_nodes()
                    cui.log_line('INFO', f'Опрос #{self.poll_count}: {len(nodes)} узлов от шлюза, {len(nodes_list)} в БД')
            
            except Exception as e:
                self.connected = False
                self.last_error = str(e)
                if self.poll_count % 10 == 0:
                    cui.log_line('ERR', f'Шлюз: {e}')
            
            time.sleep(self.interval)


# ============ HTTP SERVER ============
class RequestHandler(SimpleHTTPRequestHandler):
    """Обрабатывает API, тайлы и статику."""
    
    db: Database = None
    poller: GatewayPoller = None
    
    def log_message(self, format, *args):
        pass  # Тихий режим
    
    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)
    
    def _text(self, text, content_type='text/plain', status=200):
        body = text.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)
    
    def _file(self, data, content_type, filename=None):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)
    
    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(length) if length else b''

    @staticmethod
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
    
    def do_GET(self):
        path = self.path.split('?')[0]
        
        # UI
        if path == '/':
            if UI_FILE.exists():
                self._text(UI_FILE.read_text(encoding='utf-8'), 'text/html')
            else:
                self._text('<h1>Trophy Raid Server</h1><p>dashboard.html не найден</p>', 'text/html')
            return
        
        # Тайлы
        if path.startswith('/tiles/'):
            # Не допускаем выход из директории тайлов через ../
            requested = path[7:].lstrip('/')
            tile_path = (TILE_DIR / requested).resolve()
            tiles_root = TILE_DIR.resolve()
            if tiles_root in tile_path.parents and tile_path.suffix == '.png' and tile_path.exists():
                self._file(tile_path.read_bytes(), 'image/png')
            else:
                self.send_error(404)
            return
        
        # API
        if path == '/api/status':
            self._json({
                'gateway_connected': self.poller.connected if self.poller else False,
                'gateway_url': self.poller.url if self.poller else '',
                'poll_count': self.poller.poll_count if self.poller else 0,
                'last_error': self.poller.last_error if self.poller else None,
                'tiles_available': TILE_DIR.exists(),
                'db_path': str(DB_PATH),
                'nodes_count': len(self.db.get_nodes()),
                'tracks_count': sum(t['points'] for t in self.db.get_tracks_summary()),
            })
            return
        
        if path == '/api/nodes':
            self._json(self.db.get_nodes())
            return
        
        if path.startswith('/api/node/'):
            dev_id = self._safe_int(path.split('/')[-1])
            if dev_id is None:
                self._json({'error': 'invalid dev_id'}, 400)
                return
            row = self.db.conn.execute('SELECT * FROM nodes WHERE dev_id=?', (dev_id,)).fetchone()
            self._json(dict(row) if row else {'error': 'not found'})
            return
        
        if path == '/api/tracks':
            self._json(self.db.get_tracks_summary())
            return
        
        if path.startswith('/api/tracks/') and path.endswith('/gpx'):
            dev_id = self._safe_int(path.split('/')[-2])
            if dev_id is None:
                self._json({'error': 'invalid dev_id'}, 400)
                return
            gpx, name = self.db.get_track_gpx(dev_id)
            self._file(gpx.encode(), 'application/gpx+xml', f'track-{dev_id}.gpx')
            return
        
        if path.startswith('/api/tracks/'):
            dev_id = self._safe_int(path.split('/')[-1])
            if dev_id is None:
                self._json({'error': 'invalid dev_id'}, 400)
                return
            self._json(self.db.get_track(dev_id))
            return
        
        if path == '/api/participants':
            rows = self.db.conn.execute('SELECT * FROM participants').fetchall()
            self._json([dict(r) for r in rows])
            return
        
        if path == '/api/checkpoints':
            rows = self.db.conn.execute('SELECT * FROM checkpoints').fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d['cat_ids'] = json.loads(d['cat_ids'])
                result.append(d)
            self._json(result)
            return
        
        if path == '/api/categories':
            rows = self.db.conn.execute('SELECT * FROM categories').fetchall()
            self._json([dict(r) for r in rows])
            return
        
        if path == '/api/cp-log':
            self._json(self.db.get_cp_log())
            return
        
        if path == '/api/scores':
            self._json(self.db.get_scores())
            return
        
        self.send_error(404)
    
    def do_POST(self):
        path = self.path.split('?')[0]
        body = self._read_body()
        
        if path == '/api/participants':
            data = json.loads(body)
            self.db.conn.execute('''
                INSERT INTO participants (dev_id,num,pilot,navigator,car,cat_id)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(dev_id) DO UPDATE SET
                    num=excluded.num, pilot=excluded.pilot, navigator=excluded.navigator,
                    car=excluded.car, cat_id=excluded.cat_id
            ''', (data['dev_id'], data['num'], data['pilot'],
                  data.get('navigator',''), data.get('car',''), data.get('cat_id','')))
            self.db.commit()
            self._json({'ok': True})
            return
        
        if path == '/api/checkpoints':
            data = json.loads(body)
            cp_id = data.get('id') or f'cp{int(time.time()*1000)}'
            self.db.conn.execute('''
                INSERT INTO checkpoints (id,name,lat,lon,radius,cp_type,points,cat_ids)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, lat=excluded.lat, lon=excluded.lon,
                    radius=excluded.radius, cp_type=excluded.cp_type,
                    points=excluded.points, cat_ids=excluded.cat_ids
            ''', (cp_id, data['name'], data['lat'], data['lon'],
                  data.get('radius',50), data.get('cp_type','cp'),
                  data.get('points',10), json.dumps(data.get('cat_ids',[]))))
            self.db.commit()
            self._json({'ok': True, 'id': cp_id})
            return
        
        if path == '/api/checkpoints/import-gpx':
            # Простой парсер multipart не нужен — принимаем GPX как текст
            import xml.etree.ElementTree as ET
            data = json.loads(body)
            gpx_text = data.get('gpx', '')
            pts = data.get('points', 10)
            radius = data.get('radius', 50)
            cat_ids = data.get('cat_ids', [])
            
            root = ET.fromstring(gpx_text)
            ns = ''
            if root.tag.startswith('{'):
                ns = root.tag.split('}')[0] + '}'
            
            imported = []
            for tag in ['wpt', 'trkpt', 'rtept']:
                for el in root.iter(f'{ns}{tag}'):
                    lat = float(el.get('lat'))
                    lon = float(el.get('lon'))
                    name_el = el.find(f'{ns}name')
                    name = name_el.text if name_el is not None else f'КТ-{len(imported)+1}'
                    cp_id = f'cp{int(time.time()*1000)}{len(imported)}'
                    self.db.conn.execute(
                        'INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?,?)',
                        (cp_id, name, lat, lon, radius, 'cp', pts, json.dumps(cat_ids)))
                    imported.append({'id': cp_id, 'name': name, 'lat': lat, 'lon': lon})
            
            self.db.commit()
            self._json({'ok': True, 'imported': len(imported), 'checkpoints': imported})
            return
        
        if path == '/api/categories':
            data = json.loads(body)
            cat_id = data.get('id') or f'c{int(time.time()*1000)}'
            self.db.conn.execute(
                'INSERT INTO categories VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, color=excluded.color',
                (cat_id, data['name'], data.get('color','#ffab00')))
            self.db.commit()
            self._json({'ok': True, 'id': cat_id})
            return
        
        self.send_error(404)
    
    def do_DELETE(self):
        path = self.path.split('?')[0]
        
        if path.startswith('/api/participants/'):
            dev_id = self._safe_int(path.split('/')[-1])
            if dev_id is None:
                self._json({'error': 'invalid dev_id'}, 400)
                return
            self.db.conn.execute('DELETE FROM participants WHERE dev_id=?', (dev_id,))
            self.db.commit()
            self._json({'ok': True})
            return
        
        if path.startswith('/api/checkpoints/'):
            cp_id = path.split('/')[-1]
            self.db.conn.execute('DELETE FROM checkpoints WHERE id=?', (cp_id,))
            self.db.conn.execute('DELETE FROM cp_log WHERE cp_id=?', (cp_id,))
            self.db.commit()
            self._json({'ok': True})
            return
        
        if path.startswith('/api/categories/'):
            cat_id = path.split('/')[-1]
            self.db.conn.execute('DELETE FROM categories WHERE id=?', (cat_id,))
            self.db.commit()
            self._json({'ok': True})
            return
        
        self.send_error(404)


# ============ MAIN ============
def main():
    parser = argparse.ArgumentParser(
        description='Trophy Raid Server — оффлайн-сервер гонки',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Подготовка (дома с интернетом):
    python download_tiles.py --lat 55.75 --lon 37.62 --radius 30 --zoom 8-16

Запуск (на соревнованиях):
    python server.py --gateway http://192.168.4.1 --port 8080

Клиенты подключаются к Wi-Fi ноутбука и открывают:
    http://<ip-ноутбука>:8080
        ''')
    
    parser.add_argument('--gateway', default='http://192.168.0.17',
                        help='URL шлюза ESP32 (по умолч. http://192.168.4.1)')
    parser.add_argument('--poll', type=float, default=2.0,
                        help='Интервал опроса шлюза в секундах (по умолч. 2)')
    parser.add_argument('--port', type=int, default=8080,
                        help='HTTP порт сервера (по умолч. 8080)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Адрес для прослушивания (по умолч. 0.0.0.0)')
    parser.add_argument('--db', default='trophy_raid.db',
                        help='Путь к БД (по умолч. trophy_raid.db)')
    parser.add_argument('--tiles', default='tiles',
                        help='Папка с тайлами (по умолч. ./tiles)')
    
    args = parser.parse_args()
    
    global DB_PATH, TILE_DIR
    DB_PATH = args.db
    TILE_DIR = Path(args.tiles)
    
    # Баннер
    cui.banner_server()
    
    # Инициализация
    cui.section('Инициализация')
    database = Database(DB_PATH)
    
    # Поллер шлюза
    poller = GatewayPoller(args.gateway, database, args.poll)
    poller.start()
    
    # HTTP сервер
    RequestHandler.db = database
    RequestHandler.poller = poller
    
    server = HTTPServer((args.host, args.port), RequestHandler)
    
    # Статус
    cui.section('Статус системы')
    
    # Тайлы
    if TILE_DIR.exists():
        tile_count = sum(1 for _ in TILE_DIR.rglob('*.png'))
        cui.kv_status('Тайлы карты', True, f'{tile_count:,} файлов в {TILE_DIR}/')
    else:
        cui.kv_status('Тайлы карты', False, err_text=f'не найдены в {TILE_DIR}/')
        cui.warn('Запустите download_tiles.py для скачивания карты')
    
    cui.kv('Шлюз', f'{args.gateway} (опрос каждые {args.poll}с)')
    cui.kv('HTTP', f'http://{args.host}:{args.port}')
    cui.kv('БД', args.db, cui.C.GRY)
    
    # API
    cui.section('API endpoints', cui.C.GRY)
    endpoints = [
        ('GET  /', 'Dashboard UI'),
        ('GET  /tiles/{z}/{x}/{y}.png', 'Оффлайн-тайлы'),
        ('GET  /api/nodes', 'Все узлы'),
        ('GET  /api/scores', 'Таблица очков'),
        ('GET  /api/tracks/{id}/gpx', 'Трек в GPX'),
        ('POST /api/participants', 'Участники'),
        ('POST /api/checkpoints', 'Контрольные точки'),
    ]
    for ep, desc in endpoints:
        print(f'    {cui.C.CYN}{ep:<38}{cui.C.RST}{cui.C.GRY}{desc}{cui.C.RST}')
    
    # Готовность
    cui.ready_box([
        f'Сервер запущен на порту {args.port}',
        f'Судьи: http://<ip-ноутбука>:{args.port}',
        '',
        'Ctrl+C для остановки',
    ])
    
    cui.section('Журнал событий', cui.C.GRN)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        cui.warn('Остановка сервера...')
        poller.running = False
        server.shutdown()
        database.conn.close()
        cui.ok('Сервер остановлен')


if __name__ == '__main__':
    main()
