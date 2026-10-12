# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Consume completed direct reviews without another LLM, promotion, or send."""
from datetime import date as calendar_date
import math
from pathlib import Path
import re

from mlb_ability_inputs import ROOT, read, save, sha
from mlb_matchup_review import check_review


def _source(index, name):
    ref=index['source_packet']
    assert sha(ref['path'])==ref['sha256'], 'Source packet changed'
    path=Path(ref['path']);packet=read(path)
    entry=next(e for e in packet['entries'] if e['id']==name)
    target=(path.parent/entry['path']).resolve()
    assert target.is_relative_to(path.parent.resolve()) and sha(target)==entry['sha256']
    return read(target)['data']


def verify_game(target_date, gp, entry, review_dir):
    index_ref=entry['matchup_review'];index_path=Path(index_ref['path'])
    assert sha(index_path)==index_ref['sha256'], 'Review input changed'
    index=read(index_path)
    assert str(index['gamePk'])==gp and index['target_date']==target_date
    assert index.get('full_evidence') and index['pair_count']==18, 'Full confirmed-lineup input required'
    assert index['source_packet']==entry['source_packet'], 'Mixed source/review versions'
    review_path=review_dir/gp/'REVIEW_COMPLETED.json'
    prediction_path=review_dir/gp/'PREDICTION.json'
    if not review_path.is_file():
        return {'gamePk':gp,'ok':False,'errors':['actual_review_missing']}
    validation=check_review(index_path,review_path)
    if not validation['ok']:
        return {'gamePk':gp,'ok':False,'errors':validation['errors']}
    if not prediction_path.is_file():
        return {'gamePk':gp,'ok':False,'errors':['reviewed_prediction_missing']}
    review,prediction=read(review_path),read(prediction_path)
    assert str(prediction['gamePk'])==gp and prediction['target_us_date']==target_date
    assert prediction.get('review_index_sha256')==sha(index_path), 'Prediction is not bound to this reviewed input'
    roster=_source(index,'roster');market=_source(index,'market')
    assert roster['target_date']==target_date and str(roster['gamePk'])==gp
    assert all(len(t['batting_order'])==9 for t in roster['teams'].values())
    assert prediction['away_team']==roster['teams']['away']['name'] and prediction['home_team']==roster['teams']['home']['name']
    roles=_source(index,'pbp_player_profiles').get('starting_pitcher_appearance_roles')
    assert isinstance(roles,dict) and set(roles)=={'away','home'}, 'Starter/relief history context missing'
    for side,role in roles.items():
        assert role['player_id']==str(roster['teams'][side]['probable_pitcher']['id']) and role['target_date']==target_date
        assert set(role['by_appearance_role'])=={'starter','reliever','unknown'}
    win_loss,run_total=prediction['win_loss'],prediction['run_total']
    assert win_loss['side']==review['final']['WIN_LOSS']['direction'] and win_loss['side'] in ('HOME','AWAY','-')
    assert run_total['direction']==review['final']['RUN_TOTAL']['direction'] and run_total['direction'] in ('UPPER','LOWER','-')
    if win_loss['side']!='-':
        assert win_loss['team']==roster['teams'][win_loss['side'].lower()]['name'], 'Prediction team disagrees with side'
    line=market['parsed']['run_total_baseline']
    assert type(line) in (int,float) and math.isfinite(line) and line>0
    assert type(run_total['line']) in (int,float) and math.isfinite(run_total['line']) and run_total['line']==line, 'run total baseline mismatch'
    return {'gamePk':gp,'ok':True,'errors':[],
        'inputs':{'index':index_ref,'review':{'path':str(review_path),'sha256':sha(review_path)},
                  'prediction':{'path':str(prediction_path),'sha256':sha(prediction_path)}},
        'analysis':{'away_team':prediction['away_team'],'home_team':prediction['home_team'],
                    'win_loss':win_loss,'run_total':run_total,'final_reasoning':review['final'],'starting_pitcher_appearance_roles':roles}}


def run(target_date, gamepks, input_index, review_dir, output_dir):
    assert calendar_date.fromisoformat(target_date).isoformat()==target_date
    ids=[g.strip() for g in gamepks.split(',')]
    assert ids and len(ids)==len(set(ids)) and all(re.fullmatch(r'[0-9]+',g) for g in ids)
    base=(ROOT/'postmortem/direct_review_runs').resolve()
    output_dir=Path(output_dir).resolve()
    assert output_dir.is_relative_to(base) and output_dir!=base, 'Use an isolated direct_review_runs subdirectory'
    assert not output_dir.exists(), 'Do not overwrite a previous run'
    input_index=Path(input_index);catalog=read(input_index)
    assert set(ids)<=set(catalog), 'Requested game not in input index'
    output_dir.mkdir(parents=True,exist_ok=False)
    rows=[]
    for gp in ids:
        try:
            row=verify_game(target_date,gp,catalog[gp],Path(review_dir))
        except (AssertionError,KeyError,TypeError,ValueError,OSError,StopIteration) as exc:
            row={'gamePk':gp,'ok':False,'errors':[type(exc).__name__+': '+str(exc)]}
        rows.append(row)
    ok=all(r['ok'] for r in rows)
    report={'schema':'mlb.direct_review_run.v1','target_date':target_date,'requested_gamepks':ids,
        'input_index':{'path':str(input_index.resolve()),'sha256':sha(input_index)},
        'ok':ok,'status':'validated_no_send' if ok else 'blocked_incomplete_or_invalid',
        'llm_calls':0,'production_promoted':False,'notification_sent':False,
        'semantic_correctness_proven':False,'blindness_verified':False,
        'collection_or_reasoning_performed':False,'games':rows}
    save(output_dir/'RUN_REPORT.json',report)
    if ok:
        save(output_dir/'VALIDATED_ANALYSIS.json',{'target_date':target_date,'gamepks':ids,
            'report_sha256':sha(output_dir/'RUN_REPORT.json'),'games':[{'gamePk':r['gamePk'],**r['analysis']} for r in rows],
            'research_only':True,'not_a_send_authorization':True})
    return report
