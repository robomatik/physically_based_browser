"""
Physically Based material browser for Houdini 19.5 (PySide2).

Shows the physicallybased.info material database (materials.json, schema v2.x)
in a sortable, searchable table:  Name | Color | <IOR mode> | Density

The third column switches between:
    Complex IOR, IOR, Specular, Specular Color, Specular Color (F82), Reflectance

Stored in the JSON:  complexIor, ior, specularColor (Gulbrandsen / F82)
Derived here:        Specular    = F0 / 0.08, with F0 = ((n-1)/(n+1))^2
                     Reflectance = F0 from ior for dielectrics,
                                   stored base color for metals

Usage (shelf tool / Python Shell):
    import physically_based_browser as pbb
    pbb.show()                       # looks for materials.json next to this file
    pbb.show("/path/to/materials.json")

Compatible with Python 3.7+ (Houdini 19.5 ships 3.7 and 3.9 builds).
"""

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

MODE_TOOLTIPS = {
    MODE_COMPLEX_IOR: 'Complex IOR specifies the color of a metal with an IOR value of "n", '
                      'and an extinction coefficient of "k".',
    MODE_IOR: "IOR is the index of refraction, which controls both the reflection, "
              "as well as the refraction, of a dielectric material.",
    MODE_SPECULAR: "Specular corresponds to an index of refraction value between 1.0-1.8, "
                   "remapped to a 0-1 range, where 0.5 is equivalent to an IOR of 1.5.\n"
                   "(Computed: F0 / 0.08)",
    MODE_SPECULAR_COLOR: "Specular Color specifies the color of a metallic material "
                         "when viewed at a 90° angle.",
    MODE_SPECULAR_F82: "Specular Color (F82) specifies the color of a metallic material "
                       "when viewed at an 82° angle.",
    MODE_REFLECTANCE: "Reflectance (F0) specifies the reflectance of a material "
                      "when viewed at a 0° angle.\n"
                      "(Computed from IOR for dielectrics; base color for metals)",
}

COLOR_SPACE = "srgb-linear"

# Value kinds returned by mode_value()
KIND_NONE = "none"
KIND_SCALAR = "scalar"
KIND_RGB = "rgb"
KIND_COMPLEX = "complex"


def load_materials(path):
    """Return the list of material dicts from a physicallybased materials.json."""
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if isinstance(payload, dict):
        data = payload.get("data", [])
    else:  # tolerate a bare list
        data = payload
    return [m for m in data if isinstance(m, dict) and m.get("name")]


def _pick_space(entries, space=COLOR_SPACE):
    """From a list of {'colorSpace': ...} entries return the one in `space`
    (falling back to the first entry), or None."""
    if not entries:
        return None
    for entry in entries:
        if entry.get("colorSpace") == space:
            return entry
    return entries[0]


def base_color(material):
    entry = _pick_space(material.get("color"))
    if entry and entry.get("color"):
        return tuple(float(c) for c in entry["color"][:3])
    return None


def is_metal(material):
    try:
        return float(material.get("metalness", 0)) >= 0.5
    except (TypeError, ValueError):
        return False


def _specular_color(material, fmt):
    for item in material.get("specularColor") or []:
        if fmt in (item.get("format") or []):
            entry = _pick_space(item.get("color"))
            if entry and entry.get("color"):
                return tuple(float(c) for c in entry["color"][:3])
    return None


def f0_from_ior(ior):
    """Normal-incidence reflectance of a dielectric against air."""
    return ((ior - 1.0) / (ior + 1.0)) ** 2


def specular_from_ior(ior):
    """Disney/UE 'specular': F0 / 0.08  ->  IOR 1.5 = 0.5, IOR ~1.8 = ~1.0."""
    return f0_from_ior(ior) / 0.08


