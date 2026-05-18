import sys, os, ctypes

def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

if not is_admin():
    params = " ".join(f'"{a}"' for a in sys.argv)
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    if ret <= 32:
        print("Не удалось получить права администратора.")
        input("Нажмите Enter...")
    sys.exit(0)

import time
import threading
import signal
import atexit

import numpy as np
import mss
import cv2
import win32gui
import win32con
from ctypes import windll, wintypes

from PySide6.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QLineEdit, QFrame
)
from PySide6.QtCore import Qt, QTimer, QObject, QSize, QEvent
from PySide6.QtGui import QFont, QIntValidator, QIcon, QPainter, QColor, QPixmap
from PySide6.QtSvg import QSvgRenderer


def _get_screen_size():
    w = ctypes.windll.user32.GetSystemMetrics(0)
    h = ctypes.windll.user32.GetSystemMetrics(1)
    return w, h


def _get_resolution_profile():
    w, h = _get_screen_size()

    # Базовые пропорции, снятые с 1920×1080
    REL_LEFT    = 605  / 1920
    REL_TOP     = 65   / 1080
    REL_WIDTH   = 718  / 1920
    REL_HEIGHT  = 20   / 1080
    REL_CLICK_X = 1460 / 1920
    REL_CLICK_Y = 950  / 1080

    return {
        "capture": dict(
            left   = int(w * REL_LEFT),
            top    = int(h * REL_TOP),
            width  = int(w * REL_WIDTH),
            height = max(4, int(h * REL_HEIGHT)),
        ),
        "click_pt": [int(w * REL_CLICK_X), int(h * REL_CLICK_Y)],
    }


_KEYDOWN = 0x0000
_KEYUP   = 0x0002
_SCANMAP = {"a": 0x1E, "d": 0x20, "f": 0x21, "e": 0x12, "esc": 0x01}

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("_pad", ctypes.c_byte * 28)]

class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("_u", _INPUT_UNION)]

def _si_key(key: str, up: bool):
    scan = _SCANMAP.get(key, 0)
    flags = _KEYUP if up else _KEYDOWN
    flags |= 0x0008
    ki = _KEYBDINPUT(0, scan, flags, 0, None)
    inp = _INPUT()
    inp.type = 1
    inp._u.ki = ki
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

def _key_down(key: str): _si_key(key, False)
def _key_up(key: str):   _si_key(key, True)


def _send_mouse_input(dx, dy, flags):
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx",          ctypes.c_long),
            ("dy",          ctypes.c_long),
            ("mouseData",   ctypes.c_ulong),
            ("dwFlags",     ctypes.c_ulong),
            ("time",        ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]
    class INPUT(ctypes.Structure):
        class _INPUT(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]
        _anonymous_ = ("_input",)
        _fields_ = [("type", ctypes.c_ulong), ("_input", _INPUT)]
    mi = MOUSEINPUT(dx, dy, 0, flags, 0, None)
    inp = INPUT()
    inp.type = 0
    inp.mi = mi
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

def _move_and_click(x: int, y: int):
    screen_w, screen_h = _get_screen_size()
    nx = int(x * 65535 / max(screen_w - 1, 1))
    ny = int(y * 65535 / max(screen_h - 1, 1))
    MOUSEEVENTF_MOVE     = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP   = 0x0004
    MOUSEEVENTF_ABSOLUTE = 0x8000
    _send_mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)
    time.sleep(0.05)
    _send_mouse_input(nx, ny, MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE)
    time.sleep(0.05)
    _send_mouse_input(nx, ny, MOUSEEVENTF_LEFTUP   | MOUSEEVENTF_ABSOLUTE)


_profile = _get_resolution_profile()

# Верхняя половина полосы: RGB(32,181,161) / #20b5a1 → HSV(86,210,181)
CYAN_TOP_HSV_LOW  = np.array([ 78, 170, 141])
CYAN_TOP_HSV_HIGH = np.array([ 94, 250, 221])
# Нижняя половина полосы: RGB(54,230,191) / #36e6bf → HSV(83,195,230)
CYAN_BOT_HSV_LOW  = np.array([ 75, 155, 190])
CYAN_BOT_HSV_HIGH = np.array([ 91, 235, 255])
# Для обратной совместимости (watchdog/state не используют напрямую)
CYAN_HSV_LOW    = CYAN_TOP_HSV_LOW
CYAN_HSV_HIGH   = CYAN_TOP_HSV_HIGH
YELLOW_HSV_LOW  = np.array([15,  90, 160])
YELLOW_HSV_HIGH = np.array([45, 255, 255])

LOOP_FPS  = 240

DEAD_PX     = 1.5
PULSE_MIN   = 0.008
PULSE_MAX   = 0.045
PULSE_RANGE = 30.0
PULSE_GAP   = 0.010

WATCHDOG_TIMEOUT = 60.0

state = {
    "running":          False,
    "capture":          dict(_profile["capture"]),
    "bar_x":            0,
    "bar_w":            0,
    "stick_x":          0,
    "last_action":      "",
    "fps_real":         0.0,
    "cyan_lo":          CYAN_HSV_LOW.copy(),
    "cyan_hi":          CYAN_HSV_HIGH.copy(),
    "cyan_top_lo":      CYAN_TOP_HSV_LOW.copy(),
    "cyan_top_hi":      CYAN_TOP_HSV_HIGH.copy(),
    "cyan_bot_lo":      CYAN_BOT_HSV_LOW.copy(),
    "cyan_bot_hi":      CYAN_BOT_HSV_HIGH.copy(),
    "yellow_lo":        YELLOW_HSV_LOW.copy(),
    "yellow_hi":        YELLOW_HSV_HIGH.copy(),
    "bar_left":         0,
    "bar_right":        0,
    "click_pt":         list(_profile["click_pt"]),
    "watchdog_active":  False,
    "watchdog_fire":    False,
    "last_ad_press":    0.0,
    "watchdog_elapsed": 0.0,
}


def _longest_run(arr: np.ndarray):
    if not arr.any():
        return 0, 0
    padded = np.concatenate(([0], (arr > 0).astype(np.int8), [0]))
    diff = np.diff(padded)
    starts = np.where(diff == 1)[0]
    ends   = np.where(diff == -1)[0]
    lengths = ends - starts
    idx = np.argmax(lengths)
    return int(starts[idx]), int(lengths[idx])


