#!/usr/bin/env python3
"""Corteszinsbr: edit short, approved-source excerpts for editorial review.

Does not post, bypass access restrictions, or capture live broadcasts.
Downloads at most 10 minutes from a public VOD/clip URL (subject to extractor support).
"""
from __future__ import annotations

import argparse
import array
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
from statistics import median
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config" / "channels.json").read_text(encoding="utf-8"))
SLUGS = {c["slug"] for c in CONFIG["creators"]}
MAX_WINDOW_SEC = 600
MAX_CLIPS = 5
MAX_OUTPUT_SEC = 180  # KICK public clips and YouTube Shorts allow up to 3 minutes
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".ts"}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
GABEPEIXE_LOWER = ROOT / "assets" / "gabepeixe-gtavi-lower.png"


def run(cmd: list[str], *, log: bool = True):
    if log:
        print("RUN", shlex.join(cmd[:30]), flush=True)
    subprocess.run(cmd, check=True)


def valid_kick_link(url: str) -> tuple[str, str]:
    from urllib.parse import urlparse
    x = urlparse(url)
    if x.scheme != "https" or x.hostname not in {"kick.com", "www.kick.com"}:
        raise ValueError("Aceitamos apenas links HTTPS públicos de Kick.")
    segments = [s for s in x.path.split("/") if s]
    if len(segments) < 2:
        raise ValueError("Informe um link de VOD ou clipe, não apenas o canal ou a live atual.")
    # /video/<id> is an alternative route without a channel slug; reject to keep whitelist.
    if segments[0].lower() not in SLUGS:
        raise ValueError("Canal fora da lista autorizada dos cinco criadores.")
    if segments[1].lower() not in {"videos", "video", "clips", "clip"} or len(segments) < 3:
        raise ValueError("Link precisa apontar para VOD/clipe: /<canal>/videos/<id> ou /clips/<id>.")
    return segments[0].lower(), url


def fetch_excerpt(url: str, start: int, window_sec: int, out: Path) -> Path:
    """yt-dlp segment download; fail closed if plugin/API changes, never grab full VOD."""
    out.mkdir(parents=True, exist_ok=True)
    args = [
        "yt-dlp", "--no-playlist", "--no-progress", "--no-part", "--no-overwrites",
        "--write-info-json", "--download-sections", f"*{start}-{start + window_sec}",
        "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4",
        "-f", "bv*+ba/b",
        "-o", str(out / "source.%(ext)s"), url,
    ]
    run(args)
    media = [p for p in out.glob("source.*") if p.suffix.lower() in VIDEO_EXTS]
    if not media:
        raise RuntimeError("Não foi possível baixar trecho. Pode ser incompatibilidade do site ou VOD indisponível.")
    return max(media, key=lambda p: p.stat().st_mtime)


def duration_seconds(media: Path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(media)],
                       capture_output=True, text=True, check=True)
    return float(p.stdout.strip())


def wav_for_analysis(media: Path, path: Path):
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
         "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", str(path)])


def energy_per_second(wav: Path) -> list[float]:
    with wave.open(str(wav)) as wf:
        rate = wf.getframerate()
        if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
            raise ValueError("Arquivo WAV incompatível para análise")
        levels = []
        while True:
            raw = wf.readframes(rate)
            if not raw:
                break
            samples = array.array("h")
            samples.frombytes(raw[:len(raw) // 2 * 2])
            if sys.byteorder != "little":
                samples.byteswap()
            if samples:
                rms = math.sqrt(sum(v * v for v in samples) / len(samples)) / 32768
                levels.append(rms)
    return levels


def transcribe(wav: Path, model: str = "tiny") -> list[dict]:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("AVISO: faster-whisper indisponível; continuando sem legendas", flush=True)
        return []
    try:
        w = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=2)
        segs, info = w.transcribe(str(wav), beam_size=3, language="pt", word_timestamps=True,
                                  condition_on_previous_text=False, vad_filter=True)
        out = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip(),
                "words": [{"start": float(w.start), "end": float(w.end), "word": w.word.strip()}
                          for w in (s.words or []) if w.word.strip()]}
               for s in segs if s.text.strip()]
        print(f"Legenda: {len(out)} segmentos / linguagem {info.language}")
        return out
    except Exception as exc:
        print(f"AVISO: falha na transcrição ({exc}); continuando sem legendas", flush=True)
        return []


