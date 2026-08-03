"""
================================================================================
  S-Keystrokes v2.1  —  True-Glass PyQt6 Gaming HUD (3-window edition)
================================================================================

WHAT CHANGED FROM v2.0 (bug fix + optimization pass)
--------------------------------------------------------------------------------
1. BUG FIX — dragging was implemented by monkey-patching an *instance*
   attribute (`widget.mousePressEvent = self._drag_start`). That pattern is
   unreliable in PyQt6: Qt's C++ side dispatches virtual event handlers by
   looking them up on the class, not the instance, so it can silently fail
   to fire depending on Qt/platform version — which is why dragging felt
   broken/inconsistent. Fixed by using a real subclass (`DraggableGlassWindow`)
   with proper `mousePressEvent` / `mouseMoveEvent` / `mouseReleaseEvent`
   overrides, which Qt always calls correctly. Also added the missing
   `mouseReleaseEvent` reset (v2.0 never cleared the drag offset).

2. PERFORMANCE — style strings for each key tile are now built ONCE per
   scale change and cached (idle + active variants), instead of being
   re-built with an f-string on every single press/release. Also merged
   the separate 16ms "FPS calc" and 200ms "label refresh" timers into one
   lean, shared `StatsHub` that drives all three windows from a single
   QTimer pair instead of duplicating timers per-window.

3. NEW — split into 3 independent overlay windows, all frameless,
   all truly transparent (WA_TranslucentBackground), all always-on-top,
   and each independently movable by dragging it:
     - Window 1: Keys HUD  (W A S D . LMB RMB . SPACE)
     - Window 2: FPS HUD   (color-coded: red / orange / green)
     - Window 3: CPS HUD   (color-coded the same way, for consistency)

4. FPS COLOR RULE (as requested):
     FPS <= 15   -> RED     (struggling)
     FPS <= 30   -> ORANGE  (okay)
     FPS >  30   -> GREEN   (smooth / 60fps+ territory)
   The CPS window uses the same red/orange/green banding so both readouts
   are visually consistent at a glance.

--------------------------------------------------------------------------------
INSTALLATION
--------------------------------------------------------------------------------
    pip install PyQt6 pynput

--------------------------------------------------------------------------------
RUNNING
--------------------------------------------------------------------------------
    python s_keystrokes.py

--------------------------------------------------------------------------------
NOTES
--------------------------------------------------------------------------------
- Display-only overlay. It never logs, saves, or transmits keystrokes — it
  only mirrors the live pressed/released state of W, A, S, D, SPACE, LMB, RMB,
  and shows live FPS/CPS numbers, purely for visual feedback while gaming.
- pynput's keyboard/mouse listeners run on background threads. Their
  callbacks never touch a widget directly — they emit a Qt signal, and Qt
  marshals that onto the main/UI thread automatically (queued connection),
  which is what keeps this thread-safe and lag-free.
- Drag any of the 3 windows by left-click-dragging anywhere on its glass
  panel. Right-click the Keys HUD (or its gear icon) for the Small/Medium/
  Large scale menu -- resizing there also rescales the FPS/CPS windows.
- Esc or the Keys HUD's close button closes all three windows and stops
  the listeners.
================================================================================
"""

import sys
import time
import collections

from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import QFont, QCursor
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QFrame, QHBoxLayout, QVBoxLayout,
    QMenu, QPushButton,
)

from pynput import keyboard, mouse

# ------------------------------------------------------------------------------
# Glass color palette (rgba alpha is real per-pixel alpha, not simulated)
# ------------------------------------------------------------------------------
PANEL_BG          = "rgba(18, 20, 26, 140)"
PANEL_BORDER      = "rgba(255, 255, 255, 30)"
KEY_BG_IDLE       = "rgba(28, 31, 40, 150)"
KEY_BORDER_IDLE   = "rgba(255, 255, 255, 35)"
KEY_TEXT_IDLE     = "rgba(200, 205, 220, 220)"

