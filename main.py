import sys
import os
import re
import json
import threading
import tempfile
import requests
import urllib.parse
from PyQt6.QtCore import Qt, QUrl, QPropertyAnimation, QEasingCurve, pyqtSignal, QSize, QTimer, QObject, QParallelAnimationGroup, QAbstractAnimation
from PyQt6.QtWidgets import (QApplication, QVBoxLayout, QHBoxLayout, QWidget,
                             QSystemTrayIcon, QMenu, QListWidget, QListWidgetItem,
                             QGraphicsOpacityEffect, QComboBox, QLabel, QFrame, QAbstractItemView,
                             QFileDialog)
from PyQt6.QtGui import QIcon, QAction, QPixmap, QPainter, QPainterPath, QColor, QFontMetrics
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.flac import FLAC
from mutagen.id3 import ID3, APIC, USLT
from mutagen.oggvorbis import OggVorbis

# qfluentwidgets 组件
from qfluentwidgets import (
    FluentWindow, setTheme, Theme, PushButton, BodyLabel, CaptionLabel,
    TitleLabel, InfoBar, SimpleCardWidget, IconWidget,
    FluentIcon as FIF, setThemeColor, Slider, ToolTipFilter,
    ImageLabel, SearchLineEdit, PrimaryPushButton, ComboBox
)

# API 配置
API_BDYY = "https://api.xcvts.cn/api/music/bdyy"      # 波点音乐 API
API_NETEASE = "https://api.qijieya.cn/meting/"         # 网易云音乐 API（搜索）
API_NETEASE_BACKUP = "https://www.ffapi.cn/int/v1/dg_netease"  # 备用网易云 API

# 全局设置
_app_settings = {"netease_api": "default"}


def _format_duration(ms):
    """毫秒转 mm:ss"""
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


# 临时文件管理
_temp_dir = tempfile.mkdtemp(prefix="music_island_")
_temp_files = []
_temp_lock = threading.Lock()


def _download_to_temp(url):
    """下载 URL 到临时文件，返回本地路径"""
    try:
        print(f"[TempDownload] 下载: {url[:80]}")
        resp = requests.get(url, timeout=30, stream=True, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '')
        ext = '.mp3'
        if 'audio/flac' in content_type or url.lower().endswith('.flac'):
            ext = '.flac'
        elif 'audio/ogg' in content_type or url.lower().endswith('.ogg'):
            ext = '.ogg'
        elif 'audio/mp4' in content_type or 'audio/m4a' in content_type or url.lower().endswith(('.m4a', '.aac')):
            ext = '.m4a'
        fd, tmp_path = tempfile.mkstemp(suffix=ext, dir=_temp_dir)
        total = 0
        with os.fdopen(fd, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                total += len(chunk)
        print(f"[TempDownload] 完成: {tmp_path} ({total} bytes)")
        with _temp_lock:
            _temp_files.append(tmp_path)
        return tmp_path
    except Exception as e:
        print(f"[TempDownload] 下载失败: {e}")
        return ''


def _cleanup_temp_files():
    """清理临时文件"""
    with _temp_lock:
        for f in _temp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        _temp_files.clear()
    try:
        os.rmdir(_temp_dir)
    except Exception:
        pass


# ---------------------------- 圆角容器 ----------------------------
class RoundedContainer(QWidget):
    def __init__(self, parent=None, radius=30, bg_color=(32, 32, 32, 230), border_color=(255, 255, 255, 40)):
        super().__init__(parent)
        self.radius = radius
        self.bg_color = bg_color
        self.border_color = border_color
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), self.radius, self.radius)
        painter.fillPath(path, QColor(*self.bg_color))
        painter.setPen(QColor(*self.border_color))
        painter.drawPath(path)


# ---------------------------- 封面下载器（独立线程安全） ----------------------------
class CoverDownloader(QObject):
    cover_downloaded = pyqtSignal(bytes)  # 发送图片原始数据

    def download(self, url):
        threading.Thread(target=self._do_download, args=(url,), daemon=True).start()

    def _do_download(self, url):
        try:
            print(f"[封面下载] 开始下载: {url[:60]}...")
            resp = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            print(f"[封面下载] 数据大小: {len(resp.content)} bytes")
            self.cover_downloaded.emit(resp.content)
        except Exception as e:
            print(f"[封面下载] 异常: {e}")
            self.cover_downloaded.emit(b'')