WORDS = ("kkkk", "cara", "mano", "caralho", "olha", "meu deus", "absurdo", "não acredito",
         "que isso", "mentira", "tá louco", "nem fodendo", "nossa", "puta", "hahaha")


def window_score(start: float, end: float, levels: list[float], segs: list[dict]) -> float:
    """Heuristic score for a full clip or excerpt; never grants rights clearance."""
    a = levels[max(0, int(start)):min(len(levels), math.ceil(end))]
    loudness = (sum(a) / len(a) if a else 0.0) * 14
    overlap = [seg for seg in segs if seg["end"] > start and seg["start"] < end]
    speech = sum(max(0, min(end, seg["end"]) - max(start, seg["start"])) for seg in overlap)
    transcript = " ".join(seg["text"] for seg in overlap).lower()
    hits = sum(1 for word in WORDS if word in transcript)
    return round(min(2.5, loudness) + min(3.0, speech / max(1, end - start) * 3)
                 + min(2.0, hits * 0.6), 3)


def choose_windows(length: float, levels: list[float], segs: list[dict],
                   count: int = 3, clip_sec: int = MAX_OUTPUT_SEC) -> list[tuple[float,float,float]]:
    """Use up to three minutes per selected window; never extend past the source."""
    if length <= 0:
        return []
    clip_sec = min(MAX_OUTPUT_SEC, clip_sec)
    if length <= clip_sec:
        return [(0.0, float(length), window_score(0.0, length, levels, segs))]
    windows = []
    limit = max(0, int(length - clip_sec))
    for t in range(0, limit+1, 5):
        end = min(length, t + clip_sec)
        score = window_score(float(t), float(end), levels, segs)
        # Prefer actual speech if available, but don't drop footage if transcription unavailable.
        windows.append((float(t),float(end),float(score)))
    windows.sort(key=lambda x:x[2],reverse=True)
    selected = []
    for win in windows:
        if any(max(win[0], old[0]) < min(win[1], old[1]) + 3 for old in selected):
            continue
        selected.append(win)
        if len(selected) >= count:
            break
    return sorted(selected,key=lambda x:x[0])


def full_source_clip_window(length: float, levels: list[float], segs: list[dict]) -> list[tuple[float,float,float]]:
    """Keep a Kick clip through its original end (Kick maximum: three minutes)."""
    if length <= 0:
        return []
    end = min(float(length), MAX_OUTPUT_SEC)
    return [(0.0, end, window_score(0.0, end, levels, segs))]


