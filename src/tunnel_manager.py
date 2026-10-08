"""Per-profile lifecycles, bounded health jobs and global network events."""
import copy
import threading
import time
from collections import deque
from src.tunnel_daemon import TunnelDaemon
from src.services import MetricsService

class TunnelManager:
    def __init__(self,preferences,on_event=None,max_connections=5):
        self.preferences=preferences; self.on_event=on_event; self.max_connections=max_connections
        self.daemons={}; self.profiles={}; self.health={}; self.history={}; self._jobs={}; self._due={}; self._manual=set()
        self._lock=threading.RLock(); self._stop=threading.Event()
        self._thread=threading.Thread(target=self._scheduler,daemon=True,name='ProbeScheduler'); self._thread.start()
    def emit(self,profile_id,kind,*values):
        if self.on_event: self.on_event(profile_id,kind,*values)
    def get(self,config):
        with self._lock:
            ident=config.profile_id
            if ident not in self.daemons:
                self.daemons[ident]=TunnelDaemon(on_status_change=lambda *v:self.emit(ident,'status',*v),on_log=lambda *v:self.emit(ident,'log',*v),on_diagnostic=lambda *v:self.emit(ident,'diag',*v),on_health=lambda *v:self.emit(ident,'health',*v))
                self.history[ident]=deque(maxlen=120); self.health[ident]={'business':None,'listener':None,'failures':0,'last_success':None,'message':'尚未采样','unavailable':False}
            if not self.daemons[ident].is_running(): self.profiles[ident]=copy.deepcopy(config)
            return self.daemons[ident]
    def start(self,config):
        with self._lock:
            daemon=self.get(config)
            if daemon.is_running(): return False,'此连接已运行'
            if sum(d.is_running() for d in self.daemons.values())>=self.max_connections: return False,f'最多同时运行 {self.max_connections} 个连接'
            new=TunnelDaemon.rules(config)
            for ident,other in self.daemons.items():
                if not other.is_running(): continue
                old=self.profiles[ident]
                for a in new:
                    for b in TunnelDaemon.rules(old):
                        if a[0]==b[0] and a[1]==b[1] and (a[0]=='L' or (config.server_host.lower().rstrip('.'),config.server_port)==(old.server_host.lower().rstrip('.'),old.server_port)):
                            return False,f'监听端口 {a[1]} 与运行中的 {old.profile_name} 冲突'
            self.profiles[config.profile_id]=copy.deepcopy(config)
            self.health[config.profile_id]={'business':None,'listener':None,'failures':0,'last_success':None,'message':'等待首次探测','unavailable':False}
            self._due[config.profile_id]=time.monotonic()+3
            return daemon.start(config),'启动请求已提交'
    def stop(self,ident):
        with self._lock:
            if ident in self._jobs: self._jobs[ident][1].set()
            if ident in self.daemons: self.daemons[ident].stop()
    def stop_all(self):
        for ident in list(self.daemons): self.stop(ident)
    def network_changed(self):
        with self._lock:
            for ident,daemon in self.daemons.items():
                if daemon.is_running() and not daemon._stop_event.is_set():
                    daemon.request_network_check(); self._due[ident]=time.monotonic()+3
                    self.emit(ident,'network','网络环境变化，正在复核连接')
    def probe_now(self,ident):
        with self._lock:
            daemon=self.daemons.get(ident)
            if not daemon or daemon.current_state!='CONNECTED': return False
            self._due[ident]=0; self._manual.add(ident); return True
    def _scheduler(self):
        while not self._stop.wait(.5):
            if not self.preferences.probe_enabled and not self._manual: continue
            with self._lock:
                for ident,daemon in list(self.daemons.items()):
                    if not self.preferences.probe_enabled and ident not in self._manual: continue
                    if daemon.current_state!='CONNECTED' or daemon._stop_event.is_set(): continue
                    job=self._jobs.get(ident)
                    if job and job[0].is_alive(): continue
                    if time.monotonic()<self._due.get(ident,0): continue
                    cancel=threading.Event(); cfg=copy.deepcopy(self.profiles[ident]); run_id=daemon.run_id
                    thread=threading.Thread(target=self._probe,args=(ident,cfg,run_id,cancel),daemon=True,name='HealthProbe')
                    self._jobs[ident]=(thread,cancel); self._manual.discard(ident); self._due[ident]=time.monotonic()+self.preferences.probe_interval
                    thread.start()
    def _probe(self,ident,cfg,run_id,cancel):
        if cancel.is_set() or self._stop.is_set(): return
        result={'listener':None,'business':None,'unavailable':False}; latency=None; messages=[]
        ports=[r[1] for r in TunnelDaemon.rules(cfg) if r[0]=='R']
        if ports:
            try:
                listeners=MetricsService.remote_listener(cfg,ports,cancel)
                if set(listeners)!=set(map(str,ports)) or any(type(v) is not bool for v in listeners.values()): raise ValueError('监听探针响应无效')
                result['listener']=all(listeners.values())
            except Exception as exc:
                result['unavailable']=True; messages.append(str(exc)[:160])
        if cfg.include_proxy_forward and not cancel.is_set():
            try:
                latency=MetricsService.latency(cfg,stop=cancel)
                result['business']=latency['ok']; messages.append(f"目标 HTTP {latency['http']} · {latency['total_ms']:.0f} ms")
            except Exception as exc:
                text=str(exc)
                unsupported=any(word in text.lower() for word in ('not found','permission denied','administratively prohibited','权限'))
                result['business']=None if unsupported else False
                result['unavailable']=result['unavailable'] or unsupported
                messages.append(text[:180])
        elif not ports: messages.append('本地转发已建立；目标业务需独立验证')
        result['message']=' · '.join(messages) or '监听检查完成'
        with self._lock:
            daemon=self.daemons.get(ident)
            if cancel.is_set() or self._stop.is_set() or not daemon or daemon.run_id!=run_id or daemon.current_state!='CONNECTED': return
            old=self.health[ident]
            failed=result.get('business') is False or result.get('listener') is False
            result['failures']=old['failures']+1 if failed else 0
            result['degraded']=result['failures']>=self.preferences.probe_failures
            result['last_success']=time.time() if result.get('business') is True else old['last_success']
            result['sampled_at']=time.time()
            self.health[ident]=result
            daemon._health(listener=result.get('listener'),business=result.get('business'))
            if latency: self.history[ident].append(latency)
            self.emit(ident,'probe',copy.deepcopy(result))
    def close(self):
        self._stop.set(); self.stop_all()
    def is_running(self):
        return any(d.is_running() for d in list(self.daemons.values())) or any(t.is_alive() for t,c in list(self._jobs.values()))
