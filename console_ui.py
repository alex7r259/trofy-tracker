"""
Визуальное оформление консоли для Trophy Raid.
Цветной вывод, прогресс-бары, таблицы, баннеры.
Работает на Windows (cmd/PowerShell) и Linux/macOS.
"""

import sys
import os
import shutil

# ═══════════ ЦВЕТА ═══════════

# Включаем ANSI на Windows
if sys.platform == 'win32':
    os.system('')  # магия: активирует VT100 в cmd

class C:
    """ANSI цвета."""
    RST   = '\033[0m'
    BOLD  = '\033[1m'
    DIM   = '\033[2m'
    # Цвета
    BLK   = '\033[30m'
    RED   = '\033[91m'
    GRN   = '\033[92m'
    YEL   = '\033[93m'
    BLU   = '\033[94m'
    MAG   = '\033[95m'
    CYN   = '\033[96m'
    WHT   = '\033[97m'
    GRY   = '\033[90m'
    # Фоны
    BG_RED = '\033[41m'
    BG_GRN = '\033[42m'
    BG_YEL = '\033[43m'
    BG_BLU = '\033[44m'
    BG_MAG = '\033[45m'
    BG_CYN = '\033[46m'
    BG_GRY = '\033[100m'


def width():
    """Ширина терминала."""
    return shutil.get_terminal_size((80, 24)).columns


# ═══════════ БАННЕРЫ ═══════════

def banner_server():
    w = min(width(), 70)
    ln = '═' * w
    print()
    print(f'{C.GRN}{ln}{C.RST}')
    print(f'{C.GRN}║{C.RST}{C.BOLD}{C.WHT}  ▲ TROPHY RAID SERVER{C.RST}'.ljust(w + 15) + f'{C.GRN}║{C.RST}')
    print(f'{C.GRN}║{C.RST}{C.GRY}  Оффлайн-система трекинга для трофи-рейдов{C.RST}'.ljust(w + 15) + f'{C.GRN}║{C.RST}')
    print(f'{C.GRN}║{C.RST}{C.GRY}  LoRa Mesh · ESP32 · SQLite{C.RST}'.ljust(w + 15) + f'{C.GRN}║{C.RST}')
    print(f'{C.GRN}{ln}{C.RST}')
    print()


def banner_tiles():
    w = min(width(), 70)
    ln = '═' * w
    print()
    print(f'{C.CYN}{ln}{C.RST}')
    print(f'{C.CYN}║{C.RST}{C.BOLD}{C.WHT}  🗺  TROPHY RAID — ЗАГРУЗКА КАРТЫ{C.RST}'.ljust(w + 15) + f'{C.CYN}║{C.RST}')
    print(f'{C.CYN}║{C.RST}{C.GRY}  Скачивание тайлов OpenStreetMap для оффлайн-использования{C.RST}'.ljust(w + 15) + f'{C.CYN}║{C.RST}')
    print(f'{C.CYN}{ln}{C.RST}')
    print()


# ═══════════ СЕКЦИИ ═══════════

def section(title, color=C.CYN):
    """Заголовок секции."""
    w = min(width(), 70)
    print(f'\n{color}{"─" * w}{C.RST}')
    print(f'{color}{C.BOLD}  {title}{C.RST}')
    print(f'{color}{"─" * w}{C.RST}')


def subsection(title):
    print(f'\n  {C.GRY}▸ {C.WHT}{title}{C.RST}')


# ═══════════ КЛЮЧ-ЗНАЧЕНИЕ ═══════════

def kv(key, value, color=C.GRN, indent=4):
    """Вывод пары ключ: значение."""
    pad = ' ' * indent
    print(f'{pad}{C.GRY}{key}:{C.RST} {color}{value}{C.RST}')


def kv_status(key, ok, ok_text='ОК', err_text='ОШИБКА', indent=4):
    """Ключ со статусом ОК/ОШИБКА."""
    pad = ' ' * indent
    if ok:
        print(f'{pad}{C.GRY}{key}:{C.RST} {C.GRN}● {ok_text}{C.RST}')
    else:
        print(f'{pad}{C.GRY}{key}:{C.RST} {C.RED}● {err_text}{C.RST}')


# ═══════════ ТАБЛИЦЫ ═══════════

def table_header(columns, widths, color=C.CYN):
    """Заголовок таблицы."""
    row = ''
    for col, w in zip(columns, widths):
        row += f'{col:<{w}}'
    print(f'    {color}{C.BOLD}{row}{C.RST}')
    print(f'    {C.GRY}{"─" * sum(widths)}{C.RST}')


def table_row(values, widths, colors=None):
    """Строка таблицы."""
    row = '    '
    for i, (val, w) in enumerate(zip(values, widths)):
        c = colors[i] if colors else C.WHT
        row += f'{c}{str(val):<{w}}{C.RST}'
    print(row)


# ═══════════ ПРОГРЕСС-БАР ═══════════

def progress_bar(current, total, width_chars=30, prefix='', extra=''):
    """Прогресс-бар с процентами."""
    if total == 0:
        return
    pct = current / total
    filled = int(width_chars * pct)
    bar_fill = '█' * filled
    bar_empty = '░' * (width_chars - filled)
    
    # Цвет по прогрессу
    if pct < 0.3:
        color = C.RED
    elif pct < 0.7:
        color = C.YEL
    else:
        color = C.GRN
    
    line = f'\r    {prefix}{color}{bar_fill}{C.GRY}{bar_empty}{C.RST} {C.WHT}{pct*100:5.1f}%{C.RST} {C.GRY}{extra}{C.RST}'
    
    # Обрезаем если шире терминала
    sys.stdout.write(line[:width() - 1])
    sys.stdout.flush()
    
    if current >= total:
        print()  # новая строка в конце


