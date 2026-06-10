# =========================================================================
# SECTION: S1_header
# =========================================================================

"""OmniChat — standalone FFXI chat panel overlay (pygame).

Extracted from OmniWatch's chat subsystem so it can run as its own
addon, alongside OmniWatch or without it. The routing rules, filter
engine, tabs, composer, and rendering are the same code paths as
OmniWatch's chat panel — config lives in its own folder so the two
never fight over files or ports.

Pairs with OmniChat.lua (Windower addon), which captures chat /
combat / status events from packets and streams them here.

Wire:
  lua → python : UDP 127.0.0.1:5113  CHAT_BATCH event stream
                 (same format as OmniWatch — see chat/drain.lua)
                 plus a 1 Hz "JOB\t<job>\t<char>" heartbeat used for
                 per-job routing reloads and multibox chat pinning.
  python → lua : UDP 127.0.0.1:5111  bare windower console commands
                 (composer sends "input /p hello").

Config dir: %APPDATA%/OmniChat (or ~/.omnichat off-Windows):
  omnichat_settings.json            window/panel/tab preferences
  omnichat_chat_routing.json        global routing config
  omnichat_chat_routing-<JOB>.json  per-job overrides
On first run, routing configs are imported from an existing
%APPDATA%/OmniWatch install if present, so existing rules carry over.

Window model: the OS window IS the chat panel (borderless). Drag the
top header strip to move it; drag the bottom-right corner grip to
resize. Always-on-top is on by default (it's an overlay).
"""

import sys
import os
import json
import math
import time
import base64
import socket
import collections as _collections

import pygame

OMNICHAT_VERSION = "1.0.0"

# ── Config / settings ──────────────────────────────────────────────────────
# One flat JSON file. Mirrors the keys OmniWatch's chat panel reads so
# the extracted code (setting("chat_font_size"), hidden_chat_tabs, ...)
# works unchanged.

if sys.platform == "win32":
    USER_DIR = os.path.join(os.environ.get("APPDATA",
                            os.path.expanduser("~")), "OmniChat")
else:
    USER_DIR = os.path.join(os.path.expanduser("~"), ".omnichat")
os.makedirs(USER_DIR, exist_ok=True)
SETTINGS_DIR = USER_DIR

