#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌面宠物 - 阿比西尼亚猫
基于 PySide6 + OpenCV 的绿幕视频桌宠程序

功能：
  1. 无边框透明置顶窗口，鼠标拖动移动，不抢焦点
  2. 自动色度抠图去除绿色背景，实时播放
  3. 动画状态机：待机循环 / 随机切换 / 单击转身 / 双击仰卧
  4. 右键菜单：缩放、间隔调整、隐藏、退出
  5. 系统托盘图标，最小化到后台
  6. 优化抠图参数，消除绿边溢色
"""

import sys
import os
import math
import random
import ctypes
import threading
import json
from datetime import datetime
from enum import Enum, auto

import cv2
import numpy as np
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QSystemTrayIcon, QMenu,
)
from PySide6.QtCore import Qt, QTimer, QPoint, QSize
from PySide6.QtGui import (
    QImage, QPixmap, QAction, QIcon, QColor, QPainter, QActionGroup, QPolygon,
    QCursor,
)

# ============================================================
#  Windows 原生消息结构（用于 nativeEvent 拦截 WM_MOUSEACTIVATE）
# ============================================================
if sys.platform == "win32":
    from ctypes import wintypes

    class _WinMSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", wintypes.POINT),
        ]
else:                                   # noqa: E701
    _WinMSG = None

# ============================================================
#  配置常量
# ============================================================

DEFAULT_VIDEO_FILES = {
    "idle":        "待机.mp4",
    "sneeze":      "打喷嚏.mp4",
    "lie_down":    "趴下.mp4",
    "lick_paw":    "舔爪子.mp4",
    "walk_left":   "向左走.mp4",
    "walk_right":  "向右走.mp4",
    "turn_around": "鼠标单击-转身.mp4",
    "lie_back":    "鼠标双击-仰卧.mp4",
    "sleep":       "睡觉.mp4",
    "wake_up":     "起床.mp4",
    "head_turn":   "转头.mp4",
}

# 随机切换动作池
DEFAULT_RANDOM_ACTIONS = ["sneeze", "lie_down", "lick_paw", "walk_left", "walk_right"]

# 走路动作 → 每帧水平位移（像素），正值右移，负值左移
# 设为空字典：走路视频照常播放，但窗口不移动（原地走路）
DEFAULT_WALK_ACTIONS = {}

# 基础缩放：新的 100% = 原始视频的 25%
DEFAULT_BASE_SCALE = 0.25

DEFAULT_SCALE      = 1.0        # 默认缩放 100%（= 原始视频的 25%）
DEFAULT_INTERVAL   = 60_000     # 默认随机切换间隔（毫秒）= 1 分钟
DRAG_THRESHOLD     = 5          # 拖动判定阈值（像素）
WM_MOUSEACTIVATE   = 0x0021
MA_NOACTIVATE      = 3

# 鼠标跟随模式参数
DETECT_RADIUS_MULT = 0.9        # 检测半径 = 窗口宽度 × 0.9
DEAD_ZONE_MULT     = 0.1        # 死区半径 = 窗口宽度 × 0.1
DIRECTION_OFFSET   = 90         # 屏幕→视频角度偏移：帧0=朝上, 屏幕上(270°)+90=360→帧0

# 运行时由 pet.json 覆盖。保留默认值，使旧的素材目录仍能运行。
VIDEO_FILES = dict(DEFAULT_VIDEO_FILES)
RANDOM_ACTIONS = list(DEFAULT_RANDOM_ACTIONS)
WALK_ACTIONS = dict(DEFAULT_WALK_ACTIONS)
BASE_SCALE = DEFAULT_BASE_SCALE


def get_app_dir() -> str:
    """返回脚本或打包后 exe 所在的目录。"""
    return (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))


def load_pet_config(app_dir: str) -> dict:
    """读取可编辑的 pet.json，并与内置默认设置合并。"""
    config = {
        "name": "阿比西尼亚猫桌宠",
        "video_directory": ".",
        "base_scale": DEFAULT_BASE_SCALE,
        "default_scale": DEFAULT_SCALE,
        "random_interval_seconds": DEFAULT_INTERVAL // 1000,
        "random_actions": list(DEFAULT_RANDOM_ACTIONS),
        "walk_actions": dict(DEFAULT_WALK_ACTIONS),
        "chroma_key": {},
        "actions": {key: {"file": value}
                    for key, value in DEFAULT_VIDEO_FILES.items()},
    }
    path = os.path.join(app_dir, "pet.json")
    if not os.path.isfile(path):
        print("[提示] 未找到 pet.json，使用内置默认配置。")
        return config

    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[警告] 无法读取 pet.json，使用内置默认配置：{exc}")
        return config

    for key in ("name", "video_directory", "base_scale", "default_scale",
                "random_interval_seconds", "random_actions", "walk_actions",
                "chroma_key"):
        if key in user:
            config[key] = user[key]
    if isinstance(user.get("actions"), dict):
        config["actions"].update(user["actions"])
    return config


def apply_pet_config(config: dict):
    """将配置转换为播放器使用的动作表；缺少的可选动作回退到待机。"""
    global VIDEO_FILES, RANDOM_ACTIONS, WALK_ACTIONS, BASE_SCALE
    actions = config.get("actions", {})
    idle = actions.get("idle", {"file": DEFAULT_VIDEO_FILES["idle"]})
    idle_file = idle.get("file", DEFAULT_VIDEO_FILES["idle"]) if isinstance(idle, dict) else idle
    files = {}
    for key, default_file in DEFAULT_VIDEO_FILES.items():
        action = actions.get(key, {"file": default_file})
        filename = action.get("file") if isinstance(action, dict) else action
        files[key] = filename or idle_file
    VIDEO_FILES = files
    RANDOM_ACTIONS = [a for a in config.get("random_actions", DEFAULT_RANDOM_ACTIONS)
                      if a in VIDEO_FILES]
    WALK_ACTIONS = {a: int(dx) for a, dx in config.get("walk_actions", {}).items()
                    if a in VIDEO_FILES}
    try:
        BASE_SCALE = max(0.01, float(config.get("base_scale", DEFAULT_BASE_SCALE)))
    except (TypeError, ValueError):
        BASE_SCALE = DEFAULT_BASE_SCALE


class PetState(Enum):
    IDLE        = auto()        # 待机循环
    RANDOM      = auto()        # 随机动作
    CLICK       = auto()        # 单击 → 转身
    DOUBLE_CLICK = auto()       # 双击 → 仰卧
    SLEEP       = auto()        # 睡眠（播放完睡觉视频后停在最后一帧）
    WAKING_UP   = auto()        # 起床中（播放起床视频）
    MOUSE_TRACK = auto()        # 鼠标跟随中（渲染转头帧）


class PetMode(Enum):
    RANDOM        = auto()      # 随机切换模式（默认）
    SLEEP         = auto()      # 睡眠模式
    MOUSE_FOLLOW  = auto()      # 鼠标跟随模式


# ============================================================
#  绿幕色度抠图引擎
# ============================================================

class ChromaKey:
    """
    HSV 色域检测绿色背景 + 形态学清理 + 边缘侵蚀 + Alpha 软边缘 + 溢色抑制。
    针对纯绿幕优化，消除主体边缘的绿边溢色。
    """

    def __init__(self, settings=None):
        settings = settings or {}
        # HSV 绿色范围
        self.lower_h = int(settings.get("hue_min", 35))
        self.upper_h = int(settings.get("hue_max", 85))
        self.min_s   = int(settings.get("saturation_min", 40))
        self.min_v   = int(settings.get("brightness_min", 40))

        # 形态学核
        self._k3 = np.ones((3, 3), np.uint8)
        self._k5 = np.ones((5, 5), np.uint8)

    # ----------------------------------------------------------

    def process(self, frame: np.ndarray) -> np.ndarray:
        """
        对一帧 BGR 图像做绿幕抠图，返回 RGBA（背景透明）。
        """
        # --- 1. HSV 色域生成绿色掩膜 ---
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        h_ch, s_ch, v_ch = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

        green_mask = (
            (h_ch >= self.lower_h) & (h_ch <= self.upper_h)
            & (s_ch >= self.min_s) & (v_ch >= self.min_v)
        ).astype(np.uint8) * 255

        # --- 2. 形态学清理 ---
        # 开运算：去除细小噪点
        green_mask = cv2.morphologyEx(
            green_mask, cv2.MORPH_OPEN, self._k3, iterations=1,
        )
        # 闭运算：填补主体内部绿色孔洞
        green_mask = cv2.morphologyEx(
            green_mask, cv2.MORPH_CLOSE, self._k5, iterations=1,
        )
        # 轻微膨胀：侵蚀掉主体边缘的绿边
        green_mask = cv2.dilate(green_mask, self._k3, iterations=1)

        # Alpha = 255 - green_mask（主体可见，背景透明）
        alpha = 255 - green_mask
        # 高斯模糊：软边缘，消除锯齿
        alpha = cv2.GaussianBlur(alpha, (3, 3), 0)

        # --- 3. 溢色抑制（去除主体上的绿色反光） ---
        b, g, r = cv2.split(frame.astype(np.float32))

        # 溢出量 = G - max(R, B)，仅在主体区域生效
        spill = np.maximum(g - np.maximum(r, b), 0.0)
        spill_mask = (alpha > 0).astype(np.float32)

        g_corr = np.clip(g - spill * 0.70 * spill_mask, 0, 255)
        r_corr = np.clip(r + spill * 0.25 * spill_mask, 0, 255)
        b_corr = np.clip(b + spill * 0.25 * spill_mask, 0, 255)

        corrected = cv2.merge([b_corr, g_corr, r_corr]).astype(np.uint8)

        # --- 4. 组装 RGBA ---
        rgba = cv2.cvtColor(corrected, cv2.COLOR_BGR2RGBA)
        rgba[:, :, 3] = alpha
        return np.ascontiguousarray(rgba)


# ============================================================
#  主窗口
# ============================================================

class DesktopPet(QMainWindow):
    """桌面宠物主窗口"""

    def __init__(self, video_dir: str, config: dict):
        super().__init__()
        self.video_dir  = video_dir
        self.pet_name = str(config.get("name", "桌宠"))
        self.chroma_key = ChromaKey(config.get("chroma_key"))

        # 状态
        self.state          = PetState.IDLE
        self.mode           = PetMode.RANDOM
        self.scale          = float(config.get("default_scale", DEFAULT_SCALE))
        self.interval_ms    = max(1_000, int(float(
            config.get("random_interval_seconds", DEFAULT_INTERVAL // 1000)) * 1000))
        self.current_action = "idle"
        self._pending_mode  = PetMode.RANDOM   # 起床后要进入的模式
        self._track_active  = False             # 鼠标跟踪是否正在渲染转头帧

        # 视频播放
        self.cap = None
        self._loop = True
        self._play_serial = 0              # 防止旧视频结束事件覆盖新模式
        self._rendered_serial = -1         # 记录每段视频是否至少成功渲染一帧
        self.video_timer = QTimer(self)
        self.video_timer.timeout.connect(self._on_video_tick)

        # 随机切换定时器
        self.random_timer = QTimer(self)
        self.random_timer.timeout.connect(self._on_random_switch)
        self.random_timer.start(self.interval_ms)

        # 鼠标跟踪定时器（30fps）
        self.mouse_track_timer = QTimer(self)
        self.mouse_track_timer.timeout.connect(self._on_mouse_track)

        # 转头帧预处理（后台线程）
        self._head_frames     = []              # 全部 RGBA 帧
        self._head_frame_count = 0              # 帧数
        self._head_ready       = False          # 预处理完成标志
        self._head_wait_timer  = None           # 等待预处理完成的轮询定时器
        self._load_head_frames()

        # 拖动 & 单/双击区分
        self._press_pos    = None
        self._drag_offset  = None
        self._is_dragging  = False
        self._click_timer  = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._handle_single_click)

        # UI
        self._init_window()
        self._init_label()
        self._init_tray()
        self._apply_no_activate()

        # 启动待机
        self._play("idle", loop=True)

    # ============================================================
    #  窗口初始化
    # ============================================================

    def _init_window(self):
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAutoFillBackground(False)

    def _init_label(self):
        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet("background: transparent;")
        # QLabel 覆盖了整个窗口；将交互事件明确转发给主窗口，
        # 避免右键菜单或单/双击因事件停在 QLabel 而失效。
        self.label.mousePressEvent = self.mousePressEvent
        self.label.mouseMoveEvent = self.mouseMoveEvent
        self.label.mouseReleaseEvent = self.mouseReleaseEvent
        self.label.mouseDoubleClickEvent = self.mouseDoubleClickEvent
        self.setCentralWidget(self.label)

    def _apply_no_activate(self):
        """设置 WS_EX_NOACTIVATE，点击窗口不抢焦点。"""
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE     = -20
            WS_EX_NOACTIVATE = 0x08000000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE,
            )
        except Exception:
            pass

    # ============================================================
    #  系统托盘
    # ============================================================

    def _init_tray(self):
        icon = self._make_tray_icon()
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(self.pet_name)
        self.tray.activated.connect(self._on_tray_activated)

        self._context_menu = QMenu()

        self._act_show = self._context_menu.addAction("显示宠物")
        self._act_show.triggered.connect(self.show_pet)

        act_hide = self._context_menu.addAction("隐藏宠物")
        act_hide.triggered.connect(self.hide_pet)

        self._context_menu.addSeparator()

        # --- 缩放子菜单 ---
        scale_menu = self._context_menu.addMenu("缩放大小")
        sg = QActionGroup(self)
        sg.setExclusive(True)
        self._scale_actions = {}
        for label, val in [("25%", 0.25), ("50%", 0.50), ("75%", 0.75),
                           ("100%", 1.0), ("125%", 1.25), ("150%", 1.5)]:
            act = scale_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(abs(val - self.scale) < 1e-3)
            act.toggled.connect(
                lambda checked, value=val: self._on_scale_action_toggled(value, checked)
            )
            sg.addAction(act)
            self._scale_actions[val] = act

        # --- 间隔子菜单 ---
        interval_menu = self._context_menu.addMenu("随机切换间隔")
        ig = QActionGroup(self)
        ig.setExclusive(True)
        self._interval_actions = {}
        for label, ms in [("5 秒", 5_000), ("1 分钟", 60_000),
                          ("5 分钟", 300_000), ("10 分钟", 600_000),
                          ("30 分钟", 1_800_000), ("1 小时", 3_600_000)]:
            act = interval_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(ms == self.interval_ms)
            act.toggled.connect(
                lambda checked, value=ms: self._on_interval_action_toggled(value, checked)
            )
            ig.addAction(act)
            self._interval_actions[ms] = act

        self._context_menu.addSeparator()

        # --- 模式选择子菜单 ---
        mode_menu = self._context_menu.addMenu("模式选择")
        mg = QActionGroup(self)
        mg.setExclusive(True)
        self._mode_actions = {}
        for label, m in [("随机切换", PetMode.RANDOM),
                         ("睡眠", PetMode.SLEEP),
                         ("鼠标跟随", PetMode.MOUSE_FOLLOW)]:
            act = mode_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.mode == m)
            # 用 toggled 而非 triggered：在 WS_EX_NOACTIVATE 窗口的托盘菜单中，
            # QAction 的 triggered(bool) 在部分 Windows/PySide6 组合下不稳定，
            # 但被选中的可勾选动作始终会发出 toggled(True)。
            act.toggled.connect(
                lambda checked, mode=m: self._on_mode_action_toggled(mode, checked)
            )
            mg.addAction(act)
            self._mode_actions[m] = act

        self._context_menu.addSeparator()

        act_quit = self._context_menu.addAction("退出")
        act_quit.triggered.connect(self.quit_app)

        self.tray.setContextMenu(self._context_menu)
        self.tray.show()

    def _make_tray_icon(self) -> QIcon:
        """程序化绘制阿比西尼亚猫头部图标。"""
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)

        # 头
        p.setBrush(QColor("#E8820E"))
        p.drawEllipse(10, 14, 44, 40)
        # 耳朵
        p.drawPolygon(QPolygon([QPoint(12, 22), QPoint(18, 2),  QPoint(28, 20)]))
        p.drawPolygon(QPolygon([QPoint(52, 22), QPoint(46, 2),  QPoint(36, 20)]))
        # 耳朵内侧
        p.setBrush(QColor("#FFB347"))
        p.drawPolygon(QPolygon([QPoint(16, 18), QPoint(20, 8),  QPoint(25, 18)]))
        p.drawPolygon(QPolygon([QPoint(48, 18), QPoint(44, 8),  QPoint(39, 18)]))
        # 眼睛
        p.setBrush(QColor("#2B2B2B"))
        p.drawEllipse(22, 28, 7, 9)
        p.drawEllipse(35, 28, 7, 9)
        # 高光
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(24, 30, 2, 3)
        p.drawEllipse(37, 30, 2, 3)
        # 鼻子
        p.setBrush(QColor("#D4575E"))
        p.drawEllipse(28, 40, 8, 5)
        p.end()
        return QIcon(pm)

    def _write_mode_log(self, message: str):
        """记录模式菜单事件，便于排查发布版的 Windows 菜单问题。"""
        try:
            log_path = os.path.join(get_app_dir(), "desktop_pet.log")
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(f"{timestamp} {message}\n")
        except OSError:
            pass

    def _on_mode_action_toggled(self, mode: PetMode, checked: bool):
        """只响应被选中的模式动作，忽略互斥组取消勾选的动作。"""
        self._write_mode_log(f"menu toggled: {mode.name}, checked={checked}")
        if checked:
            self._switch_mode(mode)

    def _on_scale_action_toggled(self, scale: float, checked: bool):
        """响应缩放菜单的选中事件。"""
        self._write_mode_log(f"scale toggled: {scale}, checked={checked}")
        if checked:
            self.set_scale(scale)

    def _on_interval_action_toggled(self, ms: int, checked: bool):
        """响应随机间隔菜单的选中事件。"""
        self._write_mode_log(f"interval toggled: {ms}, checked={checked}")
        if checked:
            self.set_interval(ms)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_pet()

    # ============================================================
    #  视频播放
    # ============================================================

    def _video_path(self, key: str) -> str:
        return os.path.join(self.video_dir, VIDEO_FILES[key])

    def _stop_video(self):
        """停止当前视频，并使其尚未处理的结束事件失效。"""
        self._play_serial += 1
        self.video_timer.stop()
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def _sync_mode_ui(self, status: str = ""):
        """让菜单勾选和托盘提示反映实际模式。"""
        for mode, action in self._mode_actions.items():
            action.setChecked(mode == self.mode)
        mode_label = {
            PetMode.RANDOM: "随机切换",
            PetMode.SLEEP: "睡眠",
            PetMode.MOUSE_FOLLOW: "鼠标跟随",
        }[self.mode]
        suffix = f"（{status}）" if status else ""
        self.tray.setToolTip(f"{self.pet_name} - {mode_label}{suffix}")

    def _play(self, key: str, loop: bool = False):
        path = self._video_path(key)
        if not os.path.isfile(path):
            print(f"[警告] 视频文件不存在: {path}")
            self._write_mode_log(f"play failed, missing file: {key}")
            return

        self.current_action = key
        self._loop = loop
        self._stop_video()
        self._play_serial += 1

        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            print(f"[警告] 无法打开视频: {path}")
            self._write_mode_log(f"play failed, cannot open: {key}")
            self.cap = None
            return

        fps = self.cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0 or fps > 120:
            fps = 30
        self.video_timer.setInterval(int(1000 / fps))
        self.video_timer.start()
        self._write_mode_log(f"play started: {key}, serial={self._play_serial}, fps={fps:.1f}")

    def _on_video_tick(self):
        if self.cap is None:
            self.video_timer.stop()
            return

        ret, frame = self.cap.read()
        if not ret:
            if self._loop:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = self.cap.read()
                if not ret:
                    self._on_video_finished(self._play_serial)
                    return
            else:
                self._on_video_finished(self._play_serial)
                return

        # 先缩放（大幅减少后续抠图处理量）
        actual_scale = self.scale * BASE_SCALE
        if abs(actual_scale - 1.0) > 1e-3:
            new_w = max(1, int(frame.shape[1] * actual_scale))
            new_h = max(1, int(frame.shape[0] * actual_scale))
            frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

        # 抠图（在缩放后的小分辨率上处理，速度大幅提升）
        rgba = self.chroma_key.process(frame)

        # 显示
        h, w = rgba.shape[:2]
        img = QImage(rgba.tobytes(), w, h, w * 4, QImage.Format_RGBA8888)
        self.label.setPixmap(QPixmap.fromImage(img))

        if self.size() != QSize(w, h):
            self.resize(w, h)
        if self._rendered_serial != self._play_serial:
            self._rendered_serial = self._play_serial
            self._write_mode_log(
                f"first frame rendered: {self.current_action}, serial={self._play_serial}"
            )

    def _walk_move(self, dx: int):
        pos = self.pos()
        screen = QApplication.primaryScreen().availableGeometry()
        new_x = max(screen.left(),
                     min(pos.x() + dx, screen.right() - self.width()))
        self.move(new_x, pos.y())

    def _on_video_finished(self, serial: int):
        """仅处理当前播放会话的结束，避免旧动画恢复待机。"""
        if serial != self._play_serial:
            return
        self._write_mode_log(f"video finished: {self.current_action}, state={self.state.name}")
        self._stop_video()

        # 睡眠视频播完 → 停在最后一帧，不恢复待机
        if self.state == PetState.SLEEP:
            return

        # 起床视频播完 → 进入待定模式
        if self.state == PetState.WAKING_UP:
            if self._pending_mode == PetMode.MOUSE_FOLLOW:
                self._enter_mouse_follow()
            else:
                self._resume_idle()
            return

        self._resume_idle()

    # ============================================================
    #  动画状态机
    # ============================================================

    def _resume_idle(self):
        self.state = PetState.IDLE
        if self.mode == PetMode.RANDOM:
            self.random_timer.start(self.interval_ms)
        self._play("idle", loop=True)

    def _on_random_switch(self):
        if self.mode != PetMode.RANDOM:
            return
        if self.state != PetState.IDLE:
            return
        action = random.choice(RANDOM_ACTIONS)
        self.state = PetState.RANDOM
        self._play(action, loop=False)

    def _trigger_click(self):
        if self.mode in (PetMode.SLEEP, PetMode.MOUSE_FOLLOW):
            return
        if self.state in (PetState.CLICK, PetState.DOUBLE_CLICK,
                          PetState.SLEEP, PetState.WAKING_UP,
                          PetState.MOUSE_TRACK):
            return
        self.state = PetState.CLICK
        self.random_timer.stop()
        self._play("turn_around", loop=False)

    def _trigger_double_click(self):
        if self.mode in (PetMode.SLEEP, PetMode.MOUSE_FOLLOW):
            return
        self.state = PetState.DOUBLE_CLICK
        self.random_timer.stop()
        self._play("lie_back", loop=False)

    # ============================================================
    #  鼠标交互（拖动 + 单击/双击区分）
    # ============================================================

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos   = event.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.frameGeometry().topLeft()
            self._is_dragging = False
        elif event.button() == Qt.RightButton:
            self._context_menu.exec(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self._press_pos is not None:
            if not self._is_dragging:
                if (event.globalPosition().toPoint()
                        - self._press_pos).manhattanLength() > DRAG_THRESHOLD:
                    self._is_dragging = True
            if self._is_dragging:
                self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self._is_dragging:
                self._click_timer.start(QApplication.doubleClickInterval())
            self._is_dragging = False
            self._press_pos = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._click_timer.stop()
            self._trigger_double_click()

    def _handle_single_click(self):
        self._trigger_click()

    # ============================================================
    #  模式切换
    # ============================================================

    def _switch_mode(self, mode: PetMode):
        if mode == self.mode:
            self._write_mode_log(f"mode unchanged: {mode.name}")
            return

        old_mode = self.mode
        print(f"[模式] {old_mode.name} -> {mode.name}")
        self._write_mode_log(f"switch: {old_mode.name} -> {mode.name}")
        self.random_timer.stop()
        self._click_timer.stop()
        self._stop_video()

        # 从睡眠切出 → 先播放起床视频，记下目标模式
        if old_mode == PetMode.SLEEP:
            self.mode = mode
            self._pending_mode = mode
            self.state = PetState.WAKING_UP
            self._sync_mode_ui("起床中")
            self._play("wake_up", loop=False)
            return

        # 退出鼠标跟随 → 停止跟踪
        if old_mode == PetMode.MOUSE_FOLLOW:
            self._stop_mouse_follow()

        # 进入新模式
        self.mode = mode
        self._sync_mode_ui()
        if mode == PetMode.SLEEP:
            self.state = PetState.SLEEP
            self._play("sleep", loop=False)
        elif mode == PetMode.MOUSE_FOLLOW:
            self._enter_mouse_follow()
        else:                                       # RANDOM
            self._resume_idle()

    # ---- 鼠标跟随模式 ----

    def _load_head_frames(self):
        """启动后台线程预处理转头视频：读取全部帧 → 缩放 → 抠图 → 存储。

        新视频为 1280×720 16:9 匀速顺时针转头（212帧，帧0=朝上）。
        由于待机视频也是 1280×720，两者缩放后尺寸完全一致，
        切换时窗口尺寸/位置不变，无需任何锚点对齐。
        """
        head_path = os.path.join(self.video_dir,
                                 VIDEO_FILES.get("head_turn", ""))
        if not os.path.isfile(head_path):
            print("[转头] 未找到转头视频，鼠标跟随模式不可用")
            return

        def _worker():
            cap = cv2.VideoCapture(head_path)
            if not cap.isOpened():
                print("[转头] 无法打开转头视频")
                return
            frames = []
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                # 只按 BASE_SCALE 缩放（不乘 self.scale），
                # 用户缩放在渲染时实时处理，改缩放无需重新预处理
                actual_scale = BASE_SCALE
                new_w = max(1, int(frame.shape[1] * actual_scale))
                new_h = max(1, int(frame.shape[0] * actual_scale))
                sf = cv2.resize(frame, (new_w, new_h),
                                interpolation=cv2.INTER_AREA)
                rgba = self.chroma_key.process(sf)
                frames.append(rgba)
            cap.release()

            n = len(frames)
            if n < 2:
                print("[转头] 帧数不足")
                return

            self._head_frames = frames
            self._head_frame_count = n
            self._head_ready = True
            print(f"[转头] 预处理完成: {n} 帧")
            self._write_mode_log(f"head frames ready: {n}")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _angle_to_frame(self, screen_angle: float):
        """根据屏幕角度返回对应的 RGBA 帧（含线性插值）。

        映射公式（匀速顺时针视频，帧0=朝上）：
          video_angle = (screen_angle + DIRECTION_OFFSET) % 360
          frame_index = video_angle × N / 360

        screen_angle: atan2(dy, dx)，0°=右, 90°=下, 180°=左, 270°=上
        DIRECTION_OFFSET=90 使屏幕上(270°)→视频0°→帧0(朝上)
        """
        n = self._head_frame_count
        if n == 0:
            return None

        video_angle = (screen_angle + DIRECTION_OFFSET) % 360.0
        idx_f = video_angle * n / 360.0
        idx_lo = int(idx_f) % n
        idx_hi = (idx_lo + 1) % n
        alpha = idx_f - int(idx_f)

        frame_lo = self._head_frames[idx_lo]
        frame_hi = self._head_frames[idx_hi]
        blended = cv2.addWeighted(frame_lo, 1.0 - alpha, frame_hi, alpha, 0)

        # 按当前 self.scale 实时缩放（缓存帧固定为 BASE_SCALE 尺寸）
        if abs(self.scale - 1.0) > 1e-3:
            new_w = max(1, int(blended.shape[1] * self.scale))
            new_h = max(1, int(blended.shape[0] * self.scale))
            blended = cv2.resize(blended, (new_w, new_h),
                                 interpolation=cv2.INTER_AREA)
        return blended

    def _enter_mouse_follow(self):
        """进入鼠标跟随模式。"""
        self.random_timer.stop()
        self._click_timer.stop()

        if not self._head_ready:
            # 帧还在预处理 → 待机等待
            self.state = PetState.IDLE
            self._play("idle", loop=True)
            self._sync_mode_ui("正在准备转头动画")
            self._head_wait_timer = QTimer(self)
            self._head_wait_timer.setSingleShot(False)
            self._head_wait_timer.timeout.connect(self._check_head_ready)
            self._head_wait_timer.start(200)
            return

        self._start_mouse_tracking()

    def _check_head_ready(self):
        """轮询检查转头帧预处理是否完成。"""
        if self.mode != PetMode.MOUSE_FOLLOW:
            self._head_wait_timer.stop()
            return
        if self._head_ready:
            self._head_wait_timer.stop()
            self._start_mouse_tracking()

    def _start_mouse_tracking(self):
        """开始鼠标跟踪。"""
        self._track_active = False
        self.state = PetState.IDLE
        self._play("idle", loop=True)
        self.mouse_track_timer.start(33)            # ~30fps
        self._sync_mode_ui("已启用")
        self._write_mode_log("mouse tracking started")

    def _stop_mouse_follow(self):
        """停止鼠标跟随。"""
        self.mouse_track_timer.stop()
        if (self._head_wait_timer is not None
                and self._head_wait_timer.isActive()):
            self._head_wait_timer.stop()
        self._track_active = False

    def _on_mouse_track(self):
        """鼠标跟踪定时器回调：三区域检测，跟踪区显示转头帧，其余显示待机。"""
        if self.mode != PetMode.MOUSE_FOLLOW:
            return
        if not self._head_ready:
            return
        if self._is_dragging:
            return

        mouse_pos = QCursor.pos()
        cat_cx = self.x() + self.width()  // 2
        cat_cy = self.y() + self.height() // 2

        dx = mouse_pos.x() - cat_cx
        dy = mouse_pos.y() - cat_cy
        dist = math.sqrt(dx * dx + dy * dy)

        # 检测半径基于当前窗口宽度（两视频同尺寸，窗口不变，无振荡）
        win_w = self.width()
        detect_radius = win_w * DETECT_RADIUS_MULT
        dead_zone     = win_w * DEAD_ZONE_MULT

        # ---- 远处或死区 → 待机视频 ----
        if dist > detect_radius or dist < dead_zone:
            if self._track_active:
                self._track_active = False
                self.state = PetState.IDLE
                self._play("idle", loop=True)
            return

        # ---- 跟踪区 → 渲染转头帧 ----
        if not self._track_active:
            self._track_active = True
            self.state = PetState.MOUSE_TRACK
            self.video_timer.stop()
            if self.cap is not None:
                self.cap.release()
                self.cap = None

        # 计算屏幕角度 (0=右, 90=下, 180=左, 270=上)
        screen_angle = math.degrees(math.atan2(dy, dx))
        if screen_angle < 0:
            screen_angle += 360.0

        frame = self._angle_to_frame(screen_angle)
        if frame is None:
            return

        h, w = frame.shape[:2]
        img = QImage(frame.tobytes(), w, h, w * 4,
                     QImage.Format_RGBA8888)
        self.label.setPixmap(QPixmap.fromImage(img))

        # 同尺寸视频 → 窗口不变，仅首次同步尺寸
        if self.size() != QSize(w, h):
            self.resize(w, h)

    # ============================================================
    #  参数设置
    # ============================================================

    def set_scale(self, scale: float):
        self.scale = scale
        # 方案A：睡眠定格状态（sleep 视频已播完、video_timer 已停止）下，
        # set_scale 只更新了变量却没有重绘，需主动重渲染最后一帧以应用缩放
        if (self.mode == PetMode.SLEEP
                and self.state == PetState.SLEEP
                and self.cap is None):
            self._render_sleep_frame()

    def _render_sleep_frame(self):
        """睡眠定格时，用当前 self.scale 重新渲染 sleep 视频的最后一帧。

        正常缩放依赖 _on_video_tick 每帧重绘；睡眠模式视频 loop=False，
        播完 _on_video_finished 会停止定时器并定格，因此缩放需手动触发一次重绘。
        """
        path = self._video_path("sleep")
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame = None
        # 从末尾向前找最近的一帧有效帧（避开视频结尾可能的空帧）
        for offset in range(1, 6):
            idx = total - offset
            if idx < 0:
                continue
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, f = cap.read()
            if ret and f is not None and f.size > 0:
                frame = f
                break
        cap.release()

        if frame is None:
            return

        # 与 _on_video_tick 一致的缩放 + 抠图流程
        actual_scale = self.scale * BASE_SCALE
        if abs(actual_scale - 1.0) > 1e-3:
            new_w = max(1, int(frame.shape[1] * actual_scale))
            new_h = max(1, int(frame.shape[0] * actual_scale))
            frame = cv2.resize(frame, (new_w, new_h),
                               interpolation=cv2.INTER_AREA)

        rgba = self.chroma_key.process(frame)
        h, w = rgba.shape[:2]
        img = QImage(rgba.tobytes(), w, h, w * 4,
                     QImage.Format_RGBA8888)
        self.label.setPixmap(QPixmap.fromImage(img))

        if self.size() != QSize(w, h):
            self.resize(w, h)

    def set_interval(self, ms: int):
        self.interval_ms = ms
        self.random_timer.setInterval(ms)

    # ============================================================
    #  显示 / 隐藏 / 退出
    # ============================================================

    def hide_pet(self):
        self.hide()

    def show_pet(self):
        self.show()
        self.raise_()

    def quit_app(self):
        self.video_timer.stop()
        self.random_timer.stop()
        self.mouse_track_timer.stop()
        if (self._head_wait_timer is not None
                and self._head_wait_timer.isActive()):
            self._head_wait_timer.stop()
        if self.cap is not None:
            self.cap.release()
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event):
        """关闭窗口时最小化到托盘而非退出。"""
        event.ignore()
        self.hide_pet()

    # ============================================================
    #  Windows 原生事件：拦截 WM_MOUSEACTIVATE
    # ============================================================

    def nativeEvent(self, eventType, message):
        if eventType == b"windows_generic_MSG" and _WinMSG is not None:
            try:
                msg = _WinMSG.from_address(int(message))
                if msg.message == WM_MOUSEACTIVATE:
                    return True, MA_NOACTIVATE
            except Exception:
                pass
        return super().nativeEvent(eventType, message)


# ============================================================
#  入口
# ============================================================

def get_video_dir(config: dict) -> str:
    """定位配置指定的素材目录；兼容脚本运行与 PyInstaller 打包。"""
    base = get_app_dir()
    configured = str(config.get("video_directory", "videos"))
    if os.path.isabs(configured):
        return configured
    return os.path.normpath(os.path.join(base, configured))


def main():
    config = load_pet_config(get_app_dir())
    apply_pet_config(config)
    app = QApplication(sys.argv)
    app.setApplicationName(str(config.get("name", "桌宠")))
    app.setQuitOnLastWindowClosed(False)   # 关窗口不退出，驻留托盘

    video_dir = get_video_dir(config)

    if not os.path.isfile(os.path.join(video_dir, VIDEO_FILES["idle"])):
        print(f"[错误] 找不到待机视频，请检查目录: {video_dir}")
        sys.exit(1)

    pet = DesktopPet(video_dir, config)
    pet.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
