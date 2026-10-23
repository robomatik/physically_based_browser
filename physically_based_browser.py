"""
Physically Based material browser for Houdini 19.5 (PySide2).

Shows the physicallybased.info material database (materials.json, schema v2.x)
in a sortable, searchable table:  Name | Color | <IOR mode> | Density

The third column switches between:
    Complex IOR, IOR, Specular, Specular Color, Specular Color (F82), Reflectance

Stored in the JSON:  complexIor, ior, specularColor (Gulbrandsen / F82)
Derived here:        Specular    = F0 / 0.08, with F0 = ((n-1)/(n+1))^2
                     Reflectance = F0 from ior for dielectrics;
                                   for metals, luminance of the sRGB Linear
                                   base color, with the per-channel F0 below

Color space (sRGB Linear / ACEScg) applies to Color, Complex IOR, both
Specular Colors and metal Reflectance. Stored values are always preferred;
ACEScg missing from the database (base colours of non-metals) is converted
from sRGB Linear with the standard Bradford matrix and drawn in italics.
Complex IOR is never converted - n and k are not colours.

Usage (shelf tool / Python Shell):
    import physically_based_browser as pbb
    pbb.show()                       # looks for materials.json next to this file
    pbb.show("/path/to/materials.json")

Compatible with Python 3.7+ (Houdini 19.5 ships 3.7 and 3.9 builds).
"""

import base64
import datetime
import html
import json
import os

try:
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:  # allows testing outside Houdini; Houdini 19.5 always has PySide2
    from PySide6 import QtCore, QtGui, QtWidgets

try:
    import hou
except ImportError:
    hou = None


# --------------------------------------------------------------------------
# Data layer (no Qt) – raw values stay raw; formatting happens later
# --------------------------------------------------------------------------

MODE_COMPLEX_IOR = "Complex IOR"
MODE_IOR = "IOR"
MODE_SPECULAR = "Specular"
MODE_SPECULAR_COLOR = "Specular Color"
MODE_SPECULAR_F82 = "Specular Color (F82)"
MODE_REFLECTANCE = "Reflectance"

MODES = [
    MODE_COMPLEX_IOR,
    MODE_IOR,
    MODE_SPECULAR,
    MODE_SPECULAR_COLOR,
    MODE_SPECULAR_F82,
    MODE_REFLECTANCE,
]

# Header tooltips (text as on physicallybased.info)
IOR_COLUMN_TOOLTIP = "\n\n".join([
    'Complex IOR specifies the color of a metal with an IOR value of "n", '
    'and an extinction coefficient of "k".',
    "IOR is the index of refraction, which controls both the reflection, "
    "as well as the refraction, of a dielectric material.",
    "Specular corresponds to an index of refraction value between 1.0-1.8, "
    "remapped to a 0-1 range, where 0.5 is equivalent to an IOR of 1.5.\n"
    "(Computed: F0 / 0.08)",
    "Specular Color specifies the color of a metallic material when viewed at a 90° angle.",
    "Specular Color (F82) specifies the color of a metallic material "
    "when viewed at an 82° angle.",
    "Reflectance (F0) specifies the reflectance of a material when viewed at a 0° angle.\n"
    "(Computed from IOR for dielectrics; for metals, the luminance of the sRGB Linear "
    "base color, as on physicallybased.info)\n"
    "The per-channel F0 are shown below the scalar reflectance in case users want to "
    "tint specular.",
])

DENSITY_COLUMN_TOOLTIP = "This value can be used for rigid body physics simulation."

# CIRCLED LATIN SMALL LETTER I, appended to header labels that have a tooltip
INFO_MARK = "ⓘ"

COLOR_COLUMN_TOOLTIP = "\n\n".join([
    "For dielectrics (non-metals), this specifies diffuse reflectance for opaque materials, "
    "or transmission color for transparent materials.",
    "For conductors (metals), this specifies reflection color.",
])

# Color spaces
SPACE_SRGB_LINEAR = "srgb-linear"
SPACE_ACESCG = "acescg"
COLOR_SPACES = [  # (combobox label, colorSpace key in materials.json)
    ("sRGB Linear", SPACE_SRGB_LINEAR),
    ("ACEScg", SPACE_ACESCG),
]
COLOR_SPACE_LABELS = dict((key, label) for label, key in COLOR_SPACES)
DEFAULT_COLOR_SPACE = SPACE_ACESCG  # VFX pipelines work in ACEScg

COLOR_SPACE_TOOLTIP = (
    "Color space for the Color, Complex IOR, Specular Color, Specular Color (F82) "
    "and per-channel Reflectance F0 values. IOR, Specular, the scalar Reflectance "
    "and Density don't change.\n\n"
    "Values in italics are converted from sRGB Linear, because the database "
    "doesn't store them in this color space. All other values are stored in the database."
)

# Linear sRGB (D65) -> ACEScg (AP1, D60), Bradford chromatic adaptation.
# The standard OCIO/ACES matrix; matches Color.js / ColorAide to ~5e-7.
SRGB_LINEAR_TO_ACESCG = (
    (0.6130974, 0.3395231, 0.0473795),
    (0.0701937, 0.9163539, 0.0134524),
    (0.0206156, 0.1095698, 0.8698151),
)

# Luminance (Y) weights per space, used for sorting RGB columns
LUMINANCE_WEIGHTS = {
    SPACE_SRGB_LINEAR: (0.2126, 0.7152, 0.0722),
    SPACE_ACESCG: (0.2722287, 0.6740818, 0.0536895),
}

# Value kinds returned by mode_value()
KIND_NONE = "none"
KIND_SCALAR = "scalar"
KIND_RGB = "rgb"
KIND_COMPLEX = "complex"
KIND_REFLECTANCE = "reflectance"  # metal: scalar luminance + per-channel F0 triplet


def load_materials(path):
    """Return the list of material dicts from a physicallybased materials.json."""
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        data = payload.get("data", [])
    else:  # tolerate a bare list
        data = payload
    return [m for m in data if isinstance(m, dict) and m.get("name")]


def _pick_space(entries, space=SPACE_SRGB_LINEAR):
    """From a list of {'colorSpace': ...} entries return the one in `space`,
    or None. Deliberately no fallback: values from another space must never be
    shown under this space's label."""
    for entry in entries or []:
        if entry.get("colorSpace") == space:
            return entry
    return None


def srgb_linear_to_acescg(rgb, digits=3):
    """Matrix-convert a linear sRGB triplet to ACEScg, rounded to the
    database's precision (3 decimals) so displayed and copied values agree."""
    return tuple(round(sum(row[c] * rgb[c] for c in range(3)), digits)
                 for row in SRGB_LINEAR_TO_ACESCG)


def _rgb_in_space(entries, space):
    """(rgb, converted) for a list of colour entries in `space`.

    Stored values are used as they are. If ACEScg isn't stored, it's converted
    from the stored sRGB Linear triplet (converted=True). (None, False) if
    neither is possible."""
    entry = _pick_space(entries, space)
    if entry and entry.get("color"):
        return tuple(float(c) for c in entry["color"][:3]), False
    if space == SPACE_ACESCG:
        src = _pick_space(entries, SPACE_SRGB_LINEAR)
        if src and src.get("color"):
            return srgb_linear_to_acescg([float(c) for c in src["color"][:3]]), True
    return None, False