def srt_time(sec: float) -> str:
    ms = max(0,int(round(sec*1000)))
    h,rem = divmod(ms,3600000)
    m,rem = divmod(rem,60000)
    s,ms=divmod(rem,1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def subtitle_chunks(segs: list[dict], start: float, end: float) -> list[dict]:
    """Short readable captions, at most four words/26 chars each.

    Word timestamps come from Whisper; older transcripts get approximate timings.
    These chunks DO NOT identify speakers. Mixed game/streamer audio needs review.
    """
    chunks = []
    for seg in segs:
        words = seg.get("words") or []
        if not words:
            raw = seg.get("text", "").strip().split()
            if not raw: continue
            span = max(0.001, float(seg["end"]) - float(seg["start"]))
            words = [{"word": word, "start": float(seg["start"]) + span*i/len(raw),
                      "end": float(seg["start"]) + span*(i+1)/len(raw)}
                     for i,word in enumerate(raw)]
        group = []
        for word in words:
            ws, we = float(word["start"]), float(word["end"])
            content = word["word"].strip()
            if not content or we <= start or ws >= end: continue
            if group and (len(group) >= 4 or
                          len(" ".join(w["word"] for w in group)) + 1 + len(content) > 26 or
                          we - group[0]["start"] > 1.8):
                chunks.append(group)
                group=[]
            group.append({"word": content, "start": ws, "end": we})
        if group: chunks.append(group)
    result=[]
    for group in chunks:
        a=max(start, group[0]["start"])
        b=min(end, group[-1]["end"])
        if b-a < .12: continue
        result.append({"start":a-start,"end":b-start,"text":" ".join(w["word"] for w in group)})
    return result


def write_srt(segs: list[dict], start: float, end: float, dest: Path) -> bool:
    chunks=subtitle_chunks(segs,start,end)
    if not chunks: return False
    blocks=[]
    for i,c in enumerate(chunks,1):
        blocks.append(f'{i}\n{srt_time(c["start"])} --> {srt_time(c["end"])}\n{c["text"]}\n')
    dest.write_text("\n".join(blocks),encoding="utf-8")
    return True


def ass_time(sec: float) -> str:
    cs=max(0,int(round(sec*100)))
    h,rem=divmod(cs,360000)
    m,rem=divmod(rem,6000)
    sec,cs=divmod(rem,100)
    return f'{h}:{m:02}:{sec:02}.{cs:02}'


def write_ass(segs: list[dict], start: float, end: float, dest: Path) -> bool:
    """Render truly pixel-sized subtitle text at 1080x1920, unlike default SRT scaling."""
    chunks=subtitle_chunks(segs,start,end)
    if not chunks: return False
    header=(
        '[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n'
        'ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, '
        'BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, '
        'BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
        'Style: Streamer,DejaVu Sans,44,&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,'
        '-1,0,0,0,100,100,0,0,1,3,1,2,80,80,300,1\n\n[Events]\n'
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    )
    lines=[]
    for c in chunks:
        caption=c['text'].replace('\\','\\\\').replace('{','(').replace('}',')').replace('\n',' ')
        lines.append(f'Dialogue: 0,{ass_time(c["start"])},{ass_time(c["end"])},Streamer,,0,0,0,,{caption}')
    dest.write_text(header+'\n'.join(lines)+'\n',encoding='utf-8')
    return True


def ff_filter_escape(path: Path) -> str:
    return str(path.resolve()).replace("\\","\\\\").replace(":","\\:").replace("'", "\\'")


def creator_credit(channel: str) -> str:
    """Only print an actual authorized Kick channel: never infer one for local media."""
    return f"KICK.COM/{channel.upper()}" if channel in SLUGS else ""


# Webcam and gameplay rectangles on a 1080x608 source, followed by panel positions.
REACTION_REGIONS = {
    "allan_reaction": ("264:178:812:228", "804:540:0:34", 163, 790),
    # Gabepeixe's camera extends below the old 153px crop: preserve the face
    # by capturing the full inset and placing its panel above the gameplay.
    "gabepeixe_gtavi": ("262:200:808:8", "1080:422:0:186", 130, 935),
    "gabepeixe_reaction": ("262:200:808:8", "1080:422:0:186", 130, 905),
    "davyjones_reaction": ("236:138:844:55", "1080:412:0:196", 163, 724),
    "brabox_reaction": ("284:164:796:96", "790:558:0:40", 163, 715),
    "coringa_reaction": ("266:172:0:234", "698:504:270:104", 163, 766),
}
WEBCAM_EDGES = {
    # Allan can stream games with no webcam at the GTA/reaction coordinates.
    # Never magnify the upper-right game area as though it were his face.
    "allan_reaction": (812, 236, 138),
    "gabepeixe_reaction": (808, 26, 110),
    "davyjones_reaction": (845, 64, 116),
    "brabox_reaction": (796, 104, 145),
    "coringa_reaction": (267, 242, 153),
}


def has_webcam_edge(media: Path, edge: tuple[int, int, int], start: float) -> bool:
    """Require a persistent edge at the expected inset, leaving full-screen footage intact."""
    x, y, height = edge
    width = 16
    sample = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(media),
         "-vf", f"fps=2,scale=1080:608,format=gray,crop={width}:{height}:{x-8}:{y}",
         "-frames:v", "8", "-an", "-f", "rawvideo", "-"],
        capture_output=True, check=True, timeout=30,
    ).stdout
    frame_size = width * height
    if len(sample) < frame_size or len(sample) % frame_size:
        return False
    frames = [sample[i:i + frame_size] for i in range(0, len(sample), frame_size)]
    seams = [
        median(sum(abs(frame[row * width + col] - frame[row * width + col - 1])
                   for row in range(height)) / height for frame in frames)
        for col in range(1, width)
    ]
    center = max(seams[4:11])
    surrounding = median(seams[:4] + seams[11:])
    return center >= 13 and center >= 2.5 * max(surrounding, 1)