# ═══════════ СООБЩЕНИЯ ═══════════

def info(msg):
    print(f'  {C.CYN}ℹ{C.RST} {msg}')

def ok(msg):
    print(f'  {C.GRN}✓{C.RST} {msg}')

def warn(msg):
    print(f'  {C.YEL}⚠{C.RST} {msg}')

def error(msg):
    print(f'  {C.RED}✕{C.RST} {msg}')

def event(msg, color=C.GRN):
    """Событие (CP hit, новый узел и т.д.)"""
    print(f'  {color}▶{C.RST} {msg}')


# ═══════════ ЛОГГЕР ═══════════

def log_line(level, msg):
    """Строка лога с временем и цветным уровнем."""
    from datetime import datetime
    ts = datetime.now().strftime('%H:%M:%S')
    
    colors = {
        'INFO': C.CYN,
        'WARN': C.YEL,
        'ERR':  C.RED,
        'OK':   C.GRN,
        'RX':   C.BLU,
        'TX':   C.MAG,
        'CP':   C.GRN,
    }
    c = colors.get(level, C.GRY)
    print(f'  {C.GRY}{ts}{C.RST} {c}{level:>4}{C.RST} {msg}')


# ═══════════ СПЕЦИАЛЬНЫЕ ДЛЯ СЕРВЕРА ═══════════

def server_status(gateway_url, connected, poll_count, nodes_count, tracks_count, tiles_ok, db_path):
    """Блок статуса сервера."""
    kv_status('Шлюз', connected, f'подключён ({gateway_url})', f'нет связи ({gateway_url})')
    kv('Опросов', str(poll_count), C.WHT)
    kv('Узлов', str(nodes_count), C.GRN if nodes_count > 0 else C.GRY)
    kv('Точек трека', str(tracks_count), C.WHT)
    kv_status('Тайлы', tiles_ok, 'доступны', 'не найдены')
    kv('БД', db_path, C.GRY)


def node_table(nodes):
    """Таблица узлов в консоли."""
    if not nodes:
        print(f'    {C.GRY}(нет узлов){C.RST}')
        return
    
    table_header(['ID', 'Координаты', 'Выс.', 'Скор.', 'Бат.', 'Возраст'],
                 [6, 28, 7, 8, 6, 10])
    
    for n in nodes:
        age_s = n.get('age', 999999) / 1000
        if age_s > 120:
            age_c = C.RED
        elif age_s > 60:
            age_c = C.YEL
        else:
            age_c = C.GRN
        
        age_str = f'{int(age_s)}с' if age_s < 60 else f'{int(age_s/60)}м{int(age_s%60):02d}с'
        bat = n.get('battery', 0)
        bat_c = C.RED if bat < 20 else C.GRN
        
        table_row(
            [f'#{n["id"]}',
             f'{n.get("lat", 0):.6f}, {n.get("lon", 0):.6f}',
             f'{n.get("alt", 0)}м',
             f'{n.get("speed", 0)}км/ч',
             f'{bat}%',
             age_str],
            [6, 28, 7, 8, 6, 10],
            [C.CYN, C.GRN, C.MAG, C.BLU, bat_c, age_c]
        )


# ═══════════ СПЕЦИАЛЬНЫЕ ДЛЯ ТАЙЛОВ ═══════════

def tiles_plan(zoom_data, total, total_mb):
    """Таблица плана загрузки тайлов."""
    table_header(['Зум', 'Тайлов', '~Размер'],
                 [8, 12, 12])
    
    for z, count, mb in zoom_data:
        table_row(
            [f'z={z}', f'{count:,}', f'{mb:.0f} МБ'],
            [8, 12, 12],
            [C.CYN, C.WHT, C.GRY]
        )
    
    print(f'    {C.GRY}{"─" * 32}{C.RST}')
    print(f'    {C.BOLD}{C.WHT}Итого: {total:,} тайлов (~{total_mb:.0f} МБ){C.RST}')


def tiles_result(downloaded, cached, errors, elapsed, total_size_mb):
    """Результаты загрузки."""
    print()
    kv('Скачано', f'{downloaded:,}', C.GRN)
    kv('Из кэша', f'{cached:,}', C.CYN)
    if errors:
        kv('Ошибок', f'{errors:,}', C.RED)
    kv('Время', f'{elapsed:.0f} сек', C.WHT)
    kv('Размер', f'{total_size_mb:.1f} МБ', C.WHT)


# ═══════════ ФИНАЛЬНЫЙ БЛОК ═══════════

def ready_box(lines, color=C.GRN):
    """Блок готовности с рамкой."""
    w = max(len(l) for l in lines) + 4
    w = max(w, 40)
    print()
    print(f'  {color}╔{"═" * w}╗{C.RST}')
    for line in lines:
        padding = w - len(line) - 2
        print(f'  {color}║{C.RST} {C.BOLD}{C.WHT}{line}{C.RST}{" " * padding}{color}║{C.RST}')
    print(f'  {color}╚{"═" * w}╝{C.RST}')
    print()