def base_color_in_space(material, space=SPACE_SRGB_LINEAR):
    """(rgb, converted) for the material's base colour in `space`."""
    return _rgb_in_space(material.get("color"), space)


def base_color(material):
    """Base colour in sRGB Linear (used for the on-screen swatches)."""
    return base_color_in_space(material, SPACE_SRGB_LINEAR)[0]


def is_metal(material):
    """True for materials the IOR modes treat as conductors (metalness >= 0.5)."""
    try:
        return float(material.get("metalness", 0)) >= 0.5
    except (TypeError, ValueError):
        return False


def _specular_color(material, fmt, space=SPACE_SRGB_LINEAR):
    """(rgb, converted) for the Gulbrandsen or F82 specular colour."""
    for item in material.get("specularColor") or []:
        if fmt in (item.get("format") or []):
            return _rgb_in_space(item.get("color"), space)
    return None, False


def f0_from_ior(ior):
    """Normal-incidence reflectance of a dielectric against air."""
    return ((ior - 1.0) / (ior + 1.0)) ** 2


def specular_from_ior(ior):
    """Disney/UE 'specular': F0 / 0.08  ->  IOR 1.5 = 0.5, IOR ~1.8 = ~1.0."""
    return f0_from_ior(ior) / 0.08


def mode_value(material, mode, space=SPACE_SRGB_LINEAR):
    """Return (kind, value, converted) for `material` under `mode` in `space`.

    scalar  -> float                       (colour-space independent)
    rgb     -> (r, g, b)   linear, may exceed 1.0
    complex -> {'n': (r, g, b), 'k': (r, g, b)}
    reflectance -> {'scalar': float, 'rgb': (r, g, b)}
               scalar = luminance of the sRGB Linear base colour (never changes
               with `space`, as on physicallybased.info); rgb = per-channel F0
               in `space`
    none    -> None        (mode does not apply to this material)

    converted is True when the value was matrix-converted from sRGB Linear
    rather than stored in `space`. Complex IOR is never converted: n and k
    are not colours, so a missing space shows as 'none'.
    """
    ior = material.get("ior")

    if mode == MODE_IOR:
        if ior is not None:
            return KIND_SCALAR, float(ior), False

    elif mode == MODE_COMPLEX_IOR:
        entry = _pick_space(material.get("complexIor"), space)
        if entry and entry.get("n") and entry.get("k"):
            return KIND_COMPLEX, {
                "n": tuple(float(v) for v in entry["n"][:3]),
                "k": tuple(float(v) for v in entry["k"][:3]),
            }, False

    elif mode == MODE_SPECULAR:
        if ior is not None and not is_metal(material):
            return KIND_SCALAR, specular_from_ior(float(ior)), False

    elif mode in (MODE_SPECULAR_COLOR, MODE_SPECULAR_F82):
        fmt = "Gulbrandsen" if mode == MODE_SPECULAR_COLOR else "F82"
        rgb, converted = _specular_color(material, fmt, space)
        if rgb:
            return KIND_RGB, rgb, converted

    elif mode == MODE_REFLECTANCE:
        if is_metal(material):
            srgb = base_color(material)
            rgb, converted = base_color_in_space(material, space)
            if srgb and rgb:
                return KIND_REFLECTANCE, {
                    "scalar": luminance(srgb, SPACE_SRGB_LINEAR),
                    "rgb": rgb,
                }, converted
        elif ior is not None:
            return KIND_SCALAR, f0_from_ior(float(ior)), False

    return KIND_NONE, None, False


def format_number(value, digits=3):
    text = "{:.{d}f}".format(value, d=digits).rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def format_rgb(rgb, digits=3):
    """Display form: space separated, fixed decimals, e.g. '0.910 0.936 0.959'."""
    return " ".join("{:.{d}f}".format(float(c), d=digits) for c in rgb)


def raw_number(value):
    """Number at full stored precision: no trailing '.0', no scientific notation."""
    text = repr(float(value))
    if "e" in text or "E" in text:
        text = "{:.10f}".format(float(value)).rstrip("0").rstrip(".")
    elif text.endswith(".0"):
        text = text[:-2]
    return "0" if text in ("", "-0") else text


def rgb_copy_text(rgb, converted=False):
    """Clipboard form: comma separated, no spaces.

    Stored values copy at full stored precision, e.g. '0.728978,0.588215,0.78354'.
    Converted values copy at exactly three decimals, e.g. '0.608,0.323,0.080'."""
    if converted:
        return ",".join("{:.3f}".format(float(c)) for c in rgb)
    return ",".join(raw_number(c) for c in rgb)


def format_scalar(value):
    """Single values always show exactly three decimals, e.g. '1.500'."""
    return "{:.3f}".format(float(value))


def format_mode_value(kind, value):
    if kind == KIND_SCALAR:
        return format_scalar(value)
    if kind == KIND_REFLECTANCE:
        return "{}   F0: {}".format(format_scalar(value["scalar"]), format_rgb(value["rgb"]))
    if kind == KIND_RGB:
        return format_rgb(value)
    if kind == KIND_COMPLEX:
        return "n: {}   k: {}".format(format_rgb(value["n"]), format_rgb(value["k"]))
    return "—"


def mode_segments(kind, value, converted=False):
    """[(display_text, copy_text), ...] for cells drawn line by line, else None.
    A copy_text of None means that line has no copy icon."""
    if kind == KIND_REFLECTANCE:
        return [(format_scalar(value["scalar"]), None),
                (format_rgb(value["rgb"]), rgb_copy_text(value["rgb"], converted))]
    if kind == KIND_RGB:
        return [(format_rgb(value), rgb_copy_text(value, converted))]
    if kind == KIND_COMPLEX:
        return [("n: " + format_rgb(value["n"]), rgb_copy_text(value["n"])),
                ("k: " + format_rgb(value["k"]), rgb_copy_text(value["k"]))]
    return None


def mode_sort_key(kind, value, space=SPACE_SRGB_LINEAR):
    """Numeric key for sorting; None sorts after everything."""
    if kind == KIND_SCALAR:
        return value
    if kind == KIND_REFLECTANCE:
        return value["scalar"]
    if kind == KIND_RGB:
        return luminance(value, space)
    if kind == KIND_COMPLEX:  # sort by resulting F0 brightness
        return luminance(tuple(f0_complex(n, k) for n, k in zip(value["n"], value["k"])), space)
    return None


def f0_complex(n, k):
    return ((n - 1.0) ** 2 + k * k) / ((n + 1.0) ** 2 + k * k)


def luminance(rgb, space=SPACE_SRGB_LINEAR):
    wr, wg, wb = LUMINANCE_WEIGHTS.get(space, LUMINANCE_WEIGHTS[SPACE_SRGB_LINEAR])
    r, g, b = rgb
    return wr * r + wg * g + wb * b


