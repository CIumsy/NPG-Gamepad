
import xml.etree.ElementTree as ET

t = ET.parse('NPG-Controller.ui')
root = t.getroot()

# 1. Remove checkboxes from their layouts
for layout in root.findall('.//layout'):
    to_remove = []
    for item in layout.findall('item'):
        w = item.find('widget')
        if w is not None and w.attrib.get('name', '').startswith('chkCh'):
            to_remove.append(item)
    for r in to_remove:
        layout.remove(r)

# 2. Make grpCh checkboxes
for w in root.findall('.//widget'):
    if w.attrib.get('class') == 'QGroupBox' and w.attrib.get('name', '').startswith('grpCh'):
        # Add checkable property
        p_checkable = ET.Element('property', name='checkable')
        b_chk = ET.Element('bool')
        b_chk.text = 'true'
        p_checkable.append(b_chk)
        w.insert(0, p_checkable)
        
        # Add checked property
        p_checked = ET.Element('property', name='checked')
        b_chk_d = ET.Element('bool')
        b_chk_d.text = 'true'
        p_checked.append(b_chk_d)
        w.insert(1, p_checked)

t.write('NPG-Controller.ui', encoding='utf-8', xml_declaration=True)
print('UI modified successfully')

