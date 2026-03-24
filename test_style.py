import xml.etree.ElementTree as ET
root = ET.parse('NPG-Controller.ui').getroot()
for w in root.findall('.//widget'):
    name = w.attrib.get('name', '')
    if name.startswith('grpCh'):
        prop = w.find('.//property[@name="styleSheet"]/string')
        if prop is not None:
            print(f'=== {name} ===\n{prop.text}')
            break
