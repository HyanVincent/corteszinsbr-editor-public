"""Regression tests for the Gabepeixe facecam framing issue.

The screenshot showed the face cut by the old 153px camera crop.
Avoid shifting any other streamer's existing framing.
"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "pipeline", Path(__file__).resolve().parents[1] / "src" / "pipeline.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_gabepeixe_webcam_captures_entire_face():
    for layout in ("gabepeixe_reaction", "gabepeixe_gtavi"):
        camera, gameplay, y_camera, y_game = m.REACTION_REGIONS[layout]
        width, height, x, y = (int(v) for v in camera.split(":"))
        assert (width, x) == (262, 808)
        assert height >= 195, "Do not restore the old cropped 153px facecam"
        assert y + height <= 608
        assert x + width <= 1080
        scaled_height = height * 900 / width
        assert y_camera + scaled_height < y_game


def test_gabepeixe_branding_below_webcam():
    camera, _, top, _ = m.REACTION_REGIONS["gabepeixe_gtavi"]
    w, h, _, _ = (int(v) for v in camera.split(":"))
    filters = ";".join(m.reaction_filters("gabepeixe_gtavi", Path("credit.txt")))
    assert "[reaction][lower]overlay=(W-w)/2:840" in filters
    assert top + (h * 900 / w) < 840


def test_other_creator_layouts_are_unchanged():
    assert m.REACTION_REGIONS["allan_reaction"] == ("264:178:812:228", "804:540:0:34", 163, 790)
    assert m.REACTION_REGIONS["davyjones_reaction"] == ("236:138:844:55", "1080:412:0:196", 163, 724)
    assert m.REACTION_REGIONS["brabox_reaction"] == ("284:164:796:96", "790:558:0:40", 163, 715)
    assert m.REACTION_REGIONS["coringa_reaction"] == ("266:172:0:234", "698:504:270:104", 163, 766)
