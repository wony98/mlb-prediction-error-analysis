# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Confirmed starter matchups, conditional bullpen, and optional bench evidence.

Coverage checks cannot prove semantic attention or predict appearances/results.
"""
import argparse
from collections import Counter, defaultdict
import json
import hashlib
from pathlib import Path
from mlb_ability_inputs import read, save, sha, summarize
from mlb_evidence_field_contract import PHYSICS, SWING, CONTACT
from mlb_full_review_views import required_review_fields

WINDOWS = ('season', '30d', '14d')
BASE_COUNTS = {'pitches', 'swings', 'whiffs', 'unknown_calls', 'fair_bbe', 'hard_hit'}
MEASUREMENTS = ('speed', 'spin', 'release_extension', 'pfx_x', 'pfx_z', 'ev', 'bat_speed', 'swing_length')


def bucket(zone):
    if str(zone) in {str(i) for i in range(1, 10)}:
        return 'in_zone'
    if str(zone) in {'11', '12', '13', '14'}:
        return 'outside_zone'
    return 'unknown_zone'


def compact(profile, target):
    assert profile['target_date'] == target
    assert all(s['date'] < target for s in profile.get('sources', []))
    result = {'_full_evidence': bool(profile.get('full_evidence')),
              '_historical_context':profile.get('historical_context')}
    keys = BASE_COUNTS | {m + suffix for m in MEASUREMENTS for suffix in ('_sum', '_n')}
    for role, views in profile['roles'].items():
        cells = defaultdict(Counter)
        for window in WINDOWS:
            for row in views[window]['by_hand_pitch_zone']:
                key = (window, row['opponent_hand'], row['pitch_type'], bucket(row['zone']))
                cells[key].update({k: v for k, v in row['stats'].items() if k in keys or
                    (result['_full_evidence'] and (k.endswith('_sum') or k.endswith('_n') or k in ('barrels','barrel_class_n','balls','called_strikes')))})
        result[role] = dict(cells)
    return result


def stats(counts, full=False):
    value = summarize(counts, full)
    if full:
        return value
    keys = BASE_COUNTS | {'ev_n', 'whiff_per_swing'}
    keys |= {m + suffix for m in MEASUREMENTS for suffix in ('_mean', '_n')}
    return {k: value.get(k, 0 if k in BASE_COUNTS or k.endswith('_n') else None) for k in sorted(keys)}


def expected_pairs(roster):
    assert set(roster['teams']) == {'away', 'home'}, 'Both teams are required'
    for team in roster['teams'].values():
        ids = [p['id'] for p in team['players']]
        assert len(ids) == len(set(ids)), 'Duplicate roster player'
        order = team['batting_order']
        assert order and len(order) == len(set(order)), 'Missing or duplicate batting order'
        assert set(order) <= set(ids), 'Batting-order player missing from roster'
        assert all((p.get('position') or {}).get('abbreviation') for p in team['players']), 'Missing player position'
    expected = []
    for side, team in roster['teams'].items():
        other = roster['teams']['home' if side == 'away' else 'away']
        starter = str(team['probable_pitcher']['id'])
        arms = [p for p in team['players'] if p['id'] == starter or p['position']['abbreviation'] == 'P'
                or (p.get('primary_position') or {}).get('abbreviation') == 'P'
                or any(x.get('abbreviation') == 'P' for x in p.get('all_positions') or [])]
        hitters = [p for p in other['players'] if p['id'] in other['batting_order'] or p.get('position', {}).get('abbreviation') != 'P']
        assert any(p['id'] == starter for p in arms), 'Starter missing from roster'
        assert len({p['id'] for p in arms}) == len(arms)
        assert len({p['id'] for p in hitters}) == len(hitters)
        for arm in arms:
            for hitter in hitters:
                ph = arm.get('pitch_hand', {}).get('code') or 'unknown'
                bh = hitter.get('bat_side', {}).get('code') or 'unknown'
                if bh == 'S':
                    bh = ('L' if ph == 'R' else 'R') if ph in ('L', 'R') else 'unknown'
                expected.append({'id': f"{side}:{arm['id']}:{hitter['id']}", 'pitcher_id': arm['id'],
                    'batter_id': hitter['id'], 'pitcher_name': arm['name'], 'batter_name': hitter['name'],
                    'pitcher_side': side, 'pitch_hand': ph, 'bat_side': bh,
                    'pitcher_role': 'starter' if arm['id'] == starter else 'roster_pitcher_not_guaranteed_to_appear',
                    'batter_role': 'starting_lineup' if hitter['id'] in other['batting_order'] else 'bench_not_guaranteed_to_appear'})
    assert len({r['id'] for r in expected}) == len(expected)
    return expected


def pair_cells(pair, profiles):
    pitching = profiles[pair['pitcher_id']]['pitcher']
    batting = profiles[pair['batter_id']]['batter']
    full=bool(profiles[pair['pitcher_id']].get('_full_evidence'))
    assert full == bool(profiles[pair['batter_id']].get('_full_evidence')), 'Mixed full and legacy player inputs'
    repertoire = sorted({k[2] for k, c in pitching.items() if c.get('pitches', 0)})
    cells = []
    for window in WINDOWS:
        for pitch in repertoire:
            for zone in ('in_zone', 'outside_zone', 'unknown_zone'):
                a = pitching.get((window, pair['bat_side'], pitch, zone), Counter())
                b = batting.get((window, pair['pitch_hand'], pitch, zone), Counter())
                if not a.get('pitches') and not b.get('pitches'):
                    continue
                present_a, present_b = bool(a.get('pitches')), bool(b.get('pitches'))
                cells.append({'id': f'{window}:{pitch}:{zone}', 'window': window, 'pitch_type': pitch,
                    'zone_group': zone, 'pitcher': stats(a,full), 'batter': stats(b,full),
                    'status': 'both_observed' if present_a and present_b else ('pitcher_observation_missing' if not present_a else 'batter_observation_missing')})
    return cells


def confirmed_pairs(catalog):
    return [p for p in catalog if p['pitcher_role'] == 'starter' and p['batter_role'] == 'starting_lineup']


def scenario_context(roster, catalog, refs, bullpen):
    required_ids = {p['id'] for p in confirmed_pairs(catalog)}
    if bullpen is not None:
        assert str(bullpen['gamePk']) == str(roster['gamePk'])
        assert bullpen['target_date'] == roster['target_date'] and bullpen['source_max_date'] < roster['target_date']
    teams = {}
    for side, team in roster['teams'].items():
        estimates = {str(p['player_id']): p for p in (bullpen or {}).get('teams', {}).get(side, [])}
        ids = sorted({p['pitcher_id'] for p in catalog if p['pitcher_side'] == side and p['pitcher_role'] != 'starter'})
        names = {p['id']: p['name'] for p in team['players']}
        teams[side] = {'pitchers': [{'player_id': pid, 'name': names[pid],
            'projection': estimates.get(pid),
            'projection_status': 'estimate_only' if pid in estimates else 'not_provided',
            'workload_scope': estimates.get(pid, {}).get('workload_scope', 'not_certified_in_this_snapshot'),
            'appearance_confirmed': False} for pid in ids],
            'bench': [{'player_id': p['id'], 'name': p['name'], 'appearance_confirmed': False}
                for p in team['players'] if p['id'] not in team['batting_order'] and p['position']['abbreviation'] != 'P']}
    return {'gamePk': roster['gamePk'], 'target_date': roster['target_date'], 'teams': teams,
        'optional_pairs': [p for p in catalog if p['id'] not in required_ids],
        'player_sources': refs,
        'rule': 'Roster membership is not an appearance forecast. Select bullpen candidates using workload/role and game state. Bench is optional. No automatic probability or prediction.'}


def build(roster, profiles, refs, out, bullpen=None, source_packet=None):
    target = roster['target_date']
    catalog = expected_pairs(roster)
    expected = confirmed_pairs(catalog)
    needed = {r[k] for r in expected for k in ('pitcher_id', 'batter_id')}
    assert needed <= set(profiles) and needed <= set(refs)
    indexed = {pid: compact(profiles[pid], target) for pid in needed}
    return build_indexed(roster, catalog, indexed, refs, out, bullpen,source_packet)


def build_indexed(roster, catalog, indexed, refs, out, bullpen=None, source_packet=None):
    if source_packet:
        source_path=Path(source_packet['path'])
        assert sha(source_path)==source_packet['sha256']
        source_index=read(source_path)
        if source_index.get('schema')=='mlb.pregame_sources.v1':
            entry=next(e for e in source_index['entries'] if e['id']=='bullpen')
            path=(source_path.parent/entry['path']).resolve()
            assert path.is_relative_to(source_path.parent.resolve()) and sha(path)==entry['sha256']
            data=read(path)['data']
            assert data.get('official_workload_connected'), 'Official all-appearance workload required'
            bullpen=data
    expected = confirmed_pairs(catalog)
    grouped = defaultdict(list)
    for pair in expected:
        grouped[pair['pitcher_id']].append(pair)
    paths, total_cells = {}, 0
    for pid, pairs in grouped.items():
        rows = []
        for pair in pairs:
            cells = pair_cells(pair, indexed)
            rows.append(pair | {'cells': cells, 'status': 'observed_cells' if cells else 'no_comparable_observations',
                'pitcher_source': refs[pid], 'batter_source': refs[pair['batter_id']],
                'interpretation': 'Matched conditions across separate histories, not necessarily direct historical BvP.'})
            total_cells += len(cells)
        path = out / 'pitchers' / (pid + '.json')
        save(path, {'gamePk': roster['gamePk'], 'target_date': roster['target_date'], 'pairs': rows})
        paths[pid] = {'path': 'pitchers/' + pid + '.json', 'sha256': sha(path), 'pair_ids': [r['id'] for r in rows]}
    context = scenario_context(roster, catalog, refs, bullpen)
    save(out / 'SCENARIO_CONTEXT.json', context)
    manifest = {'schema': 'mlb.scoped_matchup_review.v2', 'gamePk': roster['gamePk'], 'target_date': roster['target_date'],
        'full_evidence':all(p.get('_full_evidence',False) for p in indexed.values()),
        'source_packet':source_packet,
        'roster_sha256': hashlib.sha256(json.dumps(roster, sort_keys=True).encode()).hexdigest(),
        'pitchers': paths, 'pair_count': len(expected), 'cell_count': total_cells,
        'expected_pair_ids': sorted(r['id'] for r in expected), 'review_status': 'PENDING',
        'optional_pair_count': len(context['optional_pairs']),
        'scenario_context': {'path': 'SCENARIO_CONTEXT.json', 'sha256': sha(out / 'SCENARIO_CONTEXT.json')},
        'no_automatic_prediction_or_importance_threshold': True,
        'semantic_attention_proven': False,
        'scope': 'Required: confirmed starters x opposing starting lineup. Bullpen reviewed as conditional team scenarios; bullpen/bench detailed pairs expanded only when relevant.',
        'limits': 'Conditions link rather than prove causation. In/out/unknown groups retain references to exact-zone source files. No claim of all-source completeness.'}
    if manifest['full_evidence']:
        manifest['role_interpretation']={'pitcher':'Pitch physics describe his pitches; swing/contact describe opponents he faced.',
            'batter':'Swing/contact describe this hitter; pitch physics describe pitches he faced, not his own throwing.'}
        roles={(p['pitcher_id'],'pitcher') for p in expected}|{(p['batter_id'],'batter') for p in expected}
        contexts=[]
        for pid,role in sorted(roles):
            raw=indexed[pid].get('_historical_context')
            rows=[r for r in raw or [] if r['role']==role and r['window'] in WINDOWS]
            contexts.append({'id':pid+':'+role,'player_id':pid,'role':role,'source':refs[pid],
                'status':'missing_source_section' if raw is None else ('observed' if rows else 'no_observations'),
                'rows':rows})
        save(out/'PLAYER_CONTEXT.json',{'gamePk':roster['gamePk'],'target_date':roster['target_date'],'players':contexts})
        manifest['player_context']={'path':'PLAYER_CONTEXT.json','sha256':sha(out/'PLAYER_CONTEXT.json')}
    save(out / 'INDEX.json', manifest)
    save(out / 'REVIEW_PENDING.json', {'index_sha256': sha(out / 'INDEX.json'), 'pairs': [
        {'id': r['id'], 'examined_cell_ids': [], 'WIN_LOSS': None, 'RUN_TOTAL': None,
         'field_family_reviews':{family:{'status':None,'examined_fields':[],'reason':None}
            for family in ('contact_quality','pitch_physics','swing_mechanics')}} for r in expected],
        'conditional_pairs': [], 'source_reviews':None,'player_context_reviews':[],
        'bullpen': {side: {'candidate_ids': [], 'usage_and_role_reason': None, 'game_state_conditions': None,
            'starter_exit_assumption': None, 'WIN_LOSS_effect': None, 'RUN_TOTAL_effect': None, 'uncertainty': None,
            'appearance_assumed_certain': False} for side in ('away', 'home')},
        'final': {'WIN_LOSS': None, 'RUN_TOTAL': None}})
    return manifest


def build_files(roster, refs, out, bullpen=None, source_packet=None):
    assert all(len(t['batting_order']) == 9 for t in roster['teams'].values()), 'Confirmed nine-player orders required'
    catalog = expected_pairs(roster)
    expected = confirmed_pairs(catalog)
    needed = {r[k] for r in expected for k in ('pitcher_id', 'batter_id')}
    indexed = {}
    for pid in sorted(needed):
        source = refs[pid]
        assert sha(source['path']) == source['sha256'], 'Player source changed'
        profile = read(source['path'])
        assert profile['player_id'] == pid
        indexed[pid] = compact(profile, roster['target_date'])
    return build_indexed(roster, catalog, indexed, refs, out, bullpen,source_packet)


def load_context(index_path, index):
    base = Path(index_path).parent.resolve()
    info = index['scenario_context']
    path = (base / info['path']).resolve()
    assert path.is_relative_to(base) and sha(path) == info['sha256']
    context = read(path)
    assert context['target_date'] == index['target_date'] and str(context['gamePk']) == str(index['gamePk'])
    return context


def optional_path(index_path, pair):
    assert pair['pitcher_side'] in ('away', 'home')
    assert str(pair['pitcher_id']).isdigit() and str(pair['batter_id']).isdigit()
    return Path(index_path).parent / 'optional' / f"{pair['pitcher_side']}_{pair['pitcher_id']}_{pair['batter_id']}.json"


def expand_optional(index_path, pair_id):
    index = read(index_path)
    context = load_context(index_path, index)
    pair = next(p for p in context['optional_pairs'] if p['id'] == pair_id)
    indexed, refs = {}, {}
    for pid in (pair['pitcher_id'], pair['batter_id']):
        source = context['player_sources'][pid]
        assert sha(source['path']) == source['sha256']
        profile = read(source['path'])
        assert profile['player_id'] == pid
        indexed[pid] = compact(profile, index['target_date'])
        refs[pid] = source
    result = pair | {'cells': pair_cells(pair, indexed), 'index_sha256': sha(index_path),
        'target_date': index['target_date'], 'gamePk': index['gamePk'],
        'pitcher_source': refs[pair['pitcher_id']], 'batter_source': refs[pair['batter_id']],
        'interpretation': 'Conditional comparison; not a confirmed appearance or direct historical BvP.'}
    path = optional_path(index_path, pair)
    save(path, result)
    return path


def check_review(index_path, review_path):
    index, review = read(index_path), read(review_path)
    assert index['schema'] in ('mlb.complete_matchup_review.v1', 'mlb.scoped_matchup_review.v2')
    errors = []
    if review.get('index_sha256') != sha(index_path):
        errors.append('review_input_hash_mismatch')
    expected = set(index['expected_pair_ids'])
    entries = review.get('pairs', [])
    seen = [r.get('id') for r in entries]
    if len(set(seen)) != len(seen) or set(seen) != expected:
        errors.append('missing_duplicate_or_extra_pair_reviews')
    cells = {}
    required_context_ids=set()
    observed_families = {}
    actual_cell_count = 0
    for info in index['pitchers'].values():
        path = (Path(index_path).parent / info['path']).resolve()
        assert path.is_relative_to(Path(index_path).parent.resolve()) and sha(path) == info['sha256']
        pack = read(path)
        if str(pack['gamePk']) != str(index['gamePk']) or pack['target_date'] != index['target_date']:
            errors.append('pitcher_pack_identity_mismatch')
        if [p['id'] for p in pack['pairs']] != info['pair_ids']:
            errors.append('pitcher_pack_pair_list_mismatch')
        for pair in pack['pairs']:
            required_context_ids.update((pair['pitcher_id']+':pitcher',pair['batter_id']+':batter'))
            ids = [c['id'] for c in pair['cells']]
            if pair['id'] in cells or len(ids) != len(set(ids)):
                errors.append('duplicate_pair_or_cell_in_source')
            cells[pair['id']] = set(ids)
            observed_families[pair['id']]={family:any(c[role].get(field+'_n',0)>0 for c in pair['cells'] for role in ('pitcher','batter') for field in fields)
                for family,fields in {'pitch_physics':('speed','spin',*PHYSICS),'swing_mechanics':tuple(SWING),'contact_quality':('ev','launch_angle',*CONTACT)}.items()}
            actual_cell_count += len(ids)
    if set(cells) != expected or len(cells) != index['pair_count'] or actual_cell_count != index['cell_count']:
        errors.append('manifest_pair_coverage_mismatch')
    conditional_entries = []
    if index.get('schema') == 'mlb.scoped_matchup_review.v2':
        context = load_context(index_path, index)
        optional = {p['id']: p for p in context['optional_pairs']}
        conditional_entries = review.get('conditional_pairs', [])
        optional_seen = [p.get('id') for p in conditional_entries]
        if len(optional_seen) != len(set(optional_seen)) or not set(optional_seen) <= set(optional):
            errors.append('unknown_or_duplicate_conditional_pair')
        for row in conditional_entries:
            pid = row.get('id')
            if pid not in optional:
                continue
            path = optional_path(index_path, optional[pid])
            if not path.exists() or row.get('input_sha256') != sha(path):
                errors.append(f'conditional_pair_input_mismatch:{pid}')
                continue
            data = read(path)
            assert data['id'] == pid and data['index_sha256'] == sha(index_path)
            cells[pid] = {c['id'] for c in data['cells']}
            observed_families[pid]={family:any(c[role].get(field+'_n',0)>0 for c in data['cells'] for role in ('pitcher','batter') for field in fields)
                for family,fields in {'pitch_physics':('speed','spin',*PHYSICS),'swing_mechanics':tuple(SWING),'contact_quality':('ev','launch_angle',*CONTACT)}.items()}
        for side in ('away', 'home'):
            scenario = review.get('bullpen', {}).get(side) or {}
            candidates = scenario.get('candidate_ids', [])
            allowed_ids = {p['player_id'] for p in context['teams'][side]['pitchers']}
            if len(candidates) != len(set(candidates)) or not set(candidates) <= allowed_ids:
                errors.append(f'invalid_bullpen_candidates:{side}')
            if scenario.get('appearance_assumed_certain') is not False:
                errors.append(f'bullpen_appearance_not_confirmed:{side}')
            for key in ('usage_and_role_reason','game_state_conditions','starter_exit_assumption','WIN_LOSS_effect','RUN_TOTAL_effect','uncertainty'):
                if not isinstance(scenario.get(key), str) or not scenario[key].strip():
                    errors.append(f'missing_bullpen_scenario:{side}:{key}')
            if not candidates and scenario.get('no_candidate_reason') not in ('source_unavailable','no_defensible_candidate'):
                errors.append(f'unexplained_empty_bullpen_candidates:{side}')
            if not candidates and scenario.get('no_candidate_reason') == 'source_unavailable' and any(p['projection'] is not None for p in context['teams'][side]['pitchers']):
                errors.append(f'bullpen_source_exists:{side}')
    for row in entries + conditional_entries:
        pid = row.get('id')
        acknowledged = row.get('examined_cell_ids', [])
        if set(acknowledged) != cells.get(pid, set()) or len(acknowledged) != len(set(acknowledged)):
            errors.append(f'unreviewed_or_unknown_cells:{pid}')
        for axis in ('WIN_LOSS', 'RUN_TOTAL'):
            value = row.get(axis) or {}
            if value.get('disposition') not in ('supports_away', 'supports_home', 'supports_upper', 'supports_lower', 'balanced', 'not_applicable', 'insufficient_evidence') or not isinstance(value.get('reason'), str) or not value['reason'].strip():
                errors.append(f'missing_axis_assessment:{pid}:{axis}')
            elif (axis == 'WIN_LOSS' and value['disposition'] in ('supports_upper', 'supports_lower')) or (axis == 'RUN_TOTAL' and value['disposition'] in ('supports_away', 'supports_home')):
                errors.append(f'wrong_axis:{pid}:{axis}')
        if index.get('full_evidence'):
            families=row.get('field_family_reviews') or {}
            for family in ('contact_quality','pitch_physics','swing_mechanics'):
                assessment=families.get(family) or {}
                fields=assessment.get('examined_fields',[])
                if (not isinstance(fields,list) or any(not isinstance(f,str) for f in fields)
                        or len(fields)!=len(set(fields)) or set(fields)!=set(required_review_fields(family))):
                    errors.append(f'unreviewed_or_unknown_fields:{pid}:{family}')
                if assessment.get('status') not in ('reviewed','no_observations','not_applicable') or not isinstance(assessment.get('reason'),str) or not assessment['reason'].strip():
                    errors.append(f'unreviewed_field_family:{pid}:{family}')
                elif assessment['status']=='no_observations' and observed_families.get(pid,{}).get(family):
                    errors.append(f'observed_field_family_claimed_missing:{pid}:{family}')
    for axis in ('WIN_LOSS', 'RUN_TOTAL'):
        final = (review.get('final') or {}).get(axis) or {}
        allowed = ('HOME', 'AWAY', '-') if axis == 'WIN_LOSS' else ('UPPER', 'LOWER', '-')
        if final.get('direction') not in allowed:
            errors.append(f'missing_or_invalid_final_direction:{axis}')
        for key in ('away_scoring_path', 'home_scoring_path', 'why_this_direction', 'strongest_counterargument'):
            if not isinstance(final.get(key), str) or not final[key].strip():
                errors.append(f'missing_final:{axis}:{key}')
        reviewed_ids = expected | {r.get('id') for r in conditional_entries}
        if not set(final.get('pair_refs', [])) or not set(final.get('pair_refs', [])) <= reviewed_ids:
            errors.append(f'missing_or_unknown_final_pair_refs:{axis}')
    if index.get('full_evidence'):
        context_ref=index.get('player_context')
        if not context_ref:
            errors.append('player_context_contract_missing')
        else:
            path=(Path(index_path).parent/context_ref['path']).resolve()
            assert path.is_relative_to(Path(index_path).parent.resolve()) and sha(path)==context_ref['sha256']
            doc=read(path)
            assert str(doc['gamePk'])==str(index['gamePk']) and doc['target_date']==index['target_date']
            expected_context={p['id']:p for p in doc['players']}
            if len(expected_context)!=len(doc['players']) or set(expected_context)!=required_context_ids:
                errors.append('player_context_manifest_coverage_mismatch')
            contexts=review.get('player_context_reviews',[])
            ids=[r.get('id') for r in contexts]
            if len(ids)!=len(set(ids)) or set(ids)!=set(expected_context):
                errors.append('missing_duplicate_or_extra_player_context_reviews')
            for row in contexts:
                pid=row.get('id');source=expected_context.get(pid)
                if source is None:continue
                if source['status']=='missing_source_section':errors.append('player_context_source_missing:'+pid)
                expected_status='reviewed' if source['status']=='observed' else source['status']
                if row.get('status')!=expected_status:errors.append('wrong_player_context_status:'+pid)
                positions=row.get('examined_rows',[])
                if (not isinstance(positions,list) or any(type(i) is not int for i in positions)
                        or len(positions)!=len(set(positions)) or set(positions)!=set(range(len(source['rows'])))):
                    errors.append('unreviewed_player_context_rows:'+pid)
                for axis in ('WIN_LOSS_reason','RUN_TOTAL_reason'):
                    if not isinstance(row.get(axis),str) or not row[axis].strip():errors.append('missing_player_context_reason:'+pid+':'+axis)
        packet=index.get('source_packet')
        if not packet:errors.append('complete_source_packet_missing')
        else:
            assert sha(packet['path'])==packet['sha256']
            source_index=read(packet['path'])
            assert source_index['target_date']==index['target_date'] and str(source_index['gamePk'])==str(index['gamePk'])
            from mlb_pregame_source_packet import check_sources
            source_result=check_sources(packet['path'],review.get('source_reviews') or {})
            errors.extend('source:'+e for e in source_result['errors'])
    return {'ok': not errors, 'errors': errors, 'review_completeness_only': True, 'semantic_correctness_proven': False}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--index', type=Path)
    ap.add_argument('--review', type=Path)
    ap.add_argument('--roster', type=Path)
    ap.add_argument('--ability-index', type=Path)
    ap.add_argument('--out', type=Path)
    ap.add_argument('--seal-prediction', type=Path)
    ap.add_argument('--receipt', type=Path)
    ap.add_argument('--bullpen', type=Path)
    ap.add_argument('--expand-pair')
    ap.add_argument('--source-packet',type=Path)
    a = ap.parse_args()
    if a.roster:
        assert a.ability_index and a.out and not (a.index or a.review or a.seal_prediction)
        from mlb_ability_inputs import attach
        roster = read(a.roster)
        ids = [p['id'] for t in roster['teams'].values() for p in t['players']]
        attached = attach(a.ability_index, roster['target_date'], ids, roster['first_pitch_utc'] or roster['scheduled_start_utc'])
        bullpen = read(a.bullpen) if a.bullpen else None
        packet={'path':str(a.source_packet.resolve()),'sha256':sha(a.source_packet)} if a.source_packet else None
        manifest = build_files(roster, attached['players'], a.out, bullpen,packet)
        print(json.dumps({k: manifest[k] for k in ('gamePk', 'pair_count', 'cell_count', 'review_status')}))
        raise SystemExit(0)
    if a.expand_pair:
        assert a.index and not (a.review or a.seal_prediction)
        print(expand_optional(a.index, a.expand_pair))
        raise SystemExit(0)
    assert a.index and a.review
    result = check_review(a.index, a.review)
    print(json.dumps(result, ensure_ascii=False))
    if a.seal_prediction:
        assert result['ok'], 'Cannot finalize while matchup reviews are pending'
        assert a.receipt
        index, prediction, review = read(a.index), read(a.seal_prediction), read(a.review)
        assert str(prediction['gamePk']) == str(index['gamePk'])
        assert prediction['target_us_date'] == index['target_date']
        assert prediction['win_loss']['side'] == review['final']['WIN_LOSS']['direction']
        assert prediction['run_total']['direction'] == review['final']['RUN_TOTAL']['direction']
        assert prediction['win_loss']['side'] in ('HOME', 'AWAY', '-') and prediction['run_total']['direction'] in ('UPPER', 'LOWER', '-')
        save(a.receipt, {'prediction_sha256': sha(a.seal_prediction), 'index_sha256': sha(a.index),
            'review_sha256': sha(a.review), 'structural_review_complete': True,
            'semantic_correctness_proven': False, 'notification_or_production_promotion': False})
    raise SystemExit(0 if result['ok'] else 2)