# ---------------------------- 灵动岛悬浮窗口 ----------------------------
class DynamicIsland(QWidget):

    play_paused = pyqtSignal()
    next_song = pyqtSignal()
    prev_song = pyqtSignal()
    play_mode_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setup_ui()
        self.setup_animation()

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                           Qt.WindowType.WindowStaysOnTopHint |
                           Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.is_expanded = False
        self._is_animating = False
        self.collapsed_size = QSize(420, 60)
        self.expanded_size = QSize(500, 240)
        self.base_collapsed_width = 420
        self.resize(self.collapsed_size)

        self.drag_pos = None
        self.lyrics_data = []
        self.current_lyric_index = -1

        self.lyric_timer = QTimer(self)
        self.lyric_timer.timeout.connect(self._update_lyric_timer)
        self.lyric_timer.start(500)
        self.current_position_ms = 0
        self.is_playing = False

        # 封面下载器
        self.cover_downloader = CoverDownloader()
        self.cover_downloader.cover_downloaded.connect(self._on_cover_downloaded)

        # 鼠标悬停检测
        self.is_mouse_inside = False
        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.timeout.connect(self._on_hover_timeout)

        self.installEventFilter(ToolTipFilter(self, showDelay=500))

        # 定时居中检查
        self._center_timer = QTimer(self)
        self._center_timer.timeout.connect(self._enforce_center)
        self._center_timer.start(300)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.container = RoundedContainer(self)
        self.container.setStyleSheet("""
            QLabel { color: white; font-family: "Microsoft YaHei"; }
            QPushButton {
                background-color: rgba(255, 255, 255, 15); border: none;
                border-radius: 20px; padding: 5px; color: white; font-size: 14px;
            }
            QPushButton:hover { background-color: rgba(255, 255, 255, 30); }
            QSlider::groove:horizontal { height: 4px; background: rgba(200, 200, 200, 100); border-radius: 2px; }
            QSlider::handle:horizontal { background: white; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
        """)

        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(15, 10, 15, 10)
        container_layout.setSpacing(8)

        # 顶部行
        top_row = QHBoxLayout()
        self.cover_label = ImageLabel(self.container)
        self.cover_label.setFixedSize(40, 40)
        self.cover_label.setBorderRadius(8, 8, 8, 8)
        self.cover_label.setStyleSheet("background-color: rgba(255,255,255,20);")
        top_row.addWidget(self.cover_label)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        self.lyric_line = BodyLabel("♫ 暂无歌词")
        self.lyric_line.setStyleSheet("font-size: 14px; font-weight: bold;")
        info_layout.addWidget(self.lyric_line)
        self.song_title = CaptionLabel("未播放")
        self.song_title.setTextColor(160, 160, 160)
        info_layout.addWidget(self.song_title)

        # 为歌名添加透明度效果
        self.song_title_opacity = QGraphicsOpacityEffect(self.song_title)
        self.song_title_opacity.setOpacity(1.0)
        self.song_title.setGraphicsEffect(self.song_title_opacity)

        top_row.addLayout(info_layout, 1)

        self.toggle_btn = PushButton("▼", self.container)
        self.toggle_btn.setFixedSize(28, 28)
        self.toggle_btn.clicked.connect(self.toggle_expand)
        top_row.addWidget(self.toggle_btn)

        container_layout.addLayout(top_row)

        # 展开区域
        self.expand_widget = QWidget()
        expand_layout = QVBoxLayout(self.expand_widget)
        expand_layout.setContentsMargins(0, 5, 0, 0)
        expand_layout.setSpacing(10)

        expand_top = QHBoxLayout()
        self.expand_cover = ImageLabel(self.expand_widget)
        self.expand_cover.setFixedSize(80, 80)
        self.expand_cover.setBorderRadius(12, 12, 12, 12)
        expand_top.addWidget(self.expand_cover)

        expand_info = QVBoxLayout()
        self.expand_title = BodyLabel("未播放")
        self.expand_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        expand_info.addWidget(self.expand_title)
        self.expand_artist = CaptionLabel("未知歌手")
        expand_info.addWidget(self.expand_artist)
        self.expand_album = CaptionLabel("未知专辑")
        self.expand_album.setTextColor(150, 150, 150)
        expand_info.addWidget(self.expand_album)

        # 为展开区域的信息添加透明度效果
        self.expand_title_opacity = QGraphicsOpacityEffect(self.expand_title)
        self.expand_title_opacity.setOpacity(1.0)
        self.expand_title.setGraphicsEffect(self.expand_title_opacity)

        self.expand_artist_opacity = QGraphicsOpacityEffect(self.expand_artist)
        self.expand_artist_opacity.setOpacity(1.0)
        self.expand_artist.setGraphicsEffect(self.expand_artist_opacity)

        self.expand_album_opacity = QGraphicsOpacityEffect(self.expand_album)
        self.expand_album_opacity.setOpacity(1.0)
        self.expand_album.setGraphicsEffect(self.expand_album_opacity)

        self.lyric_line_opacity = QGraphicsOpacityEffect(self.lyric_line)
        self.lyric_line_opacity.setOpacity(1.0)
        self.lyric_line.setGraphicsEffect(self.lyric_line_opacity)

        expand_top.addLayout(expand_info, 1)
        expand_layout.addLayout(expand_top)

        self.progress_slider = Slider(Qt.Orientation.Horizontal)
        self.progress_slider.setRange(0, 100)
        expand_layout.addWidget(self.progress_slider)

        time_layout = QHBoxLayout()
        self.current_time = CaptionLabel("00:00")
        self.current_time.setTextColor(150, 150, 150)
        time_layout.addWidget(self.current_time)
        time_layout.addStretch()
        self.total_time = CaptionLabel("00:00")
        self.total_time.setTextColor(150, 150, 150)
        time_layout.addWidget(self.total_time)
        expand_layout.addLayout(time_layout)

        btn_layout = QHBoxLayout()
        self.prev_btn = PushButton("⏮", self.expand_widget)
        self.play_btn = PushButton("▶", self.expand_widget)
        self.next_btn = PushButton("⏭", self.expand_widget)
        for btn in (self.prev_btn, self.play_btn, self.next_btn):
            btn.setFixedSize(42, 42)
            btn_layout.addWidget(btn)
        self.prev_btn.clicked.connect(self.prev_song.emit)
        self.play_btn.clicked.connect(self.play_paused.emit)
        self.next_btn.clicked.connect(self.next_song.emit)

        # 单曲循环按钮
        self.loop_btn = PushButton("🔂", self.expand_widget)
        self.loop_btn.setFixedSize(36, 36)
        self.loop_btn.clicked.connect(self.toggle_loop)
        self.play_mode = "single_loop"
        btn_layout.addSpacing(10)
        btn_layout.addWidget(self.loop_btn)

        expand_layout.addLayout(btn_layout)

        self.full_lyric = CaptionLabel("当前歌词将显示在这里")
        self.full_lyric.setWordWrap(True)
        self.full_lyric.setTextColor(150, 150, 150)
        expand_layout.addWidget(self.full_lyric)

        container_layout.addWidget(self.expand_widget)
        self.expand_widget.setVisible(False)

        layout.addWidget(self.container)
        self.move_to_top_center()

    def setup_animation(self):
        self._geom_anim = QPropertyAnimation(self, b"geometry")
        self._geom_anim.setDuration(250)
        self._geom_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._expand_opacity = QGraphicsOpacityEffect(self.expand_widget)
        self._expand_opacity.setOpacity(0.0)
        self.expand_widget.setGraphicsEffect(self._expand_opacity)

        self._fade_anim = QPropertyAnimation(self._expand_opacity, b"opacity")
        self._fade_anim.setDuration(200)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_center_x(self):
        screen = QApplication.primaryScreen()
        geo = screen.geometry()
        return geo.x() + (geo.width() - self.width()) // 2

    def _enforce_center(self):
        """强制水平居中"""
        if self._is_animating:
            return
        cx = self._get_center_x()
        if self.x() != cx:
            self.move(cx, self.y())

    def move_to_top_center(self):
        self.adjustSize()
        self.move(self._get_center_x(), 10)

    def toggle_expand(self):
        self.is_expanded = not self.is_expanded
        if self.is_expanded:
            self._expand_with_animation()
        else:
            self._collapse_with_animation()

    def _expand_with_animation(self):
        self._stop_all_animations()
        self.is_expanded = True
        self._is_animating = True
        self.toggle_btn.setText("▲")

        target = self.geometry()
        target.setSize(self.expanded_size)
        target.moveLeft(self._get_center_x())
        if target.top() < 0:
            target.moveTop(10)

        self._geom_anim.setEndValue(target)
        self.expand_widget.setVisible(True)
        self._expand_opacity.setOpacity(0.0)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)

        self._geom_anim.start()
        self._fade_anim.start()
        QTimer.singleShot(250, self._on_animation_done)

    def _collapse_with_animation(self, on_finished=None):
        self._stop_all_animations()
        self.is_expanded = False
        self._is_animating = True
        self.toggle_btn.setText("▼")

        fm = QFontMetrics(self.lyric_line.font())
        text = self.lyric_line.text()
        text_width = fm.horizontalAdvance(text)
        fixed_parts = 15 + 40 + 8 + 8 + 28 + 15
        min_w = self.base_collapsed_width
        max_w = QApplication.primaryScreen().availableGeometry().width() - 40
        collapsed_w = max(min_w, min(max_w, text_width + fixed_parts + 20))
        collapsed_h = self.collapsed_size.height()

        target = self.geometry()
        target.setWidth(collapsed_w)
        target.setHeight(collapsed_h)
        target.moveLeft(self._get_center_x())
        if target.top() < 0:
            target.moveTop(10)

        self._fade_anim.setStartValue(self._expand_opacity.opacity())
        self._fade_anim.setEndValue(0.0)

        self._geom_anim.setStartValue(self.geometry())
        self._geom_anim.setEndValue(target)
        self._geom_anim.start()
        self._fade_anim.start()

        def _done():
            self._on_animation_done()
            if self.is_expanded:
                return
            self.resize(collapsed_w, collapsed_h)
            self.move(self._get_center_x(), self.y())
            self.expand_widget.setVisible(False)
            if on_finished:
                on_finished()

        QTimer.singleShot(250, _done)

    def _on_animation_done(self):
        self._is_animating = False

    def _stop_all_animations(self):
        self._is_animating = False
        for anim in (self._geom_anim, self._fade_anim):
            if anim.state() == QAbstractAnimation.State.Running:
                anim.stop()

    def update_song_info(self, title, artist="未知歌手", album="未知专辑", cover_url="", lrc_text=""):
        print(f"[Island] 更新歌曲: {title} - {artist}, 封面URL: {cover_url[:60] if cover_url else '无'}")
        self.song_title.setText(title if title else "未播放")
        self.expand_title.setText(title if title else "未播放")
        self.expand_artist.setText(artist)
        self.expand_album.setText(album)

        if cover_url:
            print(f"[Island] 开始下载封面: {cover_url}")
            self._load_cover_from_url(cover_url)
        else:
            self.cover_label.clear()
            self.expand_cover.clear()

        if lrc_text:
            print(f"[Island] 设置歌词, 长度: {len(lrc_text)}")
            self.set_lyrics([], lrc_text)

    def _load_cover_from_url(self, url):
        print(f"[Island] 请求下载封面: {url[:60]}")
        self.cover_downloader.download(url)

    def _on_cover_downloaded(self, data):
        """在主线程中处理下载完成的封面数据"""
        if not data:
            print(f"[Island] 封面下载失败: 数据为空")
            return

        print(f"[Island] 封面数据已下载: {len(data)} bytes")
        pixmap = QPixmap()
        loaded = pixmap.loadFromData(data)
        print(f"[Island] QPixmap.loadFromData 返回: {loaded}, isNull: {pixmap.isNull()}")

        if loaded and not pixmap.isNull():
            # 缩放图片到合适的尺寸
            cover_pixmap = pixmap.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            expand_pixmap = pixmap.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.cover_label.setPixmap(cover_pixmap)
            self.expand_cover.setPixmap(expand_pixmap)
            print(f"[Island] 封面已设置 (40x40, 80x80)")
        else:
            print(f"[Island] 封面加载失败: pixmap is null")

    def set_lyrics(self, lyrics_data, raw_lyrics_text=""):
        self.current_lyric_index = -1
        if lyrics_data:
            self.lyrics_data = lyrics_data
        elif raw_lyrics_text and raw_lyrics_text.strip():
            parsed = self._parse_raw_lyrics(raw_lyrics_text)
            self.lyrics_data = parsed if parsed else []
        else:
            self.lyrics_data = []

    def _parse_raw_lyrics(self, text):
        lyrics = []
        for line in text.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            match = re.match(r'\[(\d{2}):(\d{2})(?:\.(\d{2,3}))?\](.*)', line)
            if match:
                minutes = int(match.group(1))
                seconds = int(match.group(2))
                cs = match.group(3) or "0"
                centiseconds = int(cs) // 10 if len(cs) == 3 else int(cs)
                time_ms = (minutes * 60 + seconds) * 1000 + centiseconds * 10
                lyric_text = match.group(4).strip()
                if lyric_text:
                    lyrics.append((time_ms, lyric_text))
        return sorted(lyrics, key=lambda x: x[0]) if lyrics else None

    def _update_lyric_timer(self):
        if self.is_playing and self.lyrics_data:
            # 只读取 current_position_ms，不修改它（由播放器信号 _on_position_changed 维护）
            self.update_lyric_by_position(self.current_position_ms)

    def update_lyric_by_position(self, position_ms):
        self.current_position_ms = position_ms
        if not self.lyrics_data:
            return
        current_index = -1
        for i, (time_ms, text) in enumerate(self.lyrics_data):
            if time_ms <= position_ms:
                current_index = i
            else:
                break
        if current_index != -1 and current_index != self.current_lyric_index:
            self.current_lyric_index = current_index
            self.update_lyric(self.lyrics_data[current_index][1])

    def update_lyric(self, lyric):
        self.lyric_line.setText(f"♫ {lyric}")
        self.full_lyric.setText(lyric)
        self._recalc_collapsed_size()

    def _recalc_collapsed_size(self):
        if self.is_expanded:
            return
        fm = QFontMetrics(self.lyric_line.font())
        text = self.lyric_line.text()
        text_width = fm.horizontalAdvance(text)
        fixed_parts = 15 + 40 + 8 + 8 + 28 + 15
        min_w = self.base_collapsed_width
        max_w = QApplication.primaryScreen().availableGeometry().width() - 40
        new_w = max(min_w, min(max_w, text_width + fixed_parts + 20))
        if new_w != self.width():
            old_pos = self.pos()
            self.resize(new_w, self.collapsed_size.height())
            self.move(self._get_center_x(), old_pos.y())

    def toggle_loop(self):
        """切换播放模式: 单曲循环 -> 列表循环 -> 随机播放"""
        modes = ["single_loop", "list_loop", "shuffle"]
        icons = {"single_loop": "🔂", "list_loop": "🔁", "shuffle": "🔀"}
        idx = modes.index(self.play_mode)
        self.play_mode = modes[(idx + 1) % len(modes)]
        self.loop_btn.setText(icons[self.play_mode])
        self.play_mode_changed.emit(self.play_mode)

    def update_play_mode(self, mode):
        """更新播放模式按钮状态"""
        icons = {"single_loop": "🔂", "list_loop": "🔁", "shuffle": "🔀"}
        self.play_mode = mode
        self.loop_btn.setText(icons.get(mode, "🔂"))

    def update_play_state(self, is_playing):
        self.is_playing = is_playing
        self.play_btn.setText("⏸" if is_playing else "▶")

    def update_progress(self, value, current_sec=0, total_sec=0):
        self.progress_slider.setValue(value)
        self.current_time.setText(f"{current_sec//60:02d}:{current_sec%60:02d}")
        self.total_time.setText(f"{total_sec//60:02d}:{total_sec%60:02d}")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self.drag_pos:
            new_pos = event.globalPosition().toPoint() - self.drag_pos
            self.move(self._get_center_x(), new_pos.y())
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None

    def enterEvent(self, event):
        """鼠标进入窗口"""
        super().enterEvent(event)
        self.is_mouse_inside = True
        self.hover_timer.stop()
        self._stop_all_animations()
        if not self.is_expanded:
            self._expand_with_animation()
        self._animate_to_normal_mode()

    def leaveEvent(self, event):
        """鼠标离开窗口"""
        super().leaveEvent(event)
        self.is_mouse_inside = False
        # 启动1.5秒定时器
        self.hover_timer.start(1500)

    def _on_hover_timeout(self):
        """鼠标离开1.5秒后触发"""
        if not self.is_mouse_inside:
            if self.is_expanded:
                self._collapse_with_animation(on_finished=self._animate_to_expanded_mode)
            else:
                self._animate_to_expanded_mode()

    def _animate_to_normal_mode(self):
        """动画切换到正常模式：显示歌名和专辑，歌词正常大小"""
        # 停止当前所有透明度动画
        for attr in ('_expand_title_anim', '_expand_artist_anim', '_expand_album_anim', 
                     '_song_title_anim', '_lyric_anim'):
            if hasattr(self, attr):
                anim = getattr(self, attr)
                if anim.state() == QAbstractAnimation.State.Running:
                    anim.stop()

        # 显示歌名和专辑（淡入）
        self.expand_title.setVisible(True)
        self.expand_artist.setVisible(True)
        self.expand_album.setVisible(True)
        self.song_title.setVisible(True)

        # 创建并启动淡入动画
        self._expand_title_anim = self._create_opacity_anim(self.expand_title_opacity, 1.0, 250)
        self._expand_artist_anim = self._create_opacity_anim(self.expand_artist_opacity, 1.0, 250)
        self._expand_album_anim = self._create_opacity_anim(self.expand_album_opacity, 1.0, 250)
        self._song_title_anim = self._create_opacity_anim(self.song_title_opacity, 1.0, 250)

        # 歌词恢复到正常大小（带动画效果通过透明度微调）
        self._lyric_anim = self._create_opacity_anim(self.lyric_line_opacity, 1.0, 250)
        self.lyric_line.setStyleSheet("font-size: 14px; font-weight: bold; color: white;")

        # 启动所有动画
        self._expand_title_anim.start()
        self._expand_artist_anim.start()
        self._expand_album_anim.start()
        self._song_title_anim.start()
        self._lyric_anim.start()

    def _animate_to_expanded_mode(self):
        """动画切换到沉浸模式：隐藏歌名和专辑，放大歌词"""
        # 停止当前所有透明度动画
        for attr in ('_expand_title_anim', '_expand_artist_anim', '_expand_album_anim', 
                     '_song_title_anim', '_lyric_anim'):
            if hasattr(self, attr):
                anim = getattr(self, attr)
                if anim.state() == QAbstractAnimation.State.Running:
                    anim.stop()

        # 创建并启动淡出动画
        self._expand_title_anim = self._create_opacity_anim(self.expand_title_opacity, 0.0, 400)
        self._expand_artist_anim = self._create_opacity_anim(self.expand_artist_opacity, 0.0, 400)
        self._expand_album_anim = self._create_opacity_anim(self.expand_album_opacity, 0.0, 400)
        self._song_title_anim = self._create_opacity_anim(self.song_title_opacity, 0.0, 400)

        # 歌词放大（保持白色）
        self._lyric_anim = self._create_opacity_anim(self.lyric_line_opacity, 1.0, 400)
        self.lyric_line.setStyleSheet("font-size: 20px; font-weight: bold; color: white;")

        # 启动所有动画
        self._expand_title_anim.start()
        self._expand_artist_anim.start()
        self._expand_album_anim.start()
        self._song_title_anim.start()
        self._lyric_anim.start()

        # 动画结束后隐藏控件
        QTimer.singleShot(400, lambda: self.expand_title.setVisible(False))
        QTimer.singleShot(400, lambda: self.expand_artist.setVisible(False))
        QTimer.singleShot(400, lambda: self.expand_album.setVisible(False))
        QTimer.singleShot(400, lambda: self.song_title.setVisible(False))

    def _create_opacity_anim(self, effect, target_opacity, duration):
        """创建透明度动画"""
        anim = QPropertyAnimation(effect, b'opacity')
        anim.setDuration(duration)
        anim.setEndValue(target_opacity)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        return anim