def editorial_layout(media: Path, channel: str, categories: list[str], start: float = 0) -> str:
    """Use creator-specific webcam positions for landscape streams in 16:9."""
    gabe_gtavi = channel == "gabepeixe" and any(
        re.search(r"\b(?:grand theft auto|gta)[\s-]*(?:vi|6)\b", category, re.I)
        for category in categories
    )
    layout = {
        "allanstag": "allan_reaction",
        "gabepeixe": "gabepeixe_gtavi" if gabe_gtavi else "gabepeixe_reaction",
        "davyjones": "davyjones_reaction",
        "brabox": "brabox_reaction",
        "coringa": "coringa_reaction",
    }.get(channel, "standard")
    if layout == "standard":
        return layout
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(media)],
        check=True, capture_output=True, text=True,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    if not streams:
        return "standard"
    width, height = streams[0]["width"], streams[0]["height"]
    if not height or abs(width / height - 16 / 9) > 0.03:
        return "standard"
    edge = WEBCAM_EDGES.get(layout)
    if edge is not None and not has_webcam_edge(media, edge, start):
        return "standard"
    return layout


def reaction_filters(layout: str, credit_file: Path) -> list[str]:
    camera, game, camera_y, game_y = REACTION_REGIONS[layout]
    parts = [
        "[0:v]scale=1080:608:flags=lanczos,setsar=1,split=3[back][game][cam]",
        "[back]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
        "boxblur=12:2,eq=brightness=-0.23:saturation=0.7,scale=1080:1920[background]",
        f"[game]crop={game},scale=1080:-2:flags=lanczos,setsar=1[gameplay]",
        f"[cam]crop={camera},scale=900:-2:flags=lanczos,"
        "setsar=1,eq=brightness=0.03:gamma=1.10[webcam]",
        f"[background][gameplay]overlay=0:{game_y}:shortest=1[panels]",
        f"[panels][webcam]overlay=(W-w)/2:{camera_y}:shortest=1[reaction]",
    ]
    if layout == "gabepeixe_gtavi":
        parts.extend([
            "[1:v]scale=610:-2[lower]",
            # Keep the branded strip below the now-taller camera, not over the face.
            "[reaction][lower]overlay=(W-w)/2:840:eof_action=repeat[composition]",
        ])
    else:
        parts.append(
            f"[reaction]drawtext=fontfile={FONT}:textfile={ff_filter_escape(credit_file)}:"
            "fontcolor=white:fontsize=52:borderw=3:bordercolor=black:"
            "shadowcolor=black@0.65:shadowx=1:shadowy=2:x=(w-text_w)/2:y=77[composition]"
        )
    return parts


def render(media: Path, start: float, end: float, channel: str, subs: list[dict],
           output: Path, idx: int, subtitle_mode: str = "auto", layout: str = "standard"):
    # In auto mode, embed all recognized speech, including game dialogue.
    # The captions never claim to identify the speaker.
    output.mkdir(parents=True, exist_ok=True)
    stem = f"corte_{idx:02}"
    out = output/(stem + ".mp4")
    srt = output/(stem + ".srt")
    has_subs = write_srt(subs, start, end, srt)
    credit = "kick.com/allanstag" if layout == "allan_reaction" else creator_credit(channel)
    credit_file = output/(stem + "_fonte.txt")
    if credit:
        credit_file.write_text(credit, encoding="utf-8")

    # Keep the existing blurred 9:16 background, then draw the source credit
    # at the TOP EDGE of the sharp gameplay layer (as in the reference sample).
    foreground = (
        "[foreground_source]scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
        "setsar=1"
    )
    if credit:
        foreground += (
            f",drawtext=fontfile={FONT}:textfile={ff_filter_escape(credit_file)}:"
            "fontcolor=white:fontsize=34:borderw=2:bordercolor=black@0.95:"
            "shadowcolor=black@0.70:shadowx=1:shadowy=1:x=(w-text_w)/2:y=9"
        )
    filter_parts = [
        "[0:v]split=2[background_source][foreground_source]",
        "[background_source]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1080:1920,setsar=1,boxblur=40:2,eq=brightness=-0.10:saturation=0.88[background]",
        foreground + "[foreground]",
        "[background][foreground]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1[composition]",
    ]
    if layout != "standard":
        filter_parts = reaction_filters(layout, credit_file)
    if has_subs and subtitle_mode == "auto":
        ass = output/(stem + ".ass")
        if write_ass(subs, start, end, ass):
            filter_parts.append(f"[composition]ass='{ff_filter_escape(ass)}'[final]")
        else:
            filter_parts.append("[composition]null[final]")
    else:
        filter_parts.append("[composition]null[final]")
    inputs = ["-i", str(media)]
    if layout == "gabepeixe_gtavi":
        inputs.extend(["-i", str(GABEPEIXE_LOWER)])
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
         *inputs, "-t", f"{max(0.1, end-start):.3f}",
         "-filter_complex", ";".join(filter_parts), "-map", "[final]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-r", "30",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])
    return out