def linear_to_srgb8(c):
    """Linear -> sRGB-encoded 0-255, clamped (values above 1.0 display as 1.0)."""
    c = max(0.0, min(1.0, float(c)))
    s = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055
    return int(round(s * 255.0))


def format_accessed(date_text):
    """'2025-06-26' -> '26 June 2025' (anything else is returned unchanged)."""
    try:
        d = datetime.datetime.strptime(date_text, "%Y-%m-%d")
    except (TypeError, ValueError):
        return date_text
    return "{} {}".format(d.day, d.strftime("%B %Y"))


def format_citation(ref):
    """One reference as a Harvard-style citation, matching the README's, e.g.
    'Polyanskiy, M. N. (n.d.) Refractive index database. Available at:
    https://refractiveindex.info/... (Accessed: 26 June 2025).'"""
    parts = []
    lead = ref.get("author") or ""
    year = ref.get("year") or "n.d."
    if lead:
        parts.append("{} ({}) {}.".format(lead, year, ref.get("title", "").rstrip(".")))
    else:
        parts.append("{} ({}).".format(ref.get("title", "").rstrip("."), year))
    source = ref.get("publisher") or ""
    if ref.get("volume"):
        source += (", " if source else "") + ref["volume"]
        if ref.get("issue"):
            source += "({})".format(ref["issue"])
    if ref.get("pages"):
        source += (", " if source else "") + "pp. " + ref["pages"].replace("--", "–")
    if source:
        parts.append(source + ".")
    if ref.get("doi"):
        parts.append("DOI: {}.".format(ref["doi"]))
    if ref.get("url"):
        accessed = " (Accessed: {})".format(format_accessed(ref["accessed"])) if ref.get("accessed") else ""
        parts.append("Available at: {}{}.".format(ref["url"], accessed))
    return " ".join(parts)


def reference_meta(ref):
    """Second line under a reference title: 'author · publisher · year'."""
    return " · ".join(x for x in (ref.get("author"), ref.get("publisher"), ref.get("year")) if x)


def density_values(material):
    vals = material.get("density") or []
    out = []
    for v in vals:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            pass
    return out


def format_density(material):
    """[v] -> 'v'   [lo, hi] -> 'lo–hi'   [lo, hi, typ] -> 'lo–hi (typ)'."""
    vals = density_values(material)
    if not vals:
        return "—"
    f = lambda v: format_number(v, 2)
    if len(vals) == 1:
        return f(vals[0])
    if len(vals) == 2 or vals[0] == vals[1]:
        return "{}–{}".format(f(vals[0]), f(vals[1]))
    return "{}–{} ({})".format(f(vals[0]), f(vals[1]), f(vals[2]))


def density_sort_key(material):
    vals = density_values(material)
    if not vals:
        return None
    if len(vals) >= 3:
        return vals[2]
    return sum(vals) / len(vals)



# --------------------------------------------------------------------------
# Qt model
# --------------------------------------------------------------------------

COL_NAME, COL_COLOR, COL_MODE, COL_DENSITY = range(4)

HEADER_TOOLTIPS = {
    COL_COLOR: COLOR_COLUMN_TOOLTIP,
    COL_MODE: IOR_COLUMN_TOOLTIP,
    COL_DENSITY: DENSITY_COLUMN_TOOLTIP,
}

ROLE_SORT = QtCore.Qt.UserRole + 1
ROLE_SEGMENTS = QtCore.Qt.UserRole + 2  # [(display_text, copy_text), ...] for copy icons
ROLE_MATERIAL = QtCore.Qt.UserRole + 3  # the raw material dict
ROLE_RAW = QtCore.Qt.UserRole + 4       # (kind, value, converted) for the mode column
ROLE_CONVERTED = QtCore.Qt.UserRole + 5  # True if the cell's values were converted (drawn italic)

CONVERTED_CELL_TOOLTIP = "Converted from sRGB Linear (not stored in ACEScg in the database)"


# Icon for the 'Load materials' button: 32x32 PNG, embedded so the module
# stays a single file.
LOAD_ICON_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAACXBIWXMAAADsAAAA7AF5KHG9"
    "AAAAGXRFWHRTb2Z0d2FyZQB3d3cuaW5rc2NhcGUub3Jnm+48GgAAAtxJREFUWIW9l0lrFEEU"
    "gL9XPT2aGIPbwQ23eFBRMC6HoKJIJiLBm2NQFA+iIoK/IXgQFLx5EKNBBBcET0EFnbhA9KJI"
    "UM8xenAhCSYkk8lMd9fzoMaM6ekWTfuO9b6q9/XU6+oaSDC0tTUVx0ilRFP74N4g8M8L5nXu"
    "6Jw9Hzs2VjsBL3xfVyHl84xIIZ2SU5+uzu3RlD3mBbo9sDrfopI25mX9ndzmSnUmGTa1D+61"
    "gXfWH8uvABA3PQCQVrlY8Oya749WPsei1aralscTvPJcydpNb/c1Hl17O9cWKZBpH9pv/dI5"
    "fyy/OAy0ytpKTwEQWMSttnh5MynnBxwGwgUyV4ZO2qB0OhgbmRNV4I/ChO+oj66vOCUojlxQ"
    "v/TvxSPCWp3xpmV3XahAkoV/hgJY73hYLvY1+ZsQfvVpSgTXMbjGLAsVcGvnRS+m+gpgZpV5"
    "NbPK1Eex/b5gRHCNMM04uI7gyHhfbAgVMI4bKYDo8HdTGRaNRmvdFAanUrru/YHmFUtv3O2Z"
    "ODilPZCSiucaABbb+PvYf2nCnyFIwgJGvkalFW3UbLZsj6ZUQISuGGR2j5Mva+QpFTDT7ONY"
    "xiGTnMCswtNYSG1yAgsywz0g76Ip2fL5UNOMRAQARDUXg6QLgdmamIBF4gRQlfFtmHKBtFvs"
    "BGwUI0JyAouvdQ4A3THYut6DuxYkIvAjHsbkBWt2JiZgsfF98ONYTkTApItdQCHaQDOJCSy/"
    "+mRMkGeRkLCot6V5dWJfQyX2PECdIJOYgBgb14gIkpzA0pUN3QL9UYxCQ3K/QGurVeiMwQYN"
    "8D5yIUwvgLHRHDDAju6hiQMK9yL4L6IcM4icAPpCAAUe+CUuAxi/0IZwn9BjVvuAIyLl/xoL"
    "Xs0tYfySYoEXCmcEs3PUq1my7Ob9R+O3yObrOnviZFNLsWOPjE4q9byhisCZPnFMtnVVvIpp"
    "Nut8cEdr3RpTXHipY9J63wBzZvbcU/67OQAAAABJRU5ErkJggg=="
)


def load_materials_icon():
    pix = QtGui.QPixmap()
    pix.loadFromData(base64.b64decode(LOAD_ICON_PNG_B64), "PNG")
    return QtGui.QIcon(pix)


