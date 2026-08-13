import customtkinter
import tkinter as tk
from tkinter import messagebox
import json
import os
import threading
import time
import mss
import keyboard
import pynput
from pynput.keyboard import Key, Controller as KeyboardController
from pynput.mouse import Button, Controller as MouseController, Listener as MouseListener
import win32gui
import win32con
import win32api
import win32process
import ctypes
from ctypes import wintypes
import colorsys

try:
    import psutil
except ImportError:
    psutil = None

try:
    import win32ui
except ImportError:
    win32ui = None

# --- Глобальные переменные и настройки --- #
CONFIG_FILE = "config.json"

# Состояния свапа
STATE_IDLE = 0
STATE_CASTING_SING = 1  # Нажали скилл, ждем появления ромба (возможно, надели пение)
STATE_CASTING_PA = 2    # Ромб появился, ждем конца свапа (наденем ПА)

# Минимальное время нахождения в ПА перед возвратом в ПЗ (сек),
# чтобы не "отстреливать" обратно слишком рано на быстрых скиллах,
# но и не задерживать возврат.
MIN_PA_DURATION = 0.015

# Дополнительная задержка перед возвратом в ПЗ после того,
# как зафиксирован конец каста по полоске (сек).
PZ_RETURN_EXTRA_DELAY = 0.05 # Уменьшено, так как ожидание каста убрано

# Анти-дребезг для проверки иконки ПЗ в IDLE:
# Требуем несколько подряд "НЕ ПЗ" перед автосвапом,
# и ставим кулдаун, чтобы не дёргаться туда-сюда при шуме.
PZ_ICON_NOT_MATCH_FRAMES_REQUIRED = 15 # Увеличено для стабильности
PZ_AUTO_SWAP_COOLDOWN = 1.0 # Увеличено для предотвращения частых пересвапов

# Таймаут ожидания ромба после нажатия скилла (сек) — аварийный возврат в ПЗ
CAST_DIAMOND_TIMEOUT = 1.0

# Debounce физических атакующих клавиш (сек)
ATTACK_KEY_DEBOUNCE = 0.1

# Кадров подряд для подтверждения конца каста
CAST_END_FRAMES_REQUIRED = 2

# Интервал DEBUG-лога при поиске ромба (сек)
CAST_DEBUG_INTERVAL = 0.2

# Радиус fuzzy-поиска ромба: 2 → квадрат 5×5
FUZZY_CAST_RADIUS = 2

# DEBUG и таймаут для состояния CASTING_PA
CAST_PA_DEBUG_INTERVAL = 0.1
CAST_PA_MAX_DURATION = 2.5
CAST_BAR_DIFF_RATIO = 0.15

# --- Вспомогательные функции для работы с системой (Windows) --- #
PW_PROCESS_NAMES = frozenset({
    "elementclient.exe",
    "clientx64.exe",
})
PW_TITLE_KEYWORDS = (
    "perfect world",
    "pvpclassic",
)


def _get_process_exe_basename(hwnd):
    """Имя exe-файла процесса окна (например, clientx64.exe)."""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if psutil is not None:
            return psutil.Process(pid).name().lower()
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _is_perfect_world_window(hwnd, title=None):
    if title is None:
        title = win32gui.GetWindowText(hwnd)
    exe_name = _get_process_exe_basename(hwnd)
    if exe_name in PW_PROCESS_NAMES:
        return True
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in PW_TITLE_KEYWORDS)


def _colorref_to_rgb(color_ref):
    return (color_ref & 0xFF, (color_ref >> 8) & 0xFF, (color_ref >> 16) & 0xFF)


def get_pixel_color_gdi(hwnd, screen_x, screen_y):
    """
    Чтение цвета через GDI GetPixel.
    screen_x, screen_y — экранные координаты клика.
    GetDC + ScreenToClient для клиентской области; GetWindowDC — запасной путь.
    """
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return (0, 0, 0)

        client_x, client_y = win32gui.ScreenToClient(hwnd, (int(screen_x), int(screen_y)))

        hdc = win32gui.GetDC(hwnd)
        if hdc:
            try:
                color_ref = win32gui.GetPixel(hdc, client_x, client_y)
                if color_ref != -1:
                    return _colorref_to_rgb(color_ref)
            finally:
                win32gui.ReleaseDC(hwnd, hdc)

        left, top, _, _ = win32gui.GetWindowRect(hwnd)
        window_x = int(screen_x - left)
        window_y = int(screen_y - top)
        hdc = win32gui.GetWindowDC(hwnd)
        if hdc:
            try:
                color_ref = win32gui.GetPixel(hdc, window_x, window_y)
                if color_ref != -1:
                    return _colorref_to_rgb(color_ref)
            finally:
                win32gui.ReleaseDC(hwnd, hdc)
    except Exception:
        pass
    return (0, 0, 0)


def get_window_list_windows():
    windows = []
    def winEnumHandler(hwnd, ctx):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        if not _is_perfect_world_window(hwnd, title):
            return

        rect = win32gui.GetWindowRect(hwnd)
        x, y, x2, y2 = rect
        width = x2 - x
        height = y2 - y

        client_rect = win32gui.GetClientRect(hwnd)
        client_width = client_rect[2] - client_rect[0]
        client_height = client_rect[3] - client_rect[1]

        if width > 0 and height > 0 and client_width > 0 and client_height > 0:
            windows.append({
                "hwnd": hwnd,
                "name": title,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "client_width": client_width,
                "client_height": client_height,
            })
    win32gui.EnumWindows(winEnumHandler, None)
    return windows

def get_window_current_geometry_windows(hwnd):
    try:
        client_x, client_y = win32gui.ClientToScreen(hwnd, (0, 0))
        client_rect = win32gui.GetClientRect(hwnd)
        client_width = client_rect[2] - client_rect[0]
        client_height = client_rect[3] - client_rect[1]

        return client_x, client_y, client_width, client_height
    except Exception:
        return None, None, None, None


def client_relative_to_screen(hwnd, relative_x, relative_y):
    """Клиентские координаты → экранные (без смещения от рамки/заголовка)."""
    return win32gui.ClientToScreen(hwnd, (int(relative_x), int(relative_y)))

def compare_colors(color1, color2, tolerance=None):
    """Сравнение двух цветов RGB/BGR с допуском по каждому каналу."""
    if tolerance is None:
        tolerance = 25 # Увеличена базовая толерантность
    if not (isinstance(color1, (tuple, list)) and len(color1) == 3 and
            isinstance(color2, (tuple, list)) and len(color2) == 3):
        return False

    r1, g1, b1 = int(color1[0]), int(color1[1]), int(color1[2])
    r2, g2, b2 = int(color2[0]), int(color2[1]), int(color2[2])

    return (abs(r1 - r2) <= tolerance and
            abs(g1 - g2) <= tolerance and
            abs(b1 - b2) <= tolerance)


def compare_colors_hsv(color1, color2, h_tol=0.08, s_tol=0.4, v_tol=0.6):
    """Сравнение цветов в HSV, более устойчивое к изменению яркости/локации."""
    if not (isinstance(color1, (tuple, list)) and len(color1) == 3 and
            isinstance(color2, (tuple, list)) and len(color2) == 3):
        return False

    r1, g1, b1 = [int(x) / 255.0 for x in color1]
    r2, g2, b2 = [int(x) / 255.0 for x in color2]

    h1, s1, v1 = colorsys.rgb_to_hsv(r1, g1, b1)
    h2, s2, v2 = colorsys.rgb_to_hsv(r2, g2, b2)

    dh = min(abs(h1 - h2), 1.0 - abs(h1 - h2))

    return (dh <= h_tol and
            abs(s1 - s2) <= s_tol and
            abs(v1 - v2) <= v_tol)


def is_cast_color_match(current_color, ref_color):
    """Распознавание ромба/полоски с максимально мягкими HSV-допусками."""
    if not (isinstance(current_color, (tuple, list)) and len(current_color) == 3 and
            isinstance(ref_color, (tuple, list)) and len(ref_color) == 3):
        return False

    if compare_colors_hsv(current_color, ref_color, h_tol=0.25, s_tol=0.9, v_tol=0.9):
        return True

    return compare_colors(current_color, ref_color, tolerance=80)


def is_bar_color_similar(current_color, ref_color, max_diff_ratio=CAST_BAR_DIFF_RATIO):
    """
    Полоска каста «видна», если RGB близок к калиброванному (в пределах max_diff_ratio).
    Конец каста = цвет перестал быть похожим (>15% по каналам).
    """
    if not (isinstance(current_color, (tuple, list)) and len(current_color) == 3 and
            isinstance(ref_color, (tuple, list)) and len(ref_color) == 3):
        return False

    channel_limit = max(int(255 * max_diff_ratio), 10)
    for c1, c2 in zip(current_color[:3], ref_color[:3]):
        if abs(int(c1) - int(c2)) > channel_limit:
            return False
    return True


