# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Dated, unranked Statcast supplements for direct pregame review, not predictions."""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / '_pydeps')]
from _mlb_player_detail_stats import pitch_counts, metrics, number, ratio
from mlb_evidence_field_contract import PHYSICS, SWING, CONTACT, field_contract, selected_columns

MEASURES = ('release_extension', 'pfx_x', 'pfx_z', 'release_pos_x',
            'release_pos_y', 'release_pos_z', 'spin_axis', 'bat_speed', 'swing_length')
COLUMNS = ('game_pk', 'game_date', 'game_type', 'batter', 'pitcher', 'stand', 'p_throws',
           'pitch_type', 'zone', 'description', 'type', 'release_speed',
           'release_spin_rate', 'plate_x', 'plate_z', 'launch_speed', 'launch_angle',
           'launch_speed_angle', 'estimated_woba_using_speedangle') + MEASURES
CALLS = {'ball': 'B', 'blocked_ball': 'B', 'called_strike': 'C',
         'swinging_strike': 'S', 'swinging_strike_blocked': 'W', 'missed_bunt': 'M',
         'foul_tip': 'T', 'foul': 'F', 'foul_bunt': 'L', 'hit_by_pitch': 'H',
         'hit_into_play': 'X', 'pitchout': 'P', 'intent_ball': 'I',
         'foul_pitchout': 'O', 'swinging_pitchout': 'Q'}
CALLS['bunt_foul_tip'] = 'R'
AUTOMATIC = {'automatic_ball', 'automatic_strike'}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)
    if path.exists():
        assert read(path) == obj, f'Immutable output exists: {path}'
        return
    tmp = path.with_suffix('.tmp')
    tmp.write_text(body, encoding='utf-8')
    tmp.replace(path)


def counts(row, full=False):
    if row.get('description') in AUTOMATIC:
        return Counter({row['description']: 1})
    zone = number(row.get('zone'))
    fair = row.get('type') == 'X'
    c = pitch_counts({'call_code': CALLS.get(row.get('description'), 'unknown'),
        'is_in_play': fair, 'is_ball': row.get('description') in ('ball', 'blocked_ball'),
        'pitch_data': {'zone': int(zone) if zone is not None else None,
            'start_speed': row.get('release_speed'),
            'coordinates': {'pX': row.get('plate_x'), 'pZ': row.get('plate_z')},
            'breaks': {'spinRate': row.get('release_spin_rate')}},
        'hit_data': {'launch_speed': row.get('launch_speed'), 'launch_angle': row.get('launch_angle')}})
    measures = tuple(dict.fromkeys((*MEASURES,*PHYSICS,*SWING,*CONTACT))) if full else MEASURES
    for key in measures:
        if key in CONTACT and not fair:
            continue
        value = number(row.get(key))
        # Spin direction is circular; retain vector components, never an arithmetic mean.
        if key == 'spin_axis' and value is not None:
            import math
            c['spin_axis_sin_sum'] += math.sin(math.radians(value))
            c['spin_axis_cos_sum'] += math.cos(math.radians(value))
            c['spin_axis_n'] += 1
        elif value is not None:
            c[key + '_sum'] += value
            c[key + '_n'] += 1
    if fair:
        value = number(row.get('estimated_woba_using_speedangle'))
        if value is not None:
            c.update(contact_xwoba_n=1, contact_xwoba_sum=value)
        barrel = number(row.get('launch_speed_angle'))
        if barrel is not None:
            c.update(barrel_class_n=1, barrels=int(barrel == 6))
    return c


def summarize(c, full=False):
    out = metrics(c)
    # This is physical-pitch evidence, not official PA attribution (mid-PA substitutions).
    for key in ('terminal_pa', 'ab', 'hits', 'bb', 'hbp', 'k', 'hr', 'hit_per_ab_ending_here'):
        out.pop(key, None)
    measures = tuple(dict.fromkeys((*MEASURES,*PHYSICS,*SWING,*CONTACT))) if full else MEASURES
    for key in measures:
        if key != 'spin_axis':
            out[key + '_mean'] = ratio(c.get(key + '_sum', 0), c.get(key + '_n', 0))
    if c.get('spin_axis_n'):
        import math
        x, y = c['spin_axis_cos_sum'], c['spin_axis_sin_sum']
        strength = math.hypot(x, y) / c['spin_axis_n']
        out['spin_axis_resultant_length'] = round(strength, 6)
        out['spin_axis_circular_mean'] = round(math.degrees(math.atan2(y, x)) % 360, 6) if strength > 1e-8 else None
    else:
        out['spin_axis_circular_mean'] = None
        out['spin_axis_resultant_length'] = None
    out['contact_xwoba_mean'] = ratio(c.get('contact_xwoba_sum', 0), c.get('contact_xwoba_n', 0))
    return out


