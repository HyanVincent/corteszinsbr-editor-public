"""Regression: Allan webcam crops must not turn gameplay into a fake facecam."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "pipeline", Path(__file__).resolve().parents[1] / "src" / "pipeline.py"
)
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def test_allan_has_webcam_guard():
    assert "allan_reaction" in pipeline.WEBCAM_EDGES
    x, y, h = pipeline.WEBCAM_EDGES["allan_reaction"]
    cam = pipeline.REACTION_REGIONS["allan_reaction"][0]
    w, cam_h, cam_x, cam_y = map(int, cam.split(":"))
    assert cam_x <= x < cam_x + w
    assert cam_y <= y < cam_y + cam_h
    assert y + h <= cam_y + cam_h


def test_allan_without_webcam_falls_back_to_full_frame(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"streams":[{"width":1920,"height":1080}]}'
        ),
    )
    checked = []
    monkeypatch.setattr(
        pipeline, "has_webcam_edge",
        lambda media, edge, start: checked.append(edge) or False
    )
    assert pipeline.editorial_layout(tmp_path / "allan.mp4", "allanstag", [], 0) == "standard"
    assert checked == [pipeline.WEBCAM_EDGES["allan_reaction"]]


def test_allan_with_webcam_keeps_reaction(monkeypatch, tmp_path):
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout='{"streams":[{"width":1920,"height":1080}]}'
        ),
    )
    monkeypatch.setattr(pipeline, "has_webcam_edge", lambda *args: True)
    assert pipeline.editorial_layout(tmp_path / "allan.mp4", "allanstag", [], 0) == "allan_reaction"