ACCENT            = "#5ef7ff"
KEY_BG_ACTIVE     = "rgba(30, 90, 94, 190)"
KEY_BORDER_ACTIVE = ACCENT
KEY_TEXT_ACTIVE   = ACCENT

MUTED_TEXT        = "rgba(150, 155, 175, 200)"

# performance color bands (shared by FPS + CPS readouts)
COLOR_RED    = "#ff4d4d"
COLOR_ORANGE = "#ffb020"
COLOR_GREEN  = "#3ce685"


def band_color(value: float) -> str:
    """Shared red/orange/green banding used by both the FPS and CPS HUDs.
    <=15 -> red (struggling), <=30 -> orange (okay), >30 -> green (smooth)."""
    if value <= 15:
        return COLOR_RED
    if value <= 30:
        return COLOR_ORANGE
    return COLOR_GREEN


# ------------------------------------------------------------------------------
# Scale presets - compact by design.
# ------------------------------------------------------------------------------
SCALE_PRESETS = {
    "Small":  {"btn": 30, "font": 10, "gap": 3, "radius": 8,  "space_h": 20, "stat_font": 13},
    "Medium": {"btn": 42, "font": 12, "gap": 4, "radius": 11, "space_h": 26, "stat_font": 16},
    "Large":  {"btn": 56, "font": 15, "gap": 5, "radius": 14, "space_h": 34, "stat_font": 20},
}


# ==================================================================
# Thread-safe bridge: pynput callbacks (background threads) -> Qt signals
# (auto-delivered on the main/UI thread). This is the ONLY thing pynput
# callbacks are allowed to touch - no widget access from worker threads.
# ==================================================================
class InputBridge(QObject):
    key_changed = pyqtSignal(str, bool)
    mouse_changed = pyqtSignal(str, bool)
    left_click = pyqtSignal()


# ==================================================================
# Base class for every overlay window: frameless, truly transparent,
# always-on-top, and properly draggable (the actual bug fix from v2.0).
# ==================================================================
class DraggableGlassWindow(QWidget):
    def __init__(self):
        super().__init__()
        self._drag_anchor = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        # Real per-pixel ARGB surface -> no grey/black backing frame.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setStyleSheet("background: transparent;")

    # Proper virtual-method overrides (correctly recognized by Qt,
    # unlike assigning to the instance attribute directly).
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_anchor = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_anchor is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_anchor)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_anchor = None
        event.accept()


class KeyTile(QLabel):
    """A single rounded, glass-style key/mouse indicator.

    Style strings are pre-built once per configure() call and cached, so
    toggling active/idle at input speed is just a cached setStyleSheet()
    swap - no string formatting on the hot path.
    """

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._active = False
        self._style_idle = ""
        self._style_active = ""

    def configure(self, size, font_px, radius):
        self.setFixedSize(*size)
        self.setFont(QFont("Segoe UI", font_px, QFont.Weight.Bold))
        self._style_idle = (
            f"QLabel {{ background-color: {KEY_BG_IDLE}; "
            f"border: 1px solid {KEY_BORDER_IDLE}; border-radius: {radius}px; "
            f"color: {KEY_TEXT_IDLE}; }}"
        )
        self._style_active = (
            f"QLabel {{ background-color: {KEY_BG_ACTIVE}; "
            f"border: 2px solid {KEY_BORDER_ACTIVE}; border-radius: {radius}px; "
            f"color: {KEY_TEXT_ACTIVE}; }}"
        )
        self.setStyleSheet(self._style_active if self._active else self._style_idle)

    def set_active(self, active: bool):
        if active == self._active:
            return
        self._active = active
        self.setStyleSheet(self._style_active if active else self._style_idle)