SWATCH_SIZE = 14  # px, Name-column colour chip


def swatch_icon(rgb, size=SWATCH_SIZE):
    """Small rounded colour chip (display-encoded sRGB) with a thin outline."""
    pix = QtGui.QPixmap(size, size)
    pix.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    painter.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 110), 1))
    painter.setBrush(QtGui.QColor(*(linear_to_srgb8(c) for c in rgb)))
    painter.drawRoundedRect(QtCore.QRectF(0.5, 0.5, size - 1, size - 1), 3, 3)
    painter.end()
    return QtGui.QIcon(pix)


class MaterialModel(QtCore.QAbstractTableModel):

    def __init__(self, materials=None, parent=None):
        super(MaterialModel, self).__init__(parent)
        self._materials = list(materials or [])
        self._mode = MODE_COMPLEX_IOR
        self._space = DEFAULT_COLOR_SPACE
        self._cache = {}        # row -> (kind, value, converted) for mode/space
        self._color_cache = {}  # row -> (rgb, converted) for space
        self._swatches = {}  # rgb tuple -> QIcon

    # -- API ---------------------------------------------------------------
    def set_materials(self, materials):
        self.beginResetModel()
        self._materials = list(materials)
        self._cache = {}
        self._color_cache = {}
        self.endResetModel()

    def material(self, row):
        return self._materials[row]

    def mode(self):
        return self._mode

    def set_mode(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        self._cache = {}
        if self._materials:
            top = self.index(0, COL_MODE)
            bottom = self.index(len(self._materials) - 1, COL_MODE)
            self.dataChanged.emit(top, bottom)
        self.headerDataChanged.emit(QtCore.Qt.Horizontal, COL_MODE, COL_MODE)

    def color_space(self):
        return self._space

    def set_color_space(self, space):
        if space == self._space:
            return
        self._space = space
        self._cache = {}
        self._color_cache = {}
        if self._materials:
            top = self.index(0, COL_COLOR)
            bottom = self.index(len(self._materials) - 1, COL_MODE)
            self.dataChanged.emit(top, bottom)
        self.headerDataChanged.emit(QtCore.Qt.Horizontal, COL_COLOR, COL_MODE)

    def _value(self, row):
        if row not in self._cache:
            self._cache[row] = mode_value(self._materials[row], self._mode, self._space)
        return self._cache[row]

    def _color(self, row):
        if row not in self._color_cache:
            self._color_cache[row] = base_color_in_space(self._materials[row], self._space)
        return self._color_cache[row]

    # -- Qt overrides ------------------------------------------------------
    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self._materials)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if orientation != QtCore.Qt.Horizontal:
            return None
        if role == QtCore.Qt.DisplayRole:
            space = COLOR_SPACE_LABELS[self._space]
            if section == COL_COLOR:
                text = "Color ({})".format(space)
            elif section == COL_MODE:
                # only colour-carrying modes depend on the space
                if self._mode in (MODE_IOR, MODE_SPECULAR):
                    text = self._mode
                else:
                    text = "{} ({})".format(self._mode, space)
            elif section == COL_DENSITY:
                text = "Density (kg/m³)"
            else:
                return "Name"
            return text + "  " + INFO_MARK  # marks headers that have a tooltip
        if role == QtCore.Qt.ToolTipRole:
            return HEADER_TOOLTIPS.get(section)
        return None

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        mat = self._materials[row]

        if role == ROLE_MATERIAL:
            return mat

        if col == COL_NAME:
            if role == QtCore.Qt.DisplayRole:
                return mat.get("name")
            if role == ROLE_SORT:
                return (mat.get("name") or "").lower()
            if role == QtCore.Qt.DecorationRole:
                rgb = base_color(mat)
                if not rgb:
                    return None
                if rgb not in self._swatches:
                    self._swatches[rgb] = swatch_icon(rgb)
                return self._swatches[rgb]
            if role == QtCore.Qt.ToolTipRole:
                lines = [mat.get("description") or ""]
                refs = mat.get("references") or []
                if refs:
                    lines.append("Double-click for references ({})".format(len(refs)))
                return "\n\n".join(l for l in lines if l) or None

        elif col == COL_COLOR:
            rgb, converted = self._color(row)
            if role == ROLE_SEGMENTS:
                return [(format_rgb(rgb), rgb_copy_text(rgb, converted))] if rgb else None
            if role == ROLE_CONVERTED:
                return converted
            if role == QtCore.Qt.DisplayRole:
                return format_rgb(rgb) if rgb else "—"
            if role == ROLE_SORT:
                return luminance(rgb, self._space) if rgb else None
            if role == QtCore.Qt.ToolTipRole and rgb:
                return CONVERTED_CELL_TOOLTIP if converted else COLOR_SPACE_LABELS[self._space]
            if role == QtCore.Qt.TextAlignmentRole:
                return int(QtCore.Qt.AlignCenter)

        elif col == COL_MODE:
            kind, value, converted = self._value(row)
            if role == ROLE_RAW:
                return kind, value, converted
            if role == ROLE_SEGMENTS:
                return mode_segments(kind, value, converted)
            if role == ROLE_CONVERTED:
                return converted
            if role == QtCore.Qt.DisplayRole:
                return format_mode_value(kind, value)
            if role == ROLE_SORT:
                return mode_sort_key(kind, value, self._space)
            if role == QtCore.Qt.ToolTipRole and converted:
                return CONVERTED_CELL_TOOLTIP
            if role == QtCore.Qt.TextAlignmentRole:
                return int(QtCore.Qt.AlignCenter)

        elif col == COL_DENSITY:
            if role == QtCore.Qt.DisplayRole:
                return format_density(mat)
            if role == ROLE_SORT:
                return density_sort_key(mat)
            if role == QtCore.Qt.TextAlignmentRole:
                return int(QtCore.Qt.AlignCenter)

        return None


class MaterialFilterProxy(QtCore.QSortFilterProxyModel):
    """Text search over name / tags / category, optional category filter,
    numeric sorting with empty cells always last."""

    def __init__(self, parent=None):
        super(MaterialFilterProxy, self).__init__(parent)
        self._text = ""
        self._category = None
        self.setSortRole(ROLE_SORT)
        self.setDynamicSortFilter(True)

    def set_text(self, text):
        self._text = text.strip().lower()
        self.invalidateFilter()

    def set_category(self, category):
        self._category = category or None
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        mat = self.sourceModel().material(source_row)
        if self._category and self._category not in (mat.get("category") or []):
            return False
        if not self._text:
            return True
        haystack = [mat.get("name", "")] + list(mat.get("tags") or []) + list(mat.get("category") or [])
        return any(self._text in (h or "").lower() for h in haystack)

    def lessThan(self, left, right):
        a = left.data(ROLE_SORT)
        b = right.data(ROLE_SORT)
        ascending = self.sortOrder() == QtCore.Qt.AscendingOrder
        # keep empty cells at the bottom regardless of direction
        if a is None and b is None:
            return False
        if a is None:
            return not ascending
        if b is None:
            return ascending
        return a < b


