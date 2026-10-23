#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三星手机投屏与相册管理中心 (Samsung Screen Mirror & Gallery Hub)
支持：
1. 基于 Scrcpy v3.1 的 60FPS 超清低延迟投屏（支持键鼠反控、音频同步、息屏投屏、窗口置顶）
2. 手机快捷遥控（一键截屏直存桌面、电源键、返回、主页、多任务、音量控制）
3. 手机相册（图片 & 视频）可视化分相册浏览、异步缩略图预览、勾选导出与一键全量增量备份到电脑桌面
"""

import os
import sys
import time
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QComboBox,
    QLineEdit, QProgressBar, QFileDialog, QMessageBox, QCheckBox,
    QFrame, QSplitter, QAbstractItemView, QMenu, QAction, QDialog
)
from PyQt5.QtCore import Qt, QSize, QThread, pyqtSignal, QTimer, QUrl
from PyQt5.QtGui import QIcon, QPixmap, QImage, QFont, QColor, QPainter, QDesktopServices
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
SCRCPY_DIR = BASE_DIR / "scrcpy"
ADB_EXE = SCRCPY_DIR / "adb.exe"
SCRCPY_EXE = SCRCPY_DIR / "scrcpy.exe"

# Fallback to pcsuite adb if needed
if not ADB_EXE.exists():
    alt_adb = Path(r"E:\Product\pcsuite\adb_41\adb.exe")
    if alt_adb.exists():
        ADB_EXE = alt_adb

THUMB_CACHE_DIR = BASE_DIR / "cache_thumbs"
THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_desktop_dir() -> Path:
    """获取当前用户正在使用的桌面路径（兼容 OneDrive 桌面与本地桌面）"""
    onedrive_desktop = Path(os.path.expanduser("~")) / "OneDrive" / "Desktop"
    local_desktop = Path(os.path.expanduser("~")) / "Desktop"
    if onedrive_desktop.exists():
        return onedrive_desktop
    if local_desktop.exists():
        return local_desktop
    return BASE_DIR


DEFAULT_EXPORT_DIR = get_desktop_dir() / "三星手机相册导出"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".3gp", ".webm"}

CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run_adb(args, serial=None, timeout=20, binary=False):
    """安全调用 ADB 命令"""
    cmd = [str(ADB_EXE)]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    env = os.environ.copy()
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=CREATION_FLAGS,
            env=env,
        )
        if binary:
            return res.returncode, res.stdout, res.stderr
        return (
            res.returncode,
            res.stdout.decode("utf-8", errors="ignore"),
            res.stderr.decode("utf-8", errors="ignore"),
        )
    except Exception as e:
        if binary:
            return -1, b"", str(e).encode("utf-8", errors="ignore")
        return -1, "", str(e)


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


class MediaItem:
    def __init__(self, remote_path: str, size: int, mtime: int):
        self.remote_path = remote_path
        self.name = os.path.basename(remote_path)
        self.folder = os.path.dirname(remote_path)
        self.album = self._resolve_album(self.folder)
        self.ext = os.path.splitext(self.name)[1].lower()
        self.is_video = self.ext in VIDEO_EXTS
        self.size = size
        self.mtime = mtime
        self.dt_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime > 0 else ""

    @staticmethod
    def _resolve_album(folder: str) -> str:
        mapping = {
            "/sdcard/DCIM/Camera": "📷 相机拍摄 (Camera)",
            "/sdcard/DCIM/Screen recordings": "📹 屏幕录制 (Screen recordings)",
            "/sdcard/DCIM/Screenshots": "🖼️ 系统截屏 (Screenshots)",
            "/sdcard/DCIM/touch-screenshot": "📸 快捷截屏 (touch-screenshot)",
            "/sdcard/DCIM/touch-longshot": "📜 长截图 (touch-longshot)",
            "/sdcard/DCIM/touch-sr": "🎬 快捷录屏 (touch-sr)",
            "/sdcard/Pictures": "🎨 图片收藏 (Pictures)",
            "/sdcard/Movies": "🎞️ 视频电影 (Movies)",
            "/sdcard/Download": "📥 下载内容 (Download)",
            "/sdcard": "📁 根目录临时截图 (/sdcard)",
        }
        if folder in mapping:
            return mapping[folder]
        for k, v in mapping.items():
            if k != "/sdcard" and folder.startswith(k + "/"):
                sub = folder[len(k) + 1:]
                return f"{v} - {sub}"
        return f"📂 {os.path.basename(folder) or folder}"


class ScanWorker(QThread):
    """后台扫描手机相册目录的线程"""
    finished_signal = pyqtSignal(list, str)

    def __init__(self, serial: str, include_root_temp: bool = False):
        super().__init__()
        self.serial = serial
        self.include_root_temp = include_root_temp

    def run(self):
        if not self.serial:
            self.finished_signal.emit([], "未检测到已连接的手机")
            return

        # 使用 find -printf 一次性极速获取所有文件的大小、修改时间戳和路径，再由 Python 过滤扩展名
        shell_cmd = (
            "find /sdcard/DCIM /sdcard/Pictures /sdcard/Movies /sdcard/Download "
            "-maxdepth 3 -type f -printf '%s|%T@|%p\\n' 2>/dev/null"
        )
        if self.include_root_temp:
            shell_cmd += "; find /sdcard -maxdepth 1 -type f -printf '%s|%T@|%p\\n' 2>/dev/null"

        code, out, err = run_adb(["shell", shell_cmd], serial=self.serial, timeout=20)
        if code != 0 and not out.strip():
            self.finished_signal.emit([], f"扫描失败: {err}")
            return

        valid_exts = IMAGE_EXTS | VIDEO_EXTS
        items = []
        seen = set()
        for line in out.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            try:
                sz = int(parts[0])
                mt = int(float(parts[1]))
                rpath = parts[2].strip()
                if rpath in seen or "/." in rpath:
                    continue
                ext = os.path.splitext(rpath)[1].lower()
                if ext not in valid_exts:
                    continue
                if not self.include_root_temp and os.path.dirname(rpath) == "/sdcard":
                    continue
                seen.add(rpath)
                items.append(MediaItem(rpath, sz, mt))
            except ValueError:
                continue

        # 默认按修改时间倒序排列（最新拍摄/录制的排在最前面）
        items.sort(key=lambda x: (x.mtime, x.name), reverse=True)
        self.finished_signal.emit(items, "")


class ThumbnailWorker(QThread):
    """后台异步拉取并生成缩略图的线程"""
    thumb_ready = pyqtSignal(str, str)  # (remote_path, local_thumb_path)

    def __init__(self, serial: str, media_items: list):
        super().__init__()
        self.serial = serial
        self.media_items = media_items
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        # 限制单次自动生成的缩略图数量并在每次拉取间加入短暂间隔，防止占满 USB 2.0 集线器带宽导致 ADB 断连
        loaded_new = 0
        for item in self.media_items:
            if self._stop:
                break
            if item.is_video:
                continue
            key = hashlib.md5(f"{item.remote_path}_{item.size}_{item.mtime}".encode("utf-8")).hexdigest()
            thumb_path = THUMB_CACHE_DIR / f"{key}.jpg"
            if thumb_path.exists() and thumb_path.stat().st_size > 0:
                self.thumb_ready.emit(item.remote_path, str(thumb_path))
                continue

            if loaded_new >= 36 or item.size > 15 * 1024 * 1024:
                continue
            tmp_raw = THUMB_CACHE_DIR / f"tmp_{key}{item.ext}"
            code, _, _ = run_adb(["pull", item.remote_path, str(tmp_raw)], serial=self.serial, timeout=15)
            if self._stop:
                if tmp_raw.exists():
                    try:
                        tmp_raw.unlink()
                    except Exception:
                        pass
                break
            if code == 0 and tmp_raw.exists():
                loaded_new += 1
                try:
                    with Image.open(tmp_raw) as img:
                        img = img.convert("RGB")
                        img.thumbnail((220, 220))
                        img.save(thumb_path, "JPEG", quality=82)
                    self.thumb_ready.emit(item.remote_path, str(thumb_path))
                except Exception:
                    pass
                finally:
                    try:
                        tmp_raw.unlink()
                    except Exception:
                        pass
            time.sleep(0.08)


class ExportWorker(QThread):
    """后台批量导出图片与视频到电脑的线程"""
    progress_signal = pyqtSignal(int, int, str)  # (current, total, filename)
    finished_signal = pyqtSignal(int, int, int, str)  # (success, skipped, failed, export_dir)

    def __init__(self, serial: str, items: list, dest_root: Path, keep_album_folders: bool = True, skip_existing: bool = True):
        super().__init__()
        self.serial = serial
        self.items = items
        self.dest_root = dest_root
        self.keep_album_folders = keep_album_folders
        self.skip_existing = skip_existing
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        self.dest_root.mkdir(parents=True, exist_ok=True)
        total = len(self.items)
        success = 0
        skipped = 0
        failed = 0

        for idx, item in enumerate(self.items, start=1):
            if self._stop:
                break
            self.progress_signal.emit(idx, total, item.name)

            if self.keep_album_folders:
                folder_name = os.path.basename(item.folder) or "Root"
                target_dir = self.dest_root / folder_name
            else:
                target_dir = self.dest_root
            target_dir.mkdir(parents=True, exist_ok=True)
            local_file = target_dir / item.name

            # 增量跳过检查：同名且文件大小一致则直接跳过
            if self.skip_existing and local_file.exists():
                try:
                    if local_file.stat().st_size == item.size:
                        skipped += 1
                        continue
                except Exception:
                    pass

            # 根据文件大小动态计算超时时间（大视频支持更长超时）
            timeout_sec = max(30, int(item.size / (2 * 1024 * 1024)) + 30)
            code, _, _ = run_adb(
                ["pull", "-a", item.remote_path, str(local_file)],
                serial=self.serial,
                timeout=timeout_sec,
            )
            if code == 0 and local_file.exists():
                success += 1
            else:
                failed += 1

        self.finished_signal.emit(success, skipped, failed, str(self.dest_root))


class SamsungStudioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("📱 三星手机投屏与相册中心 (Samsung Screen & Gallery Studio)")
        self.resize(1220, 780)
        if (SCRCPY_DIR / "icon.png").exists():
            self.setWindowIcon(QIcon(str(SCRCPY_DIR / "icon.png")))

        self.current_serial = None
        self.all_media_items = []
        self.filtered_items = []
        self.item_widgets = {}  # remote_path -> QListWidgetItem
        self.thumb_worker = None
        self.export_worker = None
        self.scan_worker = None
        self.scrcpy_proc = None

        self.video_icon = self._create_placeholder_icon("🎬 视频", QColor("#2563EB"), QColor("#EFF6FF"))
        self.image_icon = self._create_placeholder_icon("🖼️ 图片", QColor("#059669"), QColor("#ECFDF5"))

        self._init_ui()
        self._apply_styles()

        # 启动后自动检测设备并加载相册
        QTimer.singleShot(200, self.refresh_device_and_gallery)

    def _create_placeholder_icon(self, label_text: str, accent: QColor, bg: QColor) -> QIcon:
        pix = QPixmap(180, 140)
        pix.fill(bg)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(accent)
        font = QFont("Microsoft YaHei UI", 13, QFont.Bold)
        painter.setFont(font)
        painter.drawText(pix.rect(), Qt.AlignCenter, label_text)
        painter.end()
        return QIcon(pix)

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(12)

        # ================= 1. 顶部设备状态栏 =================
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(16, 10, 16, 10)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #10B981; font-size: 18px; font-weight: bold;")
        self.device_label = QLabel("正在检测三星手机 ADB 连接...")
        self.device_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #1E293B;")

        self.device_detail_label = QLabel("")
        self.device_detail_label.setStyleSheet("font-size: 12px; color: #64748B;")

        self.btn_open_compressor = QPushButton("🗜️ 图片/视频极速压缩工具")
        self.btn_open_compressor.setObjectName("AccentButton")
        self.btn_open_compressor.setCursor(Qt.PointingHandCursor)
        self.btn_open_compressor.clicked.connect(self.open_compressor_tool)

        self.btn_refresh_dev = QPushButton("🔄 刷新连接与相册")
        self.btn_refresh_dev.setCursor(Qt.PointingHandCursor)
        self.btn_refresh_dev.clicked.connect(self.refresh_device_and_gallery)

        top_layout.addWidget(self.status_dot)
        top_layout.addWidget(self.device_label)
        top_layout.addSpacing(12)
        top_layout.addWidget(self.device_detail_label)
        top_layout.addStretch()
        top_layout.addWidget(self.btn_open_compressor)
        top_layout.addWidget(self.btn_refresh_dev)
        main_layout.addWidget(top_bar)

        # ================= 2. 主体左右分栏 =================
        splitter = QSplitter(Qt.Horizontal)

        # ---------- 左侧：高清投屏与手机快捷遥控面板 ----------
        left_panel = QFrame()
        left_panel.setObjectName("LeftPanel")
        left_panel.setFixedWidth(320)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(16, 16, 16, 16)
        left_layout.setSpacing(12)

        title_cast = QLabel("🖥️ 三星手机实时投屏")
        title_cast.setStyleSheet("font-size: 16px; font-weight: bold; color: #0F172A;")
        left_layout.addWidget(title_cast)

        desc_cast = QLabel("基于 Scrcpy 5.0.1 硬件解码引擎（H.265 稳流模式），支持 60FPS 超低延迟同屏、电脑键鼠直接操作手机、剪贴板同步及文件拖入传输。")
        desc_cast.setWordWrap(True)
        desc_cast.setStyleSheet("font-size: 12px; color: #475569; line-height: 1.4;")
        left_layout.addWidget(desc_cast)

        # 投屏参数选项
        self.chk_stay_awake = QCheckBox("投屏时保持手机常亮 (防锁屏)")
        self.chk_stay_awake.setChecked(True)
        self.chk_turn_screen_off = QCheckBox("息屏投屏模式 (手机黑屏省电，电脑显示)")
        self.chk_turn_screen_off.setChecked(False)
        self.chk_always_on_top = QCheckBox("投屏窗口置顶显示")
        self.chk_always_on_top.setChecked(False)
        self.chk_enable_audio = QCheckBox("同步手机声音到电脑 (建议关闭以防 USB 集线器闪退)")
        self.chk_enable_audio.setChecked(False)

        left_layout.addWidget(self.chk_stay_awake)
        left_layout.addWidget(self.chk_turn_screen_off)
        left_layout.addWidget(self.chk_always_on_top)
        left_layout.addWidget(self.chk_enable_audio)

        # 画质选择
        res_row = QHBoxLayout()
        res_row.addWidget(QLabel("投屏画质:"))
        self.combo_quality = QComboBox()
        self.combo_quality.addItems([
            "高清防断连模式 (推荐 H.265 / 1280P / 60FPS)",
            "超清画质模式 (H.265 / 1600P / 60FPS)",
            "极速流畅模式 (H.265 / 960P / 60FPS)",
        ])
        res_row.addWidget(self.combo_quality, 1)
        left_layout.addLayout(res_row)

        # 启动投屏主按钮与固定展示按钮
        self.btn_start_mirror = QPushButton("🚀 立即启动手机投屏")
        self.btn_start_mirror.setObjectName("PrimaryButton")
        self.btn_start_mirror.setMinimumHeight(44)
        self.btn_start_mirror.setCursor(Qt.PointingHandCursor)
        self.btn_start_mirror.clicked.connect(lambda: self.launch_scrcpy(pinned_mode=False))
        left_layout.addWidget(self.btn_start_mirror)

        self.is_mirror_pinned = False
        self.btn_pin_mirror = QPushButton("📌 固定展示投屏 (置顶停靠桌面右侧)")
        self.btn_pin_mirror.setObjectName("PinButton")
        self.btn_pin_mirror.setMinimumHeight(40)
        self.btn_pin_mirror.setCursor(Qt.PointingHandCursor)
        self.btn_pin_mirror.clicked.connect(self.toggle_pin_mirror_window)
        left_layout.addWidget(self.btn_pin_mirror)

        # 分割线
        line1 = QFrame()
        line1.setFrameShape(QFrame.HLine)
        line1.setStyleSheet("color: #E2E8F0;")
        left_layout.addWidget(line1)

        # 快捷操作区
        title_remote = QLabel("🎮 手机快捷控制与截屏")
        title_remote.setStyleSheet("font-size: 15px; font-weight: bold; color: #0F172A;")
        left_layout.addWidget(title_remote)

        self.btn_quick_shot = QPushButton("📸 一键截取手机屏幕存到桌面")
        self.btn_quick_shot.setObjectName("AccentButton")
        self.btn_quick_shot.setMinimumHeight(38)
        self.btn_quick_shot.setCursor(Qt.PointingHandCursor)
        self.btn_quick_shot.clicked.connect(self.quick_screenshot_to_desktop)
        left_layout.addWidget(self.btn_quick_shot)

        nav_row1 = QHBoxLayout()
        btn_back = QPushButton("🔙 返回")
        btn_home = QPushButton("🏠 桌面")
        btn_recent = QPushButton("🗂️ 多任务")
        btn_back.clicked.connect(lambda: self.send_keyevent(4))
        btn_home.clicked.connect(lambda: self.send_keyevent(3))
        btn_recent.clicked.connect(lambda: self.send_keyevent(187))
        nav_row1.addWidget(btn_back)
        nav_row1.addWidget(btn_home)
        nav_row1.addWidget(btn_recent)
        left_layout.addLayout(nav_row1)

        nav_row2 = QHBoxLayout()
        btn_power = QPushButton("🔆 电源/锁屏")
        btn_vol_up = QPushButton("🔊 音量+")
        btn_vol_down = QPushButton("🔉 音量-")
        btn_power.clicked.connect(lambda: self.send_keyevent(26))
        btn_vol_up.clicked.connect(lambda: self.send_keyevent(24))
        btn_vol_down.clicked.connect(lambda: self.send_keyevent(25))
        nav_row2.addWidget(btn_power)
        nav_row2.addWidget(btn_vol_up)
        nav_row2.addWidget(btn_vol_down)
        left_layout.addLayout(nav_row2)

        # 投屏快捷键小贴士
        tips_box = QLabel(
            "💡 <b>投屏窗口实用技巧：</b><br>"
            "• <b>鼠标左键</b>：点击 / 滑动手机屏幕<br>"
            "• <b>鼠标右键</b>：等于手机【返回键】<br>"
            "• <b>鼠标中键</b>：等于手机【回到桌面】<br>"
            "• <b>Alt + O</b>：手机息屏（电脑继续显示）<br>"
            "• <b>Alt + F</b>：切换全屏投屏模式<br>"
            "• <b>拖拽电脑文件到投屏窗</b>：直接传到手机 Download 目录或安装 APK"
        )
        tips_box.setWordWrap(True)
        tips_box.setStyleSheet(
            "background-color: #F8FAFC; border: 1px solid #E2E8F0; "
            "border-radius: 8px; padding: 10px; font-size: 11.5px; color: #334155;"
        )
        left_layout.addWidget(tips_box)
        left_layout.addStretch()

        # ---------- 右侧：手机相册（图片 & 视频）浏览器与导出面板 ----------
        right_panel = QFrame()
        right_panel.setObjectName("RightPanel")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(16, 16, 16, 16)
        right_layout.setSpacing(10)

        # 标题与过滤行
        filter_bar = QHBoxLayout()
        title_gallery = QLabel("🖼️ 手机相册图片与视频管理")
        title_gallery.setStyleSheet("font-size: 16px; font-weight: bold; color: #0F172A;")
        filter_bar.addWidget(title_gallery)
        filter_bar.addSpacing(12)

        filter_bar.addWidget(QLabel("相册分类:"))
        self.combo_album = QComboBox()
        self.combo_album.setMinimumWidth(220)
        self.combo_album.currentIndexChanged.connect(self.apply_filters)
        filter_bar.addWidget(self.combo_album)

        filter_bar.addWidget(QLabel("类型:"))
        self.combo_type = QComboBox()
        self.combo_type.addItems(["全部 (图片+视频)", "仅看图片 (JPG/PNG/WEBP)", "仅看视频 (MP4/MOV)"])
        self.combo_type.currentIndexChanged.connect(self.apply_filters)
        filter_bar.addWidget(self.combo_type)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 搜索文件名...")
        self.search_input.setMaximumWidth(180)
        self.search_input.textChanged.connect(self.apply_filters)
        filter_bar.addWidget(self.search_input)

        self.chk_show_root_temp = QCheckBox("显示根目录临时截图")
        self.chk_show_root_temp.setChecked(False)
        self.chk_show_root_temp.stateChanged.connect(self.refresh_device_and_gallery)
        filter_bar.addWidget(self.chk_show_root_temp)

        filter_bar.addStretch()
        right_layout.addLayout(filter_bar)

        # 统计与批量选择工具条
        sel_bar = QHBoxLayout()
        self.stats_label = QLabel("正在读取手机相册...")
        self.stats_label.setStyleSheet("font-size: 12.5px; color: #475569; font-weight: 500;")
        sel_bar.addWidget(self.stats_label)
        sel_bar.addStretch()

        btn_sel_all = QPushButton("☑️ 全选当前视图")
        btn_sel_all.clicked.connect(self.select_all_items)
        btn_sel_none = QPushButton("🔲 取消全选")
        btn_sel_none.clicked.connect(self.deselect_all_items)
        btn_sel_videos = QPushButton("🎬 仅勾选视频")
        btn_sel_videos.clicked.connect(self.select_only_videos)
        btn_sel_images = QPushButton("🖼️ 仅勾选图片")
        btn_sel_images.clicked.connect(self.select_only_images)

        sel_bar.addWidget(btn_sel_all)
        sel_bar.addWidget(btn_sel_none)
        sel_bar.addWidget(btn_sel_videos)
        sel_bar.addWidget(btn_sel_images)
        right_layout.addLayout(sel_bar)

        # 缩略图网格列表 QListWidget
        self.gallery_list = QListWidget()
        self.gallery_list.setViewMode(QListWidget.IconMode)
        self.gallery_list.setIconSize(QSize(160, 130))
        self.gallery_list.setGridSize(QSize(195, 200))
        self.gallery_list.setResizeMode(QListWidget.Adjust)
        self.gallery_list.setMovement(QListWidget.Static)
        self.gallery_list.setSpacing(10)
        self.gallery_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.gallery_list.itemDoubleClicked.connect(self.preview_item_on_pc)
        self.gallery_list.itemChanged.connect(self.update_selection_count)
        self.gallery_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.gallery_list.customContextMenuRequested.connect(self.show_item_context_menu)
        right_layout.addWidget(self.gallery_list, 1)

        # 底部电脑保存路径与导出操作栏
        export_box = QFrame()
        export_box.setObjectName("ExportBox")
        export_layout = QVBoxLayout(export_box)
        export_layout.setContentsMargins(12, 10, 12, 10)
        export_layout.setSpacing(8)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("💻 电脑保存目录:"))
        self.export_path_edit = QLineEdit(str(DEFAULT_EXPORT_DIR))
        path_row.addWidget(self.export_path_edit, 1)

        btn_choose_dir = QPushButton("📁 更改目录...")
        btn_choose_dir.clicked.connect(self.choose_export_dir)
        btn_open_dir = QPushButton("📂 打开保存文件夹")
        btn_open_dir.clicked.connect(self.open_export_dir)

        self.chk_keep_album = QCheckBox("按相册名分文件夹保存")
        self.chk_keep_album.setChecked(True)
        self.chk_skip_exist = QCheckBox("跳过已导出的同名文件")
        self.chk_skip_exist.setChecked(True)

        path_row.addWidget(btn_choose_dir)
        path_row.addWidget(btn_open_dir)
        path_row.addWidget(self.chk_keep_album)
        path_row.addWidget(self.chk_skip_exist)
        export_layout.addLayout(path_row)

        action_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumHeight(24)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("准备就绪 (双击任意照片或视频可在电脑直接预览)")
        action_row.addWidget(self.progress_bar, 1)

        self.btn_export_selected = QPushButton("📥 导出勾选的文件到电脑 (0)")
        self.btn_export_selected.setObjectName("AccentButton")
        self.btn_export_selected.setMinimumHeight(40)
        self.btn_export_selected.setMinimumWidth(210)
        self.btn_export_selected.setCursor(Qt.PointingHandCursor)
        self.btn_export_selected.clicked.connect(self.export_checked_items)

        self.btn_export_all = QPushButton("⚡ 一键导出当前相册全部到桌面")
        self.btn_export_all.setObjectName("PrimaryButton")
        self.btn_export_all.setMinimumHeight(40)
        self.btn_export_all.setMinimumWidth(230)
        self.btn_export_all.setCursor(Qt.PointingHandCursor)
        self.btn_export_all.clicked.connect(self.export_all_filtered_items)

        action_row.addWidget(self.btn_export_selected)
        action_row.addWidget(self.btn_export_all)
        export_layout.addLayout(action_row)

        right_layout.addWidget(export_box)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        main_layout.addWidget(splitter, 1)

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #F1F5F9;
                font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
            }
            QFrame#TopBar, QFrame#LeftPanel, QFrame#RightPanel, QFrame#ExportBox {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 10px;
            }
            QFrame#ExportBox {
                background-color: #F8FAFC;
            }
            QPushButton {
                background-color: #F8FAFC;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 6px 12px;
                color: #1E293B;
                font-size: 12.5px;
                font-weight: 500;
            }
            QPushButton:hover {
                background-color: #E2E8F0;
                border-color: #94A3B8;
            }
            QPushButton#PrimaryButton {
                background-color: #2563EB;
                border: 1px solid #1D4ED8;
                color: #FFFFFF;
                font-size: 14px;
                font-weight: bold;
                border-radius: 8px;
            }
            QPushButton#PrimaryButton:hover {
                background-color: #1D4ED8;
            }
            QPushButton#AccentButton {
                background-color: #059669;
                border: 1px solid #047857;
                color: #FFFFFF;
                font-size: 13px;
                font-weight: bold;
                border-radius: 8px;
            }
            QPushButton#AccentButton:hover {
                background-color: #047857;
            }
            QPushButton#PinButton {
                background-color: #7C3AED;
                border: 1px solid #6D28D9;
                color: #FFFFFF;
                font-size: 13px;
                font-weight: bold;
                border-radius: 8px;
            }
            QPushButton#PinButton:hover {
                background-color: #6D28D9;
            }
            QPushButton#PinButtonActive {
                background-color: #EA580C;
                border: 1px solid #C2410C;
                color: #FFFFFF;
                font-size: 13px;
                font-weight: bold;
                border-radius: 8px;
            }
            QPushButton#PinButtonActive:hover {
                background-color: #C2410C;
            }
            QComboBox, QLineEdit {
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 5px 8px;
                background-color: #FFFFFF;
                font-size: 12.5px;
            }
            QListWidget {
                border: 1px solid #E2E8F0;
                border-radius: 8px;
                background-color: #F8FAFC;
                padding: 8px;
            }
            QListWidget::item {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
                padding: 6px;
                color: #1E293B;
            }
            QListWidget::item:hover {
                border: 1px solid #60A5FA;
                background-color: #EFF6FF;
            }
            QListWidget::item:selected {
                border: 2px solid #2563EB;
                background-color: #DBEAFE;
                color: #1E3A8A;
            }
            QProgressBar {
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                text-align: center;
                background-color: #FFFFFF;
                font-size: 12px;
                color: #0F172A;
            }
            QProgressBar::chunk {
                background-color: #3B82F6;
                border-radius: 5px;
            }
        """)

    def open_compressor_tool(self):
        try:
            from media_compressor import MediaCompressorWindow
            export_dir = Path(self.export_path_edit.text().strip() or str(DEFAULT_EXPORT_DIR))
            init_paths = [export_dir] if export_dir.exists() else None
            self.compressor_win = MediaCompressorWindow(initial_paths=init_paths)
            self.compressor_win.show()
            self.compressor_win.raise_()
            self.compressor_win.activateWindow()
        except Exception as e:
            QMessageBox.warning(self, "启动失败", f"无法打开压缩工具:\n{e}")

    # ================= 设备检测与连接 (USB + 5G Wi-Fi 双模防断连自愈) =================
    def detect_device(self):
        import re
        from launch_mirror import get_pc_current_wifi_credentials
        code, out, _ = run_adb(["devices", "-l"], timeout=10)
        if code != 0:
            return None, {}
        lines = out.strip().splitlines()[1:]
        usb_serial = None
        tcp_serial = None
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                s = parts[0]
                if ":" in s:
                    tcp_serial = s
                else:
                    usb_serial = s

        # 如果有 USB 连接但尚未建立 5G Wi-Fi TCP 投屏通道，自动唤醒手机 Wi-Fi 并激活 tcpip:5555
        if usb_serial and not tcp_serial:
            _, ip_out, _ = run_adb(["shell", "ip -4 addr show wlan0"], serial=usb_serial, timeout=5)
            m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", ip_out)
            if not m:
                ssid, key = get_pc_current_wifi_credentials()
                run_adb(["shell", "cmd wifi set-wifi-enabled enabled"], serial=usb_serial, timeout=5)
                if ssid and key:
                    run_adb(["shell", f"cmd wifi connect-network {ssid} wpa2 {key}"], serial=usb_serial, timeout=6)
                for _ in range(5):
                    time.sleep(0.8)
                    _, ip_out, _ = run_adb(["shell", "ip -4 addr show wlan0"], serial=usb_serial, timeout=4)
                    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", ip_out)
                    if m:
                        break
            if m:
                target_tcp = f"{m.group(1)}:5555"
                run_adb(["tcpip", "5555"], serial=usb_serial, timeout=6)
                time.sleep(1.0)
                run_adb(["connect", target_tcp], timeout=6)
                c_ok, echo_out, _ = run_adb(["shell", "echo ok"], serial=target_tcp, timeout=4)
                if c_ok == 0 and "ok" in echo_out:
                    tcp_serial = target_tcp

        # 如果都没有，尝试重连已知局域网 IP
        if not usb_serial and not tcp_serial:
            for cand in ["192.168.2.5:5555", "192.168.2.2:5555", "192.168.2.3:5555"]:
                run_adb(["connect", cand], timeout=3)
                c_ok, echo_out, _ = run_adb(["shell", "echo ok"], serial=cand, timeout=3)
                if c_ok == 0 and "ok" in echo_out:
                    tcp_serial = cand
                    break

        # 优先使用 5G Wi-Fi TCP 通道作为主通道（彻底规避电脑 USB 集线器瞬时供电跌落导致的断连闪退）
        chosen_serial = tcp_serial or usb_serial
        if not chosen_serial:
            return None, {}

        _, props_out, _ = run_adb(
            ["shell", "getprop ro.product.brand; getprop ro.product.model; getprop ro.build.version.release; wm size"],
            serial=chosen_serial,
            timeout=8,
        )
        prop_lines = [p.strip() for p in props_out.splitlines() if p.strip()]
        mode_tag = f"5G局域网防断连通道 ({tcp_serial})" if tcp_serial else f"USB直连 ({usb_serial})"
        info = {
            "brand": prop_lines[0] if len(prop_lines) > 0 else "Samsung",
            "model": prop_lines[1] if len(prop_lines) > 1 else chosen_serial,
            "android": prop_lines[2] if len(prop_lines) > 2 else "",
            "size": prop_lines[3].replace("Physical size:", "").strip() if len(prop_lines) > 3 else "",
            "mode_tag": mode_tag,
        }
        return chosen_serial, info

    def refresh_device_and_gallery(self):
        self.device_label.setText("正在检测三星手机连接...")
        QApplication.processEvents()

        serial, info = self.detect_device()
        self.current_serial = serial
        if not serial:
            self.status_dot.setStyleSheet("color: #EF4444; font-size: 18px; font-weight: bold;")
            self.device_label.setText("未检测到手机连接 (请检查 USB 数据线并确认开启 USB 调试)")
            self.device_detail_label.setText("")
            self.stats_label.setText("设备未连接")
            self.gallery_list.clear()
            return

        self.status_dot.setStyleSheet("color: #10B981; font-size: 18px; font-weight: bold;")
        self.device_label.setText(f"已连接: {info.get('brand', 'Samsung').upper()} {info.get('model', '')}")
        self.device_detail_label.setText(
            f"Android {info.get('android', '')}  |  分辨率: {info.get('size', '')}  |  模式: {info.get('mode_tag', serial)}"
        )

        # 启动后台扫描相册
        self.stats_label.setText("正在扫描手机相册中的图片与视频，请稍候...")
        if self.thumb_worker and self.thumb_worker.isRunning():
            self.thumb_worker.stop()
            self.thumb_worker.wait(1000)

        self.scan_worker = ScanWorker(serial, include_root_temp=self.chk_show_root_temp.isChecked())
        self.scan_worker.finished_signal.connect(self.on_scan_finished)
        self.scan_worker.start()

    def on_scan_finished(self, items: list, err_msg: str):
        if err_msg:
            self.stats_label.setText(err_msg)
            return
        self.all_media_items = items

        # 统计各相册数量并填充下拉框
        album_counts = {}
        for it in items:
            album_counts[it.album] = album_counts.get(it.album, 0) + 1

        prev_album = self.combo_album.currentData()
        self.combo_album.blockSignals(True)
        self.combo_album.clear()
        self.combo_album.addItem(f"📚 全部手机相册 ({len(items)})", "ALL")
        for alb, cnt in sorted(album_counts.items(), key=lambda x: -x[1]):
            self.combo_album.addItem(f"{alb} ({cnt})", alb)

        if prev_album:
            idx = self.combo_album.findData(prev_album)
            if idx >= 0:
                self.combo_album.setCurrentIndex(idx)
        self.combo_album.blockSignals(False)

        self.apply_filters()

    def apply_filters(self):
        if self.thumb_worker and self.thumb_worker.isRunning():
            self.thumb_worker.stop()
            self.thumb_worker.wait(800)

        alb_filter = self.combo_album.currentData()
        type_idx = self.combo_type.currentIndex()  # 0: 全部, 1: 图片, 2: 视频
        kw = self.search_input.text().strip().lower()

        filtered = []
        for it in self.all_media_items:
            if alb_filter and alb_filter != "ALL" and it.album != alb_filter:
                continue
            if type_idx == 1 and it.is_video:
                continue
            if type_idx == 2 and not it.is_video:
                continue
            if kw and kw not in it.name.lower():
                continue
            filtered.append(it)

        self.filtered_items = filtered
        self.populate_gallery_list()

    def populate_gallery_list(self):
        self.gallery_list.blockSignals(True)
        self.gallery_list.clear()
        self.item_widgets.clear()

        img_cnt = sum(1 for x in self.filtered_items if not x.is_video)
        vid_cnt = sum(1 for x in self.filtered_items if x.is_video)
        total_bytes = sum(x.size for x in self.filtered_items)

        for it in self.filtered_items:
            tag = "[视频]" if it.is_video else "[图片]"
            short_name = it.name if len(it.name) <= 22 else (it.name[:10] + "..." + it.name[-9:])
            display_text = f"{short_name}\n{tag} {format_size(it.size)}\n{it.dt_str}"

            icon = self.video_icon if it.is_video else self.image_icon
            lw_item = QListWidgetItem(icon, display_text)
            lw_item.setFlags(lw_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            lw_item.setCheckState(Qt.Unchecked)
            lw_item.setData(Qt.UserRole, it)
            lw_item.setToolTip(
                f"文件名: {it.name}\n"
                f"所属相册: {it.album}\n"
                f"手机路径: {it.remote_path}\n"
                f"文件大小: {format_size(it.size)}\n"
                f"修改时间: {it.dt_str}\n"
                f"💡 双击可在电脑上直接打开预览，勾选可批量导出"
            )
            self.gallery_list.addItem(lw_item)
            self.item_widgets[it.remote_path] = lw_item

        self.gallery_list.blockSignals(False)
        self.stats_label.setText(
            f"当前显示: 共 {len(self.filtered_items)} 项 （🖼️ 图片 {img_cnt} 张  |  🎬 视频 {vid_cnt} 个  |  总大小 {format_size(total_bytes)}）"
        )
        self.update_selection_count()

        # 启动缩略图异步加载线程
        if self.current_serial and self.filtered_items:
            self.thumb_worker = ThumbnailWorker(self.current_serial, list(self.filtered_items))
            self.thumb_worker.thumb_ready.connect(self.on_thumbnail_ready)
            self.thumb_worker.start()

    def on_thumbnail_ready(self, remote_path: str, local_thumb: str):
        lw_item = self.item_widgets.get(remote_path)
        if lw_item and os.path.exists(local_thumb):
            pix = QPixmap(local_thumb)
            if not pix.isNull():
                lw_item.setIcon(QIcon(pix))

    # ================= 选择与勾选控制 =================
    def update_selection_count(self):
        checked_count = 0
        for i in range(self.gallery_list.count()):
            if self.gallery_list.item(i).checkState() == Qt.Checked:
                checked_count += 1
        self.btn_export_selected.setText(f"📥 导出勾选的文件到电脑 ({checked_count})")

    def select_all_items(self):
        self.gallery_list.blockSignals(True)
        for i in range(self.gallery_list.count()):
            self.gallery_list.item(i).setCheckState(Qt.Checked)
        self.gallery_list.blockSignals(False)
        self.update_selection_count()

    def deselect_all_items(self):
        self.gallery_list.blockSignals(True)
        for i in range(self.gallery_list.count()):
            self.gallery_list.item(i).setCheckState(Qt.Unchecked)
        self.gallery_list.blockSignals(False)
        self.update_selection_count()

    def select_only_videos(self):
        self.gallery_list.blockSignals(True)
        for i in range(self.gallery_list.count()):
            lw = self.gallery_list.item(i)
            it = lw.data(Qt.UserRole)
            lw.setCheckState(Qt.Checked if it.is_video else Qt.Unchecked)
        self.gallery_list.blockSignals(False)
        self.update_selection_count()

    def select_only_images(self):
        self.gallery_list.blockSignals(True)
        for i in range(self.gallery_list.count()):
            lw = self.gallery_list.item(i)
            it = lw.data(Qt.UserRole)
            lw.setCheckState(Qt.Checked if not it.is_video else Qt.Unchecked)
        self.gallery_list.blockSignals(False)
        self.update_selection_count()

    # ================= 投屏与手机快捷操作 =================
    def _find_scrcpy_hwnd(self):
        """查找当前正在运行的 Samsung 实时投屏窗口句柄"""
        if sys.platform != "win32":
            return 0
        import ctypes
        user32 = ctypes.windll.user32
        if self.current_serial:
            title = f"Samsung 实时投屏 ({self.current_serial})"
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                return hwnd
        # 枚举模糊匹配标题包含 'Samsung 实时投屏' 的窗口
        found_hwnd = [0]
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum_cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if "Samsung 实时投屏" in buf.value:
                        found_hwnd[0] = hwnd
                        return False
            return True
        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        return found_hwnd[0]

    def _update_pin_button_ui(self, pinned: bool):
        self.is_mirror_pinned = pinned
        self.chk_always_on_top.blockSignals(True)
        self.chk_always_on_top.setChecked(pinned)
        self.chk_always_on_top.blockSignals(False)
        if pinned:
            self.btn_pin_mirror.setText("📌 已固定置顶展示 (点击解除固定)")
            self.btn_pin_mirror.setObjectName("PinButtonActive")
        else:
            self.btn_pin_mirror.setText("📌 固定展示投屏 (置顶停靠桌面右侧)")
            self.btn_pin_mirror.setObjectName("PinButton")
        self.btn_pin_mirror.style().unpolish(self.btn_pin_mirror)
        self.btn_pin_mirror.style().polish(self.btn_pin_mirror)

    def toggle_pin_mirror_window(self):
        """一键固定展示投屏窗口（若未启动则直接以置顶+停靠屏幕右侧启动；若已启动则实时切换置顶与停靠状态）"""
        hwnd = self._find_scrcpy_hwnd()
        if not hwnd or (self.scrcpy_proc and self.scrcpy_proc.poll() is not None):
            # 投屏未运行，直接以固定置顶+停靠右侧模式启动
            self._update_pin_button_ui(True)
            self.launch_scrcpy(pinned_mode=True)
            return

        import ctypes
        user32 = ctypes.windll.user32
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_SHOWWINDOW = 0x0040

        if not self.is_mirror_pinned:
            # 切换为固定展示：置顶并自动停靠到屏幕右侧
            screen_geo = QApplication.primaryScreen().availableGeometry()
            win_w = 400
            win_h = min(860, screen_geo.height() - 60)
            target_x = max(0, screen_geo.width() - win_w - 24)
            target_y = max(30, (screen_geo.height() - win_h) // 2)
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetWindowPos(hwnd, HWND_TOPMOST, target_x, target_y, win_w, win_h, SWP_SHOWWINDOW)
            self._update_pin_button_ui(True)
            self.progress_bar.setFormat("📌 投屏窗口已固定在桌面最前端（右侧停靠展示），切换其他软件也不会被遮挡")
        else:
            # 解除固定置顶
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            self._update_pin_button_ui(False)
            self.progress_bar.setFormat("🔓 已解除投屏窗口固定置顶状态")

    def launch_scrcpy(self, pinned_mode: bool = False):
        if not self.current_serial:
            self.refresh_device_and_gallery()
            if not self.current_serial:
                QMessageBox.warning(self, "未检测到手机", "请先连接您的三星手机并确保开启了 USB 调试！")
                return

        if not SCRCPY_EXE.exists():
            QMessageBox.critical(self, "组件缺失", f"未找到投屏引擎: {SCRCPY_EXE}")
            return

        # 如果已有投屏窗口运行，避免重复开多个导致冲突
        if self.scrcpy_proc and self.scrcpy_proc.poll() is None:
            try:
                self.scrcpy_proc.terminate()
                self.scrcpy_proc.wait(timeout=2)
            except Exception:
                pass

        # 启动投屏前暂停后台缩略图拉取，避免并发占用 USB 集线器带宽导致投屏断连
        if self.thumb_worker and self.thumb_worker.isRunning():
            self.thumb_worker.stop()
            self.thumb_worker.wait(800)

        cmd = [
            str(SCRCPY_EXE),
            "-s", self.current_serial,
            "--window-title", f"Samsung 实时投屏 ({self.current_serial})",
            "--shortcut-mod=lalt,ralt",
            "--video-codec=h265",
        ]

        q_idx = self.combo_quality.currentIndex()
        if q_idx == 0:
            cmd.extend(["--max-fps=60", "--video-bit-rate=2M", "--max-size=1280"])
        elif q_idx == 1:
            cmd.extend(["--max-fps=60", "--video-bit-rate=2500K", "--max-size=1600"])
        else:
            cmd.extend(["--max-fps=60", "--video-bit-rate=1M", "--max-size=960"])

        if self.chk_stay_awake.isChecked():
            cmd.append("--stay-awake")
        if self.chk_turn_screen_off.isChecked():
            cmd.append("--turn-screen-off")

        if pinned_mode or self.chk_always_on_top.isChecked():
            cmd.append("--always-on-top")
            screen_geo = QApplication.primaryScreen().availableGeometry()
            win_w = 400
            win_h = min(860, screen_geo.height() - 60)
            target_x = max(0, screen_geo.width() - win_w - 24)
            target_y = max(30, (screen_geo.height() - win_h) // 2)
            cmd.extend([
                f"--window-x={target_x}",
                f"--window-y={target_y}",
                f"--window-width={win_w}",
                f"--window-height={win_h}",
            ])
            self._update_pin_button_ui(True)
        else:
            self._update_pin_button_ui(False)

        if self.chk_enable_audio.isChecked():
            cmd.append("--audio-codec=aac")
        else:
            cmd.append("--no-audio")

        env = os.environ.copy()
        env["ADB"] = str(ADB_EXE)

        try:
            self.scrcpy_proc = subprocess.Popen(
                cmd,
                cwd=str(SCRCPY_DIR),
                env=env,
                creationflags=CREATION_FLAGS,
            )
            if pinned_mode or self.is_mirror_pinned:
                self.progress_bar.setFormat("📌 固定展示投屏已启动！窗口已置顶停靠在桌面右侧，永不被其他窗口遮挡")
            else:
                self.progress_bar.setFormat("🚀 高清投屏窗口已启动！您可以在独立窗口中直接用鼠标和键盘操控三星手机")
        except Exception as e:
            QMessageBox.critical(self, "启动投屏失败", f"无法启动 scrcpy 投屏:\n{e}")

    def send_keyevent(self, keycode: int):
        if not self.current_serial:
            return
        run_adb(["shell", f"input keyevent {keycode}"], serial=self.current_serial, timeout=5)

    def quick_screenshot_to_desktop(self):
        if not self.current_serial:
            QMessageBox.warning(self, "提示", "未检测到三星手机连接！")
            return
        export_dir = Path(self.export_path_edit.text().strip() or str(DEFAULT_EXPORT_DIR)) / "快捷截屏"
        export_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_png = export_dir / f"Samsung_Screenshot_{ts}.png"
        remote_tmp = "/sdcard/samsung_quick_shot.png"

        self.progress_bar.setFormat("📸 正在截取三星手机当前画面...")
        QApplication.processEvents()
        run_adb(["shell", "screencap", "-p", remote_tmp], serial=self.current_serial, timeout=10)
        code, _, _ = run_adb(["pull", remote_tmp, str(local_png)], serial=self.current_serial, timeout=10)
        run_adb(["shell", "rm", "-f", remote_tmp], serial=self.current_serial, timeout=5)

        if code == 0 and local_png.exists():
            self.progress_bar.setFormat(f"✅ 截屏已保存至: {local_png}")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(local_png)))
        else:
            QMessageBox.warning(self, "截屏失败", "无法从手机获取截屏，请检查手机连接状态。")

    # ================= 双击预览与右键菜单 =================
    def preview_item_on_pc(self, lw_item: QListWidgetItem):
        it: MediaItem = lw_item.data(Qt.UserRole)
        if not it or not self.current_serial:
            return
        preview_dir = Path(self.export_path_edit.text().strip() or str(DEFAULT_EXPORT_DIR)) / os.path.basename(it.folder)
        preview_dir.mkdir(parents=True, exist_ok=True)
        local_file = preview_dir / it.name

        if not local_file.exists() or local_file.stat().st_size != it.size:
            self.progress_bar.setFormat(f"⏳ 正在传输 {it.name} 到电脑以便打开预览...")
            QApplication.processEvents()
            timeout_sec = max(30, int(it.size / (2 * 1024 * 1024)) + 30)
            code, _, _ = run_adb(["pull", "-a", it.remote_path, str(local_file)], serial=self.current_serial, timeout=timeout_sec)
            if code != 0 or not local_file.exists():
                QMessageBox.warning(self, "预览失败", f"无法从手机拉取文件:\n{it.remote_path}")
                return

        self.progress_bar.setFormat(f"✅ 已拉取并打开: {local_file}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(local_file)))

    def show_item_context_menu(self, pos):
        lw_item = self.gallery_list.itemAt(pos)
        if not lw_item:
            return
        it: MediaItem = lw_item.data(Qt.UserRole)
        menu = QMenu(self)
        act_open = QAction("🔍 下载到电脑并立即打开预览", self)
        act_export_one = QAction("📥 立即导出此文件到桌面导出目录", self)
        act_toggle = QAction("☑️ 切换勾选状态", self)

        act_open.triggered.connect(lambda: self.preview_item_on_pc(lw_item))
        act_export_one.triggered.connect(lambda: self.start_export([it]))
        act_toggle.triggered.connect(
            lambda: lw_item.setCheckState(Qt.Unchecked if lw_item.checkState() == Qt.Checked else Qt.Checked)
        )

        menu.addAction(act_open)
        menu.addAction(act_export_one)
        menu.addSeparator()
        menu.addAction(act_toggle)
        menu.exec_(self.gallery_list.mapToGlobal(pos))

    # ================= 导出照片与视频到电脑 =================
    def choose_export_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出到电脑的文件夹", self.export_path_edit.text())
        if d:
            self.export_path_edit.setText(d)

    def open_export_dir(self):
        target = Path(self.export_path_edit.text().strip() or str(DEFAULT_EXPORT_DIR))
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def export_checked_items(self):
        checked_items = []
        for i in range(self.gallery_list.count()):
            lw = self.gallery_list.item(i)
            if lw.checkState() == Qt.Checked:
                checked_items.append(lw.data(Qt.UserRole))

        # 如果用户没有点复选框，但用鼠标高亮选中了若干项，也支持直接导出高亮选中项
        if not checked_items:
            for lw in self.gallery_list.selectedItems():
                checked_items.append(lw.data(Qt.UserRole))

        if not checked_items:
            QMessageBox.information(
                self,
                "请先选择文件",
                "请先勾选需要导出的图片或视频（或直接点击右侧【⚡ 一键导出当前相册全部到桌面】）！",
            )
            return

        self.start_export(checked_items)

    def export_all_filtered_items(self):
        if not self.filtered_items:
            QMessageBox.information(self, "提示", "当前相册视图中没有可导出的文件。")
            return
        self.start_export(list(self.filtered_items))

    def start_export(self, items_to_export: list):
        if not self.current_serial:
            QMessageBox.warning(self, "未连接", "未检测到三星手机连接！")
            return
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, "正在导出", "当前已有导出任务正在进行中，请稍候。")
            return

        dest_dir = Path(self.export_path_edit.text().strip() or str(DEFAULT_EXPORT_DIR))
        self.btn_export_selected.setEnabled(False)
        self.btn_export_all.setEnabled(False)
        self.progress_bar.setMaximum(len(items_to_export))
        self.progress_bar.setValue(0)

        self.export_worker = ExportWorker(
            self.current_serial,
            items_to_export,
            dest_dir,
            keep_album_folders=self.chk_keep_album.isChecked(),
            skip_existing=self.chk_skip_exist.isChecked(),
        )
        self.export_worker.progress_signal.connect(self.on_export_progress)
        self.export_worker.finished_signal.connect(self.on_export_finished)
        self.export_worker.start()

    def on_export_progress(self, current: int, total: int, filename: str):
        self.progress_bar.setValue(current)
        self.progress_bar.setFormat(f"正在导出 ({current}/{total}): {filename}")

    def on_export_finished(self, success: int, skipped: int, failed: int, export_dir: str):
        self.btn_export_selected.setEnabled(True)
        self.btn_export_all.setEnabled(True)
        self.progress_bar.setFormat(
            f"🎉 导出完成！成功导出: {success} 个，跳过已存在: {skipped} 个，失败: {failed} 个"
        )
        reply = QMessageBox.question(
            self,
            "导出完成",
            f"手机相册导出完成！\n\n"
            f"• 成功新导出：{success} 个文件\n"
            f"• 增量跳过（已存在）：{skipped} 个文件\n"
            f"• 失败：{failed} 个文件\n\n"
            f"文件已保存在电脑：\n{export_dir}\n\n"
            f"是否立即打开电脑上的保存文件夹？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(export_dir))


def main():
    # 开启高分屏缩放自适应
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    win = SamsungStudioWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
