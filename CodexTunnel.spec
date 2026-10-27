# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, tcl_tk
root=Path(SPECPATH)

# Some embedded Python distributions ship Tcl/Tk correctly but do not expose
# the metadata PyInstaller's tcl_tk hook expects.  Discover the runtime from
# the interpreter used for the build so the frozen GUI remains self-contained.
python_root=Path(sys.base_prefix)
dll_root=python_root/'DLLs'
tcl_root=python_root/'tcl'
bundled_tk=root/'artifacts'/'tk-runtime'
if not bundled_tk.exists(): bundled_tk=None
# The managed Python runtime used for release builds may not expose Tcl/Tk
# metadata even though the DLLs and data directories are present.  Override
# the hook's probe with the paths discovered above so tkinter is retained.
tk_info=tcl_tk.tcltk_info
if bundled_tk is not None or ((tcl_root/'tcl8.6'/'_tcl_data').exists() and (tcl_root/'tk8.6'/'_tk_data').exists()):
    tk_info.available=True
    tk_info.tcl_data_missing=False
    tk_info.tk_data_missing=False
    tk_info.tcl_data_dir=str((bundled_tk or tcl_root/'tcl8.6')/'_tcl_data')
    tk_info.tk_data_dir=str((bundled_tk or tcl_root/'tk8.6')/'_tk_data')
    tk_info.tcl_module_dir=str(bundled_tk or dll_root)
    tk_info.data_files=[]
tk_binaries=[]
for name in ('_tkinter.pyd','tcl86t.dll','tk86t.dll','zlib1.dll','VCRUNTIME140.dll','VCRUNTIME140_1.dll'):
    # The managed runtime's Tcl DLLs must stay paired with its Python build;
    # only use a pre-extracted _tkinter extension when that runtime provides
    # one (some cached distributions ship an incompatible extension).
    candidate=(bundled_tk/name if name == '_tkinter.pyd' and bundled_tk is not None else dll_root/name)
    if candidate.exists(): tk_binaries.append((str(candidate),'.'))
tk_datas=[]
for source_name,target_name in ((('_tcl_data' if bundled_tk else 'tcl8.6/_tcl_data'),'_tcl_data'),(('_tk_data' if bundled_tk else 'tk8.6/_tk_data'),'_tk_data')):
    source=(bundled_tk or tcl_root)/source_name
    if source.exists():
        for path in source.rglob('*'):
            if path.is_file(): tk_datas.append((str(path),str(Path(target_name,*path.relative_to(source).parent.parts))))

a=Analysis(
 [str(root/'main.py')],pathex=[str(root)],
 binaries=tk_binaries,datas=collect_data_files('customtkinter')+[(str(root/'app_icon.ico'),'.'),(str(root/'assets'/'tunnel-icon.png'),'assets'),(str(root/'assets'/'toast.ps1'),'assets'),(str(root/'assets'/'key_acl.ps1'),'assets')]+tk_datas,
 hiddenimports=['pystray._win32']+collect_submodules('tkinter'),hookspath=[],hooksconfig={},runtime_hooks=[str(root/'pyi_rth_tcl_path.py')],
 excludes=['numpy','matplotlib','pandas','scipy','pytest','IPython'],noarchive=False,optimize=0,
)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='CodexTunnel',debug=False,
 bootloader_ignore_signals=False,strip=False,upx=False,console=False,
 disable_windowed_traceback=False,icon=str(root/'app_icon.ico'),version=str(root/'version_info.txt'),
)