def _get_pixel_color_from_sct(sct, absolute_x, absolute_y):
    """Быстрое чтение цвета одного пикселя через уже созданный mss()."""
    monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

    left = monitor["left"]
    top = monitor["top"]
    right = left + monitor["width"]
    bottom = top + monitor["height"]

    if not (left <= absolute_x < right and top <= absolute_y < bottom):
        return (0, 0, 0)

    sct_img = sct.grab({"left": absolute_x, "top": absolute_y, "width": 1, "height": 1})
    pixel = sct_img.pixel(0, 0)
    return (pixel[2], pixel[1], pixel[0])  # (R, G, B)


def _get_pixel_color_avg_from_sct(sct, absolute_x, absolute_y, radius=1):
    """Усреднение цвета в небольшом квадратике вокруг точки для более стабильного цвета."""
    monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

    left_m = monitor["left"]
    top_m = monitor["top"]
    right_m = left_m + monitor["width"]
    bottom_m = top_m + monitor["height"]

    left = max(absolute_x - radius, left_m)
    top = max(absolute_y - radius, top_m)
    right = min(absolute_x + radius, right_m - 1)
    bottom = min(absolute_y + radius, bottom_m - 1)

    if left > right or top > bottom:
        return (0, 0, 0)

    width = right - left + 1
    height = bottom - top + 1

    sct_img = sct.grab({"left": left, "top": top, "width": width, "height": height})

    total_r = total_g = total_b = 0
    count = 0
    for yy in range(height):
        for xx in range(width):
            pixel = sct_img.pixel(xx, yy)
            if len(pixel) == 4:
                b, g, r, _ = pixel
            elif len(pixel) == 3:
                b, g, r = pixel
            else:
                continue
            total_r += r
            total_g += g
            total_b += b
            count += 1

    if count == 0:
        return (0, 0, 0)

    return (total_r // count, total_g // count, total_b // count)


CALIBRATION_WIDGETS = {
    "cast_detection_pixel": ("cast_pixel_info_label", "cast_pixel_color_frame"),
    "end_swap_pixel": ("end_swap_pixel_info_label", "end_swap_pixel_color_frame"),
    "pz_set_icon_pixel": ("pz_pixel_info_label", "pz_pixel_color_frame"),
}


def _is_pixel_calibrated(pixel_data):
    """Пиксель считается откалиброванным; чёрный (0, 0, 0) — допустимый цвет."""
    if not pixel_data:
        return False
    if pixel_data.get("relative_x") is None or pixel_data.get("relative_y") is None:
        return False
    color = pixel_data.get("color")
    if color is None or len(color) != 3:
        return False
    if pixel_data.get("calibrated"):
        return True
    rx, ry = pixel_data.get("relative_x", 0), pixel_data.get("relative_y", 0)
    return rx != 0 or ry != 0


def _bgr_pixel_to_rgb(pixel):
    if len(pixel) == 4:
        b, g, r, _ = pixel
    elif len(pixel) == 3:
        b, g, r = pixel
    else:
        return None
    return (r, g, b)


def _read_rgb_from_sct_img(sct_img, grab_left, grab_top, absolute_x, absolute_y):
    xx = absolute_x - grab_left
    yy = absolute_y - grab_top
    if xx < 0 or yy < 0 or xx >= sct_img.width or yy >= sct_img.height:
        return (0, 0, 0)
    rgb = _bgr_pixel_to_rgb(sct_img.pixel(xx, yy))
    return rgb if rgb else (0, 0, 0)


def _read_rgb_avg_from_sct_img(sct_img, grab_left, grab_top, absolute_x, absolute_y, radius=1):
    total_r = total_g = total_b = 0
    count = 0
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            xx = absolute_x + dx - grab_left
            yy = absolute_y + dy - grab_top
            if 0 <= xx < sct_img.width and 0 <= yy < sct_img.height:
                rgb = _bgr_pixel_to_rgb(sct_img.pixel(xx, yy))
                if rgb:
                    total_r += rgb[0]
                    total_g += rgb[1]
                    total_b += rgb[2]
                    count += 1
    if count == 0:
        return (0, 0, 0)
    return (total_r // count, total_g // count, total_b // count)


def _grab_pixel_colors_batch(sct, samples):
    """
    Захват минимальной области, покрывающей все точки, за один sct.grab().
    samples: список (key, abs_x, abs_y, radius); radius=0 — один пиксель.
    """
    if not samples:
        return {}

    monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
    left_m = monitor["left"]
    top_m = monitor["top"]
    right_m = left_m + monitor["width"]
    bottom_m = top_m + monitor["height"]

    min_x = min(ax - radius for _, ax, _, radius in samples)
    min_y = min(ay - radius for _, _, ay, radius in samples)
    max_x = max(ax + radius for _, ax, _, radius in samples)
    max_y = max(ay + radius for _, _, ay, radius in samples)

    grab_left = max(int(min_x), left_m)
    grab_top = max(int(min_y), top_m)
    grab_right = min(int(max_x), right_m - 1)
    grab_bottom = min(int(max_y), bottom_m - 1)

    if grab_left > grab_right or grab_top > grab_bottom:
        return {key: (0, 0, 0) for key, _, _, _ in samples}

    width = grab_right - grab_left + 1
    height = grab_bottom - grab_top + 1
    sct_img = sct.grab({"left": grab_left, "top": grab_top, "width": width, "height": height})

    result = {}
    for key, abs_x, abs_y, radius in samples:
        if radius <= 0:
            result[key] = _read_rgb_from_sct_img(sct_img, grab_left, grab_top, abs_x, abs_y)
        else:
            result[key] = _read_rgb_avg_from_sct_img(sct_img, grab_left, grab_top, abs_x, abs_y, radius)
    return result


def _read_rgb_from_bitmap_bits(bmp_bits, width, height, local_x, local_y):
    stride = width * 4
    if not (0 <= local_x < width and 0 <= local_y < height):
        return (0, 0, 0)
    offset = local_y * stride + local_x * 4
    b, g, r = bmp_bits[offset], bmp_bits[offset + 1], bmp_bits[offset + 2]
    return (r, g, b)


def _read_rgb_avg_from_bitmap_bits(bmp_bits, width, height, center_x, center_y, radius=1):
    total_r = total_g = total_b = 0
    count = 0
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            rgb = _read_rgb_from_bitmap_bits(bmp_bits, width, height, center_x + dx, center_y + dy)
            total_r += rgb[0]
            total_g += rgb[1]
            total_b += rgb[2]
            count += 1
    if count == 0:
        return (0, 0, 0)
    return (total_r // count, total_g // count, total_b // count)


def _grab_pixel_colors_bitblt_client(hwnd, samples):
    """
    Захват через BitBlt по координатам клиентской области (GetClientRect / GetDC).
    samples: список (key, client_rel_x, client_rel_y, radius); radius=0 — один пиксель.
    """
    if not samples or not hwnd or not win32gui.IsWindow(hwnd):
        return {key: (0, 0, 0) for key, _, _, _ in samples}
    if win32ui is None:
        return _grab_pixel_colors_gdi_fallback_client(hwnd, samples)

    client_samples = [(key, int(rx), int(ry), radius) for key, rx, ry, radius in samples]

    min_cx = min(cx - radius for _, cx, _, radius in client_samples)
    min_cy = min(cy - radius for _, cx, _, radius in client_samples)
    max_cx = max(cx + radius for _, cx, _, radius in client_samples)
    max_cy = max(cy + radius for _, cx, _, radius in client_samples)

    client_rect = win32gui.GetClientRect(hwnd)
    client_w = client_rect[2] - client_rect[0]
    client_h = client_rect[3] - client_rect[1]

    min_cx = max(min_cx, 0)
    min_cy = max(min_cy, 0)
    max_cx = min(max_cx, client_w - 1)
    max_cy = min(max_cy, client_h - 1)

    width = max_cx - min_cx + 1
    height = max_cy - min_cy + 1
    if width <= 0 or height <= 0:
        return {key: (0, 0, 0) for key, _, _, _ in samples}

    hwnd_dc = win32gui.GetDC(hwnd)
    mfc_dc = save_dc = bitmap = None
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        save_dc.BitBlt((0, 0), (width, height), mfc_dc, (min_cx, min_cy), win32con.SRCCOPY)
        bmp_bits = bitmap.GetBitmapBits(True)

        result = {}
        for key, client_x, client_y, radius in client_samples:
            local_x = client_x - min_cx
            local_y = client_y - min_cy
            if radius <= 0:
                result[key] = _read_rgb_from_bitmap_bits(bmp_bits, width, height, local_x, local_y)
            else:
                result[key] = _read_rgb_avg_from_bitmap_bits(
                    bmp_bits, width, height, local_x, local_y, radius
                )
        return result
    except Exception:
        return _grab_pixel_colors_gdi_fallback_client(hwnd, samples)
    finally:
        if save_dc is not None:
            save_dc.DeleteDC()
        if mfc_dc is not None:
            mfc_dc.DeleteDC()
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if hwnd_dc:
            win32gui.ReleaseDC(hwnd, hwnd_dc)


def _fuzzy_color_match_client(hwnd, client_rel_x, client_rel_y, ref_color, match_fn, search_radius=1):
    """
    Проверяет область (2*search_radius+1)² вокруг точки в клиентских координатах.
    Возвращает (matched: bool, seen_color: tuple) — seen_color с центра или первого совпадения.
    """
    if not hwnd or not win32gui.IsWindow(hwnd):
        return False, (0, 0, 0)
    if win32ui is None:
        center_sx, center_sy = client_relative_to_screen(hwnd, client_rel_x, client_rel_y)
        for dy in range(-search_radius, search_radius + 1):
            for dx in range(-search_radius, search_radius + 1):
                rgb = get_pixel_color_gdi(hwnd, center_sx + dx, center_sy + dy)
                if match_fn(rgb, ref_color):
                    return True, rgb
        return False, get_pixel_color_gdi(hwnd, center_sx, center_sy)

    client_x = int(client_rel_x)
    client_y = int(client_rel_y)
    client_rect = win32gui.GetClientRect(hwnd)
    client_w = client_rect[2] - client_rect[0]
    client_h = client_rect[3] - client_rect[1]

    min_cx = max(client_x - search_radius, 0)
    min_cy = max(client_y - search_radius, 0)
    max_cx = min(client_x + search_radius, client_w - 1)
    max_cy = min(client_y + search_radius, client_h - 1)
    width = max_cx - min_cx + 1
    height = max_cy - min_cy + 1
    if width <= 0 or height <= 0:
        return False, (0, 0, 0)

    hwnd_dc = win32gui.GetDC(hwnd)
    mfc_dc = save_dc = bitmap = None
    center_color = (0, 0, 0)
    try:
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        save_dc.BitBlt((0, 0), (width, height), mfc_dc, (min_cx, min_cy), win32con.SRCCOPY)
        bmp_bits = bitmap.GetBitmapBits(True)

        center_local_x = client_x - min_cx
        center_local_y = client_y - min_cy
        center_color = _read_rgb_from_bitmap_bits(
            bmp_bits, width, height, center_local_x, center_local_y
        )

        for dy in range(-search_radius, search_radius + 1):
            for dx in range(-search_radius, search_radius + 1):
                lx = center_local_x + dx
                ly = center_local_y + dy
                rgb = _read_rgb_from_bitmap_bits(bmp_bits, width, height, lx, ly)
                if match_fn(rgb, ref_color):
                    return True, rgb
        return False, center_color
    except Exception:
        center_sx, center_sy = client_relative_to_screen(hwnd, client_rel_x, client_rel_y)
        return False, get_pixel_color_gdi(hwnd, center_sx, center_sy)
    finally:
        if save_dc is not None:
            save_dc.DeleteDC()
        if mfc_dc is not None:
            mfc_dc.DeleteDC()
        if bitmap is not None:
            win32gui.DeleteObject(bitmap.GetHandle())
        if hwnd_dc:
            win32gui.ReleaseDC(hwnd, hwnd_dc)


def _grab_pixel_colors_gdi_fallback_client(hwnd, samples):
    """Поштучный GetPixel по клиентским координатам."""
    result = {}
    for key, rel_x, rel_y, radius in samples:
        screen_x, screen_y = client_relative_to_screen(hwnd, rel_x, rel_y)
        if radius <= 0:
            result[key] = get_pixel_color_gdi(hwnd, screen_x, screen_y)
            continue
        total_r = total_g = total_b = 0
        count = 0
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                rgb = get_pixel_color_gdi(hwnd, screen_x + dx, screen_y + dy)
                total_r += rgb[0]
                total_g += rgb[1]
                total_b += rgb[2]
                count += 1
        result[key] = (total_r // count, total_g // count, total_b // count)
    return result


def _grab_pixel_colors_bitblt(hwnd, samples):
    """Обёртка: экранные координаты → клиентские → BitBlt."""
    if not hwnd:
        return {key: (0, 0, 0) for key, _, _, _ in samples}
    client_samples = []
    for key, screen_x, screen_y, radius in samples:
        client_x, client_y = win32gui.ScreenToClient(hwnd, (int(screen_x), int(screen_y)))
        client_samples.append((key, client_x, client_y, radius))
    return _grab_pixel_colors_bitblt_client(hwnd, client_samples)


def _grab_pixel_colors_gdi_fallback(hwnd, samples):
    """Поштучный GetPixel, если BitBlt недоступен."""
    result = {}
    for key, screen_x, screen_y, radius in samples:
        if radius <= 0:
            result[key] = get_pixel_color_gdi(hwnd, screen_x, screen_y)
            continue
        total_r = total_g = total_b = 0
        count = 0
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                rgb = get_pixel_color_gdi(hwnd, screen_x + dx, screen_y + dy)
                total_r += rgb[0]
                total_g += rgb[1]
                total_b += rgb[2]
                count += 1
        result[key] = (total_r // count, total_g // count, total_b // count)
    return result


def get_pixel_color_at_screen(absolute_x, absolute_y, hwnd=None):
    """
    Единый путь чтения цвета: GDI GetPixel (калибровка и цикл используют одну функцию).
    """
    if hwnd:
        return get_pixel_color_gdi(hwnd, absolute_x, absolute_y)
    with mss.mss() as sct:
        return _get_pixel_color_from_sct(sct, absolute_x, absolute_y)


def get_pixel_color_client(hwnd, relative_x, relative_y):
    """Цвет в клиентских координатах — тот же путь, что при калибровке клика."""
    screen_x, screen_y = client_relative_to_screen(hwnd, relative_x, relative_y)
    return get_pixel_color_at_screen(screen_x, screen_y, hwnd=hwnd)


def get_pixel_color_client_avg(hwnd, relative_x, relative_y, radius=1):
    """Усреднение через get_pixel_color_client (тот же GetPixel, что при калибровке)."""
    total_r = total_g = total_b = 0
    count = 0
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            rgb = get_pixel_color_client(hwnd, relative_x + dx, relative_y + dy)
            total_r += rgb[0]
            total_g += rgb[1]
            total_b += rgb[2]
            count += 1
    if count == 0:
        return (0, 0, 0)
    return (total_r // count, total_g // count, total_b // count)


def fuzzy_match_in_client_area(hwnd, relative_x, relative_y, ref_color, match_fn, radius=2):
    """
    Fuzzy-поиск в квадрате (2*radius+1)² через get_pixel_color_client.
    Возвращает (matched, seen_color) — seen_color с центра или первого совпадения.
    """
    center_rgb = get_pixel_color_client(hwnd, relative_x, relative_y)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            rgb = get_pixel_color_client(hwnd, relative_x + dx, relative_y + dy)
            if match_fn(rgb, ref_color):
                return True, rgb
    return False, center_rgb

VK_CODE = {
    # ... (оставлен без изменений, так как он корректен)
    'backspace': 0x08,
    'tab': 0x09,
    'clear': 0x0C,
    'enter': 0x0D,
    'shift': 0x10,
    'ctrl': 0x11,
    'alt': 0x12,
    'pause': 0x13,
    'caps_lock': 0x14,
    'esc': 0x1B,
    'space': 0x20,
    'page_up': 0x21,
    'page_down': 0x22,
    'end': 0x23,
    'home': 0x24,
    'left': 0x25,
    'up': 0x26,
    'right': 0x27,
    'down': 0x28,
    'select': 0x29,
    'print': 0x2A,
    'execute': 0x2B,
    'print_screen': 0x2C,
    'insert': 0x2D,
    'delete': 0x2E,
    'help': 0x2F,
    '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34,
    '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39,
    'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45, 'f': 0x46, 'g': 0x47,
    'h': 0x48, 'i': 0x49, 'j': 0x4A, 'k': 0x4B, 'l': 0x4C, 'm': 0x4D, 'n': 0x4E,
    'o': 0x4F, 'p': 0x50, 'q': 0x51, 'r': 0x52, 's': 0x53, 't': 0x54, 'u': 0x55,
    'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59, 'z': 0x5A,
    'numpad_0': 0x60, 'numpad_1': 0x61, 'numpad_2': 0x62, 'numpad_3': 0x63,
    'numpad_4': 0x64, 'numpad_5': 0x65, 'numpad_6': 0x66, 'numpad_7': 0x67,
    'numpad_8': 0x68, 'numpad_9': 0x69,
    'multiply': 0x6A, 'add': 0x6B, 'separator': 0x6C, 'subtract': 0x6D,
    'decimal': 0x6E, 'divide': 0x6F,
    'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74, 'f6': 0x75,
    'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
    'num_lock': 0x90, 'scroll_lock': 0x91,
    'left_shift': 0xA0, 'right_shift': 0xA1, 'left_control': 0xA2, 'right_control': 0xA3,
    'left_menu': 0xA4, 'right_menu': 0xA5
}

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
EXTENDED_VK = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x2D, 0x2E, 0x6F, 0x90, 0x91, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,
}
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class INPUTunion(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", INPUTunion))


_key_injection_depth = 0
_key_injection_lock = threading.Lock()


def _is_key_injection_active():
    with _key_injection_lock:
        return _key_injection_depth > 0


def _resolve_vk_code(key_name):
    key_name_lower = key_name.lower()
    vk_code = VK_CODE.get(key_name_lower)

    if vk_code is None:
        if len(key_name) == 1 and 'a' <= key_name_lower <= 'z':
            vk_code = ord(key_name.upper())
        elif len(key_name) == 1 and '0' <= key_name_lower <= '9':
            vk_code = ord(key_name)

    return vk_code


def _focus_game_window(hwnd):
    if not hwnd or not win32gui.IsWindow(hwnd):
        return
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        if win32gui.GetForegroundWindow() != hwnd:
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.03)
    except Exception:
        pass


def _send_vk_input(vk_code, key_up=False):
    flags = KEYEVENTF_KEYUP if key_up else 0
    if vk_code in EXTENDED_VK:
        flags |= KEYEVENTF_EXTENDEDKEY

    inp = INPUT(type=INPUT_KEYBOARD)
    inp.union.ki = KEYBDINPUT(
        wVk=vk_code,
        wScan=0,
        dwFlags=flags,
        time=0,
        dwExtraInfo=0,
    )
    return ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


def press_key_system(hwnd, key_name):
    global _key_injection_depth
    vk_code = _resolve_vk_code(key_name)
    if vk_code is None:
        print(f"Неизвестная клавиша для нажатия: {key_name}")
        return

    with _key_injection_lock:
        _key_injection_depth += 1
    try:
        _focus_game_window(hwnd)
        _send_vk_input(vk_code, key_up=False)
        time.sleep(0.01)
        _send_vk_input(vk_code, key_up=True)
    finally:
        with _key_injection_lock:
            _key_injection_depth -= 1


def _format_attack_binding_line(physical_key, binding_info):
    if isinstance(binding_info, dict):
        game_key = binding_info.get("game_key", physical_key)
        is_double = binding_info.get("double_click", False)
    else:
        game_key = str(binding_info)
        is_double = False
    suffix = " (double)" if is_double else ""
    return f"{physical_key} -> {game_key}{suffix}"


class EasySwapApp(customtkinter.CTk):
    def __init__(self):
        super().__init__()
        self.title("EasySwap для Perfect World")
        self.geometry("800x600")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Навигация --- #
        self.navigation_frame = customtkinter.CTkFrame(self, corner_radius=0)
        self.navigation_frame.grid(row=0, column=0, sticky="nsew")
        self.navigation_frame.grid_rowconfigure(4, weight=1)

        self.navigation_frame_label = customtkinter.CTkLabel(self.navigation_frame, text="EasySwap",
                                                             compound="left",
                                                             font=customtkinter.CTkFont(size=15, weight="bold"))
        self.navigation_frame_label.grid(row=0, column=0, padx=20, pady=20)

        self.home_button = customtkinter.CTkButton(self.navigation_frame, corner_radius=0, height=40,
                                                   border_spacing=10, text="Главная",
                                                   fg_color="transparent", text_color=("gray10", "gray90"),
                                                   hover_color=("gray70", "gray30"),
                                                   anchor="w", command=self.home_button_event)
        self.home_button.grid(row=1, column=0, sticky="ew")

        self.calibration_button = customtkinter.CTkButton(self.navigation_frame, corner_radius=0, height=40,
                                                          border_spacing=10, text="Калибровка",
                                                          fg_color="transparent", text_color=("gray10", "gray90"),
                                                          hover_color=("gray70", "gray30"),
                                                          anchor="w", command=self.calibration_button_event)
        self.calibration_button.grid(row=2, column=0, sticky="ew")

        self.settings_button = customtkinter.CTkButton(self.navigation_frame, corner_radius=0, height=40,
                                                       border_spacing=10, text="Настройки",
                                                       fg_color="transparent", text_color=("gray10", "gray90"),
                                                       hover_color=("gray70", "gray30"),
                                                       anchor="w", command=self.settings_button_event)
        self.settings_button.grid(row=3, column=0, sticky="ew")

        self.appearance_mode_label = customtkinter.CTkLabel(self.navigation_frame, text="Тема:", anchor="w")
        self.appearance_mode_label.grid(row=5, column=0, padx=20, pady=(10, 0))
        self.appearance_mode_optionemenu = customtkinter.CTkOptionMenu(self.navigation_frame, values=["Light", "Dark", "System"],
                                                                       command=self.change_appearance_mode_event)
        self.appearance_mode_optionemenu.grid(row=6, column=0, padx=20, pady=10, sticky="s")

        # --- Фреймы страниц --- #
        self.home_frame = customtkinter.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.calibration_frame = customtkinter.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.settings_frame = customtkinter.CTkFrame(self, corner_radius=0, fg_color="transparent")

        # --- Переменные состояния --- #
        self.config = self.load_config()
        self.easyswap_running = False
        self.swap_thread = None
        self.mouse_listener = None
        self.current_swap_state = STATE_IDLE
        self.last_cast_time = 0.0
        self.cast_seen_once = False
        self.cast_end_confirm_frames = 0
        self.bar_color_seen = False
        self._last_attack_key_time = 0.0
        self._state_lock = threading.Lock()
        self.key_setting_listener = None
        self.current_key_setting_entry = None
        self.current_key_setting_multiple = False

        # --- Инициализация страниц --- #
        self._create_home_frame()
        self._create_calibration_frame()
        self._create_settings_frame()

        # --- Выбор начальной страницы --- #
        self.select_frame_by_name("home")

        # --- Загрузка сохраненных значений в GUI --- #
        self._load_config_to_gui()

        # --- Обработка закрытия окна --- #
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    def _on_closing(self):
        if self.easyswap_running:
            self._stop_easyswap()
        keyboard.unhook_all()
        if self.mouse_listener and self.mouse_listener.is_alive():
            self.mouse_listener.stop()
        # Освобождаем залипшие бинды при выходе
        # Используем pynput для освобождения, так как keyboard не используется для перехвата
        KeyboardController().release(Key.ctrl_l)
        KeyboardController().release(Key.ctrl_r)
        KeyboardController().release(Key.alt_l)
        KeyboardController().release(Key.alt_r)
        KeyboardController().release(Key.shift_l)
        KeyboardController().release(Key.shift_r)
        self.destroy()

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "selected_window_hwnd": None,
            "selected_window_name": "",
            "cast_detection_pixel": {"relative_x": 0, "relative_y": 0, "color": (0, 0, 0)},
            "end_swap_pixel": {"relative_x": 0, "relative_y": 0, "color": (0, 0, 0)},
            "pz_set_icon_pixel": {"relative_x": 0, "relative_y": 0, "color": (0, 0, 0)},
            "sing_key": "",
            "swap_key": "",
            "attack_keys": [],
            "attack_bindings": {},
            "swap_apply_delay": 0.08, # Задержка после свапа в ПА перед нажатием скилла
            "pz_return_timeout": 3.0, # Таймаут для автовозврата в ПЗ
            "swap_after_skill_delay": 0.05, # Задержка после нажатия скилла перед возвратом в ПЗ
            "profile_name": "Default"
        }

    def save_config(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=4)

    def _load_config_to_gui(self):
        if self.config["selected_window_name"]:
            self.window_selection_combobox.set(self.config["selected_window_name"])

        self._update_pixel_display("cast_detection_pixel", self.cast_pixel_info_label, self.cast_pixel_color_frame)
        self._update_pixel_display("end_swap_pixel", self.end_swap_pixel_info_label, self.end_swap_pixel_color_frame)
        self._update_pixel_display("pz_set_icon_pixel", self.pz_pixel_info_label, self.pz_pixel_color_frame)

        self.sing_key_entry.delete(0, tk.END)
        self.sing_key_entry.insert(0, self.config.get("sing_key", ""))
        self.swap_key_entry.delete(0, tk.END)
        self.swap_key_entry.insert(0, self.config.get("swap_key", ""))

        attack_keys_str = ", ".join(self.config.get("attack_keys", []))
        self.attack_keys_entry.delete(0, tk.END)
        self.attack_keys_entry.insert(0, attack_keys_str)

        self.attack_bindings_text.configure(state="normal")
        self.attack_bindings_text.delete("1.0", tk.END)
        for physical_key, binding_info in self.config.get("attack_bindings", {}).items():
            self.attack_bindings_text.insert(
                tk.END, f"{_format_attack_binding_line(physical_key, binding_info)}\n"
            )
        self.attack_bindings_text.configure(state="disabled")

        self.swap_apply_delay_entry.delete(0, tk.END)
        self.swap_apply_delay_entry.insert(0, str(self.config.get("swap_apply_delay", 0.08)))
        self.pz_return_timeout_entry.delete(0, tk.END)
        self.pz_return_timeout_entry.insert(0, str(self.config.get("pz_return_timeout", 3.0)))
        self.swap_after_skill_delay_entry.delete(0, tk.END)
        self.swap_after_skill_delay_entry.insert(0, str(self.config.get("swap_after_skill_delay", 0.05)))

        self.profile_name_entry.delete(0, tk.END)
        self.profile_name_entry.insert(0, self.config.get("profile_name", "Default"))


    def select_frame_by_name(self, name):
        self.home_button.configure(fg_color=("gray75", "gray25") if name == "home" else "transparent")
        self.calibration_button.configure(fg_color=("gray75", "gray25") if name == "calibration" else "transparent")
        self.settings_button.configure(fg_color=("gray75", "gray25") if name == "settings" else "transparent")

        if name == "home":
            self.home_frame.grid(row=0, column=1, sticky="nsew")
        else:
            self.home_frame.grid_forget()
        if name == "calibration":
            self.calibration_frame.grid(row=0, column=1, sticky="nsew")
        else:
            self.calibration_frame.grid_forget()
        if name == "settings":
            self.settings_frame.grid(row=0, column=1, sticky="nsew")
        else:
            self.settings_frame.grid_forget()

    def home_button_event(self):
        self.select_frame_by_name("home")
        self._update_window_list()

    def calibration_button_event(self):
        self.select_frame_by_name("calibration")

    def settings_button_event(self):
        self.select_frame_by_name("settings")

    def change_appearance_mode_event(self, new_appearance_mode: str):
        customtkinter.set_appearance_mode(new_appearance_mode)

    def _create_home_frame(self):
        self.home_frame.grid_columnconfigure(0, weight=1)
        
        self.home_label = customtkinter.CTkLabel(self.home_frame, text="Управление EasySwap", font=customtkinter.CTkFont(size=24, weight="bold"))
        self.home_label.grid(row=0, column=0, padx=20, pady=20)

        # Выбор окна на главном экране
        customtkinter.CTkLabel(self.home_frame, text="Выберите окно игры:").grid(row=1, column=0, padx=20, pady=5, sticky="w")

        self.window_picker_frame = customtkinter.CTkFrame(self.home_frame, fg_color="transparent")
        self.window_picker_frame.grid(row=2, column=0, padx=20, pady=5, sticky="ew")
        self.window_picker_frame.grid_columnconfigure(0, weight=1)

        self.window_selection_combobox = customtkinter.CTkComboBox(
            self.window_picker_frame, values=[], command=self._on_window_selected, width=300
        )
        self.window_selection_combobox.grid(row=0, column=0, padx=(0, 8), sticky="ew")

        self.update_window_list_button = customtkinter.CTkButton(
            self.window_picker_frame, text="Обновить", width=100, command=self._update_window_list
        )
        self.update_window_list_button.grid(row=0, column=1, sticky="e")

        self.start_button = customtkinter.CTkButton(self.home_frame, text="Запустить EasySwap", command=self._start_easyswap, fg_color="green")
        self.start_button.grid(row=4, column=0, padx=20, pady=10)

        self.stop_button = customtkinter.CTkButton(self.home_frame, text="Остановить EasySwap", command=self._stop_easyswap, fg_color="red", state="disabled")
        self.stop_button.grid(row=5, column=0, padx=20, pady=10)

        self.status_label = customtkinter.CTkLabel(self.home_frame, text="Статус: Остановлен", font=customtkinter.CTkFont(size=14))
        self.status_label.grid(row=6, column=0, padx=20, pady=10)

        self.log_toolbar = customtkinter.CTkFrame(self.home_frame, fg_color="transparent")
        self.log_toolbar.grid(row=7, column=0, padx=20, pady=(0, 5), sticky="ew")
        self.log_toolbar.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(self.log_toolbar, text="Лог:").grid(row=0, column=0, sticky="w")
        self.copy_log_button = customtkinter.CTkButton(
            self.log_toolbar, text="Копировать лог", width=130, command=self.copy_log
        )
        self.copy_log_button.grid(row=0, column=1, sticky="e")

        self.log_text = customtkinter.CTkTextbox(self.home_frame, width=400, height=150)
        self.log_text.grid(row=8, column=0, padx=20, pady=(0, 20), sticky="nsew")
        self.log_text.insert("end", "Добро пожаловать в EasySwap!\n")
        self._setup_log_widget()

        self._update_window_list()

    def _setup_log_widget(self):
        """Лог только для чтения, но с выделением и копированием."""
        self.log_text.bind("<Key>", lambda _event: "break")
        self.log_text.bind("<Control-c>", self._copy_log_selection)
        self.log_text.bind("<Control-C>", self._copy_log_selection)
        self.log_text.bind("<Button-3>", self._show_log_context_menu)
        self.log_context_menu = tk.Menu(self, tearoff=0)
        self.log_context_menu.add_command(label="Копировать выделение", command=self._copy_log_selection)
        self.log_context_menu.add_command(label="Копировать весь лог", command=self.copy_log)

    def _show_log_context_menu(self, event):
        try:
            self.log_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_context_menu.grab_release()

    def copy_log(self):
        if not hasattr(self, "log_text"):
            return
        content = self.log_text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(content)

    def _copy_log_selection(self, event=None):
        if not hasattr(self, "log_text"):
            return "break"
        try:
            selected = self.log_text.get("sel.first", "sel.last")
        except tk.TclError:
            selected = ""
        if selected:
            self.clipboard_clear()
            self.clipboard_append(selected)
        else:
            self.copy_log()
        return "break"

    def _log_message(self, message):
        if not hasattr(self, "log_text"):
            return
        self.log_text.insert("end", f"{time.strftime('%H:%M:%S')} - {message}\n")
        self.log_text.see("end")

    def _start_easyswap(self):
        if self.easyswap_running:
            self._log_message("EasySwap уже запущен.")
            return

        if not self.config.get("selected_window_hwnd"):
            messagebox.showerror("Ошибка", "Пожалуйста, выберите окно игры на главном экране.")
            return
        if not self.config.get("swap_key"):
            messagebox.showerror("Ошибка", "Пожалуйста, укажите клавишу свапа в разделе 'Настройки'.")
            return
        if not _is_pixel_calibrated(self.config.get("cast_detection_pixel")):
            messagebox.showerror("Ошибка", "Пожалуйста, откалибруйте пиксель каста.")
            return
        if not _is_pixel_calibrated(self.config.get("end_swap_pixel")):
            messagebox.showerror("Ошибка", "Пожалуйста, откалибруйте пиксель конца свапа.")
            return
        if not _is_pixel_calibrated(self.config.get("pz_set_icon_pixel")):
            messagebox.showerror("Ошибка", "Пожалуйста, откалибруйте пиксель иконки ПЗ.")
            return

        self.easyswap_running = True
        self.start_button.configure(state="disabled", fg_color="gray")
        self.stop_button.configure(state="normal", fg_color="red")
        self.status_label.configure(text="Статус: Запущен")
        self._log_message("EasySwap запущен.")

        self.swap_thread = threading.Thread(target=self._swap_loop, daemon=True)
        self.swap_thread.start()

        keyboard.on_press(self._on_keyboard_event)

    def _stop_easyswap(self):
        if not self.easyswap_running:
            self._log_message("EasySwap уже остановлен.")
            return

        self.easyswap_running = False
        if self.swap_thread and self.swap_thread.is_alive():
            pass
        keyboard.unhook_all()

        self.start_button.configure(state="normal", fg_color="green")
        self.stop_button.configure(state="disabled", fg_color="gray")
        self.status_label.configure(text="Статус: Остановлен")
        self._log_message("EasySwap остановлен.")
        with self._state_lock:
            self.current_swap_state = STATE_IDLE
            self.cast_seen_once = False
            self.cast_end_confirm_frames = 0
            self.bar_color_seen = False

    def _return_to_pz(self, hwnd, log_message):
        """Возврат в ПЗ через SendInput (press_key_system)."""
        if PZ_RETURN_EXTRA_DELAY > 0:
            time.sleep(PZ_RETURN_EXTRA_DELAY)
        time.sleep(float(self.config.get("swap_after_skill_delay", 0.05)))
        press_key_system(hwnd, self.config["swap_key"])
        with self._state_lock:
            self.current_swap_state = STATE_IDLE
            self.cast_seen_once = False
            self.cast_end_confirm_frames = 0
            self.bar_color_seen = False
            self.last_cast_time = time.time()
        self._log_message(log_message)

    def _on_keyboard_event(self, event):
        if event.event_type != keyboard.KEY_DOWN:
            return
        if getattr(event, "is_synthetic", False) or _is_key_injection_active():
            return
        if not event.name:
            return

        key_name = event.name.lower()
        attack_keys = [k.lower() for k in self.config.get("attack_keys", [])]
        if key_name not in attack_keys:
            return

        now = time.time()
        if now - self._last_attack_key_time < ATTACK_KEY_DEBOUNCE:
            return
        self._last_attack_key_time = now

        threading.Thread(
            target=self._handle_attack_key,
            args=(key_name,),
            daemon=True,
        ).start()

    def _parse_attack_keys_input(self, input_str):
        attack_keys_raw = [k.strip() for k in input_str.split(',') if k.strip()]
        attack_bindings = {}
        for key_entry in attack_keys_raw:
            is_double_click = False
            if '(double)' in key_entry:
                key_entry = key_entry.replace('(double)', '').strip()
                is_double_click = True

            if '=' in key_entry:
                physical, game = key_entry.split('=', 1)
                attack_bindings[physical.strip().lower()] = {'game_key': game.strip().lower(), 'double_click': is_double_click}
            else:
                attack_bindings[key_entry.lower()] = {'game_key': key_entry.lower(), 'double_click': is_double_click}
        return list(attack_bindings.keys()), attack_bindings

    def _save_keys_from_gui(self):
        # Форматируем все поля ввода
        self._format_keys_input(self.sing_key_entry)
        self._format_keys_input(self.swap_key_entry)
        self._format_keys_input(self.attack_keys_entry)

        # Сохраняем основные клавиши
        self.config["sing_key"] = self.sing_key_entry.get().lower()
        self.config["swap_key"] = self.swap_key_entry.get().lower()

        # Парсим и сохраняем атакующие клавиши
        parsed_attack_keys, parsed_attack_bindings = self._parse_attack_keys_input(self.attack_keys_entry.get())
        self.config["attack_keys"] = parsed_attack_keys
        self.config["attack_bindings"] = parsed_attack_bindings

        # Сохраняем задержки
        try:
            self.config["swap_apply_delay"] = float(self.swap_apply_delay_entry.get())
            self.config["pz_return_timeout"] = float(self.pz_return_timeout_entry.get())
            self.config["swap_after_skill_delay"] = float(self.swap_after_skill_delay_entry.get())
        except ValueError:
            self._log_message("Ошибка: Некорректные значения задержек. Используются старые значения.")

        self.config["profile_name"] = self.profile_name_entry.get()

        self.save_config()
        
        # Обновляем текстовое поле с биндами для наглядности
        self.attack_bindings_text.configure(state="normal")
        self.attack_bindings_text.delete("1.0", tk.END)
        for physical_key, info in parsed_attack_bindings.items():
            self.attack_bindings_text.insert(
                tk.END, f"{_format_attack_binding_line(physical_key, info)}\n"
            )
        self.attack_bindings_text.configure(state="disabled")

        self._log_message("Все настройки сохранены и применены.")

    def _handle_attack_key(self, physical_key: str):
        if not self.easyswap_running:
            return

        if win32gui.GetForegroundWindow() != self.config.get("selected_window_hwnd", None):
            return

        binding_info = self.config.get("attack_bindings", {}).get(physical_key, {})
        if isinstance(binding_info, dict):
            game_key = binding_info.get("game_key", physical_key)
            is_double_click = binding_info.get("double_click", False)
        else:
            game_key = str(binding_info) if binding_info else physical_key
            is_double_click = False

        # Сначала как можно раньше жмём свап (ПА)
        if self.config.get("swap_key"):
            press_key_system(self.config["selected_window_hwnd"], self.config["swap_key"])
            self._log_message(f"[Attack] Сразу надеваем ПА: {self.config['swap_key']}")

        # После этого (если нужно) надеваем сет пения отдельной клавишей
        if self.config.get("sing_key"):
            press_key_system(self.config["selected_window_hwnd"], self.config["sing_key"])
            self._log_message(f"[Attack] Надет сет Пения: {self.config['sing_key']}")

        # Нажимаем сам скилл уже после свапа в ПА.
        delay = float(self.config.get("swap_apply_delay", 0.08))
        if delay > 0:
            time.sleep(delay)
        if is_double_click:
            press_key_system(self.config["selected_window_hwnd"], game_key)
            time.sleep(float(self.config.get("double_click_delay", 50)) / 1000.0)
            press_key_system(self.config["selected_window_hwnd"], game_key)
            self._log_message(f"[Attack] Двойной клик скилла: {game_key} (по физической кнопке {physical_key})")
        else:
            press_key_system(self.config["selected_window_hwnd"], game_key)
            self._log_message(f"[Attack] Нажали скилл: {game_key} (по физической кнопке {physical_key})")

        with self._state_lock:
            self.current_swap_state = STATE_CASTING_SING
            self.last_cast_time = time.time()
            self.cast_seen_once = False
            self.cast_end_confirm_frames = 0
            self.bar_color_seen = False
        self._log_message("[Attack] Состояние: CASTING_SING — ждём ромб каста.")

    def _format_keys_input(self, entry_widget):
        """Удаляет лишние пробелы и корректно расставляет запятые в поле ввода.
        Если введена строка без разделителей (например, '12345'), она будет разбита на отдельные символы.
        """
        text = entry_widget.get().strip()
        if not text:
            return []

        # Если в тексте нет запятых и пробелов, и это длинная строка, пробуем разбить посимвольно
        if ',' not in text and ' ' not in text and len(text) > 1:
            # Проверяем, не является ли это функциональной клавишей (f1-f12)
            if not (text.lower().startswith('f') and text[1:].isdigit()):
                keys = [c.lower() for c in text]
            else:
                keys = [text.lower()]
        else:
            # Обычное разделение по запятым и пробелам
            keys = [k.strip().lower() for k in text.replace(',', ' ').split() if k.strip()]
        
        formatted_text = ", ".join(keys)
        entry_widget.delete(0, tk.END)
        entry_widget.insert(0, formatted_text)
        return keys

    def _create_calibration_frame(self):
        self.calibration_frame.grid_columnconfigure(0, weight=1)
        self.calibration_frame.grid_columnconfigure(1, weight=1)
        self.calibration_label = customtkinter.CTkLabel(self.calibration_frame, text="Калибровка Пикселей", font=customtkinter.CTkFont(size=24, weight="bold"))
        self.calibration_label.grid(row=0, column=0, columnspan=2, padx=20, pady=20)

        customtkinter.CTkLabel(self.calibration_frame, text="Контрольный пиксель каста (ромб):").grid(row=3, column=0, padx=10, pady=5, sticky="w")
        self.cast_pixel_info_label = customtkinter.CTkLabel(self.calibration_frame, text="X: -, Y: -, Color: -")
        self.cast_pixel_info_label.grid(row=4, column=0, padx=10, pady=2, sticky="w")
        self.cast_pixel_color_frame = customtkinter.CTkFrame(self.calibration_frame, width=20, height=20, corner_radius=3, fg_color="gray")
        self.cast_pixel_color_frame.grid(row=4, column=1, padx=10, pady=2, sticky="w")
        self.calibrate_cast_pixel_button = customtkinter.CTkButton(self.calibration_frame, text="Прицел", command=lambda: self._start_pixel_calibration("cast_detection_pixel"))
        self.calibrate_cast_pixel_button.grid(row=3, column=1, padx=10, pady=5, sticky="ew")

        customtkinter.CTkLabel(self.calibration_frame, text="Конец свапа (точка на полоске):").grid(row=5, column=0, padx=10, pady=5, sticky="w")
        self.end_swap_pixel_info_label = customtkinter.CTkLabel(self.calibration_frame, text="X: -, Y: -, Color: -")
        self.end_swap_pixel_info_label.grid(row=6, column=0, padx=10, pady=2, sticky="w")
        self.end_swap_pixel_color_frame = customtkinter.CTkFrame(self.calibration_frame, width=20, height=20, corner_radius=3, fg_color="gray")
        self.end_swap_pixel_color_frame.grid(row=6, column=1, padx=10, pady=2, sticky="ew")
        self.calibrate_end_swap_pixel_button = customtkinter.CTkButton(self.calibration_frame, text="Прицел", command=lambda: self._start_pixel_calibration("end_swap_pixel"))
        self.calibrate_end_swap_pixel_button.grid(row=5, column=1, padx=10, pady=5, sticky="ew")

        customtkinter.CTkLabel(self.calibration_frame, text="Иконка ПЗ Сета (для проверки):").grid(row=7, column=0, padx=10, pady=5, sticky="w")
        self.pz_pixel_info_label = customtkinter.CTkLabel(self.calibration_frame, text="X: -, Y: -, Color: -")
        self.pz_pixel_info_label.grid(row=8, column=0, padx=10, pady=2, sticky="w")
        self.pz_pixel_color_frame = customtkinter.CTkFrame(self.calibration_frame, width=20, height=20, corner_radius=3, fg_color="gray")
        self.pz_pixel_color_frame.grid(row=8, column=1, padx=10, pady=2, sticky="ew")
        self.calibrate_pz_pixel_button = customtkinter.CTkButton(self.calibration_frame, text="Прицел", command=lambda: self._start_pixel_calibration("pz_set_icon_pixel"))
        self.calibrate_pz_pixel_button.grid(row=7, column=1, padx=10, pady=5, sticky="ew")

        customtkinter.CTkLabel(self.calibration_frame, text="Задержка двойного клика (мс):").grid(row=9, column=0, padx=10, pady=5, sticky="w")
        self.double_click_delay_entry = customtkinter.CTkEntry(self.calibration_frame)
        self.double_click_delay_entry.grid(row=9, column=1, padx=10, pady=5, sticky="ew")
        self.double_click_delay_entry.insert(0, str(self.config.get("double_click_delay", 50)))

        self.save_calibration_button = customtkinter.CTkButton(self.calibration_frame, text="Сохранить калибровку", command=self._save_calibration, state="normal")
        self.save_calibration_button.grid(row=10, column=0, columnspan=2, padx=10, pady=10)

    def _update_window_list(self):
        self.windows_list = get_window_list_windows()
        window_names = [w["name"] for w in self.windows_list]
        self.window_selection_combobox.configure(values=window_names if window_names else ["Окна не найдены"])
        if self.config["selected_window_name"] in window_names:
            self.window_selection_combobox.set(self.config["selected_window_name"])
        elif window_names:
            self.window_selection_combobox.set(window_names[0])
            self._on_window_selected(window_names[0])
        else:
            self.window_selection_combobox.set("Окна не найдены")
            self.config["selected_window_hwnd"] = None
            self.config["selected_window_name"] = ""
            self.save_config()
        self._log_message(
            f"Список окон обновлён: найдено {len(window_names)} "
            f"(фильтр: {', '.join(sorted(PW_PROCESS_NAMES))} или "
            f"«{' / '.join(k.title() for k in PW_TITLE_KEYWORDS)}»)."
        )

    def _on_window_selected(self, window_name):
        selected_window = next((w for w in self.windows_list if w["name"] == window_name), None)
        if selected_window:
            self.config["selected_window_hwnd"] = selected_window["hwnd"]
            self.config["selected_window_name"] = selected_window["name"]
            self.save_config()
            self._log_message(f"Выбрано окно: {window_name}")
        else:
            self.config["selected_window_hwnd"] = None
            self.config["selected_window_name"] = ""
            self.save_config()
            self._log_message(f"Ошибка: Окно '{window_name}' не найдено.")

    def _start_pixel_calibration(self, pixel_type):
        hwnd = self.config.get("selected_window_hwnd")
        if not hwnd:
            messagebox.showerror("Ошибка", "Сначала выберите окно игры на главном экране!")
            return

        # Переключаем фокус на окно игры
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception as e:
            self._log_message(f"Предупреждение при переключении окна: {e}")

        self.current_calibration_pixel_type = pixel_type
        self._log_message(f"Начата калибровка для {pixel_type}. Нажмите ЛКМ в окне игры.")
        self.calibrate_cast_pixel_button.configure(state="disabled")
        self.calibrate_end_swap_pixel_button.configure(state="disabled")
        self.calibrate_pz_pixel_button.configure(state="disabled")

        self.mouse_listener = MouseListener(on_click=self._on_mouse_click_for_calibration)
        self.mouse_listener.start()

    def _on_mouse_click_for_calibration(self, x, y, button, pressed):
        if pressed and button == Button.left:
            hwnd = self.config.get("selected_window_hwnd")
            color = get_pixel_color_at_screen(x, y, hwnd=hwnd)
            self._log_message(f"Выбран пиксель в ({x}, {y}) с цветом {color}")

            if hwnd:
                client_x, client_y, _, _ = get_window_current_geometry_windows(hwnd)
                if client_x is not None:
                    relative_x = x - client_x
                    relative_y = y - client_y
                    self.config[self.current_calibration_pixel_type] = {
                        "relative_x": relative_x,
                        "relative_y": relative_y,
                        "color": color,
                        "calibrated": True,
                    }
                    self.save_config()
                    label_name, frame_name = CALIBRATION_WIDGETS[self.current_calibration_pixel_type]
                    self._update_pixel_display(
                        self.current_calibration_pixel_type,
                        getattr(self, label_name),
                        getattr(self, frame_name),
                    )
                    self._log_message(f"Пиксель {self.current_calibration_pixel_type} откалиброван: X:{relative_x}, Y:{relative_y}, Color:{color}")
                else:
                    self._log_message("Ошибка: Не удалось получить геометрию выбранного окна.")
            else:
                self._log_message("Ошибка: Окно игры не выбрано.")

            # Останавливаем слушатель после клика
            if self.mouse_listener:
                self.mouse_listener.stop()
                self.mouse_listener = None
            
            # Возвращаем кнопки в активное состояние
            self.calibrate_cast_pixel_button.configure(state="normal")
            self.calibrate_end_swap_pixel_button.configure(state="normal")
            self.calibrate_pz_pixel_button.configure(state="normal")
            return False
        return True

    def _update_pixel_display(self, pixel_type, label_widget, color_frame_widget):
        pixel_data = self.config.get(pixel_type, {})
        if pixel_data and pixel_data.get("color") is not None and len(pixel_data["color"]) == 3:
            label_widget.configure(text=f"X: {pixel_data['relative_x']}, Y: {pixel_data['relative_y']}, Color: {pixel_data['color']}")
            r, g, b = pixel_data['color']
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            color_frame_widget.configure(fg_color=hex_color)
        else:
            label_widget.configure(text="X: -, Y: -, Color: -")
            color_frame_widget.configure(fg_color="gray")

    def _save_calibration(self):
        try:
            self.config["double_click_delay"] = int(self.double_click_delay_entry.get())
        except ValueError:
            self.config["double_click_delay"] = 50 # Default if invalid
            self._log_message("Некорректное значение для задержки двойного клика. Установлено значение по умолчанию (50мс).")

        self.save_config()
        self._log_message("Настройки калибровки сохранены.")

    def _create_settings_frame(self):
        self.settings_frame.grid_columnconfigure(0, weight=1)
        self.settings_frame.grid_columnconfigure(1, weight=1)
        self.settings_label = customtkinter.CTkLabel(self.settings_frame, text="Настройки EasySwap", font=customtkinter.CTkFont(size=24, weight="bold"))
        self.settings_label.grid(row=0, column=0, columnspan=2, padx=20, pady=20)

        customtkinter.CTkLabel(self.settings_frame, text="Клавиша пения (Sing Key):").grid(row=1, column=0, padx=10, pady=5, sticky="w")
        self.sing_key_entry = customtkinter.CTkEntry(self.settings_frame)
        self.sing_key_entry.grid(row=1, column=1, padx=10, pady=5, sticky="ew")

        customtkinter.CTkLabel(self.settings_frame, text="Клавиша свапа (Swap Key - PA/PZ):").grid(row=2, column=0, padx=10, pady=5, sticky="w")
        self.swap_key_entry = customtkinter.CTkEntry(self.settings_frame)
        self.swap_key_entry.grid(row=2, column=1, padx=10, pady=5, sticky="ew")

        customtkinter.CTkLabel(self.settings_frame, text="Атакующие клавиши (например: 12345):", wraplength=300).grid(row=3, column=0, padx=10, pady=5, sticky="w")
        self.attack_keys_entry = customtkinter.CTkEntry(self.settings_frame)
        self.attack_keys_entry.grid(row=3, column=1, padx=10, pady=5, sticky="ew")

        customtkinter.CTkLabel(self.settings_frame, text="Задержка после свапа в ПА перед скиллом (сек):").grid(row=4, column=0, padx=10, pady=5, sticky="w")
        self.swap_apply_delay_entry = customtkinter.CTkEntry(self.settings_frame)
        self.swap_apply_delay_entry.grid(row=4, column=1, padx=10, pady=5, sticky="ew")

        customtkinter.CTkLabel(self.settings_frame, text="Таймаут для автовозврата в ПЗ (сек):").grid(row=5, column=0, padx=10, pady=5, sticky="w")
        self.pz_return_timeout_entry = customtkinter.CTkEntry(self.settings_frame)
        self.pz_return_timeout_entry.grid(row=5, column=1, padx=10, pady=5, sticky="ew")

        customtkinter.CTkLabel(self.settings_frame, text="Задержка после нажатия скилла перед возвратом в ПЗ (сек):").grid(row=6, column=0, padx=10, pady=5, sticky="w")
        self.swap_after_skill_delay_entry = customtkinter.CTkEntry(self.settings_frame)
        self.swap_after_skill_delay_entry.grid(row=6, column=1, padx=10, pady=5, sticky="ew")

        customtkinter.CTkLabel(self.settings_frame, text="Имя профиля:").grid(row=7, column=0, padx=10, pady=5, sticky="w")
        self.profile_name_entry = customtkinter.CTkEntry(self.settings_frame)
        self.profile_name_entry.grid(row=7, column=1, padx=10, pady=5, sticky="ew")

        self.save_settings_button = customtkinter.CTkButton(self.settings_frame, text="Сохранить настройки", command=self._save_keys_from_gui)
        self.save_settings_button.grid(row=8, column=0, columnspan=2, padx=10, pady=20)

        customtkinter.CTkLabel(self.settings_frame, text="Текущие бинды (физическая=игровая):").grid(row=9, column=0, padx=10, pady=5, sticky="w")
        self.attack_bindings_text = customtkinter.CTkTextbox(self.settings_frame, height=80)
        self.attack_bindings_text.grid(row=9, column=1, padx=10, pady=5, sticky="ew")
        self.attack_bindings_text.configure(state="disabled")

    def _swap_loop(self):
        pz_icon_not_match_frames = 0
        last_pz_auto_swap_time = 0.0
        last_cast_debug_log_time = 0.0
        last_pa_debug_log_time = 0.0
        hwnd = self.config["selected_window_hwnd"]

        while self.easyswap_running:
            try:
                if win32gui.GetForegroundWindow() != self.config["selected_window_hwnd"]:
                    time.sleep(0.05)
                    continue
            except Exception:
                time.sleep(0.05)
                continue

            try:
                client_x, client_y, _, _ = get_window_current_geometry_windows(hwnd)
            except Exception:
                time.sleep(0.05)
                continue

            if client_x is None:
                self._log_message("Окно игры не найдено, останавливаем EasySwap.")
                self.after(0, self._stop_easyswap)
                break

            cast_pixel_data = self.config["cast_detection_pixel"]
            end_swap_pixel_data = self.config["end_swap_pixel"]
            pz_pixel_data = self.config["pz_set_icon_pixel"]

            sleep_interval = 0.0025

            with self._state_lock:
                swap_state = self.current_swap_state

            if swap_state == STATE_IDLE:
                current_pz_color = get_pixel_color_client_avg(
                    hwnd,
                    pz_pixel_data["relative_x"],
                    pz_pixel_data["relative_y"],
                    radius=1,
                )
                is_pz = compare_colors_hsv(
                    current_pz_color,
                    pz_pixel_data["color"],
                    h_tol=0.05,
                    s_tol=0.30,
                    v_tol=0.35,
                )

                if is_pz:
                    pz_icon_not_match_frames = 0
                else:
                    pz_icon_not_match_frames += 1

                now = time.time()
                timed_out = (now - self.last_cast_time) > self.config["pz_return_timeout"]
                cooldown_ok = (now - last_pz_auto_swap_time) > PZ_AUTO_SWAP_COOLDOWN

                if (timed_out and cooldown_ok and
                    pz_icon_not_match_frames >= PZ_ICON_NOT_MATCH_FRAMES_REQUIRED):
                    press_key_system(hwnd, self.config["swap_key"])
                    self._log_message("Таймаут истек, возвращаемся в ПЗ (по иконке ПЗ, анти-дребезг).")
                    last_pz_auto_swap_time = now
                    pz_icon_not_match_frames = 0

            elif swap_state == STATE_CASTING_SING:
                sleep_interval = 0.0002
                cast_x = cast_pixel_data["relative_x"]
                cast_y = cast_pixel_data["relative_y"]
                ref_cast_color = tuple(int(c) for c in cast_pixel_data["color"][:3])

                cast_started, current_cast_color = fuzzy_match_in_client_area(
                    hwnd,
                    cast_x,
                    cast_y,
                    cast_pixel_data["color"],
                    is_cast_color_match,
                    radius=FUZZY_CAST_RADIUS,
                )

                now = time.time()
                if now - last_cast_debug_log_time >= CAST_DEBUG_INTERVAL:
                    self._log_message(
                        f"DEBUG: Ищу ромб в ({cast_x}, {cast_y}). "
                        f"Вижу цвет {current_cast_color}. Жду {ref_cast_color}"
                    )
                    last_cast_debug_log_time = now

                elapsed = time.time() - self.last_cast_time

                if cast_started:
                    with self._state_lock:
                        self.current_swap_state = STATE_CASTING_PA
                        self.cast_seen_once = True
                        self.cast_end_confirm_frames = 0
                        self.bar_color_seen = False
                        self.last_cast_time = time.time()
                    self._log_message("Ромб появился → CASTING_PA, ждём исчезновение полоски.")
                elif elapsed > CAST_DIAMOND_TIMEOUT:
                    self._return_to_pz(hwnd, "Ромб не найден, таймаут. Возвращаю ПЗ")

            elif swap_state == STATE_CASTING_PA:
                sleep_interval = 0.00025
                end_x = end_swap_pixel_data["relative_x"]
                end_y = end_swap_pixel_data["relative_y"]
                ref_bar_color = tuple(int(c) for c in end_swap_pixel_data["color"][:3])
                current_end_color = get_pixel_color_client(hwnd, end_x, end_y)

                now = time.time()
                if now - last_pa_debug_log_time >= CAST_PA_DEBUG_INTERVAL:
                    self._log_message(
                        f"DEBUG: Жду конца полоски в ({end_x}, {end_y}). "
                        f"Вижу цвет {current_end_color}. Калибровка была {ref_bar_color}"
                    )
                    last_pa_debug_log_time = now

                pa_elapsed = now - self.last_cast_time

                if pa_elapsed > CAST_PA_MAX_DURATION:
                    self._return_to_pz(
                        hwnd,
                        f"Таймаут PA {CAST_PA_MAX_DURATION}с — принудительный возврат в ПЗ.",
                    )
                elif pa_elapsed >= MIN_PA_DURATION:
                    bar_still_present = is_bar_color_similar(
                        current_end_color, end_swap_pixel_data["color"]
                    )

                    if bar_still_present:
                        self.bar_color_seen = True
                        self.cast_end_confirm_frames = 0
                    else:
                        self.cast_end_confirm_frames += 1

                    if self.cast_end_confirm_frames >= CAST_END_FRAMES_REQUIRED:
                        self._return_to_pz(
                            hwnd,
                            f"Полоска исчезла, возврат в ПЗ: {self.config['swap_key']}",
                        )

            time.sleep(sleep_interval)

        self._log_message("EasySwap loop завершен.")


if __name__ == "__main__":
    customtkinter.set_appearance_mode("Dark")
    customtkinter.set_default_color_theme("blue")

    app = EasySwapApp()
    app.mainloop()
