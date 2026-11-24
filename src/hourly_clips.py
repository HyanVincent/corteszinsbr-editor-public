#!/usr/bin/env python3
"""Find the most-viewed PUBLIC clip created within the last hour per authorized Kick channel.

This discovers links, not live broadcasts, and does not download or publish media.
The view count is CURRENT total views for each clip, not views gained during an hour.
Missing timestamps/views fail closed. Uses only yt-dlp's public Kick listings.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from zoneinfo import ZoneInfo

from discover_clips import CHANNELS, list_clips

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "data" / "hourly_seen.json"
HISTORY = ROOT / "data" / "discovery_seen.json"
TIMEZONE = ZoneInfo("America/Sao_Paulo")
LOOKBACK_SECONDS = 3600
LIST_LIMIT = 100
DEFAULT_DAILY_CAP = 30
GTA_CATEGORY = re.compile(r"^(?:grand theft auto\b|gta(?=\b|[456]))", re.IGNORECASE)


def choose_hourly(records: dict[str, list[dict]], seen: set[str],
                  now_ts: int, lookback: int = LOOKBACK_SECONDS) -> list[dict]:
    """At most one unique most-viewed clip per whitelisted creator per hour."""
    low = now_ts - lookback
    selected = []
    for slug in CHANNELS:
        eligible = []
        for clip in records.get(slug, []):
            if (clip.get("channel") != slug or clip.get("id") in seen
                    or (clip.get("age_restricted") and not any(
                        GTA_CATEGORY.match(category.strip()) for category in clip.get("categories", [])
                    ))):
                continue
            ts, views = clip.get("timestamp"), clip.get("views")
            if (not isinstance(ts, int) or not low <= ts <= now_ts
                    or not isinstance(views, int) or views < 0):
                continue
            duration = clip.get("duration_seconds")
            if duration is not None and (duration < 15 or duration > 600):
                continue
            eligible.append(clip)
        if eligible:
            best = max(eligible, key=lambda c: (c["views"], c["timestamp"]))
            selected.append({
                "id": best["id"], "channel": slug, "url": best["url"],
                "views": best["views"], "timestamp": best["timestamp"],
                "duration_seconds": best.get("duration_seconds"),
                "rule": "highest_views_created_last_hour",
                "needs_content_review": True,
                "auto_published": False,
            })
    return selected


def load_state(path: Path) -> dict:
    if path.is_file():
        state = json.loads(path.read_text(encoding="utf-8"))
    else:
        old = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.is_file() else {}
        state = {"seen_ids": old.get("seen_ids", []), "dispatched_ids": [],
                 "daily_dispatch": {}, "last_batch": [], "last_batch_at": 0}
    if not isinstance(state, dict) or not isinstance(state.get("seen_ids"), list):
        raise ValueError("Estado de clipes inválido")
    state.setdefault("dispatched_ids", [])
    state.setdefault("daily_dispatch", {})
    state.setdefault("last_batch", [])
    state.setdefault("last_batch_at", 0)
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["seen_ids"] = state["seen_ids"][-3000:]
    state["dispatched_ids"] = state["dispatched_ids"][-3000:]
    state["daily_dispatch"] = dict(sorted(state["daily_dispatch"].items())[-14:])
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def discover(outdir: Path, state_path: Path, now_ts: int | None = None,
             lookback_minutes: int = 60, recover_undispatched: bool = False) -> dict:
    now_ts = int(datetime.now(timezone.utc).timestamp()) if now_ts is None else now_ts
    state = load_state(state_path)
    records = {}
    errors = []
    for slug in CHANNELS:
        try:
            # Kick supports day sorting, not an hour range. Filter creation timestamps locally.
            records[slug] = list_clips(slug, "date", "day", limit=LIST_LIMIT)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            records[slug] = []
            errors.append({"channel": slug, "reason": str(exc)[:350]})
    if len(errors) == len(CHANNELS):
        raise RuntimeError("A Kick não respondeu em nenhum dos cinco canais; nenhuma seleção feita.")
    # One-off recovery may revisit SELECTED-but-never-dispatched clips. The scheduled
    # hourly monitor keeps existing deduplication; dispatched IDs are never replayed.
    if not 1 <= lookback_minutes <= 240:
        raise ValueError("Busca de clipes limitada a 1-240 minutos.")
    excluded = set(state["dispatched_ids"]) if recover_undispatched else set(state["seen_ids"])
    selected = choose_hourly(records, excluded, now_ts,
                             lookback=lookback_minutes * 60)
    state["seen_ids"].extend(x["id"] for x in selected)
    state["last_batch"] = selected
    state["last_batch_at"] = now_ts
    outdir.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at_utc": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "lookback_minutes": lookback_minutes,
        "selection": f"most-viewed clip created within last {lookback_minutes} minutes, max one/channel",
        "queried_channels": len(CHANNELS),
        "successful_channels": len(CHANNELS) - len(errors),
        "errors": errors,
        "candidates_count": len(selected),
        "auto_published": False,
        "candidates": selected,
    }
    (outdir / "hourly_candidates.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        f"# Corteszinsbr — melhores clipes dos últimos {lookback_minutes} minutos",
        "",
        f"Selecionados: **{len(selected)}** (máximo de 1 por canal; 5 no total).",
        f"Canais consultados com sucesso: **{len(CHANNELS)-len(errors)} de {len(CHANNELS)}**.",
        "**Nenhum vídeo publicado.** Visualizações = total atual do clipe.",
        "", "| Canal | Views | Clipe |", "|---|---:|---|",
    ]
    for row in selected:
        lines.append(f"| {row['channel']} | {row['views']} | [Ver na Kick]({row['url']}) |")
    if errors:
        lines.extend(["", f"Aviso: {len(errors)} consultas falharam, cobertura incompleta."])
    md = "\n".join(lines) + "\n"
    (outdir / "hourly_candidates.md").write_text(md, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fp:
            fp.write(md)
    save_state(state_path, state)
    print(md)
    return report


def dispatch(state_path: Path, daily_cap: int = DEFAULT_DAILY_CAP, now_ts: int | None = None,
             runner=subprocess.run) -> dict:
    """Enqueue short-lived review jobs. Never publish TikTok/YouTube videos here."""
    now_ts = int(datetime.now(timezone.utc).timestamp()) if now_ts is None else now_ts
    state = load_state(state_path)
    day = datetime.fromtimestamp(now_ts, TIMEZONE).date().isoformat()
    used = int(state["daily_dispatch"].get(day, 0))
    batch = state["last_batch"] if now_ts - state["last_batch_at"] <= 1800 else []
    already = set(state["dispatched_ids"])
    queued = []
    skipped = []
    for clip in batch:
        if clip["id"] in already:
            continue
        if used >= daily_cap:
            skipped.append({"url": clip["url"], "reason": "daily_cost_guard"})
            continue
        args = ["gh", "workflow", "run", "editar-aprovado.yml",
                "--repo", os.environ.get("GITHUB_REPOSITORY", "HyanVincent/corteszinsbr-cloud"),
                "--ref", "main", "-f", f"video_url={clip['url']}"]
        proc = runner(args, capture_output=True, text=True, timeout=45)
        if proc.returncode:
            skipped.append({"url": clip["url"], "reason": "dispatch_error",
                            "details": (proc.stderr or "")[-240:]})
            continue
        already.add(clip["id"])
        state["dispatched_ids"].append(clip["id"])
        queued.append(clip["url"])
        used += 1
        state["daily_dispatch"][day] = used
        save_state(state_path, state)
    result = {"queued_count": len(queued), "queued": queued, "skipped": skipped,
              "daily_cap": daily_cap, "used_today": used, "auto_published": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fp:
            fp.write(f"\n### Edição enfileirada: {len(queued)} de {len(batch)} clipes\n"
                     f"Limite diário de custo: {used}/{daily_cap}. Nenhuma publicação.\n")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispatch", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "discovery")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP)
    parser.add_argument("--lookback-minutes", type=int, default=60,
                        help="Janela de busca em minutos (1 a 240).")
    parser.add_argument("--recover-undispatched", action="store_true",
                        help="Recuperar clipes já encontrados mas nunca enviados à edição.")
    args = parser.parse_args()
    if not 1 <= args.daily_cap <= 120:
        parser.error("daily-cap deve ficar entre 1 e 120")
    if args.dispatch:
        dispatch(args.state, args.daily_cap)
    else:
        discover(args.output, args.state,
                 lookback_minutes=args.lookback_minutes,
                 recover_undispatched=args.recover_undispatched)


if __name__ == "__main__":
    main()
