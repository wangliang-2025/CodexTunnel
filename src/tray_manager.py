import threading
from typing import Callable, Optional
from PIL import Image, ImageDraw
import pystray
from src.resources import asset_path

class TrayManager:
    """系统托盘图标管理器"""

    def __init__(self,
                 on_show_window: Callable[[], None],
                 on_toggle_tunnel: Callable[[], None],
                 on_exit_app: Callable[[], None]):
        self.on_show_window = on_show_window
        self.on_toggle_tunnel = on_toggle_tunnel
        self.on_exit_app = on_exit_app

        self.tray_icon: Optional[pystray.Icon] = None
        self._is_running = False

    @staticmethod
    def create_tray_image(color: str = "#22c55e") -> Image.Image:
        """生成带状态色的托盘图标"""
        size = 64
        source = asset_path('assets/tunnel-icon.png')
        if source.exists():
            with Image.open(source) as mascot:
                image = mascot.convert('RGBA').resize((size, size), Image.Resampling.LANCZOS)
            draw = ImageDraw.Draw(image)
            draw.ellipse([(43, 43), (63, 63)], fill=color, outline='#FFFFFF', width=3)
            return image
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        
        # 绘制背景深色圆角矩形
        draw.rounded_rectangle([(4, 4), (60, 60)], radius=16, fill="#1e293b", outline="#334155", width=2)
        
        # 绘制中心状态圆点
        draw.ellipse([(20, 20), (44, 44)], fill=color)
        return image

    def start(self):
        """在后台线程中启动系统托盘"""
        if self._is_running:
            return

        image = self.create_tray_image("#94a3b8")  # 默认灰色待机
        menu = pystray.Menu(
            pystray.MenuItem("打开主窗口", self._on_menu_show, default=True),
            pystray.MenuItem("启停隧道", self._on_menu_toggle),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("完全退出", self._on_menu_exit)
        )

        self.tray_icon = pystray.Icon("CodexTunnel", image, "Codex 代理隧道助手", menu)
        self._is_running = True

        tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True, name="TrayThread")
        tray_thread.start()

    def update_status(self, state: str, text: str):
        """根据当前状态刷新托盘图标与提示文字"""
        if not self.tray_icon:
            return

        color_map = {
            "CONNECTED": "#22c55e",     # 绿色正常
            "CONNECTING": "#eab308",    # 黄色连接中
            "RECONNECTING": "#f97316",  # 橙色重连
            "ERROR": "#ef4444",         # 红色错误
            "STOPPED": "#64748b"        # 灰色停止
            , "PRECHECKING": "#bc3d73", "STOPPING": "#eab308"
        }
        color = color_map.get(state, "#64748b")
        try:
            self.tray_icon.icon = self.create_tray_image(color)
            self.tray_icon.title = f"Codex 隧道助手: {text[:40]}"
        except Exception:
            pass

    def stop(self):
        """停止并移除托盘图标"""
        self._is_running = False
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

    def _on_menu_show(self, icon, item):
        self.on_show_window()

    def _on_menu_toggle(self, icon, item):
        self.on_toggle_tunnel()

    def _on_menu_exit(self, icon, item):
        self.on_exit_app()
