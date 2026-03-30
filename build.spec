import os, glob

# Find vgamepad DLLs
vgamepad_path = os.path.join('.venv', 'Lib', 'site-packages', 'vgamepad', 'win', 'vigem', 'client')
vgamepad_dlls = []
for dll in glob.glob(os.path.join(vgamepad_path, '**', '*.dll'), recursive=True):
    dest = os.path.dirname(dll).replace('.venv\\Lib\\site-packages\\', '')
    vgamepad_dlls.append((dll, dest))

a = Analysis(
    ['main.py'],
    binaries=vgamepad_dlls,
    datas=[
        ('NPG-Controller.ui', '.'),
        ('Controller-Keybinds.ui', '.'),
        ('NPG SNES with Logo.svg', '.'),
        ('icons', 'icons'),
        ('ViGEmBus_1.22.0_x64_x86_arm64.exe', '.'),
    ],
    hiddenimports=['vgamepad', 'bleak', 'PySide6.QtSvg'],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='NPG Controller',
    console=True,
)
