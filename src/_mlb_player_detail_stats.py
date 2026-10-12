# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Research input only: player pitch/zone statistics from the existing PBP ledger.

No prediction rules, inferred locations, or name-based player joins. One game is held
at a time; count tables retain every observed pitch/hand/zone combination.
"""
from collections import Counter, defaultdict
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path


SWINGS = {"S", "W", "T", "Q", "M", "F", "L", "O", "R", "X", "D", "E"}
WHIFFS = {"S", "W", "M", "Q"}
KNOWN_CALLS = SWINGS | {"B", "*B", "C", "I", "P", "H", "V"}
HITS = {"single", "double", "triple", "home_run"}
NO_AB = {"walk", "intent_walk", "hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf", "sac_fly_double_play", "sac_bunt_double_play"}
AB_EVENTS = HITS | {"strikeout", "strikeout_double_play", "field_out", "force_out", "grounded_into_double_play", "double_play", "triple_play", "field_error", "fielders_choice", "fielders_choice_out", "other_out"}
MOMENTS = ("speed", "spin", "plate_x", "plate_z", "ev", "launch_angle")


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def ratio(a, b):
    return round(a / b, 6) if b else None


def metrics(counts):
    c = dict(counts)
    for key in ("pitches", "swings", "whiffs", "unknown_calls", "terminal_pa", "ab", "hits", "bb", "hbp", "k", "hr", "fair_bbe", "hard_hit", "known_zones", "outside_pitches", "outside_swings"):
        c.setdefault(key, 0)
    c["whiff_per_swing"] = ratio(c["whiffs"], c["swings"]) if not c["unknown_calls"] else None
    c["chase_per_outside_pitch"] = ratio(c["outside_swings"], c["outside_pitches"]) if not c["unknown_calls"] else None
    c["hit_per_ab_ending_here"] = ratio(c["hits"], c["ab"])
    c["hard_hit_per_measured_fair_bbe"] = ratio(c["hard_hit"], c.get("ev_n", 0))
    for key in MOMENTS:
        c[key + "_mean"] = ratio(c.get(key + "_sum", 0), c.get(key + "_n", 0))
    return c


def pitch_counts(e):
    """Physical pitch counters only; no PA or official scorer attribution."""
    pd, hd = e.get("pitch_data") or {}, e.get("hit_data") or {}
    zone = str(pd.get("zone") or "unknown")
    code = e.get("call_code") or e.get("code") or "unknown"
    swing, whiff = code in SWINGS, code in WHIFFS
    fair = e.get("is_in_play") is True
    c = Counter(pitches=1, swings=int(swing), whiffs=int(whiff), unknown_calls=int(code not in KNOWN_CALLS),
                called_strikes=int(code == "C"), balls=int(e.get("is_ball") is True),
                fair_bbe=int(fair), known_zones=int(zone in {str(n) for n in range(1, 15)}),
                outside_pitches=int(zone in {"11", "12", "13", "14"}),
                outside_swings=int(swing and zone in {"11", "12", "13", "14"}))
    c["call_code:" + code] = 1
    coords, br = pd.get("coordinates") or {}, pd.get("breaks") or {}
    vals = {"speed": pd.get("start_speed"), "spin": br.get("spinRate"),
            "plate_x": coords.get("pX"), "plate_z": coords.get("pZ"),
            "ev": hd.get("launch_speed") if fair else None,
            "launch_angle": hd.get("launch_angle") if fair else None}
    for key, value in vals.items():
        value = number(value)
        if value is not None:
            c[key + "_n"] = 1
            c[key + "_sum"] = value
    c["hard_hit"] = int(fair and number(hd.get("launch_speed")) is not None and float(hd["launch_speed"]) >= 95)
    return c


class DetailStats:
    def __init__(self, target_date, cutoff_utc, player_ids):
        self.target = date.fromisoformat(target_date)
        self.cutoff = datetime.fromisoformat(cutoff_utc.replace("Z", "+00:00"))
        if self.cutoff.tzinfo is None:
            raise ValueError("cutoff_utc must be timezone-aware")
        self.ids = {str(v) for v in player_ids}
        self.cells = defaultdict(Counter)
        self.fielding = defaultdict(Counter)
        self.running = defaultdict(Counter)
        self.sources = {}
        self.player_games = defaultdict(set)
        self.coverage = Counter()

    def add(self, record):
        ident = record.get("identity") or {}
        gp, day = str(ident.get("gamePk") or ""), str(ident.get("date") or "")
        source_day = date.fromisoformat(day)
        if source_day >= self.target or source_day.year != self.target.year:
            self.coverage["excluded_target_future_or_other_season_games"] += 1
            return
        digest = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if not gp:
            raise ValueError("missing gamePk")
        if gp in self.sources:
            if self.sources[gp]["record_sha256"] != digest:
                raise ValueError(f"conflicting duplicate game: {gp}")
            self.coverage["identical_duplicate_games"] += 1
            return
        self.sources[gp] = {"date": day, "record_sha256": digest,
                            "raw_path": record.get("source_raw_path"),
                            "raw_sha256": record.get("input_hash_sha256")}
        age = (self.target - source_day).days
        windows = ["season"] + (["30d"] if age <= 30 else []) + (["14d"] if age <= 14 else [])
        for pa in record.get("pa_states") or []:
            players = [(str((pa.get("batter") or {}).get("id") or ""), "batter", pa.get("pitch_hand")),
                       (str((pa.get("pitcher") or {}).get("id") or ""), "pitcher", pa.get("bat_side"))]
            players = [(pid, role, hand or "unknown") for pid, role, hand in players if pid in self.ids]
            if not players:
                continue
            events = pa.get("play_events") or []
            pitches = [e for e in events if e.get("is_pitch")]
            if not (pa.get("about") or {}).get("is_complete"):
                self.coverage["excluded_incomplete_pa"] += 1
                continue
            had_pitch, changed = False, False
            for e in events:
                if had_pitch and e.get("event_type") in {"pitching_substitution", "offensive_substitution"}:
                    changed = True
                had_pitch = had_pitch or bool(e.get("is_pitch"))
            if changed:
                self.coverage["excluded_mid_pa_substitution"] += 1
                continue
            for e in pitches:
                stamp = e.get("end_time") or e.get("start_time")
                if stamp:
                    instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if instant.tzinfo is None or instant >= self.cutoff:
                        raise ValueError(f"pitch is not before cutoff: {gp} {e.get('ref')}")
                else:
                    self.coverage["pitch_timestamp_missing"] += 1
            if not pitches:
                self.coverage["completed_pa_without_pitch"] += 1
            for i, e in enumerate(pitches):
                self.coverage["selected_pitch_events"] += 1
                pd, hd = e.get("pitch_data") or {}, e.get("hit_data") or {}
                pitch, zone = e.get("pitch_type") or "unknown", str(pd.get("zone") or "unknown")
                c = pitch_counts(e)
                if i == len(pitches) - 1:
                    outcome = pa.get("event_type") or "unknown"
                    c.update(terminal_pa=1, ab=int(outcome in AB_EVENTS), hits=int(outcome in HITS),
                             bb=int(outcome in {"walk", "intent_walk"}), hbp=int(outcome == "hit_by_pitch"),
                             k=int(outcome.startswith("strikeout")), hr=int(outcome == "home_run"),
                             unknown_terminal=int(outcome not in AB_EVENTS | NO_AB))
                for pid, role, hand in players:
                    self.player_games[pid].add(gp)
                    for window in windows:
                        # Signed coordinate sums must not use Counter's '+' (drops negatives).
                        self.cells[(pid, role, window, hand, pitch, zone)].update(c)
        for row in record.get("fielding_credit_ledger") or []:
            pid = str((row.get("fielder") or {}).get("id") or "")
            if pid not in self.ids:
                continue
            position = (row.get("position") or {}).get("abbreviation") or "unknown"
            self.player_games[pid].add(gp)
            for window in windows:
                self.fielding[(pid, window, position)].update(credits=1, errors=int(row.get("is_error_credit") is True))
        for row in record.get("runner_movement_ledger") or []:
            pid = str((row.get("runner") or {}).get("id") or "")
            if pid not in self.ids:
                continue
            event = str(row.get("event_type") or "")
            self.player_games[pid].add(gp)
            for window in windows:
                self.running[(pid, window)].update(movements=1, outs=int(row.get("is_out") is True),
                    scoring_events=int(row.get("is_scoring_event") is True),
                    stolen_bases=int(event.startswith("stolen_base")),
                    caught_stealing=int(event.startswith("caught_stealing") or event.startswith("pickoff_caught_stealing")),
                    pickoff_outs=int(event.startswith("pickoff") and row.get("is_out") is True))

    def player(self, pid):
        pid = str(pid)
        output = {"player_id": pid, "source_gamePks": sorted(self.player_games[pid]), "roles": {}}
        cells = sorted((key, c) for key, c in self.cells.items() if key[0] == pid)
        for role in ("batter", "pitcher"):
            views = {}
            for window in ("14d", "30d", "season"):
                groups, total = [], Counter()
                for (p, r, w, hand, pitch, zone), c in cells:
                    if (p, r, w) != (pid, role, window):
                        continue
                    total.update(c)
                    groups.append({"opponent_hand": hand, "pitch_type": pitch, "zone": zone, "stats": metrics(c)})
                views[window] = {"status": "observed" if groups else "no_observations_in_selected_sources",
                                 "total": metrics(total), "by_hand_pitch_zone": groups}
            output["roles"][role] = views
        output["fielding_by_position"] = [{"window": w, "position": pos, **dict(c)}
                                           for (p, w, pos), c in sorted(self.fielding.items()) if p == pid]
        output["defensive_start_coordinates"] = {"status": "not_available_in_this_source", "value": None}
        output["runner_windows"] = {w: dict(self.running[(pid, w)]) for w in ("14d", "30d", "season")}
        return output


def build_inputs(request, records, out_dir):
    """Attach full, unranked per-player tables to verified target rosters."""
    rosters = request["rosters"]
    ids = {p["id"] for r in rosters.values() for t in r["teams"].values() for p in t["players"]}
    cutoff = min(r["first_pitch_utc"] for r in rosters.values())
    stats = DetailStats(request["target_date"], cutoff, ids)
    for record in records:
        stats.add(record)
    profiles = {pid: stats.player(pid) for pid in sorted(ids)}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = {"schema": "mlb.player_detail_inputs.v1", "target_date": request["target_date"], "cutoff_utc": cutoff,
              "coverage": dict(stats.coverage), "source_games": len(stats.sources), "players": len(profiles),
              "source_max_date": max((s["date"] for s in stats.sources.values()), default=None),
              "source_scope": "provided ledger games only; not proof of complete season/career or historical availability",
              "source_identity": "caller-selected history; this reducer does not certify official game status",
              "definitions": {"whiff": "S/W/M/Q divided by observed swings; unknown calls make rate null",
                              "hit_rate": "hits/AB classified by the LAST pitch, not causation by that pitch",
                              "fair_contact": "is_in_play true; fouls excluded even if EV exists; absent EV is not zero",
                              "fielding": "credited events by official position, not defensive chances or starting coordinates",
                              "windows": "official source date < target; same-day earlier DH excluded; every pitch timestamp < cutoff when present",
                              "speed_units": "mph", "coordinate_units": "feet; raw pX/pZ, no handedness mirroring"},
              "production_changed": False, "all_inputs_complete": False, "independent_review": "PENDING"}
    write(out / "sources.json", stats.sources)
    for gp, roster in rosters.items():
        teams = {}
        for side, team in roster["teams"].items():
            players = []
            for p in team["players"]:
                pid = str(p["id"])
                players.append({"player_id": pid, "name": p["name"], "position": p.get("position"),
                                "batting_order": p.get("batting_order"), "details": profiles[pid]})
            teams[side] = {"name": team["name"], "batting_order": team["batting_order"],
                           "probable_pitcher": team["probable_pitcher"], "players": players}
        write(out / f"{gp}.player_details.json", {**report, "gamePk": gp, "teams": teams,
              "roster_note": "roster membership is not a reliever availability projection; no name join or top-k removal"})
    write(out / "REPORT.json", report)
    return report


def write(path, obj):
    content = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise ValueError(f"version changed output before replacing: {path}")
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)