# ---------------------------- 网易云搜索页面 ----------------------------
class NeteaseSearchPage(QWidget):

    song_selected = pyqtSignal(dict)
    add_to_playlist = pyqtSignal(dict)
    search_finished = pyqtSignal(list, str)
    no_results = pyqtSignal(str)
    search_error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.search_results = []

        self.search_finished.connect(self._display_results)
        self.no_results.connect(self._no_results)
        self.search_error.connect(self._search_error)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        title = TitleLabel("网易云音乐搜索")
        layout.addWidget(title)

        # 搜索栏
        search_layout = QHBoxLayout()
        self.search_input = SearchLineEdit()
        self.search_input.setPlaceholderText("输入歌曲名搜索...")
        self.search_input.returnPressed.connect(self.search_song)
        search_layout.addWidget(self.search_input)

        self.search_btn = PrimaryPushButton("搜索")
        self.search_btn.setFixedWidth(100)
        self.search_btn.clicked.connect(self.search_song)
        search_layout.addWidget(self.search_btn)
        layout.addLayout(search_layout)

        # 结果列表
        self.result_label = BodyLabel("搜索结果")
        layout.addWidget(self.result_label)

        self.result_list = QListWidget()
        self.result_list.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.result_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.result_list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.result_list)

        # 正在播放卡片
        self.now_playing_card = SimpleCardWidget()
        np_layout = QVBoxLayout(self.now_playing_card)
        np_layout.setContentsMargins(15, 15, 15, 15)

        np_header = QHBoxLayout()
        np_icon = IconWidget()
        np_icon.setIcon(FIF.MUSIC)
        np_icon.setFixedSize(24, 24)
        np_header.addWidget(np_icon)
        np_title = BodyLabel("正在播放")
        np_title.setStyleSheet("font-weight: bold;")
        np_header.addWidget(np_title)
        np_header.addStretch()
        np_layout.addLayout(np_header)

        self.np_song = CaptionLabel("未选择歌曲")
        np_layout.addWidget(self.np_song)
        self.np_artist = CaptionLabel("")
        self.np_artist.setTextColor(150, 150, 150)
        np_layout.addWidget(self.np_artist)
        self.np_album = CaptionLabel("")
        self.np_album.setTextColor(150, 150, 150)
        np_layout.addWidget(self.np_album)

        layout.addWidget(self.now_playing_card)
        layout.addStretch()

    def search_song(self):
        keyword = self.search_input.text().strip()
        if not keyword:
            InfoBar.warning("提示", "请输入歌曲名", duration=2000, parent=self)
            return

        self.search_btn.setEnabled(False)
        self.search_btn.setText("搜索中...")
        self.result_list.clear()
        self.search_results = []

        threading.Thread(target=self._do_search, args=(keyword,), daemon=True).start()

    def _do_search(self, keyword):
        try:
            if _app_settings.get("netease_api") == "backup":
                self._do_search_backup(keyword)
            else:
                self._do_search_default(keyword)
        except Exception:
            self.search_error.emit(keyword)

    def _do_search_default(self, keyword):
        params = {
            'type': 'search',
            'id': keyword,
            'limit': '20',
        }
        resp = requests.get(API_NETEASE, params=params, timeout=15)
        data = resp.json()

        if isinstance(data, list) and len(data) > 0:
            songs = []
            for s in data:
                songs.append({
                    'source': 'netease',
                    'id': s.get('id', ''),
                    'name': s.get('name', '未知'),
                    'artist': s.get('artist', '未知歌手'),
                    'album': '',
                    'cover': s.get('pic', ''),
                    'play_url': s.get('url', ''),
                    'lrc_url': s.get('lrc', ''),
                    'duration': 0,
                })
            self.search_finished.emit(songs, keyword)
        else:
            self.no_results.emit(keyword)

    def _do_search_backup(self, keyword):
        params = {
            'msg': keyword,
            'limit': '20',
            'format': 'json',
        }
        resp = requests.get(API_NETEASE_BACKUP, params=params, timeout=15)
        data = resp.json()

        if data.get('code') == 200 and data.get('data') and isinstance(data['data'], list):
            list_data = data['data']
            songs = []
            for item in list_data:
                n = item.get('n', '')
                title = item.get('title', '未知')
                singer = item.get('singer', '未知歌手')
                pic = item.get('pic', '')
                songs.append({
                    'source': 'netease_backup',
                    'id': '',
                    'n': str(n),
                    'search_keyword': keyword,
                    'name': title,
                    'artist': singer,
                    'album': '',
                    'cover': pic,
                    'play_url': '',
                    'lrc_url': '',
                    'duration': 0,
                })
            self.search_finished.emit(songs, keyword)
        else:
            self.no_results.emit(keyword)

    def _display_results(self, songs, keyword):
        self.search_results = songs
        self.result_list.clear()
        for song in songs:
            name = song.get('name', '未知')
            artist = song.get('artist', '未知歌手')
            album = song.get('album', '')
            duration = song.get('duration', 0)
            item_text = f"{name}  -  {artist}"
            if album:
                item_text += f"  |  《{album}》"
            if duration:
                item_text += f"  [{_format_duration(duration)}]"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, song)
            self.result_list.addItem(item)

        self.result_label.setText(f"搜索结果（共 {len(songs)} 首）")
        InfoBar.success("搜索成功", f"找到 {len(songs)} 首歌曲", duration=2000, parent=self)
        self.search_btn.setEnabled(True)
        self.search_btn.setText("搜索")

    def _no_results(self, keyword):
        self.result_list.clear()
        self.result_label.setText(f"未找到「{keyword}」相关歌曲")
        InfoBar.info("未找到结果", f"未找到与「{keyword}」相关的歌曲", duration=3000, parent=self)
        self.search_btn.setEnabled(True)
        self.search_btn.setText("搜索")

    def _search_error(self, keyword):
        self.result_list.clear()
        self.result_label.setText("搜索失败")
        InfoBar.error("搜索失败", "网络连接异常，请稍后重试", duration=3000, parent=self)
        self.search_btn.setEnabled(True)
        self.search_btn.setText("搜索")

    def on_item_double_clicked(self, item):
        song_data = item.data(Qt.ItemDataRole.UserRole)
        if song_data:
            self.song_selected.emit(song_data)

    def _on_context_menu(self, pos):
        item = self.result_list.itemAt(pos)
        if not item:
            return
        song_data = item.data(Qt.ItemDataRole.UserRole)
        if not song_data:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        play_action = menu.addAction("▶ 播放")
        add_action = menu.addAction("＋ 添加到播放列表")
        action = menu.exec(self.result_list.mapToGlobal(pos))
        if action == play_action:
            self.song_selected.emit(song_data)
        elif action == add_action:
            self.add_to_playlist.emit(song_data)

    def update_now_playing(self, name, artist, album):
        self.np_song.setText(name)
        self.np_artist.setText(f"歌手：{artist}")
        self.np_album.setText(f"专辑：{album}")


