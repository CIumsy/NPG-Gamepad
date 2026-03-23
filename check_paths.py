from PySide6.QtSvg import QSvgRenderer
r = QSvgRenderer('NPG SNES with Logo.svg')
for n in ['path1', 'path2', 'path3']:
    b = r.boundsOnElement(n)
    print(f'{n}: x={b.x():.1f}, y={b.y():.1f}, w={b.width():.1f}, h={b.height():.1f}')
