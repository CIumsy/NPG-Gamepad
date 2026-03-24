
from PySide6.QtWidgets import QApplication, QGroupBox
app=QApplication([])
g=QGroupBox('Test')
g.setStyleSheet('''
    QGroupBox::title { color: white; border: 1px solid white; }
    QGroupBox::title:unchecked { color: red; border: 1px solid red; }
''')
g.setCheckable(True)
g.setChecked(False)
g.show()
app.exec()

