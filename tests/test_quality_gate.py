import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "quality_gate", Path(__file__).resolve().parents[1] / "src" / "quality_gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def item(mode="auto"):
    return {
        "file": "corte_01.mp4",
        "source_channel": "allanstag",
        "input_url": "https://kick.com/allanstag/clips/clip_test123",
        "source_start_seconds": 0,
        "source_end_seconds": 24,
        "score_heuristic": 3.2,
        "subtitle_mode": mode,
        "subtitle_burned": mode == "auto",
    }


def metadata():
    return {
        "width": 1080, "height": 1920, "codec": "h264", "fps": 30.0,
        "duration": 24.0, "size_bytes": 2300000, "has_audio": True,
    }


def make_sample(tmp_path, monkeypatch):
    folder = tmp_path / "output"
    folder.mkdir()
    (folder / "corte_01.mp4").write_bytes(b"video-for-mocked-probe")
    (folder / "corte_01_fonte.txt").write_text("KICK.COM/ALLANSTAG")
    (folder / "corte_01.ass").write_text("[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Streamer,,0,0,0,,Olá")
    monkeypatch.setattr(gate, "probe", lambda path: metadata())
    monkeypatch.setattr(gate, "mean_volume_db", lambda path: -20.0)
    return folder


def test_good_technical_candidate(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    result = gate.assess(item(), folder, set())
    assert result["status"] == "CANDIDATO_TECNICO"
    assert not result["reasons"]
    assert result["auto_published"] is False
    assert result["editorial_approved"] is False


def test_missing_burned_subtitles_require_review(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    (folder / "corte_01.ass").unlink()
    result = gate.assess(item("auto"), folder, set())
    assert result["status"] == "REVISAO_NECESSARIA"
    assert any("Legenda não embutida" in reason for reason in result["reasons"])
    assert result["captions_burned"] is False


def test_separate_srt_only_no_longer_qualifies(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    result = gate.assess(item("review"), folder, set())
    assert result["status"] == "REVISAO_NECESSARIA"
    assert any("sem legenda embutida" in reason for reason in result["reasons"])


def test_embedded_game_dialogue_does_not_block_technical_candidate(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    result = gate.assess(item("auto"), folder, set())
    assert result["status"] == "CANDIDATO_TECNICO"
    assert result["captions_burned"] is True
    assert result["captions_unverified"] is True
    assert result["editorial_approved"] is False


def test_clips_slightly_longer_than_sixty_seconds_are_accepted(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    long = metadata()
    long["duration"] = 60.2
    monkeypatch.setattr(gate, "probe", lambda path: long)
    full_clip = item("auto")
    full_clip["source_end_seconds"] = 60.2
    assert gate.assess(full_clip, folder, set())["status"] == "CANDIDATO_TECNICO"
    long["duration"] = 180.2
    full_clip["source_end_seconds"] = 180.0
    assert gate.assess(full_clip, folder, set())["status"] == "REVISAO_NECESSARIA"



def test_full_three_minute_clip_passes_duration_and_source_span(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    long = metadata()
    long["duration"] = 179.8
    long["size_bytes"] = 160000000
    monkeypatch.setattr(gate, "probe", lambda path: long)
    full = item("auto")
    full["source_end_seconds"] = 179.8
    result = gate.assess(full, folder, set())
    assert result["status"] == "CANDIDATO_TECNICO", result["reasons"]
    assert result["auto_published"] is False

    too_long = dict(full)
    long["duration"] = 180.6
    result = gate.assess(too_long, folder, set())
    assert result["status"] == "REVISAO_NECESSARIA"
    assert any("Duração fora do padrão" in r for r in result["reasons"])


def test_bad_audio_or_bad_credit_require_review(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "mean_volume_db", lambda path: -60.0)
    (folder / "corte_01_fonte.txt").write_text("KICK.COM/CORINGA")
    result = gate.assess(item(), folder, set())
    assert result["status"] == "REVISAO_NECESSARIA"
    assert len(result["reasons"]) >= 2


def test_process_routes_files_without_drive(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    (folder / "cortes.json").write_text(json.dumps([item()]))
    (folder / "corte_01_sugestoes.md").write_text("# Título para Shorts")
    (folder / "corte_01_sugestoes.json").write_text(json.dumps({
        "title": "Trecho da live", "needs_editorial_review": True
    }))
    result = gate.process(folder, tmp_path / "qa")
    assert result["technical_candidates"] == 1
    assert result["auto_published"] == 0
    assert (tmp_path / "qa" / "prontos" / "corte_01.mp4").exists()
    assert (tmp_path / "qa" / "prontos" / "corte_01_qa.json").exists()
    assert (tmp_path / "qa" / "prontos" / "corte_01_sugestoes.md").exists()
    assert (tmp_path / "qa" / "prontos" / "corte_01_sugestoes.json").exists()
    assert not (tmp_path / "qa" / "revisar").exists()



def test_shortened_original_clip_is_not_accepted_as_complete(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    original_clip = item("auto")
    original_clip["source_end_seconds"] = 45.0
    result = gate.assess(original_clip, folder, set())
    assert result["status"] == "REVISAO_NECESSARIA"
    assert any("não preservou a duração" in r for r in result["reasons"])


def test_clip_duration_small_encoding_delta_is_tolerated(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    input_clip = item("auto")
    input_clip["source_end_seconds"] = 24.4
    result = gate.assess(input_clip, folder, set())
    assert result["status"] == "CANDIDATO_TECNICO"


def test_gabepeixe_low_score_only_routes_to_editorial_drive(tmp_path, monkeypatch):
    folder = make_sample(tmp_path, monkeypatch)
    (folder / "corte_01_fonte.txt").write_text("KICK.COM/GABEPEIXE")
    gabe = {**item(), "source_channel": "gabepeixe",
            "input_url": "https://kick.com/gabepeixe/clips/clip_test123",
            "score_heuristic": gate.RULES["min_heuristic_score"] - 1}
    (folder / "cortes.json").write_text(json.dumps([gabe]))
    qa = tmp_path / "qa"
    decision = gate.process(folder, qa)["decisions"][0]
    assert decision["reasons"] == [gate.LOW_SCORE_REASON]
    assert (qa / "gabepeixe_editorial" / "corte_01.mp4").is_file()
    assert (qa / "revisar" / "corte_01.mp4").is_file()
    assert not (qa / "prontos").exists()

    (folder / "corte_01.ass").unlink()
    second_qa = tmp_path / "qa-with-technical-failure"
    gate.process(folder, second_qa)
    assert not (second_qa / "gabepeixe_editorial").exists()