def find_bar_range(mask: np.ndarray, min_width: int = 12, min_height: int = 3):
    rows_filled = int(np.sum(np.any(mask, axis=1)))
    if rows_filled < min_height:
        return None
    total_w = mask.shape[1]
    # Гасим крайние столбцы ДО подсчёта runs —
    # именно они дают артефакт «слияния» с краем GDI-оверлея.
    EDGE_KILL = 8
    safe = mask.copy()
    safe[:, :EDGE_KILL]           = 0
    safe[:, total_w - EDGE_KILL:] = 0
    cols = np.any(safe, axis=0).astype(np.int8)
    if not cols.any():
        return None
    best_start, best_len = _longest_run(cols)
    if best_len < min_width:
        return None
    # Ограничиваем максимальную ширину: бар не может занять >85% кадра
    MAX_BAR_FRAC = 0.85
    if best_len > int(total_w * MAX_BAR_FRAC):
        return None
    left  = best_start
    right = best_start + best_len - 1
    if right <= left:
        return None
    return left, right, (left + right) // 2


def tap_key(key: str, duration: float = 0.05):
    _key_down(key)
    end = time.perf_counter() + duration
    while time.perf_counter() < end:
        if not state["running"]:
            break
        time.sleep(0.005)
    _key_up(key)


def _grab_without_overlay(cap: dict) -> np.ndarray:
    x, y, w, h = cap["left"], cap["top"], cap["width"], cap["height"]
    gdi32  = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        raise RuntimeError("GetDC(None) вернул NULL")
    hdc_mem = None
    hbmp    = None
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp    = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        gdi32.SelectObject(hdc_mem, hbmp)
        gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, 0x00CC0020)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize",          ctypes.c_uint32), ("biWidth",  ctypes.c_int32),
                ("biHeight",        ctypes.c_int32),  ("biPlanes", ctypes.c_uint16),
                ("biBitCount",      ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage",     ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),  ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant",  ctypes.c_uint32),
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w; bmi.biHeight = -h
        bmi.biPlanes = 1; bmi.biBitCount = 32; bmi.biCompression = 0
        buf = (ctypes.c_uint8 * (w * h * 4))()
        gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
        img = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
        return img[:, :, :3].copy()
    finally:
        if hbmp:    gdi32.DeleteObject(hbmp)
        if hdc_mem: gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)


def scan_frame(sct) -> tuple:
    cap = state["capture"]
    try:
        img_bgr = _grab_without_overlay(cap)
    except Exception:
        region = {"left": cap["left"], "top": cap["top"],
                  "width": cap["width"], "height": cap["height"]}
        img_bgra = np.array(sct.grab(region))
        img_bgr  = img_bgra[:, :, :3]
    img_hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)

    # Разделяем по горизонтали пополам: верх ищет #20b5a1, низ — #36e6bf
    img_h = img_hsv.shape[0]
    mid   = img_h // 2
    top_half = img_hsv[:mid, :]
    bot_half = img_hsv[mid:, :]
    top_raw  = cv2.inRange(top_half, state["cyan_top_lo"], state["cyan_top_hi"])
    bot_raw  = cv2.inRange(bot_half, state["cyan_bot_lo"], state["cyan_bot_hi"])
    cyan_mask_raw = np.zeros(img_hsv.shape[:2], dtype=np.uint8)
    cyan_mask_raw[:mid, :] = top_raw
    cyan_mask_raw[mid:, :] = bot_raw
    h = img_hsv.shape[0]
    YELLOW_STRIP_TOP    = int(h * 0.25)
    YELLOW_STRIP_BOTTOM = int(h * 0.75)
    img_hsv_strip = img_hsv[YELLOW_STRIP_TOP:YELLOW_STRIP_BOTTOM, :]
    yellow_strip  = cv2.inRange(img_hsv_strip, state["yellow_lo"], state["yellow_hi"])
    yellow_mask = np.zeros(img_hsv.shape[:2], dtype=np.uint8)
    yellow_mask[YELLOW_STRIP_TOP:YELLOW_STRIP_BOTTOM, :] = yellow_strip

    k3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    cyan_mask = cv2.morphologyEx(cyan_mask_raw, cv2.MORPH_OPEN, k3)
    k_close_cyan = cv2.getStructuringElement(cv2.MORPH_RECT, (80, 1))
    cyan_mask = cv2.morphologyEx(cyan_mask, cv2.MORPH_CLOSE, k_close_cyan)
    k_open = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    yellow_mask = cv2.morphologyEx(yellow_mask, cv2.MORPH_OPEN, k_open)
    k_close_h = cv2.getStructuringElement(cv2.MORPH_RECT, (8, 1))
    yellow_mask = cv2.morphologyEx(yellow_mask, cv2.MORPH_CLOSE, k_close_h)
    k_vert = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 4))
    yellow_mask = cv2.dilate(yellow_mask, k_vert, iterations=1)
    yellow_mask = cv2.bitwise_and(yellow_mask, cv2.bitwise_not(cyan_mask_raw))

    EDGE_MARGIN = 8
    cyan_mask_raw[:, :EDGE_MARGIN]  = 0; cyan_mask_raw[:, -EDGE_MARGIN:] = 0
    cyan_mask[:, :EDGE_MARGIN]      = 0; cyan_mask[:, -EDGE_MARGIN:]     = 0
    yellow_mask[:, :EDGE_MARGIN]    = 0; yellow_mask[:, -EDGE_MARGIN:]   = 0

    bar_range = find_bar_range(cyan_mask)
    bar_cols  = int(np.sum(np.any(cyan_mask, axis=0)))

    col_any = np.any(yellow_mask, axis=0).astype(np.int8)
    zone_cx_raw = None
    zone_left_x = None
    zone_right_x = None
    if col_any.any():
        padded = np.concatenate(([0], col_any, [0]))
        diff   = np.diff(padded)
        starts = np.where(diff == 1)[0]
        ends   = np.where(diff == -1)[0]
        if len(starts) >= 2:
            centers      = (starts + ends) // 2
            zone_left_x  = int(centers[0])
            zone_right_x = int(centers[-1])
            zone_cx_raw  = (zone_left_x + zone_right_x) // 2
        elif len(starts) == 1:
            zone_left_x  = int(starts[0])
            zone_right_x = int(ends[0])
            zone_cx_raw  = (zone_left_x + zone_right_x) // 2

    ox = cap["left"]
    if bar_range is not None:
        state["bar_x"]     = bar_range[2] + ox
        state["bar_left"]  = bar_range[0] + ox
        state["bar_right"] = bar_range[1] + ox

    if zone_cx_raw is not None:
        new_zone_x  = zone_cx_raw + ox
        now = time.perf_counter()
        STICK_EMA = 1.0
        prev_smooth = state.get("stick_x_smooth", new_zone_x)
        smooth_x = STICK_EMA * new_zone_x + (1.0 - STICK_EMA) * prev_smooth
        state["stick_x_smooth"] = smooth_x
        stick_cx = int(round(smooth_x)) - ox
        state["stick_x"] = int(round(smooth_x))
    else:
        stick_cx = None

    state["bar_w"] = bar_cols
    return bar_range, stick_cx, bar_cols


