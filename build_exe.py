"""Reproducible Windows release build with tests, previous binary backup and hash."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from src import __version__

ROOT=Path(__file__).resolve().parent

def build():
    parser=argparse.ArgumentParser()
    parser.add_argument('--copy-desktop',action='store_true',help='copy a versioned binary to Desktop')
    args=parser.parse_args()
    subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-p','test_*.py','-v'],cwd=ROOT,check=True)
    subprocess.run([sys.executable,'create_icon.py'],cwd=ROOT,check=True)
    previous=ROOT/'dist'/'CodexTunnel.exe'
    releases=ROOT/'releases'; releases.mkdir(exist_ok=True)
    if previous.exists():
        digest=hashlib.sha256(previous.read_bytes()).hexdigest()[:12]
        backup=releases/f'CodexTunnel-previous-{digest}.exe'
        if not backup.exists(): shutil.copy2(previous,backup)
    staging=ROOT/'dist'/'.build-output'/__version__
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--distpath',str(staging),'CodexTunnel.spec'],cwd=ROOT,check=True)
    binary=staging/'CodexTunnel.exe'
    versioned=ROOT/'dist'/f'CodexTunnel-v{__version__}.exe'
    shutil.copy2(binary,versioned)
    canonical_updated=False
    candidate=previous.with_suffix('.exe.pending')
    try:
        shutil.copy2(binary,candidate)
        candidate.replace(previous)
        canonical_updated=True
    except PermissionError:
        print('Existing CodexTunnel.exe is in use; preserved it. Use the new versioned executable.')
    finally:
        candidate.unlink(missing_ok=True)
    digest=hashlib.sha256(binary.read_bytes()).hexdigest()
    checksums=f'{digest}  {versioned.name}\n'
    if previous.exists(): checksums+=f'{hashlib.sha256(previous.read_bytes()).hexdigest()}  CodexTunnel.exe\n'
    (ROOT/'dist'/'SHA256SUMS.txt').write_text(checksums,encoding='utf-8')
    metadata={'version':__version__,'executable':versioned.name,'canonical_updated':canonical_updated,'built_at':datetime.now().astimezone().isoformat(),'sha256':digest,'bytes':binary.stat().st_size,'python':sys.version.split()[0]}
    (ROOT/'dist'/'release.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.copy_desktop:
        desktop=Path.home()/'Desktop'
        if not desktop.is_dir(): raise FileNotFoundError('Desktop folder not found; use dist output')
        shutil.copy2(versioned,desktop/versioned.name)
    print(f'Build complete: {versioned} ({binary.stat().st_size/1048576:.2f} MiB)')
    print(f'SHA256: {digest}')

if __name__=='__main__': build()
