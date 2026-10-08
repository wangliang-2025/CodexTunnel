"""Resource lookup for source and PyInstaller bundles."""
import sys
from pathlib import Path


def asset_path(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return root / name
