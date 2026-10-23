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
| Reflectance | computed F0 for dielectrics; for metals, the luminance of the sRGB Linear base colour (as on physicallybased.info), with the per-channel F0 below it |

All values are shown to three decimals. The copy icon next to each triplet
copies it as `r,g,b`: stored values at full stored precision, converted values
at exactly three decimals.

## Color space

The **Color space** box switches between ACEScg (the default) and sRGB Linear. It affects
the Color column and the Complex IOR, Specular Color, Specular Color (F82) and
per-channel Reflectance F0 values. IOR, Specular, the single Reflectance value
and Density don't change.

Values stored in the database are always used as they are. The database
stores ACEScg for metals only, so the base colours of non-metals are converted
from sRGB Linear with the standard Bradford-adapted matrix (the same result as
Color.js, which physicallybased.info credits for its converter). Converted
values are shown in *italics*. Complex IOR is never converted, because n and k
aren't colours. The name swatches are always drawn from sRGB Linear.

## References

Double-click a row (the cursor shows a question mark over the Name column), or
right-click and choose **References…**, to open a popup with the material's
description and references. It closes when you click outside it or press Esc.
Click a title to open it in your browser, or right-click it to open it or copy
its URL. The copy icon next to the material name copies all references as full
citations, one per line. The popup grows to fit its content and only scrolls
when it can't fit on the screen.

## Window

Name is the only column that grows and shrinks with the window. Color, IOR and
Density are sized to the widest values they can show in any IOR mode and color
space, so they don't move when you switch.

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

Palmqvist, A. (2026) Physically Based - The PBR Values Database Available at: https://physicallybased.info/ (Accessed: 22 October 2026).

`materials.json` is the Physically Based database (schema 2.2), released under
CC0 1.0 Universal. A newer copy can be downloaded from
<https://api.physicallybased.info/v2/materials>.

**Local change:** Cast Iron's `metalness` is set to `0` (upstream: `1`). Its
entry has an IOR of 1.5, no complex IOR and a measured base colour of 0.027,
which fits a non-metallic (oxidised) surface rather than bare metal. Downloading
a newer database file undoes this edit, so reapply it after updating.