class CopyValuesDelegate(QtWidgets.QStyledItemDelegate):
    """Draws each ROLE_SEGMENTS entry on its own line as 'text [copy-icon]'
    (Complex IOR: n above k, icons aligned in one column).
    Clicking an icon puts that segment's copy_text on the clipboard.
    Cells without segments (scalars, '—') fall back to normal drawing."""

    ICON = 14      # icon size in px
    PAD = 4        # left padding
    GAP = 6        # text -> icon
    LINE_PAD = 4   # extra vertical space per line
    V_MARGIN = 3   # top/bottom margin inside the cell

    copied = QtCore.Signal(str)

    def __init__(self, view):
        super(CopyValuesDelegate, self).__init__(view)
        self._view = view
        self._hover = None  # (row, column, segment) of the icon under the mouse
        self._cursor = None  # cursor shape currently set on the viewport
        self.last_double_click = None  # (viewport pos, global pos) of the last double-click
        view.setMouseTracking(True)
        view.viewport().installEventFilter(self)

    # -- geometry ----------------------------------------------------------
    @staticmethod
    def cell_font(base_font, index):
        """The view font, italic when the cell's values were converted."""
        font = QtGui.QFont(base_font)
        if index.data(ROLE_CONVERTED):
            font.setItalic(True)
        return font

    def _layout(self, rect, base_font, index):
        """[(label, copy_text, text_rect, icon_rect), ...] for the cell."""
        segments = index.data(ROLE_SEGMENTS)
        if not segments:
            return []
        fm = QtGui.QFontMetrics(self.cell_font(base_font, index))
        line_h = self._line_height(fm)
        top = rect.center().y() - (line_h * len(segments)) // 2 + 1
        # measure with every digit as '0' so narrow digits (e.g. '1') don't shift
        # the icon column from row to row
        text_w = max(max(fm.horizontalAdvance(label),
                         fm.horizontalAdvance("".join("0" if ch.isdigit() else ch for ch in label)))
                     for label, _ in segments)
        if index.data(ROLE_CONVERTED):
            text_w += 2  # italic glyphs lean past their advance width
        block_w = text_w + self.GAP + self.ICON
        # centre the text+icon block in the cell; lines stay left-aligned within it
        x = rect.left() + max(self.PAD, (rect.width() - block_w) // 2)
        icon_x = x + text_w + self.GAP  # one icon column for all lines
        out = []
        for i, (label, copy_text) in enumerate(segments):
            line_top = top + i * line_h
            text_rect = QtCore.QRect(x, line_top, text_w, line_h)
            icon_rect = QtCore.QRect(icon_x, line_top + (line_h - self.ICON) // 2,
                                     self.ICON, self.ICON)
            out.append((label, copy_text, text_rect, icon_rect))
        return out

    def _line_height(self, fm):
        return max(fm.height(), self.ICON) + self.LINE_PAD

    def row_height(self, fm, lines=2):
        """Height of a cell holding `lines` lines (same formula as sizeHint)."""
        return self._line_height(fm) * lines + 2 * self.V_MARGIN

    def _hit(self, rect, base_font, index, pos):
        for i, (_, copy_text, _, icon_rect) in enumerate(self._layout(rect, base_font, index)):
            if copy_text is not None and icon_rect.adjusted(-3, -3, 3, 3).contains(pos):
                return i, copy_text
        return None, None

    # -- painting ----------------------------------------------------------
    def paint(self, painter, option, index):
        layout = self._layout(option.rect, option.font, index)
        if not layout:
            super(CopyValuesDelegate, self).paint(painter, option, index)
            return

        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        selected = bool(opt.state & QtWidgets.QStyle.State_Selected)
        fg = opt.palette.color(QtGui.QPalette.HighlightedText if selected else QtGui.QPalette.Text)

        value_font = self.cell_font(option.font, index)  # italic if converted
        painter.save()
        painter.setClipRect(opt.rect)
        painter.setPen(fg)
        for i, (label, copy_text, text_rect, icon_rect) in enumerate(layout):
            if copy_text is None:
                # a summary line without its own copy icon (Reflectance scalar):
                # never converted, so never italic; centred over the line below
                painter.setFont(option.font)
                painter.drawText(text_rect, int(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignHCenter), label)
                continue
            painter.setFont(value_font)
            painter.drawText(text_rect, int(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft), label)
            hovered = self._hover == (index.row(), index.column(), i)
            self._draw_copy_icon(painter, icon_rect, fg, hovered)
        painter.restore()

    @staticmethod
    def _draw_copy_icon(painter, rect, color, hovered):
        """Flat 'two overlapping sheets' copy glyph."""
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        c = QtGui.QColor(color)
        if hovered:
            bg = QtGui.QColor(color)
            bg.setAlpha(45)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(QtCore.QRectF(rect).adjusted(-2, -2, 2, 2), 3, 3)
        else:
            c.setAlpha(140)
        painter.setPen(QtGui.QPen(c, 1.2))
        painter.setBrush(QtCore.Qt.NoBrush)

        r = QtCore.QRectF(rect).adjusted(1, 1, -1, -1)
        s = r.width() * 0.68                      # sheet size
        back = QtCore.QRectF(r.left(), r.top(), s, s)
        front = QtCore.QRectF(r.right() - s, r.bottom() - s, s, s)
        # back sheet: only the part not hidden by the front sheet
        painter.drawPolyline(QtGui.QPolygonF([
            QtCore.QPointF(front.left(), back.bottom()),
            QtCore.QPointF(back.left(), back.bottom()),
            QtCore.QPointF(back.left(), back.top()),
            QtCore.QPointF(back.right(), back.top()),
            QtCore.QPointF(back.right(), front.top()),
        ]))
        painter.drawRoundedRect(front, 1.5, 1.5)
        painter.restore()

    def sizeHint(self, option, index):
        hint = super(CopyValuesDelegate, self).sizeHint(option, index)
        layout = self._layout(option.rect, option.font, index)
        if layout:
            text_w = layout[0][2].width()
            hint.setWidth(text_w + self.GAP + self.ICON + 2 * self.PAD + 2)
            hint.setHeight(self._line_height(option.fontMetrics) * len(layout) + 2 * self.V_MARGIN)
        return hint

    # -- interaction -------------------------------------------------------
    def _set_hover(self, hover):
        if hover != self._hover:
            self._hover = hover
            self._view.viewport().update()

    def _set_cursor(self, shape):
        if shape != self._cursor:
            self._cursor = shape
            if shape is None:
                self._view.viewport().unsetCursor()
            else:
                self._view.viewport().setCursor(shape)

    def over_copy_icon(self, pos):
        """True if viewport position `pos` is on one of this delegate's copy icons."""
        index = self._view.indexAt(pos)
        if not index.isValid() or self._view.itemDelegateForColumn(index.column()) is not self:
            return False
        seg, _ = self._hit(self._view.visualRect(index), self._view.font(), index, pos)
        return seg is not None

    def eventFilter(self, obj, event):
        """Hover tracking (item views don't forward mouse moves to delegates)."""
        etype = event.type()
        if etype == QtCore.QEvent.MouseMove:
            pos = event.pos()
            index = self._view.indexAt(pos)
            hover = None
            if index.isValid() and self._view.itemDelegateForColumn(index.column()) is self:
                seg, _ = self._hit(self._view.visualRect(index), self._view.font(), index, pos)
                if seg is not None:
                    hover = (index.row(), index.column(), seg)
            self._set_hover(hover)
            if hover:
                self._set_cursor(QtCore.Qt.PointingHandCursor)
            elif index.isValid() and index.column() == COL_NAME:
                # hints that the Name cell does something: double-click for references
                self._set_cursor(QtCore.Qt.WhatsThisCursor)
            else:
                self._set_cursor(None)
        elif etype == QtCore.QEvent.MouseButtonDblClick:
            self.last_double_click = (event.pos(), event.globalPos())
        elif etype == QtCore.QEvent.Leave:
            self._set_hover(None)
            self._set_cursor(None)
        return False

    def editorEvent(self, event, model, option, index):
        etype = event.type()
        if etype in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease,
                     QtCore.QEvent.MouseButtonDblClick):
            seg, copy_text = self._hit(option.rect, option.font, index, event.pos())
            if seg is not None:
                if etype == QtCore.QEvent.MouseButtonRelease and event.button() == QtCore.Qt.LeftButton:
                    QtWidgets.QApplication.clipboard().setText(copy_text)
                    QtWidgets.QToolTip.showText(event.globalPos(), "Copied  " + copy_text, self._view)
                    self.copied.emit(copy_text)
                return True  # don't let icon clicks change selection / start edits
        return super(CopyValuesDelegate, self).editorEvent(event, model, option, index)


