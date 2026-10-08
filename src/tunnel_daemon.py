"""One worker owns each SSH lifecycle; callbacks never touch GUI objects."""
import copy
import subprocess
import threading
import time
from collections import deque
from dataclasses import replace
from datetime import datetime

from src.config_manager import TunnelConfig
from src.net_probes import NetworkProbes
from src.ssh_finder import SSHFinder
from src.system_helper import SystemHelper
from src.process_scope import ProcessScope

class TunnelDaemon:
    STATE_STOPPED = 'STOPPED'
    STATE_PRECHECKING = 'PRECHECKING'
    STATE_CONNECTING = 'CONNECTING'
    STATE_CONNECTED = 'CONNECTED'
    STATE_RECONNECTING = 'RECONNECTING'
    STATE_STOPPING = 'STOPPING'
    STATE_ERROR = 'ERROR'

    def __init__(self,on_status_change=None,on_log=None,on_diagnostic=None,on_health=None):
        self.on_status_change = on_status_change
        self.on_log = on_log
        self.on_diagnostic = on_diagnostic
        self.on_health = on_health
        self.current_state = self.STATE_STOPPED
        self.status_message = '隧道已停止'
        self._worker_thread = None
        self._stop_event = threading.Event()
        self._current_process = None
        self._process_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self.connect_start_time = None
        self.retry_count = 0
        self._consecutive_failures = 0
        self._current_config = None
        self._last_diagnostic = None
        self.health = {'local':None,'server':None,'tunnel':None,'listener':None,'business':None}
        self.total_connected_seconds=0.0
        self.session_count=0
        self.run_id=0
        self.last_connected_at=None
        self._network_changed=threading.Event()

    @staticmethod
    def _notify(callback,*args):
        if callback:
            callback(*args)

    def _set_state(self,state,message):
        self.current_state = state
        self.status_message = message
        self._notify(self.on_status_change,state,message)

    def _log(self,level,message):
        cfg = self._current_config
        if cfg and cfg.mask_logs:
            message = SystemHelper.mask_sensitive_text(message,cfg)
        self._notify(self.on_log,level,f'[{datetime.now():%H:%M:%S}] [{level}] {message}')

    def _health(self,**values):
        self.health.update(values)
        self._notify(self.on_health,dict(self.health))

    def _diagnostic(self,diag):
        # GUI has a persistent diagnostic card, not a popup on every retry.
        signature = (diag['title'],diag['reason'])
        if signature != self._last_diagnostic:
            self._last_diagnostic = signature
            self._notify(self.on_diagnostic,diag)
        self._log('WARN',diag['title']+'：'+diag['reason'])

    def start(self,config: TunnelConfig) -> bool:
        with self._lifecycle_lock:
            if self.is_running():
                return False
            snapshot = copy.deepcopy(config)
            errors = snapshot.validate()
            if errors:
                self._set_state(self.STATE_ERROR,'；'.join(errors.values()))
                return False
            self.run_id+=1
            self._current_config = snapshot
            self._stop_event.clear()
            self.retry_count = 0
            self._consecutive_failures = 0
            self.connect_start_time = None
            self._last_diagnostic = None
            self._health(local=None,server=None,tunnel=None,listener=None,business=None)
            self._set_state(self.STATE_PRECHECKING,'正在检查本地代理和服务器端口')
            self._worker_thread = threading.Thread(target=self._run_loop,args=(snapshot,),daemon=True,name='TunnelWorker')
            self._worker_thread.start()
            return True

    def is_running(self):
        return self._worker_thread is not None and self._worker_thread.is_alive()

    def stop(self):
        # Non-blocking GUI command: only worker kills/waits/clears its process.
        with self._lifecycle_lock:
            self._stop_event.set()
            if self.is_running():
                self._set_state(self.STATE_STOPPING,'正在停止并回收 SSH 进程…')
            else:
                self.connect_start_time = None
                self._health(local=None,server=None,tunnel=False)
                self._set_state(self.STATE_STOPPED,'隧道已停止')

    def wait_stopped(self,timeout=5):
        worker = self._worker_thread
        if worker:
            worker.join(timeout)
        return not self.is_running()

    def _wait_delay(self,seconds):
        deadline=time.monotonic()+seconds
        while not self._stop_event.wait(min(.2,max(.001,deadline-time.monotonic()))):
            if self._network_changed.is_set(): self._network_changed.clear(); return True
            if time.monotonic()>=deadline: return True
        return False

    def request_network_check(self):
        if self.is_running() and not self._stop_event.is_set(): self._network_changed.set()

    def _accumulate(self):
        if self.connect_start_time is not None:
            self.total_connected_seconds+=max(0,time.monotonic()-self.connect_start_time)
            self.connect_start_time=None

    @staticmethod
    def rules(config):
        result=[]
        if config.include_proxy_forward:
            result.append(('R',config.remote_port,'127.0.0.1',config.local_port))
        result.extend((r.direction,r.listen_port,r.target_host,r.target_port) for r in config.forward_rules if r.enabled)
        return result

    @staticmethod
    def rule_confirmed(line,rule):
        direction,listen,host,target=rule
        if direction=='R':
            return 'remote forward success for:' in line and f'listen 127.0.0.1:{listen}, connect {host}:{target}' in line
        return f'Local forwarding listening on 127.0.0.1 port {listen}.' in line

    def _retry(self,config,message,fatal=False):
        self._accumulate()
        self._health(tunnel=False,listener=None,business=None)
        if self._stop_event.is_set():
            return False
        if fatal or not config.auto_reconnect:
            self._set_state(self.STATE_ERROR,message)
            return False
        self.retry_count += 1
        self._consecutive_failures += 1
        # Bounded exponential backoff prevents permanent failures hammering sshd.
        delay = min(120,config.reconnect_delay*(2**min(self._consecutive_failures-1,4)))
        self._set_state(self.STATE_RECONNECTING,f'{message} · {delay} 秒后重试（第 {self.retry_count} 次）')
        return self._wait_delay(delay)

    @staticmethod
    def forwarding_confirmed(line,config):
        expected = f'listen 127.0.0.1:{config.remote_port}, connect 127.0.0.1:{config.local_port}'
        return 'remote forward success for:' in line and expected in line

    def _run_session(self,ssh_bin,config):
        self.run_id+=1  # Every SSH attempt invalidates late results from the previous transport.
        cmd = SystemHelper.ssh_base_command(ssh_bin,config)
        cmd.extend(['-v','-N','-o','ExitOnForwardFailure=yes'])
        rules=self.rules(config)
        for direction,listen,host,target in rules:
            address=f'[{host}]' if ':' in host else host
            cmd.extend(['-'+direction,f'127.0.0.1:{listen}:{address}:{target}'])
        cmd.extend(['--',f'{config.username}@{config.server_host}'])
        self._log('INFO',f'建立 SSH 隧道：{config.username}@{config.server_host}，共 {len(rules)} 条规则')
        ready = threading.Event()
        confirmed=set()
        errors = deque(maxlen=150)
        proc = None
        scope = None
        reader = None
        timed_out = False
        try:
            if self._stop_event.is_set():
                return '',None
            proc = subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                                    text=True,encoding='utf-8',errors='replace',creationflags=SystemHelper.creationflags())
            scope = ProcessScope(proc)
            with self._process_lock:
                self._current_process = proc
            def consume():
                try:
                    for raw in iter(lambda:proc.stderr.readline(4096),''):
                        line = raw.strip()
                        for i,rule in enumerate(rules):
                            if self.rule_confirmed(line,rule): confirmed.add(i)
                        if len(confirmed)==len(rules): ready.set()
                        if not line.startswith('debug') and line:
                            errors.append(line[:1000])
                            self._log('SSH',line)
                except (OSError,ValueError):
                    pass
            reader = threading.Thread(target=consume,daemon=True,name='SSHOutput')
            reader.start()
            deadline = time.monotonic()+config.connect_timeout+5
            last_local_probe = time.monotonic()
            announced = False
            while not self._stop_event.wait(0.1):
                if proc.poll() is not None:
                    break
                if ready.is_set() and not announced:
                    announced = True
                    self.connect_start_time = time.monotonic()
                    self._consecutive_failures=0
                    self.session_count+=1
                    self.last_connected_at=time.time()
                    self._last_diagnostic = None
                    self._health(tunnel=True)
                    self._set_state(self.STATE_CONNECTED,f'{len(rules)} 条转发监听已确认 · 业务健康单独探测')
                    self._log('SUCCESS',f'全部转发监听已确认，共 {len(rules)} 条规则')
                if not announced and time.monotonic() > deadline:
                    timed_out = True
                    break
                changed=self._network_changed.is_set()
                if changed: self._network_changed.clear()
                if announced and changed:
                    reachable,_=NetworkProbes.probe_remote_server(config.server_host,config.server_port,stop_event=self._stop_event)
                    self._health(server=reachable)
                    if not reachable: self._log('WARN','网络变化后新 TCP 探测不可达；现有 SSH 传输由保活继续确认')
                if announced and config.include_proxy_forward and (changed or time.monotonic()-last_local_probe >= 15):
                    local_ok,msg = NetworkProbes.probe_local_proxy(port=config.local_port)
                    last_local_probe = time.monotonic()
                    self._health(local=local_ok)
                    if not local_ok:
                        errors.append(msg)
                        break
            SystemHelper.terminate_process(proc)
            if reader:
                reader.join(timeout=2)
            if timed_out:
                errors.append('未在超时内收到全部 SSH 转发监听确认；请检查客户端版本和认证配置。')
            return '\n'.join(errors),proc.returncode
        finally:
            if scope:
                scope.close()
            if proc:
                SystemHelper.terminate_process(proc)
                if reader:
                    reader.join(timeout=2)
                if proc.stderr:
                    proc.stderr.close()
            with self._process_lock:
                if self._current_process is proc:
                    self._current_process = None

    def _run_loop(self,config):
        try:
            ssh_bin,msg = SSHFinder.find_ssh()
            if not ssh_bin:
                self._diagnostic(dict(title='找不到 OpenSSH',reason=msg,solution='安装 Windows 可选功能中的 OpenSSH 客户端。',level='error',fatal=True))
                self._set_state(self.STATE_ERROR,'找不到 OpenSSH 客户端')
                return
            while not self._stop_event.is_set():
                self._set_state(self.STATE_PRECHECKING,'正在检查本地代理和服务器端口')
                local_ok,local_msg = NetworkProbes.probe_local_proxy(port=config.local_port) if config.include_proxy_forward else (True,'转发模式不需要本机代理')
                self._health(local=local_ok if config.include_proxy_forward else None,server=None,tunnel=False)
                if self._stop_event.is_set():
                    break
                if not local_ok:
                    self._diagnostic(dict(title='本地代理未就绪',reason=local_msg,solution='开启 Clash 等代理服务，并检查配置中的本地混合端口。',level='warning'))
                    if self._retry(config,'本地代理未就绪'):
                        continue
                    break
                server_ok,server_msg = NetworkProbes.probe_remote_server(config.server_host,config.server_port,stop_event=self._stop_event)
                self._health(server=server_ok)
                if self._stop_event.is_set():
                    break
                if not server_ok:
                    self._diagnostic(dict(title='服务器不可达',reason=server_msg,solution='检查内网、VPN、服务器地址与 SSH 端口。',level='warning'))
                    if self._retry(config,'服务器不可达'):
                        continue
                    break
                self._set_state(self.STATE_CONNECTING,'正在认证并请求远程端口转发…')
                stderr,code = self._run_session(ssh_bin,config)
                if self._stop_event.is_set():
                    break
                diagnostic = NetworkProbes.diagnose_error(stderr,config.remote_port,config.server_host,config.username)
                if diagnostic:
                    self._diagnostic(diagnostic)
                elif stderr:
                    self._diagnostic(dict(title='隧道未能保持连接',reason=stderr[-600:],solution='查看日志；确认远端允许转发、本地代理可用和 SSH 客户端兼容。',level='warning'))
                if not self._retry(config,f'SSH 连接结束（代码 {code}）',bool(diagnostic and diagnostic.get('fatal'))):
                    break
        except Exception as exc:
            self._log('ERROR',f'隧道工作线程异常：{exc}')
            self._set_state(self.STATE_ERROR,f'连接失败：{exc}')
        finally:
            self._accumulate()
            self._health(tunnel=False,listener=None,business=None)
            if self._stop_event.is_set():
                self._health(local=None,server=None,tunnel=False)
                self._set_state(self.STATE_STOPPED,'隧道已停止，SSH 进程已回收')

    def test_e2e(self,config,**kwargs):
        ssh_bin,msg = SSHFinder.find_ssh()
        if not ssh_bin:
            return False,msg
        return SystemHelper.test_remote_proxy_e2e(ssh_bin,replace(config),**kwargs)
