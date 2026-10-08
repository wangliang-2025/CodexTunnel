"""Additional workspace pages preserve the existing accessible components."""
import copy
import queue
import statistics
import threading
import time
import uuid
from collections import deque
from dataclasses import replace,asdict
from pathlib import Path
import json
import tkinter as tk
from tkinter import filedialog,messagebox,simpledialog
import customtkinter as ctk
from src.config_manager import ForwardRule,AppPreferences
from src.tunnel_manager import TunnelManager
from src.services import MetricsService,ClashClient
from src.desktop_services import NetworkWatcher,NotificationGate,ToastService,KeyService
from src.system_helper import SystemHelper


def workspace_app(Base,C):
 class WorkspaceApp(Base):
  def __init__(self,config_path=None,enable_tray=True):
   self._workspace_mode=True
   self.workspace_events=queue.Queue(maxsize=2000)
   self._jobs={}; self._job_id=0; self._per_logs={}; self._dashboard_samples={}; self._traffic_samples=deque(maxlen=120)
   self._startup_cancelled=set(); self._next_dashboard=0; self._next_traffic=0; self._last_server_signature=None; self._key_result=None
   super().__init__(config_path,enable_tray)
   self.preferences=self.cfg_mgr.preferences
   self.runtime_enabled=config_path is None
   self.manager=TunnelManager(self.preferences,self._manager_event)
   self.daemon=self.manager.get(self.config)
   self.notification_gate=NotificationGate(); self.watcher=None
   self._build_workspace(); self._populate(); self._populate_preferences()
   ctk.set_appearance_mode(self.preferences.theme_mode)
   self._update_watcher()
   self.after(250,self._workspace_tick)
   if self.runtime_enabled:
    self.after(self.preferences.startup_delay*1000,self._auto_start)
    if self.preferences.start_in_tray and self.tray_available: self.after(600,self.withdraw)
  def _manager_event(self,ident,kind,*values):
   self._workspace_post(('manager',ident,kind,values))
  def _workspace_post(self,event):
   try:self.workspace_events.put_nowait(event)
   except queue.Full:
    # Drop verbose logs first; polling remains authoritative for states.
    if event[0]!='manager' or event[2]!='log':
     try:self.workspace_events.get_nowait();self.workspace_events.put_nowait(event)
     except (queue.Empty,queue.Full):pass
  def _scroll(self,key,title,subtitle):
   page=self.pages[key]; self.heading(page,title,subtitle)
   scroll=ctk.CTkScrollableFrame(page,fg_color='transparent',scrollbar_button_color=C['border']);scroll.pack(fill='both',expand=True)
   return scroll
  def _text_box(self,parent,height=100):
   box=ctk.CTkTextbox(parent,height=height,font=self.font(14),fg_color=C['entry'],text_color=C['text']);box.pack(fill='x',pady=(10,0));box.configure(state='disabled');return box
  def _set_box(self,box,text):
   box.configure(state='normal');box.delete('1.0','end');box.insert('end',str(text));box.configure(state='disabled')
  def _entry_row(self,parent,specs,store):
   row=ctk.CTkFrame(parent,fg_color='transparent');row.pack(fill='x',pady=(10,0));row.grid_columnconfigure(tuple(range(len(specs))),weight=1)
   for i,(key,title,default) in enumerate(specs):
    col=ctk.CTkFrame(row,fg_color='transparent');col.grid(row=0,column=i,sticky='ew',padx=(0,10) if i<len(specs)-1 else 0)
    self.label(col,title,13,True).pack(anchor='w',pady=(0,5))
    entry=ctk.CTkEntry(col,height=38,font=self.font(14),fg_color=C['entry'],border_color=C['border'],text_color=C['text']);entry.pack(fill='x');entry.insert(0,str(default));store[key]=entry
  def _action_row(self,parent,actions):
   row=ctk.CTkFrame(parent,fg_color='transparent');row.pack(fill='x',pady=(12,0));row.grid_columnconfigure(tuple(range(len(actions))),weight=1)
   for i,(text,command) in enumerate(actions): self.button(row,text,command).grid(row=0,column=i,sticky='ew',padx=(0,8) if i<len(actions)-1 else 0)
  def _build_workspace(self):
   selector=ctk.CTkFrame(self.pages['overview'],fg_color='transparent')
   selector.pack(fill='x',before=self.hero_card.master._parent_frame,pady=(0,10))
   self.label(selector,'当前服务器',13,True).pack(side='left',padx=(0,10))
   self.connection_var=tk.StringVar(value=self.config.profile_name)
   self.connection_menu=self.menu(selector,self.connection_var,list(self.cfg_mgr.profiles),self._switch_profile);self.connection_menu.pack(side='left',fill='x',expand=True)
   self.button(selector,'服务器列表',lambda:self._show('servers'),width=110).pack(side='left',padx=(8,0))
   server_scroll=self._scroll('servers','服务器列表','最多并发 5 个连接；每个服务器独立停止、重连与记录日志。')
   body=self.section(server_scroll,'连接操作','切换只改变当前查看项；不会停止其他服务器。')
   self._action_row(body,[('新建配置',self._new_profile),('启动全部',self._start_all),('停止全部',self._stop_all)])
   self._action_row(body,[('导出所有配置',lambda:self._share_export(True)),('导入分享文件',self._share_import)])
   self.server_list=ctk.CTkFrame(server_scroll,fg_color='transparent');self.server_list.pack(fill='both',expand=True)
   self._build_forwards();self._build_monitor();self._build_keys();self._build_preferences();self._refresh_servers(force=True)
  def _build_forwards(self):
   scroll=self._scroll('forwards','转发规则','默认仅监听 127.0.0.1；同一服务器的规则共用一个 SSH 会话。')
   body=self.section(scroll,'默认代理反向转发','远端回环代理端口 → 本机代理端口。端口在连接参数中设置。')
   self.primary_var=tk.BooleanVar(value=self.config.include_proxy_forward)
   ctk.CTkCheckBox(body,text='启用默认代理转发',variable=self.primary_var,command=self._primary_changed,font=self.font(14),fg_color=C['accent'],text_color=C['text']).pack(anchor='w',pady=(12,0))
   self.rule_list=ctk.CTkFrame(scroll,fg_color='transparent',height=1);self.rule_list.pack(fill='x')
   body=self.section(scroll,'添加 / 编辑规则','L：本机访问远端服务；R：远端访问本机服务。至少启用一条规则。')
   self.rule_entries={};self._rule_edit_index=None
   self._entry_row(body,[('name','用途名称','Jupyter'),('listen_port','监听端口',8888)],self.rule_entries)
   self._entry_row(body,[('target_host','目标主机','127.0.0.1'),('target_port','目标端口',8888)],self.rule_entries)
   self.direction_var=tk.StringVar(value='L · 本地转发')
   self.menu(body,self.direction_var,['L · 本地转发','R · 反向转发']).pack(fill='x',pady=(12,0))
   self._action_row(body,[('保存规则',self._save_rule),('清空编辑',self._reset_rule)])
   self.rule_notice=self.label(body,'建立任一必需规则失败时整组连接失败；业务协议另行检查。',12,muted=True,wraplength=630,justify='left');self.rule_notice.pack(fill='x',pady=(10,0))
   body=self.section(scroll,'配置分享','包含主机、用户名与转发规则；不包含私钥、口令、自启或自动连接设置。')
   self._action_row(body,[('导出当前配置',lambda:self._share_export(False)),('预览并导入',self._share_import)])
  def _build_monitor(self):
   page=self.pages['monitor'];self.heading(page,'诊断与监控','探测、测速与图表明确标注链路和统计来源。')
   toolbar=ctk.CTkFrame(page,fg_color='transparent');toolbar.pack(fill='x',pady=(0,8))
   self.tool_activity=self.label(toolbar,'后台任务：0',12,muted=True);self.tool_activity.pack(side='left')
   self.button(toolbar,'取消工具任务',self._cancel_tools,width=120).pack(side='right')
   self.monitor_tabs=ctk.CTkTabview(page,fg_color=C['soft'],segmented_button_fg_color=C['sidebar'],segmented_button_selected_color=C['accent'],segmented_button_selected_hover_color=C['hover'],segmented_button_unselected_color=C['soft'],text_color=C['text']);self.monitor_tabs._segmented_button.configure(font=self.font(14,True));self.monitor_tabs.pack(fill='both',expand=True)
   tab_scrolls={}
   for title in ('健康与测速','代理与流量','服务器仪表盘'):
    tab=self.monitor_tabs.add(title)
    frame=ctk.CTkScrollableFrame(tab,fg_color='transparent',scrollbar_button_color=C['border']);frame.pack(fill='both',expand=True);tab_scrolls[title]=frame
   scroll=tab_scrolls['健康与测速']
   body=self.section(scroll,'分层健康','本地入口、服务器 TCP、SSH 确认、远端监听、代理业务分别判断。')
   self.probe_summary=self.label(body,'尚未采样',14,muted=True,wraplength=630,justify='left',anchor='w');self.probe_summary.pack(fill='x',pady=(10,0))
   self._action_row(body,[('立即健康探测',self._probe_now),('取消当前工具任务',self._cancel_tools)])
   body=self.section(scroll,'代理延迟与成功率','远端测试经过隧道；请求阶段计时不包含 SSH 登录。每次 3 个小响应样本。')
   self._action_row(body,[('远端代理延迟',lambda:self._latency(True)),('本机代理延迟',lambda:self._latency(False))])
   self.latency_box=self._text_box(body,105)
   self._set_box(self.latency_box,'入口 DNS / TCP / CONNECT+TLS / 首字节 / 总耗时；成功率和总耗时中位数。')
   body=self.section(scroll,'手动限量测速','固定可信 HTTPS 目标；最多读取 1 MiB，超时可取消。不会定时下载或保存响应。')
   self._action_row(body,[('远端链路测速',lambda:self._download(True)),('本机代理测速',lambda:self._download(False))])
   self.speed_label=self.label(body,'测速受测试站、代理上游与网络共同影响。',13,muted=True,wraplength=630);self.speed_label.pack(fill='x',pady=(10,0))
   scroll=tab_scrolls['代理与流量']
   body=self.section(scroll,'Clash / Mihomo 检测','控制器只访问本机回环地址，token 仅保留在本次运行内存。')
   self.api_entries={};self._entry_row(body,[('port','本机控制器端口',9090),('token','控制器 token（可留空）','')],self.api_entries);self.api_entries['token'].configure(show='●')
   self._action_row(body,[('检测 HTTP / mixed 代理',self._detect_clash),('应用所选代理端口',self._apply_proxy)])
   self.proxy_var=tk.StringVar(value='尚未检测');self.proxy_menu=self.menu(body,self.proxy_var,['尚未检测']);self.proxy_menu.pack(fill='x',pady=(10,0));self.proxy_candidates=[]
   self.proxy_detail=self.label(body,'检测会验证 HTTP CONNECT；不会修改 Clash 规则、TUN 或系统代理。',12,muted=True,wraplength=630);self.proxy_detail.pack(fill='x',pady=(8,0))
   body=self.section(scroll,'带宽图表 · Mihomo 核心总流量','来源：本机控制器 /traffic，包含其他应用；不是单条 SSH 隧道流量。')
   self.traffic_var=tk.BooleanVar(value=False)
   ctk.CTkCheckBox(body,text='显示期间每 2 秒采样（需要控制器可访问）',variable=self.traffic_var,font=self.font(13),text_color=C['text'],fg_color=C['accent']).pack(anchor='w',pady=(10,0))
   self.traffic_label=self.label(body,'上传 / 下载：尚未采样 · 单位 KiB/s',13,muted=True);self.traffic_label.pack(anchor='w',pady=(10,4))
   self.traffic_canvas=tk.Canvas(body,height=160,highlightthickness=0);self.traffic_canvas.pack(fill='x');self.traffic_canvas.bind('<Configure>',lambda e:self._draw_traffic())
   scroll=tab_scrolls['服务器仪表盘']
   body=self.section(scroll,'远程服务器仪表盘 · 只读 Linux','需要远端 Python 3 和命令执行权限；无 sudo、不安装服务。网卡速率是远端所有非回环接口总和。')
   self.dashboard_var=tk.BooleanVar(value=False)
   ctk.CTkCheckBox(body,text='显示期间每 30 秒刷新当前服务器',variable=self.dashboard_var,font=self.font(13),text_color=C['text'],fg_color=C['accent']).pack(anchor='w',pady=(10,0))
   self._action_row(body,[('读取当前服务器指标',self._dashboard)])
   self.dashboard_box=self._text_box(body,150);self._set_box(self.dashboard_box,'CPU / 负载 / 内存 / 根分区 / 运行时长 / 网卡收发速率\n尚未采样。')
  def _build_keys(self):
   scroll=self._scroll('keys','密钥工具','生成、公钥核验与 Agent 解锁；不保存密钥口令。')
   body=self.section(scroll,'生成 Ed25519 密钥','点击后打开 OpenSSH 原生交互窗口输入口令。请在该窗口完成两次口令输入；最多等待 5 分钟。')
   self.key_path_var=tk.StringVar(value=str(Path.home()/'.ssh'/'id_codextunnel_ed25519'))
   entry=ctk.CTkEntry(body,textvariable=self.key_path_var,height=40,font=self.font(14),fg_color=C['entry'],text_color=C['text']);entry.pack(fill='x',pady=(12,0))
   self._action_row(body,[('选择新文件位置',self._choose_key_path),('生成密钥',self._generate_key)])
   self.key_status=self.label(body,'同名公钥或私钥存在时拒绝覆盖；私钥仅允许所有者及系统管理账户访问。',13,muted=True,wraplength=630,justify='left');self.key_status.pack(fill='x',pady=(10,0))
   body=self.section(scroll,'生成结果与使用','只展示公钥与指纹。公钥安装到服务器由你单独操作；不会自动修改 authorized_keys。')
   self.key_box=self._text_box(body,140);self._set_box(self.key_box,'生成完成后在此显示公钥和指纹。')
   self._action_row(body,[('复制公钥',self._copy_public),('应用到当前配置',self._use_key),('Agent 解锁密钥',self._unlock_key)])
   self.label(body,'Agent 解锁需要 Windows ssh-agent 服务已启动；口令在原生窗口中输入。',12,muted=True,wraplength=630).pack(fill='x',pady=(10,0))
  def _build_preferences(self):
   scroll=self._scroll('preferences','全局偏好','应用级设置独立保存，切换服务器不会改变这些选项。')
   self.pref_vars={};self.pref_entries={}
   groups=[('桌面与隐私',[('minimize_to_tray','关闭窗口后留在托盘'),('run_at_startup','登录 Windows 后启动软件'),('start_in_tray','启动后直接进入托盘'),('mask_logs','隐藏日志中的服务器、用户名与私钥路径')]),('可靠性',[('probe_enabled','连接后自动执行分层健康探测'),('network_watch','感知切网、VPN 接口变化与休眠恢复')]),('通知',[('notifications','启用 Windows Toast（故障与恢复，默认关闭）'),('quiet_enabled','启用通知静音时段')])]
   for title,checks in groups:
    body=self.section(scroll,title)
    for key,text in checks:
     var=tk.BooleanVar();self.pref_vars[key]=var
     ctk.CTkCheckBox(body,text=text,variable=var,font=self.font(14),text_color=C['text'],fg_color=C['accent']).pack(anchor='w',pady=(12,0))
    if title=='桌面与隐私':
     self.pref_theme=tk.StringVar();self.menu(body,self.pref_theme,['light','dark','system']).pack(fill='x',pady=(12,0))
     self._entry_row(body,[('startup_delay','自动连接延迟 / 秒',5)],self.pref_entries)
    elif title=='可靠性':self._entry_row(body,[('probe_interval','探测间隔 / 秒',90),('probe_failures','连续失败阈值',3)],self.pref_entries)
    else:
     self._entry_row(body,[('notification_delay','故障持续多久通知 / 秒',20),('notification_cooldown','故障通知冷却 / 秒',120)],self.pref_entries)
     self._entry_row(body,[('quiet_start','静音开始 / 0–23 时',23),('quiet_end','静音结束 / 0–23 时',8)],self.pref_entries)
   self.button(scroll,'保存全局偏好',self._save_preferences,primary=True).pack(fill='x',pady=(0,16))
  def _populate(self):
   for key,entry in self.entries.items():entry.configure(state='normal');entry.delete(0,'end');entry.insert(0,str(getattr(self.config,key)))
   for key,var in self.checks.items():var.set(getattr(self.config,key))
   self.auth_var.set('指定私钥' if self.config.auth_mode=='key_file' else '默认密钥 / Agent')
   self.host_key_var.set('严格校验已知主机' if self.config.host_key_policy=='yes' else '首次信任，变化拒绝')
   names=list(self.cfg_mgr.profiles);self.profile_menu.configure(values=names);self.profile_var.set(self.config.profile_name)
   if hasattr(self,'connection_menu'):
    self.connection_menu.configure(values=names);self.connection_var.set(self.config.profile_name);self.primary_var.set(self.config.include_proxy_forward);self._refresh_rules();self._refresh_servers(force=True)
   self._auth_changed();self._route()
  def _populate_preferences(self):
   for key,var in self.pref_vars.items():var.set(getattr(self.preferences,key))
   if self.runtime_enabled:self.pref_vars['run_at_startup'].set(SystemHelper.is_run_at_startup_enabled())
   for key,entry in self.pref_entries.items():entry.delete(0,'end');entry.insert(0,str(getattr(self.preferences,key)))
   self.pref_theme.set(self.preferences.theme_mode)
  def _read_config(self):
   cfg=super()._read_config()
   prefs=self.cfg_mgr.preferences
   return replace(cfg,**{key:getattr(prefs,key) for key in ('theme_mode','minimize_to_tray','run_at_startup','mask_logs')})
  def _save(self):
   if not self._idle():return
   try:
    cfg=self._read_config()
    if not self.cfg_mgr.save(cfg):raise ValueError(self.cfg_mgr.last_error)
    self.config=copy.deepcopy(cfg);self.daemon=self.manager.get(cfg);self._populate();self._log('INFO','当前服务器配置已保存。')
   except Exception as exc:messagebox.showerror('保存失败',str(exc),parent=self)
  def _switch_profile(self,name):
   if self._preflight_thread and self._preflight_thread.is_alive():self._log('INFO','网络体检结束后即可切换。');return
   self._test_stop.set()
   cfg=self.cfg_mgr.switch_profile(name)
   if not cfg:self._log('WARN',self.cfg_mgr.last_error);return
   self.config=copy.deepcopy(cfg);self.daemon=self.manager.get(cfg);self._last_status=None;self._populate();self._reset_test();self._load_profile_logs();self._status(self.daemon.current_state,self.daemon.status_message);self._set_health(self.daemon.health)
   self._reset_rule();self._set_box(self.latency_box,'当前服务器尚未进行延迟测试。');self.speed_label.configure(text='当前服务器尚未测速。');self._set_box(self.dashboard_box,'当前服务器尚未采样；点击读取指标。');self._show('overview')
  def _new_profile(self):
   name=simpledialog.askstring('新建服务器','配置名称：',parent=self)
   if name is None:return
   name=name.strip()
   if name in self.cfg_mgr.profiles:messagebox.showerror('名称重复','请选择不同名称',parent=self);return
   cfg=replace(self.config,profile_name=name,profile_id=uuid.uuid4().hex,forward_rules=copy.deepcopy(self.config.forward_rules),auto_connect_on_start=False)
   if self.cfg_mgr.save(cfg):self._switch_profile(name);self._show('settings')
   else:messagebox.showerror('新建失败',self.cfg_mgr.last_error,parent=self)
  def _clone(self):self._new_profile()
  def _delete(self):
   if not self._idle():return
   ident=self.config.profile_id
   super()._delete()
   if self.config.profile_id!=ident:self.daemon=self.manager.get(self.config);self._populate()
  def _start(self):
   try:
    cfg=self._read_config()
    if not self.cfg_mgr.save(cfg):raise ValueError(self.cfg_mgr.last_error)
    self.config=copy.deepcopy(cfg);self.daemon=self.manager.get(cfg);ok,msg=self.manager.start(cfg)
    self._log('INFO' if ok else 'WARN',msg);self._show('overview');self._route()
   except Exception as exc:messagebox.showerror('连接未启动',str(exc),parent=self)
  def _toggle(self):
   if self.daemon.is_running():self._startup_cancelled.add(self.config.profile_id);self.manager.stop(self.config.profile_id);self._cancel_tools()
   else:self._start()
  def _idle(self):
   if hasattr(self,'_jobs') and any(ident==self.config.profile_id and kind not in ('toast','traffic') for ident,kind in self._jobs):
    messagebox.showinfo('请稍候','请等待或取消当前服务器的工具任务后编辑。',parent=self);return False
   return super()._idle()
  def _stop_all(self):
   self._startup_cancelled.update(c.profile_id for c in self.cfg_mgr.profiles.values())
   self.manager.stop_all()
   for (ident,kind),(job_id,cancel,thread) in list(self._jobs.items()):
    if kind in ('latency','speed','dashboard'):cancel.set()
  def _start_all(self):
   for cfg in list(self.cfg_mgr.profiles.values()):
    if not self.manager.get(cfg).is_running():
     try:ok,msg=self.manager.start(cfg);self._log('INFO' if ok else 'WARN',cfg.profile_name+'：'+msg)
     except Exception as exc:self._log('WARN',str(exc))
   self._refresh_servers(force=True)
  def _auto_start(self):
   if self._closing:return
   for cfg in list(self.cfg_mgr.profiles.values()):
    if cfg.auto_connect_on_start and cfg.profile_id not in self._startup_cancelled:
     try:self.manager.start(cfg)
     except Exception as exc:self._log('WARN',str(exc))
  def _refresh_servers(self,force=False):
   if not hasattr(self,'server_list'):return
   signature=tuple((n,c.profile_id,self.manager.get(c).current_state,self.manager.get(c).retry_count) for n,c in self.cfg_mgr.profiles.items())
   if not force and signature==self._last_server_signature:return
   self._last_server_signature=signature
   for child in self.server_list.winfo_children():child.destroy()
   for name,cfg in self.cfg_mgr.profiles.items():
    daemon=self.manager.get(cfg);body=self.section(self.server_list,name,daemon.status_message)
    self.label(body,f'{cfg.server_host}:{cfg.server_port}  ·  {len(daemon.rules(cfg))} 条规则  ·  {daemon.current_state}',13,muted=True,wraplength=630).pack(fill='x',pady=(10,0))
    self._action_row(body,[('查看此连接',lambda n=name:self._switch_profile(n)),('停止' if daemon.is_running() else '启动',lambda c=copy.deepcopy(cfg):self._server_toggle(c))])
  def _server_toggle(self,cfg):
   if self.manager.get(cfg).is_running():self._startup_cancelled.add(cfg.profile_id);self.manager.stop(cfg.profile_id)
   else:
    ok,msg=self.manager.start(cfg);self._log('INFO' if ok else 'WARN',msg)
   self._refresh_servers(force=True)
  def _load_profile_logs(self):
   self._clear()
   for level,text in self._per_logs.get(self.config.profile_id,[]):super()._log(level,text)
  def _route(self):
   cfg=self._command_config() or self.config
   if not cfg.include_proxy_forward:
    self.route_label.configure(text=f'{cfg.profile_name}  ·  {len(self.daemon.rules(cfg))} 条端口转发\n{cfg.username}@{cfg.server_host}:{cfg.server_port}  ·  监听仅限回环地址');return
   super()._route()
  def _set_health(self,health):
   super()._set_health(health)
   for key in ('listener','business'):
    label=self.health_labels.get(key)
    if label:
     value=health.get(key);label.configure(text='●  '+('通过' if value is True else '异常' if value is False else '未采样 / 不适用'),text_color=C['green' if value is True else 'red' if value is False else 'muted'])
  def _tick(self):
   if self._closing:return
   if not hasattr(self,'manager'):self.after(500,self._tick);return
   seconds=int(time.monotonic()-self.daemon.connect_start_time) if self.daemon.connect_start_time else 0
   total=int(self.daemon.total_connected_seconds+seconds)
   self.stats.configure(text=f'连续 {seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02} · 累计 {total//3600:02}:{total//60%60:02}:{total%60:02}\n重试 {self.daemon.retry_count} 次 · 会话 {self.daemon.session_count} 次')
   self._set_health(self.daemon.health)
   last=getattr(self,'_last_status',None)
   if not last or last!=(self.daemon.current_state,self.daemon.status_message):self._status(self.daemon.current_state,self.daemon.status_message)
   count=sum(d.current_state=='CONNECTED' for d in self.manager.daemons.values());self.sidebar_state.configure(text=f'●  {count} 个已连接 / {sum(d.is_running() for d in self.manager.daemons.values())} 个运行中')
   if self._page=='servers':self._refresh_servers()
   self.after(500,self._tick)
  def _refresh_rules(self):
   if not hasattr(self,'rule_list'):return
   for child in self.rule_list.winfo_children():child.destroy()
   for i,rule in enumerate(self.config.forward_rules):
    body=self.section(self.rule_list,rule.name,f'{rule.direction}  127.0.0.1:{rule.listen_port} → {rule.target_host}:{rule.target_port}')
    var=tk.BooleanVar(value=rule.enabled)
    ctk.CTkCheckBox(body,text='启用此规则',variable=var,command=lambda index=i,v=var:self._rule_enabled(index,v.get()),font=self.font(13),text_color=C['text'],fg_color=C['accent']).pack(anchor='w',pady=(12,0))
    self._action_row(body,[('编辑',lambda index=i:self._edit_rule(index)),('删除',lambda index=i:self._remove_rule(index))])
  def _commit_rules(self,cfg):
   errors=cfg.validate()
   if errors:raise ValueError('；'.join(errors.values()))
   if not self.cfg_mgr.save(cfg):raise ValueError(self.cfg_mgr.last_error)
   self.config=copy.deepcopy(cfg);self.daemon=self.manager.get(cfg);self._populate();self._route()
  def _primary_changed(self):
   if not self._idle():self.primary_var.set(self.config.include_proxy_forward);return
   try:self._commit_rules(replace(self.config,include_proxy_forward=self.primary_var.get()))
   except Exception as exc:self.primary_var.set(self.config.include_proxy_forward);messagebox.showerror('规则未保存',str(exc),parent=self)
  def _save_rule(self):
   if not self._idle():return
   try:
    rule=ForwardRule(direction=self.direction_var.get()[0],listen_port=int(self.rule_entries['listen_port'].get()),target_host=self.rule_entries['target_host'].get().strip(),target_port=int(self.rule_entries['target_port'].get()),name=self.rule_entries['name'].get().strip())
    if rule.validate():raise ValueError('；'.join(rule.validate()))
    rules=copy.deepcopy(self.config.forward_rules)
    if self._rule_edit_index is None:rules.append(rule)
    else:rules[self._rule_edit_index]=replace(rule,enabled=rules[self._rule_edit_index].enabled)
    self._commit_rules(replace(self.config,forward_rules=rules));self._reset_rule();self._log('INFO','转发规则已保存。')
   except Exception as exc:messagebox.showerror('规则需要调整',str(exc),parent=self)
  def _edit_rule(self,index):
   if not self._idle():return
   self._rule_edit_index=index;rule=self.config.forward_rules[index]
   for key,entry in self.rule_entries.items():entry.delete(0,'end');entry.insert(0,str(getattr(rule,key)))
   self.direction_var.set('L · 本地转发' if rule.direction=='L' else 'R · 反向转发');self.rule_notice.configure(text='正在编辑：'+rule.name)
  def _reset_rule(self):
   self._rule_edit_index=None
   for key,value in [('name','端口转发'),('listen_port','8888'),('target_host','127.0.0.1'),('target_port','8888')]:self.rule_entries[key].delete(0,'end');self.rule_entries[key].insert(0,value)
   self.rule_notice.configure(text='建立任一必需规则失败时整组失败；至少启用一条规则。')
  def _rule_enabled(self,index,enabled):
   if not self._idle():self._refresh_rules();return
   try:
    rules=copy.deepcopy(self.config.forward_rules);rules[index].enabled=enabled;self._commit_rules(replace(self.config,forward_rules=rules))
   except Exception as exc:self._refresh_rules();messagebox.showerror('规则未保存',str(exc),parent=self)
  def _remove_rule(self,index):
   if not self._idle():return
   try:
    rules=copy.deepcopy(self.config.forward_rules);rules.pop(index);self._commit_rules(replace(self.config,forward_rules=rules));self._reset_rule()
   except Exception as exc:messagebox.showerror('无法删除',str(exc),parent=self)
  def _share_export(self,all_profiles=False):
   names=list(self.cfg_mgr.profiles) if all_profiles else [self.config.profile_name]
   preview='\n'.join(f'{n}: {self.cfg_mgr.profiles[n].username}@{self.cfg_mgr.profiles[n].server_host}' for n in names)
   if not messagebox.askyesno('导出内容预览','分享会包含服务器地址和用户名，不包含密钥或自动启动设置。\n\n'+preview[:3000],parent=self):return
   path=filedialog.asksaveasfilename(parent=self,title='导出配置分享',defaultextension='.json',initialfile='CodexTunnel-share.json',filetypes=[('JSON','*.json')])
   if path:
    try:Path(path).write_text(json.dumps(self.cfg_mgr.export_profiles(names),ensure_ascii=False,indent=2),encoding='utf-8');self._log('INFO','无密钥配置已导出。')
    except Exception as exc:messagebox.showerror('导出失败',str(exc),parent=self)
  def _share_import(self):
   path=filedialog.askopenfilename(parent=self,title='预览配置分享',filetypes=[('JSON','*.json')])
   if not path:return
   try:
    profiles=self.cfg_mgr.preview_import(path)
    preview='\n'.join(f'{n}: {c.username}@{c.server_host}:{c.server_port}，{len(self.daemon.rules(c))} 条规则' for n,c in profiles.items())
    if not messagebox.askyesno('导入预览','新配置默认不自动连接、不自启。重名项自动改名。\n\n'+preview[:3500],parent=self):return
    if not self.cfg_mgr.import_profiles(profiles):raise ValueError(self.cfg_mgr.last_error)
    self._populate();self._log('INFO',f'已导入 {len(profiles)} 个配置，尚未连接。')
   except Exception as exc:messagebox.showerror('导入失败',str(exc),parent=self)
  def _save_preferences(self):
   old_startup=SystemHelper.is_run_at_startup_enabled()
   changed=False
   try:
    values={key:var.get() for key,var in self.pref_vars.items()};values.update({key:int(entry.get()) for key,entry in self.pref_entries.items()});values['theme_mode']=self.pref_theme.get()
    prefs=AppPreferences(**values);prefs.validate()
    if prefs.run_at_startup!=old_startup:
     if not SystemHelper.set_run_at_startup(prefs.run_at_startup):raise ValueError('Windows 启动项保存失败')
     changed=True
    if not self.cfg_mgr.save_preferences(prefs):raise ValueError(self.cfg_mgr.last_error)
    self.preferences=self.cfg_mgr.preferences;self.manager.preferences=self.preferences
    self.config=replace(self.config,**{key:getattr(self.preferences,key) for key in ('theme_mode','minimize_to_tray','run_at_startup','mask_logs')})
    ctk.set_appearance_mode(prefs.theme_mode);self._update_watcher();self._draw_traffic();self._log('INFO','全局偏好已保存。')
   except Exception as exc:
    if changed:SystemHelper.set_run_at_startup(old_startup)
    messagebox.showerror('偏好未保存',str(exc),parent=self)
  def _theme(self):
   mode='dark' if ctk.get_appearance_mode()=='Light' else 'light';ctk.set_appearance_mode(mode)
   prefs=replace(self.cfg_mgr.preferences,theme_mode=mode)
   if self.cfg_mgr.save_preferences(prefs):
    self.preferences=self.cfg_mgr.preferences;self.manager.preferences=self.preferences;self.config.theme_mode=mode
    if hasattr(self,'pref_theme'):self.pref_theme.set(mode)
   else:self._log('WARN','主题已切换，但未能保存。')
   if hasattr(self,'traffic_canvas'):self._draw_traffic()
  def _update_watcher(self):
   if self.watcher:self.watcher.close();self.watcher=None
   if self.runtime_enabled and self.preferences.network_watch:
    try:
     self.watcher=NetworkWatcher(self.manager.network_changed);self.watcher.start()
     if self.watcher.error:self._log('WARN',self.watcher.error)
    except Exception as exc:self._log('WARN','网络事件监听不可用：'+str(exc))
  def _async(self,kind,function):
   key=('global' if kind in ('traffic','key','toast','unlock') else self.config.profile_id,kind)
   if key in self._jobs:return False
   if len(self._jobs)>=8:self._log('INFO','正在执行其他工具任务，请稍候。');return False
   self._job_id+=1;job_id=self._job_id;cancel=threading.Event();cfg=copy.deepcopy(self._command_config() or self.config);run_id=self.daemon.run_id
   def work():
    try:result=function(cfg,cancel);error=None
    except Exception as exc:result=None;error=str(exc)[:600]
    self._workspace_post(('result',key,job_id,result,error,run_id))
   thread=threading.Thread(target=work,daemon=True,name='Workspace-'+kind)
   self._jobs[key]=(job_id,cancel,thread);thread.start()
   if kind not in ('traffic','toast'):self._log('INFO',kind+'：任务已开始，可在诊断页取消。')
   return True
  def _cancel_tools(self):
   for (ident,kind),(job_id,cancel,thread) in list(self._jobs.items()):
    if ident in (self.config.profile_id,'global'):cancel.set()
  def _latency(self,remote=True):
   if remote and (self.daemon.current_state!='CONNECTED' or not self.config.include_proxy_forward):self._log('INFO','远端代理测试需要已建立的默认代理转发。');return
   target=self.target_var.get()
   def task(cfg,stop):
    samples=[];errors=[]
    for i in range(3):
     if stop.is_set():raise RuntimeError('操作已取消')
     try:samples.append(MetricsService.latency(cfg,target,stop,remote))
     except Exception as exc:errors.append(str(exc)[:200])
     if i<2 and stop.wait(.4):raise RuntimeError('操作已取消')
    return {'samples':samples,'errors':errors,'remote':remote,'target':target}
   self._async('latency',task)
  def _test(self):self._latency(True)
  def _download(self,remote):
   if remote and (self.daemon.current_state!='CONNECTED' or not self.config.include_proxy_forward):self._log('INFO','请先建立默认代理隧道。');return
   if messagebox.askyesno('限量测速','从固定 HTTPS 目标读取最多 1 MiB。\n路径：'+('远端 → 隧道 → 本地代理' if remote else '本机 → 本地代理')+'\n是否开始？',parent=self):self._async('speed',lambda cfg,stop:MetricsService.speed(cfg,stop,remote))
  def _clash_client(self):return ClashClient(int(self.api_entries['port'].get()),self.api_entries['token'].get())
  def _detect_clash(self):
   try:client=self._clash_client();self._async('detect',lambda cfg,stop:client.detect(stop))
   except Exception as exc:messagebox.showerror('检测参数无效',str(exc),parent=self)
  def _apply_proxy(self):
   if not self._idle():return
   try:
    index=int(self.proxy_var.get().split(' · ',1)[0])-1;choice=self.proxy_candidates[index]
    if not messagebox.askyesno('应用检测端口',f"将当前配置本地 HTTP 代理端口设为 {choice['port']}？",parent=self):return
    cfg=replace(self.config,local_port=choice['port'])
    if not self.cfg_mgr.save(cfg):raise ValueError(self.cfg_mgr.last_error)
    self.config=cfg;self._populate();self._log('INFO','代理端口已应用。')
   except Exception as exc:messagebox.showerror('无法应用',str(exc),parent=self)
  def _probe_now(self):
   if not self.manager.probe_now(self.config.profile_id):self._log('INFO','请先建立当前连接，再进行健康探测。')
  def _dashboard(self):self._async('dashboard',lambda cfg,stop:MetricsService.dashboard(cfg,stop))
  def _choose_key_path(self):
   path=filedialog.asksaveasfilename(parent=self,title='选择新的私钥文件',initialdir=str(Path.home()/'.ssh'),initialfile='id_codextunnel_ed25519')
   if path:self.key_path_var.set(path)
  def _generate_key(self):
   path=self.key_path_var.get().strip()
   if not Path(path).is_absolute():messagebox.showerror('路径无效','请使用完整路径。',parent=self);return
   self.key_status.configure(text='原生窗口将要求输入密钥口令；完成后返回此处。')
   self._async('key',lambda cfg,stop:KeyService.generate(path,stop))
  def _copy_public(self):
   if self._key_result:self._copy(self._key_result['public_key'],'公钥')
   else:self._log('INFO','请先完成密钥生成。')
  def _use_key(self):
   if not self._idle():return
   if not self._key_result:self._log('INFO','请先完成密钥生成。');return
   cfg=replace(self.config,auth_mode='key_file',key_file_path=self._key_result['private_path'])
   if self.cfg_mgr.save(cfg):self.config=cfg;self._populate();self._log('INFO','密钥引用已应用；公钥仍需授权到服务器，口令密钥先用 Agent 解锁。')
  def _unlock_key(self):
   try:
    path=self._key_result['private_path'] if self._key_result else self.key_path_var.get().strip()
    self._async('unlock',lambda cfg,stop:KeyService.unlock_wait(path,stop))
   except Exception as exc:messagebox.showerror('Agent 解锁失败',str(exc),parent=self)
  def _copy_proxy(self):
   if not self._command_config().include_proxy_forward:self._log('INFO','当前配置没有默认代理转发。');return
   r=self._command_config().remote_port
   self._copy(f'export http_proxy=http://127.0.0.1:{r}\nexport https_proxy=http://127.0.0.1:{r}\nexport all_proxy=http://127.0.0.1:{r}\nexport HTTP_PROXY="$http_proxy" HTTPS_PROXY="$https_proxy" ALL_PROXY="$all_proxy"\nexport no_proxy=localhost,127.0.0.1,::1\nexport NO_PROXY="$no_proxy"','HTTP 代理命令')
  def _result(self,key,job_id,result,error,run_id=None):
   job=self._jobs.get(key)
   if not job or job[0]!=job_id:return
   cancelled=job[1].is_set();self._jobs.pop(key,None);ident,kind=key
   if cancelled:return
   selected=ident==self.config.profile_id
   daemon=self.manager.daemons.get(ident)
   if kind in ('latency','speed','dashboard') and daemon and run_id is not None and daemon.run_id!=run_id:return
   if error:
    if selected or kind in ('traffic','key','toast','unlock'):
     if kind=='traffic':self.traffic_label.configure(text='采样不可用：'+error[:150])
     elif kind=='dashboard':self._set_box(self.dashboard_box,'采样失败；需要 Linux / Python 3 与命令执行权限。\n'+error)
     elif kind=='key':self.key_status.configure(text='生成未完成：'+error)
     else:self._log('WARN',kind+'：'+error)
    return
   if kind=='unlock':self._log('INFO','密钥已加入 Agent。');return
   if kind=='key':
    self._key_result=result;self.key_status.configure(text='已生成：'+result['private_path']);self._set_box(self.key_box,result['fingerprint']+'\n\n'+result['public_key']);return
   if kind=='traffic':
    self._traffic_samples.append(result);self.traffic_label.configure(text=f"上传 {result['up']/1024:.1f} KiB/s · 下载 {result['down']/1024:.1f} KiB/s · {time.strftime('%H:%M:%S',time.localtime(result['sampled_at']))}");self._draw_traffic();return
   if kind=='dashboard':
    old=self._dashboard_samples.get(ident);self._dashboard_samples[ident]=result
    cpu='等待第二个样本';net='等待第二个样本'
    if old:
     total=result['cpu_total']-old['cpu_total'];idle=result['cpu_idle']-old['cpu_idle'];elapsed=result['time']-old['time']
     if total>0 and 0<=idle<=total:cpu=f'{100*(1-idle/total):.1f}%'
     if elapsed>0 and result['network_rx']>=old['network_rx'] and result['network_tx']>=old['network_tx']:net=f"收 {(result['network_rx']-old['network_rx'])/elapsed/1024:.1f} KiB/s · 发 {(result['network_tx']-old['network_tx'])/elapsed/1024:.1f} KiB/s"
    if selected:self._set_box(self.dashboard_box,f"服务器：{self.config.profile_name}\nCPU {cpu} · 1 分钟负载 {result['load']:.2f}\n内存 {result['memory_used']/2**30:.2f} / {result['memory_total']/2**30:.2f} GiB\n根分区 {result['disk_used']/2**30:.1f} / {result['disk_total']/2**30:.1f} GiB\n运行 {result['uptime']/3600:.1f} 小时\n远端非回环接口总流量：{net}\n采样 {time.strftime('%H:%M:%S',time.localtime(result['time']))}")
    return
   if not selected:return
   if kind=='detect':
    self.proxy_candidates=result;labels=[f"{i+1} · {r['port']} · {r['protocol']}" for i,r in enumerate(result)] or ['未发现可验证的 HTTP 代理'];self.proxy_menu.configure(values=labels);self.proxy_var.set(labels[0]);self.proxy_detail.configure(text='\n'.join(r['source'] for r in result) or '检查客户端是否启动、控制器授权或手动设置代理端口。')
   elif kind=='latency':
    samples=result['samples'];good=[r for r in samples if r['ok']]
    lines=[self.config.profile_name+' · '+('远端 → 隧道 → 本地代理' if result['remote'] else '本机 → 本地代理')+f" · {result['target']}",f'成功 {len(good)}/3 · 成功率 {100*len(good)/3:.0f}%']
    if good:
     lines.append(f"总耗时中位数 {statistics.median(r['total_ms'] for r in good):.0f} ms")
     sample=good[-1];lines.append(f"最近样本：入口 DNS {sample['dns_ms']:.0f} / TCP {sample['tcp_ms']:.0f} / CONNECT+TLS {sample['tls_ms']:.0f} / 首字节 {sample['ttfb_ms']:.0f} ms · HTTP {sample['http']}")
    lines.extend(result['errors']);self._set_box(self.latency_box,'\n'.join(lines));self.e2e_label.configure(text=lines[1],text_color=C['green' if good else 'red'])
   elif kind=='speed':self.speed_label.configure(text=f"{result['path']}\n读取 {result['bytes']/1024:.0f} KiB · {result['seconds']:.2f} 秒 · {result['bytes_per_second']/1024:.1f} KiB/s；代表此路径本次样本。")
  def _draw_traffic(self):
   if not hasattr(self,'traffic_canvas'):return
   canvas=self.traffic_canvas;dark=ctk.get_appearance_mode()=='Dark';canvas.configure(bg=C['entry'][1 if dark else 0]);canvas.delete('all')
   width=max(250,canvas.winfo_width());height=160;text=C['muted'][1 if dark else 0]
   values=list(self._traffic_samples);maximum=max([max(r['up'],r['down'])/1024 for r in values]+[1])
   for fraction in (0,.5,1):
    y=135-fraction*105;canvas.create_line(48,y,width-15,y,fill=C['border'][1 if dark else 0]);canvas.create_text(43,y,text=f'{maximum*fraction:.0f}',anchor='e',fill=text,font=('Microsoft YaHei UI',10))
   canvas.create_text(48,10,text='KiB/s · 最近有效样本（间隔约 2 秒）',anchor='w',fill=text,font=('Microsoft YaHei UI',10))
   for key,color,label in [('up','#BC3D73','上传'),('down','#428FBA','下载')]:
    points=[]
    for i,row in enumerate(values):points.extend([48+(width-65)*i/max(1,len(values)-1),135-105*(row[key]/1024)/maximum])
    if len(points)>=4:canvas.create_line(*points,fill=color,width=2)
    elif points:canvas.create_oval(points[0]-2,points[1]-2,points[0]+2,points[1]+2,fill=color,outline=color)
    canvas.create_text(width-(92 if key=='up' else 28),150,text=label,fill=color,font=('Microsoft YaHei UI',10))
  def _workspace_tick(self):
   if self._closing:return
   for i in range(200):
    try:event=self.workspace_events.get_nowait()
    except queue.Empty:break
    if event[0]=='result':self._result(*event[1:]);continue
    _,ident,kind,values=event
    if kind=='status' and values[0] in ('STOPPING','STOPPED'):
     for (job_ident,job_kind),(job_id,cancel,thread) in list(self._jobs.items()):
      if job_ident==ident and job_kind in ('latency','speed','dashboard'):cancel.set()
    if kind=='log':
     cfg=self.manager.profiles.get(ident);level,text=values
     if self.preferences.mask_logs:text=SystemHelper.mask_sensitive_text(text,cfg)
     self._per_logs.setdefault(ident,deque(maxlen=1000)).append((level,text))
     if ident==self.config.profile_id:super()._log(level,text)
    elif ident==self.config.profile_id:
     if kind=='status':self._status(*values)
     elif kind=='health':self._set_health(*values)
     elif kind=='diag':self._diag(*values)
     elif kind=='network':self._log('INFO',values[0])
   health=self.manager.health.get(self.config.profile_id,{})
   success=health.get('last_success');last=time.strftime('%H:%M:%S',time.localtime(success)) if success else '暂无'
   self.probe_summary.configure(text=f"{health.get('message','尚未采样')}\n最近业务成功 {last} · 连续失败 {health.get('failures',0)} 次 · "+('探测方式部分不可用' if health.get('unavailable') else '已降级' if health.get('degraded') else '按层独立判断'))
   if health.get('degraded') and self.daemon.current_state=='CONNECTED':self.status_detail.configure(text='SSH 已连接，业务健康降级：'+health.get('message',''))
   now=time.monotonic()
   self.tool_activity.configure(text=f'后台任务：{len(self._jobs)}')
   if self._page=='monitor':
    if self.monitor_tabs.get()=='代理与流量' and self.traffic_var.get() and now>=self._next_traffic:
     self._next_traffic=now+2
     try:client=self._clash_client();self._async('traffic',lambda cfg,stop:client.traffic())
     except Exception as exc:self.traffic_var.set(False);self.traffic_label.configure(text=str(exc))
    if self.monitor_tabs.get()=='服务器仪表盘' and self.dashboard_var.get() and self.daemon.current_state=='CONNECTED' and now>=self._next_dashboard:self._next_dashboard=now+30;self._dashboard()
   for ident,daemon in list(self.manager.daemons.items()):
    state='DEGRADED' if daemon.current_state=='CONNECTED' and self.manager.health.get(ident,{}).get('degraded') else daemon.current_state
    message=self.notification_gate.update(ident,state,self.preferences)
    if message and self.runtime_enabled:
     # Generic content respects privacy and a single background worker is bounded by _async.
     self._async('toast',lambda cfg,stop,m=message:ToastService.send('CodexTunnel',m,stop))
   self.after(250,self._workspace_tick)
  def _close(self):
   if self.preferences.minimize_to_tray and self.tray_available:self.withdraw()
   else:self._exit()
  def _exit(self):
   if self._closing:return
   if not hasattr(self,'manager'):return super()._exit()
   self._closing=True;self._test_stop.set()
   if self.watcher:self.watcher.close()
   self.manager.close()
   for job_id,cancel,thread in list(self._jobs.values()):cancel.set()
   self.toggle_btn.configure(state='disabled',text='正在回收连接与任务…');self._finish_exit()
  def _finish_exit(self):
   if not hasattr(self,'manager'):return super()._finish_exit()
   busy=self.manager.is_running() or any(thread.is_alive() for job_id,cancel,thread in list(self._jobs.values())) or (self._preflight_thread and self._preflight_thread.is_alive())
   if busy:self.after(100,self._finish_exit);return
   self.tray.stop();self.destroy()
 return WorkspaceApp

