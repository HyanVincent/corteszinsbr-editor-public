#!/usr/bin/env python3
"""Conservative Corteszinsbr technical QA. Not an editorial or rights approval."""
from __future__ import annotations

import argparse
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / "config" / "approval_rules.json").read_text(encoding="utf-8"))
CREATORS = {x["slug"] for x in json.loads((ROOT / "config" / "channels.json").read_text(encoding="utf-8"))["creators"]}
LOW_SCORE_REASON = "Baixa pontuação de seleção; requer avaliação editorial"


def probe(video: Path) -> dict:
    run = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)],
        check=True, capture_output=True, text=True, timeout=30
    )
    data = json.loads(run.stdout)
    videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not videos:
        raise ValueError("Sem trilha de vídeo")
    v = videos[0]
    rate = v.get("avg_frame_rate", "0/1")
    try:
        fps = float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "width": int(v.get("width", 0)),
        "height": int(v.get("height", 0)),
        "codec": v.get("codec_name", ""),
        "fps": round(fps, 3),
        "duration": float(data.get("format", {}).get("duration", 0)),
        "size_bytes": video.stat().st_size,
        "has_audio": bool(audios),
    }


def mean_volume_db(video: Path) -> float | None:
    try:
        p = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video),
             "-af", "volumedetect", "-vn", "-f", "null", "-"],
            capture_output=True, text=True, timeout=100
        )
        matched = re.search(r"mean_volume:\s*(-?(?:\d+(?:\.\d+)?|inf)) dB", p.stderr)
        if matched:
            return float(matched.group(1))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def expected_channel(url: str) -> str | None:
    parsed = urlparse(url)
    pieces = [p.lower() for p in parsed.path.split("/") if p]
    if parsed.scheme != "https" or parsed.hostname not in {"kick.com", "www.kick.com"}:
        return None
    if len(pieces) < 3 or pieces[0] not in CREATORS:
        return None
    return pieces[0] if pieces[1] in {"clip", "clips", "video", "videos"} else None


def assess(item: dict, directory: Path, seen: set[tuple]) -> dict:
    name = item.get("file", "")
    video = directory / name
    reasons = []
    measurements = {}
    channel = item.get("source_channel", "")
    if not name.startswith("corte_") or not name.endswith(".mp4") or Path(name).name != name:
        reasons.append("Nome do arquivo inválido")
    if channel not in CREATORS or expected_channel(item.get("input_url", "")) != channel:
        reasons.append("Origem do clipe não confirmada na lista de canais")
    span = (channel, item.get("input_url", ""), item.get("source_start_seconds"), item.get("source_end_seconds"))
    if span in seen:
        reasons.append("Trecho duplicado nesta execução")
    seen.add(span)
    if not video.is_file():
        reasons.append("MP4 não encontrado")
    else:
        try:
            measurements = probe(video)
            if (measurements["width"], measurements["height"]) != (RULES["width"], RULES["height"]):
                reasons.append("Resolução fora do template 9:16")
            if measurements["codec"] != "h264":
                reasons.append("Codec inesperado")
            if not RULES["min_seconds"] <= measurements["duration"] <= RULES["max_seconds"]:
                reasons.append("Duração fora do padrão")
            # Prevent a future regression that silently cuts source clips to 24s.
            begin, finish = item.get("source_start_seconds"), item.get("source_end_seconds")
            if isinstance(begin, (int, float)) and isinstance(finish, (int, float)):
                expected = float(finish) - float(begin)
                if (not 0 < expected <= RULES["max_seconds"] or
                        abs(measurements["duration"] - expected) > 0.75):
                    reasons.append("Vídeo não preservou a duração selecionada do clipe")
            if measurements["fps"] < RULES["min_fps"]:
                reasons.append("FPS abaixo do mínimo")
            if not RULES["min_bytes"] <= measurements["size_bytes"] <= RULES["max_bytes"]:
                reasons.append("Tamanho fora dos limites")
            if not measurements["has_audio"]:
                reasons.append("Vídeo sem áudio")
            else:
                mean = mean_volume_db(video)
                measurements["mean_volume_db"] = mean
                if mean is None or mean < RULES["min_mean_volume_db"]:
                    reasons.append("Áudio muito baixo ou não mensurável")
        except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
            reasons.append(f"Não foi possível analisar mídia: {str(err)[:120]}")
    credit = directory / name.replace(".mp4", "_fonte.txt")
    expected = f"KICK.COM/{channel.upper()}"
    if not credit.is_file() or credit.read_text(encoding="utf-8").strip().casefold() != expected.casefold():
        reasons.append("Crédito de origem incorreto ou ausente")
    # New policy: captions must actually be embedded in the MP4.
    # Game speech is allowed (requested by owner), but accuracy still needs editorial review.
    subtitle_mode = item.get("subtitle_mode")
    subtitle_ass = directory / name.replace(".mp4", ".ass")
    if subtitle_mode == "auto":
        if (item.get("subtitle_burned") is not True or not subtitle_ass.is_file()
                or "Dialogue: 0," not in subtitle_ass.read_text(encoding="utf-8", errors="replace")):
            reasons.append("Legenda não embutida ou transcrição indisponível; requer revisão")
    elif subtitle_mode in {"off", "review"}:
        reasons.append("Vídeo sem legenda embutida; reexportar com subtitle_mode=auto")
    else:
        reasons.append("Modo de legenda desconhecido")
    if float(item.get("score_heuristic", 0)) < RULES["min_heuristic_score"]:
        reasons.append(LOW_SCORE_REASON)

    status = "CANDIDATO_TECNICO" if not reasons else "REVISAO_NECESSARIA"
    return {
        "file": name,
        "source_channel": channel,
        "status": status,
        "reasons": reasons,
        "checks": measurements,
        "source_url": item.get("input_url"),
        "source_start_seconds": item.get("source_start_seconds"),
        "source_end_seconds": item.get("source_end_seconds"),
        "auto_published": False,
        "editorial_approved": False,
        "copyright_checked": False,
        "captions_burned": subtitle_mode == "auto" and not any("Legenda não embutida" in reason for reason in reasons),
        "captions_unverified": True,
    }


