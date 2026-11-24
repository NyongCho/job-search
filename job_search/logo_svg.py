"""Rasterize small, solid-color path icons from official employer pages."""

import re
from xml.etree import ElementTree

_NAMESPACE = "{http://www.w3.org/2000/svg}"
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def render_icon_svg(raw):
    if not raw or len(raw) > 32_768 or not raw.isascii() or any(s in raw for s in (b"\0", b"<!", b"<?", b"&")):
        return None
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return None
    if (root.tag != _NAMESPACE + "svg" or root.attrib.keys() - {"width", "height", "viewBox", "fill"}
            or not 1 <= len(root) <= 8 or (root.text or "").strip()):
        return None
    width, height = root.get("width", ""), root.get("height", "")
    if (not all(re.fullmatch(r"[1-9][0-9]{0,2}", value) for value in (width, height))
            or root.get("viewBox", "").split() != ["0", "0", width, height]
            or not 0.5 <= int(width) / int(height) <= 2):
        return None
    base_fill = root.get("fill", "#000000")
    if base_fill != "none" and not _COLOR.fullmatch(base_fill):
        return None

    paths = []
    coordinates = 0
    for path in root:
        if (path.tag != _NAMESPACE + "path" or path.attrib.keys() - {"d", "fill"}
                or len(path) or (path.text or "").strip() or (path.tail or "").strip()):
            return None
        data = path.get("d", "")
        fill = path.get("fill", base_fill)
        if not _COLOR.fullmatch(fill) or not re.fullmatch(r"M[MLHVCQSZT0-9.+,\s-]+", data):
            return None
        numbers = _NUMBER.findall(data)
        coordinates += len(numbers)
        if coordinates > 2048 or any(abs(float(value)) > 4096 for value in numbers):
            return None
        paths.append(f'<path d="{data}" fill="{fill}"/>')

    # Build fresh SVG from bounded geometry, so the renderer only sees paths and literal colors.
    from resvg_py import svg_to_bytes

    scale = 128 / max(int(width), int(height))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{round(int(width) * scale)}" '
           f'height="{round(int(height) * scale)}" viewBox="0 0 {width} {height}">{"".join(paths)}</svg>')
    try:
        return svg_to_bytes(svg_string=svg, skip_system_fonts=True)
    except ValueError:
        return None