# --------------------------------------------------------------------------
# References popup
# --------------------------------------------------------------------------

def houdini_accent_color(widget):
    """Houdini's accent colour (the selection highlight of its Qt palette),
    used for the reference links so they follow the user's UI theme."""
    return widget.palette().color(QtGui.QPalette.Highlight)


def _mix(a, b, t):
    """Colour between a (t=0) and b (t=1)."""
    return QtGui.QColor(*(int(round(x + (y - x) * t)) for x, y in
                          zip(a.getRgb()[:3], b.getRgb()[:3])))


class CopyIconButton(QtWidgets.QWidget):
    """Flat copy icon, drawn exactly like the ones next to the RGB triplets
    (same glyph and hover highlight). A plain painted widget rather than a
    QToolButton, so Houdini's stylesheet can't give it a raised button look."""

    clicked = QtCore.Signal()
    ICON = CopyValuesDelegate.ICON

    def __init__(self, parent=None):
        super(CopyIconButton, self).__init__(parent)
        self._hovered = False
        self._pressed = False
        self.setFixedSize(self.ICON + 6, self.ICON + 6)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.NoFocus)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        rect = QtCore.QRect(0, 0, self.ICON, self.ICON)
        rect.moveCenter(self.rect().center())
        CopyValuesDelegate._draw_copy_icon(painter, rect, self.palette().color(QtGui.QPalette.WindowText),
                                           self._hovered)
        painter.end()

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self._pressed = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self._pressed = True
        event.accept()

    def mouseReleaseEvent(self, event):
        if (event.button() == QtCore.Qt.LeftButton and self._pressed
                and self.rect().contains(event.pos())):
            self.clicked.emit()
        self._pressed = False
        event.accept()


class ReferencesPopup(QtWidgets.QFrame):
    """Floating list of a material's references. A Qt popup: it closes when
    the user clicks anywhere outside it or presses Esc, like a menu.

    The copy icon next to the material name copies all references as full
    citations. Titles are links (open in the default browser); right-click a
    reference to open it or copy its URL. No frame lines; the popup is set
    apart from the table by a slightly lighter background. Its height is not
    capped - it only scrolls if it can't fit on the screen at all."""

    WIDTH = 460

    def __init__(self, material, parent=None):
        super(ReferencesPopup, self).__init__(parent, QtCore.Qt.Popup)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.setObjectName("ReferencesPopup")
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setLineWidth(0)

        window = self.palette().color(QtGui.QPalette.Window)
        text = self.palette().color(QtGui.QPalette.WindowText)
        background = _mix(window, text, 0.07).name()
        # a stylesheet, so Houdini's own stylesheet can't paint over it; the
        # body and (if one is needed) the scroll viewport share the background
        self.setStyleSheet(
            "#ReferencesPopup, #ReferencesPopupBody, #ReferencesPopupScroll > QWidget"
            " { background-color: %s; border: none; }" % background)

        refs = material.get("references") or []
        self._citations = [format_citation(r) for r in refs]
        link = houdini_accent_color(self).name()
        muted = _mix(text, window, 0.45).name()

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(8)

        # header: name, count, copy icon (copies all citations)
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(4)
        title = QtWidgets.QLabel('<b>{}</b>&nbsp;&nbsp;<span style="color:{}">{} reference{}</span>'.format(
            html.escape(material.get("name", "")), muted, len(refs), "" if len(refs) == 1 else "s"))
        head.addWidget(title)
        copy_btn = CopyIconButton()
        copy_btn.setToolTip("Copy citations")
        copy_btn.clicked.connect(lambda: self._copy("\n".join(self._citations),
                                                    "Copied {} citation{}".format(
                                                        len(refs), "" if len(refs) == 1 else "s")))
        head.addWidget(copy_btn, 0, QtCore.Qt.AlignVCenter)
        head.addStretch(1)
        lay.addLayout(head)

        # body: description + references
        body = QtWidgets.QWidget()
        body.setObjectName("ReferencesPopupBody")
        body_lay = QtWidgets.QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(10)
        if material.get("description"):
            desc = QtWidgets.QLabel(material["description"])
            desc.setWordWrap(True)
            body_lay.addWidget(desc)
        for ref in refs:
            url = ref.get("url", "")
            html_text = '<a href="{url}" style="color:{link}; text-decoration:none;">{title}</a>'.format(
                url=html.escape(url, quote=True), link=link,
                title=html.escape(ref.get("title") or url))
            meta = reference_meta(ref)
            if meta:
                html_text += '<br><span style="color:{}">{}</span>'.format(muted, html.escape(meta))
            label = QtWidgets.QLabel(html_text)
            label.setWordWrap(True)
            label.setTextFormat(QtCore.Qt.RichText)
            label.setOpenExternalLinks(True)
            label.setToolTip(url)
            label.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            label.customContextMenuRequested.connect(
                lambda pos, lbl=label, u=url: self._reference_menu(lbl, pos, u))
            pal = label.palette()
            pal.setColor(QtGui.QPalette.Link, QtGui.QColor(link))
            label.setPalette(pal)
            body_lay.addWidget(label)

        # The body sits directly in the popup; a scroll area is only added in
        # popup_at() when the popup can't fit on the screen.
        lay.addWidget(body)
        self._body = body
        self._scroll = None

    def _fit_height(self):
        """Exact height of the popup at WIDTH (word-wrapped labels included)."""
        lay = self.layout()
        lay.activate()
        h = lay.totalHeightForWidth(self.WIDTH)
        return h if h > 0 else self.sizeHint().height()

    def _enable_scrolling(self, max_height):
        """Move the body into a scroll area so the popup is max_height tall."""
        lay = self.layout()
        lay.removeWidget(self._body)
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("ReferencesPopupScroll")
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setLineWidth(0)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOn)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._body)
        lay.addWidget(scroll, 1)
        self._scroll = scroll
        self.setFixedSize(self.WIDTH, max_height)

    def _copy(self, text, message):
        QtWidgets.QApplication.clipboard().setText(text)
        QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), message, self)

    def _reference_menu(self, label, pos, url):
        menu = QtWidgets.QMenu(self)
        act_open = menu.addAction("Open link")
        act_copy = menu.addAction("Copy URL")
        chosen = menu.exec_(label.mapToGlobal(pos))
        if chosen is act_open:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
        elif chosen is act_copy:
            self._copy(url, "Copied  " + url)

    def popup_at(self, global_pos):
        """Show with the top-left corner at the cursor, kept fully on screen.
        Only if the popup is taller than the screen does its body scroll."""
        screen = None
        if hasattr(QtGui.QGuiApplication, "screenAt"):
            screen = QtGui.QGuiApplication.screenAt(global_pos)
        screen = screen or QtGui.QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()

        room = geo.height() - 16
        height = self._fit_height()
        if self._scroll is None and height > room:
            self._enable_scrolling(room)
        elif self._scroll is None:
            self.setFixedSize(self.WIDTH, height)

        size = self.size()
        x, y = global_pos.x() + 2, global_pos.y() + 2
        if y + size.height() > geo.bottom():
            y = global_pos.y() - size.height() - 2  # flip above the cursor
        x = min(max(geo.left(), x), geo.right() - size.width() + 1)
        y = min(max(geo.top(), y), geo.bottom() - size.height() + 1)
        self.move(x, y)
        self.show()


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

