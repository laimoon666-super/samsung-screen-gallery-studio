#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片与视频极速压缩工具 (GPU 硬件加速 & 批量智能瘦身)
支持：
1. 拖拽文件或文件夹直接添加、一键载入手机导出的相册目录
2. 图片智能压缩：支持 JPG / PNG / WebP / BMP，支持画质调节、尺寸等比缩放、PNG 智能有损瘦身或转 WebP/JPG
3. 视频极速压缩：内置 FFmpeg + NVIDIA NVENC GPU 硬件加速 (h264_nvenc / hevc_nvenc) 与 CPU (libx264 / libx265)，
   支持按画质档位 (CRF/CQ)、分辨率 (原画/1080P/720P)、帧率 (原帧率/30FPS) 批量瘦身
4. 实时显示压缩前后体积对比、节省百分比与总计节省空间，双击可直接预览压缩结果
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QComboBox, QSlider, QLineEdit, QProgressBar, QFileDialog,
    QMessageBox, QCheckBox, QFrame, QGroupBox, QAbstractItemView
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QUrl
from PyQt5.QtGui import QIcon, QColor, QFont, QDesktopServices
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".3gp", ".flv", ".m4v"}
CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def get_desktop_dir() -> Path:
    onedrive_desktop = Path(os.path.expanduser("~")) / "OneDrive" / "Desktop"
    local_desktop = Path(os.path.expanduser("~")) / "Desktop"
    if onedrive_desktop.exists():
        return onedrive_desktop
    if local_desktop.exists():
        return local_desktop
    return BASE_DIR


def find_ffmpeg() -> str:
    path_ff = shutil.which("ffmpeg")
    if path_ff:
        return path_ff
    winget_ff = Path(
        os.path.expanduser(
            r"~\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"
        )
    )
    if winget_ff.exists():
        return str(winget_ff)
    return "ffmpeg"


FFMPEG_EXE = find_ffmpeg()


def format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


class CompressTaskItem:
    def __init__(self, src_path: Path):
        self.src_path = src_path
        self.name = src_path.name
        self.ext = src_path.suffix.lower()
        self.is_video = self.ext in VIDEO_EXTS
        self.orig_size = src_path.stat().st_size if src_path.exists() else 0
        self.out_path: Path = None
        self.new_size = 0
        self.status = "待压缩"


