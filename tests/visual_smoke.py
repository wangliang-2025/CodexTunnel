"""UI rendering smoke checks use an isolated config and never connect."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from PIL import ImageGrab
import customtkinter as ctk
from src.ui_app import App

out=Path(__file__).resolve().parent.parent/'artifacts'/'ui'
out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    failures=[]
    App.report_callback_exception=lambda self,kind,value,tb:failures.append(value)
    app=App(config_path=str(Path(temp)/'config.json'),enable_tray=False)
    app.geometry('1060x820+40+40')
    def capture(name):
        app.update_idletasks(); app.update()
        x,y=app.winfo_rootx(),app.winfo_rooty()
        ImageGrab.grab(bbox=(x,y,x+app.winfo_width(),y+app.winfo_height())).save(out/f'{name}.png')
    def verify():
        try:
            capture('overview-light')
            app._show('settings'); app.update(); capture('settings-light')
            app._show('guide'); app.update(); capture('guide-light')
            app._show('overview'); ctk.set_appearance_mode('dark'); app.update(); capture('overview-dark')
            ctk.set_appearance_mode('light'); app.geometry('960x760+40+40'); app.update(); capture('overview-minimum')
            app._diag(dict(title='网络暂不可达',reason='服务器 SSH 端口不可连接。',solution='请检查内网连接、VPN 和 SSH 服务。'))
            app.update(); capture('diagnostic-minimum')
            assert app.log_box.winfo_height()>=80,app.log_box.winfo_height()
            app.diag_frame.pack_forget()
            app.geometry('780x580+40+40'); app.update(); capture('overview-compact')
            assert app.log_box.winfo_height()>=80
            assert app.sidebar_state.winfo_ismapped()
            assert app.sidebar_state.winfo_y()+app.sidebar_state.winfo_height() < app.winfo_height()
            cfg=app._read_config()
            assert cfg.profile_name==app.config.profile_name
            # GUI edits preserve hidden profile values and reject partial numbers.
            app.config.profile_name='测试预设'
            app.config.reconnect_delay=17
            app.entries['reconnect_delay'].delete(0,'end'); app.entries['reconnect_delay'].insert(0,'17')
            assert app._read_config().profile_name=='测试预设'
            entry=app.entries['local_port']; entry.delete(0,'end'); entry.insert(0,'bad')
            try:
                app._read_config()
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid GUI number accepted')
            assert not failures,repr(failures)
            print('UI smoke OK: seven screenshots; no callback errors or config/network/tray side effects')
        except Exception as exc:
            failures.append(exc); print('UI smoke FAILED:',repr(exc))
        finally: app._exit()
    app.after(1400,verify)
    app.mainloop()
    if failures: raise SystemExit(1)