class AbilityStats:
    def __init__(self, target, ids, full=False):
        self.target = date.fromisoformat(target)
        self.ids = {str(i) for i in ids}
        self.cells = defaultdict(Counter)
        self.player_cells = defaultdict(set)
        self.appearances = defaultdict(Counter)
        self.pairs = defaultdict(Counter)
        self.sources = defaultdict(set)
        self.coverage = Counter()
        self.full = full
        self.contexts = defaultdict(Counter)
        self.player_context_keys = defaultdict(set)

    def add(self, row):
        day = str(row['game_date'])[:10]
        d = date.fromisoformat(day)
        if d >= self.target or d.year != self.target.year or row['game_type'] != 'R':
            self.coverage['excluded_date_or_game_type'] += 1
            return
        bid, pid, gp = (str(int(row[k])) for k in ('batter', 'pitcher', 'game_pk'))
        if not {bid, pid} & self.ids:
            return
        self.coverage['selected_rows'] += 1
        self.coverage['selected_physical_pitches' if row.get('description') not in AUTOMATIC else 'selected_automatic_calls'] += 1
        age = (self.target - d).days
        windows = ['season'] + (['30d'] if age <= 30 else []) + (['14d'] if age <= 14 else [])
        pitch = row.get('pitch_type') or 'unknown'
        z = number(row.get('zone'))
        zone = str(int(z)) if z is not None else 'unknown'
        c = counts(row, self.full)
        for who, role, hand in ((bid, 'batter', row.get('p_throws')), (pid, 'pitcher', row.get('stand'))):
            if who not in self.ids:
                continue
            self.sources[who].add((day, gp))
            for window in windows:
                key = (who, role, window, hand or 'unknown', pitch, zone)
                self.cells[key].update(c)
                self.player_cells[who].add(key)
            self.appearances[(who, role, day, gp)].update(c)
            if self.full:
                def value(key):
                    v=row.get(key)
                    if v is None or (isinstance(v,float) and not math.isfinite(v)) or v=='':return 'unknown'
                    return str(int(v)) if isinstance(v,float) and v.is_integer() else str(v)
                values = {'count': f"{value('balls')}-{value('strikes')}",
                    'times_through_order': value('n_thruorder_pitcher'),
                    'prior_batter_pa_in_game': value('n_priorpa_thisgame_player_at_bat'),
                    'inning_outs': f"{value('inning')}:{value('inning_topbot')}:{value('outs_when_up')}",
                    'base_occupancy': ''.join('1' if number(row.get(k)) is not None else '0' for k in ('on_1b','on_2b','on_3b')) if all(k in row for k in ('on_1b','on_2b','on_3b')) else 'unknown',
                    'fielding_alignment': f"{value('if_fielding_alignment')}:{value('of_fielding_alignment')}",
                    'catcher_id': value('fielder_2'),
                    'days_since_prior_game': value('pitcher_days_since_prev_game' if role=='pitcher' else 'batter_days_since_prev_game')}
                if row.get('type') == 'X':
                    values.update(batted_ball_type=value('bb_type'),first_fielder_position=value('hit_location'))
                core={k:v for k,v in c.items() if k in ('pitches','swings','whiffs','unknown_calls','fair_bbe','hard_hit','ev_n','ev_sum','automatic_ball','automatic_strike')}
                for window in windows:
                    for family,value in values.items():
                        key=(who,role,window,family,value)
                        self.contexts[key].update(core)
                        self.player_context_keys[who].add(key)
        if bid in self.ids and pid in self.ids:
            for window in windows:
                self.pairs[(bid, pid, window, pitch, zone)].update(c)

    def player(self, pid):
        pid = str(pid)
        roles = {}
        for role in ('batter', 'pitcher'):
            views = {}
            for window in ('season', '30d', '14d'):
                groups, total = [], Counter()
                for key in sorted(self.player_cells[pid]):
                    who, r, w, hand, pitch, zone = key
                    c = self.cells[key]
                    if (who, r, w) == (pid, role, window):
                        total.update(c)
                        groups.append({'opponent_hand': hand, 'pitch_type': pitch, 'zone': zone, 'stats': summarize(c,self.full)})
                views[window] = {'status': 'observed' if groups else 'no_observations', 'total': summarize(total,self.full), 'by_hand_pitch_zone': groups}
            roles[role] = views
        return {'player_id': pid, 'target_date': self.target.isoformat(), 'roles': roles,'full_evidence':self.full,
            'historical_context': [{'role':key[1],'window':key[2],'family':key[3],'value':key[4],'counts':dict(self.contexts[key])}
                for key in sorted(self.player_context_keys[pid])],
            'appearances': [{'role': role, 'date': day, 'gamePk': gp, 'stats': summarize(c,self.full)}
                for (who, role, day, gp), c in sorted(self.appearances.items()) if who == pid],
            'sources': [{'date': day, 'gamePk': gp} for day, gp in sorted(self.sources[pid])]}