def mode_value(material, mode):
    """Return (kind, value) for `material` under `mode`.

    scalar  -> float
    rgb     -> (r, g, b)   linear, may exceed 1.0
    complex -> {'n': (r, g, b), 'k': (r, g, b)}
    none    -> None        (mode does not apply to this material)
    """
    ior = material.get("ior")

    if mode == MODE_IOR:
        if ior is not None:
            return KIND_SCALAR, float(ior)

    elif mode == MODE_COMPLEX_IOR:
        entry = _pick_space(material.get("complexIor"))
        if entry and entry.get("n") and entry.get("k"):
            return KIND_COMPLEX, {
                "n": tuple(float(v) for v in entry["n"][:3]),
                "k": tuple(float(v) for v in entry["k"][:3]),
            }

    elif mode == MODE_SPECULAR:
        if ior is not None and not is_metal(material):
            return KIND_SCALAR, specular_from_ior(float(ior))

    elif mode == MODE_SPECULAR_COLOR:
        rgb = _specular_color(material, "Gulbrandsen")
        if rgb:
            return KIND_RGB, rgb

    elif mode == MODE_SPECULAR_F82:
        rgb = _specular_color(material, "F82")
        if rgb:
            return KIND_RGB, rgb

    elif mode == MODE_REFLECTANCE:
        if is_metal(material):
            rgb = base_color(material)
            if rgb:
                return KIND_RGB, rgb
        elif ior is not None:
            return KIND_SCALAR, f0_from_ior(float(ior))

    return KIND_NONE, None


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


def rgb_copy_text(rgb):
    """Clipboard form: comma separated, no spaces, full stored precision,
    e.g. '0.728978,0.588215,0.78354'."""
    return ",".join(raw_number(c) for c in rgb)


def format_mode_value(kind, value):
    if kind == KIND_SCALAR:
        return format_number(value, 4)
    if kind == KIND_RGB:
        return format_rgb(value)
    if kind == KIND_COMPLEX:
        return "n: {}   k: {}".format(format_rgb(value["n"]), format_rgb(value["k"]))
    return "—"


def mode_segments(kind, value):
    """[(display_text, copy_text), ...] for cells that get copy icons, else None."""
    if kind == KIND_RGB:
        return [(format_rgb(value), rgb_copy_text(value))]
    if kind == KIND_COMPLEX:
        return [("n: " + format_rgb(value["n"]), rgb_copy_text(value["n"])),
                ("k: " + format_rgb(value["k"]), rgb_copy_text(value["k"]))]
    return None


def mode_sort_key(kind, value):
    """Numeric key for sorting; None sorts after everything."""
    if kind == KIND_SCALAR:
        return value
    if kind == KIND_RGB:
        return luminance(value)
    if kind == KIND_COMPLEX:  # sort by resulting F0 brightness
        return luminance(tuple(f0_complex(n, k) for n, k in zip(value["n"], value["k"])))
    return None


def f0_complex(n, k):
    return ((n - 1.0) ** 2 + k * k) / ((n + 1.0) ** 2 + k * k)


def luminance(rgb):
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def linear_to_srgb8(c):
    """Linear -> sRGB-encoded 0-255, clamped (values above 1.0 display as 1.0)."""
    c = max(0.0, min(1.0, float(c)))
    s = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055
    return int(round(s * 255.0))


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

