"""Native network changes, gated desktop Toast, and a native key generation wizard."""
import ctypes
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from src.resources import asset_path
from src.process_scope import ProcessScope
from src.services import CommandRunner
from src.system_helper import SystemHelper

class NetworkWatcher:
    def __init__(self,callback):
        self.callback=callback; self._stop=threading.Event(); self._changed=threading.Event(); self._handle=None; self._native=None; self._callback=None; self.error=''
    def start(self):
        if sys.platform=='win32':
            from ctypes import wintypes
            native=ctypes.WinDLL('iphlpapi',use_last_error=True)
            callback_type=ctypes.WINFUNCTYPE(None,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int)
            self._callback=callback_type(lambda *args:self._changed.set())
            native.NotifyIpInterfaceChange.argtypes=[ctypes.c_ushort,callback_type,ctypes.c_void_p,ctypes.c_ubyte,ctypes.POINTER(wintypes.HANDLE)]
            native.NotifyIpInterfaceChange.restype=ctypes.c_ulong
            native.CancelMibChangeNotify2.argtypes=[wintypes.HANDLE]; native.CancelMibChangeNotify2.restype=ctypes.c_ulong
            handle=wintypes.HANDLE()
            code=native.NotifyIpInterfaceChange(0,self._callback,None,0,ctypes.byref(handle))
            if code: self.error=f'网络接口事件注册失败 {code}'
            else: self._native=native; self._handle=handle
        self._thread=threading.Thread(target=self._run,daemon=True,name='NetworkWatcher'); self._thread.start()
    def _run(self):
        previous=time.monotonic(); due=None
        while not self._stop.wait(.5):
            now=time.monotonic()
            if now-previous>8: self._changed.set()  # Recheck after suspend/resume or long scheduler pause.
            previous=now
            if self._changed.is_set(): self._changed.clear(); due=now+3
            if due is not None and now>=due: due=None; self.callback()
    def close(self):
        self._stop.set()
        if self._handle:
            self._native.CancelMibChangeNotify2(self._handle); self._handle=None

class NotificationGate:
    def __init__(self): self.states={}
    @staticmethod
    def quiet(prefs,hour):
        if not prefs.quiet_enabled: return False
        a,b=prefs.quiet_start,prefs.quiet_end
        if a==b: return True
        return a<=hour<b if a<b else hour>=a or hour<b
    def update(self,ident,state,prefs,now=None,hour=None):
        now=time.monotonic() if now is None else now
        hour=datetime.now().hour if hour is None else hour
        row=self.states.setdefault(ident,{'since':None,'warned':False,'last':-1e12})
        if state in ('STOPPED','STOPPING'):
            row['since']=None; row['warned']=False; return None
        if state=='CONNECTED':
            row['since']=None
            if row['warned']:
                row['warned']=False
                if prefs.notifications and not self.quiet(prefs,hour): row['last']=now; return '连接已恢复'
            return None
        if state not in ('ERROR','RECONNECTING','DEGRADED'): return None
        if row['since'] is None: row['since']=now
        if prefs.notifications and not self.quiet(prefs,hour) and not row['warned'] and now-row['since']>=prefs.notification_delay and now-row['last']>=prefs.notification_cooldown:
            row['warned']=True; row['last']=now; return '连接持续异常，请打开软件查看诊断'
        return None