class IconButton(QPushButton):
    """Tiny transparent icon button (gear / close)."""

    def __init__(self, text, hover_bg, parent=None):
        super().__init__(text, parent)
        self.setFixedSize(18, 18)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent; color: {MUTED_TEXT};
                border: none; font-size: 11px;
            }}
            QPushButton:hover {{ background-color: {hover_bg}; border-radius: 4px; }}
        """)


# ==================================================================
# Window 1 - the WASD / mouse / SPACE key HUD
# ==================================================================
class KeysHUD(DraggableGlassWindow):
    def __init__(self, on_close):
        super().__init__()
        self._on_close_cb = on_close
        self.current_scale = "Small"
        self._build_ui()
        self.apply_scale(self.current_scale)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.panel = QFrame(self)
        self.panel.setObjectName("panel")
        self.panel.setStyleSheet(f"""
            QFrame#panel {{ background-color: {PANEL_BG}; border: 1px solid {PANEL_BORDER};
                             border-radius: 14px; }}
        """)
        outer.addWidget(self.panel)

        v = QVBoxLayout(self.panel)
        v.setContentsMargins(6, 4, 6, 6)
        v.setSpacing(3)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("S - KEYS")
        title.setStyleSheet(f"color: {MUTED_TEXT}; background: transparent;")
        title.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch()

        self.gear_btn = IconButton("\u2699", "rgba(255,255,255,25)")
        self.gear_btn.clicked.connect(self._show_scale_menu)
        header.addWidget(self.gear_btn)

        self.close_btn = IconButton("\u2715", "rgba(255,60,60,60)")
        self.close_btn.clicked.connect(self._request_close)
        header.addWidget(self.close_btn)

        header_w = QWidget()
        header_w.setLayout(header)
        header_w.setStyleSheet("background: transparent;")
        header_w.setFixedHeight(18)
        v.addWidget(header_w)

        self.row1 = self._centered_row(v)
        self.row2 = self._centered_row(v)
        self.row3 = self._centered_row(v)
        self.row4 = self._centered_row(v)

        self.tile_w = KeyTile("W")
        self.tile_a = KeyTile("A")
        self.tile_s = KeyTile("S")
        self.tile_d = KeyTile("D")
        self.tile_lmb = KeyTile("LMB")
        self.tile_rmb = KeyTile("RMB")
        self.tile_space = KeyTile("SPACE")

        self.row1.addWidget(self.tile_w)
        for t in (self.tile_a, self.tile_s, self.tile_d):
            self.row2.addWidget(t)
        for t in (self.tile_lmb, self.tile_rmb):
            self.row3.addWidget(t)
        self.row4.addWidget(self.tile_space)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_scale_menu)

    @staticmethod
    def _centered_row(parent_layout):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        inner = QWidget()
        inner.setLayout(row)
        inner.setStyleSheet("background: transparent;")

        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.addStretch()
        wrap.addWidget(inner)
        wrap.addStretch()
        wrap_w = QWidget()
        wrap_w.setLayout(wrap)
        wrap_w.setStyleSheet("background: transparent;")

        parent_layout.addWidget(wrap_w)
        return row

    def _show_scale_menu(self, *_):
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background-color: rgb(24, 26, 33); color: {KEY_TEXT_IDLE};
                     border: 1px solid {PANEL_BORDER}; border-radius: 8px; padding: 4px; }}
            QMenu::item {{ padding: 4px 18px; border-radius: 6px; }}
            QMenu::item:selected {{ background-color: rgba(30, 90, 94, 200); color: {ACCENT}; }}
        """)
        for name in SCALE_PRESETS:
            action = menu.addAction(("- " if name == self.current_scale else "   ") + name)
            action.triggered.connect(lambda checked, n=name: self.apply_scale(n))
        menu.exec(QCursor.pos())

    def apply_scale(self, name):
        preset = SCALE_PRESETS[name]
        self.current_scale = name
        btn, gap, radius = preset["btn"], preset["gap"], preset["radius"]
        font_px, space_h = preset["font"], preset["space_h"]

        self.tile_w.configure((btn, btn), font_px, radius)
        for t in (self.tile_a, self.tile_s, self.tile_d):
            t.configure((btn, btn), font_px, radius)

        mw, mh = int(btn * 1.15), int(btn * 0.72)
        small_font = max(8, font_px - 3)
        for t in (self.tile_lmb, self.tile_rmb):
            t.configure((mw, mh), small_font, radius)

        space_w = btn * 3 + gap * 2
        self.tile_space.configure((space_w, space_h), small_font, radius)

        for row in (self.row1, self.row2, self.row3, self.row4):
            row.setSpacing(gap)

        self.adjustSize()

    def set_key(self, name, pressed):
        {
            "w": self.tile_w, "a": self.tile_a, "s": self.tile_s,
            "d": self.tile_d, "space": self.tile_space,
        }[name].set_active(pressed)

    def set_mouse(self, name, pressed):
        (self.tile_lmb if name == "left" else self.tile_rmb).set_active(pressed)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._request_close()
        super().keyPressEvent(event)

    def _request_close(self):
        self._on_close_cb()