# ---------------------------- 波点音乐搜索页面 ----------------------------
class BodianSearchPage(QWidget):

    song_selected = pyqtSignal(dict)
    add_to_playlist = pyqtSignal(dict)
    search_finished = pyqtSignal(list, str)
    no_results = pyqtSignal(str)
    search_error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.search_results = []

        self.search_finished.connect(self._display_results)
        self.no_results.connect(self._no_results)
        self.search_error.connect(self._search_error)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        title = TitleLabel("波点音乐搜索")
        layout.addWidget(title)

        search_layout = QHBoxLayout()
        self.search_input = SearchLineEdit()
        self.search_input.setPlaceholderText("输入歌曲名搜索...")
        self.search_input.returnPressed.connect(self.search_song)
        search_layout.addWidget(self.search_input)

        self.search_btn = PrimaryPushButton("搜索")
        self.search_btn.setFixedWidth(100)
        self.search_btn.clicked.connect(self.search_song)
        search_layout.addWidget(self.search_btn)
        layout.addLayout(search_layout)

        self.result_label = BodyLabel("搜索结果")
        layout.addWidget(self.result_label)

        self.result_list = QListWidget()
        self.result_list.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.result_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.result_list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.result_list)

        self.now_playing_card = SimpleCardWidget()
        np_layout = QVBoxLayout(self.now_playing_card)
        np_layout.setContentsMargins(15, 15, 15, 15)

        np_header = QHBoxLayout()
        np_icon = IconWidget()
        np_icon.setIcon(FIF.MUSIC)
        np_icon.setFixedSize(24, 24)
        np_header.addWidget(np_icon)
        np_title = BodyLabel("正在播放")
        np_title.setStyleSheet("font-weight: bold;")
        np_header.addWidget(np_title)
        np_header.addStretch()
        np_layout.addLayout(np_header)

        self.np_song = CaptionLabel("未选择歌曲")
        np_layout.addWidget(self.np_song)
        self.np_artist = CaptionLabel("")
        self.np_artist.setTextColor(150, 150, 150)
        np_layout.addWidget(self.np_artist)
        self.np_album = CaptionLabel("")
        self.np_album.setTextColor(150, 150, 150)
        np_layout.addWidget(self.np_album)

        layout.addWidget(self.now_playing_card)
        layout.addStretch()

    def search_song(self):
        keyword = self.search_input.text().strip()
        if not keyword:
            InfoBar.warning("提示", "请输入歌曲名", duration=2000, parent=self)
            return

        self.search_btn.setEnabled(False)
        self.search_btn.setText("搜索中...")
        self.result_list.clear()
        self.search_results = []

        threading.Thread(target=self._do_search, args=(keyword,), daemon=True).start()

    def _do_search(self, keyword):
        try:
            encoded = urllib.parse.quote(keyword)
            songs = []
            for n in range(1, 11):
                url = f"{API_BDYY}?msg={encoded}&n={n}&type=json"
                try:
                    resp = requests.get(url, timeout=10)
                    data = resp.json()
                    if data.get('code') == 200 and data.get('data'):
                        d = data['data']
                        songs.append({
                            'source': 'bodian',
                            'name': d.get('name', '未知'),
                            'artist': d.get('artist', '未知歌手'),
                            'album': '',
                            'cover': d.get('cover', ''),
                            'play_url': d.get('play_url', ''),
                            'lrc': d.get('lrc', ''),
                        })
                except Exception:
                    continue

            if songs:
                self.search_finished.emit(songs, keyword)
            else:
                self.no_results.emit(keyword)

        except Exception:
            self.search_error.emit(keyword)

    def _display_results(self, songs, keyword):
        self.search_results = songs
        self.result_list.clear()
        for song in songs:
            name = song.get('name', '未知')
            artist = song.get('artist', '未知歌手')
            item_text = f"{name}  -  {artist}"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, song)
            self.result_list.addItem(item)

        self.result_label.setText(f"搜索结果（共 {len(songs)} 首）")
        InfoBar.success("搜索成功", f"找到 {len(songs)} 首歌曲", duration=2000, parent=self)
        self.search_btn.setEnabled(True)
        self.search_btn.setText("搜索")

    def _no_results(self, keyword):
        self.result_list.clear()
        self.result_label.setText(f"未找到「{keyword}」相关歌曲")
        InfoBar.info("未找到结果", f"未找到与「{keyword}」相关的歌曲", duration=3000, parent=self)
        self.search_btn.setEnabled(True)
        self.search_btn.setText("搜索")

    def _search_error(self, keyword):
        self.result_list.clear()
        self.result_label.setText("搜索失败")
        InfoBar.error("搜索失败", "网络连接异常，请稍后重试", duration=3000, parent=self)
        self.search_btn.setEnabled(True)
        self.search_btn.setText("搜索")

    def on_item_double_clicked(self, item):
        song_data = item.data(Qt.ItemDataRole.UserRole)
        if song_data:
            self.song_selected.emit(song_data)

    def _on_context_menu(self, pos):
        item = self.result_list.itemAt(pos)
        if not item:
            return
        song_data = item.data(Qt.ItemDataRole.UserRole)
        if not song_data:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        play_action = menu.addAction("▶ 播放")
        add_action = menu.addAction("＋ 添加到播放列表")
        action = menu.exec(self.result_list.mapToGlobal(pos))
        if action == play_action:
            self.song_selected.emit(song_data)
        elif action == add_action:
            self.add_to_playlist.emit(song_data)

    def update_now_playing(self, name, artist, album):
        self.np_song.setText(name)
        self.np_artist.setText(f"歌手：{artist}")
        self.np_album.setText(f"专辑：{album}")


