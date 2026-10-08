import os
import shutil
from typing import Tuple, Optional

class SSHFinder:
    """Windows OpenSSH 路径检索器与环境诊断"""
    
    @staticmethod
    def find_ssh() -> Tuple[Optional[str], str]:
        """
        检索系统中的 ssh.exe 可执行文件路径。
        返回: (ssh_path, diagnostic_message)
        """
        system_root = os.getenv("SystemRoot", r"C:\Windows")
        
        candidates = [
            # 1. 优先 64 位原生路径
            os.path.join(system_root, "System32", "OpenSSH", "ssh.exe"),
            # 2. 若当前为 32 位环境，穿透 WOW64 重定向访问原生 System32
            os.path.join(system_root, "Sysnative", "OpenSSH", "ssh.exe"),
            # 3. PATH 环境变量中由 which 找到的路径
            shutil.which("ssh"),
            shutil.which("ssh.exe"),
            # 4. Git 自带的 OpenSSH 客户端
            r"C:\Program Files\Git\usr\bin\ssh.exe",
            r"C:\Program Files (x86)\Git\usr\bin\ssh.exe",
            os.path.expanduser(r"~\AppData\Local\Programs\Git\usr\bin\ssh.exe")
        ]
        
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                abs_path = os.path.abspath(candidate)
                return abs_path, f"已找到 OpenSSH 客户端: {abs_path}"
                
        # 未找到时给出精准安装指引
        fail_msg = (
            "未检测到 OpenSSH 客户端 (ssh.exe)！\n"
            "安装指引：\n"
            "1. 按 Win+I 打开 Windows 设置 -> 应用 -> 可选功能；\n"
            "2. 点击【添加可选功能】，搜索并勾选【OpenSSH 客户端】后安装；\n"
            "3. 或者安装 Git for Windows 也会自带 SSH。"
        )
        return None, fail_msg