def build(request, source, out):
    import pyarrow.parquet as pq
    assert sha(source) == request['source_sha256'], 'Source changed'
    full=bool(request.get('full_evidence'))
    stats = AbilityStats(request['target_date'], request['players'],full)
    pf = pq.ParquetFile(source)
    assert set(COLUMNS) <= set(pf.schema.names)
    contract=field_contract(pf.schema.names)
    columns=selected_columns(pf.schema.names) if full else list(COLUMNS)
    try:
        for batch in pf.iter_batches(batch_size=4096, columns=columns):
            for row in batch.to_pylist():
                stats.add(row)
    finally:
        pf.close()
    players = {}
    for pid, name in request['players'].items():
        path = out / 'players' / (pid + '.json')
        save(path, stats.player(pid) | {'name': name, 'advanced_tables': request.get('advanced_players', {}).get(pid, [])})
        players[pid] = {'path': 'players/' + pid + '.json', 'sha256': sha(path)}
    pairs = [{'batter_id': bid, 'pitcher_id': pid, 'window': w, 'pitch_type': p, 'zone': z, 'stats': summarize(c,full)}
             for (bid, pid, w, p, z), c in sorted(stats.pairs.items())]
    save(out / 'matchups.json', pairs)
    save(out / 'SOURCE_CONTEXT.json', request.get('source_context', {}))
    save(out / 'FIELD_CONTRACT.json',contract)
    days = [day for values in stats.sources.values() for day, _ in values]
    manifest = {'schema': 'mlb.ability_supplement.v2' if full else 'mlb.ability_supplement.v1', 'target_date': request['target_date'],
        'full_evidence':full,'field_contract':{'path':'FIELD_CONTRACT.json','sha256':sha(out/'FIELD_CONTRACT.json')},
        'source_sha256': request['source_sha256'], 'source_max_date': max(days, default=None),
        'request_sha256': hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
        'players': players, 'matchups': {'path': 'matchups.json', 'sha256': sha(out / 'matchups.json')},
        'source_context': {'path': 'SOURCE_CONTEXT.json', 'sha256': sha(out / 'SOURCE_CONTEXT.json')},
        'coverage': dict(stats.coverage), 'columns_used': columns,
        'columns_excluded': sorted(set(pq.read_schema(source).names) - set(columns)),
        'analysis_ready': False, 'reason': 'Supplement only; requires complete daily history, confirmed roster and market',
        'limitations': ['Physical pitches, not scorer PA totals. Automatic balls/strikes are separate counters, not pitches. Use PBP/box for BB/K/HR/OPS.',
            'Same-season history only, not career BvP.', 'Historical source was fetched later; date-cut replay is not a vintage snapshot.',
            'Unknown measurements remain null; no observations is not zero skill.',
            'Pitch type and zone describe observed outcome groups, not causal effects.',
            'No ranking, automatic direction, weights, or model inference.']}
    save(out / 'INDEX.json', manifest)
    print(json.dumps({'players': len(players), 'pair_cells': len(pairs), 'coverage': manifest['coverage']}), flush=True)


def attach(index_path, target, player_ids, start_utc, history_through=None):
    path = Path(index_path)
    index = read(path)
    assert index['schema'] in ('mlb.ability_supplement.v1','mlb.ability_supplement.v2')
    assert index['target_date'] == target and index['source_max_date'] < target
    if history_through is not None:
        assert index['source_max_date'] == history_through, 'Ability and PBP history dates differ; rebuild supplement first'
    assert set(player_ids) <= set(index['players']), 'Missing ability player'
    context_path = path.parent / index['source_context']['path']
    assert context_path.resolve().is_relative_to(path.parent.resolve())
    assert sha(context_path) == index['source_context']['sha256']
    context = read(context_path)
    start = datetime.fromisoformat(start_utc.replace('Z', '+00:00'))
    assert start.tzinfo is not None
    for row in context.get('tables', []):
        stamp = datetime.fromisoformat(row['captured_utc'].replace('Z', '+00:00'))
        assert stamp.tzinfo is not None and stamp < start, 'Advanced source was captured after target start'
    refs = {}
    for pid in player_ids:
        row = index['players'][pid]
        p = (path.parent / row['path']).resolve()
        assert p.is_relative_to(path.parent.resolve()) and sha(p) == row['sha256']
        refs[pid] = {'path': str(p), 'sha256': row['sha256']}
    pairs = path.parent / index['matchups']['path']
    assert pairs.resolve().is_relative_to(path.parent.resolve()) and sha(pairs) == index['matchups']['sha256']
    result = {'index': {'path': str(path.resolve()), 'sha256': sha(path)}, 'players': refs,
            'matchups': {'path': str(pairs.resolve()), 'sha256': sha(pairs)},
            'source_context': {'path': str(context_path.resolve()), 'sha256': sha(context_path)},
            'source_max_date': index['source_max_date'], 'limitations': index['limitations']}
    if index.get('full_evidence'):
        info=index['field_contract'];contract=(path.parent/info['path']).resolve()
        assert contract.is_relative_to(path.parent.resolve()) and sha(contract)==info['sha256']
        result.update(full_evidence=True,field_contract={'path':str(contract),'sha256':sha(contract)})
    return result


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--request', type=Path, required=True)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    a = ap.parse_args()
    build(read(a.request), a.source, a.out)