# ---------------------------- 播放列表页面 ----------------------------
class PlaylistPage(QWidget):

    song_selected = pyqtSignal(dict, int)
    play_mode_changed = pyqtSignal(str)
    remove_song_requested = pyqtSignal(int)
    clear_playlist_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        title = TitleLabel("播放列表")
        layout.addWidget(title)

        # 播放模式选择
        mode_layout = QHBoxLayout()
        mode_label = BodyLabel("播放模式：")
        mode_layout.addWidget(mode_label)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["单曲循环 🔂", "列表循环 🔁", "随机播放 🔀"])
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.setFixedWidth(160)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_layout.addWidget(self.mode_combo)
        mode_layout.addStretch()
        layout.addLayout(mode_layout)

        # 歌曲列表
        self.song_count_label = CaptionLabel("共 0 首歌曲")
        self.song_count_label.setTextColor(150, 150, 150)
        layout.addWidget(self.song_count_label)

        self.song_list = QListWidget()
        self.song_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.song_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.song_list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.song_list)

        # 操作按钮
        btn_layout = QHBoxLayout()
        self.remove_btn = PushButton("移除选中")
        self.remove_btn.clicked.connect(self._on_remove_clicked)
        btn_layout.addWidget(self.remove_btn)

        self.clear_btn = PushButton("清空列表")
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        btn_layout.addWidget(self.clear_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 当前播放信息
        self.now_playing_card = SimpleCardWidget()
        np_layout = QVBoxLayout(self.now_playing_card)
        np_layout.setContentsMargins(15, 15, 15, 15)

        np_header = QHBoxLayout()
        np_icon = IconWidget()
        np_icon.setIcon(FIF.MUSIC)
        np_icon.setFixedSize(24, 24)
        np_header.addWidget(np_icon)
        np_title = BodyLabel("当前播放")
        np_title.setStyleSheet("font-weight: bold;")
        np_header.addWidget(np_title)
        np_header.addStretch()
        np_layout.addLayout(np_header)

        self.np_song = CaptionLabel("未选择歌曲")
        np_layout.addWidget(self.np_song)
        self.np_artist = CaptionLabel("")
        self.np_artist.setTextColor(150, 150, 150)
        np_layout.addWidget(self.np_artist)
        self.np_album = CaptionLabel("")
        self.np_album.setTextColor(150, 150, 150)
        np_layout.addWidget(self.np_album)

        layout.addWidget(self.now_playing_card)
        layout.addStretch()

    def set_playlist(self, playlist, current_index=-1):
        self.song_list.clear()
        for i, song in enumerate(playlist):
            name = song.get('name', '未知')
            artist = song.get('artist', '未知歌手')
            album = song.get('album', '')
            item_text = f"{i + 1}. {name}  -  {artist}"
            if album:
                item_text += f"  |  《{album}》"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, song)
            self.song_list.addItem(item)
        self.song_count_label.setText(f"共 {len(playlist)} 首歌曲")
        if current_index >= 0 and current_index < len(playlist):
            self.song_list.setCurrentRow(current_index)

    def set_play_mode(self, mode):
        mode_map = {"single_loop": 0, "list_loop": 1, "shuffle": 2}
        idx = mode_map.get(mode, 0)
        if self.mode_combo.currentIndex() != idx:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(idx)
            self.mode_combo.blockSignals(False)

    def update_now_playing(self, name, artist, album=''):
        self.np_song.setText(name)
        self.np_artist.setText(f"歌手：{artist}")
        self.np_album.setText(f"专辑：{album}" if album else "")

    def _on_mode_changed(self, index):
        modes = ["single_loop", "list_loop", "shuffle"]
        self.play_mode_changed.emit(modes[index])

    def _on_item_double_clicked(self, item):
        song_data = item.data(Qt.ItemDataRole.UserRole)
        index = self.song_list.row(item)
        if song_data:
            self.song_selected.emit(song_data, index)

    def _on_context_menu(self, pos):
        item = self.song_list.itemAt(pos)
        if not item:
            return
        song_data = item.data(Qt.ItemDataRole.UserRole)
        if not song_data:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        play_action = menu.addAction("▶ 播放")
        remove_action = menu.addAction("🗑 从列表中删除")
        action = menu.exec(self.song_list.mapToGlobal(pos))
        if action == play_action:
            index = self.song_list.row(item)
            self.song_selected.emit(song_data, index)
        elif action == remove_action:
            index = self.song_list.row(item)
            self.remove_song_requested.emit(index)

    def _on_remove_clicked(self):
        row = self.song_list.currentRow()
        if row >= 0:
            self.remove_song_requested.emit(row)

    def _on_clear_clicked(self):
        self.clear_playlist_requested.emit()


# ---------------------------- 歌曲信息获取（后台线程） ----------------------------

class _SongWorker(QObject):
    """歌曲信息获取工作器：获取播放链接、歌词、封面"""
    song_ready = pyqtSignal(str, str, str)  # play_url, lrc_text, cover_url

    def __init__(self):
        super().__init__()
        self._connections = set()

    def fetch(self, song_data, callback):
        # 连接信号（避免重复连接）
        cb_id = id(callback)
        if cb_id not in self._connections:
            self.song_ready.connect(callback)
            self._connections.add(cb_id)
        threading.Thread(target=self._do_fetch, args=(song_data,), daemon=True).start()

    def _do_fetch(self, song_data):
        try:
            source = song_data.get('source', '')
            name = song_data.get('name', '')
            clean_name = re.sub(r'[（(【\[<\{].*?[）)】\]>\}]', '', name).strip()
            print(f"[SongWorker] 来源={source}, 歌名={clean_name}")

            play_url = ''
            lrc_text = ''
            cover_url = song_data.get('cover', '')

            if source == 'netease':
                play_url = song_data.get('play_url', '')
                if play_url:
                    try:
                        resp = requests.get(play_url, timeout=15, allow_redirects=True)
                        if resp.url != play_url:
                            play_url = resp.url
                        else:
                            text = resp.text.strip()
                            if text:
                                play_url = text
                        print(f"[SongWorker] 播放URL: {play_url[:80] if play_url else '无'}")
                    except Exception as e:
                        print(f"[SongWorker] 获取播放URL失败: {e}")
                        play_url = ''

                lrc_url = song_data.get('lrc_url', '')
                if lrc_url:
                    try:
                        resp = requests.get(lrc_url, timeout=10, allow_redirects=True)
                        lrc_text = resp.text or ''
                        print(f"[SongWorker] 歌词长度: {len(lrc_text)}")
                    except Exception as e:
                        print(f"[SongWorker] 获取歌词失败: {e}")

                pic_url = song_data.get('cover', '')
                if pic_url:
                    try:
                        resp = requests.get(pic_url, timeout=10, allow_redirects=True)
                        if resp.url != pic_url:
                            cover_url = resp.url
                        else:
                            cover_url = pic_url
                        print(f"[SongWorker] 封面: {cover_url[:80] if cover_url else '无'}")
                    except Exception as e:
                        print(f"[SongWorker] 获取封面失败: {e}")

            elif source == 'netease_backup':
                search_keyword = song_data.get('search_keyword', '')
                n = song_data.get('n', '')
                play_url = song_data.get('play_url', '')
                lrc_url = song_data.get('lrc_url', '')

                if not play_url and search_keyword and n:
                    try:
                        detail_resp = requests.get(API_NETEASE_BACKUP, params={
                            'msg': search_keyword, 'n': n, 'format': 'json'
                        }, timeout=10)
                        detail = detail_resp.json()
                        if detail.get('code') == 200 and isinstance(detail.get('data'), dict):
                            d = detail['data']
                            play_url = d.get('url', '')
                            lrc_url = d.get('lrc', '')
                            if not cover_url:
                                cover_url = d.get('pic', '')
                            print(f"[SongWorker] 备用API详情获取成功")
                    except Exception as e:
                        print(f"[SongWorker] 备用API获取详情失败: {e}")

                if play_url:
                    try:
                        resp = requests.get(play_url, params={'format': 'url'}, timeout=15, allow_redirects=True)
                        text = resp.text.strip()
                        if text and text.startswith('http'):
                            play_url = text
                        elif resp.url != play_url:
                            play_url = resp.url
                        print(f"[SongWorker] 备用API播放URL: {play_url[:80] if play_url else '无'}")
                    except Exception as e:
                        print(f"[SongWorker] 备用API获取播放URL失败: {e}")
                        play_url = ''

                if lrc_url:
                    try:
                        resp = requests.get(lrc_url, timeout=10, allow_redirects=True)
                        lrc_text = resp.text or ''
                        print(f"[SongWorker] 备用API歌词长度: {len(lrc_text)}")
                    except Exception as e:
                        print(f"[SongWorker] 备用API获取歌词失败: {e}")

            elif source == 'bodian':
                play_url = song_data.get('play_url', '')
                lrc_text = song_data.get('lrc', '')

                # 清理歌词前缀（去掉非时间戳行）
                if lrc_text:
                    lrc_text = lrc_text.replace('\r\n', '\n').replace('\r', '\n')
                    lines = lrc_text.strip().split('\n')
                    cleaned = []
                    for line in lines:
                        line = line.strip()
                        if line and re.match(r'\[\d{2}:\d{2}', line):
                            cleaned.append(line)
                    lrc_text = '\n'.join(cleaned)

            print(f"[SongWorker] 完成: play_url={'有' if play_url else '无'}, lrc={'有' if lrc_text else '无'}")
            self.song_ready.emit(play_url, lrc_text, cover_url)

        except Exception as e:
            print(f"[SongWorker] 获取失败: {e}")
            import traceback
            traceback.print_exc()
            self.song_ready.emit('', '', song_data.get('cover', ''))