# ==================================================================
# Windows 2 & 3 - small standalone color-coded readouts (FPS / CPS)
# ==================================================================
class StatHUD(DraggableGlassWindow):
    """A tiny glass pill that shows one live number, color-banded
    red/orange/green. Used for both the FPS and CPS windows."""

    def __init__(self, prefix):
        super().__init__()
        self._prefix = prefix

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.panel = QFrame(self)
        self.panel.setStyleSheet(f"""
            QFrame {{ background-color: {PANEL_BG}; border: 1px solid {PANEL_BORDER};
                      border-radius: 12px; }}
        """)
        outer.addWidget(self.panel)

        v = QVBoxLayout(self.panel)
        v.setContentsMargins(10, 4, 10, 5)
        self.label = QLabel(f"{prefix} 0")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet(f"color: {COLOR_GREEN}; background: transparent;")
        v.addWidget(self.label)

    def apply_scale(self, name):
        preset = SCALE_PRESETS[name]
        self.label.setFont(QFont("Segoe UI", preset["stat_font"], QFont.Weight.Bold))
        self.adjustSize()

    def update_value(self, value: float, display_text: str):
        self.label.setText(display_text)
        color = band_color(value)
        self.label.setStyleSheet(f"color: {color}; background: transparent;")


# ==================================================================
# Central stats/timer hub - ONE pair of timers drives FPS calc + CPS
# trimming + pushing text/color into both stat windows. Consolidating
# timers (vs. one set per window) is the main runtime-cost optimization
# requested: fewer QTimer callbacks firing per second overall.
# ==================================================================
class StatsHub(QObject):
    def __init__(self, fps_window: StatHUD, cps_window: StatHUD):
        super().__init__()
        self.fps_window = fps_window
        self.cps_window = cps_window

        self._click_times = collections.deque()

        self._last_tick = time.perf_counter()
        self._fps_value = 0.0
        self._fps_alpha = 0.15  # EMA smoothing

        # High-rate timer: only does cheap arithmetic (frame delta -> EMA),
        # no widget writes here at all -> negligible overhead even at 60Hz.
        self.fps_calc_timer = QTimer(self)
        self.fps_calc_timer.timeout.connect(self._calc_fps)
        self.fps_calc_timer.start(16)

        # Low-rate timer: the only place that actually touches widgets
        # (label text + color), kept at 5Hz since eyes can't read faster.
        self.label_timer = QTimer(self)
        self.label_timer.timeout.connect(self._refresh_labels)
        self.label_timer.start(200)

    def register_click(self):
        self._click_times.append(time.perf_counter())

    def _calc_fps(self):
        now = time.perf_counter()
        dt = now - self._last_tick
        self._last_tick = now
        if dt > 0:
            instant = 1.0 / dt
            self._fps_value = self._fps_alpha * instant + (1 - self._fps_alpha) * self._fps_value

    def _refresh_labels(self):
        now = time.perf_counter()
        while self._click_times and now - self._click_times[0] > 1.0:
            self._click_times.popleft()
        cps = len(self._click_times)

        fps_int = int(round(self._fps_value))
        self.fps_window.update_value(fps_int, f"FPS {fps_int}")
        self.cps_window.update_value(cps, f"CPS {cps}")

    def stop(self):
        self.fps_calc_timer.stop()
        self.label_timer.stop()


