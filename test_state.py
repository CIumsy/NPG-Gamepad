
from PySide6.QtWidgets import QApplication, QGroupBox
app=QApplication([])
g=QGroupBox('Test')
g.setStyleSheet('''
    QGroupBox::title { color: white; border: 1px solid white; }
    QGroupBox::title:disabled { color: red; border: 1px solid red; }
    QGroupBox::title:!checked { color: blue; border: 1px solid blue; }
''')
g.setCheckable(True)
g.show()
app.exec()

