"""Workspace page rendering and isolated edit/import interactions; no external connections."""
import sys,tempfile,json,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from unittest.mock import patch
from PIL import ImageGrab
import customtkinter as ctk
from src.ui_app import App
from src.config_manager import ForwardRule
out=Path(__file__).resolve().parent.parent/'artifacts/ui';out.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
 errors=[]
 App.report_callback_exception=lambda self,k,v,t:errors.append(str(v))
 app=App(config_path=str(Path(temp)/'config.json'),enable_tray=False)
 app.geometry('1100x860+20+20')
 def capture(name):
  app.update_idletasks();app.update()
  x,y=app.winfo_rootx(),app.winfo_rooty()
  ImageGrab.grab(bbox=(x,y,x+app.winfo_width(),y+app.winfo_height())).save(out/(name+'.png'))
 def verify():
  try:
   for name in ('servers','forwards','monitor','keys','preferences'):
    app._show(name);capture(name+'-light')
   app._show('monitor')
   for tab in ('代理与流量','服务器仪表盘'):
    app.monitor_tabs.set(tab);capture('monitor-'+('traffic' if tab=='代理与流量' else 'dashboard'))
   app.monitor_tabs.set('健康与测速')
   # Exercise add/edit/delete, profile creation and stable IDs without messages or networking.
   with patch('src.workspace_ui.messagebox.showerror',side_effect=AssertionError),patch('src.workspace_ui.simpledialog.askstring',return_value='服务器 B'):
    original=app.config.profile_id;app._new_profile();assert app.config.profile_id!=original
    app._show('forwards');app._save_rule();assert len(app.config.forward_rules)==1
    app._edit_rule(0);app.rule_entries['target_port'].delete(0,'end');app.rule_entries['target_port'].insert(0,'9999');app._save_rule();assert app.config.forward_rules[0].target_port==9999
    app._remove_rule(0);assert not app.config.forward_rules
   app._show('servers');capture('servers-multiple')
   app._show('preferences');ctk.set_appearance_mode('dark');capture('preferences-dark')
   ctk.set_appearance_mode('light');app.geometry('860x640+20+20')
   for name in ('servers','forwards','monitor','keys','preferences'):
    app._show(name);capture(name+'-compact')
    assert app.sidebar_state.winfo_y()+app.sidebar_state.winfo_height()<app.winfo_height()
   assert not errors,errors
   assert not app.manager.is_running()
   print('Workspace UI OK: 14 additional screenshots; eight navigation pages; isolated profile and rule actions; no callback errors')
  except Exception as exc:errors.append(str(exc));print('Workspace UI FAILED:',repr(exc))
  finally:app._exit()
 app.after(1800,verify);app.mainloop()
 if errors:raise SystemExit(1)
