#!/usr/bin/env python3
"""
Скачивание тайлов карты для оффлайн-использования.

Запуск дома (с интернетом):
    python download_tiles.py --lat 49.45 --lon 11.08 --radius 30 --zoom 8-16
    python download_tiles.py --bbox 49.2,10.8,49.7,11.4 --zoom 8-16
    python download_tiles.py --gpx route.gpx --buffer 5 --zoom 8-16

Тайлы сохраняются в папку ./tiles/{z}/{x}/{y}.png
Сервер раздаёт их как /tiles/{z}/{x}/{y}.png
"""

import os
import sys
import math
import time
import argparse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import console_ui as cui
except ImportError:
    class _Stub:
        class C:
            RST=GRN=RED=YEL=CYN=WHT=GRY=BLU=MAG=BOLD=DIM=''
        def __getattr__(self, n):
            return lambda *a, **k: None
    cui = _Stub()

TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
HEADERS = {'User-Agent': 'TrophyRaid-TileDownloader/1.0 (offline race use)'}
TILE_DIR = Path('tiles')
MAX_WORKERS = 4          # OSM просит не больше 2-4 параллельных запросов
DELAY_PER_TILE = 0.05    # 50мс между запросами — уважаем сервер


def lat_lon_to_tile(lat, lon, zoom):
    """Координаты → номер тайла."""
    n = 2 ** zoom
    x = int((lon + 180) / 360 * n)
    lat_rad = math.radians(lat)
    y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)
    return x, y


def tiles_for_bbox(lat_min, lon_min, lat_max, lon_max, zoom):
    """Все тайлы покрывающие прямоугольник на заданном зуме."""
    x_min, y_max = lat_lon_to_tile(lat_min, lon_min, zoom)
    x_max, y_min = lat_lon_to_tile(lat_max, lon_max, zoom)
    tiles = []
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            tiles.append((zoom, x, y))
    return tiles


def bbox_from_center(lat, lon, radius_km):
    """Прямоугольник из центральной точки + радиус в км."""
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def bbox_from_gpx(gpx_path, buffer_km=5):
    """Прямоугольник из GPX-файла + буфер."""
    tree = ET.parse(gpx_path)
    root = tree.getroot()
    
    # Убираем namespace
    ns = ''
    if root.tag.startswith('{'):
        ns = root.tag.split('}')[0] + '}'
    
    lats, lons = [], []
    
    # wpt, trkpt, rtept
    for tag in ['wpt', 'trkpt', 'rtept']:
        for el in root.iter(f'{ns}{tag}'):
            lat = float(el.get('lat'))
            lon = float(el.get('lon'))
            lats.append(lat)
            lons.append(lon)
    
    if not lats:
        print('Ошибка: в GPX не найдены точки')
        sys.exit(1)
    
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    
    # Добавляем буфер
    dlat = buffer_km / 111.0
    dlon = buffer_km / (111.0 * math.cos(math.radians((lat_min + lat_max) / 2)))
    
    return lat_min - dlat, lon_min - dlon, lat_max + dlat, lon_max + dlon


def download_tile(z, x, y):
    """Скачать один тайл. Возвращает (success, cached, path)."""
    path = TILE_DIR / str(z) / str(x) / f'{y}.png'
    
    if path.exists():
        return True, True, str(path)
    
    path.parent.mkdir(parents=True, exist_ok=True)
    url = TILE_URL.format(z=z, x=x, y=y)
    
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            if len(data) < 100:
                return False, False, str(path)
            path.write_bytes(data)
        time.sleep(DELAY_PER_TILE)
        return True, False, str(path)
    except Exception as e:
        return False, False, f'{url}: {e}'


def estimate_tiles(bbox, zoom_range):
    """Подсчитать количество тайлов."""
    total = 0
    for z in zoom_range:
        tiles = tiles_for_bbox(*bbox, z)
        total += len(tiles)
    return total


