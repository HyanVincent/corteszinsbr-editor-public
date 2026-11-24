import importlib.util
import json
from pathlib import Path
import subprocess
import sys

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
spec = importlib.util.spec_from_file_location("hourly_clips", SRC / "hourly_clips.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def clip(cid, channel, now=1800000000, views=50, age=False, length=30):
    return {"id": "clip_"+cid*16, "channel": channel,
            "url": f"https://kick.com/{channel}/clips/clip_{cid*16}",
            "views": views, "timestamp": now,
            "duration_seconds": length, "age_restricted": age}


def test_hourly_picks_top_views_per_channel():
    now = 1800000000
    records = {
        "allanstag": [clip("a","allanstag",now-3599,80),
                      clip("b","allanstag",now-600,1000),
                      clip("c","allanstag",now-3601,999999)],
        "davyjones": [clip("d","davyjones",now-100,120),
                      clip("e","davyjones",now-200,9999,age=True)],
        "gabepeixe": [clip("f","gabepeixe",now-60,None)],
        "brabox": [clip("g","brabox",now+30,10000)],
        "coringa": [clip("h","coringa",now-60,400,length=9)],
    }
    out = h.choose_hourly(records,set(),now)
    assert len(out)==2
    assert {c["channel"] for c in out} == {"allanstag","davyjones"}
    assert next(x for x in out if x["channel"]=="allanstag")["views"]==1000
    assert all(c["auto_published"] is False for c in out)


def test_seen_clip_skipped_and_max_one_per_channel():
    now=1800000000
    a=clip("a","allanstag",now-5,600)
    b=clip("b","allanstag",now-15,200)
    assert h.choose_hourly({"allanstag":[a,b]},set(),now)[0]["id"]==a["id"]
    assert h.choose_hourly({"allanstag":[a,b]},{a["id"]},now)[0]["id"]==b["id"]


def test_discovery_stores_metadata_only(tmp_path, monkeypatch):
    now=1800000000
    a=clip("s","allanstag",now-100,190)
    monkeypatch.setattr(h,"list_clips",
                        lambda slug,sort,period,limit=100:
                        [a] if slug=="allanstag" else [])
    out=tmp_path/"output"
    state=tmp_path/"hourly.json"
    first=h.discover(out,state,now_ts=now)
    assert first["candidates_count"]==1
    assert first["lookback_minutes"]==60
    assert (out/"hourly_candidates.json").exists()
    assert not list(out.glob("*.mp4"))
    assert h.discover(out,state,now_ts=now+100)["candidates_count"]==0


def test_dispatch_daily_limit_and_no_publication(tmp_path, monkeypatch):
    now=1800000000
    state_path=tmp_path/"state.json"
    state={
        "seen_ids": [],"dispatched_ids":[],"daily_dispatch":{},
        "last_batch_at":now,
        "last_batch":[clip("a","allanstag",now-10,40),
                      clip("b","gabepeixe",now-10,90)],
    }
    state_path.write_text(json.dumps(state))
    calls=[]
    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args,0,"","")
    monkeypatch.setenv("GITHUB_REPOSITORY","testowner/testrepo")
    out=h.dispatch(state_path,daily_cap=1,now_ts=now,runner=fake_run)
    assert out["queued_count"]==1
    assert len(out["skipped"])==1
    assert out["auto_published"] is False
    assert len(calls)==1
    assert "editar-aprovado.yml" in calls[0]
    assert any(arg.startswith("video_url=https://kick.com/allanstag/clips/") for arg in calls[0])
    assert "subtitle_mode=auto" not in calls[0]  # The public editor has safe defaults
    assert h.dispatch(state_path,daily_cap=1,now_ts=now,runner=fake_run)["queued_count"] == 0
    assert len(calls)==1


def test_dispatch_does_not_resend_on_failed_command(tmp_path):
    now=1800000000
    state=tmp_path/"state.json"
    state.write_text(json.dumps({"seen_ids": [],"dispatched_ids":[],
                                "daily_dispatch":{},"last_batch_at":now,
                                "last_batch":[clip("a","allanstag",now-10,40)]}))
    def failure(args,**kwargs):
        return subprocess.CompletedProcess(args,1,"","GH authorization failure")
    res=h.dispatch(state,daily_cap=12,now_ts=now,runner=failure)
    assert not res["queued"]
    assert len(res["skipped"])==1
    assert not h.load_state(state)["dispatched_ids"]


def test_discovery_fails_closed_without_kick(tmp_path,monkeypatch):
    def fail(*args,**kwargs):
        raise RuntimeError("no Kick")
    monkeypatch.setattr(h,"list_clips",fail)
    try:
        h.discover(tmp_path/"out",tmp_path/"state.json",now_ts=1800000000)
    except RuntimeError as ex:
        assert "não respondeu" in str(ex)
    else:
        raise AssertionError("Should fail closed")



def test_recovery_gets_recent_seen_but_never_dispatched(tmp_path, monkeypatch):
    now=1800000000
    recovered=clip("r","allanstag",now-6000,80)
    another=clip("n","davyjones",now-60,90)
    state=tmp_path/"hourly.json"
    state.write_text(json.dumps({
        "seen_ids":[recovered["id"],another["id"]],
        "dispatched_ids":[another["id"]],
        "daily_dispatch":{},"last_batch":[],"last_batch_at":0
    }))
    monkeypatch.setattr(h,"list_clips",lambda slug,sort,period,limit=100:
        [recovered] if slug=="allanstag" else ([another] if slug=="davyjones" else []))
    result=h.discover(tmp_path/"out",state,now_ts=now,
                      lookback_minutes=180,recover_undispatched=True)
    assert result["candidates_count"]==1
    assert result["lookback_minutes"]==180
    assert result["candidates"][0]["id"]==recovered["id"]
    assert all(x["id"]!=another["id"] for x in result["candidates"])
    assert result["auto_published"] is False


def test_recovery_rejects_outside_three_hour_window(tmp_path, monkeypatch):
    now=1800000000
    stale=clip("s","allanstag",now-20000,900)
    monkeypatch.setattr(h,"list_clips",lambda slug,sort,period,limit=100:
                        [stale] if slug=="allanstag" else [])
    result=h.discover(tmp_path/"out",tmp_path/"hourly.json",now_ts=now,
                      lookback_minutes=180,recover_undispatched=True)
    assert result["candidates_count"]==0


def test_lookback_rejects_more_than_four_hours(tmp_path,monkeypatch):
    monkeypatch.setattr(h,"list_clips",lambda slug,sort,period,limit=100:[])
    try:
        h.discover(tmp_path/"out",tmp_path/"hourly.json",now_ts=1800000000,
                   lookback_minutes=300,recover_undispatched=True)
    except ValueError as error:
        assert "1-240" in str(error)
    else:
        raise AssertionError("Should refuse unbounded old clips")


def test_hourly_allows_age_flag_for_gta_only():
    now = 1800000000
    candidates = [
        {**clip("gta-vi", "gabepeixe", now, age=True),
         "categories": ["Grand Theft Auto VI (GTA)"]},
        {**clip("other", "gabepeixe", now, views=100, age=True),
         "categories": ["Just Chatting"]},
        clip("no-category", "gabepeixe", now, views=99, age=True),
    ]
    selected = h.choose_hourly({"gabepeixe": candidates}, set(), now)
    assert [item["id"] for item in selected] == [candidates[0]["id"]]
    assert selected[0]["needs_content_review"] is True