def wait_for_bar(sct, timeout: float = 30.0) -> bool:
    deadline = time.perf_counter() + timeout
    while state["running"] and time.perf_counter() < deadline:
        bar_range, stick_cx, bar_cols = scan_frame(sct)
        if bar_range is not None and bar_cols > 12:
            return True
        time.sleep(0.05)
    return False


_watchdog_running = False
_watchdog_lock    = threading.Lock()

def watchdog_thread():
    global _watchdog_running
    with _watchdog_lock:
        if _watchdog_running:
            return
        _watchdog_running = True
    try:
        while state["running"]:
            time.sleep(0.5)
            if not state["watchdog_active"]:
                continue
            elapsed = time.perf_counter() - state["last_ad_press"]
            state["watchdog_elapsed"] = elapsed
            if elapsed > WATCHDOG_TIMEOUT:
                pt = state["click_pt"]
                state["last_action"] = f"watchdog! клик→{pt[0]},{pt[1]}"
                state["watchdog_active"] = False
                state["watchdog_fire"]   = True
                _move_and_click(int(pt[0]), int(pt[1]))
                time.sleep(0.5)
                state["last_ad_press"] = time.perf_counter()
    finally:
        with _watchdog_lock:
            _watchdog_running = False


def tracking_loop():
    sct = mss.mss()
    interval = 1.0 / LOOP_FPS
    IDLE = "IDLE"; TRACKING = "TRACKING"; WAIT_ESC = "WAIT_ESC"; WAIT_REAPPEAR = "WAIT_REAPPEAR"
    phase = IDLE
    _phase_deadline = 0.0

    state["watchdog_active"]  = True
    state["watchdog_fire"]    = False
    state["last_ad_press"]    = time.perf_counter()
    state["watchdog_elapsed"] = 0.0
    threading.Thread(target=watchdog_thread, daemon=True).start()

    while state["running"]:
        t0 = time.perf_counter()

        if state["watchdog_fire"]:
            state["watchdog_fire"]   = False
            state["watchdog_active"] = True
            phase = IDLE

        bar_range, stick_cx, bar_cols = scan_frame(sct)
        bar_visible = bar_range is not None and bar_cols > 12

        if phase == IDLE:
            state["watchdog_active"] = True
            state["last_action"] = "F×2…"
            tap_key("f")
            if not state["running"]: break

            deadline = time.perf_counter() + 8.0
            while state["running"] and time.perf_counter() < deadline:
                time.sleep(0.05)
            if not state["running"]: break

            tap_key("f")
            state["last_action"] = "ждём полоску…"
            appeared = wait_for_bar(sct, timeout=30.0)
            if not state["running"]: break
            phase = TRACKING if appeared else IDLE
            continue

        elif phase == TRACKING:
            if not bar_visible:
                phase = WAIT_REAPPEAR
                _phase_deadline = time.perf_counter() + 3.0
                state["last_action"] = "ждём 3с возврата полосы…"
                continue
            bar_left, bar_right, bar_cx = bar_range
            if stick_cx is None:
                state["last_action"] = "?"
            else:
                error_px = float(bar_cx - stick_cx)
                if abs(error_px) <= DEAD_PX:
                    state["last_action"] = f"· e={error_px:+.1f}"
                else:
                    t_norm   = min(abs(error_px) / PULSE_RANGE, 1.0)
                    duration = PULSE_MIN + t_norm * (PULSE_MAX - PULSE_MIN)
                    key      = "d" if error_px > 0 else "a"
                    direction = "D" if key == "d" else "A"
                    state["last_action"] = f"{direction} e={error_px:+.1f} t={duration*1000:.1f}ms"
                    tap_key(key, duration)
                    state["last_ad_press"] = time.perf_counter()
                    time.sleep(PULSE_GAP)

        elif phase == WAIT_REAPPEAR:
            if bar_visible:
                phase = TRACKING
                state["last_action"] = "полоса вернулась"
                continue
            if time.perf_counter() >= _phase_deadline:
                phase = WAIT_ESC
                _phase_deadline = time.perf_counter() + 4.0
                state["last_action"] = "ждём 4с перед Esc…"
            continue

        elif phase == WAIT_ESC:
            state["last_action"] = "ждём 4с перед Esc…"
            while state["running"] and time.perf_counter() < _phase_deadline:
                time.sleep(0.05)
            if not state["running"]: break
            tap_key("esc")
            if not state["running"]: break
            state["last_action"] = "Esc → пауза 2с…"
            deadline = time.perf_counter() + 2.0
            while state["running"] and time.perf_counter() < deadline:
                time.sleep(0.05)
            if not state["running"]: break
            phase = IDLE
            continue

        elapsed = time.perf_counter() - t0
        state["fps_real"] = round(1.0 / max(elapsed, 1e-6), 1)
        sleep_t = interval - elapsed
        if sleep_t > 0:
            time.sleep(sleep_t)

    try: sct.close()
    except: pass


