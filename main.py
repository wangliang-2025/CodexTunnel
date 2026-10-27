"""Application entry and typed Win32 single-instance protection."""
import ctypes
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))

def ensure_single_instance():
    if sys.platform!='win32': return None
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[wintypes.LPVOID,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.CloseHandle.restype=wintypes.BOOL
    handle=kernel.CreateMutexW(None,False,f"Local\\CodexTunnel_{os.getenv('USERNAME','user')}_SingleInstance_Mutex")
    error=ctypes.get_last_error()
    if not handle: raise ctypes.WinError(error)
    if error==183:
        kernel.CloseHandle(handle)
        import tkinter as tk
        from tkinter import messagebox
        # A packaged runtime can occasionally lack Tcl/Tk resources. The
        # mutex result is still authoritative; failing to show the hint must
        # not turn a normal second-launch check into a crash.
        try:
            root=tk.Tk(); root.withdraw()
            messagebox.showinfo('已在运行','CodexTunnel 已经运行，请从右下角托盘打开窗口。',parent=root)
            root.destroy()
        except tk.TclError:
            pass
        return False
    return handle

def main():
    # PyInstaller's bundled Tcl/Tk hook can be overridden by a managed
    # Python runtime during startup; set the locations again immediately
    # before importing or constructing the GUI.
    if getattr(sys, 'frozen', False):
        bundle=Path(getattr(sys, '_MEIPASS', Path(sys.executable).parent))
        os.environ['PATH']=str(bundle)+os.pathsep+os.environ.get('PATH','')
        os.environ['TCL_LIBRARY']=str(bundle/'_tcl_data')
        os.environ['TK_LIBRARY']=str(bundle/'_tk_data')
    if '--smoke-test-report' in sys.argv:
        index=sys.argv.index('--smoke-test-report')
        if index+1>=len(sys.argv):
            raise ValueError('Missing smoke-test report path')
        # Build QA uses isolated state and never starts a tunnel or modifies
        # the current user's saved configuration/startup setting.
        report_path=Path(sys.argv[index+1]).resolve()
        from src.ui_app import App
        from src.resources import asset_path
        from src import __version__
        errors=[]
        tray_state={'visible':False}
        with tempfile.TemporaryDirectory() as folder:
            app=App(config_path=str(Path(folder)/'config.json'),enable_tray=True)
            app.report_callback_exception=lambda kind,value,tb:errors.append(str(value))
            def finish_smoke():
                tray_state['visible']=bool(app.tray.tray_icon and app.tray.tray_icon.visible)
                app._exit()
            app.after(1800,finish_smoke)
            app.mainloop()
            report={'version':__version__,'frozen':bool(getattr(sys,'frozen',False)),
                    'icon':asset_path('app_icon.ico').exists(),'toast_resource':asset_path('assets/toast.ps1').exists(),'key_acl_resource':asset_path('assets/key_acl.ps1').exists(),'icon_image':asset_path('assets/tunnel-icon.png').exists(),
                    'tray_initialized':app.tray_available,'tray_visible':tray_state['visible'],
                    'callback_errors':errors,'network_connections':0}
            report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if errors: raise SystemExit(1)
        return
    mutex=ensure_single_instance()
    if mutex is False: return
    try:
        from src.ui_app import App
        app=App(); app.mainloop()
    finally:
        if mutex:
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]; kernel.CloseHandle.restype=wintypes.BOOL
            kernel.CloseHandle(mutex)

if __name__=='__main__': main()
