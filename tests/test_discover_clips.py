import importlib.util
import json
from pathlib import Path
import subprocess

spec = importlib.util.spec_from_file_location(
    "discover_clips", Path(__file__).resolve().parents[1] / "src" / "discover_clips.py"
)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


def clip(name, channel, view=0, timestamp=0, reason="date/day", length=32):
    return {
        "id": "clip_" + name * 16, "channel": channel,
        "url": f"https://kick.com/{channel}/clips/clip_{name * 16}",
        "title": "Exemplo", "views": view, "timestamp": timestamp,
        "duration_seconds": length, "discovered_from": reason,
        "age_restricted": False, "needs_content_review": True,
    }


def test_validates_channel_and_sort():
    for slug, sort, period in [("bad", "date", "day"),
                                ("allanstag", "invalid", "day"),
                                ("allanstag", "date", "all")]:
        try:
            d.list_clips(slug, sort, period)
        except ValueError:
            pass
        else:
            raise AssertionError((slug, sort, period))


def test_extracts_canonical_url_and_metadata(monkeypatch):
    payload = {"entries": [
        {"id": "clip_01M7Q7XH7360MXFK16DH38VBKT", "title": "um momento",
         "view_count": 750, "timestamp": 1780000000, "duration": 38,
         "channel_id": 101},
        {"id": "something-dangerous", "title": "skip"},
    ]}
    def runner(args, **kwargs):
        assert "--flat-playlist" in args
        assert "sort=view&range=week" in args[-1]
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
    monkeypatch.setattr(d.subprocess, "run", runner)
    rows = d.list_clips("allanstag", "view", "week")
    assert len(rows) == 1
    assert rows[0]["url"] == "https://kick.com/allanstag/clips/clip_01M7Q7XH7360MXFK16DH38VBKT"
    assert rows[0]["views"] == 750


def test_choose_both_modes_and_no_duplicates():
    recent = clip("A", "allanstag", 100, 1800000030, "date/day")
    popular = clip("B", "davyjones", 15000, 1799990000, "view/week")
    blocked = clip("C", "brabox", 30000, 1800002000, "view/week")
    blocked["age_restricted"] = True
    duplicate = dict(recent)
    selected = d.choose({"allanstag:date": [recent], "davyjones:view": [popular, duplicate, blocked]},
                        set(), 4)
    assert len(selected) == 2
    assert {x["selection_reason"] for x in selected} == {"recent", "most_viewed"}
    assert all(x["needs_content_review"] for x in selected)
    assert d.choose({"allanstag:date": [recent]}, {recent["id"]}) == []


def test_discovery_persists_ids_and_does_not_store_media(tmp_path, monkeypatch):
    sample = clip("D", "allanstag", 500, 1800000000)
    def discover(slug, sort, period, limit=10):
        return [sample] if (slug, sort) == ("allanstag", "date") else []
    monkeypatch.setattr(d, "list_clips", discover)
    state = tmp_path / "seen.json"
    out = tmp_path / "report"
    first = d.run_discovery(out, state)
    assert first["candidates_count"] == 1
    assert first["auto_published"] is False
    assert (out / "candidates.json").exists()
    assert (out / "candidates.md").exists()
    assert not list(out.glob("*.mp4"))
    second = d.run_discovery(out, state)
    assert second["candidates_count"] == 0


def test_discovery_fails_closed_when_all_sources_fail(tmp_path, monkeypatch):
    def bad(*args, **kwargs):
        raise RuntimeError("Kick indisponivel")
    monkeypatch.setattr(d, "list_clips", bad)
    try:
        d.run_discovery(tmp_path / "output", tmp_path / "seen.json")
    except RuntimeError as exc:
        assert "Nenhuma listagem" in str(exc)
    else:
        raise AssertionError("Must fail rather than silently report zero new clips")
