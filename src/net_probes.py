"""Bounded network probes and evidence-based fault messages."""
import socket
import queue
import threading
import time
from typing import Optional, Tuple

class NetworkProbes:
    @staticmethod
    def probe_local_proxy(host='127.0.0.1',port=7897,timeout=1.0) -> Tuple[bool,str]:
        try:
            with socket.create_connection((host,port),timeout=timeout):
                return True,f'本地 TCP 端口 {port} 可连接（代理协议需端到端验证）'
        except OSError as exc:
            return False,f'本地代理端口 {port} 不可连接。请开启代理并检查混合端口。{exc}'

    @staticmethod
    def probe_remote_server(host,port=22,timeout=2.0,stop_event=None) -> Tuple[bool,str]:
        # socket.create_connection's timeout does not bound getaddrinfo.
        # Keep DNS outside the tunnel worker so cancel/exit remain bounded.
        result = queue.Queue(maxsize=1)
        def resolve():
            try:
                result.put((socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)[:16],None))
            except OSError as exc:
                result.put((None,exc))
        threading.Thread(target=resolve,daemon=True,name='DNSProbe').start()
        deadline = time.monotonic()+timeout
        last_error = '探测超时'
        try:
            while time.monotonic()<deadline:
                if stop_event is not None and stop_event.is_set():
                    return False,'服务器探测已取消'
                try:
                    addresses,error = result.get(timeout=min(0.1,max(0.001,deadline-time.monotonic())))
                    break
                except queue.Empty:
                    continue
            else:
                return False,'服务器域名解析超时；请检查 DNS 或使用实际 IP'
            if error:
                raise error
            for family,socktype,proto,_,address in addresses:
                if stop_event is not None and stop_event.is_set():
                    return False,'服务器探测已取消'
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    break
                try:
                    with socket.socket(family,socktype,proto) as connection:
                        connection.settimeout(min(0.5,remaining))
                        connection.connect(address)
                        return True,f'服务器 TCP 端口 {port} 可达（尚未验证 SSH 认证）'
                except OSError as exc:
                    last_error=str(exc)
            return False,f'服务器 SSH 端口不可达：{last_error}。请检查 VPN、地址和防火墙。'
        except OSError as exc:
            return False,f'服务器 SSH 端口不可达：{exc}。请检查 VPN、内网路由、SSH 端口和防火墙；TUN 绕过规则需按实际网络配置。'

    @staticmethod
    def diagnose_error(stderr_text,remote_port,host,user) -> Optional[dict]:
        text = stderr_text.lower()
        def diag(title,reason,solution,fatal=False):
            return dict(title=title,level='error',reason=reason,solution=solution,action_command=None,fatal=fatal)
        if 'host key verification failed' in text or 'remote host identification has changed' in text:
            return diag('主机指纹未通过校验','服务器指纹未知或与已保存记录不一致。',
                        '请先向管理员核对指纹，再使用终端完成首次登录。指纹发生变化时先确认服务器身份；本软件不会删除 known_hosts 记录。',True)
        if 'permission denied' in text or 'no supported authentication' in text or 'sign_and_send_pubkey' in text:
            return diag('SSH 认证失败','公钥登录失败，或加密私钥尚未解锁。',
                        '请确认用户名和公钥授权；有口令的私钥须先加入 ssh-agent。程序采用非交互认证，不保存密码或私钥内容。',True)
        if 'address already in use' in text or 'could not request local forwarding' in text or 'remote port forwarding failed' in text or 'cannot listen to port' in text or 'administratively prohibited' in text:
            return diag('端口转发被拒绝',f'无法建立远程端口 {remote_port} 的监听。',
                        f'可能是端口占用，或 sshd 的 AllowTcpForwarding / PermitListen 策略限制。先用 ss -ltnp 查看端口；确认归属后再处理，或更换远程端口。',True)
        if 'bad configuration' in text or 'unknown option' in text or 'unsupported option' in text:
            return diag('SSH 客户端不兼容','SSH 不支持当前安全参数。','请使用较新的 Windows OpenSSH 客户端。',True)
        if 'timed out' in text or 'connection refused' in text or 'no route to host' in text or 'could not resolve' in text:
            return diag('网络连接失败','服务器网络或 SSH 服务当前不可达。','检查服务器地址、VPN、DNS、SSH 服务和代理 TUN 路由。')
        return None