# ==================================================================
# App wiring
# ==================================================================
class App:
    def __init__(self):
        self.qapp = QApplication(sys.argv)

        self.bridge = InputBridge()
        self.bridge.key_changed.connect(self._on_key_changed)
        self.bridge.mouse_changed.connect(self._on_mouse_changed)
        self.bridge.left_click.connect(self._on_left_click)

        self.keys_hud = KeysHUD(on_close=self.shutdown)
        self.fps_hud = StatHUD("FPS")
        self.cps_hud = StatHUD("CPS")
        for w in (self.fps_hud, self.cps_hud):
            w.apply_scale(self.keys_hud.current_scale)

        # keep the two stat windows rescaling together with the keys HUD
        orig_apply_scale = self.keys_hud.apply_scale
        def apply_scale_all(name):
            orig_apply_scale(name)
            self.fps_hud.apply_scale(name)
            self.cps_hud.apply_scale(name)
        self.keys_hud.apply_scale = apply_scale_all

        self.stats = StatsHub(self.fps_hud, self.cps_hud)

        self._kb_listener = keyboard.Listener(
            on_press=self._kb_press, on_release=self._kb_release
        )
        self._mouse_listener = mouse.Listener(on_click=self._mouse_click)
        self._kb_listener.start()
        self._mouse_listener.start()

    # ---- pynput callbacks (background threads): signal-only, no widget access
    def _kb_press(self, key):
        name = self._map_key(key)
        if name:
            self.bridge.key_changed.emit(name, True)

    def _kb_release(self, key):
        name = self._map_key(key)
        if name:
            self.bridge.key_changed.emit(name, False)

    @staticmethod
    def _map_key(key):
        try:
            ch = key.char.lower() if hasattr(key, "char") and key.char else None
        except Exception:
            ch = None
        if ch in ("w", "a", "s", "d"):
            return ch
        if key == keyboard.Key.space:
            return "space"
        return None

    def _mouse_click(self, x, y, button, pressed):
        if button == mouse.Button.left:
            self.bridge.mouse_changed.emit("left", pressed)
            if pressed:
                self.bridge.left_click.emit()
        elif button == mouse.Button.right:
            self.bridge.mouse_changed.emit("right", pressed)

    # ---- UI-thread slots
    def _on_key_changed(self, name, pressed):
        self.keys_hud.set_key(name, pressed)

    def _on_mouse_changed(self, name, pressed):
        self.keys_hud.set_mouse(name, pressed)

    def _on_left_click(self):
        self.stats.register_click()

    # ---- layout + lifecycle
    def place_windows(self):
        screen_geo = self.qapp.primaryScreen().availableGeometry()
        margin = 40
        self.keys_hud.move(screen_geo.width() - self.keys_hud.width() - margin, margin)
        self.fps_hud.move(
            screen_geo.width() - self.fps_hud.width() - margin,
            margin + self.keys_hud.height() + 10,
        )
        self.cps_hud.move(
            screen_geo.width() - self.cps_hud.width() - margin,
            margin + self.keys_hud.height() + self.fps_hud.height() + 20,
        )

    def show_all(self):
        self.keys_hud.show()
        self.fps_hud.show()
        self.cps_hud.show()

    def shutdown(self):
        try:
            self._kb_listener.stop()
            self._mouse_listener.stop()
        except Exception:
            pass
        self.stats.stop()
        QApplication.quit()

    def run(self):
        self.place_windows()
        self.show_all()
        return self.qapp.exec()


if __name__ == "__main__":
    app = App()
    sys.exit(app.run())