import io
import unittest

from PIL import Image

from job_search.logo_svg import render_icon_svg


def icon(paths, dimensions='width="16" height="15" viewBox="0 0 16 15"'):
    return f'<svg xmlns="http://www.w3.org/2000/svg" {dimensions} fill="none">{paths}</svg>'.encode()


GREEN = '<path d="M2 2H14V13H2Z" fill="#00dc64"/>'


class IconSVGTests(unittest.TestCase):
    def test_renders_a_solid_path_and_preserves_transparency_and_proportions(self):
        data = render_icon_svg(icon(GREEN))
        self.assertIsNotNone(data)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (128, 120))
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertEqual(image.getpixel((64, 60)), (0, 220, 100, 255))

    def test_rejects_markup_or_paint_that_can_load_resources_or_execute(self):
        for extra in ('<image href="asset.png"/>', '<script/>', '<use href="#part"/>',
                      '<path d="M1 1" fill="url(#paint)"/>', '<style/>',
                      '<path d="M1 1" onclick="doSomething()"/>'):
            with self.subTest(extra=extra):
                self.assertIsNone(render_icon_svg(icon(GREEN + extra)))
        for prefix in (b'<!DOCTYPE svg>', b'<?xml-stylesheet href="theme.css"?>'):
            with self.subTest(prefix=prefix):
                self.assertIsNone(render_icon_svg(prefix + icon(GREEN)))
        self.assertIsNone(render_icon_svg(icon(GREEN).replace(b'#00dc64', b'&#35;00dc64')))

    def test_rejects_large_geometry_and_complex_documents(self):
        for svg in (
            b'<svg>',
            icon(GREEN, 'width="20000" height="20000" viewBox="0 0 20000 20000"'),
            icon(GREEN.replace('M2 2', 'M900000 2')),
            icon(GREEN + '<path d="M0 0' + 'L1 1' * 3000 + '" fill="#000000"/>'),
            icon(GREEN * 1000),
        ):
            with self.subTest(length=len(svg), prefix=svg[:40]):
                self.assertIsNone(render_icon_svg(svg))