def main():
    parser = argparse.ArgumentParser(
        description='Скачивание тайлов OSM для оффлайн-использования',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Примеры:
  %(prog)s --lat 49.45 --lon 11.08 --radius 30 --zoom 8-16
  %(prog)s --bbox 49.2,10.8,49.7,11.4 --zoom 8-16
  %(prog)s --gpx route.gpx --buffer 5 --zoom 8-16
  %(prog)s --lat 55.75 --lon 37.62 --radius 50 --zoom 8-15  # Москва 50км
        ''')
    
    parser.add_argument('--lat', type=float, help='Центр: широта')
    parser.add_argument('--lon', type=float, help='Центр: долгота')
    parser.add_argument('--radius', type=float, default=20, help='Радиус в км (по умолч. 20)')
    parser.add_argument('--bbox', help='Прямоугольник: lat_min,lon_min,lat_max,lon_max')
    parser.add_argument('--gpx', help='GPX файл (маршрут или точки)')
    parser.add_argument('--buffer', type=float, default=5, help='Буфер вокруг GPX в км (по умолч. 5)')
    parser.add_argument('--zoom', default='8-16', help='Диапазон зумов (по умолч. 8-16)')
    parser.add_argument('--output', default='tiles', help='Папка для тайлов (по умолч. ./tiles)')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS, help='Потоков (по умолч. 4)')
    parser.add_argument('--dry-run', action='store_true', help='Только подсчитать, не скачивать')
    
    args = parser.parse_args()
    
    global TILE_DIR
    TILE_DIR = Path(args.output)
    
    # Парсим зум
    if '-' in args.zoom:
        z_min, z_max = map(int, args.zoom.split('-'))
    else:
        z_min = z_max = int(args.zoom)
    zoom_range = range(z_min, z_max + 1)
    
    # Определяем bbox
    if args.gpx:
        bbox = bbox_from_gpx(args.gpx, args.buffer)
        cui.banner_tiles()
        cui.section('Источник: GPX файл')
        cui.kv('Файл', args.gpx)
        cui.kv('Буфер', f'{args.buffer} км')
    elif args.bbox:
        parts = list(map(float, args.bbox.split(',')))
        bbox = tuple(parts)
        cui.banner_tiles()
        cui.section('Источник: координаты')
        cui.kv('Bbox', str(bbox))
    elif args.lat and args.lon:
        bbox = bbox_from_center(args.lat, args.lon, args.radius)
        cui.banner_tiles()
        cui.section('Источник: центр + радиус')
        cui.kv('Центр', f'{args.lat}, {args.lon}')
        cui.kv('Радиус', f'{args.radius} км')
    else:
        parser.print_help()
        print('\nУкажите --lat/--lon, --bbox или --gpx')
        sys.exit(1)
    
    cui.kv('Область', f'{bbox[0]:.4f}, {bbox[1]:.4f} → {bbox[2]:.4f}, {bbox[3]:.4f}')
    cui.kv('Зумы', f'{z_min}–{z_max}')
    cui.kv('Папка', str(TILE_DIR))
    
    # Подсчёт
    all_tiles = []
    zoom_data = []
    cui.section('План загрузки')
    for z in zoom_range:
        tiles = tiles_for_bbox(*bbox, z)
        all_tiles.extend(tiles)
        size_mb = len(tiles) * 25 / 1024
        zoom_data.append((z, len(tiles), size_mb))
    
    total = len(all_tiles)
    total_mb = total * 25 / 1024
    cui.tiles_plan(zoom_data, total, total_mb)
    
    if args.dry_run:
        print()
        cui.info('Режим dry-run, скачивание не выполняется')
        return
    
    if total > 50000:
        print()
        cui.warn(f'Много тайлов ({total:,})')
        ans = input(f'  Продолжить? [y/N] ')
        if ans.lower() != 'y':
            cui.info('Отменено')
            return
    
    # Скачивание
    cui.section('Скачивание')
    
    downloaded = 0
    cached = 0
    errors = 0
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_tile, z, x, y): (z, x, y) for z, x, y in all_tiles}
        
        for i, future in enumerate(as_completed(futures)):
            ok, was_cached, info = future.result()
            if ok:
                if was_cached:
                    cached += 1
                else:
                    downloaded += 1
            else:
                errors += 1
                if errors <= 3:
                    cui.error(info)
            
            # Прогресс
            if (i + 1) % 50 == 0 or (i + 1) == total:
                elapsed = time.time() - start_time
                speed = downloaded / elapsed if elapsed > 0 else 0
                eta = int((total - i - 1) / speed) if speed > 0 else 0
                extra = f'{downloaded}↓ {cached}⊙ {errors}✕ {speed:.0f}/с ~{eta}с'
                cui.progress_bar(i + 1, total, prefix='', extra=extra)
    
    elapsed = time.time() - start_time
    size = sum(f.stat().st_size for f in TILE_DIR.rglob('*.png'))
    size_mb = size / 1024 / 1024
    
    cui.section('Результат')
    cui.tiles_result(downloaded, cached, errors, elapsed, size_mb)
    
    cui.ready_box([
        f'Тайлы сохранены в {TILE_DIR.resolve()}',
        f'Размер: {size_mb:.1f} МБ ({total:,} файлов)',
        '',
        'Скопируйте папку tiles/ рядом с server.py',
    ], cui.C.CYN)


if __name__ == '__main__':
    main()
