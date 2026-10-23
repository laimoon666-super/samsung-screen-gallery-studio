# 📱 Samsung Screen Mirror & Gallery Studio + Media Compressor

一套专为三星 / Android 手机打造的桌面端 **60FPS 超清投屏 + 手机相册可视化批量导出 + NVIDIA GPU 图片/视频极速压缩工具套件**。

---

## ✨ 核心功能模块

### 1. 🖥️ 三星手机 60FPS 高清投屏 (`samsung_studio.py` & `launch_mirror.pyw`)
- **USB + 5G Wi-Fi 双模防断连自愈引擎**：
  - 自动检测手机 USB 连接，并通过 `netsh` 动态同步当前电脑 Wi-Fi 凭证，一键激活手机 `tcpip:5555` 5GHz 高速无线投屏通道。
  - **彻底解决**电脑外接无源 USB 2.0 集线器在手机开启 120Hz 高刷（`Touch Boost`）与硬件编码器时因瞬时电流拉高导致的物理掉线重连（`USB_STATE=DISCONNECTED`）与投屏闪退问题。
- **基于 Scrcpy v5.0.1 + H.265 (HEVC) 硬件解码**：
  - 支持电脑鼠标左键点滑、右键返回、中键回桌面、电脑键盘直接打字、剪贴板双向同步、文件拖拽传输。
  - 内置快捷遥控面板：支持 **一键截屏直存电脑桌面**、电源/锁屏、音量控制、息屏投屏模式、窗口置顶。

### 2. 🖼️ 手机相册（图片 & 视频）可视化管理与增量导出 (`samsung_studio.py`)
- **极速扫描与分相册浏览**：
  - 采用 Android `find -printf` 单次管道扫描，毫秒级读取 `/sdcard/DCIM`、`Pictures`、`Movies`、`Download` 下所有相册（相机拍摄、屏幕录制 MP4、系统截屏、快捷截屏、长截图等）。
- **异步缩略图缓存 & 双击直接预览**：
  - 后台错峰拉取生成图片缩略图并持久化缓存；双击任意图片或视频即可自动拉取到电脑并用系统默认播放器/看图软件打开。
- **一键全量导出 & 增量跳过**：
  - 支持按相册自动分文件夹导出到桌面 `三星手机相册导出` 目录，自动比对同名同体积文件实现增量备份。

### 3. 🗜️ 图片与视频极速压缩工具 (`media_compressor.py`)
- **支持文件/文件夹直接拖拽导入**，或一键导入手机导出的相册目录。
- **图片智能压缩**：
  - 支持 `JPG / PNG / WebP / BMP`，可保持原格式（内置 PNG `MEDIANCUT` 调色板智能有损量化，体积减小 60%~80%）或批量转换为高压缩比 `WebP` / `JPG`。
  - 支持自定义画质百分比（20%~95%）与长边最大分辨率等比缩放（原尺寸 / 2K / 1080P / 720P）。
- **视频 NVIDIA GPU 硬件加速压缩**：
  - 支持 `h264_nvenc` / `hevc_nvenc` GPU 硬件编码（几秒内压完百兆录屏视频，若无独显自动平滑回退至 `libx264` / `libx265`）。
  - 支持多档位 CQ/CRF 压缩强度、分辨率缩放（原画 / 1080P / 720P）及最高帧率限制（原帧率 / 30FPS / 24FPS）。
  - 内置 **智能防反向膨胀保护**：若源文件已处于极限压缩状态，自动保留原文件。

---

## 🚀 快速开始

### 1. 环境依赖
- Windows 10 / 11 (64-bit)
- Python 3.10+
- 安装依赖：
  ```bash
  pip install -r requirements.txt
  ```
- 将 [Scrcpy win64](https://github.com/Genymobile/scrcpy/releases) 解压至项目目录下的 `scrcpy/` 文件夹，并确保系统已安装 `ffmpeg`（可通过 `winget install Gyan.FFmpeg` 安装）。

### 2. 启动方式
- **启动投屏与相册中心**：
  ```bash
  python samsung_studio.py
  ```
- **单独启动图片/视频极速压缩工具**：
  ```bash
  python media_compressor.py
  ```
- **一键静默启动防断连投屏**：
  ```bash
  pythonw launch_mirror.pyw
  ```
