#!/usr/bin/env python3
"""Discover public Kick clip URLs via yt-dlp's KickClipsIE, with no video downloads.

Two queries per allowed channel: newest (day) and most viewed (week).
If Kick changes its public interface, report the failure; never bypass login, bot
protection or rate limits. A selected URL is only a candidate, never an approval.
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

ROOT = Path(__file__).resolve().parents[1]
CHANNELS = [x["slug"] for x in json.loads(
    (ROOT / "config" / "channels.json").read_text(encoding="utf-8")
)["creators"]]
CLIP_RE = re.compile(r"^clip_[a-zA-Z0-9]{12,64}$")


def list_clips(slug: str, sort: str, period: str, limit: int = 10) -> list[dict]:
    if slug not in CHANNELS or sort not in {"date", "view"} or period not in {"day", "week"}:
        raise ValueError("Parâmetros de descoberta não permitidos")
    url = f"https://kick.com/{slug}/clips?sort={sort}&range={period}"
    proc = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-single-json", "--no-download",
         "--no-warnings", "--no-progress", "--playlist-end", str(limit),
         "--socket-timeout", "12", "--retries", "1", "--extractor-retries", "1",
         url],
        capture_output=True, text=True, timeout=100
    )
    if proc.returncode:
        raise RuntimeError(f"Kick/{slug} ({sort}): {proc.stderr[-400:]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as ex:
        raise RuntimeError(f"Kick/{slug}: resposta inválida da listagem") from ex
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise RuntimeError(f"Kick/{slug}: playlist sem entradas válidas")
    found = []
    for raw in payload["entries"]:
        if not isinstance(raw, dict):
            continue
        clip_id = str(raw.get("id", ""))
        if not CLIP_RE.fullmatch(clip_id):
            continue
        # Numeric channel_id is not a slug. Enforce the channel when a text slug
        # is explicitly present, and always canonicalize links to the allowed slug.
        explicit_slug = raw.get("channel_slug")
        if explicit_slug and str(explicit_slug).lower() != slug:
            continue
        views = raw.get("view_count")
        try:
            views = max(0, int(views)) if views is not None else None
        except (TypeError, ValueError):
            views = None
        timestamp = raw.get("timestamp")
        try:
            timestamp = int(timestamp) if timestamp else None
        except (TypeError, ValueError):
            timestamp = None
        duration = raw.get("duration")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        age_limit = raw.get("age_limit")
        categories = raw.get("categories")
        found.append({
            "id": clip_id, "channel": slug,
            "url": f"https://kick.com/{slug}/clips/{clip_id}",
            "title": str(raw.get("title") or "")[:140],
            "views": views, "timestamp": timestamp, "duration_seconds": duration,
            "categories": [name for name in categories if isinstance(name, str)]
            if isinstance(categories, list) else [],
            "needs_content_review": True,
            "age_restricted": age_limit is not None and int(age_limit) >= 18,
            "discovered_from": f"{sort}/{period}",
        })
    return found


def choose(records: dict[str, list[dict]], seen: set[str], limit: int = 4) -> list[dict]:
    """Reserve spots for BOTH newest and most-viewed, never repeat known IDs."""
    per_mode = {"newest": [], "popular": []}
    for slug, items in records.items():
        for item in items:
            if item["id"] in seen or item.get("age_restricted"):
                continue
            if item.get("duration_seconds") is not None and item["duration_seconds"] < 15:
                continue
            mode = "newest" if item["discovered_from"].startswith("date/") else "popular"
            per_mode[mode].append(item)
    per_mode["newest"].sort(key=lambda x: (x["timestamp"] or 0, x["views"] or 0), reverse=True)
    per_mode["popular"].sort(key=lambda x: (x["views"] or -1, x["timestamp"] or 0), reverse=True)
    chosen, used, channel_counts = [], set(), {}
    order = ["newest", "popular"]
    while len(chosen) < limit:
        progressed = False
        for mode in order:
            while per_mode[mode]:
                item = per_mode[mode].pop(0)
                if item["id"] in used or channel_counts.get(item["channel"], 0) >= 2:
                    continue
                used.add(item["id"])
                chosen.append({**item, "selection_reason": "recent" if mode == "newest" else "most_viewed"})
                channel_counts[item["channel"]] = channel_counts.get(item["channel"], 0) + 1
                progressed = True
                break
            if len(chosen) >= limit:
                break
        if not progressed:
            break
    return chosen


def run_discovery(out_dir: Path, state_path: Path, max_clips: int = 4) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    history = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"seen_ids": []}
    if (not isinstance(history, dict) or not isinstance(history.get("seen_ids"), list)
            or not isinstance(history.get("pending", []), list)):
        raise ValueError("Arquivo de deduplicação/fila inválido")
    seen = set(history["seen_ids"])
    records = {}
    errors = []
    for slug in CHANNELS:
        for sort, period in [("date", "day"), ("view", "week")]:
            key = f"{slug}:{sort}"
            try:
                records[key] = list_clips(slug, sort, period)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as ex:
                records[key] = []
                errors.append({"channel": slug, "sort": sort, "reason": str(ex)[:450]})
    if not any(records.values()) and errors:
        raise RuntimeError("Nenhuma listagem de clipes disponível; Kick pode ter mudado ou bloqueado consultas. "
                           + json.dumps(errors, ensure_ascii=False))
    selected = choose(records, seen, max_clips)
    pending = list(history.get("pending", []))
    pending_ids = {p["id"] for p in pending if isinstance(p, dict) and "id" in p}
    for clip in selected:
        if clip["id"] not in pending_ids:
            pending.append({"id": clip["id"], "url": clip["url"],
                            "channel": clip["channel"],
                            "selection_reason": clip["selection_reason"]})
            pending_ids.add(clip["id"])
    report = {
        "queue_waiting_count": len(pending),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "queries_ok": len(records) - len(errors),
        "queries_failed": len(errors),
        "errors": errors,
        "candidates_count": len(selected),
        "auto_published": False,
        "candidates": selected,
    }
    (out_dir / "candidates.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = ["# Corteszinsbr — clipes encontrados", "",
            f"Consultas com sucesso: **{report['queries_ok']}** de {len(records)}.",
            f"Novos candidatos: **{len(selected)}**; na fila: **{len(pending)}**. Nenhuma publicação automática.", "",
            "| Criador | Seleção | Views | Link |",
            "|---|---|---:|---|"]
    for item in selected:
        rows.append(f"| {item['channel']} | {item['selection_reason']} | {item['views'] if item['views'] is not None else '?'} | [Clipe]({item['url']}) |")
    if errors:
        rows.extend(["", f"Atenção: **{len(errors)} consultas falharam**; cobertura incompleta."])
    md = "\n".join(rows) + "\n"
    (out_dir / "candidates.md").write_text(md, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md)
    # Store only selected identifiers, not video bytes. Keep a finite history.
    for item in selected:
        seen.add(item["id"])
    history["seen_ids"] = (history["seen_ids"] + [i["id"] for i in selected])[-1200:]
    history["pending"] = pending[-100:]
    state_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(md)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "discovery")
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "discovery_seen.json")
    parser.add_argument("--max-clips", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.max_clips <= 8:
        parser.error("max-clips deve ficar entre 1 e 8")
    run_discovery(args.output, args.state, args.max_clips)


if __name__ == "__main__":
    main()