class PhysicallyBasedBrowser(QtWidgets.QWidget):

    NAME_COLUMN_WIDTH = 230  # starting width of the stretching Name column

    def __init__(self, json_path=None, parent=None):
        super(PhysicallyBasedBrowser, self).__init__(parent)
        self.setWindowFlags(QtCore.Qt.Window)
        self.setWindowTitle("Physically Based Materials")
        self.setObjectName(WINDOW_OBJECT_NAME)  # used by close_existing()
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)
        self.resize(920, 640)

        self._json_path = None

        # widgets
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Search name, tag or category…")
        self._search.setClearButtonEnabled(True)

        self._category = QtWidgets.QComboBox()
        self._category.setToolTip("Filter by category")

        self._mode = QtWidgets.QComboBox()
        self._mode.addItems(MODES)  # mode descriptions live in the column header tooltip
        self._mode.setCurrentText(MODE_COMPLEX_IOR)
        self._mode.setToolTip("Value shown in the third column")

        self._space = QtWidgets.QComboBox()
        for label, key in COLOR_SPACES:
            self._space.addItem(label, key)
        self._space.setToolTip(COLOR_SPACE_TOOLTIP)
        self._space.setCurrentIndex(self._space.findData(DEFAULT_COLOR_SPACE))

        self._open_btn = QtWidgets.QToolButton()
        self._open_btn.setIcon(load_materials_icon())
        self._open_btn.setIconSize(QtCore.QSize(20, 20))
        self._open_btn.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self._open_btn.setToolTip("Load materials (choose a materials.json file)")
        self._open_btn.setAccessibleName("Load materials")

        self._model = MaterialModel(parent=self)
        self._proxy = MaterialFilterProxy(self)
        self._proxy.setSourceModel(self._model)

        self._view = QtWidgets.QTableView()
        self._view.setModel(self._proxy)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(COL_NAME, QtCore.Qt.AscendingOrder)
        self._view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._view.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self._view.setAlternatingRowColors(True)
        self._view.setWordWrap(False)
        self._view.verticalHeader().setVisible(False)
        self._view.setIconSize(QtCore.QSize(SWATCH_SIZE, SWATCH_SIZE))
        self._view.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._copy_delegate = CopyValuesDelegate(self._view)  # shared by both RGB columns
        # every row is two lines high (the height of an n-over-k Complex IOR cell),
        # so rows don't jump when switching modes
        two_lines = self._copy_delegate.row_height(self._view.fontMetrics(), lines=2)
        vheader = self._view.verticalHeader()
        vheader.setMinimumSectionSize(two_lines)
        vheader.setDefaultSectionSize(two_lines)
        vheader.setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
        self._view.setItemDelegateForColumn(COL_COLOR, self._copy_delegate)
        self._view.setItemDelegateForColumn(COL_MODE, self._copy_delegate)

        # Name is the only column that follows the window width; the others are
        # set to the widest they can ever need (see _fit_columns), so they
        # don't move when the window, IOR mode or colour space changes.
        header = self._view.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QtWidgets.QHeaderView.Stretch)
        for col in (COL_COLOR, COL_MODE, COL_DENSITY):
            header.setSectionResizeMode(col, QtWidgets.QHeaderView.Interactive)

        self._status = QtWidgets.QLabel()
        self._status.setStyleSheet("color: gray;")

        # layout
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self._search, 1)
        top.addWidget(self._category)
        top.addWidget(QtWidgets.QLabel("IOR mode:"))
        top.addWidget(self._mode)
        top.addWidget(QtWidgets.QLabel("Color space:"))
        top.addWidget(self._space)
        top.addWidget(self._open_btn)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addLayout(top)
        lay.addWidget(self._view, 1)
        lay.addWidget(self._status)

        # signals
        self._search.textChanged.connect(self._on_filter_changed)
        self._category.currentIndexChanged.connect(self._on_filter_changed)
        self._mode.currentTextChanged.connect(self._on_mode_changed)
        self._space.currentIndexChanged.connect(self._on_space_changed)
        self._open_btn.clicked.connect(self._browse_json)
        self._view.customContextMenuRequested.connect(self._context_menu)
        self._view.doubleClicked.connect(self._on_double_clicked)

        shortcut_cls = getattr(QtWidgets, "QShortcut", None) or QtGui.QShortcut  # PySide6 moved it
        copy_sc = shortcut_cls(QtGui.QKeySequence.Copy, self._view)
        copy_sc.activated.connect(self._copy_selection)

        if json_path:
            self.load(json_path)
            # room for the fixed columns plus a comfortable Name column
            fixed = sum(header.sectionSize(c) for c in (COL_COLOR, COL_MODE, COL_DENSITY))
            self.resize(max(self.width(), fixed + self.NAME_COLUMN_WIDTH + 40), self.height())
        else:
            self._update_status()

    # -- loading -----------------------------------------------------------
    def load(self, path):
        try:
            materials = load_materials(path)
        except (IOError, OSError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Physically Based",
                                          "Could not read {}:\n{}".format(path, exc))
            return False
        self._json_path = path
        self._model.set_materials(materials)

        categories = sorted({c for m in materials for c in (m.get("category") or [])})
        self._category.blockSignals(True)
        self._category.clear()
        self._category.addItem("All categories", None)
        for c in categories:
            self._category.addItem(c, c)
        self._category.blockSignals(False)

        self._on_filter_changed()
        self._fit_columns()
        return True

    def _fit_columns(self):
        """Fix Color, IOR and Density at the widest width they need in any IOR
        mode and either colour space (values and header), so nothing is clipped
        whatever font size Houdini's UI uses, and nothing jumps when switching."""
        model = self._model
        header = self._view.horizontalHeader()
        saved_mode, saved_space = model.mode(), model.color_space()

        opt = QtWidgets.QStyleOptionViewItem()
        opt.font = self._view.font()
        opt.fontMetrics = QtGui.QFontMetrics(opt.font)
        opt.rect = QtCore.QRect(0, 0, 2000, self._view.verticalHeader().defaultSectionSize())
        sort_arrow = 18  # room for the sort indicator when a column is sorted

        widths = dict.fromkeys((COL_COLOR, COL_MODE, COL_DENSITY), 0)
        model.blockSignals(True)  # measuring only; the view is restored below
        try:
            for _, space in COLOR_SPACES:
                for mode in MODES:
                    model.set_color_space(space)
                    model.set_mode(mode)
                    for col in widths:
                        delegate = self._view.itemDelegateForColumn(col) or self._view.itemDelegate()
                        width = header.sectionSizeHint(col) + sort_arrow
                        for row in range(model.rowCount()):
                            width = max(width, delegate.sizeHint(opt, model.index(row, col)).width())
                        widths[col] = max(widths[col], width)
        finally:
            model.set_color_space(saved_space)
            model.set_mode(saved_mode)
            model.blockSignals(False)
        for col, width in widths.items():
            header.resizeSection(col, width + 8)

    def _browse_json(self):
        start = os.path.dirname(self._json_path) if self._json_path else ""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open materials.json", start, "JSON (*.json)")
        if path:
            self.load(path)

    # -- reactions ---------------------------------------------------------
    def _on_filter_changed(self, *_):
        self._proxy.set_text(self._search.text())
        self._proxy.set_category(self._category.currentData())
        self._update_status()

    def _on_mode_changed(self, mode):
        self._model.set_mode(mode)
        self._proxy.invalidate()  # re-sort on the new values
        self._update_status()

    def _on_space_changed(self, *_):
        self._model.set_color_space(self._space.currentData())
        self._proxy.invalidate()  # luminance sort keys change with the space
        self._update_status()

    def _update_status(self):
        total = self._model.rowCount()
        shown = self._proxy.rowCount()
        mode = self._model.mode()
        with_value = sum(
            1 for r in range(shown)
            if self._proxy.index(r, COL_MODE).data(ROLE_RAW)[0] != KIND_NONE
        )
        src = os.path.basename(self._json_path) if self._json_path else "no file loaded"
        self._status.setText("{} of {} materials  ·  {} with {}  ·  {}  ·  {}".format(
            shown, total, with_value, mode, COLOR_SPACE_LABELS[self._model.color_space()], src))

    # -- copy --------------------------------------------------------------
    def _selected_rows(self):
        return sorted({i.row() for i in self._view.selectionModel().selectedIndexes()})

    def _row_text(self, proxy_row, columns=(COL_NAME, COL_MODE, COL_DENSITY)):
        return "\t".join(self._proxy.index(proxy_row, c).data() or "" for c in columns)

    def _copy_selection(self):
        rows = self._selected_rows()
        if rows:
            QtWidgets.QApplication.clipboard().setText(
                "\n".join(self._row_text(r) for r in rows))

    # -- references ----------------------------------------------------------
    def _on_double_clicked(self, index):
        click = self._copy_delegate.last_double_click
        pos, global_pos = click if click else (
            self._view.viewport().mapFromGlobal(QtGui.QCursor.pos()), QtGui.QCursor.pos())
        if self._copy_delegate.over_copy_icon(pos):
            return  # a double-click on a copy icon only copies
        self.show_references(index, global_pos)

    def show_references(self, index, global_pos):
        material = index.data(ROLE_MATERIAL)
        if material and material.get("references"):
            ReferencesPopup(material, self).popup_at(global_pos)

    def _context_menu(self, pos):
        index = self._view.indexAt(pos)
        if not index.isValid():
            return
        menu = QtWidgets.QMenu(self)
        refs = (index.data(ROLE_MATERIAL) or {}).get("references") or []
        act_refs = menu.addAction("References ({})…".format(len(refs)))
        act_refs.setEnabled(bool(refs))
        menu.addSeparator()
        segments = index.data(ROLE_SEGMENTS)
        if segments and len(segments) == 1:
            cell = segments[0][1]  # single RGB triplet -> same r,g,b form as the icon
        else:
            cell = index.data() or ""
        act_cell = menu.addAction("Copy value")
        act_row = menu.addAction("Copy row")
        global_pos = self._view.viewport().mapToGlobal(pos)
        chosen = menu.exec_(global_pos)
        if chosen is act_refs:
            self.show_references(index, global_pos)
        elif chosen is act_cell:
            QtWidgets.QApplication.clipboard().setText(cell)
        elif chosen is act_row:
            QtWidgets.QApplication.clipboard().setText(
                self._row_text(index.row(), (COL_NAME, COL_COLOR, COL_MODE, COL_DENSITY)))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