def _set_htgame_mute(mute: bool):
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        sessions = AudioUtilities.GetAllSessions()
        for session in sessions:
            if session.Process and session.Process.name().lower() == "htgame.exe":
                volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                volume.SetMute(1 if mute else 0, None)
    except Exception:
        pass


def _emergency_restore_sound():
    _set_htgame_mute(False)


atexit.register(_emergency_restore_sound)

try:
    signal.signal(signal.SIGTERM, lambda *_: (_emergency_restore_sound(), sys.exit(0)))
    signal.signal(signal.SIGBREAK, lambda *_: (_emergency_restore_sound(), sys.exit(0)))
except (OSError, ValueError):
    pass

_CTRL_HANDLER_TYPE = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)

def _ctrl_handler(ctrl_type):
    _emergency_restore_sound()
    return False

_ctrl_handler_cb = _CTRL_HANDLER_TYPE(_ctrl_handler)
ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_handler_cb, True)


class GDIOverlay:
    WNDPROCTYPE = ctypes.WINFUNCTYPE(
        ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
            ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HANDLE),
        ]
    class PAINTSTRUCT(ctypes.Structure):
        _fields_ = [
            ("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
            ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
            ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32),
        ]

    COL_BLACK      = 0x00000000
    COL_BAR_BG     = 0x00181818
    COL_BAR_BORDER = 0x002a2a2a
    COL_CAP_FRAME  = 0x003a3a3a
    COL_CYAN_BAR   = 0x00c8a000
    COL_DOT_FILL   = 0x00181818
    COL_DOT_RING   = 0x00606060

    PS_SOLID = 0

    def __init__(self):
        self.hwnd = None
        self._wndproc_cb = None
        self._screen_w, self._screen_h = _get_screen_size()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @staticmethod
    def _u32(): return windll.user32
    @staticmethod
    def _gdi32(): return windll.gdi32

    def _run(self):
        u32 = self._u32(); gdi = self._gdi32(); k32 = windll.kernel32
        hInst = k32.GetModuleHandleW(None)
        self._wndproc_cb = self.WNDPROCTYPE(self._wndproc)
        wc = self.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(self.WNDCLASSEXW); wc.style = 0x0003
        wc.lpfnWndProc = ctypes.cast(self._wndproc_cb, ctypes.c_void_p).value
        wc.hInstance = hInst
        wc.hCursor = u32.LoadCursorW(None, ctypes.cast(32512, wintypes.LPCWSTR))
        wc.hbrBackground = None; wc.lpszClassName = "FishBotOverlay"
        if not u32.RegisterClassExW(ctypes.byref(wc)):
            err = k32.GetLastError()
            if err != 1410: return
        self.hwnd = u32.CreateWindowExW(
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TRANSPARENT |
            win32con.WS_EX_LAYERED | win32con.WS_EX_TOOLWINDOW,
            "FishBotOverlay", "FishBot Overlay", win32con.WS_POPUP,
            0, 0, self._screen_w, self._screen_h, None, None, hInst, None
        )
        if not self.hwnd: return
        u32.SetLayeredWindowAttributes(self.hwnd, self.COL_BLACK, 0, win32con.LWA_COLORKEY)
        u32.ShowWindow(self.hwnd, win32con.SW_HIDE)
        u32.SetTimer(self.hwnd, 1, 8, None)
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == 0x000F: self._paint(hwnd); return 0
        elif msg == 0x0113: self._u32().InvalidateRect(hwnd, None, True); return 0
        elif msg == 0x0002: self._u32().PostQuitMessage(0); return 0
        return self._u32().DefWindowProcW(hwnd, msg,
                                          ctypes.c_size_t(wparam), ctypes.c_ssize_t(lparam))

    def _draw_line(self, hdc, x1, y1, x2, y2, color, thick=1):
        gdi = self._gdi32()
        pen = gdi.CreatePen(self.PS_SOLID, thick, color)
        old = gdi.SelectObject(hdc, pen)
        gdi.MoveToEx(hdc, x1, y1, None)
        gdi.LineTo(hdc, x2, y2)
        gdi.SelectObject(hdc, old)
        gdi.DeleteObject(pen)

    def _fill_round_rect(self, hdc, x1, y1, x2, y2, rx, ry,
                         fill_color, border_color, border_thick=1):
        gdi = self._gdi32()
        brush = gdi.CreateSolidBrush(fill_color)
        pen   = gdi.CreatePen(self.PS_SOLID, border_thick, border_color)
        old_brush = gdi.SelectObject(hdc, brush)
        old_pen   = gdi.SelectObject(hdc, pen)
        gdi.RoundRect(hdc, x1, y1, x2, y2, rx, ry)
        gdi.SelectObject(hdc, old_brush)
        gdi.SelectObject(hdc, old_pen)
        gdi.DeleteObject(brush)
        gdi.DeleteObject(pen)

    def _draw_round_rect(self, hdc, x1, y1, x2, y2, rx, ry, color, thick=1):
        gdi = self._gdi32()
        pen   = gdi.CreatePen(self.PS_SOLID, thick, color)
        brush = gdi.GetStockObject(5)
        old_pen   = gdi.SelectObject(hdc, pen)
        old_brush = gdi.SelectObject(hdc, brush)
        gdi.RoundRect(hdc, x1, y1, x2, y2, rx, ry)
        gdi.SelectObject(hdc, old_pen)
        gdi.SelectObject(hdc, old_brush)
        gdi.DeleteObject(pen)

    def _fill_ellipse(self, hdc, cx, cy, r, fill_color, border_color=None, thick=1):
        gdi = self._gdi32()
        brush = gdi.CreateSolidBrush(fill_color)
        pen   = gdi.CreatePen(self.PS_SOLID, thick,
                               border_color if border_color is not None else fill_color)
        old_brush = gdi.SelectObject(hdc, brush)
        old_pen   = gdi.SelectObject(hdc, pen)
        gdi.Ellipse(hdc, cx-r, cy-r, cx+r, cy+r)
        gdi.SelectObject(hdc, old_brush)
        gdi.SelectObject(hdc, old_pen)
        gdi.DeleteObject(brush)
        gdi.DeleteObject(pen)

    def _draw_ellipse(self, hdc, cx, cy, r, color, thick=1):
        gdi = self._gdi32()
        pen   = gdi.CreatePen(self.PS_SOLID, thick, color)
        brush = gdi.GetStockObject(5)
        old_pen   = gdi.SelectObject(hdc, pen)
        old_brush = gdi.SelectObject(hdc, brush)
        gdi.Ellipse(hdc, cx-r, cy-r, cx+r, cy+r)
        gdi.SelectObject(hdc, old_pen)
        gdi.SelectObject(hdc, old_brush)
        gdi.DeleteObject(pen)

    def _draw_click_dot(self, hdc, px, py):
        self._fill_ellipse(hdc, px, py, 9, self.COL_DOT_FILL, self.COL_DOT_FILL)
        self._draw_ellipse(hdc, px, py, 7, self.COL_DOT_RING, thick=1)
        self._fill_ellipse(hdc, px, py, 2, self.COL_DOT_RING, self.COL_DOT_RING)

    def _paint(self, hwnd):
        gdi = self._gdi32(); u32 = self._u32()
        ps  = self.PAINTSTRUCT()
        hdc = u32.BeginPaint(hwnd, ctypes.byref(ps))

        rc_full = wintypes.RECT(0, 0, self._screen_w, self._screen_h)
        brush_black = gdi.CreateSolidBrush(self.COL_BLACK)
        u32.FillRect(hdc, ctypes.byref(rc_full), brush_black)
        gdi.DeleteObject(brush_black)

        cap = state["capture"]
        bx, by, bw, bh = cap["left"], cap["top"], cap["width"], cap["height"]

        PAD = 3
        RX  = 10

        self._fill_round_rect(
            hdc,
            bx - PAD, by - PAD, bx + bw + PAD, by + bh + PAD,
            RX * 2, RX * 2,
            self.COL_BAR_BG, self.COL_BAR_BORDER, border_thick=1
        )
        self._fill_round_rect(
            hdc,
            bx, by, bx + bw, by + bh,
            RX * 2, RX * 2,
            self.COL_BLACK, self.COL_CAP_FRAME, border_thick=1
        )

        bar_left  = state["bar_left"]
        bar_right = state["bar_right"]
        bar_w     = state["bar_w"]
        if bar_w > 0 and bar_right > bar_left:
            bar_inner_h = bh - 2
            bar_rx = bar_inner_h
            self._draw_round_rect(
                hdc,
                bar_left, by + 1, bar_right, by + bh - 1,
                bar_rx, bar_rx,
                self.COL_CYAN_BAR, thick=1
            )
            bar_cx = (bar_left + bar_right) // 2
            self._draw_line(hdc, bar_cx, by + 2, bar_cx, by + bh - 2,
                            self.COL_CYAN_BAR, thick=1)

        pt = state["click_pt"]
        self._draw_click_dot(hdc, int(pt[0]), int(pt[1]))

        u32.EndPaint(hwnd, ctypes.byref(ps))

    def set_visible(self, visible: bool):
        if self.hwnd:
            cmd = win32con.SW_SHOW if visible else win32con.SW_HIDE
            self._u32().ShowWindow(self.hwnd, cmd)

    def destroy(self):
        if self.hwnd:
            self._u32().PostMessageW(self.hwnd, 0x0002, 0, 0)