class CompressWorker(QThread):
    item_started = pyqtSignal(int, str)  # (row_idx, status_text)
    item_finished = pyqtSignal(int, bool, int, str, str)  # (row_idx, ok, new_size, out_path, msg)
    all_finished = pyqtSignal(int, int, int, int, str)  # (ok_cnt, fail_cnt, total_orig, total_new, out_dir)

    def __init__(self, tasks: list, config: dict):
        super().__init__()
        self.tasks = tasks
        self.config = config
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        out_root = Path(self.config["out_dir"])
        out_root.mkdir(parents=True, exist_ok=True)

        ok_cnt = 0
        fail_cnt = 0
        total_orig = 0
        total_new = 0

        for idx, task in enumerate(self.tasks):
            if self._stop:
                break
            self.item_started.emit(idx, "⏳ 正在压缩...")
            try:
                if task.is_video:
                    ok, out_file, msg = self._compress_video(task, out_root)
                else:
                    ok, out_file, msg = self._compress_image(task, out_root)

                if ok and out_file and out_file.exists():
                    new_sz = out_file.stat().st_size
                    # 如果勾选了“若压缩后反而变大则保留原文件”
                    if self.config.get("keep_smaller", True) and new_sz >= task.orig_size and out_file.suffix.lower() == task.ext:
                        shutil.copy2(task.src_path, out_file)
                        new_sz = out_file.stat().st_size
                        msg = "已为最优体积(保留原图)"

                    task.out_path = out_file
                    task.new_size = new_sz
                    ok_cnt += 1
                    total_orig += task.orig_size
                    total_new += new_sz
                    self.item_finished.emit(idx, True, new_sz, str(out_file), msg or "✅ 完成")
                else:
                    fail_cnt += 1
                    self.item_finished.emit(idx, False, 0, "", msg or "❌ 失败")
            except Exception as e:
                fail_cnt += 1
                self.item_finished.emit(idx, False, 0, "", f"❌ 异常: {e}")

        self.all_finished.emit(ok_cnt, fail_cnt, total_orig, total_new, str(out_root))

    def _compress_image(self, task: CompressTaskItem, out_root: Path):
        img_fmt_mode = self.config["img_format"]  # "keep", "webp", "jpg"
        quality = self.config["img_quality"]  # 10..95
        max_dim = self.config["img_max_dim"]  # 0, 2560, 1920, 1280

        stem = task.src_path.stem
        if img_fmt_mode == "webp":
            out_ext = ".webp"
        elif img_fmt_mode == "jpg":
            out_ext = ".jpg"
        else:
            out_ext = task.ext if task.ext in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"

        out_file = out_root / f"{stem}_compressed{out_ext}"
        with Image.open(task.src_path) as im:
            # Resize if needed
            w, h = im.size
            if max_dim > 0 and max(w, h) > max_dim:
                if w >= h:
                    new_w = max_dim
                    new_h = int(h * (max_dim / w))
                else:
                    new_h = max_dim
                    new_w = int(w * (max_dim / h))
                im = im.resize((new_w, new_h), Image.LANCZOS)

            if out_ext in {".jpg", ".jpeg"}:
                if im.mode in ("RGBA", "LA", "P"):
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    if im.mode == "P":
                        im = im.convert("RGBA")
                    bg.paste(im, mask=im.split()[-1] if "A" in im.mode else None)
                    im = bg
                else:
                    im = im.convert("RGB")
                im.save(out_file, "JPEG", quality=quality, optimize=True, progressive=True)
            elif out_ext == ".webp":
                im.save(out_file, "WEBP", quality=quality, method=4)
            elif out_ext == ".png":
                # PNG 智能调色板有损量化瘦身（体积可减小 60%~80%）
                if quality < 90:
                    colors = max(32, min(256, int(256 * (quality / 95.0))))
                    im_q = im.quantize(colors=colors, method=Image.MEDIANCUT)
                    im_q.save(out_file, "PNG", optimize=True)
                else:
                    im.save(out_file, "PNG", optimize=True)

        return True, out_file, "✅ 图片完成"

    def _compress_video(self, task: CompressTaskItem, out_root: Path):
        encoder_mode = self.config["vid_encoder"]  # "nvenc_h264", "nvenc_hevc", "cpu_x264", "cpu_x265"
        crf_val = self.config["vid_crf"]  # e.g., 23, 26, 30, 34
        max_res = self.config["vid_max_res"]  # 0, 1920, 1280, 960
        fps_limit = self.config["vid_fps"]  # 0, 30, 24

        stem = task.src_path.stem
        out_file = out_root / f"{stem}_compressed.mp4"

        cmd = [FFMPEG_EXE, "-y", "-hide_banner", "-loglevel", "error", "-i", str(task.src_path)]

        # 视频滤镜：等比缩放长边并保证宽高为 2 的倍数
        vf_parts = []
        if max_res > 0:
            vf_parts.append(
                f"scale='if(gt(iw,ih),min({max_res},iw),-2)':'if(gt(iw,ih),-2,min({max_res},ih))'"
            )
        if fps_limit > 0:
            vf_parts.append(f"fps=fps='min(source_fps,{fps_limit})'")
        if vf_parts:
            cmd.extend(["-vf", ",".join(vf_parts)])

        if encoder_mode == "nvenc_h264":
            cmd.extend(["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", str(crf_val), "-preset", "p5"])
        elif encoder_mode == "nvenc_hevc":
            cmd.extend(["-c:v", "hevc_nvenc", "-rc", "vbr", "-cq", str(crf_val), "-preset", "p5", "-tag:v", "hvc1"])
        elif encoder_mode == "cpu_x265":
            cmd.extend(["-c:v", "libx265", "-crf", str(crf_val), "-preset", "medium", "-tag:v", "hvc1"])
        else:
            cmd.extend(["-c:v", "libx264", "-crf", str(crf_val), "-preset", "medium"])

        cmd.extend(["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_file)])

        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATION_FLAGS,
        )
        if res.returncode != 0 and "nvenc" in encoder_mode:
            # 自动回退到 CPU 编码确保万无一失
            fallback_cmd = [FFMPEG_EXE, "-y", "-hide_banner", "-loglevel", "error", "-i", str(task.src_path)]
            if vf_parts:
                fallback_cmd.extend(["-vf", ",".join(vf_parts)])
            fallback_cmd.extend([
                "-c:v", "libx264", "-crf", str(crf_val), "-preset", "fast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_file)
            ])
            res = subprocess.run(
                fallback_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATION_FLAGS,
            )

        if res.returncode == 0 and out_file.exists():
            return True, out_file, "✅ 视频完成"
        err_txt = res.stderr.decode("utf-8", errors="ignore").strip().splitlines()
        short_err = err_txt[-1] if err_txt else "编码失败"
        return False, None, f"❌ {short_err[:40]}"


class MediaCompressorWindow(QMainWindow):
    def __init__(self, initial_paths=None):
        super().__init__()
        self.setWindowTitle("🗜️ 图片与视频极速压缩工具 (NVIDIA GPU 硬件加速)")
        self.resize(1060, 720)
        self.setAcceptDrops(True)
        if (BASE_DIR / "app_icon.ico").exists():
            self.setWindowIcon(QIcon(str(BASE_DIR / "app_icon.ico")))

        self.tasks = []
        self.worker = None
        self.default_out_dir = get_desktop_dir() / "压缩输出文件夹"

        self._init_ui()
        self._apply_styles()

        if initial_paths:
            self.add_paths(initial_paths)

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(12)

        # ================= 1. 顶部文件添加栏 =================
        top_bar = QFrame()
        top_bar.setObjectName("CardFrame")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(14, 10, 14, 10)

        btn_add_files = QPushButton("➕ 添加图片 / 视频文件")
        btn_add_files.setObjectName("PrimaryButton")
        btn_add_files.setCursor(Qt.PointingHandCursor)
        btn_add_files.clicked.connect(self.browse_files)

        btn_add_folder = QPushButton("📁 添加整个文件夹")
        btn_add_folder.setCursor(Qt.PointingHandCursor)
        btn_add_folder.clicked.connect(self.browse_folder)

        btn_add_samsung = QPushButton("📱 一键导入桌面「三星手机相册导出」")
        btn_add_samsung.setObjectName("AccentButton")
        btn_add_samsung.setCursor(Qt.PointingHandCursor)
        btn_add_samsung.clicked.connect(self.import_samsung_export_dir)

        btn_clear = QPushButton("🗑️ 清空列表")
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.clicked.connect(self.clear_tasks)

        hint_lbl = QLabel("💡 支持直接把电脑上的图片、视频或文件夹拖拽到本窗口！")
        hint_lbl.setStyleSheet("color: #64748B; font-size: 12px;")

        top_layout.addWidget(btn_add_files)
        top_layout.addWidget(btn_add_folder)
        top_layout.addWidget(btn_add_samsung)
        top_layout.addWidget(btn_clear)
        top_layout.addStretch()
        top_layout.addWidget(hint_lbl)
        main_layout.addWidget(top_bar)

        # ================= 2. 中部图片与视频压缩参数配置区 =================
        cfg_row = QHBoxLayout()
        cfg_row.setSpacing(12)

        # --- 左：图片压缩设置 ---
        img_group = QGroupBox("🖼️ 图片压缩配置 (JPG / PNG / WebP)")
        img_layout = QVBoxLayout(img_group)
        img_layout.setSpacing(8)

        row_ifmt = QHBoxLayout()
        row_ifmt.addWidget(QLabel("输出格式:"))
        self.combo_img_fmt = QComboBox()
        self.combo_img_fmt.addItem("保持原格式 (PNG智能量化瘦身)", "keep")
        self.combo_img_fmt.addItem("统一转为 WebP (体积最小，推荐)", "webp")
        self.combo_img_fmt.addItem("统一转为 JPG (兼容性最好)", "jpg")
        row_ifmt.addWidget(self.combo_img_fmt, 1)
        img_layout.addLayout(row_ifmt)

        row_iq = QHBoxLayout()
        self.lbl_img_q = QLabel("压缩画质: 75% (高画质高压缩)")
        self.slider_img_q = QSlider(Qt.Horizontal)
        self.slider_img_q.setRange(20, 95)
        self.slider_img_q.setValue(75)
        self.slider_img_q.valueChanged.connect(self._on_img_q_changed)
        row_iq.addWidget(self.lbl_img_q)
        row_iq.addWidget(self.slider_img_q, 1)
        img_layout.addLayout(row_iq)

        row_idim = QHBoxLayout()
        row_idim.addWidget(QLabel("尺寸限制:"))
        self.combo_img_dim = QComboBox()
        self.combo_img_dim.addItem("保持原始分辨率 (不缩放)", 0)
        self.combo_img_dim.addItem("长边最大 2560px (2K 超清)", 2560)
        self.combo_img_dim.addItem("长边最大 1920px (1080P 高清)", 1920)
        self.combo_img_dim.addItem("长边最大 1280px (适合网页/文档)", 1280)
        row_idim.addWidget(self.combo_img_dim, 1)
        img_layout.addLayout(row_idim)

        cfg_row.addWidget(img_group, 1)

        # --- 右：视频压缩设置 ---
        vid_group = QGroupBox("🎬 视频压缩配置 (FFmpeg + NVIDIA GPU 极速加速)")
        vid_layout = QVBoxLayout(vid_group)
        vid_layout.setSpacing(8)

        row_venc = QHBoxLayout()
        row_venc.addWidget(QLabel("编码引擎:"))
        self.combo_vid_enc = QComboBox()
        self.combo_vid_enc.addItem("⚡ NVIDIA GPU 硬件极速 H.264 (h264_nvenc，通用极速)", "nvenc_h264")
        self.combo_vid_enc.addItem("⚡ NVIDIA GPU 硬件高压缩 H.265 (hevc_nvenc，体积更小)", "nvenc_hevc")
        self.combo_vid_enc.addItem("🎯 CPU 标准精压 H.264 (libx264)", "cpu_x264")
        self.combo_vid_enc.addItem("🎯 CPU 极限精压 H.265 (libx265)", "cpu_x265")
        row_venc.addWidget(self.combo_vid_enc, 1)
        vid_layout.addLayout(row_venc)

        row_vcrf = QHBoxLayout()
        row_vcrf.addWidget(QLabel("压缩强度:"))
        self.combo_vid_crf = QComboBox()
        self.combo_vid_crf.addItem("🔥 推荐均衡瘦身 (CQ/CRF 27 — 通常减小 60%~85% 体积)", 27)
        self.combo_vid_crf.addItem("💎 视觉无损高画质 (CQ/CRF 23 — 适合高清存档)", 23)
        self.combo_vid_crf.addItem("🚀 强力极限瘦身 (CQ/CRF 31 — 适合微信/飞书快速发送)", 31)
        self.combo_vid_crf.addItem("📦 超小体积模式 (CQ/CRF 35 — 最小文件体积)", 35)
        row_vcrf.addWidget(self.combo_vid_crf, 1)
        vid_layout.addLayout(row_vcrf)

        row_vres = QHBoxLayout()
        row_vres.addWidget(QLabel("视频尺寸:"))
        self.combo_vid_res = QComboBox()
        self.combo_vid_res.addItem("保持原视频分辨率", 0)
        self.combo_vid_res.addItem("缩放到最大 1080P (1920px)", 1920)
        self.combo_vid_res.addItem("缩放到最大 720P (1280px，大幅省空间)", 1280)
        row_vres.addWidget(self.combo_vid_res, 1)

        row_vres.addWidget(QLabel("帧率:"))
        self.combo_vid_fps = QComboBox()
        self.combo_vid_fps.addItem("保持原帧率", 0)
        self.combo_vid_fps.addItem("限制最高 30 FPS", 30)
        self.combo_vid_fps.addItem("限制最高 24 FPS", 24)
        row_vres.addWidget(self.combo_vid_fps)
        vid_layout.addLayout(row_vres)

        cfg_row.addWidget(vid_group, 1)
        main_layout.addLayout(cfg_row)

        # ================= 3. 文件队列表格 =================
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            "文件名", "类型", "原始大小", "压缩后大小", "节省比例", "状态 (双击打开预览)"
        ])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, 6):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self.on_row_double_clicked)
        main_layout.addWidget(self.table, 1)

        # ================= 4. 底部输出目录与开始执行栏 =================
        bottom_frame = QFrame()
        bottom_frame.setObjectName("CardFrame")
        bottom_layout = QVBoxLayout(bottom_frame)
        bottom_layout.setContentsMargins(14, 10, 14, 10)
        bottom_layout.setSpacing(8)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("📂 压缩后保存位置:"))
        self.out_dir_edit = QLineEdit(str(self.default_out_dir))
        out_row.addWidget(self.out_dir_edit, 1)

        btn_choose_out = QPushButton("📁 更改目录...")
        btn_choose_out.clicked.connect(self.choose_out_dir)
        btn_open_out = QPushButton("📂 打开输出文件夹")
        btn_open_out.clicked.connect(self.open_out_dir)

        self.chk_keep_smaller = QCheckBox("智能防反向膨胀 (若已无法再压缩则保留原文件)")
        self.chk_keep_smaller.setChecked(True)

        out_row.addWidget(btn_choose_out)
        out_row.addWidget(btn_open_out)
        out_row.addWidget(self.chk_keep_smaller)
        bottom_layout.addLayout(out_row)

        run_row = QHBoxLayout()
        self.summary_label = QLabel("等待添加文件...")
        self.summary_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #0F172A;")
        run_row.addWidget(self.summary_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumHeight(26)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("就绪")
        run_row.addWidget(self.progress_bar, 1)

        self.btn_start = QPushButton("🚀 开始批量极速压缩")
        self.btn_start.setObjectName("PrimaryButton")
        self.btn_start.setMinimumHeight(42)
        self.btn_start.setMinimumWidth(210)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.clicked.connect(self.start_compression)
        run_row.addWidget(self.btn_start)

        bottom_layout.addLayout(run_row)
        main_layout.addWidget(bottom_frame)

    def _apply_styles(self):
        self.setStyleSheet("""
            QMainWindow {
                background-color: #F1F5F9;
                font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
            }
            QFrame#CardFrame, QGroupBox {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 10px;
            }
            QGroupBox {
                font-size: 13.5px;
                font-weight: bold;
                color: #0F172A;
                margin-top: 8px;
                padding-top: 14px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
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
            }
            QPushButton#PrimaryButton {
                background-color: #2563EB;
                border: 1px solid #1D4ED8;
                color: #FFFFFF;
                font-size: 13.5px;
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
                font-size: 12.5px;
                font-weight: bold;
                border-radius: 8px;
            }
            QPushButton#AccentButton:hover {
                background-color: #047857;
            }
            QTableWidget {
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
                font-size: 12.5px;
            }
            QComboBox, QLineEdit {
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 5px 8px;
                background-color: #FFFFFF;
                font-size: 12.5px;
            }
        """)

    def _on_img_q_changed(self, val: int):
        desc = "视觉无损" if val >= 85 else ("均衡高压缩" if val >= 70 else "极限瘦身")
        self.lbl_img_q.setText(f"压缩画质: {val}% ({desc})")

    # ================= 拖拽与文件添加 =================
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        self.add_paths(paths)

    def browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要压缩的图片或视频",
            str(get_desktop_dir()),
            "媒体文件 (*.jpg *.jpeg *.png *.webp *.bmp *.mp4 *.mov *.mkv *.avi *.webm *.3gp);;所有文件 (*.*)",
        )
        if files:
            self.add_paths([Path(f) for f in files])

    def browse_folder(self):
        d = QFileDialog.getExistingDirectory(self, "选择包含图片/视频的文件夹", str(get_desktop_dir()))
        if d:
            self.add_paths([Path(d)])

    def import_samsung_export_dir(self):
        export_dir = get_desktop_dir() / "三星手机相册导出"
        if not export_dir.exists():
            QMessageBox.information(self, "提示", f"桌面尚未生成相册导出文件：\n{export_dir}\n请先从相册中心导出文件或手动选择其他目录。")
            return
        self.add_paths([export_dir])

    def add_paths(self, paths):
        valid_exts = IMAGE_EXTS | VIDEO_EXTS
        existing_srcs = {t.src_path.resolve() for t in self.tasks}
        added = 0

        for p in paths:
            p = Path(p)
            if not p.exists():
                continue
            if p.is_file():
                if p.suffix.lower() in valid_exts and p.resolve() not in existing_srcs:
                    self.tasks.append(CompressTaskItem(p))
                    existing_srcs.add(p.resolve())
                    added += 1
            elif p.is_dir():
                for sub in sorted(p.rglob("*")):
                    if sub.is_file() and sub.suffix.lower() in valid_exts and sub.resolve() not in existing_srcs:
                        self.tasks.append(CompressTaskItem(sub))
                        existing_srcs.add(sub.resolve())
                        added += 1

        self.refresh_table()

    def clear_tasks(self):
        if self.worker and self.worker.isRunning():
            return
        self.tasks.clear()
        self.refresh_table()

    def refresh_table(self):
        self.table.setRowCount(len(self.tasks))
        img_cnt = sum(1 for t in self.tasks if not t.is_video)
        vid_cnt = sum(1 for t in self.tasks if t.is_video)
        total_orig = sum(t.orig_size for t in self.tasks)

        for row, t in enumerate(self.tasks):
            self.table.setItem(row, 0, QTableWidgetItem(t.name))
            self.table.setItem(row, 1, QTableWidgetItem("🎬 视频" if t.is_video else "🖼️ 图片"))
            self.table.setItem(row, 2, QTableWidgetItem(format_size(t.orig_size)))
            self.table.setItem(row, 3, QTableWidgetItem(format_size(t.new_size) if t.new_size else "-"))
            if t.new_size > 0 and t.orig_size > 0:
                ratio = (1.0 - t.new_size / t.orig_size) * 100
                self.table.setItem(row, 4, QTableWidgetItem(f"-{ratio:.1f}%"))
            else:
                self.table.setItem(row, 4, QTableWidgetItem("-"))
            self.table.setItem(row, 5, QTableWidgetItem(t.status))

        self.summary_label.setText(
            f"共 {len(self.tasks)} 个文件 (🖼️ 图片 {img_cnt} | 🎬 视频 {vid_cnt})  |  原始总大小: {format_size(total_orig)}"
        )

    # ================= 压缩执行与回调 =================
    def choose_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择压缩后保存的文件夹", self.out_dir_edit.text())
        if d:
            self.out_dir_edit.setText(d)

    def open_out_dir(self):
        target = Path(self.out_dir_edit.text().strip() or str(self.default_out_dir))
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def start_compression(self):
        if not self.tasks:
            QMessageBox.information(self, "提示", "请先添加或拖入需要压缩的图片或视频文件！")
            return
        if self.worker and self.worker.isRunning():
            return

        config = {
            "out_dir": self.out_dir_edit.text().strip() or str(self.default_out_dir),
            "keep_smaller": self.chk_keep_smaller.isChecked(),
            "img_format": self.combo_img_fmt.currentData(),
            "img_quality": self.slider_img_q.value(),
            "img_max_dim": self.combo_img_dim.currentData(),
            "vid_encoder": self.combo_vid_enc.currentData(),
            "vid_crf": self.combo_vid_crf.currentData(),
            "vid_max_res": self.combo_vid_res.currentData(),
            "vid_fps": self.combo_vid_fps.currentData(),
        }

        self.btn_start.setEnabled(False)
        self.progress_bar.setMaximum(len(self.tasks))
        self.progress_bar.setValue(0)

        self.worker = CompressWorker(self.tasks, config)
        self.worker.item_started.connect(self.on_item_started)
        self.worker.item_finished.connect(self.on_item_finished)
        self.worker.all_finished.connect(self.on_all_finished)
        self.worker.start()

    def on_item_started(self, row: int, status_text: str):
        self.table.setItem(row, 5, QTableWidgetItem(status_text))
        self.table.scrollToItem(self.table.item(row, 0))
        self.progress_bar.setFormat(f"正在处理 ({row + 1}/{len(self.tasks)}): {self.tasks[row].name}")

    def on_item_finished(self, row: int, ok: bool, new_size: int, out_path: str, msg: str):
        self.progress_bar.setValue(row + 1)
        orig_sz = self.tasks[row].orig_size
        if ok and new_size > 0:
            self.table.setItem(row, 3, QTableWidgetItem(format_size(new_size)))
            saved_pct = (1.0 - new_size / orig_sz) * 100 if orig_sz > 0 else 0.0
            ratio_item = QTableWidgetItem(f"-{saved_pct:.1f}%" if saved_pct >= 0 else f"+{abs(saved_pct):.1f}%")
            ratio_item.setForeground(QColor("#059669" if saved_pct >= 0 else "#D97706"))
            ratio_item.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
            self.table.setItem(row, 4, ratio_item)
        self.table.setItem(row, 5, QTableWidgetItem(msg))

    def on_all_finished(self, ok_cnt: int, fail_cnt: int, total_orig: int, total_new: int, out_dir: str):
        self.btn_start.setEnabled(True)
        saved_bytes = max(0, total_orig - total_new)
        saved_pct = (saved_bytes / total_orig * 100) if total_orig > 0 else 0.0

        summary = (
            f"🎉 压缩完成！原始: {format_size(total_orig)} → 压缩后: {format_size(total_new)} "
            f"（共省下 {format_size(saved_bytes)} / -{saved_pct:.1f}%）"
        )
        self.summary_label.setText(summary)
        self.progress_bar.setFormat(f"全部完成：成功 {ok_cnt} 个，失败 {fail_cnt} 个")

        reply = QMessageBox.question(
            self,
            "压缩完成",
            f"批量压缩已完成！\n\n"
            f"• 成功压缩：{ok_cnt} 个文件\n"
            f"• 原始总大小：{format_size(total_orig)}\n"
            f"• 压缩后总大小：{format_size(total_new)}\n"
            f"• 累计节省空间：{format_size(saved_bytes)} (-{saved_pct:.1f}%)\n\n"
            f"输出目录：\n{out_dir}\n\n"
            f"是否立即打开输出文件夹？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            QDesktopServices.openUrl(QUrl.fromLocalFile(out_dir))

    def on_row_double_clicked(self, row: int, _col: int):
        if 0 <= row < len(self.tasks):
            t = self.tasks[row]
            target = t.out_path if (t.out_path and t.out_path.exists()) else t.src_path
            if target and target.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    init_paths = [Path(a) for a in sys.argv[1:]] if len(sys.argv) > 1 else None
    win = MediaCompressorWindow(initial_paths=init_paths)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