def process(output: Path, root: Path) -> dict:
    manifest = output / "cortes.json"
    if not manifest.is_file():
        raise FileNotFoundError("Arquivo cortes.json não encontrado")
    clips = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(clips, list) or not clips:
        raise ValueError("Nenhum corte gerado")
    ready = root / "prontos"
    review = root / "revisar"
    seen = set()
    results = []
    for item in clips:
        decision = assess(item, output, seen)
        results.append(decision)
        target = ready if decision["status"] == "CANDIDATO_TECNICO" else review
        target.mkdir(parents=True, exist_ok=True)
        stem = Path(decision["file"]).stem
        for candidate in output.glob(stem + ".*"):
            if candidate.is_file() and candidate.suffix in {".mp4", ".srt", ".ass"}:
                shutil.copy2(candidate, target / candidate.name)
        for candidate in output.glob(stem + "_*.txt"):
            shutil.copy2(candidate, target / candidate.name)
        # Keep title/caption proposals beside the clip, but never use them as QA evidence.
        for ext in (".md", ".json"):
            suggestion = output / f"{stem}_sugestoes{ext}"
            if suggestion.is_file():
                shutil.copy2(suggestion, target / suggestion.name)
        (target / f"{stem}_qa.json").write_text(
            json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if decision["source_channel"] == "gabepeixe" and decision["reasons"] == [LOW_SCORE_REASON]:
            editorial = root / "gabepeixe_editorial"
            editorial.mkdir(exist_ok=True)
            for name in (decision["file"], f"{stem}_qa.json", f"{stem}_sugestoes.json"):
                if (target / name).is_file():
                    shutil.copy2(target / name, editorial / name)
    report = {
        "total": len(results),
        "technical_candidates": sum(x["status"] == "CANDIDATO_TECNICO" for x in results),
        "needs_review": sum(x["status"] == "REVISAO_NECESSARIA" for x in results),
        "auto_published": 0,
        "decisions": results,
    }
    (root / "qa_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = ["## Corteszinsbr — triagem técnica",
             f"**{report['technical_candidates']} candidatos técnicos**, "
             f"**{report['needs_review']} para revisão**. Nenhum vídeo publicado.",
             "",
             "| Vídeo | Resultado | Motivo |",
             "|---|---|---|"]
    for decision in results:
        notes = "; ".join(decision["reasons"]) or "Passou nos critérios técnicos, falta avaliação editorial"
        lines.append(f"| {decision['file']} | {decision['status']} | {notes.replace('|', '/')} |")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "output")
    parser.add_argument("--destination", type=Path, default=ROOT / "qa")
    args = parser.parse_args()
    process(args.output, args.destination)


if __name__ == "__main__":
    main()
