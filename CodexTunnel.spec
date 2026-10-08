# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files
root=Path(SPECPATH)
a=Analysis(
 [str(root/'main.py')],pathex=[str(root)],
 binaries=[],datas=collect_data_files('customtkinter')+[(str(root/'app_icon.ico'),'.'),(str(root/'assets'/'tunnel-icon.png'),'assets'),(str(root/'assets'/'toast.ps1'),'assets'),(str(root/'assets'/'key_acl.ps1'),'assets')],
 hiddenimports=['pystray._win32'],hookspath=[],hooksconfig={},runtime_hooks=[],
 excludes=['numpy','matplotlib','pandas','scipy','pytest','IPython'],noarchive=False,optimize=0,
)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='CodexTunnel',debug=False,
 bootloader_ignore_signals=False,strip=False,upx=False,console=False,
 disable_windowed_traceback=False,icon=str(root/'app_icon.ico'),version=str(root/'version_info.txt'),
)
