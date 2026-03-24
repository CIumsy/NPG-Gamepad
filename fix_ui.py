import xml.etree.ElementTree as ET

tree = ET.parse('NPG-Controller.ui')
root = tree.getroot()

# Search and update grpNotch
for w in root.findall('.//widget'):
    if w.attrib.get('name') == 'grpNotch':
        prop = ET.SubElement(w, 'property', {'name': 'checkable'})
        ET.SubElement(prop, 'bool').text = 'true'
        prop2 = ET.SubElement(w, 'property', {'name': 'checked'})
        ET.SubElement(prop2, 'bool').text = 'true'
        
        # Remove empty or old inline stylesheets
        for p in w.findall('property'):
            if p.attrib.get('name') == 'styleSheet':
                w.remove(p)

# Search for the layout that contains chkNotch
for lay in root.findall('.//layout'):
    # Iterate over items safely to remove the one containing chkNotch
    items_to_remove = []
    for item in list(lay):
        inner_w = item.find('widget')
        if inner_w is not None and inner_w.attrib.get('name') == 'chkNotch':
            items_to_remove.append(item)
    for item in items_to_remove:
        lay.remove(item)

tree.write('NPG-Controller.ui', encoding='utf-8', xml_declaration=True)
print("UI successfully mutated!")