_window = None  # keep a reference so the window isn't garbage-collected


def _default_json_path():
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(here, "materials.json")
    return candidate if os.path.isfile(candidate) else None


WINDOW_OBJECT_NAME = "PhysicallyBasedBrowser"


def close_existing():
    """Close every open browser window and return how many were closed.

    Windows are found by object name rather than through _window, so this also
    catches windows from earlier imports of this module: importlib.reload()
    starts a fresh module whose _window is None, but the old window lives on.
    """
    closed = 0
    for widget in QtWidgets.QApplication.allWidgets():
        try:
            if widget.objectName() == WINDOW_OBJECT_NAME and widget.isWindow():
                widget.close()  # also closes its references popup, if open
                widget.deleteLater()
                closed += 1
        except RuntimeError:  # C++ object already deleted
            continue
    return closed


def show(json_path=None):
    """Open the browser window. Only one may exist: any open browser window,
    including one from an earlier (reloaded) version of this module, is closed
    before the new one opens."""
    global _window
    close_existing()
    parent = hou.qt.mainWindow() if hou else None
    _window = PhysicallyBasedBrowser(json_path or _default_json_path(), parent)
    _window.show()
    _window.raise_()
    _window.activateWindow()
    return _window


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = show(sys.argv[1] if len(sys.argv) > 1 else None)
    sys.exit(app.exec_())
