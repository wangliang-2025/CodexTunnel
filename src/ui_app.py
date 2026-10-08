""" desktop UI with main-thread events and truthful health indicators."""
import queue
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
import customtkinter as ctk
from PIL import Image
from src.config_manager import ConfigManager
from src.net_probes import NetworkProbes
from src.resources import asset_path
from src.system_helper import SystemHelper, TEST_TARGETS
from src.tray_manager import TrayManager
from src.tunnel_daemon import TunnelDaemon

C = {
 'root':('#FFF7FA','#201820'),'surface':('#FFFFFF','#2C222D'),
 'soft':('#FCECF3','#3B2936'),'sidebar':('#F8E4ED','#271D28'),
 'border':('#EED5E0','#503848'),'text':('#382330','#FFF1F7'),
 'muted':('#765367','#CFABBF'),'accent':('#BC3D73','#EC8BB3'),
 'hover':('#9F2D5E','#F5AAC8'),'on_accent':('#FFFFFF','#291921'),
 'green':('#19785B','#70D6AE'),'red':('#B52E4A','#FF92A9'),
 'entry':('#FFFBFD','#241C25'),
}

class App(ctk.CTk):
 def __init__(self,config_path=None,enable_tray=True):
  self.cfg_mgr = ConfigManager(config_path)
  self.config = replace(self.cfg_mgr.config)
  ctk.set_appearance_mode(self.config.theme_mode)
  ctk.set_default_color_theme('blue')
  super().__init__()
  self.title('CodexTunnel')
  # CTk applies monitor DPI scaling to logical geometry. Fit the monitor
  # rather than opening a 1025px-tall window on a 768px laptop display.
  scale=self._get_window_scaling()
  width=min(1060,max(760,int(self.winfo_screenwidth()/scale)-48))
  height=min(820,max(560,int(self.winfo_screenheight()/scale)-64))
  self.geometry(f'{width}x{height}'); self.minsize(800,560)
  self.configure(fg_color=C['root'])
  if asset_path('app_icon.ico').exists(): self.iconbitmap(str(asset_path('app_icon.ico')))
  self.events = queue.Queue(maxsize=2500)
  self.logs = deque(maxlen=1500)
  self._closing = self._testing = False
  self._test_stop = threading.Event()
  self._test_thread = self._preflight_thread = None
  self._page = 'overview'
  self.entries = {}; self.checks = {}; self.nav = {}
  self.daemon = TunnelDaemon(on_status_change=lambda *v:self._post('status',*v),on_log=lambda *v:self._post('log',*v),
                            on_diagnostic=lambda *v:self._post('diag',*v),on_health=lambda *v:self._post('health',*v))
  self.tray = TrayManager(lambda:self._post('show'),lambda:self._post('toggle'),lambda:self._post('exit'))
  self.tray_available = False
  self._build(); self._populate()
  self._set_health({'local':None,'server':None,'tunnel':False})
  self.protocol('WM_DELETE_WINDOW',self._close)
  if enable_tray:
   try:
    self.tray.start(); self.tray_available = True
   except Exception as exc: self._log('WARN',f'托盘不可用：{exc}')
  self.after(60,self._drain); self.after(500,self._tick)
  self._log('INFO','准备就绪。请先开启本地代理，并配置 SSH 公钥或 Agent 登录。')
  if self.cfg_mgr.load_warning:
   self._diag(dict(title='配置读取提示',reason=self.cfg_mgr.load_warning,solution='保存时会保留原文件备份。'))
  if self.config.auto_connect_on_start and not getattr(self,'_workspace_mode',False): self.after(800,self._start)

 def font(self,size=15,bold=False):
  return ctk.CTkFont(family='Microsoft YaHei UI',size=size,weight='bold' if bold else 'normal')
 def label(self,parent,text,size=15,bold=False,muted=False,**kw):
  widget=ctk.CTkLabel(parent,text=text,font=self.font(size,bold),text_color=C['muted' if muted else 'text'],**kw)
  if 'wraplength' in kw:
   def resize_wrap(event):
    length=max(160,int(parent.winfo_width()/widget._get_widget_scaling())-44)
    if widget.cget('wraplength')!=length: widget.configure(wraplength=length)
   parent.bind('<Configure>',resize_wrap,add='+')
  return widget
 def button(self,parent,text,command,primary=False,**kw):
  return ctk.CTkButton(parent,text=text,command=command,height=40,corner_radius=10,font=self.font(14,True),
    fg_color=C['accent'] if primary else C['soft'],text_color=C['on_accent'] if primary else C['accent'],
    hover_color=C['hover'] if primary else C['border'],**kw)
 def card(self,parent):
  return ctk.CTkFrame(parent,fg_color=C['surface'],corner_radius=16,border_width=1,border_color=C['border'])
 def heading(self,parent,title,subtitle):
  self.label(parent,title,26,True).pack(anchor='w')
  self.label(parent,subtitle,14,muted=True).pack(anchor='w',pady=(4,18))

 def _build(self):
  self.grid_columnconfigure(1,weight=1); self.grid_rowconfigure(0,weight=1)
  sidebar = ctk.CTkFrame(self,width=205,corner_radius=0,fg_color=C['sidebar'])
  sidebar.grid(row=0,column=0,sticky='nsew'); sidebar.grid_propagate(False)
  path = asset_path('assets/tunnel-icon.png')
  if path.exists():
   with Image.open(path) as image: self.mascot = ctk.CTkImage(light_image=image.copy(),dark_image=image.copy(),size=(52,52))
   ctk.CTkLabel(sidebar,text='',image=self.mascot).pack(pady=(16,6))
  self.label(sidebar,'CodexTunnel',22,True).pack()
  self.label(sidebar,'让连接轻松一点',13,muted=True).pack(pady=(2,10))
  for key,text in [('overview','连接概览'),('servers','服务器列表'),('forwards','转发规则'),('monitor','诊断与监控'),('keys','密钥工具'),('settings','连接参数'),('preferences','全局偏好'),('guide','使用与安全')]:
   btn = self.button(sidebar,text,lambda k=key:self._show(k)); btn.configure(height=32); btn.pack(fill='x',padx=16,pady=3); self.nav[key]=btn
  ctk.CTkFrame(sidebar,fg_color='transparent',height=1).pack(fill='both',expand=True)
  self.sidebar_state = self.label(sidebar,'●  待连接',14,True); self.sidebar_state.pack(pady=(12,6))
  self.button(sidebar,'切换明暗',self._theme).pack(fill='x',padx=16,pady=(4,12))
  self.label(sidebar,'版本  3.0.2',12,muted=True).pack(pady=(0,22))
  container = ctk.CTkFrame(self,fg_color='transparent'); container.grid(row=0,column=1,sticky='nsew',padx=26,pady=24)
  container.grid_columnconfigure(0,weight=1); container.grid_rowconfigure(0,weight=1)
  self.pages = {}
  for name in ('overview','settings','guide','servers','forwards','monitor','keys','preferences'):
   page = ctk.CTkFrame(container,fg_color='transparent'); page.grid(row=0,column=0,sticky='nsew'); self.pages[name]=page
  self._overview(); self._settings(); self._guide(); self._show('overview')

 def _overview(self):
  page=self.pages['overview']; self.heading(page,'连接概览','把本机代理，稳稳送到远程服务器。')
  outer=page
  page=ctk.CTkScrollableFrame(outer,fg_color='transparent',scrollbar_button_color=C['border'])
  page.pack(fill='both',expand=True,pady=(0,10))
  hero=self.card(page); self.hero_card=hero; hero.pack(fill='x',pady=(0,14))
  top=ctk.CTkFrame(hero,fg_color='transparent'); top.pack(fill='x',padx=22,pady=(18,6))
  self.status_title=self.label(top,'准备连接',22,True); self.status_title.pack(side='left')
  self.stats=self.label(top,'在线 00:00:00  ·  重试 0 次',12,muted=True); self.stats.pack(side='right')
  self.status_detail=self.label(hero,'本地代理与 SSH 公钥准备好后，即可启动。',13,muted=True,anchor='w',justify='left',wraplength=650)
  self.status_detail.pack(fill='x',padx=22,pady=(0,14))
  self.toggle_btn=self.button(hero,'启动代理隧道',self._toggle,primary=True); self.toggle_btn.configure(height=48,font=self.font(16,True))
  self.toggle_btn.pack(fill='x',padx=22,pady=(0,18))
  health=ctk.CTkFrame(page,fg_color='transparent'); health.pack(fill='x',pady=(0,14)); health.grid_columnconfigure((0,1,2),weight=1)
  self.health_labels={}
  for i,(key,title) in enumerate([('local','本地代理端口'),('server','服务器端口'),('tunnel','SSH 转发确认'),('listener','远端入口探针'),('business','代理业务探针')]):
   item=self.card(health); item.grid(row=i//3,column=i%3,sticky='nsew',padx=(0,10) if i%3<2 else 0,pady=(0,8))
   self.label(item,title,13,muted=True).pack(anchor='w',padx=14,pady=(12,2))
   label=self.label(item,'●  未检测',15,True); label.pack(anchor='w',padx=14,pady=(0,12)); self.health_labels[key]=label
  route=self.card(page); route.pack(fill='x',pady=(0,14))
  self.label(route,'当前连接路径',14,True).pack(anchor='w',padx=18,pady=(12,3))
  self.route_label=self.label(route,'',14,muted=True,anchor='w',justify='left',wraplength=650); self.route_label.pack(fill='x',padx=18,pady=(0,12))
  actions=ctk.CTkFrame(page,fg_color='transparent'); actions.pack(fill='x',pady=(0,10)); actions.grid_columnconfigure((0,1,2),weight=1)
  self.preflight_btn=self.button(actions,'网络体检',self._preflight); self.preflight_btn.grid(row=0,column=0,sticky='ew',padx=(0,8))
  self.button(actions,'复制代理命令',self._copy_proxy).grid(row=0,column=1,sticky='ew',padx=(0,8))
  self.button(actions,'复制取消代理',lambda:self._copy('unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY no_proxy NO_PROXY','取消代理命令')).grid(row=0,column=2,sticky='ew')
  test=ctk.CTkFrame(page,fg_color='transparent'); test.pack(fill='x',pady=(0,10))
  self.target_var=tk.StringVar(value='Cloudflare')
  self.target_menu=ctk.CTkOptionMenu(test,variable=self.target_var,values=list(TEST_TARGETS),width=130,height=34,font=self.font(13),
      fg_color=C['soft'],button_color=C['accent'],button_hover_color=C['hover'],text_color=C['text'],command=lambda _:self._reset_test())
  self.target_menu.pack(side='left')
  self.test_btn=self.button(test,'验证外网',self._test,width=102); self.test_btn.configure(height=34); self.test_btn.pack(side='left',padx=8)
  self.e2e_label=self.label(test,'尚未验证',12,muted=True,anchor='w',wraplength=410); self.e2e_label.pack(side='left',fill='x',expand=True)
  self.diag_frame=self.card(page)
  self.diag_label=self.label(self.diag_frame,'',13,muted=True,anchor='w',justify='left',wraplength=650); self.diag_label.pack(fill='x',padx=16,pady=12)
  self.log_card=self.card(outer); self.log_card.pack(fill='x')
  bar=ctk.CTkFrame(self.log_card,fg_color='transparent'); bar.pack(fill='x',padx=16,pady=(10,6))
  self.label(bar,'运行日志',14,True).pack(side='left')
  self.button(bar,'清空',self._clear,width=60).pack(side='right',padx=(6,0)); self.button(bar,'导出',self._export,width=60).pack(side='right')
  self.log_box=ctk.CTkTextbox(self.log_card,font=self.font(14),fg_color=C['entry'],text_color=C['text'],corner_radius=10,height=140)
  self.log_box.pack(fill='both',expand=True,padx=12,pady=(0,12)); self.log_box.configure(state='disabled')

 def section(self,scroll,title,subtitle=''):
  card=self.card(scroll); card.pack(fill='x',pady=(0,14))
  self.label(card,title,17,True).pack(anchor='w',padx=20,pady=(16,0))
  if subtitle: self.label(card,subtitle,13,muted=True,wraplength=630,justify='left').pack(anchor='w',padx=20,pady=(3,0))
  body=ctk.CTkFrame(card,fg_color='transparent'); body.pack(fill='x',padx=20,pady=(0,18)); return body
 def field(self,parent,key,title):
  self.label(parent,title,14,True).pack(anchor='w',pady=(10,5))
  entry=ctk.CTkEntry(parent,height=40,font=self.font(15),fg_color=C['entry'],border_color=C['border'],text_color=C['text'],corner_radius=9)
  entry.pack(fill='x'); self.entries[key]=entry
 def pair(self,parent,fields):
  row=ctk.CTkFrame(parent,fg_color='transparent'); row.pack(fill='x'); row.grid_columnconfigure((0,1),weight=1)
  for i,(key,title) in enumerate(fields):
   col=ctk.CTkFrame(row,fg_color='transparent'); col.grid(row=0,column=i,sticky='ew',padx=(0,12) if i==0 else 0); self.field(col,key,title)
 def check(self,parent,key,text):
  var=tk.BooleanVar(); self.checks[key]=var
  ctk.CTkCheckBox(parent,text=text,variable=var,font=self.font(14),fg_color=C['accent'],hover_color=C['hover'],border_color=C['border'],
      text_color=C['text'],checkmark_color=C['on_accent']).pack(anchor='w',pady=8)
 def menu(self,parent,var,values,command=None):
  return ctk.CTkOptionMenu(parent,variable=var,values=values,command=command,font=self.font(14),height=40,
      fg_color=C['soft'],button_color=C['accent'],text_color=C['text'],button_hover_color=C['hover'])

 def _settings(self):
  page=self.pages['settings']; self.heading(page,'连接与偏好','预设、认证与运行策略，集中管理。')
  self.label(page,'仅当前运行中的服务器暂停编辑；其他服务器仍可独立操作。',13,muted=True).pack(anchor='w',pady=(0,10))
  scroll=ctk.CTkScrollableFrame(page,fg_color='transparent',scrollbar_button_color=C['border']); scroll.pack(fill='both',expand=True)
  body=self.section(scroll,'配置预设'); row=ctk.CTkFrame(body,fg_color='transparent'); row.pack(fill='x',pady=(12,0))
  self.profile_var=tk.StringVar(); self.profile_menu=self.menu(row,self.profile_var,list(self.cfg_mgr.profiles),self._switch_profile)
  self.profile_menu.pack(side='left',fill='x',expand=True)
  self.button(row,'另存预设',self._clone,width=95).pack(side='left',padx=8); self.button(row,'删除',self._delete,width=62).pack(side='left')
  body=self.section(scroll,'远程服务器','使用可直接解析的主机名或 IP；远程转发仅请求监听回环地址。')
  self.field(body,'server_host','服务器地址'); self.pair(body,[('username','登录用户名'),('server_port','SSH 端口')])
  body=self.section(scroll,'SSH 身份与信任','公钥 / Agent 非交互认证；不保存密码或私钥内容。')
  self.auth_var=tk.StringVar(); self.auth_menu=self.menu(body,self.auth_var,['默认密钥 / Agent','指定私钥'],lambda _:self._auth_changed())
  self.auth_menu.pack(fill='x',pady=(12,0)); self.field(body,'key_file_path','私钥文件')
  self.button(body,'浏览私钥文件',self._browse,width=135).pack(anchor='w',pady=(8,0))
  self.label(body,'加密私钥须先通过 ssh-add 解锁到本机 Agent。',12,muted=True).pack(anchor='w',pady=(5,0))
  self.label(body,'主机指纹策略',14,True).pack(anchor='w',pady=(14,5))
  self.host_key_var=tk.StringVar(); self.menu(body,self.host_key_var,['首次信任，变化拒绝','严格校验已知主机']).pack(fill='x')
  self.label(body,'首次信任会记录新指纹；严格模式须先在终端核验。指纹变化不会自动删除记录。',12,muted=True,wraplength=600,justify='left').pack(anchor='w',pady=(5,0))
  body=self.section(scroll,'端口与重连')
  for pair in [[('local_port','本地代理端口'),('remote_port','远程代理端口')],[('reconnect_delay','重连基础间隔 / 秒'),('connect_timeout','连接超时 / 秒')],[('keepalive_interval','心跳间隔 / 秒'),('keepalive_count_max','心跳容错次数')]]: self.pair(body,pair)
  self.check(body,'auto_reconnect','网络故障自动重连（逐步退避，最多等待 120 秒）')
  body=self.section(scroll,'此服务器启动策略')
  self.check(body,'auto_connect_on_start','启动软件后自动连接此服务器')
  self.label(body,'主题、登录自启、通知与隐私选项在全局偏好中统一管理。',12,muted=True,wraplength=600).pack(anchor='w',pady=(4,0))
  self.button(scroll,'保存当前配置',self._save,primary=True).pack(fill='x',pady=(2,16))

 def _guide(self):
  page=self.pages['guide']; self.heading(page,'使用与安全','了解连接路径，也了解信任边界。')
  scroll=ctk.CTkScrollableFrame(page,fg_color='transparent'); scroll.pack(fill='both',expand=True)
  guides=[
   ('三步开始','1. 开启本地代理，确认混合端口。\n2. 配置服务器与用户名，准备 SSH 公钥或 Agent 登录。\n3. 启动隧道，复制代理命令到远程终端，再验证外网。'),
   ('绿灯分别代表什么','本地与服务器状态只表示 TCP 端口可达；SSH 已确认表示收到远程转发成功回应。外网验证单独执行远端 curl 请求，不把 CONNECT 200 当作目标请求成功。'),
   ('首次登录与加密私钥','默认策略自动记录新指纹、拒绝后续变化。严格模式须先在终端与管理员核验指纹。加密私钥通过 ssh-add 解锁到本机 Agent；程序不输入或保存密码。'),
   ('代理命令的范围','export 影响当前 shell 及其后续子进程；已有程序、其他会话和 systemd 服务需单独配置。SOCKS 使用 socks5h，让代理端解析域名；no_proxy 保留本机回环直连。'),
   ('连接故障与端口占用','检查 VPN、SSH 服务与本地代理。TUN 绕过规则按实际网段设置。转发失败也可能来自服务器策略。使用只读 ss 命令核验端口归属；程序不会杀死其他服务或删除 known_hosts。'),
   ('安全边界','转发仅请求监听 127.0.0.1；服务器的其他本机用户仍可能访问。管理员可通过 GatewayPorts 影响监听范围，必要时用 ss 核验。程序不转发 Agent/X11，并忽略 SSH 配置文件；配置别名和 ProxyJump 需另行使用终端。'),
   ('隐私与配置','配置位于当前用户 AppData/CodexTunnel/config.json，保存主机、用户名和私钥路径，不保存密钥内容。日志默认脱敏并限制条数；损坏配置保存前备份。诊断留在面板，重试不反复弹窗。'),
  ]
  for title,text in guides:
   body=self.section(scroll,title); self.label(body,text,14,wraplength=630,justify='left',anchor='w').pack(fill='x',pady=(12,0))
  row=ctk.CTkFrame(scroll,fg_color='transparent'); row.pack(fill='x',pady=8)
  self.button(row,'复制首次登录命令',self._copy_login).pack(side='left',padx=(0,10)); self.button(row,'复制端口检查命令',self._copy_inspect).pack(side='left')

 def _post(self,kind,*values):
  try: self.events.put_nowait((kind,values))
  except queue.Full:
   if kind!='log':
    try: self.events.get_nowait(); self.events.put_nowait((kind,values))
    except (queue.Empty,queue.Full): pass

 def _drain(self):
  if self._closing: return
  for _ in range(150):
   try: kind,values=self.events.get_nowait()
   except queue.Empty: break
   if kind=='status': self._status(*values)
   elif kind=='health': self._set_health(*values)
   elif kind=='log': self._log(*values)
   elif kind=='diag': self._diag(*values)
   elif kind=='show': self._restore()
   elif kind=='toggle': self._toggle()
   elif kind=='exit': self._exit()
   elif kind=='preflight':
    health,results=values
    if not self.daemon.is_running(): self._set_health(health)
    self._log('INFO',results); self.preflight_btn.configure(state='normal',text='网络体检')
   elif kind=='e2e':
    ok,msg=values; self._testing=False
    if self.daemon.current_state=='CONNECTED': self.e2e_label.configure(text=msg,text_color=C['green' if ok else 'red'])
    else: self._reset_test()
    self.test_btn.configure(text='验证外网',state='normal'); self.target_menu.configure(state='normal')
    self._log('SUCCESS' if ok else 'WARN',msg)
  self.after(60,self._drain)

 def _show(self,key):
  if key=='settings' and self.daemon.is_running():
   self._log('INFO','请先停止隧道，再编辑配置。'); return
  self.pages[key].tkraise(); self._page=key
  for name,btn in self.nav.items():
   btn.configure(fg_color=C['accent'] if name==key else 'transparent',text_color=C['on_accent'] if name==key else C['muted'])

 def _populate(self):
  for key,entry in self.entries.items():
   entry.configure(state='normal'); entry.delete(0,'end'); entry.insert(0,str(getattr(self.config,key)))
  for key,var in self.checks.items(): var.set(getattr(self.config,key))
  # Startup is an OS-wide preference for this user, not per-server state.
  self.checks['run_at_startup'].set(SystemHelper.is_run_at_startup_enabled())
  self.auth_var.set('指定私钥' if self.config.auth_mode=='key_file' else '默认密钥 / Agent')
  self.host_key_var.set('严格校验已知主机' if self.config.host_key_policy=='yes' else '首次信任，变化拒绝')
  self.profile_menu.configure(values=list(self.cfg_mgr.profiles)); self.profile_var.set(self.config.profile_name)
  self._auth_changed(); self._route()
 def _auth_changed(self):
  self.entries['key_file_path'].configure(state='normal' if self.auth_var.get()=='指定私钥' else 'disabled')
 def _browse(self):
  path=filedialog.askopenfilename(parent=self,title='选择私钥文件',initialdir=str(Path.home()/'.ssh'))
  if path:
   self.auth_var.set('指定私钥'); self._auth_changed()
   entry=self.entries['key_file_path']; entry.delete(0,'end'); entry.insert(0,path)
 def _read_config(self):
  values={}
  for key,entry in self.entries.items():
   value=entry.get().strip()
   if type(getattr(self.config,key)) is int:
    try: value=int(value)
    except ValueError: raise ValueError('端口、超时、心跳和重连间隔须填写整数')
   values[key]=value
  values.update({key:var.get() for key,var in self.checks.items()})
  values['auth_mode']='key_file' if self.auth_var.get()=='指定私钥' else 'system'
  values['host_key_policy']='yes' if self.host_key_var.get()=='严格校验已知主机' else 'accept-new'
  cfg=replace(self.config,**values); errors=cfg.validate()
  if errors: raise ValueError('\n'.join(errors.values()))
  return cfg
 def _idle(self):
  if self.daemon.is_running() or self._testing or (self._preflight_thread and self._preflight_thread.is_alive()):
   messagebox.showinfo('请稍候','请先停止隧道，并等待检测完成。',parent=self); return False
  return True
 def _save(self):
  if not self._idle(): return
  try: cfg=self._read_config()
  except ValueError as exc: messagebox.showerror('配置需要调整',str(exc),parent=self); return
  old=SystemHelper.is_run_at_startup_enabled()
  if cfg.run_at_startup!=old and not SystemHelper.set_run_at_startup(cfg.run_at_startup):
   messagebox.showerror('保存未完成','无法设置当前用户的 Windows 启动项。',parent=self); return
  if not self.cfg_mgr.save(cfg):
   if cfg.run_at_startup!=old: SystemHelper.set_run_at_startup(old)
   messagebox.showerror('保存失败',self.cfg_mgr.last_error,parent=self); return
  self.config=replace(cfg); self._reset_test(); self._route()
  self._log('INFO','当前配置已保存。'); messagebox.showinfo('已保存','连接参数和偏好已保存。',parent=self)
 def _switch_profile(self,name):
  if not self._idle(): self.profile_var.set(self.config.profile_name); return
  cfg=self.cfg_mgr.switch_profile(name)
  if cfg: self.config=cfg; self._populate(); self._reset_test()
  else:
   self.profile_var.set(self.config.profile_name); messagebox.showerror('切换失败',self.cfg_mgr.last_error,parent=self)
 def _clone(self):
  if not self._idle(): return
  try: cfg=self._read_config()
  except ValueError as exc: messagebox.showerror('配置需要调整',str(exc),parent=self); return
  name=simpledialog.askstring('另存预设','为这个连接填写名称：',parent=self)
  if name is None: return
  name=name.strip()
  if name in self.cfg_mgr.profiles: messagebox.showerror('名称已存在','请选择不同名称。',parent=self); return
  cfg=replace(cfg,profile_name=name)
  if self.cfg_mgr.save(cfg): self.config=cfg; self._populate(); self._reset_test()
  else: messagebox.showerror('保存失败',self.cfg_mgr.last_error,parent=self)
 def _delete(self):
  if not self._idle(): return
  if not messagebox.askyesno('删除预设',f'删除“{self.config.profile_name}”？至少保留一个预设。',parent=self): return
  if self.cfg_mgr.delete_profile(self.config.profile_name): self.config=replace(self.cfg_mgr.config); self._populate(); self._reset_test()
  else: messagebox.showerror('无法删除',self.cfg_mgr.last_error,parent=self)
 def _command_config(self): return self.daemon._current_config if self.daemon.is_running() else self.config
 def _route(self):
  cfg=self._command_config() or self.config
  self.route_label.configure(text=f'Linux  127.0.0.1:{cfg.remote_port}   →   SSH 加密隧道   →   本机  127.0.0.1:{cfg.local_port}\n服务器  {cfg.username}@{cfg.server_host}:{cfg.server_port}   ·   预设  {cfg.profile_name}')
 def _toggle(self):
  if self.daemon.is_running(): self.daemon.stop(); self._test_stop.set()
  else: self._start()
 def _start(self):
  if self._testing or (self._preflight_thread and self._preflight_thread.is_alive()): self._log('INFO','请等待当前检测完成。'); return
  try: cfg=self._read_config()
  except ValueError as exc:
   self._show('settings'); messagebox.showerror('配置需要调整',str(exc),parent=self); return
  self.config=cfg; self._reset_test(); self.diag_frame.pack_forget(); self._show('overview')
  if not self.daemon.start(cfg): self._log('WARN','启动未接受，请等待停止完成。')
  self._route()
 def _status(self,state,message):
  self._last_status=(state,message)
  names={'STOPPED':'准备连接','PRECHECKING':'正在检查环境','CONNECTING':'正在建立隧道','CONNECTED':'隧道已建立','RECONNECTING':'等待恢复连接','STOPPING':'正在安全停止','ERROR':'连接需要处理'}
  color='green' if state=='CONNECTED' else 'red' if state=='ERROR' else 'accent'
  self.status_title.configure(text=names.get(state,state),text_color=C[color]); self.sidebar_state.configure(text='●  '+names.get(state,state),text_color=C[color])
  self.status_detail.configure(text=message)
  busy=state in ('PRECHECKING','CONNECTING','CONNECTED','RECONNECTING','STOPPING')
  self.toggle_btn.configure(text='停止隧道' if state=='CONNECTED' else '取消连接' if busy else '启动代理隧道',state='disabled' if state=='STOPPING' else 'normal')
  self.nav['settings'].configure(state='disabled' if busy else 'normal')
  self.preflight_btn.configure(state='disabled' if busy else 'normal')
  if state!='CONNECTED': self._test_stop.set(); self._reset_test()
  if busy and self._page=='settings': self._show('overview')
  self.tray.update_status(state,message); self._route()
 def _set_health(self,health):
  for key,label in self.health_labels.items():
   value=health.get(key)
   text='已确认' if key=='tunnel' and value is True else '可连接' if value is True else '未建立' if key=='tunnel' and value is False else '不可连接' if value is False else '未检测'
   neutral=key=='tunnel' and value is False and self.daemon.current_state=='STOPPED'
   label.configure(text='●  '+text,text_color=C['green' if value is True else 'red' if value is False and not neutral else 'muted'])
 def _reset_test(self): self.e2e_label.configure(text='尚未验证',text_color=C['muted'])
 def _diag(self,diag):
  text=f"{diag['title']}  ·  {diag.get('reason','')}\n{diag.get('solution','')}"
  if self.config.mask_logs: text=SystemHelper.mask_sensitive_text(text,self.config)
  self.diag_label.configure(text=text)
  if not self.diag_frame.winfo_manager(): self.diag_frame.pack(fill='x',pady=(0,10),before=self.hero_card)
 def _log(self,level,text):
  if self.config.mask_logs: text=SystemHelper.mask_sensitive_text(text,self.config)
  self.logs.append(text); self.log_box.configure(state='normal'); self.log_box.insert('end',text+'\n')
  lines=int(self.log_box.index('end-1c').split('.')[0])
  if lines>1501: self.log_box.delete('1.0',f'{lines-1500}.0')
  self.log_box.see('end'); self.log_box.configure(state='disabled')
 def _clear(self):
  self.logs.clear(); self.log_box.configure(state='normal'); self.log_box.delete('1.0','end'); self.log_box.configure(state='disabled')
 def _export(self):
  path=filedialog.asksaveasfilename(parent=self,title='导出脱敏日志',defaultextension='.txt',initialfile='CodexTunnel-诊断日志.txt',filetypes=[('文本文件','*.txt')])
  if path:
   text=SystemHelper.mask_sensitive_text('\n'.join(self.logs),self.config)
   for cfg in self.cfg_mgr.profiles.values(): text=SystemHelper.mask_sensitive_text(text,cfg)
   try: Path(path).write_text(text+'\n',encoding='utf-8'); self._log('INFO','脱敏日志已导出。')
   except OSError as exc: messagebox.showerror('导出失败',str(exc),parent=self)
 def _preflight(self):
  if self.daemon.is_running() or self._testing or (self._preflight_thread and self._preflight_thread.is_alive()): return
  try: cfg=self._read_config()
  except ValueError as exc: messagebox.showerror('配置需要调整',str(exc),parent=self); return
  self.preflight_btn.configure(state='disabled',text='检测中…')
  def work():
   local,lmsg=NetworkProbes.probe_local_proxy(port=cfg.local_port); server,smsg=NetworkProbes.probe_remote_server(cfg.server_host,cfg.server_port)
   self._post('preflight',{'local':local,'server':server,'tunnel':None},lmsg+'\n'+smsg)
  self._preflight_thread=threading.Thread(target=work,daemon=True); self._preflight_thread.start()
 def _test(self):
  if self._testing: self._test_stop.set(); self.test_btn.configure(text='取消中…',state='disabled'); return
  if self.daemon.current_state!='CONNECTED' or not self.daemon.is_running(): self._log('INFO','请先建立隧道，再验证远端外网。'); return
  self._testing=True; self._test_stop.clear(); self.test_btn.configure(text='取消验证'); self.target_menu.configure(state='disabled')
  self.e2e_label.configure(text='远程请求进行中…',text_color=C['muted'])
  cfg=replace(self.daemon._current_config); target=self.target_var.get()
  def work():
   try: ok,msg=self.daemon.test_e2e(cfg,stop_event=self._test_stop,target=target)
   except Exception as exc: ok,msg=False,f'验证异常：{exc}'
   self._post('e2e',ok,msg)
  self._test_thread=threading.Thread(target=work,daemon=True); self._test_thread.start()
 def _copy(self,text,label): self.clipboard_clear(); self.clipboard_append(text); self._log('INFO',label+'已复制。')
 def _copy_proxy(self):
  r=self._command_config().remote_port
  self._copy(f'export http_proxy=http://127.0.0.1:{r}\nexport https_proxy=http://127.0.0.1:{r}\nexport all_proxy=socks5h://127.0.0.1:{r}\nexport HTTP_PROXY="$http_proxy" HTTPS_PROXY="$https_proxy" ALL_PROXY="$all_proxy"\nexport no_proxy=localhost,127.0.0.1,::1\nexport NO_PROXY="$no_proxy"','代理命令')
 def _copy_login(self):
  try: cfg=self._read_config()
  except ValueError as exc: messagebox.showerror('配置需要调整',str(exc),parent=self); return
  args=['ssh','-F','none','-p',str(cfg.server_port)]
  if cfg.auth_mode=='key_file': args.extend(['-i',cfg.key_file_path])
  args.extend(['--',f'{cfg.username}@{cfg.server_host}'])
  self._copy(' '.join("'"+arg.replace("'","''")+"'" if i else arg for i,arg in enumerate(args)),'首次登录命令')
 def _copy_inspect(self): self._copy(f"ss -ltnp 'sport = :{self._command_config().remote_port}'",'远程端口检查命令')
 def _theme(self):
  mode='dark' if ctk.get_appearance_mode()=='Light' else 'light'; ctk.set_appearance_mode(mode); self.config=replace(self.config,theme_mode=mode)
  saved=self.cfg_mgr.profiles.get(self.config.profile_name)
  if saved and not self.cfg_mgr.save(replace(saved,theme_mode=mode)): self._log('WARN','主题已切换，但未能写入配置。')
 def _tick(self):
  if self._closing: return
  seconds=int(time.monotonic()-self.daemon.connect_start_time) if self.daemon.connect_start_time else 0
  self.stats.configure(text=f'在线 {seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}  ·  重试 {self.daemon.retry_count} 次')
  if self.daemon.is_running(): self._set_health(self.daemon.health)
  last=getattr(self,'_last_status',('STOPPED',''))
  if last[0]!=self.daemon.current_state: self._status(self.daemon.current_state,self.daemon.status_message)
  if not self.daemon.is_running() and self.daemon.current_state in ('STOPPED','ERROR'): self.toggle_btn.configure(state='normal')
  self.after(500,self._tick)
 def _close(self):
  if self.config.minimize_to_tray and self.tray_available: self.withdraw()
  else: self._exit()
 def _restore(self): self.deiconify(); self.lift(); self.focus_force()
 def _exit(self):
  if self._closing: return
  self._closing=True; self._test_stop.set(); self.daemon.stop(); self.toggle_btn.configure(state='disabled',text='正在退出…'); self._finish_exit()
 def _finish_exit(self):
  if self.daemon.is_running() or (self._test_thread and self._test_thread.is_alive()) or (self._preflight_thread and self._preflight_thread.is_alive()):
   self.after(100,self._finish_exit); return
  self.tray.stop(); self.destroy()


from src.workspace_ui import workspace_app
App = workspace_app(App, C)

