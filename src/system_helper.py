"""Explicit SSH policy, cancellable verification, and Windows integration."""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Tuple
from src.process_scope import ProcessScope

TEST_TARGETS = {
    'Cloudflare': 'https://www.cloudflare.com/cdn-cgi/trace',
    'Google': 'https://www.google.com/generate_204',
    'OpenAI': 'https://api.openai.com/v1/models',
}

class SystemHelper:
    REG_KEY_PATH = r'Software\Microsoft\Windows\CurrentVersion\Run'
    APP_REG_NAME = 'CodexTunnelAssistant'

    @staticmethod
    def creationflags():
        return subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

    @staticmethod
    def ssh_base_command(ssh_bin, config):
        errors = config.validate()
        if errors:
            raise ValueError('；'.join(errors.values()))
        # Ignore user config directives that could run local commands, add forwards,
        # use a persistent master, or silently change the target. Default keys and
        # the local agent remain available. Hostnames must resolve directly.
        cmd = [ssh_bin, '-F', 'none', '-T', '-a', '-x']
        options = {
            'BatchMode':'yes', 'ConnectTimeout':str(config.connect_timeout),
            'ConnectionAttempts':'1', 'StrictHostKeyChecking':config.host_key_policy,
            'ServerAliveInterval':str(config.keepalive_interval),
            'ServerAliveCountMax':str(config.keepalive_count_max),
            'PermitLocalCommand':'no', 'ForwardAgent':'no', 'ForwardX11':'no',
            'ControlMaster':'no', 'ControlPath':'none', 'IPQoS':'none',
        }
        for key,value in options.items():
            cmd.extend(['-o',f'{key}={value}'])
        cmd.extend(['-p',str(config.server_port)])
        if config.auth_mode == 'key_file':
            cmd.extend(['-o','IdentitiesOnly=yes','-i',config.key_file_path])
        return cmd

    @staticmethod
    def terminate_process(proc):
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

    @classmethod
    def set_run_at_startup(cls, enable: bool) -> bool:
        if sys.platform != 'win32':
            return False
        try:
            import winreg
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,cls.REG_KEY_PATH,0,winreg.KEY_SET_VALUE) as key:
                if enable:
                    if getattr(sys,'frozen',False):
                        command = subprocess.list2cmdline([sys.executable])
                    else:
                        pythonw = Path(sys.executable).with_name('pythonw.exe')
                        interpreter = str(pythonw) if pythonw.exists() else sys.executable
                        command = subprocess.list2cmdline([interpreter,str(Path(__file__).resolve().parent.parent/'main.py')])
                    winreg.SetValueEx(key,cls.APP_REG_NAME,0,winreg.REG_SZ,command)
                else:
                    try:
                        winreg.DeleteValue(key,cls.APP_REG_NAME)
                    except FileNotFoundError:
                        pass
            return True
        except OSError:
            return False

    @classmethod
    def is_run_at_startup_enabled(cls) -> bool:
        if sys.platform != 'win32':
            return False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,cls.REG_KEY_PATH,0,winreg.KEY_READ) as key:
                value,_ = winreg.QueryValueEx(key,cls.APP_REG_NAME)
                return bool(value)
        except OSError:
            return False

    @staticmethod
    def test_remote_proxy_e2e(ssh_bin, config, timeout_sec=None, stop_event=None, target='Cloudflare') -> Tuple[bool,str]:
        if target not in TEST_TARGETS:
            return False,'测试目标无效'
        try:
            cmd = SystemHelper.ssh_base_command(ssh_bin,config)
        except ValueError as exc:
            return False,f'配置无效：{exc}'
        remote_cmd = (
            f"curl -q --silent --show-error --output /dev/null --noproxy '' "
            f"--connect-timeout 4 --max-time 8 --proxy http://127.0.0.1:{config.remote_port} "
            f"--write-out '__CT_HTTP__:%{{http_code}}' {TEST_TARGETS[target]}"
        )
        cmd.extend(['--',f'{config.username}@{config.server_host}',remote_cmd])
        timeout = timeout_sec if timeout_sec is not None else config.connect_timeout + 12
        started = time.monotonic()
        proc = None
        scope = None
        readers = []
        captured = {'out': [], 'err': []}
        overflow = threading.Event()
        try:
            proc = subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                    text=True,encoding='utf-8',errors='replace',creationflags=SystemHelper.creationflags())
            scope = ProcessScope(proc)
            def read_stream(stream, key):
                size = 0
                try:
                    while True:
                        chunk = stream.read(1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > 65536:
                            overflow.set()
                            break
                        captured[key].append(chunk)
                except (OSError, ValueError):
                    pass
            for key,stream in [('out',proc.stdout),('err',proc.stderr)]:
                thread = threading.Thread(target=read_stream,args=(stream,key),daemon=True)
                thread.start()
                readers.append(thread)
            while True:
                if stop_event is not None and stop_event.is_set():
                    return False,'验证已取消'
                if time.monotonic()-started >= timeout:
                    return False,'验证超时：请检查远程 curl、代理规则或网络'
                if overflow.is_set():
                    return False,'验证输出超过安全上限，已终止检测'
                if proc.poll() is not None:
                    break
                if stop_event is not None:
                    stop_event.wait(0.1)
                else:
                    time.sleep(0.1)
            for thread in readers:
                thread.join(timeout=2)
            if overflow.is_set():
                return False,'验证输出超过安全上限，已终止检测'
            out,err = ''.join(captured['out']),''.join(captured['err'])
            match = re.search(r'__CT_HTTP__:(\d{3})\s*$',out)
            code = int(match.group(1)) if match else 0
            if proc.returncode == 0 and 200 <= code < 500 and code != 407:
                elapsed = int((time.monotonic()-started)*1000)
                suffix = '（目标已响应，访问该 API 仍可能需要凭据）' if code in (401,403) else ''
                return True,f'{target} 可达 · HTTP {code} · {elapsed} ms{suffix}'
            detail = err.strip() or (f'HTTP {code}' if match else '未收到目标 HTTP 状态码')
            return False,f'验证失败（退出码 {proc.returncode}）：{detail[:350]}'
        except OSError as exc:
            return False,f'验证执行失败：{exc}'
        finally:
            if scope:
                scope.close()
            if proc is not None:
                SystemHelper.terminate_process(proc)
                for thread in readers:
                    thread.join(timeout=2)
                for stream in (proc.stdout,proc.stderr):
                    if stream:
                        stream.close()

    @staticmethod
    def mask_sensitive_text(text: str, config=None) -> str:
        if config is not None:
            for value,replacement in [(config.key_file_path,'[私钥路径]'),(config.server_host,'[服务器]'),(config.username,'[用户]')]:
                if value:
                    text = text.replace(value,replacement)
        home = str(Path.home())
        text = text.replace(home,'[用户目录]').replace(home.replace('\\','/'),'[用户目录]')
        text = re.sub(r'\b(?:\d{1,3}\.){3}\d{1,3}\b','[IP]',text)
        text = re.sub(r'(?<!\w)(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]+','[IPv6]',text)
        return text