def main():
    parser=argparse.ArgumentParser()
    src=parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--video-url",help="Kick VOD or clip URL, one of five approved channels")
    src.add_argument("--input-file",help="Test/local use: previously obtained authorized video")
    parser.add_argument("--start",type=int,default=0,help="Source timestamp in seconds (video URL only)")
    parser.add_argument("--window-sec",type=int,default=180,help="VOD source window, <=600; direct Kick clips always use their full length (<=180s)")
    parser.add_argument("--max-clips",type=int,default=3)
    parser.add_argument("--creator-slug",choices=sorted(SLUGS),help="Creator source for --input-file tests")
    parser.add_argument("--skip-whisper",action="store_true",help="Test footage without transcript/model")
    parser.add_argument("--subtitle-mode",choices=["review", "auto", "off"],default="auto",
                        help="auto: embutir as legendas, inclusive falas do jogo (padrão); "
                             "review: SRT separado; off: sem legendas (somente diagnóstico)")
    args=parser.parse_args()
    if args.start < 0 or not (15 <= args.window_sec <= MAX_WINDOW_SEC) or not (1 <= args.max_clips <= MAX_CLIPS):
        parser.error("start >= 0; window-sec 15..600; max-clips 1..5")
    working = ROOT/"work"; working.mkdir(exist_ok=True)
    outputs = ROOT/"output"; outputs.mkdir(exist_ok=True)
    is_direct_clip = False
    source_start = args.start
    categories = []
    if args.video_url:
        channel,url=valid_kick_link(args.video_url)
        # A direct KICK clip is already an edited 10-180s recording.
        # Never truncate it at an older 60s workflow argument or skip its beginning.
        is_direct_clip = bool(re.search(r"/clips?/[^/?#]+", url.split("?")[0]))
        source_start = 0 if is_direct_clip else args.start
        fetch_window = MAX_OUTPUT_SEC if is_direct_clip else args.window_sec
        media=fetch_excerpt(url,source_start,fetch_window,working)
        info_file = working / "source.info.json"
        if channel == "gabepeixe" and info_file.is_file():
            info = json.loads(info_file.read_text(encoding="utf-8"))
            categories = [item for item in (info.get("categories") or []) if isinstance(item, str)]
    else:
        channel=args.creator_slug or "local"
        media=Path(args.input_file).resolve()
        if not media.exists(): parser.error("Arquivo de teste não existe")
    length=min(duration_seconds(media),float(MAX_OUTPUT_SEC if is_direct_clip else MAX_WINDOW_SEC))
    wav=working/"audio.wav"
    wav_for_analysis(media,wav)
    levels=energy_per_second(wav)
    segments = [] if args.skip_whisper or args.subtitle_mode == "off" else transcribe(wav)
    # A direct Kick /clips/ URL defines the entire editorial boundary (<=180s).
    # Keep it intact; VODs may still be divided into selected excerpts.
    windows = (full_source_clip_window(length, levels, segments) if is_direct_clip
               else choose_windows(length, levels, segments, count=args.max_clips))
    outputs_meta=[]
    for i,(start,end,score) in enumerate(windows,1):
        layout = editorial_layout(media, channel, categories, start)
        out=render(media,start,end,channel,segments,outputs,i,
                   subtitle_mode=args.subtitle_mode,layout=layout)
        subtitle_burned = (args.subtitle_mode == "auto"
                           and (outputs / f"corte_{i:02}.ass").is_file())
        outputs_meta.append({"file":out.name,"source_channel":channel,
            "input_url":args.video_url or "uploaded file", "source_start_seconds":source_start+round(start,2),
            "source_end_seconds":source_start+round(end,2),"score_heuristic":round(score,3),
            "approval_required":True,"status":"PARA_REVISAO","editorial_layout":layout,
            "subtitle_mode":args.subtitle_mode,"subtitle_burned":subtitle_burned,
            "subtitle_unverified":True,"speaker_separation":"not_available",
            "source_clip_full_length_preserved":bool(is_direct_clip and length <= MAX_OUTPUT_SEC),
            "max_output_seconds":MAX_OUTPUT_SEC})
    (outputs/"cortes.json").write_text(json.dumps(outputs_meta,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Finalizados",len(outputs_meta),"cortes; nenhum foi publicado.")

if __name__=="__main__":
    main()