ROLE_SORT = QtCore.Qt.UserRole + 1
ROLE_SEGMENTS = QtCore.Qt.UserRole + 2  # [(display_text, copy_text), ...] for copy icons
ROLE_MATERIAL = QtCore.Qt.UserRole + 3  # the raw material dict
ROLE_RAW = QtCore.Qt.UserRole + 4       # (kind, value) for the mode column


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
        self._cache = {}
        self._swatches = {}  # rgb tuple -> QIcon

    # -- API ---------------------------------------------------------------
    def set_materials(self, materials):
        self.beginResetModel()
        self._materials = list(materials)
        self._cache = {}
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

    def _value(self, row):
        if row not in self._cache:
            self._cache[row] = mode_value(self._materials[row], self._mode)
        return self._cache[row]

    # -- Qt overrides ------------------------------------------------------
    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self._materials)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else 4

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if orientation != QtCore.Qt.Horizontal:
            return None
        if role == QtCore.Qt.DisplayRole:
            return ("Name", "Color", self._mode, "Density (kg/m³)")[section]
        if role == QtCore.Qt.ToolTipRole and section == COL_MODE:
            return MODE_TOOLTIPS.get(self._mode)
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
                return mat.get("description") or None

        elif col == COL_COLOR:
            rgb = base_color(mat)
            if role == ROLE_SEGMENTS:
                return [(format_rgb(rgb), rgb_copy_text(rgb))] if rgb else None
            if role == QtCore.Qt.DisplayRole:
                return format_rgb(rgb) if rgb else "—"
            if role == ROLE_SORT:
                return luminance(rgb) if rgb else None
            if role == QtCore.Qt.ToolTipRole and rgb:
                return "Linear sRGB"
            if role == QtCore.Qt.TextAlignmentRole:
                return int(QtCore.Qt.AlignCenter)

        elif col == COL_MODE:
            kind, value = self._value(row)
            if role == ROLE_RAW:
                return kind, value
            if role == ROLE_SEGMENTS:
                return mode_segments(kind, value)
            if role == QtCore.Qt.DisplayRole:
                return format_mode_value(kind, value)
            if role == ROLE_SORT:
                return mode_sort_key(kind, value)
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
        view.setMouseTracking(True)
        view.viewport().installEventFilter(self)

    # -- geometry ----------------------------------------------------------
    def _layout(self, rect, fm, index):
        """[(label, copy_text, text_rect, icon_rect), ...] for the cell."""
        segments = index.data(ROLE_SEGMENTS)
        if not segments:
            return []
        line_h = self._line_height(fm)
        top = rect.center().y() - (line_h * len(segments)) // 2 + 1
        # measure with every digit as '0' so narrow digits (e.g. '1') don't shift
        # the icon column from row to row
        text_w = max(max(fm.horizontalAdvance(label),
                         fm.horizontalAdvance("".join("0" if ch.isdigit() else ch for ch in label)))
                     for label, _ in segments)
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

    def _hit(self, rect, fm, index, pos):
        for i, (_, copy_text, _, icon_rect) in enumerate(self._layout(rect, fm, index)):
            if icon_rect.adjusted(-3, -3, 3, 3).contains(pos):
                return i, copy_text
        return None, None

    # -- painting ----------------------------------------------------------
    def paint(self, painter, option, index):
        layout = self._layout(option.rect, option.fontMetrics, index)
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

        painter.save()
        painter.setClipRect(opt.rect)
        for i, (label, _, text_rect, icon_rect) in enumerate(layout):
            painter.setPen(fg)
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
        layout = self._layout(option.rect, option.fontMetrics, index)
        if layout:
            text_w = layout[0][2].width()
            hint.setWidth(text_w + self.GAP + self.ICON + 2 * self.PAD + 2)
            hint.setHeight(self._line_height(option.fontMetrics) * len(layout) + 2 * self.V_MARGIN)
        return hint

    # -- interaction -------------------------------------------------------
    def _set_hover(self, hover):
        if hover != self._hover:
            self._hover = hover
            cursor = QtCore.Qt.PointingHandCursor if hover else QtCore.Qt.ArrowCursor
            self._view.viewport().setCursor(cursor)
            self._view.viewport().update()

    def eventFilter(self, obj, event):
        """Hover tracking (item views don't forward mouse moves to delegates)."""
        etype = event.type()
        if etype == QtCore.QEvent.MouseMove:
            pos = event.pos()
            index = self._view.indexAt(pos)
            hover = None
            if index.isValid() and self._view.itemDelegateForColumn(index.column()) is self:
                seg, _ = self._hit(self._view.visualRect(index), self._view.fontMetrics(), index, pos)
                if seg is not None:
                    hover = (index.row(), index.column(), seg)
            self._set_hover(hover)
        elif etype == QtCore.QEvent.Leave:
            self._set_hover(None)
        return False

    def editorEvent(self, event, model, option, index):
        etype = event.type()
        if etype in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease,
                     QtCore.QEvent.MouseButtonDblClick):
            seg, copy_text = self._hit(option.rect, option.fontMetrics, index, event.pos())
            if seg is not None:
                if etype == QtCore.QEvent.MouseButtonRelease and event.button() == QtCore.Qt.LeftButton:
                    QtWidgets.QApplication.clipboard().setText(copy_text)
                    QtWidgets.QToolTip.showText(event.globalPos(), "Copied  " + copy_text, self._view)
                    self.copied.emit(copy_text)
                return True  # don't let icon clicks change selection / start edits
        return super(CopyValuesDelegate, self).editorEvent(event, model, option, index)


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

