"""Bounded network probes and evidence-based fault messages."""
import socket
import queue
import re
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
        # OpenSSH emits several different messages for a failed -R request.
        # Keeping the classification here makes the GUI useful without trying
        # to "fix" a server that the client cannot safely administer.
        raw_lines = [line.strip() for line in (stderr_text or '').splitlines() if line.strip()]
        raw = '\n'.join(raw_lines)
        text = raw.lower()
        def evidence(*needles):
            for line in raw_lines:
                lowered = line.lower()
                if any(needle in lowered for needle in needles):
                    return line[:320]
            return None
        def diag(title,reason,solution,fatal=False,kind='unknown',needles=()):
            item = dict(title=title,level='error',reason=reason,solution=solution,
                        action_command=None,fatal=fatal,kind=kind)
            detail = evidence(*needles) if needles else None
            if detail:
                item['evidence'] = detail
            return item
        if 'host key verification failed' in text or 'remote host identification has changed' in text:
            return diag('主机指纹未通过校验','服务器指纹未知或与已保存记录不一致。',
                        '请先向管理员核对指纹，再使用终端完成首次登录。指纹发生变化时先确认服务器身份；本软件不会删除 known_hosts 记录。',True,'host_key',('host key','remote host identification'))
        if 'permission denied' in text or 'no supported authentication' in text or 'sign_and_send_pubkey' in text:
            return diag('SSH 认证失败','公钥登录失败，或加密私钥尚未解锁。',
                        '请确认用户名和公钥授权；有口令的私钥须先加入 ssh-agent。程序采用非交互认证，不保存密码或私钥内容。',True,'authentication',('permission denied','supported authentication','sign_and_send_pubkey'))
        policy_markers = ('administratively prohibited','no-port-forwarding','disableforwarding',
                          'allowtcpforwarding','permitlisten')
        port_match = re.search(r'(?:remote port forwarding failed for listen port|cannot listen to port)\s*:?\s*(\d+)',text)
        if 'address already in use' in text and not any(marker in text for marker in policy_markers):
            detected_port = int(port_match.group(1)) if port_match else remote_port
            return diag('远程端口已被占用',f'服务器上的远程端口 {detected_port} 无法监听，通常已有进程占用该端口。',
                        f"先在服务器执行 ss -ltnp 'sport = :{detected_port}' 确认进程归属；确认后可停止对应服务，或把本软件的远程端口改为未占用端口。不要直接执行 fuser -k。",True,'remote_port_in_use',('address already in use','remote port forwarding failed','cannot listen to port'))
        if (any(marker in text for marker in policy_markers) or
                'could not request local forwarding' in text):
            return diag('服务器策略禁止端口转发',f'sshd 或当前公钥策略拒绝建立远程端口 {remote_port}。',
                        '请管理员检查 sshd -T 的 AllowTcpForwarding、DisableForwarding 和 PermitListen，以及 authorized_keys 中的 no-port-forwarding / restrict 限制；只放行所需端口，不要删除全部安全限制。',True,'forwarding_policy',('administratively prohibited','no-port-forwarding','disableforwarding','allowtcpforwarding','permitlisten','could not request local forwarding'))
        if 'remote port forwarding failed' in text or 'cannot listen to port' in text:
            detected_port = int(port_match.group(1)) if port_match else remote_port
            return diag('远程端口监听失败',f'服务器没有建立远程端口 {detected_port} 的监听。',
                        f"先执行 ss -ltnp 'sport = :{detected_port}' 检查端口占用，再让管理员检查 AllowTcpForwarding、PermitListen 和 authorized_keys 限制；客户端不会自动改服务器配置。",True,'remote_forward_failed',('remote port forwarding failed','cannot listen to port'))
        if ('cannot assign requested address' in text or 'unknown port' in text or
                'invalid argument' in text or 'bad address' in text):
            return diag('远程监听地址不被允许',f'服务器不接受对 127.0.0.1:{remote_port} 的监听请求。',
                        '请检查 sshd 的 PermitListen、账户限制和服务器网络命名空间；保持回环监听可降低暴露面，不要为了绕过限制直接改成 0.0.0.0。',True,'listen_address',('cannot assign requested address','unknown port','invalid argument','bad address'))
        if 'bad configuration' in text or 'unknown option' in text or 'unsupported option' in text:
            return diag('SSH 客户端不兼容','SSH 不支持当前安全参数。','请使用较新的 Windows OpenSSH 客户端。',True,'client_compatibility',('bad configuration','unknown option','unsupported option'))
        if 'timed out' in text or 'connection refused' in text or 'no route to host' in text or 'could not resolve' in text:
            return diag('网络连接失败','服务器网络或 SSH 服务当前不可达。','检查服务器地址、VPN、DNS、SSH 服务和代理 TUN 路由。',False,'network',('timed out','connection refused','no route to host','could not resolve'))
        return None