class SongFetcher:
    """歌曲信息获取器（封装 Worker）"""
    def __init__(self):
        self.worker = _SongWorker()

    def fetch(self, song_data, callback):
        self.worker.fetch(song_data, callback)


def _read_audio_metadata(filepath):
    """读取音频文件内嵌的歌词、封面、元数据"""
    ext = os.path.splitext(filepath)[1].lower()
    result = {'title': '', 'artist': '', 'album': '', 'lrc_text': '', 'cover_data': b''}
    try:
        if ext == '.mp3':
            audio = MP3(filepath, ID3=ID3)
            if audio.tags:
                if audio.tags.get('TIT2'):
                    result['title'] = str(audio.tags['TIT2'])
                if audio.tags.get('TPE1'):
                    result['artist'] = str(audio.tags['TPE1'])
                if audio.tags.get('TALB'):
                    result['album'] = str(audio.tags['TALB'])
                for tag in audio.tags.getall('USLT'):
                    result['lrc_text'] = tag.text
                    break
                for tag in audio.tags.getall('APIC'):
                    result['cover_data'] = tag.data
                    break
        elif ext in ('.m4a', '.mp4', '.aac'):
            audio = MP4(filepath)
            if audio.tags:
                result['title'] = audio.tags.get('\xa9nam', [''])[0]
                result['artist'] = audio.tags.get('\xa9ART', [''])[0]
                result['album'] = audio.tags.get('\xa9alb', [''])[0]
                covr = audio.tags.get('covr', [])
                if covr:
                    result['cover_data'] = covr[0]
                for k, v in audio.tags.items():
                    if k == '\xa9lyr':
                        result['lrc_text'] = v[0] if v else ''
                        break
        elif ext == '.flac':
            audio = FLAC(filepath)
            result['title'] = audio.get('title', [''])[0]
            result['artist'] = audio.get('artist', [''])[0]
            result['album'] = audio.get('album', [''])[0]
            result['lrc_text'] = audio.get('lyrics', [''])[0]
            if audio.pictures:
                result['cover_data'] = audio.pictures[0].data
        elif ext == '.ogg':
            audio = OggVorbis(filepath)
            result['title'] = audio.get('title', [''])[0]
            result['artist'] = audio.get('artist', [''])[0]
            result['album'] = audio.get('album', [''])[0]
            result['lrc_text'] = audio.get('lyrics', [''])[0]
    except Exception as e:
        print(f"[LocalMusic] 读取元数据失败: {e}")
    return result


def _find_sibling_files(audio_path):
    """在音频文件同目录下搜索同名歌词和封面"""
    folder = os.path.dirname(audio_path)
    base = os.path.splitext(os.path.basename(audio_path))[0]
    lrc_text = ''
    cover_data = b''
    cover_path = ''

    for f in os.listdir(folder):
        name_no_ext = os.path.splitext(f)[0]
        f_ext = os.path.splitext(f)[1].lower()
        if name_no_ext == base:
            if f_ext == '.lrc':
                try:
                    with open(os.path.join(folder, f), 'r', encoding='utf-8', errors='ignore') as fh:
                        raw = fh.read()
                        lines = raw.strip().split('\n')
                        cleaned = []
                        for line in lines:
                            line = line.strip()
                            if line and re.match(r'\[\d{2}:\d{2}', line):
                                cleaned.append(line)
                        lrc_text = '\n'.join(cleaned)
                except Exception:
                    pass
            elif f_ext in ('.jpg', '.jpeg', '.png', '.bmp', '.webp'):
                cover_path = os.path.join(folder, f)
                try:
                    with open(cover_path, 'rb') as fh:
                        cover_data = fh.read()
                except Exception:
                    pass

    return lrc_text, cover_data, cover_path


# ---------------------------- 设置页面 ----------------------------
class SettingsPage(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        title = TitleLabel("设置")
        layout.addWidget(title)

        # API 设置卡片
        api_card = SimpleCardWidget()
        api_layout = QVBoxLayout(api_card)
        api_layout.setContentsMargins(15, 15, 15, 15)
        api_layout.setSpacing(10)

        api_header = QHBoxLayout()
        api_icon = IconWidget()
        api_icon.setIcon(FIF.GLOBE)
        api_icon.setFixedSize(24, 24)
        api_header.addWidget(api_icon)
        api_title = BodyLabel("网易云音乐 API")
        api_title.setStyleSheet("font-weight: bold;")
        api_header.addWidget(api_title)
        api_header.addStretch()
        api_layout.addLayout(api_header)

        api_desc = CaptionLabel("切换搜索和播放所使用的网易云 API 源")
        api_desc.setTextColor(150, 150, 150)
        api_layout.addWidget(api_desc)

        self.api_combo = ComboBox()
        self.api_combo.addItems(["默认 API (qijieya)", "备用 API (ffapi)"])
        self.api_combo.setFixedWidth(250)
        self.api_combo.currentIndexChanged.connect(self._on_api_changed)
        api_layout.addWidget(self.api_combo)

        layout.addWidget(api_card)

        # 关于卡片
        about_card = SimpleCardWidget()
        about_layout = QVBoxLayout(about_card)
        about_layout.setContentsMargins(15, 15, 15, 15)
        about_layout.setSpacing(8)

        about_header = QHBoxLayout()
        about_icon = IconWidget()
        about_icon.setIcon(FIF.INFO)
        about_icon.setFixedSize(24, 24)
        about_header.addWidget(about_icon)
        about_title = BodyLabel("关于软件")
        about_title.setStyleSheet("font-weight: bold;")
        about_header.addWidget(about_title)
        about_header.addStretch()
        about_layout.addLayout(about_header)

        about_name = BodyLabel("灵动音乐盒")
        about_layout.addWidget(about_name)

        about_author = CaptionLabel("作者：文宇香香（文宇香香工作室）")
        about_author.setTextColor(180, 180, 180)
        about_layout.addWidget(about_author)

        about_ver = CaptionLabel("版本：1.0.0")
        about_ver.setTextColor(150, 150, 150)
        about_layout.addWidget(about_ver)

        about_desc = CaptionLabel(
            "支持网易云音乐、波点音乐在线搜索播放，\n"
            "支持本地音乐播放，自动匹配歌词和封面。"
        )
        about_desc.setTextColor(150, 150, 150)
        about_layout.addWidget(about_desc)

        layout.addWidget(about_card)
        layout.addStretch()

    def _on_api_changed(self, index):
        _app_settings["netease_api"] = "backup" if index == 1 else "default"
        api_name = "备用 API" if index == 1 else "默认 API"
        InfoBar.success("已切换", f"网易云 API 已切换为{api_name}", duration=2000, parent=self)


# ---------------------------- 本地音乐页面 ----------------------------
class LocalMusicPage(QWidget):

    song_selected = pyqtSignal(dict)
    add_to_playlist = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.local_songs = []

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        title = TitleLabel("本地音乐")
        layout.addWidget(title)

        desc = CaptionLabel("支持 mp3 / flac / ogg / m4a 格式，自动匹配同名歌词和封面")
        desc.setTextColor(150, 150, 150)
        layout.addWidget(desc)

        btn_layout = QHBoxLayout()
        self.add_audio_btn = PrimaryPushButton("添加音频文件")
        self.add_audio_btn.clicked.connect(self._add_audio_files)
        btn_layout.addWidget(self.add_audio_btn)

        self.add_folder_btn = PushButton("添加文件夹")
        self.add_folder_btn.clicked.connect(self._add_folder)
        btn_layout.addWidget(self.add_folder_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        extra_layout = QHBoxLayout()
        self.add_lrc_btn = PushButton("指定歌词文件")
        self.add_lrc_btn.clicked.connect(self._add_lrc_file)
        extra_layout.addWidget(self.add_lrc_btn)

        self.add_cover_btn = PushButton("指定封面图片")
        self.add_cover_btn.clicked.connect(self._add_cover_file)
        extra_layout.addWidget(self.add_cover_btn)
        extra_layout.addStretch()
        layout.addLayout(extra_layout)

        self.song_count_label = CaptionLabel("共 0 首本地歌曲")
        self.song_count_label.setTextColor(150, 150, 150)
        layout.addWidget(self.song_count_label)

        self.song_list = QListWidget()
        self.song_list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.song_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.song_list.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.song_list)

        self.now_playing_card = SimpleCardWidget()
        np_layout = QVBoxLayout(self.now_playing_card)
        np_layout.setContentsMargins(15, 15, 15, 15)

        np_header = QHBoxLayout()
        np_icon = IconWidget()
        np_icon.setIcon(FIF.MUSIC)
        np_icon.setFixedSize(24, 24)
        np_header.addWidget(np_icon)
        np_title = BodyLabel("当前播放")
        np_title.setStyleSheet("font-weight: bold;")
        np_header.addWidget(np_title)
        np_header.addStretch()
        np_layout.addLayout(np_header)

        self.np_song = CaptionLabel("未选择歌曲")
        np_layout.addWidget(self.np_song)
        self.np_artist = CaptionLabel("")
        self.np_artist.setTextColor(150, 150, 150)
        np_layout.addWidget(self.np_artist)
        self.np_album = CaptionLabel("")
        self.np_album.setTextColor(150, 150, 150)
        np_layout.addWidget(self.np_album)

        layout.addWidget(self.now_playing_card)
        layout.addStretch()

    def _add_audio_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择音频文件", "",
            "音频文件 (*.mp3 *.flac *.ogg *.m4a *.aac *.wav *.wma);;所有文件 (*)"
        )
        if paths:
            for p in paths:
                self._process_audio_file(p)
            self._refresh_list()

    def _add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            count = 0
            for f in os.listdir(folder):
                ext = os.path.splitext(f)[1].lower()
                if ext in ('.mp3', '.flac', '.ogg', '.m4a', '.aac', '.wav', '.wma'):
                    self._process_audio_file(os.path.join(folder, f))
                    count += 1
            self._refresh_list()
            if count == 0:
                InfoBar.info("提示", "该文件夹下没有找到音频文件", duration=2000, parent=self)

    def _process_audio_file(self, filepath, lrc_override='', cover_override=''):
        for s in self.local_songs:
            if s.get('local_path') == filepath:
                return

        meta = _read_audio_metadata(filepath)
        base_name = os.path.splitext(os.path.basename(filepath))[0]

        title = meta['title'] or base_name
        artist = meta['artist'] or '未知歌手'
        album = meta['album'] or ''

        lrc_text = lrc_override or meta['lrc_text']
        cover_data = meta['cover_data']
        cover_path = cover_override

        if not lrc_text or not cover_data:
            sib_lrc, sib_cover, sib_cover_path = _find_sibling_files(filepath)
            if not lrc_text:
                lrc_text = sib_lrc
            if not cover_data:
                cover_data = sib_cover
            if not cover_path:
                cover_path = sib_cover_path

        cover_b64 = ''
        if cover_data:
            import base64
            cover_b64 = base64.b64encode(cover_data).decode('ascii')

        song = {
            'source': 'local',
            'name': title,
            'artist': artist,
            'album': album,
            'local_path': filepath,
            'lrc_text': lrc_text,
            'cover_data_b64': cover_b64,
            'cover': cover_path or '',
        }
        self.local_songs.append(song)

    def _add_lrc_file(self):
        if not self.local_songs:
            InfoBar.warning("提示", "请先添加音频文件", duration=2000, parent=self)
            return
        row = self.song_list.currentRow()
        if row < 0:
            InfoBar.warning("提示", "请先选中一首歌曲", duration=2000, parent=self)
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择歌词文件", "",
            "歌词文件 (*.lrc);;所有文件 (*)"
        )
        if path:
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    raw = f.read()
                    lines = raw.strip().split('\n')
                    cleaned = []
                    for line in lines:
                        line = line.strip()
                        if line and re.match(r'\[\d{2}:\d{2}', line):
                            cleaned.append(line)
                    lrc_text = '\n'.join(cleaned)
            except Exception:
                lrc_text = ''
            self.local_songs[row]['lrc_text'] = lrc_text
            InfoBar.success("已关联", f"歌词已关联到「{self.local_songs[row]['name']}」", duration=2000, parent=self)

    def _add_cover_file(self):
        if not self.local_songs:
            InfoBar.warning("提示", "请先添加音频文件", duration=2000, parent=self)
            return
        row = self.song_list.currentRow()
        if row < 0:
            InfoBar.warning("提示", "请先选中一首歌曲", duration=2000, parent=self)
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "",
            "图片文件 (*.jpg *.jpeg *.png *.bmp *.webp);;所有文件 (*)"
        )
        if path:
            try:
                with open(path, 'rb') as f:
                    cover_data = f.read()
                import base64
                cover_b64 = base64.b64encode(cover_data).decode('ascii')
            except Exception:
                cover_b64 = ''
            self.local_songs[row]['cover_data_b64'] = cover_b64
            self.local_songs[row]['cover'] = path
            InfoBar.success("已关联", f"封面已关联到「{self.local_songs[row]['name']}」", duration=2000, parent=self)

    def _refresh_list(self):
        self.song_list.clear()
        for song in self.local_songs:
            name = song.get('name', '未知')
            artist = song.get('artist', '未知歌手')
            album = song.get('album', '')
            item_text = f"{name}  -  {artist}"
            if album:
                item_text += f"  |  《{album}》"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, song)
            self.song_list.addItem(item)
        self.song_count_label.setText(f"共 {len(self.local_songs)} 首本地歌曲")

    def _on_item_double_clicked(self, item):
        song_data = item.data(Qt.ItemDataRole.UserRole)
        if song_data:
            self.song_selected.emit(song_data)

    def _on_context_menu(self, pos):
        item = self.song_list.itemAt(pos)
        if not item:
            return
        song_data = item.data(Qt.ItemDataRole.UserRole)
        if not song_data:
            return
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        play_action = menu.addAction("▶ 播放")
        add_action = menu.addAction("＋ 添加到播放列表")
        action = menu.exec(self.song_list.mapToGlobal(pos))
        if action == play_action:
            self.song_selected.emit(song_data)
        elif action == add_action:
            self.add_to_playlist.emit(song_data)

    def update_now_playing(self, name, artist, album=''):
        self.np_song.setText(name)
        self.np_artist.setText(f"歌手：{artist}" if artist else "")
        self.np_album.setText(f"专辑：{album}" if album else "")