class PhysicallyBasedBrowser(QtWidgets.QWidget):

    def __init__(self, json_path=None, parent=None):
        super(PhysicallyBasedBrowser, self).__init__(parent)
        self.setWindowFlags(QtCore.Qt.Window)
        self.setWindowTitle("Physically Based Materials")
        self.setObjectName("PhysicallyBasedBrowser")
        self.resize(920, 640)

        self._json_path = None

        # widgets
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Search name, tag or category…")
        self._search.setClearButtonEnabled(True)

        self._category = QtWidgets.QComboBox()
        self._category.setToolTip("Filter by category")

        self._mode = QtWidgets.QComboBox()
        self._mode.addItems(MODES)
        for i, m in enumerate(MODES):
            self._mode.setItemData(i, MODE_TOOLTIPS[m], QtCore.Qt.ToolTipRole)
        self._mode.setCurrentText(MODE_COMPLEX_IOR)
        self._mode.setToolTip("Value shown in the third column")

        self._open_btn = QtWidgets.QToolButton()
        self._open_btn.setText("Open JSON…")
        self._open_btn.setToolTip("Load a different materials.json")

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

        header = self._view.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QtWidgets.QHeaderView.Interactive)
        header.setSectionResizeMode(COL_COLOR, QtWidgets.QHeaderView.Interactive)
        header.setSectionResizeMode(COL_MODE, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(COL_DENSITY, QtWidgets.QHeaderView.Interactive)
        header.resizeSection(COL_NAME, 190)
        header.resizeSection(COL_COLOR, 150)
        header.resizeSection(COL_DENSITY, 140)

        self._status = QtWidgets.QLabel()
        self._status.setStyleSheet("color: gray;")

        # layout
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self._search, 1)
        top.addWidget(self._category)
        top.addWidget(QtWidgets.QLabel("IOR mode:"))
        top.addWidget(self._mode)
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
        self._open_btn.clicked.connect(self._browse_json)
        self._view.customContextMenuRequested.connect(self._context_menu)

        shortcut_cls = getattr(QtWidgets, "QShortcut", None) or QtGui.QShortcut  # PySide6 moved it
        copy_sc = shortcut_cls(QtGui.QKeySequence.Copy, self._view)
        copy_sc.activated.connect(self._copy_selection)

        if json_path:
            self.load(json_path)
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
        return True

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

    def _update_status(self):
        total = self._model.rowCount()
        shown = self._proxy.rowCount()
        mode = self._model.mode()
        with_value = sum(
            1 for r in range(shown)
            if self._proxy.index(r, COL_MODE).data(ROLE_RAW)[0] != KIND_NONE
        )
        src = os.path.basename(self._json_path) if self._json_path else "no file loaded"
        self._status.setText("{} of {} materials  ·  {} with {}  ·  {}".format(
            shown, total, with_value, mode, src))

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

    def _context_menu(self, pos):
        index = self._view.indexAt(pos)
        if not index.isValid():
            return
        menu = QtWidgets.QMenu(self)
        segments = index.data(ROLE_SEGMENTS)
        if segments and len(segments) == 1:
            cell = segments[0][1]  # single RGB triplet -> same r,g,b form as the icon
        else:
            cell = index.data() or ""
        act_cell = menu.addAction("Copy value")
        act_row = menu.addAction("Copy row")
        chosen = menu.exec_(self._view.viewport().mapToGlobal(pos))
        if chosen is act_cell:
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


def show(json_path=None):
    """Open (or raise) the browser window."""
    global _window
    parent = hou.qt.mainWindow() if hou else None

    if _window is not None:
        try:
            if json_path:
                _window.load(json_path)
            _window.show()
            _window.raise_()
            _window.activateWindow()
            return _window
        except RuntimeError:  # underlying C++ object was deleted
            _window = None

    _window = PhysicallyBasedBrowser(json_path or _default_json_path(), parent)
    _window.show()
    return _window


if __name__ == "__main__":
    import sys
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = show(sys.argv[1] if len(sys.argv) > 1 else None)
    sys.exit(app.exec_())