class ToastService:
    @staticmethod
    def send(title,message,stop=None):
        if sys.platform!='win32': raise RuntimeError('Toast 仅支持 Windows')
        script=asset_path('assets/toast.ps1')
        with tempfile.TemporaryDirectory(prefix='CodexTunnel-toast-') as folder:
            payload=Path(folder)/'message.json'
            payload.write_text(json.dumps({'title':title[:80],'message':message[:250],'icon':str(asset_path('app_icon.ico'))},ensure_ascii=False),encoding='utf-8')
            cmd=['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),'-PayloadFile',str(payload)]
            code,out,err=CommandRunner.run(cmd,stop,8,8192)
            if code: raise RuntimeError('Windows 通知不可用：'+err[:200])

class KeyService:
    @staticmethod
    def locate(name):
        from src.ssh_finder import SSHFinder
        ssh,_=SSHFinder.find_ssh()
        sibling=Path(ssh).with_name(name+'.exe') if ssh else None
        binary=str(sibling) if sibling and sibling.is_file() else shutil.which(name+'.exe') or shutil.which(name)
        if not binary: raise RuntimeError(f'找不到 {name}；请安装 OpenSSH 客户端')
        return binary
    @staticmethod
    def generate(path,stop=None):
        if sys.platform!='win32': raise RuntimeError('原生密钥向导仅支持 Windows')
        target=Path(path).resolve(); public=Path(str(target)+'.pub')
        if target.exists() or public.exists(): raise ValueError('同名私钥或公钥已存在，拒绝覆盖')
        if not target.parent.is_dir(): target.parent.mkdir(parents=True,exist_ok=True)
        binary=KeyService.locate('ssh-keygen')
        scope=None
        folder=Path(tempfile.mkdtemp(prefix='.CodexTunnel-key-',dir=target.parent)); proc=None; private_created=False; public_created=False
        try:
            code,out,err=CommandRunner.run(['whoami.exe','/user','/fo','csv','/nh'],stop,5,8192)
            match=re.search(r'S-1-5-(?:\d+-)*\d+',out)
            if code or not match: raise RuntimeError('无法识别当前用户 SID')
            sid=match.group()
            code,out,err=CommandRunner.run(['icacls.exe',str(folder),'/inheritance:r','/grant:r',f'*{sid}:(OI)(CI)F'],stop,5,8192)
            if code: raise RuntimeError('无法限制密钥目录权限；未生成私钥')
            temporary=folder/'id_ed25519'
            # Native console owns passphrase input; no passphrase enters our argv, logs or configuration.
            proc=subprocess.Popen([binary,'-t','ed25519','-a','64','-f',str(temporary),'-C','CodexTunnel'],creationflags=subprocess.CREATE_NEW_CONSOLE)
            scope=ProcessScope(proc)
            deadline=time.monotonic()+300
            while proc.poll() is None:
                if stop and stop.is_set(): raise RuntimeError('密钥生成已取消')
                if time.monotonic()>deadline: raise RuntimeError('密钥生成窗口等待超时')
                time.sleep(.1)
            if proc.returncode or not temporary.is_file() or not Path(str(temporary)+'.pub').is_file(): raise RuntimeError('生成未完成，原有文件保持不变')
            code,out,err=CommandRunner.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(asset_path('assets/key_acl.ps1')),'-KeyPath',str(temporary),'-OwnerSid',sid],stop,8,8192)
            if code: raise RuntimeError('生成密钥权限检查未通过：'+err[:160])
            # Hard links are create-if-absent and preserve the restricted ACL. No overwrite race.
            os.link(temporary,target); private_created=True
            os.link(Path(str(temporary)+'.pub'),public); public_created=True
            code,out,err=CommandRunner.run([binary,'-l','-f',str(public)],stop,5,8192)
            if code: raise RuntimeError('公钥指纹读取失败')
            return {'private_path':str(target),'public_path':str(public),'public_key':public.read_text(encoding='utf-8').strip(),'fingerprint':out.strip()}
        except Exception:
            if private_created and target.exists() and os.path.samefile(target,temporary): target.unlink(missing_ok=True)
            if public_created and public.exists() and os.path.samefile(public,Path(str(temporary)+'.pub')): public.unlink(missing_ok=True)
            raise
        finally:
            if scope: scope.close()
            if proc: SystemHelper.terminate_process(proc)
            shutil.rmtree(folder)
    @staticmethod
    def unlock(path):
        if not Path(path).is_file(): raise ValueError('私钥文件不存在')
        return subprocess.Popen([KeyService.locate('ssh-add'),str(Path(path).resolve())],creationflags=subprocess.CREATE_NEW_CONSOLE)

    @staticmethod
    def unlock_wait(path,stop=None):
        proc=KeyService.unlock(path); scope=None
        try:
            scope=ProcessScope(proc); deadline=time.monotonic()+300
            while proc.poll() is None:
                if stop and stop.is_set(): raise RuntimeError('Agent 解锁已取消')
                if time.monotonic()>deadline: raise RuntimeError('Agent 解锁等待超时')
                time.sleep(.1)
            if proc.returncode: raise RuntimeError('ssh-add 未完成；确认 ssh-agent 服务已运行')
            return True
        finally:
            if scope: scope.close()
            SystemHelper.terminate_process(proc)
