# Physically Based material browser for Houdini

A PySide2 window for Houdini 19.5 that shows the
[physicallybased.info](https://physicallybased.info) material database as a
sortable, searchable table: **Name | Color | IOR | Density**.

The IOR column switches between six modes (Complex IOR is the default):

| Mode | Source |
|---|---|
| Complex IOR | stored `complexIor` (n above k) |
| IOR | stored `ior` |
| Specular | computed: F0 / 0.08, with F0 = ((n−1)/(n+1))², dielectrics only |
| Specular Color | stored `specularColor`, Gulbrandsen entry |
| Specular Color (F82) | stored `specularColor`, F82 entry |
| Reflectance | computed F0 for dielectrics; stored base colour for metals |

RGB triplets are shown in linear sRGB to three decimals. The copy icon next to
each triplet copies it as `r,g,b` at full stored precision.

## Install

Copy `physically_based_browser.py` and `materials.json` into
`houdini19.5/scripts/python`, then create a shelf tool:

```python
import importlib
import physically_based_browser as pbb
importlib.reload(pbb)
pbb.show()
```

`pbb.show("/path/to/materials.json")` loads a database from elsewhere, and the
**Load materials** icon button at the top right of the window does the same.

## Data

`materials.json` is the Physically Based database (schema 2.2), released under
CC0 1.0 Universal. A newer copy can be downloaded from
<https://api.physicallybased.info/v2/materials>.