SVG_PLAY = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v14l11-7-11-7z"/></svg>"""
SVG_STOP = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="1.5"/></svg>"""
SVG_GEAR = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>"""
SVG_CLOSE = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>"""
SVG_TIMER_PLAY = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>"""
SVG_TIMER_CANCEL = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>"""


def _svg_icon(svg_bytes: bytes, size: int, color: str) -> QIcon:
    colored = svg_bytes.replace(b'stroke="currentColor"', f'stroke="{color}"'.encode())
    colored = colored.replace(b'fill="currentColor"', f'fill="{color}"'.encode())
    renderer = QSvgRenderer(colored)
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    return QIcon(pm)


def _make_icon_button(svg_bytes, size_px, icon_sz,
                      c_normal, c_hover, c_active=None,
                      border_normal="", border_hover="", border_active="",
                      circle=False, obj_name="", checkable=False):
    if c_active is None:
        c_active = c_hover
    btn = QPushButton()
    if obj_name:
        btn.setObjectName(obj_name)
    btn.setFixedSize(size_px, size_px)
    btn.setCheckable(checkable)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFlat(True)
    btn.setFocusPolicy(Qt.NoFocus)
    btn._active = False

    def _refresh(hover=False, active=False):
        if active:
            icon_c   = c_active
            border_c = border_active if border_active else c_active
        elif hover:
            icon_c   = c_hover
            border_c = border_hover if border_hover else c_hover
        else:
            icon_c   = c_normal
            border_c = border_normal if border_normal else "#2a2a2a"
        btn.setIcon(_svg_icon(svg_bytes, icon_sz, icon_c))
        btn.setIconSize(QSize(icon_sz, icon_sz))
        if circle:
            r = size_px // 2
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: 1.8px solid {border_c};
                    border-radius: {r}px;
                    padding: 0px;
                }}
            """)
        else:
            if active:
                bg = "#252525"
            elif hover:
                bg = "rgba(255,255,255,0.07)"
            else:
                bg = "transparent"
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {bg};
                    border: none;
                    border-radius: 10px;
                    padding: 0px;
                }}
            """)

    btn._refresh = _refresh
    _refresh()

    class _Filter(QObject):
        def eventFilter(self_, obj, ev):
            if ev.type() == QEvent.Enter:
                _refresh(hover=True,  active=btn._active)
            elif ev.type() == QEvent.Leave:
                _refresh(hover=False, active=btn._active)
            return False

    btn._ef = _Filter(btn)
    btn.installEventFilter(btn._ef)

    def _set_active(val: bool):
        btn._active = val
        _refresh(active=val)

    btn.setActive = _set_active
    return btn


C_BG       = "#111111"
C_CARD     = "#181818"
C_BORDER   = "#2a2a2a"
C_FG       = "#e0e0e0"
C_DIM      = "#555555"
C_HINT     = "#404040"
C_ACCENT   = "#1a6ed8"
C_ACCENT_H = "#2280f0"
C_YELLOW   = "#f0c040"


def _num_input(width=64, val=0):
    e = QLineEdit(str(val))
    e.setFixedSize(width, 36)
    e.setValidator(QIntValidator(0, 9999))
    e.setAlignment(Qt.AlignCenter)
    e.setFont(QFont("Consolas", 12))
    e.setFocusPolicy(Qt.ClickFocus)
    e.setStyleSheet(f"""
        QLineEdit {{
            background: #1c1c1c;
            border: 1px solid {C_BORDER};
            border-radius: 8px;
            color: {C_FG};
            padding: 0 4px;
        }}
        QLineEdit:focus {{
            border-color: {C_ACCENT};
        }}
    """)
    return e


def _dim_lbl(text):
    l = QLabel(text)
    l.setFont(QFont("Consolas", 10))
    l.setStyleSheet(f"color: {C_DIM}; background: transparent;")
    l.setFixedHeight(36)
    l.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
    return l


def _section_lbl(text):
    l = QLabel(text)
    l.setFont(QFont("Consolas", 9))
    l.setStyleSheet(f"color: {C_HINT}; background: transparent; letter-spacing: 1px;")
    return l


def _vline():
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setFixedWidth(1)
    f.setFixedHeight(28)
    f.setStyleSheet(f"background: {C_BORDER}; border: none;")
    return f


class OpaquePanel(QWidget):
    def __init__(self, bg=C_CARD, border=C_BORDER, radius=12, parent=None):
        super().__init__(parent)
        self._bg     = QColor(bg)
        self._border = QColor(border)
        self._radius = radius

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(self._border)
        p.setBrush(self._bg)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.drawRoundedRect(r, self._radius, self._radius)
        p.end()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("fishing bot")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self._drag_pos      = None
        self._settings_open = False
        self._elapsed_secs  = 0
        self._countdown_n   = 0

        self.overlay = GDIOverlay()
        self._build_ui()

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(1000)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        bar = OpaquePanel(C_CARD, C_BORDER, 14)
        bar.setFixedHeight(68)
        bar_l = QHBoxLayout(bar)
        bar_l.setContentsMargins(16, 0, 16, 0)
        bar_l.setSpacing(0)

        ICON = 22
        self.btn_play = _make_icon_button(
            SVG_PLAY, 44, ICON,
            c_normal="#1d6b35", c_hover="#2ecc60",  c_active="#22c55e",
            border_normal="#1a4d28", border_hover="#2ecc60", border_active="#22c55e",
            circle=True)
        self.btn_play.clicked.connect(self._on_play)
        bar_l.addWidget(self.btn_play)

        bar_l.addSpacing(10)

        self.btn_stop = _make_icon_button(
            SVG_STOP, 44, ICON,
            c_normal="#6b1d1d", c_hover="#ff4d4d",  c_active="#ef4444",
            border_normal="#4d1a1a", border_hover="#ff4d4d", border_active="#ef4444",
            circle=True)
        self.btn_stop.clicked.connect(self._on_stop)
        bar_l.addWidget(self.btn_stop)

        bar_l.addSpacing(14)
        bar_l.addWidget(_vline())
        bar_l.addStretch(1)

        self.display = QLabel("--:--:--")
        self.display.setFont(QFont("Consolas", 22, QFont.Bold))
        self.display.setStyleSheet(f"color: {C_DIM}; background: transparent; letter-spacing: 2px;")
        self.display.setAlignment(Qt.AlignVCenter | Qt.AlignCenter)
        self.display.setFixedHeight(68)
        bar_l.addWidget(self.display)

        bar_l.addStretch(1)
        bar_l.addWidget(_vline())
        bar_l.addSpacing(10)

        self.btn_gear = _make_icon_button(
            SVG_GEAR, 40, 20,
            c_normal="#505050", c_hover="#d0d0d0", c_active="#ffffff",
            circle=False, checkable=True)
        self.btn_gear.clicked.connect(self._toggle_settings)
        bar_l.addWidget(self.btn_gear)

        bar_l.addSpacing(10)
        bar_l.addWidget(_vline())
        bar_l.addSpacing(10)

        btn_close = _make_icon_button(
            SVG_CLOSE, 36, 18,
            c_normal="#505050", c_hover="#d0d0d0", c_active="#d0d0d0",
            circle=False)
        btn_close.clicked.connect(self._on_close)
        bar_l.addWidget(btn_close)

        root.addWidget(bar)

        self.settings_panel = _SettingsPanel()
        self.settings_panel.setVisible(False)
        root.addWidget(self.settings_panel)

        self.setMinimumWidth(500)

    def _on_play(self):
        if state["running"] or self._countdown_n > 0: return
        self.btn_play.setActive(True)
        self.btn_stop.setActive(False)
        self._countdown_n = 2
        self._do_countdown()

    def _do_countdown(self):
        if self._countdown_n > 0:
            self._countdown_n -= 1
            QTimer.singleShot(1000, self._do_countdown)
        else:
            self._countdown_n = 0
            state["running"] = True
            threading.Thread(target=tracking_loop, daemon=True).start()

    def _on_stop(self):
        state["running"] = False
        state["watchdog_active"]  = False
        state["watchdog_elapsed"] = 0.0
        self.btn_play.setActive(False)
        self.btn_stop.setActive(False)
        self._refresh_display()

    def _toggle_settings(self):
        self._settings_open = not self._settings_open
        self.btn_gear.setActive(self._settings_open)
        self.settings_panel.setVisible(self._settings_open)
        self.adjustSize()

    def _on_close(self):
        state["running"] = False
        try: self.settings_panel.restore_sound()
        except: pass
        try: self.overlay.destroy()
        except: pass
        QApplication.quit()

    def _refresh_display(self):
        sd = self.settings_panel._shutdown_at
        if sd is not None:
            rem = sd - time.time()
            if rem > 0:
                h = int(rem // 3600)
                m = int((rem % 3600) // 60)
                s = int(rem % 60)
                self.display.setText(f"{h:02d}:{m:02d}:{s:02d}")
                self.display.setStyleSheet(
                    f"color: {C_YELLOW}; background: transparent; letter-spacing: 2px;")
                return
        self.display.setText("--:--:--")
        self.display.setStyleSheet(
            f"color: {C_DIM}; background: transparent; letter-spacing: 2px;")

    def _tick(self):
        self._refresh_display()
        self.settings_panel.tick()


class _SettingsPanel(OpaquePanel):
    _shutdown_at = None

    def mousePressEvent(self, e):
        focused = QApplication.focusWidget()
        if focused and isinstance(focused, QLineEdit):
            focused.clearFocus()
        super().mousePressEvent(e)

    def __init__(self):
        super().__init__(C_CARD, C_BORDER, 12)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(12)

        wd_hdr = QHBoxLayout()
        wd_hdr.setContentsMargins(0, 0, 0, 0)
        wd_hdr.setSpacing(0)
        wd_hdr.addWidget(_section_lbl("watchdog  (60s без нажатий → клик)"))
        wd_hdr.addStretch(1)
        self.lbl_wd = QLabel("WD: —")
        self.lbl_wd.setFont(QFont("Consolas", 10))
        self.lbl_wd.setStyleSheet(f"color: {C_DIM}; background: transparent;")
        wd_hdr.addWidget(self.lbl_wd)
        root.addLayout(wd_hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {C_BORDER}; border: none;")
        root.addWidget(sep)

        root.addWidget(_section_lbl("shutdown timer"))

        tmr_row = QHBoxLayout()
        tmr_row.setSpacing(8)
        tmr_row.setContentsMargins(0, 0, 0, 0)

        self.inp_h = _num_input(64, 0)
        self.inp_m = _num_input(64, 0)
        self.inp_s = _num_input(64, 0)

        tmr_row.addWidget(_dim_lbl("h"))
        tmr_row.addWidget(self.inp_h)
        tmr_row.addWidget(_dim_lbl("m"))
        tmr_row.addWidget(self.inp_m)
        tmr_row.addWidget(_dim_lbl("s"))
        tmr_row.addWidget(self.inp_s)
        tmr_row.addStretch(1)

        self.btn_tplay = QPushButton()
        self.btn_tplay.setFixedSize(40, 40)
        self.btn_tplay.setCursor(Qt.PointingHandCursor)
        self.btn_tplay.setFlat(True)
        self.btn_tplay.setFocusPolicy(Qt.NoFocus)

        def _tplay_style(hover=False, enabled=True):
            icon_c = "#ffffff" if hover and enabled else ("#333333" if not enabled else "#666666")
            bg     = "#252525" if hover and enabled else "transparent"
            bdr    = "#383838" if hover and enabled else "#222222"
            self.btn_tplay.setIcon(_svg_icon(SVG_TIMER_PLAY, 18, icon_c))
            self.btn_tplay.setIconSize(QSize(18, 18))
            self.btn_tplay.setStyleSheet(f"""
                QPushButton {{
                    background: {bg};
                    border: 1px solid {bdr};
                    border-radius: 10px;
                    padding: 0px;
                }}
            """)

        _tplay_style()
        self._tplay_style = _tplay_style

        class _TPlayFilter(QObject):
            def eventFilter(self_, obj, ev):
                if ev.type() == QEvent.Enter:
                    _tplay_style(hover=True, enabled=self.btn_tplay.isEnabled())
                elif ev.type() == QEvent.Leave:
                    _tplay_style(hover=False, enabled=self.btn_tplay.isEnabled())
                return False

        self.btn_tplay._ef = _TPlayFilter(self.btn_tplay)
        self.btn_tplay.installEventFilter(self.btn_tplay._ef)
        self.btn_tplay.clicked.connect(self._timer_start)
        tmr_row.addWidget(self.btn_tplay)

        self.btn_tcancel = QPushButton()
        self.btn_tcancel.setFixedSize(40, 40)
        self.btn_tcancel.setCursor(Qt.PointingHandCursor)
        self.btn_tcancel.setFlat(True)
        self.btn_tcancel.setFocusPolicy(Qt.NoFocus)

        def _tcancel_style(hover=False, enabled=True):
            icon_c = "#cc4444" if hover and enabled else ("#333333" if not enabled else "#555555")
            bg     = "#2e1a1a" if hover and enabled else "transparent"
            bdr    = "#6b2a2a" if hover and enabled else "#222222"
            self.btn_tcancel.setIcon(_svg_icon(SVG_TIMER_CANCEL, 16, icon_c))
            self.btn_tcancel.setIconSize(QSize(16, 16))
            self.btn_tcancel.setStyleSheet(f"""
                QPushButton {{
                    background: {bg};
                    border: 1px solid {bdr};
                    border-radius: 10px;
                    padding: 0px;
                }}
            """)

        _tcancel_style()
        self._tcancel_style = _tcancel_style

        class _TCancelFilter(QObject):
            def eventFilter(self_, obj, ev):
                if ev.type() == QEvent.Enter:
                    _tcancel_style(hover=True, enabled=self.btn_tcancel.isEnabled())
                elif ev.type() == QEvent.Leave:
                    _tcancel_style(hover=False, enabled=self.btn_tcancel.isEnabled())
                return False

        self.btn_tcancel._ef = _TCancelFilter(self.btn_tcancel)
        self.btn_tcancel.installEventFilter(self.btn_tcancel._ef)
        self.btn_tcancel.setEnabled(False)
        self.btn_tcancel.clicked.connect(self._timer_cancel)
        tmr_row.addWidget(self.btn_tcancel)

        root.addLayout(tmr_row)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setFixedHeight(1)
        sep2.setStyleSheet(f"background: {C_BORDER}; border: none;")
        root.addWidget(sep2)

        ovl_row = QHBoxLayout()
        ovl_row.setSpacing(10)
        ovl_row.setContentsMargins(0, 0, 0, 0)

        self._ovl_checked = True
        self.btn_ovl = QPushButton()
        self.btn_ovl.setFixedSize(36, 36)
        self.btn_ovl.setCursor(Qt.PointingHandCursor)
        self.btn_ovl.setFlat(True)
        self.btn_ovl.setFocusPolicy(Qt.NoFocus)

        def _ovl_btn_style(hover=False):
            bg  = "#303030" if hover else "#252525"
            bdr = "#404040" if hover else "#303030"
            self.btn_ovl.setStyleSheet(f"""
                QPushButton {{
                    background: {bg};
                    border: 1px solid {bdr};
                    border-radius: 8px;
                    padding: 0px;
                }}
            """)

        _ovl_btn_style()

        class _OvlFilter(QObject):
            def eventFilter(self_, obj, ev):
                if ev.type() == QEvent.Enter:   _ovl_btn_style(hover=True)
                elif ev.type() == QEvent.Leave: _ovl_btn_style(hover=False)
                return False

        self.btn_ovl._ef = _OvlFilter(self.btn_ovl)
        self.btn_ovl.installEventFilter(self.btn_ovl._ef)
        self.btn_ovl.clicked.connect(self._toggle_overlay_btn)
        ovl_row.addWidget(self.btn_ovl)

        lbl_ovl = QLabel("Отключить оверлей")
        lbl_ovl.setFont(QFont("Consolas", 11))
        lbl_ovl.setStyleSheet(f"color: {C_FG}; background: transparent;")
        ovl_row.addWidget(lbl_ovl)

        ovl_row.addStretch(1)
        root.addLayout(ovl_row)

        mute_row = QHBoxLayout()
        mute_row.setSpacing(10)
        mute_row.setContentsMargins(0, 0, 0, 0)

        self._mute_checked = False
        self.btn_mute = QPushButton()
        self.btn_mute.setFixedSize(36, 36)
        self.btn_mute.setCursor(Qt.PointingHandCursor)
        self.btn_mute.setFlat(True)
        self.btn_mute.setFocusPolicy(Qt.NoFocus)

        def _mute_btn_style(hover=False):
            bg  = "#303030" if hover else "#252525"
            bdr = "#404040" if hover else "#303030"
            self.btn_mute.setStyleSheet(f"""
                QPushButton {{
                    background: {bg};
                    border: 1px solid {bdr};
                    border-radius: 8px;
                    padding: 0px;
                }}
            """)

        _mute_btn_style()

        class _MuteFilter(QObject):
            def eventFilter(self_, obj, ev):
                if ev.type() == QEvent.Enter:   _mute_btn_style(hover=True)
                elif ev.type() == QEvent.Leave: _mute_btn_style(hover=False)
                return False

        self.btn_mute._ef = _MuteFilter(self.btn_mute)
        self.btn_mute.installEventFilter(self.btn_mute._ef)
        self.btn_mute.clicked.connect(self._toggle_mute_btn)
        mute_row.addWidget(self.btn_mute)

        lbl_mute = QLabel("Отключить звук")
        lbl_mute.setFont(QFont("Consolas", 11))
        lbl_mute.setStyleSheet(f"color: {C_FG}; background: transparent;")
        mute_row.addWidget(lbl_mute)

        mute_row.addStretch(1)
        root.addLayout(mute_row)

        self._update_overlay_icon(True)
        self._update_mute_icon(False)

    def _timer_start(self):
        try:
            total = (int(self.inp_h.text() or 0) * 3600
                   + int(self.inp_m.text() or 0) * 60
                   + int(self.inp_s.text() or 0))
        except ValueError: return
        if total <= 0:
            return
        self._shutdown_at = time.time() + total
        self.btn_tplay.setEnabled(False)
        self._tplay_style(hover=False, enabled=False)
        self.btn_tcancel.setEnabled(True)
        self._tcancel_style(hover=False, enabled=True)

    def _timer_cancel(self):
        self._shutdown_at = None
        self.btn_tplay.setEnabled(True)
        self._tplay_style(hover=False, enabled=True)
        self.btn_tcancel.setEnabled(False)
        self._tcancel_style(hover=False, enabled=False)

    def _toggle_overlay_btn(self):
        self._ovl_checked = not self._ovl_checked
        self._on_overlay_toggle(self._ovl_checked)

    def _on_overlay_toggle(self, checked: bool):
        from PySide6.QtWidgets import QApplication
        for w in QApplication.topLevelWidgets():
            if hasattr(w, 'overlay'):
                w.overlay.set_visible(not checked)
                break
        self._update_overlay_icon(checked)

    def _update_overlay_icon(self, hidden: bool):
        icon_sz = 18
        if hidden:
            svg = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>"""
        else:
            svg = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>"""
        renderer = QSvgRenderer(svg)
        pm = QPixmap(icon_sz, icon_sz)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        self.btn_ovl.setIcon(QIcon(pm))
        self.btn_ovl.setIconSize(QSize(icon_sz, icon_sz))

    def _toggle_mute_btn(self):
        self._mute_checked = not self._mute_checked
        _set_htgame_mute(self._mute_checked)
        self._update_mute_icon(self._mute_checked)

    def _update_mute_icon(self, muted: bool):
        icon_sz = 18
        if muted:
            svg = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>"""
        else:
            svg = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#ef4444" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>"""
        renderer = QSvgRenderer(svg)
        pm = QPixmap(icon_sz, icon_sz)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        self.btn_mute.setIcon(QIcon(pm))
        self.btn_mute.setIconSize(QSize(icon_sz, icon_sz))

    def restore_sound(self):
        if self._mute_checked:
            _set_htgame_mute(False)

    def tick(self):
        if state.get("running") and state.get("watchdog_active"):
            wd_e = state.get("watchdog_elapsed", 0.0)
            rem  = max(0.0, WATCHDOG_TIMEOUT - wd_e)
            self.lbl_wd.setText(f"WD: {rem:.0f}s")
            color = "#ef4444" if rem < 10 else C_DIM
            self.lbl_wd.setStyleSheet(f"color: {color}; background: transparent;")
        else:
            self.lbl_wd.setText("WD: —")
            self.lbl_wd.setStyleSheet(f"color: {C_DIM}; background: transparent;")

        if self._shutdown_at is None: return
        rem = self._shutdown_at - time.time()
        if rem <= 0:
            state["running"] = False
            self._shutdown_at = None
            QTimer.singleShot(1500, lambda: os.system("shutdown /s /t 0"))


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
