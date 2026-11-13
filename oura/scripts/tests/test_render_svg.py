import pathlib
import unittest
from html.parser import HTMLParser
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import render


class EscTest(unittest.TestCase):
    def test_escapes_tag_and_quote(self):
        self.assertEqual(render.esc('<script>alert("x")</script>'),
                         "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;")

    def test_escapes_handles_none(self):
        self.assertEqual(render.esc(None), "None")

    def test_escapes_ampersand_first(self):
        self.assertEqual(render.esc("a & <b>"), "a &amp; &lt;b&gt;")


class NoExternalRefParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bad = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("href", "src", "xlink:href") and value and not value.startswith("#"):
                self.bad.append((tag, key, value))


class SvgTest(unittest.TestCase):
    def test_stacked_phases_four_colors(self):
        svg = render.svg_stacked_phases("4411222331")
        self.assertTrue(svg.startswith("<svg"))
        self.assertEqual(svg.count("<rect"), 10)
        for color in ("#3b82f6", "#64748b", "#a78bfa", "#f5a524"):
            self.assertIn(color, svg)

    def test_line_has_polyline_and_no_script(self):
        svg = render.svg_line([("11-09", 82), ("11-10", 77), ("11-11", 90)])
        self.assertIn("<polyline", svg)
        self.assertNotIn("<script", svg)

    def test_bars_have_rects(self):
        svg = render.svg_bars([("11-09", 7.0), ("11-10", 6.5)])
        self.assertEqual(svg.count("<rect"), 2)

    def test_empty_input_produces_placeholder_not_error(self):
        self.assertIn("<svg", render.svg_line([]))
        self.assertIn("<svg", render.svg_bars([]))
        self.assertIn("<svg", render.svg_stacked_phases(""))

    def test_svg_has_no_external_reference(self):
        for svg in (render.svg_stacked_phases("441122"),
                    render.svg_line([("d", 1)]),
                    render.svg_bars([("d", 1)])):
            parser = NoExternalRefParser()
            parser.feed(svg)
            self.assertEqual(parser.bad, [])

    def test_unknown_phase_char_does_not_crash(self):
        svg = render.svg_stacked_phases("44?11")
        self.assertTrue(svg.startswith("<svg"))

    def test_line_handles_single_point(self):
        self.assertIn("<svg", render.svg_line([("11-09", 82)]))


if __name__ == "__main__":
    unittest.main()