# ── Session log (PyInstaller --noconsole support) ─────────────────────────
# Frozen --noconsole builds have sys.stdout/stderr = None, so the first
# print() would raise AttributeError and kill the overlay. Redirect all
# console output to a rotating session log under <config>/logs/ in that
# case (and any other case where stdout is missing). Keeps the last 5
# session logs. Dev runs from a terminal are untouched.
def _open_session_log():
    if sys.stdout is not None and sys.stderr is not None \
            and not getattr(sys, "frozen", False):
        return
    try:
        log_dir = os.path.join(USER_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        # Rotate: keep the 5 most recent session logs.
        try:
            old = sorted(f for f in os.listdir(log_dir)
                         if f.startswith("session_") and f.endswith(".log"))
            for f in old[:-4]:
                os.remove(os.path.join(log_dir, f))
        except Exception:
            pass
        import datetime
        path = os.path.join(log_dir, "session_"
                            + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                            + ".log")
        f = open(path, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
        print(f"[OmniChat] session log opened: {path}")
    except Exception:
        # Last resort: swallow output rather than crash on print().
        class _Null:
            def write(self, *_a): pass
            def flush(self): pass
        if sys.stdout is None:
            sys.stdout = _Null()
        if sys.stderr is None:
            sys.stderr = _Null()


_open_session_log()

SETTINGS_FILE = os.path.join(SETTINGS_DIR, "omnichat_settings.json")

# Minimal settings schema. Only entries the chat code actually reads;
# SETTINGS_BY_KEY membership is checked by _chat_font_sizes().
SETTINGS_SCHEMA = [
    {"key": "chat_font_size",   "default": "medium"},   # small|medium|large
    {"key": "hidden_chat_tabs", "default": []},
    {"key": "chat_main_char",   "default": ""},         # multibox chat pin
    {"key": "always_on_top",    "default": True},
    {"key": "window_opacity",   "default": 100},      # BOX opacity %, 20-100
    {"key": "chat_theme",       "default": "dark"},   # dark | light
    {"key": "no_focus_steal",   "default": True},     # clicks don't take game focus
    {"key": "global_ui_scale",  "default": 1.0},      # 0.5 - 2.0
    {"key": "window_pos",       "default": None},       # [x, y] or None
    {"key": "chat_panel_dims",  "default": [800, 280]},
    {"key": "chat_composer_visible", "default": True},
]
SETTINGS_BY_KEY = {s["key"]: s for s in SETTINGS_SCHEMA}


def load_settings():
    out = {s["key"]: s["default"] for s in SETTINGS_SCHEMA}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            out.update(data)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[OmniChat] settings load failed ({e}); using defaults")
    return out


settings = load_settings()


def save_settings():
    try:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        print(f"[OmniChat] settings save failed: {e}")


def setting(key):
    if key in settings:
        return settings[key]
    s = SETTINGS_BY_KEY.get(key)
    return s["default"] if s else None


def set_setting(key, value):
    settings[key] = value
    save_settings()


def _import_omniwatch_routing_configs():
    """First-run convenience: copy OmniWatch's routing JSONs into the
    OmniChat config dir (renamed) so existing rules/filters carry over.
    Never overwrites — only fills gaps. Safe to call every launch."""
    import shutil
    import glob
    if sys.platform == "win32":
        ow_dir = os.path.join(os.environ.get("APPDATA",
                              os.path.expanduser("~")), "OmniWatch")
    else:
        ow_dir = os.path.join(os.path.expanduser("~"), ".omniwatch")
    if not os.path.isdir(ow_dir):
        return
    copied = []
    for src in glob.glob(os.path.join(ow_dir, "omnichat_chat_routing*.json")):
        fn = os.path.basename(src).replace("omnichat_chat_routing",
                                           "omnichat_chat_routing")
        dst = os.path.join(SETTINGS_DIR, fn)
        if not os.path.exists(dst):
            try:
                shutil.copy2(src, dst)
                copied.append(fn)
            except Exception as e:
                print(f"[OmniChat] routing import {fn}: {e!r}")
    if copied:
        print(f"[OmniChat] imported {len(copied)} routing config(s) "
              f"from OmniWatch: {', '.join(copied)}")


_import_omniwatch_routing_configs()

# =========================================================================
# SECTION: S2_window
# =========================================================================

# ── DPI awareness ──────────────────────────────────────────────────────────
# Same rationale as OmniWatch: opt into per-monitor-V2 DPI awareness so
# Win32 coordinate APIs return physical pixels (window positioning +
# multi-monitor mixed DPI). Must run before any window is created.
if sys.platform == "win32":
    try:
        import ctypes
        from ctypes import wintypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                ctypes.windll.user32.SetProcessDPIAware()
    except Exception as _e:
        print(f"[OmniChat] DPI awareness setup failed (non-fatal): {_e!r}")


# Force SDL's software (GDI) window-surface presentation. SDL2 defaults
# to an accelerated (Direct3D) framebuffer for the display surface, and
# DWM does NOT apply layered-window color keying to D3D-presented
# content — which made the box-opacity transparency a silent no-op.
# The GDI path honors LWA_COLORKEY, and a software blit of a chat-panel
# sized window at 60fps is negligible. Must be set before pygame.init().
os.environ.setdefault("SDL_FRAMEBUFFER_ACCELERATION", "0")

pygame.init()

# Unicode text input for the composer field (CJK via IME etc.).
try:
    pygame.key.start_text_input()
    pygame.key.set_repeat(400, 40)
except (AttributeError, pygame.error):
    pass

# Window = the chat panel. Borderless; sized to the persisted panel
# dims. Position restored after creation via SetWindowPos (pygame's
# SDL_VIDEO_WINDOW_POS env var must be set before set_mode).
chat_panel_dims = list(setting("chat_panel_dims") or [800, 280])
chat_panel_dims[0] = max(240, int(chat_panel_dims[0]))
chat_panel_dims[1] = max(80,  int(chat_panel_dims[1]))

_saved_win_pos = setting("window_pos")
if (isinstance(_saved_win_pos, (list, tuple)) and len(_saved_win_pos) == 2):
    os.environ["SDL_VIDEO_WINDOW_POS"] = (
        f"{int(_saved_win_pos[0])},{int(_saved_win_pos[1])}")

screen = pygame.display.set_mode(tuple(chat_panel_dims), pygame.NOFRAME)
pygame.display.set_caption("OmniChat")

# Window icon (best-effort; looks next to the script/exe).
try:
    if getattr(sys, "frozen", False):
        _self_dir_for_icon = os.path.dirname(os.path.abspath(sys.executable))
    else:
        _self_dir_for_icon = os.path.dirname(os.path.abspath(__file__))
    for _rel in ("icons/ui/OmniChat.png", "icon.png"):
        _icon_path = os.path.join(_self_dir_for_icon, _rel)
        if os.path.exists(_icon_path):
            _icon = pygame.image.load(_icon_path)
            try:
                _icon = pygame.transform.smoothscale(_icon, (32, 32))
            except Exception:
                pass
            pygame.display.set_icon(_icon)
            break
except Exception as _e:
    print(f"[OmniChat] window icon load failed: {_e!r}")


def _get_hwnd():
    """pygame window HWND (Windows only; None elsewhere)."""
    if sys.platform != "win32":
        return None
    try:
        info = pygame.display.get_wm_info()
        return info.get("window") or info.get("hwnd")
    except Exception:
        return None


def _apply_always_on_top(enabled):
    """HWND_TOPMOST / HWND_NOTOPMOST via SetWindowPos. Windows-only."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = _get_hwnd()
        if not hwnd:
            return
        SetWindowPos = ctypes.windll.user32.SetWindowPos
        SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                 ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int,
                                 ctypes.c_uint]
        SetWindowPos.restype = wintypes.BOOL
        HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
        SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
        target = HWND_TOPMOST if enabled else HWND_NOTOPMOST
        SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(target),
                     0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
    except Exception as e:
        print(f"[OmniChat] always-on-top failed: {e!r}")


_apply_always_on_top(bool(setting("always_on_top")))


# ── Box opacity (background translucency; text stays solid) ──────────────
# Whole-window opacity (SDL_SetWindowOpacity) fades TEXT along with the
# box, which makes chat unreadable long before the background is
# pleasantly see-through. Instead: a Win32 layered-window COLORKEY makes
# one exact color fully transparent, and the panel background is drawn
# as an ordered-dither mix of the background color and that key color.
# The dither fraction = the opacity setting, so the box becomes
# translucent against the game while glyphs stay fully opaque.
#
# _OC_KEY is near-black so the antialiased edges of text (which blend
# toward whatever is underneath) blend toward near-black — visually a
# subtle dark outline instead of color fringing.
_OC_KEY = (1, 2, 3)


def _box_opacity_pct():
    try:
        pct = int(setting("window_opacity") or 100)
    except (TypeError, ValueError):
        pct = 100
    return max(20, min(100, pct))


def _apply_box_opacity(pct):
    """Persist-side clamp + Win32 layered-colorkey toggle. At 100% the
    layered style is removed entirely (zero overhead, exact pre-feature
    behavior). Below 100% the key color becomes transparent and the
    dithered background (see _chat_blit_panel_bg) does the rest."""
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        pct = 100
    pct = max(20, min(100, pct))
    if os.environ.get("SDL_VIDEODRIVER") == "dummy":
        return pct          # headless test driver: no real window
    if sys.platform != "win32":
        return pct          # dither still draws; no true transparency
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = _get_hwnd()
        if not hwnd:
            print("[OmniChat] box opacity: no HWND")
            return pct
        # use_last_error=True is required for reliable GetLastError
        # readout through ctypes (plain windll loses it).
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        GWL_EXSTYLE, WS_EX_LAYERED, LWA_COLORKEY = -20, 0x80000, 0x1
        # SetWindowLongPtrW is the 64-bit-safe variant (plain
        # SetWindowLongW truncates HWND/LONG_PTR on x64 builds).
        try:
            GetWL, SetWL = u32.GetWindowLongPtrW, u32.SetWindowLongPtrW
        except AttributeError:
            GetWL, SetWL = u32.GetWindowLongW, u32.SetWindowLongW
        GetWL.argtypes = [wintypes.HWND, ctypes.c_int]
        GetWL.restype = ctypes.c_long
        SetWL.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        SetWL.restype = ctypes.c_long
        u32.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte,
            wintypes.DWORD]
        u32.SetLayeredWindowAttributes.restype = wintypes.BOOL
        hwnd_t = wintypes.HWND(hwnd)

        def _frame_changed():
            # MSDN: after changing window styles via SetWindowLong,
            # call SetWindowPos with SWP_FRAMECHANGED for the change
            # to take effect. Also nudge a repaint.
            SWP = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
            # NOSIZE | NOMOVE | NOZORDER | NOACTIVATE | FRAMECHANGED
            u32.SetWindowPos(hwnd_t, None, 0, 0, 0, 0, SWP)
            u32.InvalidateRect(hwnd_t, None, True)

        style = GetWL(hwnd_t, GWL_EXSTYLE)
        if pct < 100:
            SetWL(hwnd_t, GWL_EXSTYLE, style | WS_EX_LAYERED)
            key = _OC_KEY[0] | (_OC_KEY[1] << 8) | (_OC_KEY[2] << 16)
            ok = u32.SetLayeredWindowAttributes(
                hwnd_t, wintypes.COLORREF(key), 0, LWA_COLORKEY)
            _frame_changed()
            if not ok:
                print(f"[OmniChat] box opacity: SetLayeredWindowAttributes "
                      f"FAILED (GetLastError={ctypes.get_last_error()})")
            # Read back what Windows actually has — this is the line
            # that settles any "is it applied?" question in the log.
            new_style = GetWL(hwnd_t, GWL_EXSTYLE)
            crkey = wintypes.COLORREF(0)
            balpha = ctypes.c_ubyte(0)
            flags = wintypes.DWORD(0)
            got = u32.GetLayeredWindowAttributes(
                hwnd_t, ctypes.byref(crkey), ctypes.byref(balpha),
                ctypes.byref(flags))
            print(f"[OmniChat] box opacity: applied {pct}% | "
                  f"layered_bit={'YES' if new_style & WS_EX_LAYERED else 'NO'}"
                  f" | readback={'ok' if got else 'FAILED'}"
                  f" key=0x{crkey.value:06X} flags=0x{flags.value:X}"
                  f" (want key=0x{key:06X} flags=0x1)")
        else:
            SetWL(hwnd_t, GWL_EXSTYLE, style & ~WS_EX_LAYERED)
            _frame_changed()
            print("[OmniChat] box opacity: 100% — layering removed")
    except Exception as e:
        print(f"[OmniChat] box opacity failed: {e!r}")
    return pct


# ── Keep-game-focus (clickable without activation) ───────────────────────
# WS_EX_NOACTIVATE makes the window receive mouse input (clicks, drags,
# wheel on Win10+ "scroll inactive windows") WITHOUT ever becoming the
# active window — so clicking tabs/buttons/scrollbar never takes
# keyboard focus away from FFXI. WS_EX_APPWINDOW is set alongside it to
# keep the taskbar button (NOACTIVATE alone would remove it).
#
# The exception is typing: SDL only delivers key/TEXTINPUT events to a
# focused window, so the composer explicitly takes focus when its text
# field is clicked and hands it straight back to the game on
# send/Esc/unfocus (see the focus-edge hook in the main loop).

_prev_foreground_hwnd = None     # window to give focus back to


def _apply_no_activate(enabled):
    """Set/clear WS_EX_NOACTIVATE (+APPWINDOW) on the overlay window."""
    if sys.platform != "win32" \
            or os.environ.get("SDL_VIDEODRIVER") == "dummy":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = _get_hwnd()
        if not hwnd:
            return
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE, WS_EX_APPWINDOW = 0x08000000, 0x00040000
        try:
            GetWL, SetWL = u32.GetWindowLongPtrW, u32.SetWindowLongPtrW
        except AttributeError:
            GetWL, SetWL = u32.GetWindowLongW, u32.SetWindowLongW
        hwnd_t = wintypes.HWND(hwnd)
        style = GetWL(hwnd_t, GWL_EXSTYLE)
        if enabled:
            style = style | WS_EX_NOACTIVATE | WS_EX_APPWINDOW
        else:
            style = style & ~WS_EX_NOACTIVATE
        SetWL(hwnd_t, GWL_EXSTYLE, style)
        # SWP_FRAMECHANGED so the style change takes effect now.
        u32.SetWindowPos(hwnd_t, None, 0, 0, 0, 0,
                         0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
        print("[OmniChat] keep-game-focus: "
              + ("ON (clicks do not steal focus)" if enabled else "OFF"))
    except Exception as e:
        print(f"[OmniChat] keep-game-focus failed: {e!r}")


def _take_keyboard_focus():
    """Composer field clicked: remember the game window, then activate
    ourselves so SDL receives key/TEXTINPUT events. Explicit
    SetForegroundWindow is allowed despite WS_EX_NOACTIVATE (the style
    only suppresses mouse/system activation), and succeeds here because
    our process just received the click."""
    global _prev_foreground_hwnd
    if sys.platform != "win32" \
            or os.environ.get("SDL_VIDEODRIVER") == "dummy":
        return
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        ours = _get_hwnd()
        cur = u32.GetForegroundWindow()
        if cur and cur != ours:
            _prev_foreground_hwnd = cur
        if ours:
            u32.SetForegroundWindow(wintypes.HWND(ours))
    except Exception:
        pass


def _return_keyboard_focus():
    """Composer done (send/Esc/unfocus): give focus back to whatever
    had it before — i.e., the game."""
    global _prev_foreground_hwnd
    if sys.platform != "win32" \
            or os.environ.get("SDL_VIDEODRIVER") == "dummy":
        return
    prev, _prev_foreground_hwnd = _prev_foreground_hwnd, None
    if not prev:
        return
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        if u32.IsWindow(wintypes.HWND(prev)):
            u32.SetForegroundWindow(wintypes.HWND(prev))
    except Exception:
        pass


_apply_no_activate(bool(setting("no_focus_steal")))


# Dithered-background cache: one surface per (w, h, pct, theme). A 4x4
# Bayer matrix gives 16 evenly-distributed transparency levels with no
# visible banding; rebuilt only when size/opacity/theme changes.
_bg_dither_cache = {}

_BAYER4 = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


def _chat_blit_panel_bg(surface, x, y, pw, ph):
    """Panel background: solid at 100% box opacity, dithered toward the
    transparent key color below it. Called by draw_chat_panel in place
    of the old flat fill."""
    pct = _box_opacity_pct()
    bg = CHAT_BG_COLOR[:3]
    if pct >= 100:
        pygame.draw.rect(surface, bg, (x, y, pw, ph))
        return
    key = (pw, ph, pct, _current_theme, bg)
    cached = _bg_dither_cache.get(key)
    if cached is None:
        _bg_dither_cache.clear()        # only ever one size/look live
        cached = pygame.Surface((pw, ph))
        cached.fill(bg)
        # Pure-pygame tiling — deliberately NO numpy here. The first
        # version used pygame.surfarray (which imports numpy); on a
        # build machine without numpy installed, PyInstaller doesn't
        # bundle it, the import fails silently, and the background
        # falls back to solid — leaving ZERO transparent-key pixels,
        # so the color key has nothing to punch through and "opacity
        # does nothing". Build one 4x4 Bayer tile with set_at and
        # batch-blit it across the surface; runs once per
        # (size, opacity, theme) and is cached, so speed is moot.
        level = (100 - pct) * 16.0 / 100.0
        tile = pygame.Surface((4, 4))
        n_key = 0
        for ty in range(4):
            for tx in range(4):
                if _BAYER4[ty][tx] < level:
                    tile.set_at((tx, ty), _OC_KEY)
                    n_key += 1
                else:
                    tile.set_at((tx, ty), bg)
        if n_key:
            try:
                cached.blits(
                    [(tile, (cx * 4, cy * 4))
                     for cy in range((ph + 3) // 4)
                     for cx in range((pw + 3) // 4)],
                    doreturn=False)
            except Exception:
                # blits() unavailable/failed: plain loop fallback.
                for cy in range((ph + 3) // 4):
                    for cx in range((pw + 3) // 4):
                        cached.blit(tile, (cx * 4, cy * 4))
        print(f"[OmniChat] box opacity: dither built {pw}x{ph} @ {pct}% "
              f"({n_key}/16 key pixels per tile)")
        _bg_dither_cache[key] = cached
    surface.blit(cached, (x, y))


def _window_clear_color():
    """Per-frame window fill: the transparent key while box opacity is
    active so the area outside the panel chrome vanishes; otherwise a
    near-background solid."""
    if _box_opacity_pct() < 100:
        return _OC_KEY
    return (10, 11, 14) if _current_theme == "dark" else (235, 237, 240)


if _box_opacity_pct() < 100:
    _apply_box_opacity(_box_opacity_pct())


# ── Options popup ──────────────────────────────────────────────────────
# Small dropdown under the header Options button: opacity stepper,
# always-on-top toggle, font size cycler, composer show/hide. Mirrors
# the tab right-click popup pattern: per-frame rect list, click
# dispatch in the main loop, click-outside dismisses.
_chat_options_button_rect = None
_options_popup_open  = False
_options_popup_rects = []      # list of (Rect, action_str)

_FONT_SIZE_ORDER = ["small", "medium", "large"]


def draw_options_popup(surface):
    """Render the options popup (no-op when closed). Rebuilds
    _options_popup_rects each frame for the click dispatcher."""
    global _options_popup_rects
    _options_popup_rects = []
    if not _options_popup_open:
        return
    f = _chat_get_font("meta", 11)

    row_h, pad, p_w = 22, 8, 222
    rows = 8
    p_h = pad * 2 + row_h * rows

    # Anchor under the Options button; clamp into the window.
    win_w, win_h = chat_panel_size()
    btn = _chat_options_button_rect
    px = (btn.x if btn else win_w - p_w - 8)
    px = max(2, min(px, win_w - p_w - 2))
    py = (btn.bottom + 2 if btn else 20)
    py = max(2, min(py, win_h - p_h - 2))
    panel = pygame.Rect(px, py, p_w, p_h)

    s = pygame.Surface((p_w, p_h), pygame.SRCALPHA)
    s.fill((24, 28, 36, 248))
    surface.blit(s, panel.topleft)
    pygame.draw.rect(surface, (90, 105, 125), panel, 1)
    _options_popup_rects.append((panel, "popup_bg"))

    def _row_y(i):
        return py + pad + i * row_h

    def _label(text, i, color=(210, 215, 225)):
        surface.blit(f.render(text, True, color), (px + pad, _row_y(i) + 4))

    def _button(text, right_edge, i, action, w=None):
        t = f.render(text, True, (235, 235, 240))
        bw = w or (t.get_width() + 14)
        r = pygame.Rect(right_edge - bw, _row_y(i) + 1, bw, row_h - 4)
        hov = r.collidepoint(pygame.mouse.get_pos())
        bs = pygame.Surface((r.w, r.h), pygame.SRCALPHA)
        bs.fill((70, 85, 110, 235) if hov else (46, 54, 68, 225))
        surface.blit(bs, r.topleft)
        pygame.draw.rect(surface, (95, 110, 130), r, 1)
        surface.blit(t, (r.x + (r.w - t.get_width()) // 2,
                         r.y + (r.h - t.get_height()) // 2))
        _options_popup_rects.append((r, action))
        return r

    right = px + p_w - pad

    # Row 0: theme cycler.
    _label("Theme", 0)
    _button("Light" if _current_theme == "light" else "Dark",
            right, 0, "toggle_theme", w=58)

    # Row 1: always on top toggle.
    _label("Always on top", 1)
    aot = bool(setting("always_on_top"))
    _button("ON" if aot else "OFF", right, 1, "toggle_aot", w=44)

    # Row 2: box opacity stepper. Background translucency only —
    # text stays fully opaque at every level.
    pct = _box_opacity_pct()
    _label(f"Box opacity  {pct}%", 2)
    r_plus  = _button("+", right, 2, "opacity_up", w=26)
    _button("\u2212", r_plus.x - 4, 2, "opacity_down", w=26)

    # Row 3: UI scale stepper (scales all panel text).
    try:
        _scl = float(setting("global_ui_scale") or 1.0)
    except (TypeError, ValueError):
        _scl = 1.0
    _label(f"UI scale  {int(round(_scl * 100))}%", 3)
    r_splus = _button("+", right, 3, "scale_up", w=26)
    _button("\u2212", r_splus.x - 4, 3, "scale_down", w=26)

    # Row 4: font size cycler.
    pref = (setting("chat_font_size") or "medium").lower()
    _label("Font size", 4)
    r_next = _button("\u25b8", right, 4, "font_next", w=24)
    r_lab  = _button(pref.capitalize(), r_next.x - 2, 4, "font_label", w=68)
    _button("\u25c2", r_lab.x - 2, 4, "font_prev", w=24)

    # Row 5: composer toggle.
    _label("Message composer", 5)
    _button("ON" if chat_composer_visible else "OFF", right, 5,
            "toggle_composer", w=44)

    # Row 6: keep-game-focus toggle. ON = clicking the panel never
    # takes focus from FFXI (typing in the composer still works — it
    # borrows focus while the field is active and gives it back).
    _label("Keep game focus", 6)
    _button("ON" if setting("no_focus_steal") else "OFF", right, 6,
            "toggle_nofocus", w=44)

    # Row 7: exit. The window has no title bar (no [X]), so this and
    # Ctrl+Q are the two ways out. Full-width, visually distinct (red
    # tint) so it can't be mistaken for a toggle.
    exit_r = pygame.Rect(px + pad, _row_y(7) + 1,
                         p_w - pad * 2, row_h - 4)
    exit_hov = exit_r.collidepoint(pygame.mouse.get_pos())
    exit_s = pygame.Surface((exit_r.w, exit_r.h), pygame.SRCALPHA)
    exit_s.fill((140, 55, 50, 240) if exit_hov else (90, 45, 45, 225))
    surface.blit(exit_s, exit_r.topleft)
    pygame.draw.rect(surface, (160, 90, 85), exit_r, 1)
    exit_t = f.render("Exit OmniChat", True, (245, 230, 228))
    surface.blit(exit_t, (exit_r.x + (exit_r.w - exit_t.get_width()) // 2,
                          exit_r.y + (exit_r.h - exit_t.get_height()) // 2))
    _options_popup_rects.append((exit_r, "exit_app"))


def dispatch_options_popup_click(mx, my):
    """Handle a left-click while the options popup is open. Returns
    True if consumed; False when the click missed the popup (caller
    dismisses + lets the click fall through)."""
    global _options_popup_open, chat_composer_visible
    global _chat_wrap_cache, _chat_render_cache
    if not _options_popup_open:
        return False
    for rect, action in reversed(_options_popup_rects):
        if not rect.collidepoint(mx, my):
            continue
        if action == "popup_bg":
            return True   # eat clicks on chrome; keep popup open
        if action == "toggle_aot":
            new = not bool(setting("always_on_top"))
            set_setting("always_on_top", new)
            _apply_always_on_top(new)
        elif action == "toggle_theme":
            new = "light" if _current_theme == "dark" else "dark"
            set_setting("chat_theme", new)
            _apply_theme(new)
        elif action in ("opacity_up", "opacity_down"):
            pct = _box_opacity_pct()
            pct += 5 if action == "opacity_up" else -5
            pct = _apply_box_opacity(pct)
            set_setting("window_opacity", pct)
        elif action in ("scale_up", "scale_down"):
            try:
                scl = float(setting("global_ui_scale") or 1.0)
            except (TypeError, ValueError):
                scl = 1.0
            scl += 0.1 if action == "scale_up" else -0.1
            scl = max(0.5, min(2.0, round(scl, 2)))
            set_setting("global_ui_scale", scl)
            # Every font and every wrapped line was built at the old
            # scale — flush so the new size applies this frame.
            _chat_fonts.clear()
            _chat_wrap_cache.clear()
            _chat_render_cache.clear()
        elif action in ("font_prev", "font_next", "font_label"):
            pref = (setting("chat_font_size") or "medium").lower()
            try:
                i = _FONT_SIZE_ORDER.index(pref)
            except ValueError:
                i = 1
            if action != "font_label":
                i = (i + (1 if action == "font_next" else -1)) \
                    % len(_FONT_SIZE_ORDER)
            else:
                i = (i + 1) % len(_FONT_SIZE_ORDER)
            set_setting("chat_font_size", _FONT_SIZE_ORDER[i])
            # Wrapped-line and glyph caches were built with the old
            # fonts — flush so the new size takes effect immediately.
            _chat_wrap_cache.clear()
            _chat_render_cache.clear()
        elif action == "toggle_composer":
            chat_composer_visible = not chat_composer_visible
            set_setting("chat_composer_visible", chat_composer_visible)
        elif action == "toggle_nofocus":
            new = not bool(setting("no_focus_steal"))
            set_setting("no_focus_steal", new)
            _apply_no_activate(new)
        elif action == "exit_app":
            # Route through the normal QUIT path so shutdown persists
            # window position/size exactly like closing any other way.
            pygame.event.post(pygame.event.Event(pygame.QUIT))
        return True
    # Missed the popup entirely: close it, let the click fall through.
    _options_popup_open = False
    return False


def _begin_native_window_drag():
    """Hand the current left-button press to the OS as a title-bar drag
    (WM_NCLBUTTONDOWN + HTCAPTION). Lets the borderless window be moved
    natively from the header strip. No-op off-Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = _get_hwnd()
        if not hwnd:
            return
        ctypes.windll.user32.ReleaseCapture()
        ctypes.windll.user32.SendMessageW(hwnd, 0x00A1, 2, 0)
    except Exception as e:
        print(f"[OmniChat] window drag failed: {e!r}")


def _get_cursor_screen_pos():
    """Cursor position in SCREEN coordinates (Windows). Needed for
    left/top-edge resizes, where the window origin moves under the
    cursor mid-drag and window-relative coordinates become useless."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))
    except Exception:
        pass
    return None


def _set_window_pos(x, y):
    """Move the OS window to screen (x, y) without resizing/z-change."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = _get_hwnd()
        if not hwnd:
            return
        SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
        ctypes.windll.user32.SetWindowPos(
            wintypes.HWND(hwnd), None, int(x), int(y), 0, 0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
    except Exception:
        pass


def _get_window_pos():
    """Top-left of the OS window, for persisting across sessions."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = _get_hwnd()
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return [int(rect.left), int(rect.top)]
    except Exception:
        pass
    return None


# ── Shared render helpers (subset of OmniWatch's) ─────────────────────────

# Font cache by (name, size, bold, italic).
_font_cache = {}
def get_font(name, size, bold=False, italic=False):
    size = max(6, int(size))
    key  = (name, size, bold, italic)
    f    = _font_cache.get(key)
    if f is None:
        f = pygame.font.SysFont(name, size, bold=bold, italic=italic)
        _font_cache[key] = f
    return f


def _eff(panel_scale):
    """Effective render scale: the input times the global_ui_scale
    setting (Options → UI scale). All chat fonts route their sizes
    through this, so one multiplier scales the whole panel's text —
    same mechanism OmniWatch uses. Clamped 0.5–2.0; falls back to the
    input unchanged on any error."""
    try:
        g = float(setting("global_ui_scale") or 1.0)
        if g < 0.5:
            g = 0.5
        elif g > 2.0:
            g = 2.0
        return float(panel_scale) * g
    except (TypeError, ValueError):
        return panel_scale


ACCENT_CHAT = (140, 220, 180)   # mint — chat (communication/signal)

def draw_accent_stripe(surface, x, y, h, color, w=2):
    """Vertical accent stripe at the left edge of a panel."""
    if h < 8:
        return
    pygame.draw.rect(surface, color, (x + 1, y + 3, w, h - 6))


RESIZE_GRIP = 14
_GRIP_BG  = (28, 32, 40)
_GRIP_BDR = (60, 70, 85)
_GRIP_FG  = (140, 150, 165)

def draw_resize_grip(surface, x, y):
    """Diagonal-stripe resize handle at (x, y) bottom-right corner.
    Always shown (the standalone window has no setup mode — the grip
    is the only resize affordance)."""
    g = RESIZE_GRIP
    pygame.draw.rect(surface, _GRIP_BG,  (x - g, y - g, g, g))
    pygame.draw.rect(surface, _GRIP_BDR, (x - g, y - g, g, g), 1)
    for off in (3, 7):
        pygame.draw.line(surface, _GRIP_FG,
                         (x - off, y - 1), (x - 1, y - off))


# ── Sockets ─────────────────────────────────────────────────────────────────

def _bind_udp(port, label):
    """Non-blocking UDP socket on 127.0.0.1:port with a clear error on
    collision (most common cause: a second OmniChat overlay running)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError as e:
        print(f"[OmniChat] FATAL: could not bind UDP port {port} "
              f"({label}): {e}")
        print(f"[OmniChat] Another process is already using this port — "
              f"most likely a second OmniChat overlay. Close it and "
              f"relaunch.")
        raise
    s.setblocking(False)
    return s


# Event stream from OmniChat.lua. Receive buffer large: heavy combat
# may emit a batch spanning multiple datagrams.
sock_chat = _bind_udp(5113, "chat events")

# Command rail to OmniChat.lua (composer sends, /pcmd invites).
sock_cmd_out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
CMD_OUT_ADDR = ("127.0.0.1", 5111)


# ── Shared state the extracted chat code expects ──────────────────────────

# Rolling buffer of parsed event dicts in arrival order (see the event
# shape documented in _parse_chat_batch). 2000 ≈ 100s of scrollback at
# heavy-combat rates; older lines fall off the front.
chat_events = _collections.deque(maxlen=2000)

# Identity of the live character (set from the lua JOB heartbeat).
player_self_name = ""
current_char_name = ""

# Party-panel data streams don't exist in the standalone — the Python
# name-classifier fallbacks that walk these simply find nothing and
# defer to the lua-side actor_class (the authoritative source).
party_data  = []
ally1_data  = []
ally2_data  = []
target_info = None

# ── Multibox gate ──────────────────────────────────────────────────────────
# chat/drain.lua prefixes every datagram with "@CharName@". When two
# characters are logged in (multiboxing), pin chat to the configured
# main character (chat_main_char setting) so the panel doesn't
# interleave both characters' streams; fall back to whichever
# character is live when no main is set.
_MB_DEBUG = False
_mb_seen_senders = set()


def list_known_characters():
    chars = set(_mb_seen_senders)
    if current_char_name:
        chars.add(current_char_name)
    return sorted(chars)


def _mb_lock_target():
    return current_char_name


def _mb_chat_lock_target():
    main = (setting("chat_main_char") or "").strip()
    live = list_known_characters()
    if main and main in live:
        return main
    return current_char_name or _mb_lock_target()


def _mb_gate(raw, for_chat=False):
    if not raw or raw[0] != "@":
        return True, raw            # untagged → pass through unchanged
    end = raw.find("@", 1)
    if end == -1:
        return True, raw            # malformed prefix → don't risk dropping
    sender = raw[1:end]
    payload = raw[end + 1:]
    if sender:
        _mb_seen_senders.add(sender)
    target = _mb_chat_lock_target() if for_chat else _mb_lock_target()
    # Accept when: no lock target yet, sender matches, or sender is
    # empty (brief transient right after login).
    accepted = (not target or not sender or sender == target)
    if accepted:
        return True, payload
    return False, payload           # different character → drop

# =========================================================================
# SECTION: A_state
# =========================================================================

_chat_event_seq = 0
def _chat_assign_seq(ev):
    """Stamp an event with a unique sequence number for cache keying.
    Returns the assigned number. Idempotent — re-stamping is fine
    (uses .get to avoid overwrite if already set)."""
    global _chat_event_seq
    if "_seq" not in ev:
        _chat_event_seq += 1
        ev["_seq"] = _chat_event_seq
    return ev["_seq"]

# Diagnostic print toggle. When True, each parsed event is printed to
# the Python console as it lands. Off by default — turn on with the
# environment variable OMNICHAT_CHAT_TRACE=1 for first-time debugging.
# In step 3 pass 2 this becomes a panel; for pass 1 it's the only
# visible feedback that the receiver is working.
_chat_trace = bool(os.environ.get("OMNICHAT_CHAT_TRACE"))

# Always-on routing trace counter. The first N events get their full
# classifier output + tab destinations logged, so when a user reports
# "filters not working" the session log already has the diagnostic info
# to debug the routing. Capped to keep log size sane on long sessions.
_CHAT_ROUTE_TRACE_CAP = 50
_chat_route_trace_count = 0

# Diagnostic counters — total events received per stream since launch.
# Useful for sanity-checking that both Lua streams are connected:
# after a few minutes of play, chat_recv_text should be non-zero;
# chat_recv_battle stays 0 until step 5 lands.
chat_recv_text   = 0
chat_recv_battle = 0
chat_recv_errors = 0     # malformed packets dropped (logged once each)

# ── Chat panel layout state ──────────────────────────────────────────────
# Floating panel registered with the same draggable/resizable system as
# the other panels (recast, buff, dps, ...). Differs from those in that
# resize tracks pixel size directly (mirroring sim_window_size) rather
# than scaling text — chat is fundamentally about readable lines, not
# icon density, so width/height controls make more sense than scale.
#
# anchor is the corner pin (tl/tr/bl/br + offset) used by all panels;
# pos is the resolved absolute coordinate updated each frame.
chat_anchor          = None             # ["bl", ox, oy] etc.
chat_pos             = None             # [x, y] derived from anchor each frame
chat_scale           = 1.0              # font scale only (not panel size)
chat_panel_dims      = [800, 280]       # [w, h] user-controlled in pixels
CHAT_PANEL_MIN_W     = 240              # below this, lines render uselessly short
CHAT_PANEL_MIN_H     = 80               # below this, less than 2 lines visible
chat_panel_visible   = True

# Body font size keyed off the chat_font_size setting (Small/Medium/Large).
# Each level scales body/meta/tab fonts proportionally so the strip stays
# balanced (timestamps don't dwarf body, tabs don't overflow). Read once
# per draw via _chat_font_sizes() so changing the setting takes effect
# on next render without restart.
CHAT_FONT_SIZE_MAP = {
    "small":  {"body": 10, "meta":  9, "tab": 10},
    "medium": {"body": 12, "meta": 10, "tab": 11},
    "large":  {"body": 14, "meta": 12, "tab": 13},
}

# Scroll state. 0 = pinned to bottom (auto-scroll new events). Positive
# = scrolled UP that many *visible lines* (after wrap) from the bottom.
# We track in visible-lines, not events, because one event may wrap
# into multiple visible lines and the user thinks in terms of what
# they see on screen.
chat_scroll_offset   = 0

# Scrollbar drag tracking for the chat panel. Same shape as the
# checklist drag state. Per-frame thumb/track rects + max_scroll are
# captured during draw and consumed by the mousedown + motion
# handlers.
_chat_scroll_drag = None
_chat_scrollbar_thumb_rect = None
_chat_scrollbar_track_rect = None
_chat_scrollbar_max_scroll = 0

# ── Tabs ───────────────────────────────────────────────────────────
# Each tab has a name, a filter predicate (event_dict → bool), its
# own scroll position, and an unread count incremented when the
# tab is NOT active and a new event would land in it.
#
# Order is fixed for step 6 (hardcoded design). Step 6.5 / 10 will
# expose tab definitions in a JSON config file so users can add /
# remove / reorder / re-filter without code edits.
#
# Filter functions return True when an event belongs in that tab.
# Define them after chat_tab_names so they can reference modes
# easily. The actual filter functions are below this block.
#
# Indices:
#   0 = All    matches everything
#   1 = World  say (1) + shout (3) + yell (11) + emotes — all
#              spoken-aloud messages from any range
#   2 = LS1    linkshell slot 1
#   3 = LS2    linkshell slot 2
#   4 = Party  /party chat ONLY (mode 5) — not battle, just chat
#   5 = B1    Battle filter 1 (placeholder, user-defined later)
#   6 = B2    Battle filter 2 (placeholder, user-defined later)
#   7 = Sys   System / RoE / sparks / gains / errors / drops
#   8 = Cust  Custom tab (empty filter)

# (short_name, full_name) — both shown contextually. Short for tab
# strip, full reserved for tooltips and settings UI.
#
# For built-in tabs, short_name IS the routing identifier — saving
# `["Battle"]` in a routing config routes to whichever tab has
# short_name "Battle". For the two user-customizable tabs at the
# end, the routing identifier is fixed as "custom_1" / "custom_2"
# even though the short_name displayed can be renamed by the user
# (overrides loaded from _meta.tab_names in the routing JSON).
# This way users can rename "Custom 1" → "Songs" without breaking
# any cells they'd previously routed into it.
chat_tab_names = [
    ("Tell",      "Tell"),       # incoming/outgoing tells ONLY — acts as
                                 # an answering machine so tells aren't
                                 # buried in World while you're away
    ("World",     "World"),
    ("Assist",    "Assist"),     # FFXI assist channel chatter (cross-zone
                                 # broadcasts with language preference tag).
                                 # Mode 35 + similar variants route here.
                                 # Filterable / hide-able like any other tab.
    ("Unity",     "Unity"),       # Unity Concord chat — mode 33 carries
                                  # Unity-wide broadcasts (event announcements,
                                  # chat from same Unity Leader). Separate tab
                                  # so it doesn't get lost in World.
    ("LS1",       "Linkshell 1"),
    ("LS2",       "Linkshell 2"),
    ("Party",     "Party"),
    ("Battle",    "Battle"),
    ("Buffs",     "Buffs"),
    ("Debuffs",   "Debuffs"),
    ("Mob",       "Mob"),         # buffs/debuffs landing on mobs
    ("Custom 1",  "Custom 1"),    # user-customizable; routing id "custom_1"
    ("Custom 2",  "Custom 2"),    # user-customizable; routing id "custom_2"
    ("System",    "System"),
    ("Gearswap",  "Gearswap"),
]

# Internal-id → display-label override map. Populated from the
# routing JSON's _meta.tab_names section at load time. Only used
# for the two custom tabs; built-in tabs ignore overrides for them.
# After load, chat_tab_names[i] entries for custom_1 / custom_2 are
# rewritten in-place with the user's chosen label.
_CUSTOM_TAB_IDS = {
    "custom_1": 11,   # index in chat_tab_names — keep in sync above
    "custom_2": 12,   # (shifted +1 again by the Unity tab insertion)
}
_chat_tab_label_overrides = {}    # id → label, persisted via _meta

# Index of the active tab. Persisted in save_layout.
chat_active_tab = 0

# Per-tab scroll positions. Independent — switching tabs preserves
# where the user was reading in each. dict {tab_idx: int}; default 0.
chat_tab_scroll = {i: 0 for i in range(len(chat_tab_names))}

# Per-tab record of the last frame's true filtered physical-line count,
# used to keep the view anchored when scrolled up (new arrivals bump the
# scroll offset instead of sliding the view). None = pinned to bottom.
_chat_tab_line_total = {}

# Per-tab unread counts. Incremented in _ingest_chat_packet when
# a new event lands in an inactive tab. Zeroed when the user
# switches to that tab.
chat_tab_unread = {i: 0 for i in range(len(chat_tab_names))}

# ── Focus words/phrases ──────────────────────────────────────────────
# User-defined strings (edited in the Filters GUI, persisted under
# _meta.focus_phrases in the routing JSON). Any chat event whose text
# contains one (case-insensitive substring, any channel/tab) gets a
# pulsing amber highlight behind its lines for FOCUS_PULSE_SECS, then
# settles to a steady faint highlight so it's still findable when
# scrolling back. Inactive tabs holding an unseen hit pulse their
# label until visited.
_chat_focus_phrases_global = []   # lowercased, from global routing json
_chat_focus_phrases_perjob = []   # lowercased, from per-job routing json
_chat_tab_focus_pulse = {}        # tab_idx -> ts of newest unseen hit
FOCUS_PULSE_SECS = 12.0
CHAT_FOCUS_HL = (255, 184, 40)    # highlight tint (themed)


def _chat_focus_phrases():
    return _chat_focus_phrases_global + _chat_focus_phrases_perjob


def _set_perjob_focus_phrases(lst):
    global _chat_focus_phrases_perjob
    _chat_focus_phrases_perjob = [
        s.strip().lower() for s in (lst or [])
        if isinstance(s, str) and s.strip()]


def _chat_focus_check(ev):
    """Stamp ev with focus_hit/focus_ts when its text contains any
    focus phrase. Called once at ingest — O(len(phrases)) substring
    scans on one lowercased copy."""
    phrases = _chat_focus_phrases()
    if not phrases:
        return False
    tl = (ev.get("text") or "").lower()
    if not tl:
        return False
    hits = [p for p in phrases if p in tl]
    if hits:
        ev["focus_hit"] = True
        ev["focus_ts"] = time.time()
        ev["focus_words"] = hits
        return True
    return False

# Per-frame click-target list for tab strip. (pygame.Rect, tab_idx).
# Reset and rebuilt every render; mousedown checks against it.
chat_tab_rects = []

# Horizontal scroll offset (in pixels) for the tab strip. When the tabs'
# total width exceeds the available strip, the strip scrolls sideways
# rather than abbreviating names or spilling past the panel border. Left/
# right arrows (drawn at the strip ends) advance this. Clamped each frame
# to [0, max_scroll] in draw_chat_panel.
_chat_tab_hscroll = 0
# Per-frame arrow hit-targets: {"left": Rect|None, "right": Rect|None}.
# Rebuilt every render; mousedown checks these before the tab rects.
_chat_tab_arrow_rects = {"left": None, "right": None}

# ── Composer (bottom-of-panel chat input) ──────────────────────
# A single-line text field at the bottom of the chat panel with a
# channel selector (< say >). Typing into it and pressing Enter
# sends the message to FFXI via the existing port-5111 UDP command
# channel that hotbar buttons already use.
#
# State:
#   chat_composer_text       — current text in the input field
#   chat_composer_cursor     — cursor position (insertion index, 0..len)
#   chat_composer_focused    — True when keystrokes go to the field
#   chat_composer_channel    — index into CHAT_COMPOSER_CHANNELS
#   chat_composer_tell_to    — target name when channel == "tell"
#   chat_composer_tell_to_cursor — cursor position in tell-target field
#   chat_composer_tell_to_focused — True when target field is focused
#                                    (instead of main message field)
#   chat_composer_last_blink — for cursor blink animation
#   chat_composer_visible    — bool, whole composer row shown or hidden
#                              (separate setting; some users want a
#                              read-only chat panel)
chat_composer_text          = ""
chat_composer_cursor        = 0
chat_composer_focused       = False
chat_composer_channel       = 0     # default to "say"
chat_composer_tell_to       = ""
chat_composer_tell_to_cursor = 0
chat_composer_tell_to_focused = False
chat_composer_last_blink    = 0.0
chat_composer_visible       = True

# Channel options. Each entry is (key, label, slash_command_prefix).
# slash_command_prefix is what we send to FFXI's input command. For
# tell, it's just "/t " — the target name and message are appended
# at send time. For reply, FFXI handles the target server-side so we
# just send "/r <message>". For linkshells, /l and /l2 are the
# canonical FFXI commands (shorter than /linkshell, /linkshell2).
CHAT_COMPOSER_CHANNELS = [
    ("say",    "say",    "/s "),
    ("tell",   "tell",   "/t "),       # needs a target prefix at send
    ("party",  "party",  "/p "),       # party chat
    ("shout",  "shout",  "/sh "),
    ("yell",   "yell",   "/yell "),
    ("ls1",    "ls1",    "/l "),
    ("ls2",    "ls2",    "/l2 "),
    ("unity",  "unity",  "/u "),       # Unity Concord chat (/u sends; /cm u
                                       #   only toggles the display). Echoes
                                       #   back on mode 211 ("{You} ...").
    ("assist", "assist", "/ae "),      # Assist Channel (English: /ae or
                                       #   /assiste; the JP channel is /aj).
                                       #   Asura is an English world, so /ae.
                                       #   Only sendable in hub cities / the
                                       #   listed Assist-enabled zones — FFXI
                                       #   rejects it elsewhere (silent fail).
]

# Maps each composer channel key to a CHAT_SEGMENT_COLORS class, so the
# channel-selector label in the composer is tinted to match that
# channel's chat color (e.g. party = pale teal, unity = gold, ls1 =
# green). Reply uses the tell color (it's a tell reply). Any key not
# listed falls back to the neutral CHAT_COMPOSER_CHANNEL_FG.
CHAT_COMPOSER_CHANNEL_COLOR_CLASS = {
    "say":    "ch_say",
    "tell":   "ch_tell_composer",
    "reply":  "ch_tell_composer",   # reply is a tell — share the pink label
    "party":  "ch_party",
    "shout":  "ch_shout",
    "yell":   "ch_yell",
    "ls1":    "ch_ls1",
    "ls2":    "ch_ls2",
    "unity":  "ch_unity",
    "assist": "ch_assist",
}

# Composer-specific colors. Reuse panel palette where possible.
CHAT_COMPOSER_BG          = (16, 18, 24, 240)
CHAT_COMPOSER_FIELD_BG    = (28, 32, 40, 240)
CHAT_COMPOSER_FIELD_FOCUS = (40, 48, 60, 240)   # background when focused
CHAT_COMPOSER_FIELD_BDR   = (60, 70, 85)
CHAT_COMPOSER_FIELD_BDR_F = (140, 200, 220)     # focused border (cool blue)
CHAT_COMPOSER_TEXT        = (230, 230, 230)
CHAT_COMPOSER_PLACEHOLDER = (110, 115, 125)
CHAT_COMPOSER_CHANNEL_FG  = (220, 220, 220)
CHAT_COMPOSER_ARROW_FG    = (200, 200, 200)
CHAT_COMPOSER_ARROW_FG_H  = (255, 220, 130)     # hover/active arrow
CHAT_COMPOSER_SEND_BG     = (40, 80, 120)
CHAT_COMPOSER_SEND_FG     = (240, 240, 240)

# Per-frame click target rects (rebuilt by draw_chat_panel each frame).
_chat_composer_rect_arrow_l   = None
_chat_composer_rect_arrow_r   = None
_chat_composer_rect_channel   = None
_chat_composer_rect_input     = None
_chat_composer_rect_tell_to   = None
_chat_composer_rect_send      = None
_chat_composer_rect_at        = None  # { } auto-translate wrap button

# Wrap cache: maps (event_id, panel_width) → list[str] of wrapped lines.
# Invalidated implicitly when panel width changes (different cache key).
# Old entries linger but the cap-via-events deque keeps the working set
# bounded; chat_events.maxlen=2000 so cache size is bounded too.
_chat_wrap_cache     = {}
_chat_wrap_cache_w   = 0       # current cache's width — clear cache on change

# Per-event rendered-surface cache: (event_id, color, font_id) → Surface.
# Same lifecycle as wrap cache. Saves font.render() calls on redraws of
# unchanged events (the common case at 60fps).
_chat_render_cache   = {}

# Set each frame by draw_chat_panel when scrolled up — Rect of the
# "jump to bottom" badge, or None when not visible. Read by mousedown
# handler to detect a click on the badge.
_chat_jump_badge_rect = None

# Routing settings gear button rect, or None until first draw. Click
# launches the standalone routing config GUI. Reset each draw so
# resizing/hiding the chat panel doesn't leak a stale rect.
_chat_settings_button_rect = None

# Clear-buttons in the chat header. "Clear Tab" removes events from the
# active tab only; "Clear All" wipes the whole chat buffer. None until
# first draw; set each frame by draw_chat_panel.
_chat_clear_tab_button_rect = None
_chat_clear_all_button_rect = None
_chat_show_all_button_rect  = None
# Per-tab right-click popup state. Right-clicking a tab opens a
# small floating menu at the click position offering "Hide tab".
# The user must then click "Hide tab" to actually hide. Click
# anywhere else dismisses the popup without changes — same
# pattern as a desktop OS context menu.
#   _chat_tab_rclick_tab     — tab index the popup is anchored to
#   _chat_tab_rclick_anchor  — screen (x, y) where it appeared
#   _chat_tab_rclick_rects   — per-frame click-test rects:
#                              list of (rect, action) tuples
_chat_tab_rclick_tab    = None
_chat_tab_rclick_anchor = None
_chat_tab_rclick_rects  = []

# ── Clickable sender names (per-frame hit-test) ──────────────────────────
# Rebuilt every draw of the chat panel. Entries are dicts:
#   {"rect": pygame.Rect, "name": str, "actor_class": str}
# Populated by the sender-rendering branch with the rect of the
# sender NAME portion (NOT the colon, NOT the zone tag, NOT the
# message body). Read by the mousedown handler:
#   * left-click → populate the chat composer with /tell <name>
#   * right-click → open the chat-name context menu over the rect
# Reset to [] at the top of each draw so a hidden chat panel can't
# leave stale rects clickable from elsewhere on screen.
_chat_clickable_senders = []

# Name of the sender currently under the mouse, or None. Used so the
# rendering branch can paint the name in CHAT_NAME_HOVER_COLOR instead
# of its normal sender color, giving visual feedback that it's clickable.
# Set by the mousemotion handler (cheap collidepoint walk over
# _chat_clickable_senders); read on the NEXT draw.
_chat_name_hover = None

# Context menu state. Open when the user right-clicks a sender name.
# Layout:
#   {"name": str, "actor_class": str, "rect": pygame.Rect,
#    "items": [{"label": str, "action": str, "rect": Rect|None}, ...]}
# Items are populated when the menu opens; each item's `rect` is set
# during draw so the next mousedown can hit-test them. Cleared (= None)
# by: choosing an item, clicking outside the menu, or pressing Escape.
_chat_name_context_menu = None

# Mode → color map. Modes derived from in-game capture (see emit.lua
# header comments). Unknown modes fall back to CHAT_COLOR_DEFAULT.
#
# Color philosophy:
#   * Whites/light gray = ordinary speech and battle text
#   * Cyans = personal communication (tell, party)
#   * Yellows = own gains, kills, drops (good things)
#   * Reds = errors, can't-do messages
#   * Mid-gray = system stuff (RoE, sparks, accolades)
#   * Light blue = own buffs landing
#
# Refine over time once the panel is visible and we see what actually
# helps versus what's noise.
CHAT_COLOR_DEFAULT     = (200, 200, 200)
CHAT_MODE_PALETTE = {
    # World-tab differentiation: each mode gets its own color so a
    # stream of mixed say/shout/tell/yell/emote reads at a glance.
    1:   (240, 240, 240),   # /say — white
    2:   (240, 240, 240),   # /say echo (outgoing)
    3:   (255, 240, 150),   # /shout — light yellow
    4:   (200, 160, 255),   # /tell received — light purple
    5:   (180, 230, 220),   # /party
    7:   (200, 160, 255),   # /linkshell  (placeholder, mode TBD)
    8:   (200, 160, 255),   # /linkshell 2 (placeholder)
    11:  (255, 150, 200),   # /yell — pink, attention-getting
    12:  (180, 140, 230),   # /tell sent — purple (dimmer than received)
    13:  (150, 200, 255),   # /emote — blueish
    15:  (150, 200, 255),   # /emote (mode-15 social emote variant) — same
                           #   blue as 13. Mode 15 is overloaded (battle
                           #   text + social emotes); emit.lua's _is_emote
                           #   carve-out passes only the social-emote lines
                           #   through, and they route to chat_emote/World.
                           #   Without this entry they'd fall to default
                           #   gray instead of the emote blue.
    28:  (200, 200, 200),   # enemy spell cast on you
    29:  (200, 200, 200),   # melee battle
    30:  (200, 200, 200),   # mob ability use
    36:  (255, 230, 110),   # kill / defeat
    50:  (160, 200, 240),   # magic effect applied
    56:  (170, 210, 200),   # buff/regen on entity
    59:  (200, 200, 200),   # spell resist
    101: (170, 230, 255),   # own song land
    121: (255, 230, 140),   # item find / drops
    122: (220, 140, 140),   # error / unable / out of range
    123: (220, 140, 140),   # cannot use command
    127: (180, 180, 180),   # system / RoE / sparks
    131: (240, 220, 150),   # gain (limit points / gil)
    # LS message-of-the-day lines arrive as native incoming text:
    #   mode 205 = LS1 MoTD, mode 217 = LS2 MoTD (confirmed via chatdebug).
    # Without a palette entry these fell to the default gray/white. Color
    # BOTH the same LS green so the two linkshells' MoTDs look identical
    # (per request) and read as linkshell content. The native text already
    # carries the correct LS name + set-date, so this native line is the
    # canonical MoTD — chat_packets.lua suppresses OmniWatch's own 0x0CC
    # duplicate so only this one shows.
    205: (144, 238, 144),   # LS1 message-of-the-day — LS green
    217: (144, 238, 144),   # LS2 message-of-the-day — same green
}

# Synthetic-event colors for buff/debuff lines (from chat/buff_events.lua).
# Approximate FFXI's classic in-client colors for status messages:
#   - buffs render in light cyan (the "you gain the effect of" blue)
#   - debuffs render in dark pink/magenta (the "you are afflicted with" color)
# These are visually distinct from the Battle red and the regular chat
# white so the lines stand out at a glance.
CHAT_COLOR_BUFF   = (128, 224, 255)   # light cyan
CHAT_COLOR_DEBUFF = (255, 128, 192)   # dark pink / magenta
COL_SKILLCHAIN    = (255, 230, 130)   # warm gold — skillchain result lines

# Word-level color palette for events with segments. Each event's
# `segments` field is a list of (text, color_class) tuples; the
# renderer looks up the class here. Classes that don't match fall back
# to CHAT_COLOR_DEFAULT (gray-white).
#
# Color choices:
#   self/party    — close blues (you and your team are "us")
#   alliance      — soft teal (adjacent to party but green-tinted)
#   mob/npc/other — red / gray / off-white
#   pet/party_pet — gold (yours, warm)
#   spell         — mint-aqua (distinct from blues used for self/party)
#   ability       — peach
#   weaponskill   — magenta
#   buff_status   — light cyan (matches Buffs tab color)
#   debuff_status — pink (matches Debuffs tab color)
#   damage_number — cream (numbers stand out without shouting)
#   default       — body-text gray
CHAT_SEGMENT_COLORS = {
    "self":          (120, 180, 255),
    "party":         (170, 220, 255),
    "alliance":      (180, 230, 220),
    "mob":           (255, 110, 110),
    "npc":           (200, 200, 200),
    "pet":           (255, 220, 130),
    "party_pet":     (255, 220, 130),
    "other":         (220, 220, 220),
    "spell":         (140, 250, 220),
    "ability":       (255, 200, 150),
    "weaponskill":   (220, 130, 220),
    "buff_status":   (128, 224, 255),
    "debuff_status": (255, 128, 192),
    # Verb colors for status events. Semantic:
    #   gaining a buff / recovering from a debuff = good = yellow
    #   losing a buff / being afflicted with debuff = bad = pink
    "verb_good":     (255, 220, 130),   # warm yellow
    "verb_bad":      (255, 130, 180),   # pink
    "damage_number": (255, 240, 180),
    "default":       (220, 220, 220),
    # Channel-themed colors for chat sender names. Used by
    # chat_packets.lua to color the sender according to which channel
    # the message came in on, instead of by actor_class. This makes
    # World vs LS1 vs LS2 vs Party visually distinct at a glance.
    #
    # Kept SEPARATE from the message-body palette (CHAT_MSG_COLOR_BY_MODE
    # / CHAT_MODE_PALETTE further down). The sender-name color signals
    # "who said it" (channel-themed at the name); the body color signals
    # "what channel" (channel-themed at the message). Same goal, two
    # axes — touching this table would recolor sender names, which is
    # not what we want when adjusting body colors.
    "ch_say":     (240, 240, 240),   # /say — white
    "ch_shout":   (210, 105,  30),   # /shout — dark orange (distinct from yell)
    "ch_yell":    (255, 200, 100),   # /yell — orange-yellow
    "ch_tell":    (140, 220, 255),   # /tell — light blue
    "ch_party":   (180, 230, 220),   # /party — pale teal
    "ch_ls1":     (144, 238, 144),   # /linkshell 1 — light green
    "ch_ls2":     (60,  150, 60),    # /linkshell 2 — dark green (matches tab)
    "ch_emote":   (238, 130, 238),   # /em — violet
    # Unity Concord — sender name in the Unity tab's gold-amber so the
    # speaker color matches the tab title, for BOTH PC member chat and
    # NPC dialogue (the chat_packets mode-33 PC path already uses this
    # class; the brace-split below applies it to the mode-212 NPC path).
    "ch_unity":   (220, 180, 100),   # Unity — gold-amber (matches Unity tab)
    "ch_assist":  (130, 220, 210),   # Assist Channel — teal (matches Assist tab)
    # Composer-label-only pink for the tell/reply channels, matching the
    # Tell tab color (255,170,230). Kept as its own class so it tints the
    # composer selector WITHOUT recoloring incoming tell sender names
    # (those still use ch_tell). Tell message bodies are white (set via
    # CHAT_MSG_COLOR_BY_MODE below).
    "ch_tell_composer": (255, 170, 230),   # tell/reply composer label — pink
    "ch_other":   (200, 200, 200),   # fallback grey
    "ch_system":  (170, 190, 210),   # system/checkparam — soft blue-grey
    # Explicit body color classes so chat_packets.lua can color a
    # message body directly, bypassing the mode-based color override
    # (CHAT_MSG_COLOR_BY_MODE). Needed for yell, whose mode can land
    # on a value that the override would tint tell-purple. Using a
    # fixed class guarantees the canonical channel color regardless
    # of the mode byte.
    "body_yell":  (240, 240, 240),   # /yell body — white (sender stays colored)
    "body_shout": (240, 240, 240),   # /shout body — white (sender stays colored)
    # /say body — white. Originally the say path used 'default' as
    # the body color, which resolved to gray-white (200, 200, 200);
    # bumped to a true white (240, 240, 240) so /say messages from
    # other players stand out from system text (which uses default).
    "body_say":   (240, 240, 240),
    # Yell zone tag — chat_packets.lua emits "[ZoneName]" as its own
    # segment between sender name and the colon, using this class. Paler
    # than ch_yell so the eye reads "speaker first, zone second" rather
    # than the bracket competing with the name. Matches the same color
    # used in the text-path bracket-split renderer (CHAT_YELL_ZONE_COLOR
    # constant) — kept in sync so packet-sourced and text-sourced yells
    # look identical.
    "zone_tag":   (255, 220, 130),   # /yell [Zone] tag — pale yellow
}

# Timestamp prefix dim color.
CHAT_TIMESTAMP_COLOR   = (140, 140, 140)
# Self-identity color: the player's own name renders in this color
# in any chat line, regardless of which channel it came in on. Applied
# in draw_chat_panel when a ch_*-class span (sender region) contains
# the player's character name. Other senders keep their channel-themed
# ch_* colors so channels remain visually distinct.
CHAT_SELF_NAME_COLOR   = (140, 200, 255)       # soft blue
# Background colors. Slightly different from other panels to read as
# a distinct surface. Header is a thin top strip.
CHAT_BG_COLOR          = (12, 14, 18, 235)     # near-black, mostly opaque
CHAT_HEADER_COLOR      = (28, 32, 40, 235)
CHAT_BORDER_COLOR      = (60, 70, 85)
# "Jump to bottom" badge that appears when scrolled up + new events arrive.
CHAT_BADGE_BG          = (40, 80, 120)
CHAT_BADGE_FG          = (240, 240, 240)

# Visual style for the tab strip.
CHAT_TAB_BG_INACTIVE   = (20, 22, 28, 235)
CHAT_TAB_BG_ACTIVE     = (45, 55, 75, 235)
CHAT_TAB_FG_INACTIVE   = (160, 170, 180)
CHAT_TAB_FG_ACTIVE     = (240, 240, 240)
CHAT_TAB_UNREAD_BG     = (180, 70, 60)       # red badge background
CHAT_TAB_UNREAD_FG     = (250, 250, 250)

# Per-tab color theme. Active tab uses the "active" tuple as foreground;
# inactive uses "inactive". Underline beneath active tab takes the
# active tuple too. Order matches chat_tab_names — must update both
# in lockstep if tabs are added/reordered.
#
# Colors chosen to mirror the message palette: World matches yells
# (orange-yellow), LS1/LS2 take light/dark green, Party takes cyan
# (matches /party message color), Battle is red, System is light
# gray, Custom slots are dim gray.
CHAT_TAB_PALETTE = [
    {"active": (255, 170, 230), "inactive": (185, 120, 165)},  # 0  Tell     — pink (FFXI tell color)
    {"active": (255, 200, 100), "inactive": (180, 140,  80)},  # 1  World    — orange-yellow
    {"active": (130, 220, 210), "inactive": ( 90, 160, 150)},  # 2  Assist   — teal (cross-zone assist)
    {"active": (220, 180, 100), "inactive": (160, 130,  75)},  # 3  Unity    — gold-amber (concord)
    {"active": (130, 230, 130), "inactive": ( 90, 160,  90)},  # 4  LS1      — light green
    {"active": ( 60, 150,  60), "inactive": ( 45, 105,  45)},  # 5  LS2      — dark green
    {"active": (140, 220, 255), "inactive": (100, 160, 190)},  # 6  Party    — cyan
    {"active": (235,  80,  80), "inactive": (165,  60,  60)},  # 7  Battle   — red
    {"active": (120, 200, 255), "inactive": ( 85, 145, 185)},  # 8  Buffs    — sky blue
    {"active": (220, 130, 220), "inactive": (160,  95, 160)},  # 9  Debuffs  — magenta-purple
    {"active": (255, 110, 110), "inactive": (180,  80,  80)},  # 10 Mob      — soft red
    {"active": ( 70, 110, 215), "inactive": ( 50,  78, 150)},  # 11 Custom 1 — dark blue
    {"active": (255, 150,  60), "inactive": (185, 110,  50)},  # 12 Custom 2 — orange
    {"active": (200, 200, 200), "inactive": (140, 140, 140)},  # 13 System   — light gray
    {"active": (255, 215,  80), "inactive": (180, 150,  55)},  # 14 Gearswap — gold
]


# ── Mode classification helpers ───────────────────────────────────
# Empirical mode groupings based on in-game capture (May 2026). These
# are subject to refinement as more modes are identified. The sets
# below are read by the per-tab filter predicates; updating a set
# here automatically updates every tab that uses it.
#
# We use sets rather than enums or literals so the membership check
# in filters is O(1) and adding a newly-identified mode is a one-line
# edit instead of a multi-clause boolean.
#
# When a mode is unknown (not in any set), the All tab still catches
# it, and the user can investigate via chat_listen.py / //ow chatdump
# to identify what mode FFXI emitted.

# World-range chat: say + shout + yell + emotes. One tab for every
# spoken-aloud message regardless of range. Emote modes (13, 14) are
# still guessed — verify with hex capture if emotes don't show up.
CHAT_MODE_SET_WORLD    = {1, 3, 11, 13}
# Confirmed via chatdebug capture:
#   mode 1  → /say  (own + immediate)
#   mode 3  → /shout (own — text-mode 3, not text-mode 10)
#   mode 11 → /yell
#   mode 13 → emote fallthrough
# Text-mode 10 (other players' /shouts) is DROPPED at the lua side
# in emit.lua's DROPPED_CHAT_MODES set, because those shouts also
# arrive via the 0x017 chat-packet path → chat_packets.lua →
# chat_shout. Suppressing here would create a duplicate; suppressing
# at the lua level prevents the dupe from ever crossing the wire.
CHAT_MODE_SET_LS1      = {6, 205}              # LS1 chat (text mode 6),
                                                #   LS message-of-the-day
                                                #   on login (205, empirical)
CHAT_MODE_SET_LS2      = {217}                 # LS2 message-of-the-day
                                                #   (mode 217, confirmed via
                                                #   chatdebug: "[2]< <LS2>:
                                                #   <setter> >" + body). Regular
                                                #   LS2 chat still arrives on
                                                #   its own mode and is tagged
                                                #   kind=chat_ls2 by the lua
                                                #   side; 217 is specifically
                                                #   the MoTD. Was empty before
                                                #   (LS2 MoTD fell to System).
CHAT_MODE_SET_PARTY    = {5}                   # /party — chat only
# Emotes — confirmed from Asura packet log:
#   mode 7  = self-emote ("Wormfood shakes with laughter!")
#   mode 9  = numeric / party emote ("Ochatea : 70")
#   mode 15 = targeted social emote ("Aquathea bows courteously to ...")
CHAT_MODE_SET_EMOTE    = {7, 9, 15}
CHAT_MODE_SET_BATTLE   = {                     # combat-derived messages
    20, 21, 22, 28, 29, 30, 31, 32, 33, 34, 36, 37, 38, 39,
    50, 56, 59, 60, 61, 62, 63, 64, 65, 101,
    # NOTE: mode 35 was previously in this set but it's actually
    # the FFXI Assist Channel (cross-zone chat with the (E) language
    # tag, format `..Name..(E) : message`), not a battle-text mode.
    # Routed to chat_assist via the dedicated classifier branch.
}
# NPC dialog — confirmed mode 144 ("Yoskolo : Welcome to the Merry
# Minstrel's Meadhouse.") and mode 150 (NPC conversation, e.g.
# "Jeggim : Without a watercraft..."). NPC dialog also re-fires on
# mode 152 (duplicate framing) — emit.lua drops 152 so it isn't
# triplicated, so only 144/150 reach here. Separate set so NPC
# chatter routes to its own channel (chat_npc) and can be filtered.
CHAT_MODE_SET_NPC      = {144, 150}
CHAT_MODE_SET_SYSTEM   = {                     # system / RoE / sparks / drops
    0, 121, 122, 123, 127, 131, 148,           # 0 = area announce
                                                # 123 = no-LS-equipped etc
                                                # 121 = shop/award notices
                                                # 127 = RoE objectives
                                                # 131 = limit/capacity points
                                                # 148 = signet / nation
    161,                                        # 161 = world announcements:
                                                # Kupofried "ancient magic"
                                                # buff + "Page N of the tome
                                                # flares up!" (Ambuscade tome).
                                                # Confirmed via trace.
}

# Modes that carry area-broadcast loot/RoE/exp gains from OTHER
# players ("Kly obtains 5,000 gil.", "Drakaros gains 976 experience
# points."). These are zone-wide noise in busy areas. Lines on these
# modes get a text-pattern check: if the line opens with a name
# that ISN'T the player's own and the verb is loot-like, the line
# gets re-tagged chat_loot_other (hidden by default; users who want
# to see other-player gains can route it to a tab via the routing
# GUI). The player's OWN gains land in System as before.
#
# Confirmed modes via chatdebug:
#   127 — "Kly obtains 5,000 gil"  (loot / gil drops)
#   131 — "Drakaros gains 976 experience points"  (exp gains)
# Add more if/when observed.
CHAT_MODE_SET_LOOT_NOISE_MODES = {127, 131}


# ── Tab filter predicates ─────────────────────────────────────────
# Each filter takes one event dict and returns True if that event
# belongs in the corresponding tab. Order matches chat_tab_names.
# Pure functions — no side effects. Cheap enough to call per-event.
#
# Battle catches CHAT_MODE_SET_BATTLE (combat-derived messages).
# Custom 1 and Custom 2 return False (empty filter) until the user
# wires up keyword/sender rules for them. They render as empty tabs.

# Mode 122 ambiguity: FFXI uses mode 122 for a mixed bag of error and
# notification lines — some are battle-context ("X is too far away",
# "Unable to see Y", "out of range"), others are non-battle system
# messages (RoE confirmations, "Cannot use here", etc.). The mode byte
# alone can't distinguish them, so we look at the text. Lines containing
# any of these phrases go to Battle; everything else on mode 122 stays
# in System. Match is case-insensitive substring; the canonical FFXI
# wording for each error is stable across game versions.
_BATTLE_KEYWORDS_MODE_122 = (
    "out of range",
    "too far away",
    "unable to see",
    "cannot see",
    "is not facing",
    "must wait longer",
    "cannot perform",          # "...cannot perform this action on a member of..."
    "no longer engaged",
    "no valid target",
    "is fighting someone else",
)

def _is_mode_122_battle_line(ev):
    """True if a mode-122 event is battle-context, by text keyword match.
    Returns False for any non-122 mode or any mode-122 line without a
    matching keyword. Cheap — substring scan over ~50-char text."""
    if ev.get("mode") != 122:
        return False
    text = (ev.get("text") or "").lower()
    if not text:
        return False
    for kw in _BATTLE_KEYWORDS_MODE_122:
        if kw in text:
            return True
    return False




# =========================================================================
# SECTION: B_routing
# =========================================================================

# ─────────────────────────────────────────────────────────────────────
# Chat routing system
# ─────────────────────────────────────────────────────────────────────
# Events are classified into (actor_class, channel) cells. The routing
# grid maps each cell to a list of tab names. An event with no matching
# cell falls back to ["Battle"]. An empty list means "hide this event".
#
# actor_class values (from chat/classifier.lua):
#   self, party, alliance, other, pet, party_pet, other_pet,
#   mob_engaged (your group has claim), mob_passive (another party's
#   claim or unclaimed), npc, system
#   ('mob' is accepted as a legacy alias for mob_engaged in routing
#    lookups so pre-split configs keep working.)
#
# channel values are derived from the event's source and content:
#   chat_say, chat_shout, chat_yell, chat_tell, chat_party, chat_ls1,
#   chat_ls2, chat_emote      — real FFXI chat modes
#   buff_apply, buff_wear     — synthetic from buff_events.lua
#   debuff_apply, debuff_wear — synthetic from buff_events.lua
#   battle                    — synthetic from battle_events.lua (covers
#                               melee/ranged/WS/ability/spell/etc.)
#   checkparam                — synthetic from checkparam_events.lua
#   gearswap                  — text-pattern-matched gearswap echoes
#   system                    — real FFXI system mode lines
#   unknown                   — anything that doesn't match above
#
# Hand-edit `omnichat_chat_routing.json` (or use the future GUI) to
# customize routing. If the file is missing or malformed, defaults
# below are used.

import re as _re_routing
_GEARSWAP_TEXT_PREFIXES_R = ("[GearSwap]", "[CHAR]", "[Macro Set:")
_GEARSWAP_STATE_PATTERN_R = _re_routing.compile(
    r"^[A-Za-z][A-Za-z0-9 ]{0,30} is now [A-Za-z0-9_]{1,40}\.$"
)

# COR roll-broadcast text. FFXI publishes the resolution of every roll
# as a single line listing all affected party members followed by the
# roll name, the rolled number, and the resulting bonus. Example forms:
#   "Wormfood, Yoran-Oran, ... . Samurai Roll <8> (+62 Store TP Bonus)"
#   "Wormfood, ... . Chaos Roll <11>.(Lucky!) (+66% Attack!)"
#   "Bust! Wormfood, ... . Chaos Roll <5> (-9.76% Attack!)"
#
# These broadcasts arrive on /say-class modes (1/3/etc.) and would
# otherwise land in World. Route them to Battle instead — they're
# combat output that belongs with the rest of the COR's roll info.
# The buff itself (Store TP Bonus, Attack% bonus) is independently
# applied via the action handler and renders on the Buffs tab, so
# this routing doesn't lose anything.
_ROLL_BROADCAST_PATTERN_R = _re_routing.compile(
    r"(Roll[\s\S]*Lucky|Roll[\s\S]*Unlucky|Roll's Lucky #|"
    r"Roll[\s\S]*\(Bust!\)|^Bust!.*Roll)"
)

# Cast-start / ready text. FFXI's native incoming_text fires when any
# entity starts casting or readies an ability:
#   "Wormfood starts casting Honor March on Wormfood."
#   "The Goblin Leecher readies Hundred Fists."
#   "Yoran-Oran starts casting Cure IV on Wormfood."
# These arrive on mode 0 and would route to System by default. They're
# combat events — Battle is the right destination. Pattern matches the
# canonical verb phrases regardless of who the actor is.
_CAST_READY_PATTERN_R = _re_routing.compile(
    r"\b(starts casting|readies)\b"
)

# "Unable to cast" / "Unable to use" — close cousin of cast-start.
# Triggered when a spell or JA is blocked by silence, status, recast,
# distance, etc. Same routing logic: combat-context, belongs on Battle.
#   "Unable to cast spells at this time."
#   "Unable to use that ability now."
_CAST_BLOCKED_PATTERN_R = _re_routing.compile(
    r"^Unable to (cast|use)\b"
)

# BattleMod-formatted combat lines injected into chat as raw text.
# BattleMod rewrites FFXI's battle log into "Actor verb Ability → Target"
# form using a Unicode RIGHTWARDS ARROW (→, U+2192). FFXI's own
# incoming_text never uses that arrow — native lines read "X starts
# casting Y on Z." / "X hits Y for N points of damage." So the arrow is
# a reliable signature for a BattleMod text line.
#
# These duplicate OmniWatch's own colored synth (battle_events.lua
# builds the same line from the 0x028 action packet, classified AND
# ally-gated). When BattleMod is loaded alongside OmniWatch its text
# lines leak in via incoming_text on a mode we don't drop, landing in
# System via the unknown→System fallback — including OTHER parties'
# fights, since incoming_text carries no affiliation and the actor name
# often won't resolve to a mob id. Example seen in the wild:
#   "Apex Raptor casts ERROR 111 → ArkEV"
# (an unaffiliated mob's cast on an unaffiliated player; "ERROR 111" is
# BattleMod's own failed spell-id resolution).
#
# Disposition: route to raw_battle, which defaults to hidden — exactly
# how we treat FFXI's native battle modes. The colored synth is the
# canonical source; the BattleMod text twin is redundant. Users who
# WANT these can route raw_battle to a tab in the GUI.
_BATTLEMOD_LINE_PATTERN_R = _re_routing.compile("\u2192")

# /check examine lines — generated when another player runs /check on
# you or someone else /checks a mob nearby. FFXI sends these on a
# battle-range mode byte (typically 36), so the default mode-based
# classifier sends them to raw_battle. But they're informational, not
# combat: the player just wants to know who's checking them. Route to
# System instead. Patterns covered:
#   "Vynseres examines you."
#   "<Name> examines <Target>."
#   "<Name> seems to be looking at <Target>."
# Stable, low-risk to false-positive — players don't write "examines"
# in chat the same way they write "cast" / "ready".
_EXAMINE_PATTERN_R = _re_routing.compile(
    r"\b(examines|seems to be looking at)\b"
)

# Bazaar transaction notices — sent to the seller when a buyer
# purchases something from their bazaar. Wording variants observed
# across FFXI versions:
#   "<Player> has bought your <item> for <N> gil."
#   "<Player> bought your <item>."
#   "<Player> bought <item> from your bazaar."
# Pattern matches either "bought your" (anchored to the possessive,
# so player chat like "I bought a sword" doesn't trip it) or
# "from your bazaar" (the standalone bazaar phrase). Routed to
# System by default.
_BAZAAR_PATTERN_R = _re_routing.compile(
    r"\bbought\s+your\b|\bfrom\s+your\s+bazaar\b",
    _re_routing.IGNORECASE
)

# Skillchain lines. FFXI emits the skillchain result on a battle-range
# mode byte (mode 20 confirmed via trace: "Fragmentation: 1023 → Apex
# Crab"), so the default mode classifier sends them to raw_battle
# (hidden). But a skillchain IS a weaponskill outcome the player wants
# to see, so route it to the 'weaponskills' channel — the same place WS
# damage goes — and it follows whatever the user routed weaponskills to.
# Matched by the finite set of skillchain property names anchored at a
# word boundary (these are not words players type in normal chat the way
# they appear here, immediately followed by ':' or 'Skillchain:'), so
# false-positive risk is low. Both "<Name>:" and "Skillchain: <Name>"
# forms are covered.
_SKILLCHAIN_PATTERN_R = _re_routing.compile(
    r"\b(Light|Darkness|Radiance|Umbra|Gravitation|Fragmentation|"
    r"Fusion|Distortion|Compression|Liquefaction|Induration|"
    r"Reverberation|Transfixion|Scission|Detonation|Impaction)\s*:"
    r"|\bSkillchain\b"
)

# Unity NPC parameter-broadcast dumps. The Unity Concord NPC periodically
# emits raw parameter packets (msg-ids ~0x01F0-0x01FF) that arrive on the
# Unity chat path as a body of comma-separated hex groups, e.g.
#   "0a,01f1,0000000a,0000002f,00000121,00000001,00000001,"
# and frequently with an EMPTY-sender colon prefix from the packet path:
#   " : 0a,01f1,0000000a,..."
# These are NOT chat — they're the binary parameter payload leaking as
# text. We .search() (not match) for the comma-hex run anchored at the
# END of the line, allowing any "sender : " prefix in front. The run must
# start with a short-hex group IMMEDIATELY followed by a comma and include
# at least one 8-digit hex group — so ordinary chat ("Thoroar[BastokMark]:
# msg", "Name : {Mhaura}?") can't match (no leading "hhhh," + 8-hex run).
_PARAM_DUMP_PATTERN_R = _re_routing.compile(
    r"(?:^|\s|:)[0-9a-fA-F]{1,4},(?:[0-9a-fA-F]{1,8},)*"
    r"[0-9a-fA-F]{8}(?:,[0-9a-fA-F]{1,8})*,?\s*$"
)

# Chat kinds that should short-circuit text-pattern classification.
# These are SPECIFIC player-chat channels — the lua side has already
# identified them by player-chat mode bytes (0/1/3/4/5/7/26/27 in
# 0x017 packet space). Generic catchalls (chat_npc, chat_other) are
# NOT in this set and fall through to text-pattern matchers so they
# can be re-classified as examine / bazaar / system / etc.
_CHAT_KIND_SPECIFIC = frozenset({
    "chat_say", "chat_tell", "chat_shout", "chat_yell",
    "chat_party", "chat_ls1", "chat_ls2", "chat_emote",
    # chat_assist — the FFXI Assist Channel, tagged by chat_packets.lua
    # when mode 35 arrives (cross-zone hub chat with the `..Name..(E):`
    # language-preference wrapper). Was previously missing from this
    # set, so when chat_packets sent kind='chat_assist' the Python
    # classifier didn't recognize it as authoritative and fell through
    # to text-pattern checks, eventually defaulting to chat_other →
    # System. Adding here makes the packet-path tag honored directly.
    # (The incoming-text path uses a separate mode==222 short-circuit
    # in the classifier below; these are the two paths assist arrives on.)
    "chat_assist",
    # chat_unity — Unity Concord chat (mode 33). Same pattern as
    # chat_assist: chat_packets.lua tags the packet, the kind needs
    # to be in this set so the Python classifier honors the tag
    # directly instead of falling through to defaults.
    "chat_unity",
})

# Parses cast-start / ability-ready lines into actor / verb / ability /
# target groups for colorization. Matches:
#   "Wormfood starts casting Foe Lullaby II on the Huge Hornet."
#   "Yoran-Oran starts casting Cure IV on Wormfood."
#   "The Goblin Leecher starts casting Firaga III on Wormfood."
#   "Huge Hornet readies Final Sting on Wormfood."
#   "Wormfood starts casting Honor March on Wormfood."   ← no target prefix; same-name case
# Captures:
#   actor   (greedy up to verb; may include "The " prefix)
#   verb    ("starts casting" or "readies")
#   ability (spell or move name)
#   target  (may include "the " prefix; trailing period stripped)
_CAST_PARSE_R = _re_routing.compile(
    r"^(?P<actor>.+?)\s+(?P<verb>starts casting|readies)\s+"
    r"(?P<ability>.+?)"
    r"(?:\s+on\s+(?P<target>.+?))?\.?$"
)

def _chat_classify_name(name):
    """Best-effort actor_class for a name string, using current state.

    Returns one of: 'self', 'party', 'alliance', 'pet', 'party_pet',
    'mob', 'other'. 'self' for the active player. Party / alliance
    walk the party_data / ally1_data / ally2_data lists. Mob match
    is heuristic (current target or "the X" prefix). Falls back to
    'other'.
    """
    if not name:
        return "other"
    clean = name.strip()
    # Strip leading "The " / "the " for mob name comparisons. We
    # remember whether the prefix was there to bias toward 'mob'
    # for ambiguous matches.
    had_the_prefix = False
    if clean[:4].lower() == "the ":
        clean = clean[4:]
        had_the_prefix = True
    # Self.
    if player_self_name and clean == player_self_name:
        return "self"
    # Party.
    try:
        for m in party_data:
            if isinstance(m, dict) and m.get("name") == clean:
                return "party"
        for m in ally1_data:
            if isinstance(m, dict) and m.get("name") == clean:
                return "alliance"
        for m in ally2_data:
            if isinstance(m, dict) and m.get("name") == clean:
                return "alliance"
    except Exception:
        pass
    # Mob: current target match, or "the X" prefix bias.
    try:
        if target_info and isinstance(target_info, dict):
            tn = target_info.get("name") or ""
            if tn and tn == clean:
                return "mob"
    except Exception:
        pass
    if had_the_prefix:
        return "mob"
    return "other"

def _build_cast_segments(text):
    """Parse a cast-start text line into colored segments.

    Returns a list of (text, color_class) tuples, or None if the
    pattern doesn't match (caller falls back to the flat-text path).
    """
    m = _CAST_PARSE_R.match(text)
    if not m:
        return None
    actor   = m.group("actor")
    verb    = m.group("verb")
    ability = m.group("ability")
    target  = m.group("target")   # may be None
    actor_cls  = _chat_classify_name(actor)
    target_cls = _chat_classify_name(target) if target else None
    # Pick ability color. "readies" → ability color (peach); "starts
    # casting" → spell color (mint-aqua).
    ability_cls = "spell" if verb == "starts casting" else "ability"
    segs = [
        (actor, actor_cls),
        (f" {verb} ", "default"),
        (ability, ability_cls),
    ]
    if target:
        segs.append((" on ", "default"))
        segs.append((target, target_cls or "other"))
    segs.append(("." , "default"))
    return segs

# Text patterns that identify FFXI server-broadcast notices. Routes
# events to the 'system' channel regardless of incoming mode byte,
# since FFXI uses inconsistent modes for these notices (some are 0,
# some are 144, some are uncategorized server-broadcast modes we
# don't enumerate).
#
# Two flavors of pattern:
#   _SYSTEM_TEXT_PREFIXES   — text.startswith match
#   _SYSTEM_TEXT_SUBSTRINGS — `marker in text` match (more flexible
#                             but slightly higher false-positive risk)
#
# Both run AFTER all mode-based classification, so /say lines reach
# chat_say via CHAT_MODE_SET_WORLD before reaching here. False-
# positive risk is low because by the time these patterns are
# consulted, the line's mode byte is unknown to us (not /say,
# /tell, /shout, /LS, /party, /yell, raw battle, or known system
# mode).
#
# Add new patterns here when users report system messages landing
# in Battle (or unknown→System fallback): explicit classification
# lets users build routing overrides on the "system" channel that
# fire for those lines specifically.
_SYSTEM_TEXT_PREFIXES = (
    # Login-campaign reward narration
    "In celebration of",
    # Item-obtained announcements
    "Obtained: ",
    "Obtained key item: ",
    "You have obtained ",
    "You received ",
    # Login point balance
    "Login Points: ",
    "Current login points: ",
    "You earned ",
    # World-event broadcasts (Voidwatch, Campaign, Besieged spawns
    # and announcements). SE's standard opener for these is
    # "Word has been received..."
    "Word has been received",
    # Besieged / conquest mobilization notices
    "Forces from ",
    # Conquest tally
    "The conquest tally",
    "Conquest results",
    # Adoulin Colonization Reives — different opening word but
    # always end with "Reive" and route through here when their
    # mode byte isn't in CHAT_MODE_SET_SYSTEM. Both flavors:
    "A Heroes' Reive",
    "A Wildskeeper Reive",
    # Voidwalker / Voidwatch alternate phrasing
    "An ominous aura",
    "The veil between worlds",
)

# Substring markers (case-sensitive, fragment match). These catch
# SE notices whose opening word varies (month names, etc.) but
# whose content has a stable identifier. Anchored to recognizable
# campaign / event phrasing so player chat is unlikely to trip them.
_SYSTEM_TEXT_SUBSTRINGS = (
    "Login Campaign",         # "The May 2026 Login Campaign..."
    "Gratitude Campaign",     # "The Adventurer Gratitude Campaign..."
    "Repeat Login Campaign",
    # World-event broadcast markers — paired with the "Word has
    # been received" prefix above, but also catches variant
    # wordings. "undead threat" / "beastman threat" / "demon
    # threat" etc. cover Voidwatch / Campaign Ops spawns. The
    # phrase is verbose enough that player chat is very unlikely
    # to false-positive.
    "an undead threat",
    "a beastman threat",
    "a demon threat",
    "an aquan threat",
    "a beast threat",
    "a plantoid threat",
    "an arcana threat",
    "an amorph threat",
    "a dragon threat",
    "a lizard threat",
    "a vermin threat",
    "a bird threat",
    "an unsettling presence",   # alternate Voidwatch wording
)

def _chat_classify_event(ev):
    """Return (actor_class, channel) for routing.

    Both strings; channel is derived from source + mode + text shape.
    Returns ('system', 'unknown') as a safe fallback so unclassified
    events still route somewhere (default destination = Battle, which
    is fine for "I don't know what this is" — Battle is the catchall).
    """
    source = ev.get("source") or "chat"
    mode   = ev.get("mode", 0)
    text   = ev.get("text") or ""
    actor  = ev.get("actor_class") or "other"

    # Synthetic events (have explicit source). These are authoritative —
    # the Lua side knows what they are.
    if source == "buff":
        result = ev.get("result")  # 'apply' or 'wear'
        return (actor, "buff_wear" if result == "wear" else "buff_apply")
    if source == "debuff":
        result = ev.get("result")
        return (actor, "debuff_wear" if result == "wear" else "debuff_apply")
    if source == "battle":
        # battle_events.lua tags each synthesized line with its specific
        # combat channel ('melee', 'ranged', 'weaponskills', 'damage',
        # 'healing', 'casting', 'readies', 'abilities', 'uses', 'misses').
        # Fall back to a generic 'battle' bucket if the tag is missing —
        # which only happens on old Lua side or events before the kind
        # field was added.
        return (actor, ev.get("kind") or "battle")
    if source == "system" and mode == -2:
        return ("system", "checkparam")

    # Chat-source events may carry an explicit `kind` field to override
    # mode-based routing. Used by chat_packets.lua for cases where the
    # mode byte doesn't disambiguate (e.g. LS message-of-the-day for
    # LS2 — mode 205 alone can't tell LS1 from LS2; the Lua handler
    # tags `kind = "chat_ls2"` to route correctly).
    #
    # Two tiers of chat_ kinds:
    #   1. Specific player-chat channels (chat_say/tell/shout/yell/
    #      party/ls1/ls2/emote) — honored directly; the lua side has
    #      already identified them by their player-chat mode bytes.
    #   2. Generic catchalls (chat_npc, chat_other) — used by the lua
    #      side for unknown-mode packets that may carry routable info
    #      (NPC dialog, /check examines, bazaar sales, server notices).
    #      These DON'T short-circuit classification; we still run the
    #      text-pattern matchers below so a generic-kinded event with
    #      "examines you" content lands on the examine channel instead.
    if source == "chat":
        kind = ev.get("kind")
        if kind and kind in _CHAT_KIND_SPECIFIC:
            return (actor, kind)
        # Generic chat_ kinds fall through to text patterns below.

    # FFXI Assist Channel — mode 222 carries cross-zone broadcast
    # chat in major hub cities (the `..Name..(E) : message` format).
    # Mode 35 carries the same content via the chat-packet path
    # (chat_packets.lua already tags those with chat_assist before
    # we get here — that branch hits the source=="chat" specific-
    # kind shortcut above). The 222 short-circuit catches the
    # incoming-text path's version of the same chat. Routed to the
    # dedicated Assist tab between World and LS1.
    #
    # Short-circuited HERE (before any text-pattern checks below) so
    # an assist line that happens to mention loot keywords, examine
    # phrases, or other catch-patterns doesn't get redirected. The
    # mode byte is the authoritative signal — text content is just
    # the message body.
    if mode == 222:
        return (actor, "chat_assist")

    # Unity Concord chat — mode 212 (the incoming-text path) carries both
    # Unity member chat ("{Name} body", including 1-char "." check-ins)
    # AND Unity NPC dialogue ("{Yoran-Oran} ..."). Route ALL of it to
    # chat_unity → Unity tab. This MUST be here: the ingestion-side
    # brace-split that colors the sender gold only runs when kind ==
    # 'chat_unity', and the chat_packets mode-33 path is intentionally
    # dropped (it duplicated mode 212), so without this route mode-212
    # Unity lines get no kind and Unity chat vanishes entirely.
    # Short-circuited before the text-pattern matchers so a Unity line
    # mentioning loot/quest words can't be pulled into System/Battle.
    if mode == 212:
        return (actor, "chat_unity")

    # Mode 211 — your OWN outgoing Unity chat ("{Wormfood} ..."). Echoes
    # on 211, not 212/33. Same destination as 212 → Unity tab, gold name.
    if mode == 211:
        return (actor, "chat_unity")

    # Emote modes (7/9/15 on Asura) → chat_emote → World. Short-
    # circuited BEFORE text-pattern checks below so an emote line
    # ("Knightedge waves") with mob-like phrasing doesn't get
    # misrouted to Battle or System via a pattern false-positive.
    # Confirmed via chatdebug capture: mode 15 carries social
    # emotes like `..Knightedge.. waves` and was leaking to System
    # before this early bail because the text-pattern fallback in
    # the unknown-mode branch caught them.
    if mode in CHAT_MODE_SET_EMOTE:
        return (actor, "chat_emote")

    # Real FFXI text. First check the text-pattern overrides (Gearswap
    # state-set echoes piggyback on mode 1, indistinguishable by mode).
    for prefix in _GEARSWAP_TEXT_PREFIXES_R:
        if text.startswith(prefix):
            return ("other", "gearswap")
    if _GEARSWAP_STATE_PATTERN_R.match(text):
        return ("other", "gearswap")

    # COR roll broadcasts: route to Battle. FFXI sends these on
    # say-class modes (mode 1/3/etc.) so they'd land in World by
    # default, but they're combat output, not chat. Match before
    # mode-based dispatch so /say chat content isn't affected.
    if _ROLL_BROADCAST_PATTERN_R.search(text):
        return (actor, "battle")

    # Cast-start / ready lines and cast-blocked notices route to Battle.
    # Arrive on mode 0 from incoming_text, would otherwise hit System.
    if _CAST_READY_PATTERN_R.search(text):
        return (actor, "casting")
    if _CAST_BLOCKED_PATTERN_R.search(text):
        return (actor, "battle")

    # Skillchain result lines ("Fragmentation: 1023 → Apex Crab",
    # "Skillchain: Distortion ...") — a weaponskill outcome from your
    # fight. These CONTAIN the → arrow, so they MUST be checked BEFORE the
    # generic BattleMod-arrow catch below (which would otherwise grab them
    # into raw_battle/hidden). Route to 'weaponskills' with actor forced
    # to 'self': the skillchain text carries no resolvable sender
    # (emit.lua defaults it to 'other', which has no weaponskills routing
    # cell and would fall to System), so attributing it to 'self' makes it
    # follow exactly where your own weaponskills are routed.
    if mode in CHAT_MODE_SET_BATTLE and _SKILLCHAIN_PATTERN_R.search(text):
        return ("self", "weaponskills")

    # BattleMod-formatted combat text (contains the → arrow). Redundant
    # with our own colored synth and carries no affiliation, so other
    # parties' fights leak in. Route to raw_battle (hidden by default),
    # the same disposition as FFXI's native battle modes. Checked after
    # the real cast/ready patterns so a legitimate "starts casting … on
    # …" line (no arrow) still reaches the casting channel above.
    if _BATTLEMOD_LINE_PATTERN_R.search(text):
        return (actor, "raw_battle")

    # /check examine lines — informational, route to System. Arrive on
    # battle-range modes (typically 36) so without this check they'd
    # fall through to raw_battle and either hide or land in Battle.
    if _EXAMINE_PATTERN_R.search(text):
        return (actor, "examine")

    # Bazaar sale notices — same disposition as examine but a
    # distinct channel so users can route bazaar separately (e.g. to a
    # Custom tab when actively selling). Defaults to System.
    if _BAZAAR_PATTERN_R.search(text):
        return (actor, "bazaar")

    # Mode 122 needs sub-classification (battle context vs system).
    if mode == 122:
        if _is_mode_122_battle_line(ev):
            return (actor, "raw_battle")
        return ("system", "system")

    # Raw FFXI battle log modes (28/29/30/etc.) — these are the lines
    # FFXI's client itself generates, like "Wormfood scores a critical
    # hit!The Belaboring Wasp takes 2300 points of damage." When our
    # synthesizer fires for the same action packet, the colored synth
    # version makes these duplicates. Route them to a 'raw_battle'
    # channel that defaults to hidden — they only appear if the user
    # explicitly routes them somewhere.
    if mode in CHAT_MODE_SET_BATTLE:
        return (actor, "raw_battle")

    # Emote check is now done EARLIER in the classifier, before
    # text-pattern matching, so it can't be stolen by a stray
    # pattern. The early bail covers modes 7/9/15.

    # NPC dialog (mode 144) → chat_npc → System. Lets users filter
    # NPC chatter (shopkeepers, quest NPCs) separately from real chat.
    if mode in CHAT_MODE_SET_NPC:
        return (actor, "chat_npc")

    # Chat-mode based channels (real FFXI player chat).
    if mode in CHAT_MODE_SET_WORLD:
        # World captures say/shout/yell/emote.
        if mode == 1 or mode == 2:    return (actor, "chat_say")
        if mode == 3:                  return (actor, "chat_shout")
        if mode == 11:                 return (actor, "chat_yell")
        return (actor, "chat_emote")
    if mode in CHAT_MODE_SET_LS1:    return (actor, "chat_ls1")
    if mode in CHAT_MODE_SET_LS2:    return (actor, "chat_ls2")
    if mode in CHAT_MODE_SET_PARTY:  return (actor, "chat_party")
    if mode == 4 or mode == 12:      return (actor, "chat_tell")
    if mode in CHAT_MODE_SET_SYSTEM:
        # Some "system" modes (127 for loot, 131 for exp) carry BOTH
        # the player's own gains AND area-wide gains from OTHER
        # players. The mode alone doesn't tell us which. If the line
        # opens with another player's name and uses a loot-like verb
        # ("obtains" / "gains" / "found" / "loots"), treat it as
        # area-noise and route to chat_loot_other (hidden by default;
        # users who want to see it can route it via the GUI). Own
        # gains stay in System where they always were.
        if mode in CHAT_MODE_SET_LOOT_NOISE_MODES:
            stripped = (text or "").lstrip()
            for verb in (" obtains ", " gains ", " found ", " loots "):
                vi = stripped.find(verb)
                if vi <= 0:
                    continue
                name_token = stripped[:vi].strip()
                # Empty / multi-word / matches player → leave to
                # normal system routing (player's own gain or a line
                # we can't parse confidently).
                if (not name_token
                        or " " in name_token
                        or name_token == player_self_name):
                    break
                # Otherwise: someone else's gain → area noise.
                return ("other", "chat_loot_other")
        return ("system", "system")

    # System message text-pattern detection. Runs AFTER all mode-based
    # classification, so /say lines starting with "The cat..." reach
    # chat_say via CHAT_MODE_SET_WORLD above — this only catches lines
    # whose mode byte isn't in any known set (FFXI uses a grab-bag
    # of mode bytes for server-broadcast notices: login campaigns,
    # gratitude campaigns, item-obtained announcements, etc.).
    #
    # Two pattern sets:
    #   _SYSTEM_TEXT_PREFIXES   — text.startswith (anchored, safest)
    #   _SYSTEM_TEXT_SUBSTRINGS — substring `in` (catches notices
    #                             whose opening word varies, like
    #                             "The May 2026 Login Campaign...")
    #
    # Add new entries to the appropriate set when a server notice
    # lands in System only by the unknown→System fallback below
    # (still works, but explicit classification lets users build
    # routing overrides on the "system" channel that take effect
    # for that line).
    for prefix in _SYSTEM_TEXT_PREFIXES:
        if text.startswith(prefix):
            return ("system", "system")
    for marker in _SYSTEM_TEXT_SUBSTRINGS:
        if marker in text:
            return ("system", "system")

    # Anything else (raw FFXI battle modes 28/29/30 etc. when not using
    # synthesis, weird modes from addons, etc.) gets bucketed as
    # 'unknown' → default destination (System; was Battle until the
    # 'unknown' routing default was changed to stop polluting Battle).
    # If the lua side set a generic chat_ kind, preserve it so users
    # can build routing for chat_npc vs chat_other independently.
    if source == "chat":
        kind = ev.get("kind")
        if kind in ("chat_npc", "chat_other"):
            return (actor, kind)
    return (actor, "unknown")


# ─────────────────────────────────────────────────────────────────────
# Default routing grid
# ─────────────────────────────────────────────────────────────────────
# Format: routing[actor][channel] = [tab_name, ...]
#   - Empty list = hide this event
#   - Missing entry = default destination (["Battle"])
#   - Multiple tabs = event appears in each
#
# Use "*" as the actor wildcard for "any actor class". When looking up,
# specific actor wins over "*".

_CHAT_ROUTING_DEFAULTS = {
    "*": {
        # Real player chat channels — uniform across actors
        "chat_say":    ["World"],
        "chat_shout":  ["World"],
        "chat_yell":   ["World"],
        "chat_emote":  ["World"],
        "chat_tell":   ["Tell"],
        "chat_party":  ["Party"],
        "chat_ls1":    ["LS1"],
        "chat_ls2":    ["LS2"],
        # Gearswap → its own tab, NOT in World
        "gearswap":    ["Gearswap"],
        # System → System tab
        "system":      ["System"],
        "checkparam":  ["System"],
        # Battle synthesis — Battle tab.
        #
        # 'battle' is the generic catch-all the synthesizer emits for
        # events that don't fit a more specific kind. The sub-channels
        # below are emitted by battle_events.lua for specific event
        # types — melee swings, ranged attacks, weaponskill animations,
        # JA uses, item uses, spell casts, etc. They all route to the
        # Battle tab here so users can per-job override individual
        # sub-channels (e.g. hide "misses" while keeping damage).
        #
        # Without these explicit entries, sub-channel events fell
        # through to the unknown→System fallback below, polluting the
        # System tab with combat output. (That regression appeared
        # when unknown→System was added — under the previous
        # unknown→Battle default, the fall-through masked this gap.)
        "battle":        ["Battle"],
        "melee":         ["Battle"],
        "ranged":        ["Battle"],
        "weaponskills":  ["Battle"],
        "abilities":     ["Battle"],
        "uses":          ["Battle"],
        "damage":        ["Battle"],
        "healing":       ["Battle"],
        "casting":       ["Battle"],
        "readies":       ["Battle"],
        "misses":        ["Battle"],
        # Raw FFXI battle log (modes 28/29/30/etc.) — HIDDEN by
        # default. The colored synthesizer from battle_events.lua
        # replaces these, so showing both is duplication. Users who
        # want the raw text can override this in their per-job config.
        "raw_battle":  [],
        # /check examine messages ("<Name> examines you." etc.) —
        # informational, default to System. Override-friendly: users
        # who want to ignore check spam can map this to [].
        "examine":     ["System"],
        # Generic chat catchalls — used when chat_packets.lua receives
        # a packet on an unknown mode (NPC dialog, /check messages,
        # bazaar notices, server broadcasts that came via 0x017 instead
        # of incoming text). Default to System; users can override to
        # put NPC dialog in its own tab.
        "chat_npc":    ["System"],
        "chat_other":  ["System"],
        # Assist Channel — FFXI's cross-zone broadcast (the
        # `..Name..(E) :` format common in major hub cities). Has
        # its own dedicated tab between World and LS1 so users can
        # follow the chatter (or mute it entirely) independent of
        # their /yell preferences. Sourced from mode 35 on the lua
        # side; chat_packets.lua tags those packets with chat_assist.
        "chat_assist": ["Assist"],
        # Unity Concord chat (mode 33 → tagged by chat_packets.lua).
        # Default-routes to the dedicated Unity tab so Concord-wide
        # broadcasts and Unity-Leader chatter stay separate from
        # regular chat.
        "chat_unity":  ["Unity"],
        # Area-broadcast loot / RoE / exp gains from OTHER players
        # (e.g. "Kly obtains 5,000 gil", "Drakaros gains 976
        # experience points"). Splits out from chat_system via the
        # text-pattern check in _chat_classify_event for modes
        # 127/131. Defaults to no tab (hidden) so System stays
        # clean. Users who want to see local loot/exp rolls can map
        # this to a Custom tab via the routing GUI. Your OWN gains
        # still land in System unchanged.
        "chat_loot_other": [],
        # Bazaar sale notices ("<Name> bought your <item>...") —
        # informational, default to System. Route to a Custom tab
        # when actively selling if you want a dedicated stream.
        "bazaar":      ["System"],
        # Status events — Battle tab by default. The Buffs/Debuffs/
        # Mob tabs are present but receive nothing automatically; users
        # who want category-specific routing redirect cells in the
        # routing GUI. This avoids pre-filtering: everything combat-
        # related lands in Battle first, the user moves out from there.
        "buff_apply":   ["Battle"],
        "buff_wear":    ["Battle"],
        "debuff_apply": ["Battle"],
        "debuff_wear":  ["Battle"],
        # Unknown / uncategorized — System tab.
        #
        # IMPORTANT: this used to default to Battle, but that turned
        # Battle into a junk drawer for anything the classifier didn't
        # recognize: login campaign messages, "The Adventurer
        # Gratitude Campaign" notices, "Obtained: <item>" lines, and
        # generally any FFXI server-broadcast or notice line whose
        # mode byte isn't in CHAT_MODE_SET_SYSTEM yet. Routing them to
        # Battle confused users who expected Battle to mean "combat
        # log only" (matching FFXI's own chat filter naming).
        #
        # System is a much better default for "I don't know what this
        # is": it's the catchall tab and users won't be surprised to
        # see misc server notices there. Misclassified events that
        # SHOULD be in Battle (e.g. a new battle-mode byte we haven't
        # added yet) will land in System instead, where they're still
        # visible — easier to spot the misclassification and add a
        # proper mode-byte entry than to hunt for them buried in
        # Battle alongside the synthesizer's correct events.
        "unknown":     ["System"],
    },
    # Engaged monsters — your group has claim. Combat output defaults to
    # Battle; the Mob tab is an empty bucket users can redirect mob
    # events into via the routing GUI. (The classifier returns
    # 'mob_engaged' for these; the lookup also accepts a legacy 'mob'
    # cell as an alias, so an older config that used 'mob' keeps working.)
    "mob_engaged": {
        "buff_apply":   ["Battle"],
        "buff_wear":    ["Battle"],
        "debuff_apply": ["Battle"],
        "debuff_wear":  ["Battle"],
        # Mob misses against the player. Visible by default — users
        # can hide them per-job or globally in the routing GUI when
        # they get noisy on a busy fight.
        "misses":       ["Battle"],
        # Mob combat output (TP moves → 'readies', spell casts, melee,
        # damage, etc.). Previously omitted here, so these relied on the
        # '*' wildcard fallback — which failed and dumped mob abilities
        # into the System tab. Route them explicitly to Battle.
        "battle":        ["Battle"],
        "melee":         ["Battle"],
        "ranged":        ["Battle"],
        "weaponskills":  ["Battle"],
        "abilities":     ["Battle"],
        "uses":          ["Battle"],
        "damage":        ["Battle"],
        "healing":       ["Battle"],
        "casting":       ["Battle"],
        "readies":       ["Battle"],
    },
    # Passive mobs — monsters NOT claimed by your group (another party's
    # claim, or unclaimed). The classifier tags these 'mob_passive' so
    # the engaged-vs-passive distinction (what in-game filters and
    # BattleMod expose) is expressible in routing. HIDDEN by default:
    # the common case is "don't show me a nearby party's fight." Users
    # who want passive-mob lines (e.g. watching for a specific NM to be
    # claimed) can route any of these cells to a tab in the GUI.
    #
    # Engaged mobs keep the plain 'mob' actor class above, so an
    # existing mob.* config keeps applying to the fight you're actually
    # in with no migration needed.
    "mob_passive": {
        "buff_apply":   [],
        "buff_wear":    [],
        "debuff_apply": [],
        "debuff_wear":  [],
        "misses":       [],
        "battle":       [],
        "melee":        [],
        "ranged":       [],
        "weaponskills": [],
        "abilities":    [],
        "uses":         [],
        "damage":       [],
        "healing":      [],
        "casting":      [],
        "readies":      [],
    },
}

# Loaded routing config. Set by load_chat_routing()/load_chat_routing_for_job().
# Two levels:
#   _chat_routing_global   — from omnichat_chat_routing.json (global; things
#                            you never want to see regardless of job, plus the
#                            initial defaults shape)
#   _chat_routing_perjob   — from omnichat_chat_routing-<JOB>.json
#                            (per-job overrides; loaded on job change)
#   _chat_routing_current_job — the job string the per-job config was loaded
#                               for. None if not loaded yet.
#
# At lookup time we walk: per-job → global → baked-in defaults.
_chat_routing_global = _CHAT_ROUTING_DEFAULTS
_chat_routing_perjob = {}

# When True, the classifier prints one line per mode-222 event so we
# can confirm assist-channel routing is firing. Toggle via the
# //ow chatroutedebug slash command (lua side just bumps this via UDP).
# Off by default; flipping it on does not affect routing, only logging.
_ow_chat_route_debug = False   # diag — flip on to confirm routing paths
# Global channel-hide toggles + sender blacklist, loaded from the
# routing JSON's _meta section (set by the GUI footer controls).
#   _chat_channel_hidden: set of channel names the user switched off
#     (chat_say/chat_tell/chat_emote/chat_party/chat_shout/chat_yell).
#     Events on these channels are hidden in every tab.
#   _chat_blacklist: set of lowercased sender names. Any chat event
#     whose actor_name matches is hidden regardless of channel.
_chat_channel_hidden = set()
_chat_blacklist      = set()
_chat_routing_current_job = None

# Sentinel: a tier returns this when it has no entry for a cell, which
# is distinct from [] (the tier explicitly routes the cell to no tabs,
# i.e. hidden). Used by _chat_routing_lookup to tell "global is silent"
# apart from "global hides this."
_NO_ENTRY = object()

def _chat_routing_lookup(actor_class, channel, target_dim=None):
    """Return list of tab names for this (actor, channel) cell.

    Walks the resolution chain: per-job config first, then global,
    then baked-in defaults. Specific actor wins over '*' wildcard
    within each level. Missing cell anywhere → ["Battle"] fallback.

    target_dim (optional): a target-side sub-key like "to_me",
    "to_party", "to_other". When provided (for the 'mob_engaged' and
    'mob_passive' actors) the lookup first tries a NESTED cell
    actor_class[target_dim][channel] and only falls back to the flat
    actor_class[channel] cell if the nested one is absent. This lets
    users route, e.g., "mob hits me" to Battle but "mob hits another
    player" to hidden, without disturbing the flat model for anyone
    who hasn't configured target filtering.

    Legacy alias: the classifier used to return a single 'mob' actor
    for all monsters; it now returns 'mob_engaged' (your group's claim)
    or 'mob_passive' (another party's / unclaimed). An older routing
    config (or hand-edited JSON) that still uses the 'mob' key is honored
    for 'mob_engaged' lookups — 'mob_engaged' falls back to a 'mob' cell
    when no explicit 'mob_engaged' cell exists. This means existing
    configs keep routing engaged monsters exactly as before with no
    migration. 'mob_passive' has NO such fallback: passive monsters were
    not separable under the old model, so a legacy 'mob' rule must not
    silently start showing another party's fight.

    Note on layering: GLOBAL IS THE CEILING. If global explicitly
    hides a cell (empty tab list), it stays hidden regardless of the
    per-job config — per-job can never un-hide what global hid. That
    is the whole point of the global tier (e.g. a sender blacklist or
    battle-spam channels the user never wants to see on ANY job). When
    global allows a cell (or is silent on it), the per-job config then
    applies and may hide it or refine which tab it lands in. So per-job
    can only RESTRICT within what global permits, never expand it.
    """
    # Actor keys to try, in order. 'mob_engaged' also accepts a legacy
    # 'mob' cell (see docstring). Every other actor tries only itself.
    if actor_class == "mob_engaged":
        actor_keys = ("mob_engaged", "mob")
    else:
        actor_keys = (actor_class,)

    # Per-tier cell resolver. Returns the cell's tab list if this tier
    # specifies the cell, or _NO_ENTRY if the tier is silent on it.
    # Order within a tier:
    #   1. nested target-specific:  tier[actor][target_dim][channel]
    #   2. flat actor-specific:     tier[actor][channel]
    #   3. nested wildcard target:  tier['*'][target_dim][channel]
    #   4. flat wildcard:           tier['*'][channel]
    # Actor-specific keys are tried in actor_keys order (so an explicit
    # 'mob_engaged' cell wins over a legacy 'mob' cell) before '*'.
    def _tier_lookup(tier):
        if not tier:
            return _NO_ENTRY
        for ak in actor_keys:
            specific = tier.get(ak)
            if specific is not None:
                if target_dim is not None:
                    nested = specific.get(target_dim)
                    if isinstance(nested, dict) and channel in nested:
                        return nested[channel]
                if channel in specific:
                    return specific[channel]
        wildcard = tier.get("*")
        if wildcard is not None:
            if target_dim is not None:
                nested = wildcard.get(target_dim)
                if isinstance(nested, dict) and channel in nested:
                    return nested[channel]
            if channel in wildcard:
                return wildcard[channel]
        return _NO_ENTRY

    global_res = _tier_lookup(_chat_routing_global)
    perjob_res = _tier_lookup(_chat_routing_perjob)

    # GLOBAL IS THE CEILING. If global explicitly hides this cell
    # (empty list), it stays hidden no matter what the per-job config
    # says — per-job can never un-hide what global hid. This is the
    # entire purpose of the global tier (e.g. a sender blacklist the
    # user never wants to see on ANY job).
    if global_res is not _NO_ENTRY and not global_res:
        return []

    # Global allows the cell (or is silent). Now the per-job filter
    # applies: it may hide the cell (empty list) or refine which tab
    # it lands in. Per-job only RESTRICTS within global's permission.
    if perjob_res is not _NO_ENTRY:
        return perjob_res

    # No per-job entry — global's result stands (if it had one).
    if global_res is not _NO_ENTRY:
        return global_res

    # No mapping anywhere — default to System. System is the catchall
    # for "uncategorized notice"; Battle is reserved for events the
    # classifier explicitly identified as combat. See the "unknown"
    # entry in _CHAT_ROUTING_DEFAULTS for the longer rationale.
    return ["System"]

def _chat_target_dim(ev):
    """Derive a target-side dimension key for mob actions, or None.

    Only mob actors ('mob_engaged', 'mob_passive', or the legacy 'mob')
    get target filtering — the use case is routing "mob hits me"
    separately from "mob hits another player," plus distinguishing a mob
    acting on ITSELF (self-buff/cure) from a mob acting on a DIFFERENT
    monster. For every other actor this returns None so the flat routing
    applies unchanged.

    The dimension reuses the classifier's category strings (self/
    party/alliance/pet/party_pet/other/other_pet/npc) for most
    targets. The mob-on-mob case is split into two synthetic keys
    by comparing actor_id and target_id:
      - "self_mob"  the mob targeted itself (actor_id == target_id)
      - "other_mob" the mob targeted a different monster
    """
    if ev.get("actor_class") not in ("mob_engaged", "mob_passive", "mob"):
        return None
    tc = (ev.get("target_class") or "").strip().lower()
    if not tc:
        return None
    _alias = {
        "me":      "self",
        "player":  "self",
        "trust":   "pet",
    }
    tc = _alias.get(tc, tc)
    # Mob-on-mob: split into self vs other by id comparison. Include the
    # engaged/passive classes (a mob self-buff has target_class
    # 'mob_engaged' == its own actor class, not the bare 'mob' alias), so
    # a mob buffing ITSELF resolves to 'self_mob' rather than leaking out
    # as a literal 'mob_engaged' target dimension that no routing cell
    # names.
    if tc in ("mob", "monster", "enemy", "mob_engaged", "mob_passive"):
        actor_id  = ev.get("actor_id")
        target_id = ev.get("target_id")
        if actor_id and target_id and actor_id == target_id:
            return "self_mob"
        return "other_mob"
    return tc


def _chat_route_event(ev):
    """Return set of tab indices this event should appear in.

    Walks the routing grid to find which tab names match, then maps
    those names to indices in chat_tab_names. Unknown tab names in
    the config are silently ignored (so renames don't crash). Empty
    set means hide.
    """
    actor, channel = _chat_classify_event(ev)

    # Global sender blacklist: hide any chat event from a named
    # sender, regardless of channel or tab. Matches actor_name
    # case-insensitively. Applies only to chat-source events (combat
    # synth events have actor_name set to entity names we don't want
    # to accidentally blacklist via a same-named player).
    if _chat_blacklist and ev.get("source") == "chat":
        nm = (ev.get("actor_name") or "").strip().lower()
        if nm and nm in _chat_blacklist:
            return set()

    # Global channel-hide toggles: if the user switched this channel
    # off in the GUI footer, hide it everywhere.
    if channel in _chat_channel_hidden:
        return set()

    target_dim = _chat_target_dim(ev)
    tab_names = _chat_routing_lookup(actor, channel, target_dim)
    if _ow_chat_route_debug and channel in (
            "chat_assist", "chat_emote", "chat_other", "chat_npc"):
        # Show full path: what came in, what the classifier said,
        # what routing decided, whether the channel-hide table is
        # eating it, and whether any tab subscribed.
        hidden = channel in _chat_channel_hidden
        print(f"[OmniChat] route: src={ev.get('source')!r} "
              f"mode={ev.get('mode')} kind={ev.get('kind')!r} "
              f"actor={actor!r} → channel={channel!r} "
              f"hidden={hidden} tabs={tab_names!r} "
              f"text={(ev.get('text') or '')[:40]!r}")
    if not tab_names:
        return set()
    out = set()
    for name in tab_names:
        # Custom tabs route by stable internal id, not by short_name
        # (which is just a display label and can be renamed). The
        # _CUSTOM_TAB_IDS map gives us the tab index for these ids.
        if name in _CUSTOM_TAB_IDS:
            out.add(_CUSTOM_TAB_IDS[name])
            continue
        for i, (short, _full) in enumerate(chat_tab_names):
            if short == name:
                out.add(i)
                break
    return out


def _make_tab_predicate(tab_idx):
    """Return a predicate function for a specific tab index.

    Each per-tab predicate is built by routing the event and checking
    if THIS tab is in the destination set. The route is computed once
    per event-per-tab via _chat_route_event; for very busy events the
    grid lookup is O(1) so this is fine.
    """
    # Resolve the System tab index once at predicate-construction
    # time so the fallback doesn't repeat a name lookup per event.
    # Falls back to 0 if System somehow isn't in the tabs list.
    _SYSTEM_TAB_IDX = next(
        (i for i, (short, _full) in enumerate(chat_tab_names)
         if short == "System"),
        0)

    def _pred(ev):
        try:
            return tab_idx in _chat_route_event(ev)
        except Exception:
            # Defensive: a broken classifier shouldn't kill rendering.
            # Default to "show in System, hide everywhere else" so the
            # event still appears somewhere. Was previously Battle but
            # that turned Battle into a junk drawer for any event the
            # classifier choked on. System is the better catchall.
            return tab_idx == _SYSTEM_TAB_IDX
    return _pred


def _parse_routing_data(data, source_label):
    """Validate the loaded JSON dict matches the routing schema.

    Returns the cleaned data dict (drops _meta and any malformed
    entries with a warning). Doesn't return errors — bad shapes
    are skipped silently aside from a log line.
    """
    if not isinstance(data, dict):
        print(f"[OmniChat] chat routing config malformed (not a dict): "
              f"{source_label}")
        return None
    cleaned = {}
    for actor, ch_map in data.items():
        if actor.startswith("_"):
            continue   # _meta etc. — documentation only
        if not isinstance(ch_map, dict):
            print(f"[OmniChat] chat routing: skipping bad actor {actor!r}")
            continue
        cleaned_actor = {}
        for channel, tabs in ch_map.items():
            if isinstance(tabs, list):
                # Flat cell: actor[channel] = [tabs]
                cleaned_actor[channel] = list(tabs)
            elif isinstance(tabs, dict):
                # Nested target cell: actor[target_dim][channel] = [tabs].
                # Used for mob target filtering (to_me/to_party/to_other).
                # Here `channel` is actually a target_dim key and `tabs`
                # is a {channel: [tabs]} sub-dict.
                cleaned_nested = {}
                for sub_ch, sub_tabs in tabs.items():
                    if isinstance(sub_tabs, list):
                        cleaned_nested[sub_ch] = list(sub_tabs)
                    else:
                        print(f"[OmniChat] chat routing: skipping bad "
                              f"nested cell {actor!r}/{channel!r}/{sub_ch!r}")
                if cleaned_nested:
                    cleaned_actor[channel] = cleaned_nested
            else:
                print(f"[OmniChat] chat routing: skipping bad cell "
                      f"{actor!r}/{channel!r}")
                continue
        if cleaned_actor:
            cleaned[actor] = cleaned_actor
    return cleaned


def _routing_path_for_job(job):
    """Return the JSON path for a job's per-job routing config.
    Empty/None job returns the global path."""
    try:
        if job and job.strip():
            return os.path.join(SETTINGS_DIR,
                                f"omnichat_chat_routing-{job.strip().upper()}.json")
        return os.path.join(SETTINGS_DIR, "omnichat_chat_routing.json")
    except NameError:
        return None


def load_chat_routing(path=None):
    """Load the global routing config from omnichat_chat_routing.json.

    This is the layered foundation: events that aren't overridden by
    per-job config use this. Missing file = silent default (uses the
    baked-in _CHAT_ROUTING_DEFAULTS).

    Per-job overrides come from load_chat_routing_for_job().
    """
    global _chat_routing_global
    if path is None:
        path = _routing_path_for_job(None)
    if path is None:
        return
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cleaned = _parse_routing_data(data, path)
        if cleaned is None:
            return
        # Apply tab-name overrides from _meta.tab_names. Only the
        # custom_1 / custom_2 ids are user-renameable; built-in tabs
        # ignore overrides (their short_names are also their routing
        # ids and must not drift). Applied in-place to chat_tab_names
        # so the strip renders the user's labels on next frame.
        meta = data.get("_meta") if isinstance(data, dict) else None
        if isinstance(meta, dict):
            overrides = meta.get("tab_names")
            if isinstance(overrides, dict):
                for tab_id, label in overrides.items():
                    if tab_id in _CUSTOM_TAB_IDS and isinstance(label, str) \
                            and label.strip():
                        idx = _CUSTOM_TAB_IDS[tab_id]
                        if 0 <= idx < len(chat_tab_names):
                            clean = label.strip()
                            chat_tab_names[idx] = (clean, clean)
                            _chat_tab_label_overrides[tab_id] = clean
            # Global channel-hide toggles + sender blacklist (GUI footer).
            global _chat_channel_hidden, _chat_blacklist
            hidden = meta.get("channel_hidden")
            if isinstance(hidden, list):
                _chat_channel_hidden = {
                    c for c in hidden if isinstance(c, str)}
            else:
                _chat_channel_hidden = set()
            global _chat_focus_phrases_global
            fp = meta.get("focus_phrases")
            if isinstance(fp, list):
                _chat_focus_phrases_global = [
                    s.strip().lower() for s in fp
                    if isinstance(s, str) and s.strip()]
            else:
                _chat_focus_phrases_global = []
            bl = meta.get("blacklist")
            if isinstance(bl, list):
                _chat_blacklist = {
                    s.strip().lower() for s in bl
                    if isinstance(s, str) and s.strip()}
            else:
                _chat_blacklist = set()
        # Merge cleaned into the defaults: cleaned ENTRIES override
        # defaults, but keys not in cleaned keep the default value.
        # This way users can have a global JSON with just a few
        # overrides (e.g. "always hide gearswap from World") without
        # having to specify every default cell.
        merged = {}
        for actor in set(list(_CHAT_ROUTING_DEFAULTS.keys()) +
                          list(cleaned.keys())):
            base   = _CHAT_ROUTING_DEFAULTS.get(actor, {})
            extras = cleaned.get(actor, {})
            merged[actor] = {**base, **extras}
        _chat_routing_global = merged
        print(f"[OmniChat] Loaded global chat routing config from {path}")
    except Exception as e:
        print(f"[OmniChat] Could not read chat routing config: {e}")


def load_chat_routing_for_job(job):
    """Load per-job routing config from omnichat_chat_routing-<JOB>.json.

    If the file doesn't exist, the per-job overrides are cleared and
    the lookup falls through to the global config. Called on job
    change (detected by polling _inv_for_sim["main_job"] in the
    main loop).

    Future drop will add: on first load for a job with no JSON,
    convert BattleMod's filters-<JOB>.xml and save as the seed.
    For now: no file = no overrides = use global.
    """
    global _chat_routing_perjob, _chat_routing_current_job

    job = (job or "").strip().upper()
    if job == _chat_routing_current_job and _chat_routing_perjob:
        return  # already loaded
    _chat_routing_current_job = job

    if not job:
        # No job known yet — clear per-job overrides so global config
        # is fully in effect.
        if _chat_routing_perjob:
            _chat_routing_perjob = {}
            print("[OmniChat] Chat routing per-job overrides cleared "
                  "(no job known)")
        _set_perjob_focus_phrases([])
        return

    path = _routing_path_for_job(job)
    if path is None:
        return

    if not os.path.exists(path):
        # No per-job file — clear overrides; global fully in effect.
        # Drop 3 will add BattleMod XML import on first load for this job.
        if _chat_routing_perjob:
            _chat_routing_perjob = {}
        _set_perjob_focus_phrases([])
        print(f"[OmniChat] No per-job chat routing for {job} "
              f"(global config in effect)")
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _pj_meta = data.get("_meta") if isinstance(data, dict) else None
        _pj_fp = (_pj_meta or {}).get("focus_phrases")
        _set_perjob_focus_phrases(_pj_fp if isinstance(_pj_fp, list) else [])
        cleaned = _parse_routing_data(data, path)
        if cleaned is None:
            _chat_routing_perjob = {}
            return
        _chat_routing_perjob = cleaned
        print(f"[OmniChat] Loaded per-job chat routing for {job} "
              f"from {path}")
    except Exception as e:
        print(f"[OmniChat] Could not read per-job chat routing for {job}: {e}")
        _chat_routing_perjob = {}


def _launch_routing_gui():
    """Launch the standalone routing config GUI.

    Looks for omnichat_routing_gui.exe alongside OmniChat.exe first
    (the deployed case), falls back to running the .py script directly
    (the development case). Detached subprocess — doesn't block the
    overlay.
    """
    import subprocess
    # Resolve "alongside this binary" — handle both PyInstaller-frozen
    # OmniChat.exe (sys.executable points to it) and source-running
    # case (sys.executable is python.exe, so use __file__'s dir).
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    exe_path = os.path.join(base_dir, "omnichat_routing_gui.exe")
    py_path  = os.path.join(base_dir, "omnichat_routing_gui.py")

    try:
        if os.path.exists(exe_path):
            # Detached launch — DETACHED_PROCESS so closing OmniWatch
            # won't kill the GUI. Windows-only flag; on other OSes
            # subprocess.Popen with start_new_session=True is equivalent.
            flags = 0x00000008 if sys.platform == "win32" else 0
            subprocess.Popen([exe_path], creationflags=flags,
                             cwd=base_dir, close_fds=True)
            print(f"[OmniChat] Launched {exe_path}")
        elif os.path.exists(py_path):
            # Dev fallback: run the .py directly.
            flags = 0x00000008 if sys.platform == "win32" else 0
            subprocess.Popen([sys.executable, py_path],
                             creationflags=flags, cwd=base_dir,
                             close_fds=True)
            print(f"[OmniChat] Launched {py_path} via Python")
        else:
            print(f"[OmniChat] Routing GUI not found. Tried:\n"
                  f"  {exe_path}\n  {py_path}")
    except Exception as e:
        print(f"[OmniChat] Failed to launch routing GUI: {e}")


# Build the per-tab predicates from the factory. One predicate per
# tab index. Tabs that don't appear in the routing config get an
# empty predicate (always False) so the tab still renders, just
# empty. Order matches chat_tab_names exactly.
chat_tab_filters = [_make_tab_predicate(i) for i in range(len(chat_tab_names))]
assert len(chat_tab_filters) == len(chat_tab_names), \
    "chat_tab_filters and chat_tab_names must have the same length"


# =========================================================================
# SECTION: C_sender
# =========================================================================

# Per-mode SENDER + MESSAGE colors for chat lines (say/tell/shout/yell).
# When a mode is in this map, draw_chat_panel splits the first wrapped
# line at the sender|message boundary and renders the two halves with
# distinct colors. Modes NOT in this map render as a single color (the
# CHAT_MODE_PALETTE entry).
#
# Design: sender name is always orange (signals "who"); message text
# uses the channel color (signals "what channel"). This makes a stream
# of mixed-channel chat readable at a glance — see orange "Wormfood:"
# and your eye lands on the speaker, then the message tells you the
# channel by color.
CHAT_SENDER_COLOR = (255, 170,  80)            # orange — speaker name
# Zone-tag color for /yell senders: FFXI yells arrive shaped
# "Sender[ZoneName]: msg" because the server appends the originator's
# current zone in brackets right after the name. Rather than render that
# whole region in CHAT_SENDER_COLOR (which makes the zone tag visually
# dominate the speaker name), we paint just the "[Zone]" portion in a
# lighter, paler yellow so the eye lands on the speaker first and reads
# the zone as a secondary attribution. Only applied when both a sender
# region AND a bracketed zone tag are detected in the first line of a
# mode-11 (/yell) event.
CHAT_YELL_ZONE_COLOR = (255, 235, 175)         # light pale yellow — yell zone tag
# Hover color for clickable sender names. Painted on the sender name
# portion when the mouse is over it, signaling that left-click composes
# a /tell to that name and right-click opens a context menu. Brighter +
# slightly cyan-shifted from the normal sender orange so the hover is
# obvious without being garish. Applied to ALL clickable names regardless
# of channel (say/tell/shout/yell/ls/party) — keeps the hover behavior
# consistent across the panel.
CHAT_NAME_HOVER_COLOR = (255, 245, 200)        # bright cream — sender hover
# GearSwap output body color — gold, matching the Gearswap tab theme.
# Applied to macro-set echoes and "X is now Y" state lines, which
# arrive on mode 1 and would otherwise render /say-white.
CHAT_GEARSWAP_BODY_COLOR = (255, 215, 80)
CHAT_MSG_COLOR_BY_MODE = {
    1:  (240, 240, 240),                       # /say — white
    3:  (240, 240, 240),                       # /shout — white (was light yellow); sender stays colored
    4:  (240, 240, 240),                       # /tell received — white (was light purple)
    11: (240, 240, 240),                       # /yell — white (was pink); sender stays colored
    12: (240, 240, 240),                       # /tell sent — white (was purple)
    # mode 13 (/emote) intentionally absent: emotes don't follow the
    # "Sender: message" wire format, so the splitter would fail anyway.
    # They render single-color via CHAT_MODE_PALETTE[13] (blueish).
}


def _chat_split_sender(text, mode):
    """Split a chat line into (sender_text, message_text).

    Used for say/tell/shout/yell where FFXI's wire format puts the
    sender's name before a `:` (or `>>` for outgoing tells) and the
    message after. Returns (None, text) for modes that don't follow
    this pattern (battle, system, etc.) — caller should render
    single-color in that case.

    Sender includes everything up to and INCLUDING the boundary
    delimiter and the single space that typically follows.

    Boundary by mode:
      mode 1, 3, 4, 11    — split on first ':'
      mode 12             — split on first '>>'  (outgoing tell wire
                            format is "RecipientName>> message", no
                            colon present)

    For yells, the zone tag in brackets stays grouped with the sender
    ("Mytoy [BastokMark]: "). Received tells include the `>>` prefix
    in the sender region since `>>` identifies the line as a tell.

    Examples (text after FFXI marker strip):
      /say        "Wormfood : hello there"     -> ("Wormfood : ",      "hello there")
      /tell rcv   ">>Wormfood : hello"         -> (">>Wormfood : ",    "hello")
      /tell sent  "Wormfood>> hello"           -> ("Wormfood>> ",      "hello")
      /shout      "Wormfood : LFG"             -> ("Wormfood : ",      "LFG")
      /yell       "Mytoy[BastokMark]: REMAP"   -> ("Mytoy[BastokMark]: ", "REMAP")

    Edge cases:
      * Boundary missing — degrade gracefully, return (None, text).
      * Message text contains additional colons / >> — only split on
        the FIRST occurrence (the sender|message boundary).
      * Message starts immediately after boundary (no space) — split
        boundary includes just the delimiter.
    """
    if mode not in CHAT_MSG_COLOR_BY_MODE:
        return (None, text)
    if not text:
        return (None, text)

    # Outgoing tells use ">>" as the boundary, not ":". The wire form
    # from FFXI after marker strip is "RecipientName>> message".
    if mode == 12:
        idx = text.find(">>")
        if idx < 0:
            return (None, text)
        boundary = idx + 2     # past ">>"
        if boundary < len(text) and text[boundary] == " ":
            boundary += 1
        return (text[:boundary], text[boundary:])

    # All other chat modes use ":" as the boundary.
    colon_idx = text.find(":")
    if colon_idx < 0:
        return (None, text)
    boundary = colon_idx + 1
    if boundary < len(text) and text[boundary] == " ":
        boundary += 1
    return (text[:boundary], text[boundary:])


def _chat_split_sender_cached(ev):
    """Memoized sender split per event."""
    key = ev.get("_seq") or id(ev)
    cached = _chat_split_cache.get(key)
    if cached is not None:
        return cached
    result = _chat_split_sender(ev.get("text", ""), ev.get("mode", 0))
    _chat_split_cache[key] = result
    # Bounded cleanup — same rationale as _chat_wrap_cache.
    if len(_chat_split_cache) > 4000:
        try:
            evict = sorted(_chat_split_cache.keys())[:1000]
            for k in evict:
                _chat_split_cache.pop(k, None)
        except TypeError:
            _chat_split_cache.clear()
    return result


# Cache: event_id → (sender_text, message_text). Same lifecycle as
# wrap cache — bounded by chat_events.maxlen.
_chat_split_cache = {}


# =========================================================================
# SECTION: D_parse
# =========================================================================

def _parse_chat_batch(raw):
    """Parse a CHAT_BATCH UDP datagram into a list of event dicts.

    Wire format:
      CHAT_BATCH\t<count>\t<batch_index>\t<batch_total>
      chat\t<ts>\t<source>\t<mode>\t<actor_id>\t<actor_name>\t<actor_class>\t<target_id>\t<target_name>\t<target_class>\t<segments_b64>\t<text_b64>
      chat\t...

    Returns:
      (events, header) where events is a list of dicts, header is
      a dict {"count": int, "batch_index": int, "batch_total": int}
      or None if the header is malformed.

    Malformed event lines are skipped individually — one bad line
    doesn't drop the whole batch. The caller is expected to handle
    None for the header by dropping the whole datagram.

    Reassembly across multiple datagrams (when batch_total > 1) is
    NOT handled here — each datagram is independent and events are
    appended in arrival order. The Lua side guarantees chronological
    ordering both within and across datagrams of a single batch, so
    naive append gives correct ordering as long as the loopback
    socket preserves send order (it does on Windows + Linux).
    """
    lines = raw.split("\n")
    if not lines:
        return [], None

    # Parse header.
    hdr_parts = lines[0].split("\t")
    if len(hdr_parts) < 4 or hdr_parts[0] != "CHAT_BATCH":
        return [], None
    try:
        header = {
            "count":       int(hdr_parts[1]),
            "batch_index": int(hdr_parts[2]),
            "batch_total": int(hdr_parts[3]),
        }
    except ValueError:
        return [], None

    events = []
    for ln in lines[1:]:
        if not ln:
            continue
        fields = ln.split("\t")
        # Expected layout: chat \t ts \t source \t mode \t actor_id \t
        # actor_name \t actor_class \t target_id \t target_name \t
        # target_class \t segments_b64 \t text_b64 \t [kind]
        # — 12 fields minimum; 13th (kind) optional for backward compat
        # with older Lua side that doesn't emit the kind tag.
        if len(fields) < 12 or fields[0] != "chat":
            continue
        try:
            ts        = float(fields[1])
            mode      = int(fields[3])
            actor_id  = int(fields[4])
            target_id = int(fields[7])
        except ValueError:
            continue

        # Decode text (b64) defensively. Empty payload is legitimate
        # for events with no text body (rare but possible). Bad b64
        # produces empty text rather than killing the event.
        #
        # Encoding: try UTF-8 strict first (succeeds for ASCII and any
        # UTF-8 the Lua side encoded). If it fails, the bytes are
        # almost certainly Shift-JIS (FFXI's native chat encoding) —
        # try that. If both fail, fall back to UTF-8 with replacement
        # so we at least show something.
        text_b64 = fields[11]
        try:
            raw_bytes = base64.b64decode(text_b64) if text_b64 else b""
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    # cp932 is a superset of Shift-JIS that handles
                    # FFXI's custom additions (elemental icons, etc.)
                    # better than strict shift_jis.
                    text = raw_bytes.decode("cp932")
                except UnicodeDecodeError:
                    text = raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = ""

        # Segments encode word-level color info for rich rendering. The
        # Lua side emits a JSON-encoded list of [text, color_class] pairs
        # wrapped in base64. Empty string = no segments, render as flat
        # text (backward compat with older events). Bad payload = empty.
        # On success, segments is a list of (text, color_class) tuples.
        #
        # Same UTF-8/Shift-JIS encoding fallback as the text field
        # above — FFXI's chat is SJIS, so player names and message
        # content can include non-UTF-8 bytes that need cp932 decoding.
        seg_b64 = fields[10]
        segments = []
        if seg_b64:
            try:
                raw_seg_bytes = base64.b64decode(seg_b64)
                try:
                    raw_json = raw_seg_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        raw_json = raw_seg_bytes.decode("cp932")
                    except UnicodeDecodeError:
                        raw_json = raw_seg_bytes.decode("utf-8",
                                                        errors="replace")
                parsed = json.loads(raw_json)
                if isinstance(parsed, list):
                    # Each element should be a 2-element list [text, color].
                    # Defensive: skip malformed entries rather than aborting
                    # the whole event.
                    for item in parsed:
                        if (isinstance(item, list) and len(item) >= 2
                            and isinstance(item[0], str)
                            and isinstance(item[1], str)):
                            segments.append((item[0], item[1]))
            except Exception:
                segments = []

        events.append({
            "ts":           ts,
            "source":       fields[2],
            "mode":         mode,
            "actor_id":     actor_id,
            "actor_name":   fields[5],
            "actor_class":  fields[6],
            "target_id":    target_id,
            "target_name":  fields[8],
            "target_class": fields[9],
            "text":         text,
            "segments":     segments,
            # 13th field — combat channel for routing. Lua emits this
            # on source='battle' events to tell us which channel
            # (melee/ranged/damage/healing/etc.) the event is. Optional;
            # missing or empty = let _chat_classify_event fall back.
            "kind":         fields[12] if len(fields) > 12 else "",
        })

    return events, header


def _ingest_chat_packet(raw, stream_label):
    """Parse one CHAT_BATCH datagram and append events to chat_events.

    stream_label is "text" or "battle" — used only for trace output
    and the diagnostic counters. The events themselves identify their
    own source via the event's `source` field.
    """
    global chat_recv_text, chat_recv_battle, chat_recv_errors
    events, header = _parse_chat_batch(raw)
    if header is None:
        chat_recv_errors += 1
        if chat_recv_errors <= 5:
            # Cap noisy logs: print first 5 errors only. After that,
            # the counter tells you it's happening without spamming.
            print(f"[OmniChat] chat: bad packet on {stream_label} "
                  f"(first 80 bytes): {raw[:80]!r}")
        return
    for ev in events:
        _chat_assign_seq(ev)
        # Skillchain line cleanup: FFXI separates the parts of a mob name
        # with an internal token that windower.from_shift_jis converts to
        # U+30FB (・, the Katakana middle dot) — so "Apex Crab" arrives as
        # "Apex・Crab" and displays with a stray dot ("Fragmentation: 6345
        # → Apex・Crab"). Replace that middle dot with a plain space, but
        # ONLY on skillchain result lines so we don't disturb any other
        # text (kept deliberately narrow after an earlier broad byte-edit
        # regressed normal lines). Isolated to this one event's text.
        _txt = ev.get("text") or ""
        # Drop Unity NPC parameter-broadcast dumps — comma-separated hex
        # groups ("0a,01f1,0000000a,...") that the Unity NPC emits and
        # that leak onto the Unity chat path as raw text. Not chat; skip
        # the event entirely so it never reaches any tab. Gated on a chat
        # source so it can't eat a (hypothetical) battle-synth line.
        if (ev.get("source") == "chat" and _txt
                and _PARAM_DUMP_PATTERN_R.search(_txt)):
            continue
        if "\u30fb" in _txt and _SKILLCHAIN_PATTERN_R.match(_txt):
            ev["text"] = _txt.replace("\u30fb", " ")
        # Colorize cast-start / readies text. Incoming_text events
        # arrive as flat strings with empty segments; if the text
        # matches the cast/readies shape, rebuild as colored segments
        # so it visually matches the packet-synth combat lines (actor
        # in their class color, spell in spell color, target in their
        # class color). Battle-source synth events already carry
        # colored segments and are skipped.
        if (ev.get("source") == "chat"
                and not ev.get("segments")):
            txt = ev.get("text") or ""
            # Unity chat (mode 212, NPC dialogue + PC member chat) arrives
            # as a flat line with no segments, so the speaker name has no
            # color. Split it into a gold-amber sender segment (matching
            # the Unity tab title) and a white body. Two sender forms:
            #   "{Yoran-Oran} <body>"  — NPC dialogue (brace-wrapped)
            #   "Name : <body>"         — PC member chat (body may be a
            #                             single "." / "@" check-in, kept)
            # Unmatched forms stay flat (a plain line beats a mis-split).
            #
            # Keyed on the MODE byte (212), NOT ev["kind"]: kind comes
            # straight from the wire packet field, which is EMPTY for
            # incoming-text lines (the chat_unity channel is decided later
            # by _chat_classify_event and never written back to ev["kind"]
            # before this point). Checking mode==212 is the authoritative,
            # always-populated Unity signal — keying on kind here left the
            # names un-colored because the test was never true.
            if ev.get("mode") in (212, 211) and txt:
                u_segs = None
                if txt.startswith("{"):
                    # The sender name is the LEADING brace group, and a real
                    # member/NPC line has the shape "{Name} <body>" — a
                    # space immediately follows the name's closing brace.
                    # Autotranslate phrases ALSO use literal { }, so we must
                    # NOT assume the first "}" closes a name: a message that
                    # is purely/mostly an AT phrase (e.g. "{Good evening!}"
                    # with no space-separated body) would otherwise be
                    # mis-split, treating the AT text as the "name" and
                    # dropping the message body — the reported "only the
                    # sender name shows" bug for autotranslate users.
                    #
                    # Rule: only split when the closing brace is followed by
                    # a SPACE and a non-empty body. The body keeps its own
                    # braces (an AT phrase inside the body renders as-is).
                    # If the closing brace is at end-of-string, or isn't
                    # followed by " <body>", leave the line flat so the
                    # whole message (AT phrase included) still displays.
                    _close = txt.find("}")
                    if (_close > 1
                            and _close + 1 < len(txt)
                            and txt[_close + 1] == " "
                            and txt[_close + 2:].strip() != ""):
                        _name = txt[1:_close]
                        _body = txt[_close + 2:]   # keep body verbatim (may hold {AT})
                        u_segs = [(_name, "ch_unity"),
                                  (" ", "default"),
                                  (_body, "body_say")]   # white body
                    # else: AT-only or no real body → leave flat (renders whole)
                else:
                    _ci = txt.find(" : ")
                    if _ci > 0:
                        u_segs = [(txt[:_ci], "ch_unity"),
                                  (" : ", "default"),
                                  (txt[_ci + 3:], "body_say")]
                if u_segs:
                    ev["segments"] = u_segs
            # Assist Channel sender split (text path, mode 222). The
            # cross-zone Assist chat arrives as "Name(E) : body" (the "(E)"
            # is the language-preference tag FFXI appends). On the PACKET
            # path (mode 35) chat_packets.lua already builds a teal
            # ch_assist sender segment, but the incoming-TEXT path (mode
            # 222) emits a flat line with no segments, so its sender fell
            # back to the default gold CHAT_SENDER_COLOR instead of the
            # teal Assist-tab color. Build the segment here, mirroring the
            # Unity split above: sender region (name + "(E)") in ch_assist,
            # " : " neutral, body default. Keyed on mode 222 (the
            # authoritative, always-populated text-path Assist signal).
            if ev.get("mode") == 222 and txt and not ev.get("segments"):
                _ci = txt.find(" : ")
                if _ci > 0:
                    ev["segments"] = [
                        (txt[:_ci],     "ch_assist"),
                        (" : ",         "default"),
                        (txt[_ci + 3:], "default"),
                    ]
            # Colorize cast-start / readies text. Incoming_text events
            # arrive as flat strings with empty segments; if the text
            # matches the cast/readies shape, rebuild as colored segments
            # so it visually matches the packet-synth combat lines (actor
            # in their class color, spell in spell color, target in their
            # class color). Battle-source synth events already carry
            # colored segments and are skipped.
            if not ev.get("segments") and _CAST_READY_PATTERN_R.search(txt):
                segs = _build_cast_segments(txt)
                if segs:
                    ev["segments"] = segs
        chat_events.append(ev)
        if stream_label == "text":
            chat_recv_text += 1
        else:
            chat_recv_battle += 1

        # Focus words/phrases: stamp the event (pulsing highlight in
        # the renderer) and mark every non-active tab it routes to so
        # the tab label pulses until visited.
        if _chat_focus_check(ev):
            for tab_idx, predicate in enumerate(chat_tab_filters):
                if tab_idx == chat_active_tab:
                    continue
                try:
                    if predicate(ev):
                        _chat_tab_focus_pulse[tab_idx] = ev["focus_ts"]
                except Exception:
                    pass

        # Increment unread count on every tab the event matches, EXCEPT
        # the currently-active tab (you've effectively "read" it as it
        # appears in front of you). Tab 0 (All) is always either active
        # or accumulating unread like any other tab. Cap unread at 999
        # to avoid the badge ballooning into multi-digit clutter.
        for tab_idx, predicate in enumerate(chat_tab_filters):
            if tab_idx == chat_active_tab:
                continue
            try:
                if predicate(ev):
                    cur = chat_tab_unread.get(tab_idx, 0)
                    if cur < 999:
                        chat_tab_unread[tab_idx] = cur + 1
            except Exception:
                # Bad filter shouldn't kill ingest; just skip this tab.
                pass

        if _chat_trace:
            # Console trace for pass-1 verification. Compact format:
            # [HH:MM:SS] source.mode actor[class] -> target[class]: text
            ts_str = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
            actor  = ev["actor_name"] or "?"
            target = (" -> " + ev["target_name"]) if ev["target_name"] else ""
            print(f"[chat] {ts_str} {ev['source']}.{ev['mode']:>3} "
                  f"{actor}[{ev['actor_class']}]{target}: "
                  f"{ev['text'][:120]}")

        # ── Always-on routing trace, first N events only ─────────────────
        # Logs the classifier output AND the resolved tab destinations
        # for the first N chat events. Capped so the session log
        # doesn't bloat under sustained chat. Purpose: when a user
        # reports "filters not working", their session log already
        # has the routing trace so Cooper can see WHY events landed
        # where they did without needing a reproducer or env var.
        #
        # Logging once-per-event for a short window covers the most
        # important diagnostic — "what does THIS event classify as,
        # and which tabs does it route to." The cap keeps logs small
        # for typical sessions (50 events is reached in seconds during
        # busy chat).
        global _chat_route_trace_count
        if _chat_route_trace_count < _CHAT_ROUTE_TRACE_CAP:
            _chat_route_trace_count += 1
            try:
                actor_cls, channel = _chat_classify_event(ev)
                tab_set = _chat_route_event(ev)
                tab_names = [chat_tab_names[i][0] for i in sorted(tab_set)
                             if 0 <= i < len(chat_tab_names)]
                print(f"[chat-route] #{_chat_route_trace_count} "
                      f"mode={ev.get('mode', 0)} "
                      f"source={ev.get('source', '?')} "
                      f"actor={ev.get('actor_name', '?')!r}[{actor_cls}] "
                      f"channel={channel} -> tabs={tab_names} "
                      f"text={(ev.get('text', '') or '')[:60]!r}")
                if _chat_route_trace_count == _CHAT_ROUTE_TRACE_CAP:
                    print(f"[chat-route] (further events not traced; "
                          f"trace cap = {_CHAT_ROUTE_TRACE_CAP})")
            except Exception as _e:
                print(f"[chat-route] classifier error on event: {_e}")


def dump_chat_routing_state():
    """Print the current chat routing configuration to the session log.

    Called from the right-click handler on the chat panel header. Gives
    a full snapshot — current active tab, per-tab unread counts, the
    resolved routing table (per-job overrides + global merged), and a
    classification trace of the most recent N events in chat_events.

    Designed for "user reports a bug, sends their session log" workflow:
    one click and the diagnostic state is captured.
    """
    print("=" * 60)
    print("[OmniChat] Chat routing diagnostic dump")
    if 0 <= chat_active_tab < len(chat_tab_names):
        _active_name = chat_tab_names[chat_active_tab][0]
    else:
        _active_name = "?"
    print(f"  Active tab: {chat_active_tab} ({_active_name})")
    print(f"  Tabs: {[t[0] for t in chat_tab_names]}")
    print(f"  Unread: {dict(chat_tab_unread)}")
    print(f"  Current job for routing: {_chat_routing_current_job}")
    print(f"  Per-job overrides loaded: {bool(_chat_routing_perjob)}")
    if _chat_routing_perjob:
        print(f"  Per-job actors: {list(_chat_routing_perjob.keys())}")
    print(f"  Global routing actors: "
          f"{list(_chat_routing_global.keys())}")

    # Classify and route the last 10 events to show the actual mapping.
    recent = list(chat_events)[-10:]
    print(f"  Last {len(recent)} event classifications:")
    for ev in recent:
        try:
            actor_cls, channel = _chat_classify_event(ev)
            tab_set = _chat_route_event(ev)
            tab_names = [chat_tab_names[i][0] for i in sorted(tab_set)
                         if 0 <= i < len(chat_tab_names)]
            text_preview = (ev.get('text', '') or '')[:60]
            print(f"    mode={ev.get('mode', 0):>3} "
                  f"source={ev.get('source', '?'):8s} "
                  f"{actor_cls}/{channel} -> {tab_names}: "
                  f"{text_preview!r}")
        except Exception as _e:
            print(f"    [classify error: {_e}]")
    print("=" * 60)


# =========================================================================
# SECTION: E_wrap
# =========================================================================

def _chat_color_for_mode(mode_or_ev):
    """Return RGB color tuple for a chat event.

    Accepts either a bare mode byte (legacy callers) OR a full event
    dict (preferred — lets us route on the `source` field for
    synthetic events that don't have a real chat mode). Synthetic
    events (buff/debuff/checkparam) have mode=-1/-2 which won't match
    any real FFXI mode in the palette; we route them by source instead
    to give them FFXI's classic status-effect colors.

    Falls back to CHAT_COLOR_DEFAULT for unknown modes — the panel
    still renders in plain gray, so adding mappings later doesn't
    risk breaking existing modes.
    """
    # Backward-compat: bare int → just look up the palette.
    if isinstance(mode_or_ev, int):
        return CHAT_MODE_PALETTE.get(mode_or_ev, CHAT_COLOR_DEFAULT)

    ev = mode_or_ev
    src = ev.get("source")
    # Skillchain result lines ("Fragmentation: 5665 → Apex Crab") get a
    # single distinct skillchain color (a bright cyan-white) so they stand
    # out in the feed. Detected by a property name followed by ':' at the
    # START of the line, so only the actual SC result line is colored.
    txt = ev.get("text") or ""
    if txt and _SKILLCHAIN_PATTERN_R.match(txt):
        return COL_SKILLCHAIN
    # Synthetic buff/debuff events use FFXI's classic status colors:
    # buffs render in light cyan, debuffs in dark pink/magenta. These
    # match how status messages appear in FFXI's own chat log.
    if src == "buff":
        return CHAT_COLOR_BUFF
    if src == "debuff":
        return CHAT_COLOR_DEBUFF
    return CHAT_MODE_PALETTE.get(ev.get("mode"), CHAT_COLOR_DEFAULT)


def _chat_wrap_text(text, body_font, cjk_font, max_width):
    """Greedy word-wrap `text` to fit within max_width pixels.

    Measures with both body_font and cjk_font where each script run
    falls, so CJK characters (which Consolas can't measure correctly)
    use Yu Gothic UI widths and wrap accurately.

    Returns a list of strings, one per visible line. Wraps at spaces;
    a single word longer than max_width is rendered uncut (overflows
    the panel) — better than character-level wrap which makes a mess
    of mob names and URLs. In practice chat lines are short enough
    that this is rare.

    Empty input returns [""] so callers can iterate uniformly.
    """
    if not text:
        return [""]
    words = text.split(" ")
    lines = []
    current = ""
    for w in words:
        if not current:
            trial = w
        else:
            trial = current + " " + w
        if _chat_measure_mixed(trial, body_font, cjk_font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines if lines else [""]


def _chat_wrap_segments(segments, body_font, cjk_font, max_width):
    """Wrap a list of (text, color_class) segments to max_width.

    Returns a list of wrapped LINES, where each line is itself a list
    of (text, color_class) spans. This lets the renderer paint each
    span in its own color while honoring word-wrap boundaries.

    Algorithm: greedy by-word wrap (same logic as _chat_wrap_text)
    but the working unit is "word + color of each char in that word".
    Words crossing color boundaries get split at the boundary so each
    sub-word carries a single color. Spaces between words inherit the
    color of the preceding char (cosmetic; matters only when a
    differently-colored span ends with a space).

    Empty segments → one empty line of one empty default span.
    """
    if not segments:
        return [[("", "default")]]

    # Build flat (char, color) list. Lets us treat color boundaries
    # uniformly without per-segment string slicing.
    flat = []
    for seg_text, seg_color in segments:
        for ch in seg_text:
            flat.append((ch, seg_color))
    if not flat:
        return [[("", "default")]]

    # Split into "words" (runs separated by spaces). A word here is a
    # list of (char, color) tuples. We wrap by accumulating words into
    # a current line; when adding the next word would overflow, flush.
    words = []
    cur_word = []
    for ch, color in flat:
        if ch == " ":
            if cur_word:
                words.append(cur_word)
                cur_word = []
            words.append([(" ", color)])
        else:
            cur_word.append((ch, color))
    if cur_word:
        words.append(cur_word)

    # Helper: collapse a (char, color) list to "<text>" for measuring.
    def _word_text(w):
        return "".join(ch for ch, _ in w)

    # Helper: collapse a list of words to one string for measuring.
    def _line_text(ws):
        return "".join(_word_text(w) for w in ws)

    # Greedy pack words into lines.
    lines_of_words = []
    current = []  # list of words
    for w in words:
        trial = current + [w]
        if _chat_measure_mixed(_line_text(trial), body_font, cjk_font) <= max_width:
            current = trial
        else:
            if current:
                # Trim a trailing pure-space word from the wrapping
                # line, since the visible text shouldn't include it.
                while current and _word_text(current[-1]).strip() == "":
                    current.pop()
                lines_of_words.append(current)
            # Skip pure-space words at line start to avoid leading
            # whitespace on a wrapped continuation.
            if _word_text(w).strip() == "":
                current = []
            else:
                current = [w]
    if current:
        # Same trailing-space trim as above for the final line.
        while current and _word_text(current[-1]).strip() == "":
            current.pop()
        if current:
            lines_of_words.append(current)

    if not lines_of_words:
        return [[("", "default")]]

    # Convert each line (list of words = list of (char,color) lists)
    # back to a list of (text, color) spans, merging adjacent chars
    # that share a color.
    output_lines = []
    for line_words in lines_of_words:
        spans = []
        cur_text = []
        cur_color = None
        for w in line_words:
            for ch, color in w:
                if cur_color is None:
                    cur_color = color
                    cur_text.append(ch)
                elif color == cur_color:
                    cur_text.append(ch)
                else:
                    spans.append(("".join(cur_text), cur_color))
                    cur_text = [ch]
                    cur_color = color
        if cur_text:
            spans.append(("".join(cur_text), cur_color or "default"))
        output_lines.append(spans if spans else [("", "default")])

    return output_lines


def _chat_wrap_cached(ev, body_font, cjk_font, max_width):
    """Cached wrap by (event identity, panel width).

    Returns one of two shapes depending on whether the event has
    segments:
      - No segments: list[str]   (each str a wrapped line)
      - With segments: list[list[(text, color_class)]]
                        (each line is its own list of colored spans)

    The renderer detects which by checking type of first element.

    Invalidates the whole cache when panel width changes (the cache
    keys would become useless anyway). Per-event entries linger but
    are bounded by chat_events deque's maxlen since events not in
    that deque can never be looked up again.
    """
    global _chat_wrap_cache, _chat_wrap_cache_w
    if max_width != _chat_wrap_cache_w:
        _chat_wrap_cache.clear()
        _chat_render_cache.clear()
        _chat_wrap_cache_w = max_width
    # Stable per-event key. Falls back to id() only for events that
    # somehow weren't stamped (defensive — every event going through
    # chat_events.append should be stamped).
    key = ev.get("_seq") or id(ev)
    cached = _chat_wrap_cache.get(key)
    if cached is not None:
        return cached

    segs = ev.get("segments") or []
    if segs:
        # Segment-aware wrap. Returns list[list[(text, color)]].
        wrapped = _chat_wrap_segments(segs, body_font, cjk_font, max_width)
    else:
        # Plain-text wrap. Returns list[str].
        wrapped = _chat_wrap_text(ev.get("text", ""), body_font, cjk_font, max_width)
    _chat_wrap_cache[key] = wrapped
    # Bounded cleanup. Without this, the cache grows without limit
    # since we no longer rely on id() collisions for eviction. Cap
    # is 2x chat_events.maxlen so there's headroom for events that
    # left the deque but are still being rendered this frame.
    if len(_chat_wrap_cache) > 4000:
        # Drop the 1000 smallest keys (oldest by _seq). This is O(n)
        # but only runs when over cap; amortized cost is negligible.
        try:
            evict = sorted(_chat_wrap_cache.keys())[:1000]
            for k in evict:
                _chat_wrap_cache.pop(k, None)
        except TypeError:
            # Mixed key types from id() fallback — just clear everything.
            _chat_wrap_cache.clear()
    return wrapped


# =========================================================================
# SECTION: F_fonts
# =========================================================================

# ── Chat panel ────────────────────────────────────────────────────────────
# Scrolling chat log floating panel. Auto-scrolls to bottom; mouse wheel
# scrolls up and pauses auto-scroll; "↓ N new" indicator appears at
# bottom-right when new events arrive while scrolled up, click to jump.
# Resize via corner grip changes pixel size directly (not text scale).
#
# Rendering strategy:
#   1. Compute the visible-line window from chat_scroll_offset.
#   2. Walk chat_events from newest backward, wrap each event, count
#      lines until we've covered the visible window + scroll offset.
#   3. Render those wrapped lines into the visible area, bottom-up.
#   4. Overlay the "jump to bottom" badge if applicable.
#
# Performance: at 60fps with ~12 visible events, ~720 line-renders/sec
# worst case. Wrap is cached per (event, panel_width); font.size() is
# fast. No issues observed in profiling but watch perf if cache misses
# spike (e.g. during rapid resize).

def chat_panel_size(scale=1.0):
    """Return current chat panel (w, h) in pixels.

    Width and height come from chat_panel_dims which the user
    controls via the corner-grip drag handler. Scale is currently
    a no-op for the panel envelope — text size is fixed by font
    selection. Reserved for future per-panel font scaling.
    """
    w = max(CHAT_PANEL_MIN_W, int(chat_panel_dims[0]))
    h = max(CHAT_PANEL_MIN_H, int(chat_panel_dims[1]))
    return w, h


def _chat_format_timestamp(ts):
    """HH:MM format from a unix-seconds float. Used dim/prefix only."""
    return time.strftime("%H:%M", time.localtime(ts))


# Cached fonts. Created lazily on first draw to avoid pygame init order
# issues. Keys: ("body"|"meta"|"badge", size).
_chat_fonts = {}

def _chat_get_font(kind, size):
    # Apply the global UI scale so chat text tracks the same multiplier
    # as every other panel (4K/high-DPI legibility). Fixed call-site
    # sizes get scaled here; the cache key uses the scaled size so
    # different global scales don't collide in the font cache.
    size = max(7, int(round(_eff(size))))
    key = (kind, size)
    f = _chat_fonts.get(key)
    if f is None:
        # The body font uses the global font helper so it picks up the
        # same monospaced face the rest of the UI uses (Consolas with
        # SysFont fallback). meta is slightly smaller; badge slightly
        # bolder.
        f = get_font("Consolas", size, bold=(kind == "badge"))
        _chat_fonts[key] = f
    return f


def _chat_get_cjk_font(size):
    """Return a font with CJK glyph coverage at the requested size.

    Consolas can't render hiragana/katakana/kanji — it shows tofu
    boxes. For chat lines containing Japanese characters (or other
    non-ASCII content like accented player names), we fall back to
    a font with full coverage.

    Strategy (in order):
      1. The bundled Noto Sans JP at chat/fonts/NotoSansJP-Regular.ttf
         — this is the reliable path. Ships with OmniWatch under the
         SIL OFL license. Covers all kana, JIS first+second level
         kanji, and Latin/punctuation. Works regardless of which
         system fonts the user has installed.
      2. System CJK fonts if the bundled file isn't found (e.g. user
         moved it or extraction was incomplete). Tries Yu Gothic UI,
         Meiryo, MS Gothic — all bundled with modern Windows.
      3. pygame's default font — last resort. Lacks CJK coverage so
         Japanese characters will show as tofu boxes, but the panel
         won't crash.

    The first lookup at each size is logged so users can confirm
    Japanese rendering will work without launching FFXI.
    """
    # Match the global UI scale applied in _chat_get_font so CJK and
    # Latin chat text stay the same size.
    size = max(7, int(round(_eff(size))))
    key = ("cjk", size)
    f = _chat_fonts.get(key)
    if f is not None:
        return f

    # Try bundled font first. Path resolution mirrors the icon-loading
    # convention earlier in this file — when frozen (PyInstaller .exe),
    # we look in the .exe's directory + walking up; when running from
    # source, relative to __file__.
    chosen_name = None
    if getattr(sys, "frozen", False):
        _self_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        _self_dir = os.path.dirname(os.path.abspath(__file__))
    # Search the .py/exe folder and one level up (PyInstaller dumps
    # the exe into <addon>/dist/ while chat/ lives at <addon>/chat/).
    for _base in [_self_dir, os.path.dirname(_self_dir)]:
        if not _base or not os.path.isdir(_base):
            continue
        for _rel in ("chat/fonts/NotoSansJP-Regular.ttf",
                     "chat/fonts/NotoSansJP-Regular.otf"):
            _path = os.path.join(_base, _rel.replace("/", os.sep))
            if os.path.isfile(_path):
                try:
                    f = pygame.font.Font(_path, size)
                    chosen_name = f"bundled ({_rel})"
                    break
                except (pygame.error, OSError) as _e:
                    print(f"[OmniChat] Bundled font load failed "
                          f"({_path}): {_e!r}")
        if f is not None:
            break

    # Fallback to system fonts.
    if f is None:
        system_candidates = [
            "Yu Gothic UI", "Yu Gothic",
            "Meiryo UI",    "Meiryo",
            "MS UI Gothic", "MS Gothic",
            "Noto Sans CJK JP", "Noto Sans JP",
            "MS Mincho",
        ]
        for name in system_candidates:
            path = pygame.font.match_font(name)
            if path:
                try:
                    f = pygame.font.Font(path, size)
                    chosen_name = f"system ({name})"
                    break
                except (pygame.error, OSError):
                    continue

    # Last resort: pygame default. No CJK glyphs — tofu boxes ahead.
    if f is None:
        f = pygame.font.SysFont(None, size)
        chosen_name = "<pygame default — NO CJK SUPPORT>"

    # Log selection once per size. If users see "<pygame default>"
    # in their console output they know to install / drop in the
    # bundled font file.
    if size not in _chat_cjk_logged:
        print(f"[OmniChat] Chat CJK font @ {size}px: {chosen_name}")
        _chat_cjk_logged.add(size)

    _chat_fonts[key] = f
    return f


# Tracks which sizes we've already logged CJK font selection for, so
# repeated calls don't spam the console.
_chat_cjk_logged = set()


def _chat_is_cjk_char(c):
    """Should this character use the CJK fallback font?

    Heuristic: ASCII (codepoint < 0x80) → Consolas. Anything above
    → fallback. This errs on the side of using the fallback font
    for accented Latin too (é, ü, ñ), which Yu Gothic UI renders
    fine; the alternative (per-codepoint coverage check) is much
    more code for marginal visual benefit.
    """
    return ord(c) >= 0x80


def _chat_split_runs(text):
    """Split text into runs of consistent script (ASCII vs CJK/non-ASCII).

    Returns a list of (chunk_str, is_cjk) tuples. Adjacent characters
    of the same script class are grouped into one chunk — fewer runs
    means fewer font.render() calls per line. ASCII-only or pure-CJK
    lines return a single run.
    """
    if not text:
        return []
    runs = []
    cur_chunk = ""
    cur_is_cjk = None
    for c in text:
        is_cjk = _chat_is_cjk_char(c)
        if cur_is_cjk is None:
            cur_is_cjk = is_cjk
        if is_cjk != cur_is_cjk:
            runs.append((cur_chunk, cur_is_cjk))
            cur_chunk = ""
            cur_is_cjk = is_cjk
        cur_chunk += c
    if cur_chunk:
        runs.append((cur_chunk, cur_is_cjk))
    return runs


def _chat_strip_unrenderable(text):
    """Remove codepoints that pygame fonts can't render as readable
    glyphs. Specifically:

      - C0 controls (0x00-0x1F) except tab and newline. These should
        already be stripped on the lua side, but defense in depth.
      - C1 controls (0x80-0x9F). FFXI's autotranslate sometimes
        survives the SJIS decode as bytes in this range.
      - Private Use Area (U+E000-U+F8FF). The Shift-JIS user-defined
        area maps into the PUA, and autotranslate phrase wrappers
        commonly land here when the decoder doesn't know what to do
        with the bytes. Pygame's fallback font renders these as empty
        boxes which look like garbage to the user.
      - Specials (U+FFF0-U+FFFF) including the replacement character
        U+FFFD when it appears as decoder residue (we keep U+FFFD
        only in segments that came in pre-segmented, since those have
        already been decoded once on the lua side — but at the render
        level we treat all of these as unrenderable).
      - Variation selectors and zero-width joiner residue
        (U+FE00-U+FE0F, U+200B-U+200F).

    The result is what the user actually sees. Original text in
    chat_events is unchanged so diagnostics still show the raw bytes.
    """
    if not text:
        return text
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 0x09 or cp == 0x0A:
            out.append(ch)
            continue
        if cp < 0x20:
            continue          # C0 control
        if 0x7F <= cp <= 0x9F:
            continue          # DEL + C1 controls
        if 0xE000 <= cp <= 0xF8FF:
            continue          # Private Use Area
        if 0xFE00 <= cp <= 0xFE0F:
            continue          # Variation selectors
        if 0x200B <= cp <= 0x200F:
            continue          # ZWJ / direction marks
        if 0xFFF0 <= cp <= 0xFFFF:
            continue          # Specials block (incl. U+FFFD)
        out.append(ch)
    return "".join(out)


def _chat_render_mixed(text, body_font, cjk_font, color):
    """Render a line that may contain mixed ASCII and CJK/non-ASCII.

    Splits the text into script runs, renders each with the
    appropriate font, then composes a single surface. ASCII-only
    text falls through to a single body_font.render() — same cost
    as the non-fallback path.

    Returns a pygame.Surface (caller blits at the desired position).
    """
    if not text:
        return body_font.render("", True, color)
    # Strip codepoints pygame can't render as glyphs — FFXI auto-
    # translate residue, control bytes, PUA junk. Done at the render
    # funnel so every chat path benefits without changes upstream.
    text = _chat_strip_unrenderable(text)
    if not text:
        return body_font.render("", True, color)
    runs = _chat_split_runs(text)

    # Diagnostic: dump up to 5 different non-ASCII texts that arrive at
    # the renderer. The earlier one-shot version showed U+0702 (Syriac)
    # from an unidentified source, so we want broader visibility. We
    # also dump the full UTF-8 byte encoding of each chunk to identify
    # whether the bytes look like valid Japanese (E3 8x xx, E4-E9 xx
    # xx) or something else entirely.
    global _chat_render_diag_count
    if _chat_render_diag_count < 5:
        for chunk, is_cjk in runs:
            if is_cjk and chunk:
                cps = [f"U+{ord(c):04X}" for c in chunk[:12]]
                tail = "..." if len(chunk) > 12 else ""
                bytes_hex = chunk.encode("utf-8", errors="replace").hex(" ")
                # Truncate to first 60 bytes for log readability.
                if len(bytes_hex) > 180:
                    bytes_hex = bytes_hex[:180] + " ..."
                print(f"[OmniChat] CJK render #{_chat_render_diag_count + 1}: "
                      f"text={chunk!r} "
                      f"codepoints=[{', '.join(cps)}{tail}] "
                      f"utf8_hex=[{bytes_hex}]")
                # Also log the surrounding FULL text so we can see what
                # came in as a whole and identify which chat line / UI
                # element produced this.
                full_cps = [f"U+{ord(c):04X}" for c in text[:30]]
                print(f"[OmniChat] CJK render #{_chat_render_diag_count + 1} "
                      f"full text={text!r} (first 30 codepoints: "
                      f"{', '.join(full_cps)})")
                _chat_render_diag_count += 1
                break

    # Fast path: single run — render directly, no compositing.
    if len(runs) == 1:
        chunk, is_cjk = runs[0]
        font = cjk_font if is_cjk else body_font
        return font.render(chunk, True, color)
    # Mixed: render each run, compose horizontally.
    surfs = []
    for chunk, is_cjk in runs:
        font = cjk_font if is_cjk else body_font
        surfs.append(font.render(chunk, True, color))
    total_w = sum(s.get_width() for s in surfs)
    max_h = max(s.get_height() for s in surfs)
    composed = pygame.Surface((max(1, total_w), max(1, max_h)),
                              pygame.SRCALPHA)
    cur_x = 0
    for s in surfs:
        # Top-align (not baseline-align) for simplicity. The two
        # fonts have similar metrics at the same size; tiny vertical
        # mismatch is acceptable. If users complain about wobble, we
        # can compute baselines per font and align by that.
        composed.blit(s, (cur_x, 0))
        cur_x += s.get_width()
    return composed


# Module-level counter for the CJK render diagnostic. Allows up to 5
# different non-ASCII texts to be logged per session — enough to spot
# patterns without flooding the console. Reset by reloading OmniWatch.
_chat_render_diag_count = 0


def _chat_measure_mixed(text, body_font, cjk_font):
    """Measure pixel width of mixed-script text without rendering.

    Used by the wrap routine. Walks runs the same way _chat_render_mixed
    does, sums per-run widths. font.size(s)[0] is O(n) in n bytes; this
    is fine for wrap measurement (called against trial strings during
    word fit-checking).
    """
    if not text:
        return 0
    runs = _chat_split_runs(text)
    w = 0
    for chunk, is_cjk in runs:
        font = cjk_font if is_cjk else body_font
        w += font.size(chunk)[0]
    return w


def _chat_font_sizes():
    """Return current body/meta/tab font sizes based on chat_font_size setting.

    Setting is "small", "medium", or "large" — anything else falls back
    to medium. Sizes are looked up in CHAT_FONT_SIZE_MAP. Reading the
    setting once per draw keeps the panel responsive to setting changes
    without needing render-side cache invalidation.
    """
    pref = setting("chat_font_size") if "chat_font_size" in SETTINGS_BY_KEY else "medium"
    return CHAT_FONT_SIZE_MAP.get(pref, CHAT_FONT_SIZE_MAP["medium"])



# =========================================================================
# SECTION: G_composer
# =========================================================================

def _chat_composer_send():
    """Format and dispatch the current composer text as a slash command.

    Format depends on:
      * Active channel (say/tell/reply/shout/yell/ls1/ls2)
      * Whether the message starts with '/' (raw command escape: any
        text starting with / is sent as-is, bypassing channel prefix
        — works on ALL channels per design)
      * For tell: requires a non-empty target name in the tell-target
        field, otherwise refuses to send

    Dispatch is the same UDP rail that hotbar button commands use:
      socket.sendto("input <slash command>", ("127.0.0.1", 5111))
    The lua side translates "input ..." into windower.send_command,
    which feeds FFXI's chat system. We use SETTING|... or other prefix
    schemes for non-FFXI ones; for chat we pass "input <cmd>" directly.

    On success, clears the input field but preserves channel selection
    (the user is likely to send another message on the same channel).
    """
    global chat_composer_text, chat_composer_cursor

    text = chat_composer_text.strip()
    if not text:
        return

    ch_idx = chat_composer_channel % len(CHAT_COMPOSER_CHANNELS)
    _ch_key, _ch_label, ch_prefix = CHAT_COMPOSER_CHANNELS[ch_idx]

    # Slash-command escape: any input starting with '/' is sent as a
    # raw FFXI command on every channel. Bypasses the channel prefix.
    # Useful for /lockstyle, /follow, /target, etc. without leaving
    # the chat input.
    if text.startswith("/"):
        payload = "input " + text
    elif _ch_key == "tell":
        target = chat_composer_tell_to.strip()
        if not target:
            # Silent fail — the empty target field will visually nag
            # the user. Could pop a transient message later.
            return
        payload = "input /t " + target + " " + text
    else:
        # Standard channel: prefix + message.
        payload = "input " + ch_prefix + text

    # Dispatch via the same UDP socket buttons use.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(payload.encode("utf-8"), ("127.0.0.1", 5111))
        s.close()
    except Exception as e:
        print(f"[OmniChat] chat send failed: {e}")
        return

    # Clear input on successful dispatch. Keep tell-target so the user
    # can fire follow-up tells to the same person without retyping.
    chat_composer_text = ""
    chat_composer_cursor = 0


# ── Click-on-sender-name actions ─────────────────────────────────────────
# Mouse interactions on rendered sender names in the chat panel. The
# rendering side records per-frame hit-test rects into
# _chat_clickable_senders (entries: {"rect", "name", "actor_class"}).
# These helpers are called from the main MOUSEBUTTONDOWN handler when
# a click hits one of those rects.
#
# Left-click → populate the chat composer with a tell to that name. We
# deliberately do NOT fire the /tell immediately: the user almost always
# wants to type a message first. Switching the composer channel to
# "tell" + filling the tell-target field + focusing the message input
# is the natural setup for that.
#
# Right-click → open a context menu next to the rect with "Send Tell"
# and "Invite to Party". "Send Tell" reuses the left-click setup;
# "Invite to Party" fires `/pcmd add <name>` directly via the same UDP
# rail the hotbar buttons use.

def _chat_compose_tell_to(name):
    """Set up the composer to send a tell to `name`.

    Switches the composer to the "tell" channel (index 1 in
    CHAT_COMPOSER_CHANNELS), populates the tell-target field, focuses
    the message body input (so the user can immediately start typing
    without an extra click), and shows the composer if hidden. The
    user still has to type a message and hit send / Enter — we never
    fire a /tell automatically from a click, since that would make
    accidental clicks send empty tells.

    Safe to call with an empty `name` (no-op).
    """
    if not name:
        return
    global chat_composer_channel, chat_composer_tell_to
    global chat_composer_tell_to_cursor
    global chat_composer_focused, chat_composer_tell_to_focused
    global chat_composer_visible
    # Find the "tell" channel index by key, in case the channels list
    # is reordered later (safer than hard-coding 1).
    tell_idx = 1
    for i, (key, _label, _prefix) in enumerate(CHAT_COMPOSER_CHANNELS):
        if key == "tell":
            tell_idx = i
            break
    chat_composer_channel = tell_idx
    chat_composer_tell_to = name
    chat_composer_tell_to_cursor = len(name)
    # Focus the MESSAGE body, not the tell-target, since the target is
    # now filled in. The user wants to start typing the message.
    chat_composer_focused = True
    chat_composer_tell_to_focused = False
    if not chat_composer_visible:
        chat_composer_visible = True


def _chat_invite_to_party(name):
    """Send /pcmd add <name> via the UDP command rail.

    Same dispatch path used by hotbar windower-kind buttons and the
    composer send helper. If the target is already in your party, or
    you're not the party leader, FFXI will reject the invite with its
    own error message — we don't try to validate client-side. The
    in-game error feedback is more informative than anything we'd
    invent.

    No-op for empty name.
    """
    if not name:
        return
    try:
        payload = ("input /pcmd add " + name).encode("utf-8")
        sock_cmd_out.sendto(payload, CMD_OUT_ADDR)
        print(f"[OmniChat] invite -> {name}")
    except Exception as e:
        print(f"[OmniChat] invite failed: {e!r}")


def _chat_blacklist_add(name):
    """Add `name` to the OmniWatch routing-config blacklist.

    Writes to _meta.blacklist in omnichat_chat_routing.json (same
    field the routing GUI's footer manages — the "Mytoy ×" chip row
    you see in the GUI). NOT to FFXI's server-side /blist: OmniWatch
    chat is a local overlay concern, separate from the in-game
    blacklist by design. Blacklisting in the overlay should NOT
    affect what FFXI itself sees, only what the OW chat panel shows.

    Two-step update for instant effect with no reload required:
      1. Add to _chat_blacklist live set so the very next filter pass
         hides messages from this sender (chat panel updates on the
         next frame).
      2. Append to _meta.blacklist in the JSON file so the entry
         survives an addon reload / restart.

    The live set is compared case-insensitively (lowercase strings);
    the JSON stores the original-case name to match the GUI display
    convention ("Mytoy" not "mytoy"). De-duplicates on both sides.

    Known race: if the routing GUI is open at the same time, its
    Save will rewrite the whole file with its in-memory blacklist
    snapshot — possibly clobbering our addition. The GUI footer text
    already tells users to "Reload OmniChat to apply", so collision
    in practice is rare and self-recovering (re-add and don't reopen
    the GUI mid-session).

    No-op for empty name.
    """
    if not name:
        return
    name_clean = name.strip()
    if not name_clean:
        return

    global _chat_blacklist
    # Live set update first. Filter takes effect on the next frame's
    # _chat_classify_for_routing call — chat panel immediately hides
    # messages from this sender without a reload.
    _chat_blacklist = set(_chat_blacklist) if _chat_blacklist else set()
    _chat_blacklist.add(name_clean.lower())

    # Persist to JSON. Pull current file, splice our entry into
    # _meta.blacklist, rewrite. We do this manually rather than
    # routing through the GUI's save_config helper because that helper
    # rewrites the WHOLE config (stripping empty actor sections, etc.);
    # we only want to touch _meta.blacklist, leaving everything else
    # byte-for-byte intact in case the user has hand-edited the rest.
    try:
        path = os.path.join(SETTINGS_DIR, "omnichat_chat_routing.json")
    except Exception as e:
        print(f"[OmniChat] blacklist add: bad path: {e!r}")
        return
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
        meta = data.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
            data["_meta"] = meta
        # Existing list (or empty if missing / wrong type).
        cur = meta.get("blacklist")
        if not isinstance(cur, list):
            cur = []
        # De-dupe case-insensitively against existing entries while
        # preserving original casing where present. If the name is
        # already there in any case, skip the write — live set update
        # above is still beneficial (it normalizes to lowercase) but
        # we don't need to touch the file.
        cur_lower = {s.strip().lower() for s in cur
                     if isinstance(s, str) and s.strip()}
        if name_clean.lower() in cur_lower:
            print(f"[OmniChat] blacklist add: {name_clean!r} already "
                  "in routing config (live set refreshed)")
            return
        cur.append(name_clean)
        # Sort for stable file diffs — matches the GUI's save behavior.
        meta["blacklist"] = sorted(cur, key=str.lower)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[OmniChat] blacklist add: {name_clean} → "
              "omnichat_chat_routing.json")
    except Exception as e:
        print(f"[OmniChat] blacklist add failed for {name_clean!r}: {e!r}")


def _chat_open_name_context_menu(name, actor_class, anchor_pos):
    """Open the right-click context menu for a chat sender name.

    Stores the menu state in _chat_name_context_menu; the actual
    rendering happens in draw_chat_panel (which positions the menu
    near anchor_pos and records per-item rects for hit-testing).

    All names get the same three items (Send Tell, Invite to Party,
    Blacklist) regardless of actor_class — even self/party. Per the
    design call: party/self click is harmless (tell to a party member
    works fine, invite is silently rejected by FFXI for already-in-party
    targets, blacklist on a self/party name is the user's call).

    The Blacklist item carries a `gap_before` flag that the menu
    renderer uses to insert extra vertical space + a separator line
    above it. Misclick safety: blacklist is a one-way action in FFXI
    (no undo confirmation), so the visual gap keeps it from sitting
    directly under Invite where an off-by-one click might land.
    """
    global _chat_name_context_menu
    _chat_name_context_menu = {
        "name":        name,
        "actor_class": actor_class or "other",
        "anchor":      anchor_pos,    # (x, y) where the menu opens
        "items": [
            {"label": "Send Tell",       "action": "tell",      "rect": None},
            {"label": "Invite to Party", "action": "invite",    "rect": None},
            {"label": "Blacklist Add",   "action": "blacklist", "rect": None,
             "gap_before": True},
        ],
    }


def _chat_close_name_context_menu():
    """Dismiss the context menu, if open."""
    global _chat_name_context_menu
    _chat_name_context_menu = None


def _chat_handle_name_context_action(action, name):
    """Dispatch a context-menu item's action.

    Called when the user clicks one of the menu items in
    _chat_name_context_menu. Closes the menu unconditionally after
    dispatch so the menu doesn't linger after the action fires.
    """
    if action == "tell":
        _chat_compose_tell_to(name)
    elif action == "invite":
        _chat_invite_to_party(name)
    elif action == "blacklist":
        _chat_blacklist_add(name)
    _chat_close_name_context_menu()


def _chat_update_name_hover(mx, my):
    """Update _chat_name_hover based on current mouse position.

    Walks _chat_clickable_senders and sets the hover state to the
    sender name whose rect contains (mx, my), or None if no rect
    matches. Cheap — typically <50 entries, single collidepoint per.
    Called from MOUSEMOTION; the next draw paints accordingly.
    """
    global _chat_name_hover
    hit = None
    for entry in _chat_clickable_senders:
        try:
            if entry["rect"].collidepoint(mx, my):
                hit = entry["name"]
                break
        except Exception:
            continue
    _chat_name_hover = hit


def _chat_handle_name_click(button, mx, my):
    """Check if (mx, my) hit a clickable sender name and act on it.

    Returns True if a name was hit and the click was consumed.
    `button` is the pygame mouse button index (1 = left, 3 = right).
    Should be called BEFORE more generic chat-panel click handlers so
    a click on a name doesn't fall through to e.g. tab switching.
    """
    for entry in _chat_clickable_senders:
        try:
            if not entry["rect"].collidepoint(mx, my):
                continue
        except Exception:
            continue
        name = entry.get("name") or ""
        actor_class = entry.get("actor_class") or "other"
        if not name:
            return False
        if button == 1:
            _chat_compose_tell_to(name)
            return True
        if button == 3:
            _chat_open_name_context_menu(name, actor_class, (mx, my))
            return True
        return False
    return False


def _chat_handle_name_context_click(button, mx, my):
    """Hit-test the open context menu, if any.

    Returns True if the click was consumed (either by selecting an
    item or by clicking outside the menu, which dismisses it).
    """
    if _chat_name_context_menu is None:
        return False
    # Any click while the menu is open consumes the event — either as
    # an item selection or as a dismiss. This is standard menu UX:
    # the menu has modal focus until resolved.
    for item in _chat_name_context_menu.get("items", []):
        r = item.get("rect")
        if r is not None and r.collidepoint(mx, my):
            # Left-click selects; right-click on a menu item also
            # selects (slightly forgiving — user already opened the
            # menu, they probably meant to click).
            if button in (1, 3):
                action = item.get("action")
                name = _chat_name_context_menu.get("name") or ""
                _chat_handle_name_context_action(action, name)
                return True
    # Click was inside the menu's overall rect but not on any item?
    # Still consume to prevent accidental fall-through. Otherwise it's
    # outside the menu → dismiss.
    menu_rect = _chat_name_context_menu.get("rect")
    if menu_rect is not None and menu_rect.collidepoint(mx, my):
        return True
    _chat_close_name_context_menu()
    return True


def _chat_draw_name_context_menu(surface):
    """Render the open context menu, if any, and record item rects.

    Called from draw_chat_panel after all other chat rendering so the
    menu sits ON TOP of everything else in the panel. Sets each item's
    rect for hit-testing on the next click.

    Layout:
      ┌────────────────────────┐
      │ <Name>                 │   ← header row (non-clickable label)
      ├────────────────────────┤
      │ Send Tell              │   ← item row
      │ Invite to Party        │   ← item row
      └────────────────────────┘

    Width is sized to fit the longest item label + name. Anchored at
    the click position; if it would clip off the right/bottom edge of
    the surface, shift it left/up to stay on-screen.
    """
    if _chat_name_context_menu is None:
        return
    menu = _chat_name_context_menu
    name = menu.get("name") or ""
    items = menu.get("items", [])
    if not items:
        return

    # Font + sizing.
    font = _chat_get_font("meta", 12)
    pad_x = 10
    pad_y = 5
    row_h = font.get_linesize() + 2
    # Extra vertical space inserted above any item that carries the
    # `gap_before` flag (currently only Blacklist). Acts as a visual
    # "section break" so a misclick from the row above doesn't slide
    # into a destructive action.
    gap_h = 6     # blank pixels above the gapped item
    sep_h = 1     # separator line drawn inside the gap
    header_text = font.render(name, True, CHAT_YELL_ZONE_COLOR)
    item_surfs = []
    for it in items:
        item_surfs.append(font.render(it.get("label") or "",
                                      True, (230, 230, 230)))
    max_w = header_text.get_width()
    for s in item_surfs:
        if s.get_width() > max_w:
            max_w = s.get_width()
    menu_w = max_w + pad_x * 2
    # Sum extra height from any gap_before items so the backdrop is
    # tall enough to contain them. Counting it once here keeps the
    # menu_h / menu_rect / clamping math in one place.
    gap_total = 0
    for it in items:
        if it.get("gap_before"):
            gap_total += gap_h
    # Header row + separator + items rows + gaps.
    menu_h = row_h + 1 + row_h * len(items) + gap_total + pad_y * 2

    # Anchor; clamp to surface bounds.
    ax, ay = menu.get("anchor", (0, 0))
    sw, sh = surface.get_size()
    mx = min(ax, sw - menu_w - 2)
    my = min(ay, sh - menu_h - 2)
    if mx < 0:
        mx = 0
    if my < 0:
        my = 0
    menu_rect = pygame.Rect(mx, my, menu_w, menu_h)
    menu["rect"] = menu_rect

    # Backdrop.
    bg = pygame.Surface((menu_w, menu_h), pygame.SRCALPHA)
    bg.fill((24, 28, 36, 240))
    surface.blit(bg, menu_rect.topleft)
    pygame.draw.rect(surface, (100, 110, 130), menu_rect, 1)

    # Header label (the name in pale yellow, non-clickable).
    cy = my + pad_y
    surface.blit(header_text, (mx + pad_x, cy))
    cy += row_h
    # Separator line.
    pygame.draw.line(surface, (60, 70, 85),
                     (mx + 4, cy), (mx + menu_w - 4, cy), 1)
    cy += 1

    # Item rows. Record each row's rect for hit-testing. Hover
    # highlight uses the live mouse position. Items flagged
    # `gap_before` get extra vertical space plus a thin separator
    # line inserted above them — the misclick-safety affordance for
    # destructive actions like Blacklist.
    mouse_pos = pygame.mouse.get_pos()
    for i, it in enumerate(items):
        if it.get("gap_before"):
            # Inject the gap, with a separator centered vertically
            # within it. The separator runs the full menu width
            # (minus a small inset) so it visually belongs to the
            # menu rather than to either neighboring item.
            sep_y = cy + (gap_h // 2)
            pygame.draw.line(surface, (60, 70, 85),
                             (mx + 4, sep_y), (mx + menu_w - 4, sep_y),
                             sep_h)
            cy += gap_h
        item_rect = pygame.Rect(mx + 1, cy, menu_w - 2, row_h)
        it["rect"] = item_rect
        if item_rect.collidepoint(mouse_pos):
            hov_bg = pygame.Surface((item_rect.width, item_rect.height),
                                    pygame.SRCALPHA)
            hov_bg.fill((60, 80, 110, 220))
            surface.blit(hov_bg, item_rect.topleft)
        surface.blit(item_surfs[i], (mx + pad_x, cy + 1))
        cy += row_h


def _chat_composer_handle_keydown(event):
    """Process a pygame KEYDOWN event for the composer.

    Returns True if the event was consumed, False if it should fall
    through to other handlers. Routes based on which composer field
    is focused (main message vs tell-target).
    """
    global chat_composer_text, chat_composer_cursor
    global chat_composer_tell_to, chat_composer_tell_to_cursor
    global chat_composer_focused, chat_composer_tell_to_focused

    key = event.key

    # Enter on any focused field → send.
    if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
        _chat_composer_send()
        return True

    # Escape clears the focused field's content AND unfocuses.
    # Matches the design: "Enter sends, Escape clears + unfocuses."
    if key == pygame.K_ESCAPE:
        if chat_composer_tell_to_focused:
            chat_composer_tell_to = ""
            chat_composer_tell_to_cursor = 0
            chat_composer_tell_to_focused = False
        if chat_composer_focused:
            chat_composer_text = ""
            chat_composer_cursor = 0
            chat_composer_focused = False
        return True

    # Tab: switch focus between main and tell-target when on tell
    # channel. Doesn't apply otherwise — fall through (allowing tab
    # to be consumed by other handlers if any).
    if key == pygame.K_TAB:
        ch_idx = chat_composer_channel % len(CHAT_COMPOSER_CHANNELS)
        is_tell = CHAT_COMPOSER_CHANNELS[ch_idx][0] == "tell"
        if is_tell:
            chat_composer_focused, chat_composer_tell_to_focused = (
                chat_composer_tell_to_focused, chat_composer_focused)
            return True
        return False

    # The rest operates on whichever field is focused.
    if chat_composer_tell_to_focused:
        # Edit the tell-target buffer.
        if key == pygame.K_BACKSPACE:
            if chat_composer_tell_to_cursor > 0:
                cur = chat_composer_tell_to_cursor
                chat_composer_tell_to = (
                    chat_composer_tell_to[:cur - 1]
                    + chat_composer_tell_to[cur:])
                chat_composer_tell_to_cursor -= 1
            return True
        if key == pygame.K_DELETE:
            cur = chat_composer_tell_to_cursor
            chat_composer_tell_to = (
                chat_composer_tell_to[:cur]
                + chat_composer_tell_to[cur + 1:])
            return True
        if key == pygame.K_LEFT:
            chat_composer_tell_to_cursor = max(
                0, chat_composer_tell_to_cursor - 1)
            return True
        if key == pygame.K_RIGHT:
            chat_composer_tell_to_cursor = min(
                len(chat_composer_tell_to),
                chat_composer_tell_to_cursor + 1)
            return True
        if key == pygame.K_HOME:
            chat_composer_tell_to_cursor = 0
            return True
        if key == pygame.K_END:
            chat_composer_tell_to_cursor = len(chat_composer_tell_to)
            return True
        # Other keys fall through — TEXTINPUT will handle character
        # insertion for typed letters.
        return False

    if chat_composer_focused:
        # Edit the main message buffer. Same operations as above but
        # against chat_composer_text + chat_composer_cursor.
        if key == pygame.K_BACKSPACE:
            if chat_composer_cursor > 0:
                cur = chat_composer_cursor
                chat_composer_text = (chat_composer_text[:cur - 1]
                                      + chat_composer_text[cur:])
                chat_composer_cursor -= 1
            return True
        if key == pygame.K_DELETE:
            cur = chat_composer_cursor
            chat_composer_text = (chat_composer_text[:cur]
                                  + chat_composer_text[cur + 1:])
            return True
        if key == pygame.K_LEFT:
            chat_composer_cursor = max(0, chat_composer_cursor - 1)
            return True
        if key == pygame.K_RIGHT:
            chat_composer_cursor = min(
                len(chat_composer_text), chat_composer_cursor + 1)
            return True
        if key == pygame.K_HOME:
            chat_composer_cursor = 0
            return True
        if key == pygame.K_END:
            chat_composer_cursor = len(chat_composer_text)
            return True
        return False

    return False


def _chat_composer_handle_textinput(text):
    """Insert typed text at the cursor of the focused composer field.

    pygame's TEXTINPUT event fires for committed Unicode input,
    including IME-composed sequences. `text` is the string to insert
    (one character normally, but can be multi-char for IME results).
    Inserts at the focused field's cursor position.
    """
    global chat_composer_text, chat_composer_cursor
    global chat_composer_tell_to, chat_composer_tell_to_cursor

    if not text:
        return

    if chat_composer_tell_to_focused:
        # Tell-target field — FFXI names are letters only. Strip any
        # non-alpha characters from the typed text (paste-protection).
        # Cap at 16 chars (longest FFXI name is 15).
        filtered = "".join(c for c in text if c.isalpha())
        if not filtered:
            return
        room = max(0, 16 - len(chat_composer_tell_to))
        if room == 0:
            return
        filtered = filtered[:room]
        cur = chat_composer_tell_to_cursor
        chat_composer_tell_to = (chat_composer_tell_to[:cur]
                                 + filtered
                                 + chat_composer_tell_to[cur:])
        chat_composer_tell_to_cursor += len(filtered)
        return

    if chat_composer_focused:
        # Main message field — accept anything. FFXI chat messages
        # can be quite long; cap at 500 to avoid runaway input
        # (FFXI's own limit is around 256 bytes after encoding).
        room = max(0, 500 - len(chat_composer_text))
        if room == 0:
            return
        insert = text[:room]
        cur = chat_composer_cursor
        chat_composer_text = (chat_composer_text[:cur]
                              + insert
                              + chat_composer_text[cur:])
        chat_composer_cursor += len(insert)


def _chat_composer_height():
    """Pixel height of the composer row. Constant — same height
    whether or not the tell target field is showing (the target
    field is inline-left, not stacked)."""
    return 30


def _draw_chat_composer(surface, x, y, w, body_font, meta_font, cjk_font):
    """Render the composer row (channel selector + input field + send).

    Layout (left to right):
        [< say >]                          channel selector — arrow,
                                           channel name, arrow
        [target]   (only when tell)        tell-target field
        [............message field..............]   main input
        [send]                             send button

    Click targets are stashed in the _chat_composer_rect_* globals
    so the mousedown handler can hit-test them without recomputing
    geometry.
    """
    global _chat_composer_rect_arrow_l, _chat_composer_rect_arrow_r
    global _chat_composer_rect_channel, _chat_composer_rect_input
    global _chat_composer_rect_tell_to, _chat_composer_rect_send
    global chat_composer_last_blink

    h = _chat_composer_height()
    pad_inner = 4

    # Composer background fills the full width of the panel and
    # leaves room for borders on left/right that the panel itself
    # draws elsewhere.
    bg = pygame.Surface((w, h), pygame.SRCALPHA)
    bg.fill(CHAT_COMPOSER_BG)
    surface.blit(bg, (x, y))
    # Thin top border separating composer from scrollback above.
    pygame.draw.line(surface, CHAT_BORDER_COLOR, (x, y), (x + w, y), 1)

    # Get current channel info.
    ch_idx = chat_composer_channel % len(CHAT_COMPOSER_CHANNELS)
    _ch_key, ch_label, _ch_prefix = CHAT_COMPOSER_CHANNELS[ch_idx]
    is_tell = (_ch_key == "tell")

    # ── Channel selector: [<] channel-name [>] ──────────────────
    # Compute widths so the components line up consistently regardless
    # of channel name length. We allocate a fixed channel-name width
    # based on the widest channel name in the list — keeps the input
    # field's left edge from jumping when channels change.
    arrow_font = body_font     # arrows are bigger and easier to click
    name_font  = body_font
    widest = max(name_font.size(label)[0]
                 for _k, label, _p in CHAT_COMPOSER_CHANNELS)
    arrow_w   = arrow_font.size("<")[0] + 8
    chname_w  = widest + 6     # +6 for small horizontal padding
    cx = x + 4
    cy = y + (h - 18) // 2
    # Left arrow
    arr_l = pygame.Rect(cx, y + 2, arrow_w, h - 4)
    _chat_composer_rect_arrow_l = arr_l
    al_surf = arrow_font.render("<", True, CHAT_COMPOSER_ARROW_FG)
    surface.blit(al_surf,
                 (arr_l.x + (arr_l.w - al_surf.get_width()) // 2,
                  arr_l.y + (arr_l.h - al_surf.get_height()) // 2))
    cx += arrow_w
    # Channel name (clickable — same as right arrow)
    ch_rect = pygame.Rect(cx, y + 2, chname_w, h - 4)
    _chat_composer_rect_channel = ch_rect
    # Tint the label to match the channel's chat color (party teal, unity
    # gold, ls1 green, etc.) so the selector reads at a glance. Falls back
    # to the neutral foreground for any unmapped key.
    _ch_color_class = CHAT_COMPOSER_CHANNEL_COLOR_CLASS.get(_ch_key)
    _ch_name_color = (CHAT_SEGMENT_COLORS.get(_ch_color_class,
                                              CHAT_COMPOSER_CHANNEL_FG)
                      if _ch_color_class else CHAT_COMPOSER_CHANNEL_FG)
    ch_surf = name_font.render(ch_label, True, _ch_name_color)
    surface.blit(ch_surf,
                 (ch_rect.x + (ch_rect.w - ch_surf.get_width()) // 2,
                  ch_rect.y + (ch_rect.h - ch_surf.get_height()) // 2))
    cx += chname_w
    # Right arrow
    arr_r = pygame.Rect(cx, y + 2, arrow_w, h - 4)
    _chat_composer_rect_arrow_r = arr_r
    ar_surf = arrow_font.render(">", True, CHAT_COMPOSER_ARROW_FG)
    surface.blit(ar_surf,
                 (arr_r.x + (arr_r.w - ar_surf.get_width()) // 2,
                  arr_r.y + (arr_r.h - ar_surf.get_height()) // 2))
    cx += arrow_w + 4

    # ── { } auto-translate button ───────────────────────────────
    # Wraps the composer text for auto-translate sending (see the
    # click handler): with text present, the whole message becomes
    # {message}; empty, it inserts {} and parks the cursor inside so
    # you type the phrase directly. The lua side converts {phrase}
    # into the real in-game auto-translate token on send.
    global _chat_composer_rect_at
    at_label = "{ }"
    at_w = body_font.size(at_label)[0] + 12
    at_rect = pygame.Rect(cx, y + 3, at_w, h - 6)
    _chat_composer_rect_at = at_rect
    _at_hov = at_rect.collidepoint(pygame.mouse.get_pos())
    at_bg = pygame.Surface((at_rect.w, at_rect.h), pygame.SRCALPHA)
    at_bg.fill((70, 85, 110, 235) if _at_hov else (46, 54, 68, 225))
    surface.blit(at_bg, at_rect.topleft)
    pygame.draw.rect(surface, CHAT_COMPOSER_FIELD_BDR, at_rect, 1)
    at_surf = body_font.render(at_label, True,
                               (240, 240, 245) if _at_hov
                               else (185, 195, 210))
    surface.blit(at_surf,
                 (at_rect.x + (at_rect.w - at_surf.get_width()) // 2,
                  at_rect.y + (at_rect.h - at_surf.get_height()) // 2))
    cx += at_w + 4

    # ── Send button (right-aligned, reserved width) ────────────
    send_label = "send"
    send_w = body_font.size(send_label)[0] + 16
    send_rect = pygame.Rect(x + w - send_w - 4, y + 3,
                            send_w, h - 6)
    _chat_composer_rect_send = send_rect
    send_bg = pygame.Surface((send_rect.w, send_rect.h), pygame.SRCALPHA)
    send_bg.fill((*CHAT_COMPOSER_SEND_BG, 235))
    surface.blit(send_bg, send_rect.topleft)
    pygame.draw.rect(surface, CHAT_COMPOSER_FIELD_BDR, send_rect, 1)
    s_surf = body_font.render(send_label, True, CHAT_COMPOSER_SEND_FG)
    surface.blit(s_surf,
                 (send_rect.x + (send_rect.w - s_surf.get_width()) // 2,
                  send_rect.y + (send_rect.h - s_surf.get_height()) // 2))

    # ── Tell-target field (only when channel == tell) ──────────
    # Placed inline-left of the message field. Width is fixed at
    # ~100px which fits "Wormfood" comfortably and is short enough
    # not to crowd the main field.
    available_w = (send_rect.x - 4) - cx       # space between selector
                                               # and send button
    if is_tell:
        tt_w = 110
        tt_rect = pygame.Rect(cx, y + 3, tt_w, h - 6)
        _chat_composer_rect_tell_to = tt_rect
        # Background — focused vs unfocused
        focused = chat_composer_tell_to_focused
        tt_bg = pygame.Surface((tt_rect.w, tt_rect.h), pygame.SRCALPHA)
        tt_bg.fill(CHAT_COMPOSER_FIELD_FOCUS if focused
                   else CHAT_COMPOSER_FIELD_BG)
        surface.blit(tt_bg, tt_rect.topleft)
        bdr = (CHAT_COMPOSER_FIELD_BDR_F if focused
               else CHAT_COMPOSER_FIELD_BDR)
        pygame.draw.rect(surface, bdr, tt_rect, 1)
        # Render text or placeholder
        tt_text = chat_composer_tell_to
        if tt_text:
            tt_surf = _chat_render_mixed(tt_text, body_font,
                                         cjk_font, CHAT_COMPOSER_TEXT)
        else:
            tt_surf = body_font.render("to:", True, CHAT_COMPOSER_PLACEHOLDER)
        surface.blit(tt_surf,
                     (tt_rect.x + 6,
                      tt_rect.y + (tt_rect.h - tt_surf.get_height()) // 2))
        # Cursor for tell-target field (only when focused)
        if focused:
            now = time.time()
            blink_on = (int(now * 2) % 2) == 0
            if blink_on:
                cursor_x = tt_rect.x + 6
                if tt_text:
                    pre = tt_text[:chat_composer_tell_to_cursor]
                    cursor_x += _chat_measure_mixed(pre, body_font, cjk_font)
                cy_top = tt_rect.y + 4
                cy_bot = tt_rect.bottom - 4
                pygame.draw.line(surface, CHAT_COMPOSER_TEXT,
                                 (cursor_x, cy_top), (cursor_x, cy_bot), 1)
        cx += tt_w + 4
    else:
        _chat_composer_rect_tell_to = None

    # ── Main message field ─────────────────────────────────────
    field_w = (send_rect.x - 4) - cx
    if field_w < 40:
        # Panel too narrow — clip the field but still draw something.
        field_w = max(40, field_w)
    field_rect = pygame.Rect(cx, y + 3, field_w, h - 6)
    _chat_composer_rect_input = field_rect
    focused = chat_composer_focused
    field_bg = pygame.Surface((field_rect.w, field_rect.h), pygame.SRCALPHA)
    field_bg.fill(CHAT_COMPOSER_FIELD_FOCUS if focused
                  else CHAT_COMPOSER_FIELD_BG)
    surface.blit(field_bg, field_rect.topleft)
    bdr = (CHAT_COMPOSER_FIELD_BDR_F if focused
           else CHAT_COMPOSER_FIELD_BDR)
    pygame.draw.rect(surface, bdr, field_rect, 1)

    # Render text or placeholder. For horizontal scrolling within
    # the field: compute the cursor pixel offset; if it would land
    # past the visible field width, shift the view left so the
    # cursor stays in view.
    inner_x = field_rect.x + 6
    inner_max_x = field_rect.right - 6
    inner_w = inner_max_x - inner_x
    cursor_px_in_text = _chat_measure_mixed(
        chat_composer_text[:chat_composer_cursor], body_font, cjk_font)
    # Scroll offset within the text: we want cursor_px_in_text to
    # fall in [0, inner_w). If it would exceed inner_w, shift right.
    scroll_off = max(0, cursor_px_in_text - inner_w + 4)

    # Clip rendering to the field's interior so long text doesn't
    # bleed over the send button.
    prev_clip = surface.get_clip()
    surface.set_clip(pygame.Rect(inner_x, field_rect.y + 1,
                                 inner_w, field_rect.h - 2))
    if chat_composer_text:
        text_surf = _chat_render_mixed(chat_composer_text, body_font,
                                       cjk_font, CHAT_COMPOSER_TEXT)
        surface.blit(text_surf,
                     (inner_x - scroll_off,
                      field_rect.y + (field_rect.h - text_surf.get_height()) // 2))
    else:
        ph = body_font.render(
            "Click to type, Enter to send, Esc to cancel",
            True, CHAT_COMPOSER_PLACEHOLDER)
        surface.blit(ph,
                     (inner_x,
                      field_rect.y + (field_rect.h - ph.get_height()) // 2))

    # Cursor (only when main field focused)
    if focused:
        now = time.time()
        blink_on = (int(now * 2) % 2) == 0
        if blink_on:
            cursor_x = inner_x + cursor_px_in_text - scroll_off
            cy_top = field_rect.y + 4
            cy_bot = field_rect.bottom - 4
            pygame.draw.line(surface, CHAT_COMPOSER_TEXT,
                             (cursor_x, cy_top), (cursor_x, cy_bot), 1)
    surface.set_clip(prev_clip)



# =========================================================================
# SECTION: H_draw
# =========================================================================

def draw_chat_tab_rclick_popup(surface):
    """Render the right-click tab popup if one is open.

    A small floating menu anchored at the right-click position with
    one button: 'Hide tab'. Click that button to add the tab's
    index to the hidden_chat_tabs setting (causing it to vanish
    from the tab strip). Click anywhere else to dismiss the popup
    without changes.

    Drawn AFTER the chat panel so it floats above the tab strip.
    Per-frame click rects are written to _chat_tab_rclick_rects for
    the main left-click handler to consume.
    """
    global _chat_tab_rclick_rects
    _chat_tab_rclick_rects = []
    if _chat_tab_rclick_tab is None or _chat_tab_rclick_anchor is None:
        return
    if not (0 <= _chat_tab_rclick_tab < len(chat_tab_names)):
        return   # defensive — stale index after tab list change

    ax, ay = _chat_tab_rclick_anchor
    tab_label = chat_tab_names[_chat_tab_rclick_tab][0]
    title_font = _chat_get_font("meta", 11)
    # Two text rows: a small header showing which tab the popup
    # targets, and the actionable button below.
    hdr_text  = f"Tab: {tab_label}"
    btn_text  = "Hide tab"
    hdr_w     = title_font.size(hdr_text)[0]
    btn_w     = title_font.size(btn_text)[0]
    pad       = 8
    row_h     = 20
    panel_w   = max(hdr_w, btn_w) + pad * 2 + 6
    panel_h   = row_h * 2 + pad
    # Keep the popup on-screen — clamp to surface bounds.
    sw, sh = surface.get_size()
    px = min(max(0, ax), sw - panel_w)
    py = min(max(0, ay), sh - panel_h)

    # Backdrop with a 1px border so it visually pops above chat.
    panel_rect = pygame.Rect(px, py, panel_w, panel_h)
    pygame.draw.rect(surface, (30, 34, 42), panel_rect, border_radius=3)
    pygame.draw.rect(surface, (140, 160, 200), panel_rect, 1,
                     border_radius=3)
    # Eat clicks on the panel background so they don't dismiss the
    # popup (a click on the panel chrome is still inside the popup).
    _chat_tab_rclick_rects.append(
        (panel_rect, {"action": "rclick_panel_bg"}))

    # Header label.
    hdr_surf = title_font.render(hdr_text, True, (180, 190, 200))
    surface.blit(hdr_surf, (px + pad, py + pad // 2))

    # "Hide tab" button.
    btn_rect = pygame.Rect(px + pad - 2, py + row_h + 2,
                            panel_w - (pad - 2) * 2, row_h - 2)
    mouse_pos = pygame.mouse.get_pos()
    is_hov   = btn_rect.collidepoint(mouse_pos)
    bg = (90, 50, 60) if is_hov else (55, 40, 48)
    pygame.draw.rect(surface, bg, btn_rect, border_radius=2)
    pygame.draw.rect(surface, (180, 120, 140), btn_rect, 1,
                     border_radius=2)
    btn_surf = title_font.render(btn_text, True,
        (255, 220, 220) if is_hov else (220, 200, 205))
    surface.blit(btn_surf,
        (btn_rect.x + (btn_rect.w - btn_surf.get_width()) // 2,
         btn_rect.y + (btn_rect.h - btn_surf.get_height()) // 2))
    _chat_tab_rclick_rects.append(
        (btn_rect, {"action": "rclick_hide_tab"}))


def dispatch_chat_tab_rclick_popup_click(mx, my, button=1):
    """Handle a left-click while the tab right-click popup is open.

    Returns True if the click was consumed (either by the Hide
    button or by clicking the panel background). Returns False if
    the click missed the popup entirely — in that case the caller
    should also dismiss the popup AND let the click fall through
    to whatever it would normally hit.
    """
    global _chat_tab_rclick_tab, _chat_tab_rclick_anchor
    global chat_active_tab
    if _chat_tab_rclick_tab is None:
        return False
    target_tab = _chat_tab_rclick_tab
    # Iterate in REVERSE order so the most-specific (innermost) rect
    # wins over outer ones. The panel-background rect is added first
    # but contains the Hide button rect added after — without
    # reversed(), a click on the button would hit the background
    # first and return early as "click on chrome", missing the hide.
    for rect, action in reversed(_chat_tab_rclick_rects):
        if not rect.collidepoint(mx, my):
            continue
        what = action.get("action", "")
        if what == "rclick_panel_bg":
            # Click on the popup chrome but not a button — eat it
            # but keep the popup open so the user can still click
            # the Hide button. Same UX as a desktop context menu.
            return True
        if what == "rclick_hide_tab":
            _current = list(settings.get("hidden_chat_tabs") or [])
            if target_tab not in _current:
                _current.append(target_tab)
                set_setting("hidden_chat_tabs", _current)
            # If we just hid the active tab, bump to the first
            # visible remaining tab.
            if chat_active_tab == target_tab:
                for i in range(len(chat_tab_names)):
                    if i not in _current:
                        chat_active_tab = i
                        chat_tab_unread[i] = 0
                        break
            print(f"[chat-tab] hid tab {target_tab} "
                  f"({chat_tab_names[target_tab][0]!r}) via "
                  f"right-click popup; hidden list now {_current}")
            _chat_tab_rclick_tab    = None
            _chat_tab_rclick_anchor = None
            return True
    # Click landed outside the popup — dismiss without changes.
    # Returning False lets the click also fall through to the
    # normal handler so the user can do whatever they intended.
    _chat_tab_rclick_tab    = None
    _chat_tab_rclick_anchor = None
    return False


def draw_chat_panel(surface, x, y, locked=False):
    """Render the chat panel at (x, y).

    Reads chat_events (newest-last deque), the active tab's filter and
    scroll position, and chat_panel_dims. Sets _chat_jump_badge_rect and
    chat_tab_rects each frame for the mousedown handler to consume.
    """
    global _chat_jump_badge_rect, chat_tab_rects
    global _chat_clickable_senders

    pw, ph = chat_panel_size()

    # Reset per-frame clickable-sender hit-test list. Populated inside
    # the message-rendering loop below whenever a sender name is blitted
    # (left-click → /tell composer, right-click → context menu). Reset
    # here so a hidden chat panel or a re-layout can't leak stale
    # rectangles into the next frame's hit-tests.
    _chat_clickable_senders = []

    # Background
    _chat_blit_panel_bg(surface, x, y, pw, ph)

    # Header strip
    hdr_h = 18
    hdr_surf = pygame.Surface((pw, hdr_h), pygame.SRCALPHA)
    hdr_surf.fill(CHAT_HEADER_COLOR)
    surface.blit(hdr_surf, (x, y))
    title_font = _chat_get_font("meta", 11)
    title = title_font.render(
        f"Chat  ({chat_recv_text + chat_recv_battle} events)",
        True, (200, 210, 220))
    surface.blit(title, (x + 8, y + 3))

    # Routing-config gear button at the right edge of the header.
    # Clicking launches omnichat_routing_gui.exe (or the .py fallback
    # if exe isn't built yet). Hit-testing is done in the click
    # handler via the rect stored in _chat_settings_button_rect.
    global _chat_settings_button_rect
    global _chat_clear_tab_button_rect, _chat_clear_all_button_rect
    mouse_pos = pygame.mouse.get_pos()

    gear_h = hdr_h - 2
    gear_w = title_font.size("Filters \u2699")[0] + 14
    gear_rect = pygame.Rect(x + pw - gear_w - 4, y + 1, gear_w, gear_h)
    _chat_settings_button_rect = gear_rect
    gear_hovered = gear_rect.collidepoint(mouse_pos)
    gear_bg = (60, 70, 90, 220) if gear_hovered else (40, 46, 56, 200)
    gear_surf = pygame.Surface((gear_rect.width, gear_rect.height),
                                pygame.SRCALPHA)
    gear_surf.fill(gear_bg)
    surface.blit(gear_surf, gear_rect.topleft)
    gear_text = title_font.render("Filters ⚙",
                                   True,
                                   (240, 240, 240) if gear_hovered
                                   else (180, 190, 200))
    gtx = gear_rect.x + (gear_rect.width - gear_text.get_width()) // 2
    gty = gear_rect.y + (gear_rect.height - gear_text.get_height()) // 2
    surface.blit(gear_text, (gtx, gty))

    # Options button — window/display preferences (opacity, always on
    # top, font size, composer visibility). Sits just left of Filters.
    # Hit-testing in the click handler via _chat_options_button_rect;
    # the popup itself is drawn by draw_options_popup after everything
    # else so it floats on top.
    global _chat_options_button_rect
    opt_w = title_font.size("Options")[0] + 14
    opt_rect = pygame.Rect(gear_rect.x - 6 - opt_w, y + 1, opt_w, gear_h)
    _chat_options_button_rect = opt_rect
    opt_hovered = opt_rect.collidepoint(mouse_pos)
    opt_active = opt_hovered or _options_popup_open
    opt_bg = (60, 70, 90, 220) if opt_active else (40, 46, 56, 200)
    opt_surf = pygame.Surface((opt_rect.width, opt_rect.height),
                               pygame.SRCALPHA)
    opt_surf.fill(opt_bg)
    surface.blit(opt_surf, opt_rect.topleft)
    opt_text = title_font.render("Options",
                                  True,
                                  (240, 240, 240) if opt_active
                                  else (180, 190, 200))
    surface.blit(opt_text,
                 (opt_rect.x + (opt_rect.width - opt_text.get_width()) // 2,
                  opt_rect.y + (opt_rect.height - opt_text.get_height()) // 2))

    # Two clear buttons just AFTER the title text, on the LEFT side —
    # deliberately far from the Filters button so they're not hit by
    # accident.
    #   "Clear Tab"  — removes only events visible in the active tab.
    #   "Clear All"  — wipes the entire chat buffer (every tab).
    def _draw_hdr_button(label, left_edge, width):
        r = pygame.Rect(left_edge, y + 1, width, gear_h)
        hov = r.collidepoint(mouse_pos)
        bg = (70, 55, 55, 220) if hov else (40, 46, 56, 200)
        s = pygame.Surface((r.width, r.height), pygame.SRCALPHA)
        s.fill(bg)
        surface.blit(s, r.topleft)
        t = title_font.render(label, True,
                              (240, 240, 240) if hov else (180, 190, 200))
        surface.blit(t, (r.x + (r.width - t.get_width()) // 2,
                         r.y + (r.height - t.get_height()) // 2))
        return r

    _gap = 6
    # Buttons size to their label so text can never overflow into the
    # neighbor (the old fixed 60px was narrower than "Clear Tab"
    # renders, which made the two buttons read as one merged blob).
    _clr_tab_w = title_font.size("Clear Tab")[0] + 14
    _clr_all_w = title_font.size("Clear All")[0] + 14
    _clr_left = x + 8 + title.get_width() + 12   # just past the title text
    _chat_clear_tab_button_rect = _draw_hdr_button(
        "Clear Tab", _clr_left, _clr_tab_w)
    _chat_clear_all_button_rect = _draw_hdr_button(
        "Clear All", _chat_clear_tab_button_rect.right + _gap, _clr_all_w)

    # "Show all tabs" — anchored to the right side of the header,
    # just LEFT of the Filters gear button (which is right-edge
    # anchored). Renders only when at least one tab is hidden so
    # there's something to unhide. Putting it on the right keeps
    # the left-side cluster (Clear Tab / Clear All) tight and
    # avoids hit-confusion with the destructive Clear buttons.
    global _chat_show_all_button_rect
    _chat_show_all_button_rect = None
    _hidden_now = settings.get("hidden_chat_tabs") or []
    if _hidden_now:
        _sa_label = f"Show all tabs ({len(_hidden_now)})"
        _sa_w     = max(110, title_font.size(_sa_label)[0] + 16)
        # Position: right of Filters minus gap minus our width.
        _sa_x = opt_rect.x - _gap - _sa_w
        _sa_r = pygame.Rect(_sa_x, y + 1, _sa_w, gear_h)
        _sa_hov = _sa_r.collidepoint(mouse_pos)
        _sa_bg  = (60, 80, 70, 220) if _sa_hov else (40, 46, 56, 200)
        _sa_surf = pygame.Surface((_sa_r.width, _sa_r.height),
                                   pygame.SRCALPHA)
        _sa_surf.fill(_sa_bg)
        surface.blit(_sa_surf, _sa_r.topleft)
        _sa_t = title_font.render(_sa_label, True,
            (240, 240, 240) if _sa_hov else (180, 200, 190))
        surface.blit(_sa_t,
            (_sa_r.x + (_sa_r.width  - _sa_t.get_width())  // 2,
             _sa_r.y + (_sa_r.height - _sa_t.get_height()) // 2))
        _chat_show_all_button_rect = _sa_r

    # Read font sizes once per draw based on chat_font_size setting.
    # Tab strip height scales with tab font so the strip doesn't look
    # cramped on Large or oversized on Small.
    fs = _chat_font_sizes()
    body_font_size = fs["body"]
    meta_font_size = fs["meta"]
    tab_font_size  = fs["tab"]

    # ── Tab strip ───────────────────────────────────────────────
    # Renders below the header bar. Each tab is sized to fit its
    # full name + unread badge (if any). Rendered left-to-right;
    # if total width exceeds the panel, later tabs are clipped.
    # Default panel width is 800 to accommodate all 10 tabs at
    # medium font size; users on Small can fit at narrower widths,
    # Large may need 900+.
    tab_h = max(20, tab_font_size + 10)
    tab_font = _chat_get_font("body", tab_font_size)
    tab_pad_x = 10                          # horizontal padding inside each tab
    tab_gap   = 2                           # gap between tabs
    tab_y = y + hdr_h
    # Rebuild click-target list for this frame.
    chat_tab_rects = []
    badge_h = max(12, tab_font_size + 2)

    # ── Measure every tab first ─────────────────────────────────
    # We need the total width up front to know whether the strip
    # overflows (and thus whether to show scroll arrows). Names are
    # never abbreviated — overflow is handled by sideways scrolling.
    global _chat_tab_hscroll, _chat_tab_arrow_rects
    tab_meta = []   # list of (tab_idx, full, tab_w, badge_text, badge_w, name_w)
    # Per-tab hide state lives in settings as a list of indices the
    # user has hidden via right-click. The set is populated lazily
    # from the persisted list each frame (cheap — typically <14
    # entries). When all tabs are visible the set is empty.
    hidden_tabs = set(settings.get("hidden_chat_tabs") or [])
    for tab_idx, (_short, full) in enumerate(chat_tab_names):
        if tab_idx in hidden_tabs:
            continue   # user hid this tab via right-click
        active = (tab_idx == chat_active_tab)
        unread = chat_tab_unread.get(tab_idx, 0) if not active else 0
        name_w = tab_font.size(full)[0]
        badge_w = 0
        badge_text = None
        if unread > 0:
            badge_text = str(unread) if unread < 1000 else "999+"
            badge_w = tab_font.size(badge_text)[0] + 8
        tab_w = tab_pad_x * 2 + name_w + (4 + badge_w if badge_w else 0)
        tab_meta.append((tab_idx, full, tab_w, badge_text, badge_w, name_w))
    total_tabs_w = sum(m[2] for m in tab_meta) + tab_gap * max(0, len(tab_meta) - 1)

    # ── Reserve arrow zones / compute the scrollable strip ──────
    # Strip spans from just inside the left edge to just inside the
    # right edge. If the tabs overflow, we carve out an arrow button
    # at each end and scroll the middle.
    strip_left  = x + 2
    strip_right = x + pw - 2
    strip_w     = strip_right - strip_left
    arrow_w     = tab_h        # square-ish arrow buttons
    overflow    = total_tabs_w > strip_w

    _chat_tab_arrow_rects = {"left": None, "right": None}
    if overflow:
        # Inner area between the two arrows.
        inner_left  = strip_left + arrow_w
        inner_right = strip_right - arrow_w
        inner_w     = inner_right - inner_left
        max_scroll  = max(0, total_tabs_w - inner_w)
        # Clamp the persisted scroll into range.
        if _chat_tab_hscroll < 0:
            _chat_tab_hscroll = 0
        elif _chat_tab_hscroll > max_scroll:
            _chat_tab_hscroll = max_scroll
        clip_left, clip_w = inner_left, inner_w
        tab_x0 = inner_left - _chat_tab_hscroll
    else:
        # Everything fits — no arrows, no scroll.
        _chat_tab_hscroll = 0
        max_scroll = 0
        clip_left, clip_w = strip_left, strip_w
        tab_x0 = strip_left

    # ── Draw tabs (clipped to the inner strip) ──────────────────
    prev_clip = surface.get_clip()
    surface.set_clip(pygame.Rect(clip_left, tab_y, clip_w, tab_h))
    tab_x = tab_x0
    for (tab_idx, full, tab_w, badge_text, badge_w, name_w) in tab_meta:
        active = (tab_idx == chat_active_tab)
        theme = (CHAT_TAB_PALETTE[tab_idx]
                 if tab_idx < len(CHAT_TAB_PALETTE)
                 else {"active":   CHAT_TAB_FG_ACTIVE,
                       "inactive": CHAT_TAB_FG_INACTIVE})
        # Skip drawing tabs fully outside the visible strip (perf + the
        # clip already hides them, but this avoids needless blits).
        if tab_x + tab_w < clip_left or tab_x > clip_left + clip_w:
            tab_x += tab_w + tab_gap
            continue
        # Draw tab background.
        bg = CHAT_TAB_BG_ACTIVE if active else CHAT_TAB_BG_INACTIVE
        tab_bg = pygame.Surface((tab_w, tab_h), pygame.SRCALPHA)
        tab_bg.fill(bg)
        surface.blit(tab_bg, (tab_x, tab_y))
        if active:
            pygame.draw.line(surface, theme["active"],
                             (tab_x, tab_y + tab_h - 1),
                             (tab_x + tab_w - 1, tab_y + tab_h - 1), 2)
        fg = theme["active"] if active else theme["inactive"]
        # Focus-phrase tab pulse: an inactive tab holding an unseen
        # focus hit blinks its label toward the highlight amber until
        # the user visits it (visit clears the marker below).
        if active:
            _chat_tab_focus_pulse.pop(tab_idx, None)
        elif tab_idx in _chat_tab_focus_pulse:
            _pt = math.sin(time.time() * 5.0) * 0.5 + 0.5   # 0..1
            fg = tuple(
                int(fg[i] + (CHAT_FOCUS_HL[i] - fg[i]) * (0.35 + 0.65 * _pt))
                for i in range(3))
        name_surf = tab_font.render(full, True, fg)
        surface.blit(name_surf,
                     (tab_x + tab_pad_x,
                      tab_y + (tab_h - name_surf.get_height()) // 2))
        if badge_text:
            bx_text = tab_x + tab_pad_x + name_w + 4
            by_text = tab_y + (tab_h - badge_h) // 2
            badge_surf = pygame.Surface((badge_w, badge_h), pygame.SRCALPHA)
            badge_surf.fill((*CHAT_TAB_UNREAD_BG, 240))
            surface.blit(badge_surf, (bx_text, by_text))
            badge_surf2 = tab_font.render(badge_text, True, CHAT_TAB_UNREAD_FG)
            surface.blit(badge_surf2,
                         (bx_text + (badge_w - badge_surf2.get_width()) // 2,
                          by_text + (badge_h - badge_surf2.get_height()) // 2 - 1))
        # Record hit-target only for the portion within the strip. Clamp
        # the rect to the visible area so a click on a half-scrolled tab
        # at the edge still maps correctly (and clicks in the arrow zone
        # don't fall through to a tab underneath).
        vis_l = max(tab_x, clip_left)
        vis_r = min(tab_x + tab_w, clip_left + clip_w)
        if vis_r > vis_l:
            chat_tab_rects.append(
                (pygame.Rect(vis_l, tab_y, vis_r - vis_l, tab_h), tab_idx))
        tab_x += tab_w + tab_gap
    surface.set_clip(prev_clip)

    # ── Scroll arrows (only when overflowing) ───────────────────
    if overflow:
        arrow_mid_y = tab_y + tab_h // 2
        # Left arrow — enabled only if scrolled right of start.
        l_active = _chat_tab_hscroll > 0
        l_rect = pygame.Rect(strip_left, tab_y, arrow_w, tab_h)
        l_bg = pygame.Surface((arrow_w, tab_h), pygame.SRCALPHA)
        l_bg.fill(CHAT_TAB_BG_INACTIVE)
        surface.blit(l_bg, (l_rect.x, l_rect.y))
        l_col = (CHAT_TAB_FG_ACTIVE if l_active else (90, 95, 105))
        _ax = l_rect.centerx
        pygame.draw.polygon(surface, l_col, [
            (_ax + 3, arrow_mid_y - 5),
            (_ax + 3, arrow_mid_y + 5),
            (_ax - 4, arrow_mid_y)])
        _chat_tab_arrow_rects["left"] = l_rect if l_active else None
        # Right arrow — enabled only if more tabs lie past the right edge.
        r_active = _chat_tab_hscroll < max_scroll
        r_rect = pygame.Rect(strip_right - arrow_w, tab_y, arrow_w, tab_h)
        r_bg = pygame.Surface((arrow_w, tab_h), pygame.SRCALPHA)
        r_bg.fill(CHAT_TAB_BG_INACTIVE)
        surface.blit(r_bg, (r_rect.x, r_rect.y))
        r_col = (CHAT_TAB_FG_ACTIVE if r_active else (90, 95, 105))
        _bx = r_rect.centerx
        pygame.draw.polygon(surface, r_col, [
            (_bx - 3, arrow_mid_y - 5),
            (_bx - 3, arrow_mid_y + 5),
            (_bx + 4, arrow_mid_y)])
        _chat_tab_arrow_rects["right"] = r_rect if r_active else None

    # Border (1px outline around whole panel)
    pygame.draw.rect(surface, CHAT_BORDER_COLOR, (x, y, pw, ph), 1)
    # Mint accent stripe down the left edge — matches the per-panel
    # accent convention used by every other OmniWatch panel.
    draw_accent_stripe(surface, x, y, ph, ACCENT_CHAT)
    # Line under the tab strip to separate from content
    pygame.draw.line(surface, CHAT_BORDER_COLOR,
                     (x, y + hdr_h + tab_h),
                     (x + pw, y + hdr_h + tab_h), 1)

    # ── Content area (filtered by active tab) ───────────────────
    # Reserve space at the bottom for the composer row if visible.
    composer_h = _chat_composer_height() if chat_composer_visible else 0
    content_x = x + 6
    content_y = y + hdr_h + tab_h + 4
    content_w = pw - 12
    content_h = ph - hdr_h - tab_h - 8 - composer_h

    body_font = _chat_get_font("body", body_font_size)
    meta_font = _chat_get_font("meta", meta_font_size)
    cjk_font  = _chat_get_cjk_font(body_font_size)

    line_h = body_font.get_linesize()
    if line_h <= 0:
        line_h = body_font_size + 2

    visible_lines = max(1, content_h // line_h)

    # Reserve horizontal space for the timestamp prefix.
    ts_text = "00:00 "
    ts_w = meta_font.size(ts_text)[0]
    text_x = content_x + ts_w + 4
    text_max_w = content_w - ts_w - 4

    # Apply the active tab's filter as we walk events. The All tab's
    # filter trivially accepts everything; specialized tabs evaluate
    # a mode set lookup. Filter call cost is negligible (set lookup).
    active_filter = chat_tab_filters[chat_active_tab]
    active_scroll = chat_tab_scroll.get(chat_active_tab, 0)

    # Walk events newest -> oldest, applying filter, wrap each, until
    # we have visible_lines + scroll covered.
    events = list(chat_events)

    # Total physical lines across the FULL filtered+wrapped chat —
    # not just what we rendered. Used for two things:
    #   1. Scroll anchoring (below): keep the user's view stable when
    #      new events arrive while scrolled up.
    #   2. Scrollbar thumb sizing: without this, the rendering loop's
    #      early-break (it stops once it has enough lines to cover
    #      the visible window + a 10-line buffer) would make
    #      physical_lines_total grow as the user scrolls UP and
    #      shrink as they scroll DOWN — and thus make the thumb
    #      visibly resize. With true_total in hand the thumb size
    #      stays anchored to the real chat depth, only changing
    #      when actual messages arrive or filter changes.
    # Cost is one extra full-history walk per chat-panel frame. For
    # typical chat sizes (sub-1000 events) this is cheap (~100µs).
    true_total = 0
    for ev in events:
        try:
            if not active_filter(ev):
                continue
        except Exception:
            continue
        w = _chat_wrap_cached(ev, body_font, cjk_font, text_max_w)
        true_total += len(w) if w else 1

    # Scroll anchoring: when the user has scrolled UP, new events arriving
    # at the bottom must NOT slide their view. active_scroll is measured
    # in visible-lines-from-bottom, so if N new physical lines were
    # appended since last frame while scrolled up, bump active_scroll by N
    # to keep the same messages in view. At the bottom (active_scroll==0)
    # we leave it 0 so autoscroll keeps showing the newest line. Done
    # BEFORE the walk so `needed` below accounts for the bumped offset.
    if active_scroll > 0:
        prev_total = _chat_tab_line_total.get(chat_active_tab)
        if prev_total is not None and true_total > prev_total:
            active_scroll = active_scroll + (true_total - prev_total)
            chat_tab_scroll[chat_active_tab] = active_scroll
        _chat_tab_line_total[chat_active_tab] = true_total
    else:
        _chat_tab_line_total[chat_active_tab] = None

    physical_lines_total = 0
    rendered_segments = []
    needed = visible_lines + max(0, active_scroll)
    for ev in reversed(events):
        try:
            if not active_filter(ev):
                continue
        except Exception:
            continue
        wrapped = _chat_wrap_cached(ev, body_font, cjk_font, text_max_w)
        mode = ev.get("mode", 0)
        ts_str = _chat_format_timestamp(ev.get("ts", 0))

        # Detect wrap shape: list[str] = plain text wrap, list[list]
        # = segmented (colored spans) wrap. Drives the rendering path
        # for this event.
        is_segmented = (wrapped
                        and isinstance(wrapped[0], list))

        # For splittable chat modes (say/tell/shout/yell), use the
        # per-mode message color and the sender-orange split. For
        # everything else (battle, system, unknown), use the regular
        # palette color and no split. Segmented events bypass both
        # paths since per-segment colors override.
        sender_text, _msg_text_full = (None, None)
        if not is_segmented:
            sender_text, _msg_text_full = _chat_split_sender_cached(ev)
        # GearSwap output (macro-set echoes + "X is now Y" state lines)
        # arrives on mode 1, so the mode-based color below would tint it
        # /say-white. Detect it the same way the classifier routes it and
        # force gold to match the Gearswap tab theme.
        _ev_text = ev.get("text") or ""
        _is_gearswap_line = (
            any(_ev_text.startswith(p) for p in _GEARSWAP_TEXT_PREFIXES_R)
            or _GEARSWAP_STATE_PATTERN_R.match(_ev_text) is not None
        )
        if _is_gearswap_line:
            body_color = CHAT_GEARSWAP_BODY_COLOR
            sender_text = None      # no sender/orange split for addon output
        elif sender_text is not None:
            body_color = CHAT_MSG_COLOR_BY_MODE.get(mode, CHAT_COLOR_DEFAULT)
        else:
            # Pass the full event (not just mode) so source-based color
            # routing applies to synthetic buff/debuff events.
            body_color = _chat_color_for_mode(ev)

        # wrapped[0] is the FIRST physical line. Sender (if any) only
        # appears on that first line. Subsequent wrapped lines are
        # all message body, rendered in body_color uniformly.
        first_physical = wrapped[0] if wrapped else ""
        for i, ln in enumerate(reversed(wrapped)):
            is_top_of_event = (i == len(wrapped) - 1)
            # `is_top_of_event` here means "first physical line of the
            # event" (we walked the wrapped list in reverse to render
            # bottom-up, so the LAST element in our reversed iteration
            # is the original wrapped[0]).
            seg = {
                "color":  body_color,
                "text":   ln,
                "ts":     ts_str if is_top_of_event else "",
                "spans":  ln if is_segmented else None,
            }
            if ev.get("focus_hit"):
                seg["focus_ts"] = ev.get("focus_ts") or ev.get("ts") or 0
                # Character ranges of each matched word within THIS
                # physical line (a phrase split across a wrap boundary
                # simply doesn't highlight — neither half is the
                # phrase). Plain text is the span-concat for segmented
                # lines, the line string otherwise.
                _fp = (ln if not is_segmented
                       else "".join(t for t, _ in ln))
                _fl = _fp.lower()
                _ranges = []
                for _w in (ev.get("focus_words") or []):
                    _i = 0
                    while True:
                        _i = _fl.find(_w, _i)
                        if _i < 0:
                            break
                        _ranges.append((_i, _i + len(_w)))
                        _i += len(_w)
                if _ranges:
                    seg["focus_plain"]  = _fp
                    seg["focus_ranges"] = _ranges
            # Mark the first-physical-line of a splittable event with
            # the sender region info so the renderer can do two-color
            # blit. We compute the sender pixel-width here (once per
            # event per width change, since wrap+split are cached).
            if (not is_segmented) and is_top_of_event \
                    and sender_text is not None \
                    and ln == first_physical:
                # Edge case: extremely narrow panel could wrap the
                # sender text itself. Only enable split rendering if
                # the sender text is fully contained in this first
                # wrapped line. Otherwise fall back to single color.
                if ln.startswith(sender_text):
                    seg["sender_text"] = sender_text
                    # Color the sender's name. Default is orange
                    # (CHAT_SENDER_COLOR), but if this is the player
                    # talking (sender text contains their character
                    # name), use the fixed self-identity blue so the
                    # player's own name reads consistently across
                    # every chat type — see CHAT_SELF_NAME_COLOR.
                    if (player_self_name
                            and player_self_name in sender_text):
                        seg["sender_color"] = CHAT_SELF_NAME_COLOR
                    else:
                        seg["sender_color"] = CHAT_SENDER_COLOR
                    seg["msg_offset"] = len(sender_text)
            rendered_segments.append(seg)
            physical_lines_total += 1
            if physical_lines_total >= needed + 10:
                break
        if physical_lines_total >= needed + 10:
            break

    # Clamp scroll to available filtered history.
    max_scroll = max(0, physical_lines_total - visible_lines)
    if active_scroll > max_scroll:
        active_scroll = max_scroll
        chat_tab_scroll[chat_active_tab] = active_scroll
    if active_scroll < 0:
        active_scroll = 0
        chat_tab_scroll[chat_active_tab] = 0

    # Slice the visible window. rendered_segments is newest-first.
    start_idx = active_scroll
    end_idx   = min(physical_lines_total, start_idx + visible_lines)
    window = rendered_segments[start_idx:end_idx]

    # Render bottom-up. window[0] goes at bottom, window[-1] at top.
    bottom_y = content_y + content_h - line_h
    _focus_now_t = time.time()
    for offset, seg in enumerate(window):
        ly = bottom_y - offset * line_h
        if ly < content_y:
            break
        # Focus-word highlight: a pulsing amber shade behind JUST the
        # matched word(s), not the whole line. Word pixel positions
        # come from measuring the text before/inside each match with
        # the same body font the line renders in. Pulses (sine on
        # alpha) for FOCUS_PULSE_SECS after the hit, then settles to
        # a steady faint shade so older hits stay findable.
        _fts = seg.get("focus_ts")
        _frs = seg.get("focus_ranges")
        if _fts and _frs:
            _age = _focus_now_t - _fts
            if _age < FOCUS_PULSE_SECS:
                _fa = int(95 + 70 * math.sin(_age * 6.0))
            else:
                _fa = 48
            _fa = max(0, min(255, _fa))
            _fp = seg.get("focus_plain") or ""
            for _a, _b in _frs:
                try:
                    _pre_w  = body_font.size(_fp[:_a])[0]
                    _word_w = body_font.size(_fp[_a:_b])[0]
                except Exception:
                    continue
                _wx = text_x + _pre_w - 2
                _ww = _word_w + 4
                _patch = pygame.Surface((max(1, _ww), line_h),
                                        pygame.SRCALPHA)
                _patch.fill(CHAT_FOCUS_HL[:3] + (_fa,))
                surface.blit(_patch, (_wx, ly))
        if seg["ts"]:
            ts_surf = meta_font.render(seg["ts"], True, CHAT_TIMESTAMP_COLOR)
            surface.blit(ts_surf,
                         (content_x,
                          ly + (line_h - ts_surf.get_height()) // 2))
        # Three render paths:
        #   1. Segmented: walk per-span and blit each in its own color.
        #   2. Splittable sender (say/tell): two-color blit with orange
        #      sender + body color message.
        #   3. Plain single-color body.
        spans = seg.get("spans")
        sender_marker = seg.get("sender_text")
        if spans is not None:
            # Walk spans left-to-right. Each span gets its own color
            # via _chat_render_mixed (which also handles CJK fallback).
            #
            # Two color overrides on top of the static class→color
            # lookup: (1) "default"-class spans on a player-chat mode
            # get the per-mode body color instead of gray, so message
            # bodies pick up the channel tint (purple tells, pink
            # yells, light-yellow shouts, etc.) even when lua emits
            # the line as pre-segmented spans rather than as a single
            # splittable string. (2) ch_*-class spans that contain
            # the player's own character name render in a fixed blue
            # so the player's name reads as one consistent identity
            # color regardless of channel — other senders keep their
            # channel-themed ch_* colors so channels remain visually
            # distinct at a glance.
            cur_x = text_x
            # Track outgoing-tell context: once we see a sender region
            # that ends with '>>' (the unique outgoing-tell wire marker)
            # and contains the player's name, treat following default
            # spans as the tell body even if the mode field disagrees.
            # Source path differences (text-event vs 0x017 packet) can
            # land an outgoing tell at mode 0, 4, or 12 depending on
            # the addon load order and Windower version; this content-
            # based detection works regardless.
            _saw_outgoing_tell_marker = False
            # Event-level actor_class for per-segment clickable rects.
            # Used by the right-click context menu (some actions could
            # gate on class later — invite to a self/party row is a
            # silent no-op, but we don't suppress the menu).
            _ev_actor_class_seg = ev.get("actor_class") or "other"
            for span_text, span_color_class in spans:
                if not span_text:
                    continue
                color = CHAT_SEGMENT_COLORS.get(span_color_class,
                                                CHAT_COLOR_DEFAULT)
                # Detect outgoing-tell marker BEFORE the color decision
                # so a default-class span that contains the '>>' marker
                # gets colored as the tell body, not as gray default.
                # The lua chat module commonly emits outgoing tells as
                # 'Wormfood' (sender span) + '>> y' (default body span
                # containing the marker AND the body text in one span),
                # so the marker check has to happen first to catch it.
                if span_text and ">>" in span_text:
                    _saw_outgoing_tell_marker = True
                # Override 1: default-class span on a chat mode → body color.
                if (span_color_class == "default"
                        and mode in CHAT_MSG_COLOR_BY_MODE):
                    color = CHAT_MSG_COLOR_BY_MODE[mode]
                # Override 1b: default-class span on an outgoing-tell
                # line → mode 12 (tell-sent) body color. Catches both
                # the case where '>>' is in this span itself and the
                # case where '>>' was in an earlier span and the body
                # follows in a separate default span. Either way the
                # marker is True by now.
                elif (span_color_class == "default"
                        and _saw_outgoing_tell_marker
                        and 12 in CHAT_MSG_COLOR_BY_MODE):
                    color = CHAT_MSG_COLOR_BY_MODE[12]
                # Override 2: ch_*-class sender span containing player's
                # own name → fixed self-identity blue. Substring check
                # so it works for both incoming and outgoing tell
                # formats ("Wormfood : ", ">>Wormfood : ", "Wormfood>> ").
                elif (span_color_class is not None
                        and isinstance(span_color_class, str)
                        and span_color_class.startswith("ch_")
                        and player_self_name
                        and player_self_name in span_text):
                    color = CHAT_SELF_NAME_COLOR
                # Override 3 (hover): ch_*-class sender span whose text
                # matches the current mouse-hover name → brighten to
                # CHAT_NAME_HOVER_COLOR. Applied AFTER the self-name
                # override so the hover wins even on your own name —
                # consistent visual feedback that the cell is clickable.
                # Note: span_text for chat_packets-emitted sender spans
                # is the bare name (no delimiter), so direct equality
                # against _chat_name_hover is correct.
                _is_sender_span = (
                    isinstance(span_color_class, str)
                    and span_color_class.startswith("ch_")
                    and span_color_class != "ch_other"
                    and span_color_class != "ch_system"
                )
                if (_is_sender_span and _chat_name_hover
                        and _chat_name_hover == span_text):
                    color = CHAT_NAME_HOVER_COLOR
                span_surf = _chat_render_mixed(span_text, body_font,
                                                cjk_font, color)
                surface.blit(span_surf, (cur_x, ly))
                # Record sender-span rect for click-handler hit-tests.
                # Excludes ch_other and ch_system because those are
                # generic catch-alls (NPC dialog, system messages) where
                # the "sender" isn't a real player you can tell/invite.
                if _is_sender_span and span_text:
                    _chat_clickable_senders.append({
                        "rect": pygame.Rect(cur_x, ly,
                                            span_surf.get_width(),
                                            span_surf.get_height()),
                        "name": span_text,
                        "actor_class": _ev_actor_class_seg,
                    })
                cur_x += span_surf.get_width()
        elif sender_marker:
            # Pass 1: sender in orange.
            #
            # Special-case /yell (mode 11): FFXI yells arrive shaped
            # "Sender[ZoneName]: msg" — the originator's zone is embedded
            # in brackets right after the name. Rendering that entire
            # region in CHAT_SENDER_COLOR makes the zone tag fight the
            # speaker name for visual weight. Split the sender_marker
            # into name + bracket portions and paint the bracket in a
            # paler yellow so the eye reads "name first, zone second".
            #
            # Detection is conservative: only fires when mode==11 AND a
            # '[' appears in the sender_marker AND a matching ']' appears
            # before the trailing ': ' delimiter. If the shape doesn't
            # match (yell without zone tag, or some other channel that
            # happens to contain brackets), fall through to the unified
            # single-color render below.
            #
            # In both sub-branches, we extract the bare sender NAME
            # (no brackets, no trailing colon/space, no '>>') and record
            # its blit rect into _chat_clickable_senders so the mouse
            # handler can left-click → /tell composer or right-click →
            # context menu. The name color also switches to the hover
            # tint when the mouse is currently over this name.
            _ev_actor_class = ev.get("actor_class") or "other"
            _split_rendered = False
            _clickable_name = None
            _clickable_rect = None
            if mode == 11 and "[" in sender_marker and "]" in sender_marker:
                _lb = sender_marker.find("[")
                _rb = sender_marker.find("]", _lb)
                # Ensure both brackets land BEFORE the sender-region
                # boundary (msg_offset). _lb > 0 guarantees we don't
                # color a name that starts with '[' (degenerate case).
                if 0 < _lb < _rb < seg["msg_offset"]:
                    name_part   = sender_marker[:_lb]
                    zone_part   = sender_marker[_lb:_rb + 1]   # includes [ ]
                    tail_part   = sender_marker[_rb + 1:]       # the " : " suffix
                    # Hover state: brighten the name color when this is
                    # the sender under the mouse.
                    _name_clean = name_part.rstrip()
                    _name_color = (CHAT_NAME_HOVER_COLOR
                                   if _chat_name_hover == _name_clean
                                   else seg["sender_color"])
                    name_surf = _chat_render_mixed(name_part, body_font,
                                                   cjk_font, _name_color)
                    zone_surf = _chat_render_mixed(zone_part, body_font,
                                                   cjk_font, CHAT_YELL_ZONE_COLOR)
                    tail_surf = _chat_render_mixed(tail_part, body_font,
                                                   cjk_font, seg["sender_color"])
                    _cx = text_x
                    surface.blit(name_surf, (_cx, ly))
                    # Record the name rect for hit-testing BEFORE we
                    # advance past it. Use the rstripped name (not the
                    # raw name_part which may have trailing whitespace).
                    if _name_clean:
                        _clickable_name = _name_clean
                        _clickable_rect = pygame.Rect(_cx, ly,
                                                      name_surf.get_width(),
                                                      name_surf.get_height())
                    _cx += name_surf.get_width()
                    surface.blit(zone_surf, (_cx, ly)); _cx += zone_surf.get_width()
                    surface.blit(tail_surf, (_cx, ly)); _cx += tail_surf.get_width()
                    _sender_width = _cx - text_x
                    _split_rendered = True
            if not _split_rendered:
                # Unsplit sender: blit the whole sender_marker as one
                # surface (orange) but extract the bare name for the
                # hit-test rect. The name portion is sender_marker
                # minus the trailing delimiter (": ", ">> ", or ") ").
                # We approximate by finding the FIRST occurrence of any
                # of those delimiters and slicing off everything from
                # there. If no delimiter found, the whole marker is the
                # name (defensive — shouldn't happen for properly-split
                # senders, but doesn't break if it does).
                _name_clean = sender_marker
                for _delim in (" : ", ">> ", ") ", "] ", "> "):
                    _di = sender_marker.find(_delim)
                    if _di >= 0:
                        _name_clean = sender_marker[:_di]
                        break
                # '>>' prefix on incoming tells — drop it from the clean
                # name so right-click on ">>Wormfood : " gives just
                # "Wormfood".
                if _name_clean.startswith(">>"):
                    _name_clean = _name_clean[2:]
                # Build the rendered sender surface, switching to hover
                # color if this name is currently hovered. The HOVER
                # paints the WHOLE sender_marker region (including the
                # delimiter) for simplicity — looks fine and lets us
                # reuse the single-surface code path.
                _name_color = (CHAT_NAME_HOVER_COLOR
                               if (_name_clean
                                   and _chat_name_hover == _name_clean)
                               else seg["sender_color"])
                sender_surf = _chat_render_mixed(sender_marker, body_font,
                                                 cjk_font, _name_color)
                surface.blit(sender_surf, (text_x, ly))
                _sender_width = sender_surf.get_width()
                # Hit-test rect covers only the NAME portion of the
                # blitted surface. We compute the name's pixel width
                # by measuring the clean-name string with the same
                # mixed-font measurer used during wrap, so the rect
                # doesn't include the trailing colon/space/etc.
                if _name_clean:
                    _name_w = _chat_measure_mixed(_name_clean,
                                                  body_font, cjk_font)
                    if _name_w > 0:
                        # If we stripped a '>>' prefix, the rect should
                        # start after the '>>' glyph too. Measure that
                        # explicitly so the hit area aligns visually.
                        _prefix_offset = 0
                        if sender_marker.startswith(">>"):
                            _prefix_offset = _chat_measure_mixed(
                                ">>", body_font, cjk_font)
                        _clickable_name = _name_clean
                        _clickable_rect = pygame.Rect(
                            text_x + _prefix_offset, ly,
                            _name_w, sender_surf.get_height())
            if _clickable_name and _clickable_rect is not None:
                _chat_clickable_senders.append({
                    "rect": _clickable_rect,
                    "name": _clickable_name,
                    "actor_class": _ev_actor_class,
                })
            # Pass 2: message body in mode color, positioned right
            # after the sender region. Slice the segment text using
            # msg_offset so we render only the post-sender portion.
            msg_part = seg["text"][seg["msg_offset"]:]
            if msg_part:
                msg_surf = _chat_render_mixed(msg_part, body_font,
                                              cjk_font, seg["color"])
                surface.blit(msg_surf,
                             (text_x + _sender_width, ly))
        else:
            body_surf = _chat_render_mixed(seg["text"], body_font,
                                           cjk_font, seg["color"])
            surface.blit(body_surf, (text_x, ly))

    # Jump-to-bottom badge: shown when active tab is scrolled up.
    _chat_jump_badge_rect = None
    if active_scroll > 0:
        badge_text = f"  ↓ scrolled up {active_scroll} lines  "
        badge_font = _chat_get_font("badge", 11)
        bw, bh = badge_font.size(badge_text)
        bw += 12
        bh += 6
        bx = x + pw - bw - 8
        # Badge sits just above the composer row (or above the panel
        # bottom edge when composer is hidden). Without this offset the
        # badge would overlap the composer's send button.
        by = y + ph - bh - 6 - (composer_h if chat_composer_visible else 0)
        badge_surf = pygame.Surface((bw, bh), pygame.SRCALPHA)
        badge_surf.fill((*CHAT_BADGE_BG, 235))
        surface.blit(badge_surf, (bx, by))
        pygame.draw.rect(surface, CHAT_BORDER_COLOR, (bx, by, bw, bh), 1)
        text_surf = badge_font.render(badge_text, True, CHAT_BADGE_FG)
        surface.blit(text_surf,
                     (bx + (bw - text_surf.get_width()) // 2,
                      by + (bh - text_surf.get_height()) // 2))
        _chat_jump_badge_rect = pygame.Rect(bx, by, bw, bh)

    # ── Scrollbar (right edge of content area) ──────────────────
    # Only renders when there's overflow (more filtered lines than fit
    # in the visible window). Track + thumb rects + max_scroll are
    # captured each frame for the click + drag handlers in the main
    # event loop. Sized to match the checklist scrollbar (8px wide,
    # 28px min thumb) so the two feel like the same widget.
    #
    # IMPORTANT: thumb size and scrollbar max_scroll are computed
    # against `true_total` (the unbounded full-history line count),
    # NOT against `physical_lines_total` (which only counts what we
    # actually rendered before the early-break optimization kicked
    # in). Using physical_lines_total would make the thumb visibly
    # grow as the user scrolls toward the newest message — because
    # less of the buffer needs rendering when near the bottom — and
    # shrink as they scroll up. true_total stays anchored to real
    # chat depth so thumb size only changes when new messages
    # actually arrive.
    global _chat_scrollbar_thumb_rect, _chat_scrollbar_track_rect
    global _chat_scrollbar_max_scroll
    _chat_scrollbar_thumb_rect = None
    _chat_scrollbar_track_rect = None
    _chat_scrollbar_max_scroll = 0
    sb_max_scroll = max(0, true_total - visible_lines)
    if sb_max_scroll > 0 and true_total > visible_lines:
        cb_w = 8
        cb_x = x + pw - cb_w - 4
        # Track spans the message content area, not the full panel —
        # avoids overlapping the header strip or composer row.
        cb_track_y = content_y
        cb_track_h = content_h
        cb_track = pygame.Rect(cb_x, cb_track_y, cb_w, cb_track_h)
        _chat_scrollbar_track_rect = cb_track
        _chat_scrollbar_max_scroll = sb_max_scroll
        pygame.draw.rect(surface, (40, 46, 60),
                         cb_track, border_radius=3)
        # Thumb: proportion of visible / total filtered lines. Uses
        # true_total so size stays stable as the user scrolls (only
        # actual chat growth shrinks it).
        thumb_h = max(28, int(cb_track_h * visible_lines
                              / max(1, true_total)))
        thumb_h = min(thumb_h, cb_track_h)
        # Position: active_scroll=0 → thumb at BOTTOM (newest), so the
        # vertical mapping is inverted relative to checklist. At
        # sb_max_scroll the thumb sits at the top of the track.
        avail = cb_track_h - thumb_h
        # scroll_fraction: 0 at newest (bottom), 1 at oldest (top).
        scroll_frac = (active_scroll / sb_max_scroll) if sb_max_scroll > 0 else 0
        scroll_frac = max(0.0, min(1.0, scroll_frac))
        thumb_y = cb_track_y + int(avail * (1 - scroll_frac))
        cb_thumb = pygame.Rect(cb_x, thumb_y, cb_w, thumb_h)
        _chat_scrollbar_thumb_rect = cb_thumb
        # Hover/drag affordance.
        is_dragging = _chat_scroll_drag is not None
        mouse_p = pygame.mouse.get_pos()
        is_hover = cb_thumb.collidepoint(mouse_p)
        thumb_col = ((180, 200, 230) if (is_dragging or is_hover)
                     else (110, 130, 170))
        pygame.draw.rect(surface, thumb_col, cb_thumb, border_radius=3)

    # ── Composer row (bottom of panel) ──────────────────────────
    # Renders last so it sits above the scrollback's last line in
    # z-order (the scrollback is clipped to content_h which excludes
    # the composer band, so there's no actual overlap, but rendering
    # order keeps things tidy).
    if chat_composer_visible:
        comp_y = y + ph - composer_h
        _draw_chat_composer(surface, x, comp_y, pw,
                            body_font, meta_font, cjk_font)

    # ── Sender-name context menu (right-click popup) ──────────────
    # Drawn LAST so it sits on top of every other panel element —
    # composer, badge, content, header. Skips draw when no menu is
    # open (the common case). Records each menu item's rect into the
    # menu state dict so the next mousedown can hit-test them.
    _chat_draw_name_context_menu(surface)


# =========================================================================
# SECTION: S9_mainloop
# =========================================================================

# ─────────────────────────────────────────────────────────────────────
# Theme engine (Options → Theme: Dark / Light)
# ─────────────────────────────────────────────────────────────────────
# All panel colors live in module-level constants, looked up at render
# time — so theming is "reassign the globals and flush the caches".
# The pristine dark values are snapshotted once here; switching back to
# dark restores them exactly. Light mode swaps the chrome to light
# grays and darkens every text color toward black (hue preserved) so
# channel colors stay distinguishable on the light background.

_current_theme = "dark"

_THEMED_NAMES = [
    # chrome
    "CHAT_BG_COLOR", "CHAT_HEADER_COLOR", "CHAT_BORDER_COLOR",
    "CHAT_TAB_BG_INACTIVE", "CHAT_TAB_BG_ACTIVE",
    "CHAT_TAB_FG_INACTIVE", "CHAT_TAB_FG_ACTIVE",
    "CHAT_TAB_UNREAD_BG", "CHAT_TAB_UNREAD_FG",
    "CHAT_BADGE_BG", "CHAT_BADGE_FG", "CHAT_TIMESTAMP_COLOR",
    "CHAT_COMPOSER_BG", "CHAT_COMPOSER_FIELD_BG",
    "CHAT_COMPOSER_FIELD_FOCUS", "CHAT_COMPOSER_FIELD_BDR",
    "CHAT_COMPOSER_FIELD_BDR_F", "CHAT_COMPOSER_TEXT",
    "CHAT_COMPOSER_PLACEHOLDER", "CHAT_COMPOSER_CHANNEL_FG",
    "CHAT_COMPOSER_ARROW_FG", "CHAT_COMPOSER_ARROW_FG_H",
    "CHAT_COMPOSER_SEND_BG", "CHAT_COMPOSER_SEND_FG",
    # text palettes
    "CHAT_COLOR_DEFAULT", "CHAT_MODE_PALETTE", "CHAT_SEGMENT_COLORS",
    "CHAT_MSG_COLOR_BY_MODE", "CHAT_SENDER_COLOR",
    "CHAT_YELL_ZONE_COLOR", "CHAT_NAME_HOVER_COLOR",
    "CHAT_GEARSWAP_BODY_COLOR", "CHAT_SELF_NAME_COLOR",
    "CHAT_COLOR_BUFF", "CHAT_COLOR_DEBUFF", "COL_SKILLCHAIN",
    "CHAT_TAB_PALETTE", "CHAT_FOCUS_HL",
]

import copy as _copy
_DARK_SNAPSHOT = {n: _copy.deepcopy(globals()[n]) for n in _THEMED_NAMES
                  if n in globals()}


def _darken(c, f=0.52):
    """Scale an RGB(A) color toward black, preserving hue. Used to make
    the dark theme's bright-on-dark text colors readable on light."""
    if isinstance(c, (list, tuple)):
        out = [max(0, min(255, int(v * f))) for v in c[:3]]
        return tuple(out) + tuple(c[3:])
    return c


def _darken_deep(obj, f=0.52):
    if isinstance(obj, dict):
        return {k: _darken_deep(v, f) for k, v in obj.items()}
    if isinstance(obj, list):
        # Could be a list of dicts (CHAT_TAB_PALETTE) or a color.
        if obj and all(isinstance(v, (int, float)) for v in obj):
            return list(_darken(obj, f))
        return [_darken_deep(v, f) for v in obj]
    if isinstance(obj, tuple):
        return _darken(obj, f)
    return obj


# Light chrome. Alphas are 255 (fully opaque) in BOTH themes' applied
# values — translucent chrome would alpha-blend with the transparent
# key pixels of the dithered background and contaminate them into
# opaque near-key garbage. Box translucency is the dither's job.
_LIGHT_CHROME = {
    "CHAT_BG_COLOR":            (243, 244, 247, 255),
    "CHAT_HEADER_COLOR":        (221, 225, 231, 255),
    "CHAT_BORDER_COLOR":        (148, 156, 168),
    "CHAT_TAB_BG_INACTIVE":     (224, 227, 233, 255),
    "CHAT_TAB_BG_ACTIVE":       (196, 207, 224, 255),
    "CHAT_TAB_FG_INACTIVE":     (96, 104, 116),
    "CHAT_TAB_FG_ACTIVE":       (18, 22, 30),
    "CHAT_TAB_UNREAD_BG":       (200, 70, 58),
    "CHAT_TAB_UNREAD_FG":       (255, 255, 255),
    "CHAT_BADGE_BG":            (70, 110, 160),
    "CHAT_BADGE_FG":            (255, 255, 255),
    "CHAT_TIMESTAMP_COLOR":     (128, 134, 142),
    "CHAT_COMPOSER_BG":         (228, 231, 236, 255),
    "CHAT_COMPOSER_FIELD_BG":   (250, 251, 253, 255),
    "CHAT_COMPOSER_FIELD_FOCUS": (255, 255, 255, 255),
    "CHAT_COMPOSER_FIELD_BDR":  (162, 170, 182),
    "CHAT_COMPOSER_FIELD_BDR_F": (60, 120, 170),
    "CHAT_COMPOSER_TEXT":       (24, 28, 34),
    "CHAT_COMPOSER_PLACEHOLDER": (138, 144, 154),
    "CHAT_COMPOSER_CHANNEL_FG": (30, 34, 42),
    "CHAT_COMPOSER_ARROW_FG":   (70, 76, 86),
    "CHAT_COMPOSER_ARROW_FG_H": (190, 120, 20),
    "CHAT_COMPOSER_SEND_BG":    (60, 110, 165),
    "CHAT_COMPOSER_SEND_FG":    (255, 255, 255),
}

_LIGHT_TEXT_OVERRIDES = {
    # Hand-tuned where straight darkening reads poorly on light.
    "CHAT_COLOR_DEFAULT":   (40, 45, 53),
    "CHAT_SENDER_COLOR":    (176, 96, 10),     # darker speaker orange
    "CHAT_NAME_HOVER_COLOR": (120, 70, 0),     # hover: deep amber
    "CHAT_SELF_NAME_COLOR": (30, 90, 160),
    "CHAT_YELL_ZONE_COLOR": (150, 120, 30),
    "CHAT_GEARSWAP_BODY_COLOR": (150, 110, 0),
    "CHAT_COLOR_BUFF":      (0, 110, 150),
    "CHAT_COLOR_DEBUFF":    (170, 30, 100),
    "COL_SKILLCHAIN":       (150, 115, 0),
    "CHAT_FOCUS_HL":        (255, 196, 80),
}


def _force_opaque_chrome():
    """Bump 4-tuple chrome alphas to 255 (see _LIGHT_CHROME note)."""
    for n in ("CHAT_BG_COLOR", "CHAT_HEADER_COLOR", "CHAT_TAB_BG_INACTIVE",
              "CHAT_TAB_BG_ACTIVE", "CHAT_COMPOSER_BG",
              "CHAT_COMPOSER_FIELD_BG", "CHAT_COMPOSER_FIELD_FOCUS"):
        v = globals().get(n)
        if isinstance(v, tuple) and len(v) == 4:
            globals()[n] = v[:3] + (255,)


def _apply_theme(name):
    """Swap the live color constants to `name` ('dark'/'light') and
    flush every cache built from the old colors."""
    global _current_theme
    name = "light" if str(name).lower() == "light" else "dark"
    if name == "dark":
        for k, v in _DARK_SNAPSHOT.items():
            globals()[k] = _copy.deepcopy(v)
    else:
        # Start from pristine dark, darken all text palettes, then lay
        # the chrome + hand-tuned overrides on top.
        for k, v in _DARK_SNAPSHOT.items():
            globals()[k] = _darken_deep(_copy.deepcopy(v))
        for k, v in _LIGHT_CHROME.items():
            if k in globals():
                globals()[k] = v
        for k, v in _LIGHT_TEXT_OVERRIDES.items():
            if k in globals():
                globals()[k] = v
    _force_opaque_chrome()
    _current_theme = name
    _chat_wrap_cache.clear()
    _chat_render_cache.clear()
    _bg_dither_cache.clear()


_apply_theme(setting("chat_theme"))


# ─────────────────────────────────────────────────────────────────────
# Startup + main loop
# ─────────────────────────────────────────────────────────────────────

# Load the global routing config (per-job overrides load when the lua
# JOB heartbeat tells us what job is live).
load_chat_routing()

# Restore composer visibility preference.
chat_composer_visible = bool(setting("chat_composer_visible"))

# The window IS the panel: it always draws at (0, 0).
chat_pos = [0, 0]

# Restore the persisted panel size. The extracted chat-state section
# above re-initialized chat_panel_dims to its [800, 280] default,
# clobbering the value the window was created with — which left the
# panel drawing smaller than the window (black band right/below) and
# broke size persistence (the default got saved back on exit). Sync
# the global to the saved size and re-assert the window to match.
_sd = setting("chat_panel_dims") or [800, 280]
try:
    chat_panel_dims[0] = max(CHAT_PANEL_MIN_W, int(_sd[0]))
    chat_panel_dims[1] = max(CHAT_PANEL_MIN_H, int(_sd[1]))
except (TypeError, ValueError, IndexError):
    chat_panel_dims[0], chat_panel_dims[1] = 800, 280
if screen.get_size() != (chat_panel_dims[0], chat_panel_dims[1]):
    screen = pygame.display.set_mode(
        (chat_panel_dims[0], chat_panel_dims[1]), pygame.NOFRAME)
    _apply_always_on_top(bool(setting("always_on_top")))
    _apply_box_opacity(_box_opacity_pct())
    _apply_no_activate(bool(setting("no_focus_steal")))

_last_seen_main_job = None     # job code from the lua JOB heartbeat
_grip_resize = None            # active resize: {"mode": "both"|"w"|"h"} or None
EDGE_BAND = 5                  # px: right/bottom edge single-axis resize bands
_cur_cursor = None             # last applied system cursor (avoid re-set spam)
_running = True
_composer_focus_prev = False   # keep-game-focus edge tracker
_rt_mtimes = {}                # routing-file mtime watcher state
_clock = pygame.time.Clock()

print(f"[OmniChat] v{OMNICHAT_VERSION} — listening on 127.0.0.1:5113, "
      f"commands -> 5111. Config dir: {SETTINGS_DIR}")


def _persist_window_state():
    """Persist window position + panel dims + composer visibility."""
    pos = _get_window_pos()
    if pos:
        settings["window_pos"] = pos
    settings["chat_panel_dims"] = [int(chat_panel_dims[0]),
                                   int(chat_panel_dims[1])]
    settings["chat_composer_visible"] = bool(chat_composer_visible)
    save_settings()


while _running:

    # ── Receive: chat event stream ──────────────────────────────────
    try:
        while True:
            cdata, _ = sock_chat.recvfrom(32768)
            raw = cdata.decode("utf-8", errors="replace")
            if not raw:
                continue
            # Multibox: chat pins to the configured main character.
            _ok, raw = _mb_gate(raw, for_chat=True)
            if not _ok:
                continue
            # JOB heartbeat from OmniChat.lua: "JOB\t<job>\t<char>".
            # Drives per-job routing reloads + identifies the live
            # character for self-name highlighting and the mb gate.
            if raw.startswith("JOB\t"):
                _jparts = raw.split("\t")
                _job  = (_jparts[1] if len(_jparts) > 1 else "").strip().upper()
                _name = (_jparts[2] if len(_jparts) > 2 else "").strip()
                if _name:
                    current_char_name = _name
                    player_self_name  = _name
                if _job != _last_seen_main_job:
                    _last_seen_main_job = _job
                    try:
                        load_chat_routing_for_job(_job)
                    except Exception as _e:
                        print(f"[OmniChat] job routing reload failed: {_e}")
                continue
            _ingest_chat_packet(raw, "text")
    except BlockingIOError:
        pass
    except Exception as e:
        print(f"[OmniChat] chat drain error: {e!r}")

    # Routing-config live reload: every ~2s, compare mtimes of the
    # global + current per-job routing JSONs; reload a tier when the
    # Filters GUI (or anything else) rewrites it. Cheap stat calls.
    _rt_frame = globals().get("_rt_watch_frame", 0) + 1
    globals()["_rt_watch_frame"] = _rt_frame
    if _rt_frame % 120 == 0:
        try:
            for _rt_job in (None, _last_seen_main_job or None):
                _rt_path = _routing_path_for_job(_rt_job)
                if not _rt_path or not os.path.exists(_rt_path):
                    continue
                _rt_m = os.path.getmtime(_rt_path)
                _rt_key = ("_rt_mtime", _rt_job)
                _rt_prev = _rt_mtimes.get(_rt_key)
                if _rt_prev is None:
                    _rt_mtimes[_rt_key] = _rt_m
                elif _rt_m != _rt_prev:
                    _rt_mtimes[_rt_key] = _rt_m
                    if _rt_job is None:
                        load_chat_routing()
                        print("[OmniChat] routing config reloaded "
                              "(file changed)")
                    else:
                        load_chat_routing_for_job(_rt_job)
                        print(f"[OmniChat] per-job routing reloaded "
                              f"({_rt_job}; file changed)")
        except Exception as _rt_e:
            print(f"[OmniChat] routing watch error: {_rt_e!r}")

    # Composer focus edge-detection (keep-game-focus feature): one
    # central hook instead of chasing every focus/unfocus site. When
    # either composer field gains focus we activate the window so
    # typing works; when both lose it we hand focus back to the game.
    _focus_now = bool(chat_composer_focused or chat_composer_tell_to_focused)
    if setting("no_focus_steal"):
        if _focus_now and not _composer_focus_prev:
            _take_keyboard_focus()
        elif _composer_focus_prev and not _focus_now:
            _return_keyboard_focus()
    _composer_focus_prev = _focus_now

    # ── Events ──────────────────────────────────────────────────────
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            _running = False

        elif event.type == pygame.MOUSEWHEEL:
            # Scroll the active tab. event.y > 0 = wheel up = view
            # earlier messages (positive offset).
            cur = chat_tab_scroll.get(chat_active_tab, 0)
            chat_tab_scroll[chat_active_tab] = max(0, cur + event.y * 3)

        elif event.type == pygame.KEYDOWN:
            # Escape always dismisses the name context menu; don't
            # consume — Escape may also blur the composer below.
            if (event.key == pygame.K_ESCAPE
                    and _chat_name_context_menu is not None):
                _chat_close_name_context_menu()

            # Composer keyboard input — highest priority when focused.
            if chat_composer_focused or chat_composer_tell_to_focused:
                if _chat_composer_handle_keydown(event):
                    continue

            # Quit hotkey: Ctrl+Q (no title bar = no [X] button).
            if (event.key == pygame.K_q
                    and (event.mod & pygame.KMOD_CTRL)):
                _running = False

        elif event.type == pygame.TEXTINPUT:
            if chat_composer_focused or chat_composer_tell_to_focused:
                _chat_composer_handle_textinput(event.text)

        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            mx, my = event.pos

            # Options popup: if open, route the click through it first
            # (a row action consumes; outside dismisses + falls through).
            if _options_popup_open:
                if dispatch_options_popup_click(mx, my):
                    continue

            # Tab right-click popup: if open, route the click through
            # it first (select = consume; outside = dismiss + fall
            # through).
            if _chat_tab_rclick_tab is not None:
                if dispatch_chat_tab_rclick_popup_click(mx, my):
                    continue

            # Corner resize grip — checked BEFORE all functional UI.
            # The grip's 14px square overlaps the composer's send
            # button at the bottom-right; with the composer checked
            # first the send button swallowed every grip click and
            # the panel could never be resized. The grip is the
            # dedicated resize affordance, so it wins its corner.
            _w, _h = chat_panel_size()
            if (_w - RESIZE_GRIP) <= mx < _w and (_h - RESIZE_GRIP) <= my < _h:
                _grip_resize = {"mode": "both"}
                continue

            # Top / left edge bands — these resize toward a FIXED
            # opposite edge, so the window origin moves with the drag.
            # That needs screen coordinates (Windows-only; on other
            # platforms these bands are inert and the right/bottom
            # edges + corner still work). The opposite edge is
            # captured once at grab time so rounding can't walk it.
            if my < EDGE_BAND or mx < EDGE_BAND:
                _wp = _get_window_pos()
                if _wp is not None:
                    if my < EDGE_BAND and mx >= EDGE_BAND:
                        _grip_resize = {"mode": "top",
                                        "fixed_bottom": _wp[1] + _h,
                                        "win_x": _wp[0]}
                        continue
                    if mx < EDGE_BAND and my >= EDGE_BAND:
                        _grip_resize = {"mode": "left",
                                        "fixed_right": _wp[0] + _w,
                                        "win_y": _wp[1]}
                        continue

            # Tab strip scroll arrows (when the strip overflows).
            if _chat_tab_arrow_rects:
                _arrow_step = 120
                _la = _chat_tab_arrow_rects.get("left")
                _ra = _chat_tab_arrow_rects.get("right")
                if _la is not None and _la.collidepoint(mx, my):
                    _chat_tab_hscroll = max(0, _chat_tab_hscroll - _arrow_step)
                    continue
                if _ra is not None and _ra.collidepoint(mx, my):
                    _chat_tab_hscroll = _chat_tab_hscroll + _arrow_step
                    continue

            # Tab click: switch active tab, zero its unread count.
            if chat_tab_rects:
                _tab_hit = None
                for _rect, _tidx in chat_tab_rects:
                    if _rect.collidepoint(mx, my):
                        _tab_hit = _tidx
                        break
                if _tab_hit is not None:
                    chat_active_tab = _tab_hit
                    chat_tab_unread[_tab_hit] = 0
                    continue

            # Name context menu: when open, every click routes through
            # it (select or dismiss) before anything below.
            if _chat_handle_name_context_click(1, mx, my):
                continue

            # Sender-name left-click → set up /tell composer.
            if _chat_handle_name_click(1, mx, my):
                continue

            # Scrollbar thumb: start drag. Track click = page jump.
            if _chat_scrollbar_thumb_rect is not None \
               and _chat_scrollbar_thumb_rect.collidepoint(mx, my):
                _chat_scroll_drag = {
                    "origin_mouse_y": my,
                    "origin_scroll":  chat_tab_scroll.get(chat_active_tab, 0),
                    "track_top":      _chat_scrollbar_track_rect.top,
                    "track_h":        _chat_scrollbar_track_rect.h,
                    "thumb_h":        _chat_scrollbar_thumb_rect.h,
                    "max_scroll":     _chat_scrollbar_max_scroll,
                }
                continue
            if _chat_scrollbar_track_rect is not None \
               and _chat_scrollbar_track_rect.collidepoint(mx, my):
                cur = chat_tab_scroll.get(chat_active_tab, 0)
                step = max(3, int(_chat_scrollbar_thumb_rect.h
                                  * _chat_scrollbar_max_scroll
                                  / max(1, _chat_scrollbar_track_rect.h)))
                if my < _chat_scrollbar_thumb_rect.top:
                    new = cur + step
                else:
                    new = cur - step
                new = max(0, min(new, _chat_scrollbar_max_scroll))
                chat_tab_scroll[chat_active_tab] = new
                continue

            # Jump-to-bottom badge.
            if _chat_jump_badge_rect is not None \
               and _chat_jump_badge_rect.collidepoint(mx, my):
                chat_tab_scroll[chat_active_tab] = 0
                continue

            # Clear-tab button: remove events matching the active
            # tab's filter, leaving other tabs intact.
            if _chat_clear_tab_button_rect is not None \
               and _chat_clear_tab_button_rect.collidepoint(mx, my):
                try:
                    _filt = chat_tab_filters[chat_active_tab]
                    def _matches(ev):
                        try:
                            return bool(_filt(ev))
                        except Exception:
                            return False
                    _kept = _collections.deque(
                        (ev for ev in chat_events if not _matches(ev)),
                        maxlen=chat_events.maxlen)
                    chat_events.clear()
                    chat_events.extend(_kept)
                    chat_tab_scroll[chat_active_tab] = 0
                    _chat_tab_line_total[chat_active_tab] = None
                    if chat_active_tab in chat_tab_unread:
                        chat_tab_unread[chat_active_tab] = 0
                except Exception as _e:
                    print(f"[OmniChat] clear tab failed: {_e}")
                continue

            # Clear-all button: wipe the entire chat buffer.
            if _chat_clear_all_button_rect is not None \
               and _chat_clear_all_button_rect.collidepoint(mx, my):
                try:
                    chat_events.clear()
                    for _ti in range(len(chat_tab_names)):
                        chat_tab_scroll[_ti] = 0
                        _chat_tab_line_total[_ti] = None
                        if _ti in chat_tab_unread:
                            chat_tab_unread[_ti] = 0
                except Exception as _e:
                    print(f"[OmniChat] clear all failed: {_e}")
                continue

            # Show-all-tabs button: clear the hidden-tab list.
            if _chat_show_all_button_rect is not None \
               and _chat_show_all_button_rect.collidepoint(mx, my):
                _was_hidden = list(settings.get("hidden_chat_tabs") or [])
                if _was_hidden:
                    set_setting("hidden_chat_tabs", [])
                    print(f"[OmniChat] show all tabs: cleared hidden "
                          f"list (was {_was_hidden})")
                continue

            # Routing settings (gear) button → launch the routing GUI.
            if _chat_settings_button_rect is not None \
               and _chat_settings_button_rect.collidepoint(mx, my):
                _launch_routing_gui()
                continue

            # Options button → toggle the options popup.
            if _chat_options_button_rect is not None \
               and _chat_options_button_rect.collidepoint(mx, my):
                _options_popup_open = not _options_popup_open
                continue

            # Composer clicks — channel arrows, send, field focus.
            if chat_composer_visible:
                # { } auto-translate button: wrap the message for AT
                # sending. With text: the whole message becomes
                # {message} (click again to unwrap). Empty: insert {}
                # with the cursor parked inside so you type the phrase
                # directly. Focuses the input either way so typing
                # continues without another click.
                if _chat_composer_rect_at is not None \
                        and _chat_composer_rect_at.collidepoint(mx, my):
                    _t = chat_composer_text
                    if not _t:
                        chat_composer_text = "{}"
                        chat_composer_cursor = 1
                    elif _t.startswith("{") and _t.endswith("}") \
                            and len(_t) >= 2:
                        # Toggle off: unwrap.
                        chat_composer_text = _t[1:-1]
                        chat_composer_cursor = len(chat_composer_text)
                    else:
                        chat_composer_text = "{" + _t + "}"
                        chat_composer_cursor = len(chat_composer_text)
                    chat_composer_focused = True
                    chat_composer_tell_to_focused = False
                    continue
                if (_chat_composer_rect_arrow_r is not None
                        and _chat_composer_rect_arrow_r.collidepoint(mx, my)) \
                   or (_chat_composer_rect_channel is not None
                        and _chat_composer_rect_channel.collidepoint(mx, my)):
                    chat_composer_channel = (
                        (chat_composer_channel + 1) % len(CHAT_COMPOSER_CHANNELS))
                    if chat_composer_channel != 1:
                        chat_composer_tell_to_focused = False
                    continue
                if _chat_composer_rect_arrow_l is not None \
                        and _chat_composer_rect_arrow_l.collidepoint(mx, my):
                    chat_composer_channel = (
                        (chat_composer_channel - 1) % len(CHAT_COMPOSER_CHANNELS))
                    if chat_composer_channel != 1:
                        chat_composer_tell_to_focused = False
                    continue
                if _chat_composer_rect_send is not None \
                        and _chat_composer_rect_send.collidepoint(mx, my):
                    _chat_composer_send()
                    continue
                if _chat_composer_rect_tell_to is not None \
                        and _chat_composer_rect_tell_to.collidepoint(mx, my):
                    chat_composer_tell_to_focused = True
                    chat_composer_focused = False
                    continue
                if _chat_composer_rect_input is not None \
                        and _chat_composer_rect_input.collidepoint(mx, my):
                    chat_composer_focused = True
                    chat_composer_tell_to_focused = False
                    continue
                # Any other click unfocuses the composer fields.
                if chat_composer_focused or chat_composer_tell_to_focused:
                    chat_composer_focused = False
                    chat_composer_tell_to_focused = False
                    # fall through — the click may also start a drag

            # Edge resize bands: the outer few pixels of the right and
            # bottom edges resize ONE axis at a time (width-only /
            # height-only), complementing the corner grip's both-axes
            # drag. Checked late so the scrollbar (right side) and
            # composer buttons keep priority over their interiors —
            # only the outermost EDGE_BAND pixels resize.
            if mx >= _w - EDGE_BAND and my >= 18:
                _grip_resize = {"mode": "w"}
                continue
            if my >= _h - EDGE_BAND:
                _grip_resize = {"mode": "h"}
                continue

            # Header strip (top 18px, no functional rect hit) → native
            # OS window drag. Anywhere else: Shift+drag also moves, so
            # the window can be grabbed without aiming for the header.
            _is_header = (EDGE_BAND <= my < 18)
            _shift_held = bool(pygame.key.get_mods() & pygame.KMOD_SHIFT)
            if _is_header or _shift_held:
                _begin_native_window_drag()
                # The OS owns the mouse for the duration of the drag;
                # persist the new position when the next event arrives.
                _persist_window_state()
                continue

        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 3:
            mx, my = event.pos

            # Open name-context menu routing (select item / dismiss).
            if _chat_handle_name_context_click(3, mx, my):
                continue

            # Tab right-click → "Hide tab" popup at the click position.
            if chat_tab_rects:
                _hit_tab = None
                for _rect, _tidx in chat_tab_rects:
                    if _rect.collidepoint(mx, my):
                        _hit_tab = _tidx
                        break
                if _hit_tab is not None:
                    _chat_tab_rclick_tab    = _hit_tab
                    _chat_tab_rclick_anchor = (mx, my)
                    continue

            # Sender-name right-click → context menu (Tell / Invite).
            if _chat_handle_name_click(3, mx, my):
                continue

            # Header strip right-click → dump routing diagnostic.
            if my < 18:
                dump_chat_routing_state()
                continue

        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if _chat_scroll_drag is not None:
                _chat_scroll_drag = None
                continue
            if _grip_resize is not None:
                _grip_resize = None
                # Re-assert topmost + the box-opacity color key
                # (set_mode can recreate the window/styles) and
                # persist the final size.
                _apply_always_on_top(bool(setting("always_on_top")))
                _apply_box_opacity(_box_opacity_pct())
                _apply_no_activate(bool(setting("no_focus_steal")))
                _persist_window_state()
                continue

        elif (event.type == pygame.MOUSEMOTION
              and _chat_scroll_drag is not None):
            # Scrollbar thumb drag. chat_tab_scroll counts lines back
            # from the newest (0 = pinned to bottom), so mouse-down
            # decreases (toward newest), mouse-up increases (older).
            drag = _chat_scroll_drag
            _, my = event.pos
            dy = my - drag["origin_mouse_y"]
            usable = max(1, drag["track_h"] - drag["thumb_h"])
            max_scroll = drag.get("max_scroll", 0)
            if max_scroll > 0:
                new_scroll = drag["origin_scroll"] - int(dy * max_scroll / usable)
                new_scroll = max(0, min(new_scroll, max_scroll))
                chat_tab_scroll[chat_active_tab] = new_scroll

        elif (event.type == pygame.MOUSEMOTION
              and _grip_resize is not None):
            # Live resize: the window tracks the mouse. Text size is
            # unchanged — more/fewer lines become visible. Width and
            # height adjust independently per the grab mode: corner
            # drags both, right edge width only, bottom edge height
            # only.
            mx, my = event.pos
            _mode = _grip_resize.get("mode", "both")
            new_w = chat_panel_dims[0]
            new_h = chat_panel_dims[1]
            _move_to = None
            if _mode in ("both", "w"):
                new_w = max(CHAT_PANEL_MIN_W, mx)
            if _mode in ("both", "h"):
                new_h = max(CHAT_PANEL_MIN_H, my)
            if _mode == "left":
                _cur = _get_cursor_screen_pos()
                if _cur is not None:
                    fixed_right = _grip_resize["fixed_right"]
                    new_w = max(CHAT_PANEL_MIN_W, fixed_right - _cur[0])
                    _move_to = (fixed_right - new_w,
                                _grip_resize["win_y"])
            elif _mode == "top":
                _cur = _get_cursor_screen_pos()
                if _cur is not None:
                    fixed_bottom = _grip_resize["fixed_bottom"]
                    new_h = max(CHAT_PANEL_MIN_H, fixed_bottom - _cur[1])
                    _move_to = (_grip_resize["win_x"],
                                fixed_bottom - new_h)
            if (new_w, new_h) != (chat_panel_dims[0], chat_panel_dims[1]):
                chat_panel_dims[0] = new_w
                chat_panel_dims[1] = new_h
                screen = pygame.display.set_mode(
                    (new_w, new_h), pygame.NOFRAME)
                if _move_to is not None:
                    _set_window_pos(*_move_to)

        elif event.type == pygame.MOUSEMOTION:
            # Hover state for clickable sender names.
            try:
                _chat_update_name_hover(*event.pos)
            except Exception:
                pass
            # Resize-affordance cursors: diagonal over the corner grip,
            # horizontal over the right edge band, vertical over the
            # bottom edge band, arrow elsewhere.
            try:
                _hx, _hy = event.pos
                _w, _h = chat_panel_size()
                if (_w - RESIZE_GRIP) <= _hx and (_h - RESIZE_GRIP) <= _hy:
                    _want = pygame.SYSTEM_CURSOR_SIZENWSE
                elif (_hx >= _w - EDGE_BAND and _hy >= 18) \
                        or _hx < EDGE_BAND:
                    _want = pygame.SYSTEM_CURSOR_SIZEWE
                elif _hy >= _h - EDGE_BAND or _hy < EDGE_BAND:
                    _want = pygame.SYSTEM_CURSOR_SIZENS
                else:
                    _want = pygame.SYSTEM_CURSOR_ARROW
                if _want != globals().get("_cur_cursor"):
                    pygame.mouse.set_cursor(_want)
                    _cur_cursor = _want
            except Exception:
                pass

    # ── Draw ────────────────────────────────────────────────────────
    screen.fill(_window_clear_color())
    try:
        draw_chat_panel(screen, 0, 0, False)
    except Exception as e:
        # A render error must not kill the overlay; show it instead.
        import traceback
        traceback.print_exc()
        _err_font = get_font("Consolas", 12)
        screen.blit(_err_font.render(
            f"render error: {e!r}", True, (255, 120, 120)), (8, 24))
    _w, _h = chat_panel_size()
    draw_resize_grip(screen, _w, _h)
    draw_chat_tab_rclick_popup(screen)
    draw_options_popup(screen)

    pygame.display.flip()
    _clock.tick(60)

    # Headless test hook: exit after N frames when OMNICHAT_TEST_FRAMES
    # is set (used by automated smoke tests; harmless otherwise).
    _tf = os.environ.get("OMNICHAT_TEST_FRAMES")
    if _tf:
        _test_frames_left = globals().get("_test_frames_left", int(_tf)) - 1
        if _test_frames_left <= 0:
            _running = False
            _shot = os.environ.get("OMNICHAT_TEST_SHOT")
            if _shot:
                try:
                    pygame.image.save(screen, _shot)
                except Exception as _e:
                    print(f"[OmniChat] test shot failed: {_e}")

# ── Shutdown ────────────────────────────────────────────────────────
_persist_window_state()
pygame.quit()
print("[OmniChat] exited cleanly.")