# ---------------------------- 播放器主窗口 ----------------------------
class MusicPlayer(FluentWindow):
    _play_local_signal = pyqtSignal(str)
    _play_url_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        setTheme(Theme.DARK)
        setThemeColor("#0078D4")

        self._play_local_signal.connect(self._do_temp_play)
        self._play_url_signal.connect(self._do_fallback_play)

        # 灵动岛（独立窗口）
        self.island = DynamicIsland()
        self.island.play_paused.connect(self.toggle_play)
        self.island.next_song.connect(self.next_song)
        self.island.prev_song.connect(self.prev_song)
        self.island.play_mode_changed.connect(self._on_play_mode_changed)
        self.island.show()
        QTimer.singleShot(300, self.island._animate_to_expanded_mode)

        # 搜索页面（网易云 - 默认）
        self.search_page = NeteaseSearchPage()
        self.search_page.setObjectName("NeteaseSearchPage")
        self.search_page.song_selected.connect(self.on_song_selected)
        self.search_page.add_to_playlist.connect(self._on_add_to_playlist)

        self.addSubInterface(self.search_page, FIF.SEARCH, "网易云音乐搜索")

        # 搜索页面（波点音乐）
        self.bodian_page = BodianSearchPage()
        self.bodian_page.setObjectName("BodianSearchPage")
        self.bodian_page.song_selected.connect(self.on_song_selected)
        self.bodian_page.add_to_playlist.connect(self._on_add_to_playlist)

        self.addSubInterface(self.bodian_page, FIF.MUSIC, "波点音乐搜索")

        # 本地音乐页面
        self.local_page = LocalMusicPage()
        self.local_page.setObjectName("LocalMusicPage")
        self.local_page.song_selected.connect(self._on_local_song_selected)
        self.local_page.add_to_playlist.connect(self._on_add_to_playlist)

        self.addSubInterface(self.local_page, FIF.FOLDER, "本地音乐")

        # 播放列表页面
        self.playlist_page = PlaylistPage()
        self.playlist_page.setObjectName("PlaylistPage")
        self.playlist_page.song_selected.connect(self._on_playlist_song_selected)
        self.playlist_page.play_mode_changed.connect(self._on_play_mode_changed)
        self.playlist_page.remove_song_requested.connect(self._on_remove_song)
        self.playlist_page.clear_playlist_requested.connect(self._on_clear_playlist)

        self.addSubInterface(self.playlist_page, FIF.PLAY_SOLID, "播放列表")

        # 设置页面
        self.settings_page = SettingsPage()
        self.settings_page.setObjectName("SettingsPage")

        self.addSubInterface(self.settings_page, FIF.SETTING, "设置")

        self.setWindowTitle("灵动音乐盒")
        self.resize(500, 700)
        self.closeEvent = self.hide_event

        # 歌曲信息获取器
        self.song_fetcher = SongFetcher()

        # 播放器
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(50)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)

        # 播放状态
        self.current_song = {}
        self.total_duration_ms = 0
        self.play_mode = "single_loop"

        # 播放列表
        self.playlist = []
        self.current_index = -1

        # 系统托盘
        self.setup_tray()

        InfoBar.success(
            title="欢迎使用",
            content="搜索歌曲后双击播放，关闭主窗口后灵动岛仍会保留",
            duration=4000,
            parent=self
        )

    def on_song_selected(self, song_data):
        """双击歌曲 -> 添加到播放列表并播放"""
        self.playlist.append(song_data)
        self.current_index = len(self.playlist) - 1
        self.playlist_page.set_playlist(self.playlist, self.current_index)
        self._play_song(song_data)

    def _on_add_to_playlist(self, song_data):
        """右键添加到播放列表"""
        self.playlist.append(song_data)
        self.playlist_page.set_playlist(self.playlist, self.current_index)
        name = song_data.get('name', '未知')
        artist = song_data.get('artist', '未知歌手')
        InfoBar.success("已添加", f"{name} - {artist}", duration=2000, parent=self)

    def _on_playlist_song_selected(self, song_data, index):
        """播放列表选择歌曲"""
        self.current_index = index
        self.playlist_page.set_playlist(self.playlist, self.current_index)
        self._play_song(song_data)

    def _on_local_song_selected(self, song_data):
        """本地音乐双击播放"""
        self.playlist.append(song_data)
        self.current_index = len(self.playlist) - 1
        self.playlist_page.set_playlist(self.playlist, self.current_index)
        self.local_page.update_now_playing(song_data.get('name', ''), song_data.get('artist', ''), song_data.get('album', ''))
        self._play_song(song_data)

    def _play_song(self, song_data):
        """播放指定歌曲"""
        self.current_song = song_data
        source = song_data.get('source', '')
        name = song_data.get('name', '未知')
        artist = song_data.get('artist', '未知歌手')
        album = song_data.get('album', '')
        cover_url = song_data.get('cover', '')

        # 更新所有页面
        self.search_page.update_now_playing(name, artist, album)
        self.bodian_page.update_now_playing(name, artist, album)
        self.playlist_page.update_now_playing(name, artist, album)
        self.local_page.update_now_playing(name, artist, album)

        # 停止当前播放
        self.player.stop()

        # 本地音乐直接播放
        if source == 'local':
            local_path = song_data.get('local_path', '')
            lrc_text = song_data.get('lrc_text', '')
            cover_data_b64 = song_data.get('cover_data_b64', '')
            cover_file = song_data.get('cover', '')

            # 先显示基本信息到灵动岛
            cover_display = cover_file or ''
            self.island.update_song_info(name, artist, album, cover_display, "")
            self.island.update_progress(0, 0, 0)
            self.island.current_position_ms = 0

            if local_path and os.path.exists(local_path):
                self.player.setSource(QUrl.fromLocalFile(local_path))
                self.player.play()

            if cover_data_b64:
                import base64
                cover_bytes = base64.b64decode(cover_data_b64)
                pixmap = QPixmap()
                if pixmap.loadFromData(cover_bytes):
                    self.island.cover_label.setPixmap(pixmap.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    self.island.expand_cover.setPixmap(pixmap.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

            if lrc_text:
                self.island.set_lyrics([], lrc_text)

            InfoBar.success("开始播放", f"{name} - {artist}", duration=2000, parent=self)
            return

        # 先显示基本信息到灵动岛
        self.island.update_song_info(name, artist, album, cover_url, "")
        self.island.update_progress(0, 0, 0)
        self.island.current_position_ms = 0

        # 波点音乐有直接播放链接，先下载到本地再播放（避免 mp3float TLS 问题）
        if source == 'bodian':
            play_url = song_data.get('play_url', '')
            if play_url:
                threading.Thread(target=self._play_bodian_url, args=(play_url,), daemon=True).start()

        # 异步获取播放链接（网易云）和歌词
        self.song_fetcher.fetch(song_data, self._on_song_ready)

        InfoBar.success("开始播放", f"{name} - {artist}", duration=2000, parent=self)

    def _on_song_ready(self, play_url, lrc_text, cover_url):
        """歌曲信息获取完成回调"""
        print(f"[主线程] 歌曲回调: play_url={'有' if play_url else '无'}, 歌词长度={len(lrc_text) if lrc_text else 0}")

        source = self.current_song.get('source', '')

        if play_url and source != 'bodian':
            threading.Thread(target=self._download_and_play, args=(play_url,), daemon=True).start()
        elif not play_url and source == 'netease':
            InfoBar.warning("无法播放", "获取播放链接失败", duration=3000, parent=self)

        if cover_url:
            print(f"[主线程] 更新封面")
            self.island._load_cover_from_url(cover_url)

        if lrc_text:
            print(f"[主线程] 设置歌词到灵动岛")
            self.island.set_lyrics([], lrc_text)
            if play_url:
                InfoBar.success("歌词已匹配", "已加载歌词", duration=2000, parent=self)
        else:
            InfoBar.warning("无歌词", "未找到歌词", duration=3000, parent=self)

    def _play_bodian_url(self, play_url):
        """后台下载波点音乐 URL 后播放"""
        local_path = _download_to_temp(play_url)
        if local_path and os.path.exists(local_path):
            self._play_local_signal.emit(local_path)
        else:
            print("[播放] 波点音乐下载失败")

    def _download_and_play(self, play_url):
        """后台下载在线 URL 后播放（解决 mp3float TLS/backstep 问题）"""
        local_path = _download_to_temp(play_url)
        if local_path and os.path.exists(local_path):
            self._play_local_signal.emit(local_path)
        else:
            print("[播放] 下载失败，回退到 URL 播放")
            self._play_url_signal.emit(play_url)

    def _do_temp_play(self, local_path):
        """在主线程中从临时文件播放"""
        print(f"[播放] 从本地临时文件播放: {local_path}")
        self.player.setSource(QUrl.fromLocalFile(local_path))
        self.player.play()

    def _do_fallback_play(self, url):
        """回退：直接用 URL 播放"""
        print(f"[播放] 回退 URL 播放: {url[:80]}")
        self.player.setSource(QUrl(url))
        self.player.play()

    def _on_position_changed(self, position):
        """播放进度变化"""
        self.island.current_position_ms = position
        self.island.update_lyric_by_position(position)
        if self.total_duration_ms > 0:
            value = int(position / self.total_duration_ms * 100)
            current_sec = position // 1000
            total_sec = self.total_duration_ms // 1000
            self.island.update_progress(value, current_sec, total_sec)

    def _on_duration_changed(self, duration):
        """歌曲时长变化"""
        self.total_duration_ms = duration

    def _on_playback_state_changed(self, state):
        """播放状态变化"""
        is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.island.update_play_state(is_playing)
        status_map = {
            QMediaPlayer.PlaybackState.StoppedState: "已停止",
            QMediaPlayer.PlaybackState.PlayingState: "播放中",
            QMediaPlayer.PlaybackState.PausedState: "已暂停",
        }
        print(f"[播放] 状态: {status_map.get(state, str(state))}")

    def _on_media_status_changed(self, status):
        """媒体状态变化"""
        status_map = {
            QMediaPlayer.MediaStatus.NoMedia: "无媒体",
            QMediaPlayer.MediaStatus.LoadingMedia: "加载中",
            QMediaPlayer.MediaStatus.LoadedMedia: "已加载",
            QMediaPlayer.MediaStatus.StalledMedia: "缓冲中",
            QMediaPlayer.MediaStatus.BufferingMedia: "缓冲中",
            QMediaPlayer.MediaStatus.BufferedMedia: "已缓冲",
            QMediaPlayer.MediaStatus.EndOfMedia: "播放结束",
            QMediaPlayer.MediaStatus.InvalidMedia: "无效媒体",
        }
        desc = status_map.get(status, f"未知({status})")
        print(f"[播放] 媒体状态: {desc}")

        if status == QMediaPlayer.MediaStatus.StalledMedia:
            print(f"[播放] 网络缓冲中...")

        # 播放结束时根据播放模式处理
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.play_mode == "single_loop":
                self.player.setPosition(0)
                self.player.play()
            elif self.play_mode in ("list_loop", "shuffle"):
                self.next_song()

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def next_song(self):
        """播放下一首"""
        if not self.playlist:
            InfoBar.info("提示", "播放列表为空", duration=2000, parent=self)
            return
        if self.play_mode == "shuffle":
            import random
            self.current_index = random.randint(0, len(self.playlist) - 1)
        else:
            self.current_index = (self.current_index + 1) % len(self.playlist)
        self.playlist_page.set_playlist(self.playlist, self.current_index)
        self._play_song(self.playlist[self.current_index])

    def prev_song(self):
        """播放上一首"""
        if not self.playlist:
            InfoBar.info("提示", "播放列表为空", duration=2000, parent=self)
            return
        if self.play_mode == "shuffle":
            import random
            self.current_index = random.randint(0, len(self.playlist) - 1)
        else:
            self.current_index = (self.current_index - 1) % len(self.playlist)
        self.playlist_page.set_playlist(self.playlist, self.current_index)
        self._play_song(self.playlist[self.current_index])

    def _on_play_mode_changed(self, mode):
        """处理播放模式切换"""
        self.play_mode = mode
        self.island.update_play_mode(mode)
        mode_names = {"single_loop": "单曲循环", "list_loop": "列表循环", "shuffle": "随机播放"}
        InfoBar.info("播放模式", f"已切换为{mode_names.get(mode, mode)}", duration=1500, parent=self)
        self.playlist_page.set_play_mode(mode)

    def _on_remove_song(self, index):
        """从播放列表移除歌曲"""
        if 0 <= index < len(self.playlist):
            self.playlist.pop(index)
            if self.current_index >= len(self.playlist):
                self.current_index = len(self.playlist) - 1
            self.playlist_page.set_playlist(self.playlist, self.current_index)

    def _on_clear_playlist(self):
        """清空播放列表"""
        self.playlist.clear()
        self.current_index = -1
        self.playlist_page.set_playlist(self.playlist, self.current_index)

    def setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon.fromTheme("media-playback-start"))
        self.tray_icon.setToolTip("灵动音乐盒")

        tray_menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self.show_normal)
        play_action = QAction("播放/暂停", self)
        play_action.triggered.connect(self.toggle_play)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)

        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(play_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_activated)
        self.tray_icon.show()

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.show_normal()

    def show_normal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_event(self, event):
        event.ignore()
        self.hide()

    def quit_app(self):
        self.player.stop()
        _cleanup_temp_files()
        self.island.close()
        self.tray_icon.hide()
        QApplication.quit()


if __name__ == "__main__":
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    window = MusicPlayer()
    window.show()
    sys.exit(app.exec())
