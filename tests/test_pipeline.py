import importlib.util
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location("pipeline", Path(__file__).resolve().parents[1]/"src/pipeline.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def test_urls():
    assert m.valid_kick_link("https://kick.com/brabox/videos/abc")[0] == "brabox"
    for url in ["https://kick.com/kaicenat/videos/foo", "http://kick.com/brabox/videos/foo", 
                "https://www.twitch.tv/brino", "https://kick.com/brabox"]:
        try: m.valid_kick_link(url)
        except ValueError: pass
        else: raise AssertionError(url)

def test_windows():
    w = m.choose_windows(90,[0.2]*90,[],3)
    assert 1 <= len(w) <= 3
    assert all(0 <= x[0] < x[1] <= 90 for x in w)

def test_subtitles(tmp_path):
    out=tmp_path/"test.srt"
    assert m.write_srt([{"start":3,"end":5,"text":"Olá!"}],3,9,out)
    assert "00:00:00,000 --> 00:00:02,000" in out.read_text()


def test_short_caption_style(tmp_path):
    segs=[{"start":0.0,"end":2.0,"text":"Um dois tres quatro cinco seis sete",
           "words":[{"start":i*.25,"end":(i+1)*.25,"word":word}
                    for i,word in enumerate("Um dois tres quatro cinco seis sete".split())]}]
    chunks=m.subtitle_chunks(segs,0,2)
    assert len(chunks)>1
    assert all(len(chunk["text"].split())<=4 and len(chunk["text"])<=26 for chunk in chunks)
    out=tmp_path/"new.ass"
    assert m.write_ass(segs,0,2,out)
    script=out.read_text()
    assert "PlayResX: 1080" in script
    assert "PlayResY: 1920" in script
    assert "Fontsize" in script
    assert "Style: Streamer,DejaVu Sans,44," in script


def test_render_uses_blurred_background(tmp_path, monkeypatch):
    calls=[]
    monkeypatch.setattr(m,"run",lambda argv,**kw: calls.append(argv))
    dummy=tmp_path/"sample.mp4"
    m.render(dummy,0,2,"allanstag",[],tmp_path/"result",1)
    cmd=calls[0]
    filter_script=cmd[cmd.index("-filter_complex")+1]
    assert "boxblur=40:2" in filter_script
    assert "overlay=x=(W-w)/2:y=(H-h)/2" in filter_script
    assert "pad=1080:1920" not in filter_script
    assert "-map" in cmd


def test_kick_creator_credit(tmp_path, monkeypatch):
    for channel in m.SLUGS:
        assert m.creator_credit(channel) == f"KICK.COM/{channel.upper()}"
    assert m.creator_credit("local") == ""
    calls=[]
    monkeypatch.setattr(m, "run", lambda argv, **kw: calls.append(argv))
    m.render(tmp_path/"source.mp4", 0, 1, "davyjones", [], tmp_path/"out", 1)
    assert (tmp_path/"out"/"corte_01_fonte.txt").read_text() == "KICK.COM/DAVYJONES"
    assert not (tmp_path/"out"/"corte_01_titulo.txt").exists()
    cmd = calls[0]
    graph = cmd[cmd.index("-filter_complex")+1]
    assert "fontsize=34" in graph
    assert "boxblur=40:2" in graph
    assert "drawtext=" in graph
    assert "@Corteszinsbr" not in graph
    assert "enable='between" not in graph


def test_subtitle_auto_keeps_source_credit(tmp_path, monkeypatch):
    calls=[]
    monkeypatch.setattr(m, "run", lambda argv, **kw: calls.append(argv))
    m.render(tmp_path/"source.mp4", 0, 2, "allanstag",
             [{"start":0,"end":1,"text":"olha só"}], tmp_path/"out", 1,
             subtitle_mode="auto")
    graph=calls[0][calls[0].index("-filter_complex")+1]
    assert "]ass='" in graph
    assert "textfile=" in graph



def test_direct_kick_clip_keeps_full_duration_to_three_minutes():
    assert m.MAX_OUTPUT_SEC == 180
    for length in (18.0, 24.0, 35.0, 59.9, 60.0, 60.1, 74.0, 91.5, 120.0, 179.9, 180.0):
        windows = m.full_source_clip_window(length, [0.3] * 181, [])
        assert len(windows) == 1
        assert windows[0][0] == 0.0
        assert windows[0][1] == length
        assert windows[0][2] >= 2.0
    assert m.full_source_clip_window(210.0, [0.3] * 210, [])[0][1] == 180.0


def test_vod_windows_maximum_three_minutes():
    windows = m.choose_windows(320, [0.3] * 320, [], count=3)
    assert 1 <= len(windows) <= 3
    assert all(0 < end - start <= 180.0 for start, end, _ in windows)
    short = m.choose_windows(70, [0.3] * 70, [], count=1)
    assert short == [(0.0, 70.0, 2.5)]


def test_default_render_burns_game_dialogue_and_caps_duration(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(m, "run", lambda argv, **kw: calls.append(argv))
    segs = [
        {"start": 1, "end": 3, "text": "fala do jogo"},
        {"start": 4, "end": 6, "text": "fala do streamer"},
    ]
    result = tmp_path / "out"
    m.render(tmp_path / "source.mp4", 0, 60, "davyjones",
             segs, result, 1)
    cmd = calls[0]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "]ass='" in graph
    assert "-t" in cmd and float(cmd[cmd.index("-t") + 1]) == 60.0
    ass = (result / "corte_01.ass").read_text()
    # Captions are intentionally broken into short readable chunks.
    assert "jogo" in ass and "streamer" in ass
    assert ass.count("Dialogue: 0,") >= 2
    assert (result / "corte_01.srt").exists()



def test_direct_kick_cli_ignores_stale_sixty_second_window(tmp_path, monkeypatch):
    """Legacy/manual --window-sec=60 must not cut a Kick clip before its real end."""
    import json
    from pathlib import Path
    calls = []
    monkeypatch.setattr(m, "ROOT", tmp_path)
    monkeypatch.setattr(m, "fetch_excerpt", lambda url, start, duration, out:
                        calls.append((url, start, duration)) or tmp_path/"dummy.mp4")
    monkeypatch.setattr(m, "duration_seconds", lambda media: 91.4)
    monkeypatch.setattr(m, "editorial_layout", lambda *args: "standard")
    monkeypatch.setattr(m, "wav_for_analysis", lambda *a: None)
    monkeypatch.setattr(m, "energy_per_second", lambda *a: [0.3]*95)
    monkeypatch.setattr(m, "transcribe", lambda *a: [
        {"start":1.0, "end":2.5, "text":"fala durante o jogo"}
    ])
    def fake_render(media, start, end, channel, subs, output, idx, subtitle_mode, layout="standard"):
        assert layout == "standard"
        output.mkdir(parents=True,exist_ok=True)
        (output/"corte_01.ass").write_text("Dialogue: 0,0:00:01.00,0:00:02.50,Streamer,,0,0,0,,fala")
        file=output/"corte_01.mp4"
        file.write_bytes(b"fake media")
        return file
    monkeypatch.setattr(m, "render", fake_render)
    monkeypatch.setattr(sys,"argv",["pipeline.py",
        "--video-url","https://kick.com/gabepeixe/clips/clip_012345",
        "--start","8","--window-sec","60","--subtitle-mode","auto"])
    m.main()
    assert calls == [("https://kick.com/gabepeixe/clips/clip_012345", 0, 180)]
    meta=json.loads((tmp_path/"output"/"cortes.json").read_text())[0]
    assert meta["source_start_seconds"]==0
    assert meta["source_end_seconds"]==91.4
    assert meta["source_clip_full_length_preserved"] is True
    assert meta["max_output_seconds"]==180
    assert meta["subtitle_burned"] is True
    assert meta["approval_required"] is True
