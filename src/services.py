"""Bounded command execution, read-only metrics and local proxy integration."""
import base64
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from src.process_scope import ProcessScope
from src.system_helper import SystemHelper, TEST_TARGETS
from src.ssh_finder import SSHFinder

class CommandRunner:
    @staticmethod
    def run(command, stop=None, timeout=25, limit=262144):
        if stop and stop.is_set(): raise RuntimeError('操作已取消')
        proc=None; scope=None; readers=[]; chunks={'out':[], 'err':[]}; overflow=threading.Event()
        try:
            environment=None
            if Path(command[0]).name.lower()=='powershell.exe':
                environment=dict(os.environ)
                environment['PSModulePath']=str(Path(os.getenv('SystemRoot','C:/Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'Modules')
            proc=subprocess.Popen(command,env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=SystemHelper.creationflags())
            scope=ProcessScope(proc)
            def read(stream,key):
                count=0
                try:
                    while True:
                        data=stream.read(4096)
                        if not data: break
                        count+=len(data)
                        if count>limit: overflow.set(); break
                        chunks[key].append(data)
                except (OSError,ValueError): pass
            for key,stream in [('out',proc.stdout),('err',proc.stderr)]:
                thread=threading.Thread(target=read,args=(stream,key),daemon=True); thread.start(); readers.append(thread)
            deadline=time.monotonic()+timeout
            while proc.poll() is None:
                if stop and stop.is_set(): raise RuntimeError('操作已取消')
                if overflow.is_set(): raise RuntimeError('命令输出超过安全上限')
                if time.monotonic()>deadline: raise RuntimeError('操作超时')
                if stop: stop.wait(.05)
                else: time.sleep(.05)
            for thread in readers: thread.join(2)
            if overflow.is_set(): raise RuntimeError('命令输出超过安全上限')
            return proc.returncode,b''.join(chunks['out']).decode('utf-8','replace'),b''.join(chunks['err']).decode('utf-8','replace')
        finally:
            if scope: scope.close()
            if proc:
                SystemHelper.terminate_process(proc)
                for thread in readers: thread.join(2)
                for stream in (proc.stdout,proc.stderr):
                    if stream: stream.close()

    @staticmethod
    def remote(config, command, stop=None, timeout=25):
        ssh,msg=SSHFinder.find_ssh()
        if not ssh: raise RuntimeError(msg)
        args=SystemHelper.ssh_base_command(ssh,config)
        args.extend(['--',f'{config.username}@{config.server_host}',command])
        return CommandRunner.run(args,stop,timeout)

    @staticmethod
    def remote_python(config, script, stop=None, timeout=25):
        encoded=base64.b64encode(script.encode()).decode('ascii')
        command='python3 -c '+shlex.quote("import base64;exec(base64.b64decode('"+encoded+"'))")
        code,out,err=CommandRunner.remote(config,command,stop,timeout)
        if code: raise RuntimeError('远端只读采样失败；需要 Python 3 和命令执行权限。'+err[:200])
        data=json.loads(out)
        if not isinstance(data,dict): raise ValueError('远端返回格式无效')
        return data

class MetricsService:
    @staticmethod
    def latency(config, target='Cloudflare', stop=None, remote=True):
        if target not in TEST_TARGETS: raise ValueError('测试目标无效')
        marker='__CT_METRICS__:%{http_code}:%{time_namelookup}:%{time_connect}:%{time_appconnect}:%{time_starttransfer}:%{time_total}'
        args=['curl','-q','--silent','--show-error','--output','/dev/null' if remote else 'NUL','--noproxy','','--connect-timeout','4','--max-time','8','--max-filesize','65536','--proxy',f'http://127.0.0.1:{config.remote_port if remote else config.local_port}','--write-out',marker,TEST_TARGETS[target]]
        if remote: code,out,err=CommandRunner.remote(config,shlex.join(args),stop,config.connect_timeout+12)
        else:
            binary=shutil.which('curl.exe') or shutil.which('curl')
            if not binary: raise RuntimeError('找不到 curl')
            args[0]=binary; code,out,err=CommandRunner.run(args,stop,12)
        match=re.search(r'__CT_METRICS__:(\d{3}):([\d.]+):([\d.]+):([\d.]+):([\d.]+):([\d.]+)\s*$',out)
        if not match or code: raise RuntimeError('测速失败：'+(err[:250] or f'退出码 {code}'))
        http=int(match.group(1)); numbers=[float(x)*1000 for x in match.groups()[1:]]
        if any(not math.isfinite(x) or x<0 or x>120000 for x in numbers): raise ValueError('计时样本无效')
        dns,connect,tls,first,total=numbers
        return {'ok':200<=http<500 and http!=407,'http':http,'dns_ms':dns,'tcp_ms':max(0,connect-dns),'tls_ms':max(0,tls-connect),'ttfb_ms':first,'total_ms':total,'target':target,'path':'远端 → SSH 隧道 → 本地 HTTP 代理' if remote else '本机 → 本地 HTTP 代理','sampled_at':time.time()}

    @staticmethod
    def remote_listener(config, ports, stop=None):
        checked=[p for p in ports if type(p) is int and 1<=p<=65535]
        if len(checked)!=len(ports): raise ValueError('监听探针端口无效')
        script="import socket,json\nr={}\nfor p in "+repr(checked)+":\n try:\n  s=socket.create_connection(('127.0.0.1',p),timeout=2);s.close();r[str(p)]=True\n except OSError:r[str(p)]=False\nprint(json.dumps(r))"
        return CommandRunner.remote_python(config,script,stop,config.connect_timeout+min(12,len(ports)*2+3))

    @staticmethod
    def dashboard(config,stop=None):
        script='''import json,os,shutil,time
from pathlib import Path
mem={}
for line in Path('/proc/meminfo').read_text().splitlines():
 k,v=line.split(':',1);mem[k]=int(v.strip().split()[0])*1024
cpu=[int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
rx=tx=0
for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
 name,values=line.split(':',1)
 if name.strip()=='lo':continue
 values=values.split();rx+=int(values[0]);tx+=int(values[8])
disk=shutil.disk_usage('/')
print(json.dumps({'time':time.time(),'uptime':float(Path('/proc/uptime').read_text().split()[0]),'load':os.getloadavg()[0],'cpu_total':sum(cpu),'cpu_idle':cpu[3]+cpu[4],'memory_total':mem['MemTotal'],'memory_used':mem['MemTotal']-mem.get('MemAvailable',mem.get('MemFree',0)),'disk_total':disk.total,'disk_used':disk.used,'network_rx':rx,'network_tx':tx}))'''
        data=CommandRunner.remote_python(config,script,stop)
        required=['time','uptime','load','cpu_total','cpu_idle','memory_total','memory_used','disk_total','disk_used','network_rx','network_tx']
        if set(data)!=set(required) or any(type(data[k]) not in (int,float) or not math.isfinite(data[k]) or data[k]<0 for k in required): raise ValueError('远端指标无效')
        if data['memory_total']<=0 or data['disk_total']<=0 or data['cpu_idle']>data['cpu_total'] or data['memory_used']>data['memory_total'] or data['disk_used']>data['disk_total']: raise ValueError('远端指标范围无效')
        return data

    @staticmethod
    def speed(config,stop=None,remote=False):
        # Fixed trusted HTTPS endpoint, 1 MiB maximum application read; no response is persisted.
        script='''import urllib.request,json,time
limit=1048576
opener=urllib.request.build_opener(urllib.request.ProxyHandler({'http':'http://127.0.0.1:PORT','https':'http://127.0.0.1:PORT'}))
start=time.monotonic();count=0
with opener.open('https://speed.cloudflare.com/__down?bytes=1048576',timeout=4) as response:
 if response.status!=200:raise RuntimeError('HTTP status')
 while count<limit and time.monotonic()-start<15:
  data=response.read(min(16384,limit-count))
  if not data:break
  count+=len(data)
elapsed=max(.001,time.monotonic()-start)
if count!=limit:raise RuntimeError('下载样本不完整或超时')
print(json.dumps({'bytes':count,'seconds':elapsed,'bytes_per_second':count/max(.001,elapsed),'limit':limit}))'''.replace('PORT',str(config.remote_port if remote else config.local_port))
        if remote: result=CommandRunner.remote_python(config,script,stop,config.connect_timeout+20)
        else:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({'http':f'http://127.0.0.1:{config.local_port}','https':f'http://127.0.0.1:{config.local_port}'}))
            start=time.monotonic(); count=0; limit=1048576
            with opener.open('https://speed.cloudflare.com/__down?bytes=1048576',timeout=4) as response:
                if response.status!=200: raise RuntimeError('下载目标响应无效')
                while count<limit:
                    if stop and stop.is_set(): raise RuntimeError('操作已取消')
                    if time.monotonic()-start>15: raise RuntimeError('下载超时')
                    chunk=response.read(min(16384,limit-count))
                    if not chunk: break
                    count+=len(chunk)
            if count!=limit: raise RuntimeError('下载样本不完整')
            elapsed=max(.001,time.monotonic()-start)
            result={'bytes':count,'seconds':elapsed,'bytes_per_second':count/max(.001,elapsed),'limit':limit}
        if set(result)!={'bytes','seconds','bytes_per_second','limit'} or result['bytes']!=1048576 or result['limit']!=1048576 or not 0<result['seconds']<30 or type(result['bytes_per_second']) not in (int,float) or not math.isfinite(result['bytes_per_second']) or result['bytes_per_second']<=0: raise ValueError('测速样本无效')
        result['path']='远端 → 隧道 → 本地代理' if remote else '本机 → 本地代理'
        return result

class ClashClient:
    def __init__(self,port=9090,token=''):
        if type(port) is not int or not 1<=port<=65535: raise ValueError('控制端口无效')
        if not isinstance(token,str) or len(token)>4096 or any(ord(c)<32 for c in token): raise ValueError('控制器密钥无效')
        self.port=port; self.token=token
    def get(self,path):
        if path not in ('/configs','/version','/traffic'): raise ValueError('只允许只读控制接口')
        headers={'Authorization':'Bearer '+self.token} if self.token else {}
        request=urllib.request.Request(f'http://127.0.0.1:{self.port}'+path,headers=headers)
        # Never follow a local controller redirect to another host with credentials.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs): return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=2) as response:
            raw=response.readline(8193) if path=='/traffic' else response.read(65537)
        if len(raw)>(8192 if path=='/traffic' else 65536): raise ValueError('控制器响应过大')
        data=json.loads(raw)
        if not isinstance(data,dict): raise ValueError('控制器响应格式无效')
        return data
    @staticmethod
    def http_proxy(port):
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.4) as stream:
                stream.settimeout(.8)
                stream.sendall(b'CONNECT www.cloudflare.com:443 HTTP/1.1\r\nHost: www.cloudflare.com:443\r\nConnection: close\r\n\r\n')
                first=stream.recv(1024).split(b'\r\n',1)[0]
                return bool(re.match(rb'HTTP/1\.[01] 200(?:\s|$)',first))
        except OSError: return False
    def detect(self,stop=None):
        candidates={7890,7897,10809,8080}; identified=False
        try:
            config=self.get('/configs'); version=self.get('/version')
            identified=bool(version.get('version'))
            for key in ('mixed-port','port'):
                value=config.get(key)
                if type(value) is int and 1<=value<=65535: candidates.add(value)
        except (OSError,ValueError): pass
        found=[]
        for port in sorted(candidates):
            if stop and stop.is_set(): break
            if self.http_proxy(port): found.append({'port':port,'protocol':'HTTP CONNECT 已验证','source':'Mihomo API 与协议验证' if identified and port in (config.get('port'),config.get('mixed-port')) else '本机 HTTP 代理候选，品牌未确认'})
        return found
    def traffic(self):
        data=self.get('/traffic'); up=data.get('up'); down=data.get('down')
        if any(type(x) not in (int,float) or not math.isfinite(x) or x<0 for x in (up,down)): raise ValueError('流量样本无效')
        return {'up':up,'down':down,'sampled_at':time.time(),'source':'Mihomo 核心总流量（包含其他应用，不等于单隧道流量）'}
