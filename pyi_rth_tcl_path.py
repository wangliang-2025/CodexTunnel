"""Point Tkinter at the bundled Tcl/Tk data in a frozen Windows app."""
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    tcl_dir = root / "_tcl_data"
    tk_dir = root / "_tk_data"
    os.environ["PATH"] = str(root) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):
        _dll_dir_handle = os.add_dll_directory(str(root))
    os.environ["TCL_LIBRARY"] = str(tcl_dir)
    os.environ["TK_LIBRARY"] = str(tk_dir)
