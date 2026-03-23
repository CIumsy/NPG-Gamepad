
import sys
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QImage, QColor
from PySide6.QtSvg import QSvgRenderer

app = QApplication(sys.argv)
class T(QWidget):
    def paintEvent(self, e):
        p = QPainter(self)
        r = QSvgRenderer('NPG SNES with Logo.svg')
        r_rect = QRectF(0, 0, 540, 250)
        
        img = QImage(540, 250, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        tmp_p = QPainter(img)
        
        # Helper string eval to map rect
        def wr(eid):
            vw = r.viewBoxF().width()
            vh = r.viewBoxF().height()
            sx = 540 / vw; sy = 250 / vh
            rect = r.matrixForElement(eid).mapRect(r.boundsOnElement(eid))
            return QRectF(rect.x() * sx, rect.y() * sy, rect.width() * sx, rect.height() * sy)
            
        r.render(tmp_p, 'path1', wr('path1'))
        r.render(tmp_p, 'path2', wr('path2'))
        
        tmp_p.setCompositionMode(QPainter.CompositionMode_SourceIn)
        tmp_p.fillRect(img.rect(), QColor('#555555'))
        
        tmp_p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
        r.render(tmp_p, 'path3', wr('path3'))
        tmp_p.end()
        
        r.render(p, r_rect)
        p.drawImage(0, 0, img)

