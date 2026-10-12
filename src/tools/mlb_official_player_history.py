# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Authoritative per-game counting stats, separate from physical pitch ownership."""
import argparse
from collections import Counter
from datetime import date,datetime,timezone
import json
import hashlib
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'_pydeps'),str(ROOT)]
from mlb_ability_inputs import read,save,sha

FIELDS={
 'hitting':('plateAppearances','atBats','hits','doubles','triples','homeRuns','baseOnBalls','intentionalWalks',
            'hitByPitch','strikeOuts','sacBunts','sacFlies','catchersInterference','runs','rbi','stolenBases','caughtStealing'),
 'pitching':('battersFaced','outs','hits','homeRuns','baseOnBalls','intentionalWalks','hitBatsmen','strikeOuts',
             'earnedRuns','runs','numberOfPitches','gamesStarted','inheritedRunners','inheritedRunnersScored','wildPitches','balks')}


def integer(v):
    if isinstance(v,bool):raise ValueError('Boolean count')
    f=float(v)
    if not f.is_integer() or f<0:raise ValueError('Invalid official count')
    return int(f)


def normalize(person,target,manifest):
    records={};excluded=Counter()
    for group in person.get('stats',[]):
        role=group['group']['displayName']
        if role not in FIELDS:continue
        assert group['type']['displayName']=='gameLog'
        for r in group.get('splits',[]):
            day=r['date'];gp=str(r['game']['gamePk'])
            if day>=target or r.get('gameType')!='R' or r.get('season')!=target[:4]:
                excluded['target_future_or_nonregular']+=1;continue
            assert gp in manifest and manifest[gp]['date']==day, (gp,day,'outside verified history')
            assert int((r.get('player') or {}).get('id',person['id']))==person['id']
            assert r['team']['id'] in (manifest[gp]['home_id'],manifest[gp]['away_id'])
            values={k:integer(r['stat'][k]) for k in FIELDS[role] if k in r['stat']}
            required=('plateAppearances','atBats','hits','baseOnBalls','strikeOuts') if role=='hitting' else ('battersFaced','outs','hits','baseOnBalls','strikeOuts')
            assert all(k in values for k in required),(person['id'],gp,role,'missing required counting stats')
            key=(gp,role)
            item={'gamePk':gp,'date':day,'role':role,'team_id':str(r['team']['id']),'counts':values}
            if key in records:
                assert records[key]==item,'Conflicting duplicate log'
                excluded['identical_duplicate']+=1
            else:records[key]=item
    return sorted(records.values(),key=lambda r:(r['date'],r['gamePk'],r['role'])),dict(excluded)


def profile(pid,name,records,target):
    roles={}
    for role in FIELDS:
        windows={}
        for window,days in [('season',None),('30d',30),('14d',14)]:
            selected=[r for r in records if r['role']==role and 0<(date.fromisoformat(target)-date.fromisoformat(r['date'])).days and (days is None or (date.fromisoformat(target)-date.fromisoformat(r['date'])).days<=days)]
            total=Counter();seen=Counter()
            for r in selected:total.update(r['counts']);seen.update(r['counts'].keys())
            values={k:total[k] if seen[k]==len(selected) and selected else None for k in FIELDS[role]}
            windows[window]={'status':'observed' if selected else 'no_game_logs_in_source','games':len(selected),'counts':values,'field_observation_games':dict(seen)}
        roles[role]=windows
    return {'schema':'mlb.official_counting_history.v1','player_id':pid,'name':name,'target_date':target,
        'source_max_date':max((r['date'] for r in records),default=None),'roles':roles,'games':records,
        'definitions':['Official PA/result attribution; do not attach it to the last physical pitch owner.',
            'Counts only. Provider season-rate/ERA/AVG fields deliberately not consumed.',
            'No logs means no observations, not an automatic zero-performance prior.',
            'Later-retrieved historical official data, not a vintage pregame snapshot.']}


def workload(value,target):
    assert value['target_date']==target
    records=[r for r in value['games'] if r['role']=='pitching']
    assert all(r['date']<target for r in records)
    ages=[((date.fromisoformat(target)-date.fromisoformat(r['date'])).days,r) for r in records]
    last=max((r['date'] for r in records),default=None)
    gap=(date.fromisoformat(target)-date.fromisoformat(last)).days if last else None
    result={'last_app_date':last,'days_rest':gap,'full_rest_days':gap-1 if gap is not None else None,
        'workload_scope':'all official pitching appearances, including starts',
        'appearance_confirmed':False,'availability':'not_confirmed',
        'workload_status':'observed' if records else 'no_game_logs_in_source'}
    for days in (1,2,3,7):
        selected=[r for age,r in ages if age<=days]
        counts=[r['counts'].get('numberOfPitches') for r in selected]
        result[f'apps_{days}d']=len(selected) if records else None
        result[f'pitches_{days}d']=sum(counts) if records and all(v is not None for v in counts) else None
    result['recent_appearances']=[r for age,r in ages if age<=7]
    return result


def appearance_role_context(value,target):
    assert value['target_date']==target
    records=sorted((r for r in value['games'] if r['role']=='pitching'),key=lambda r:(r['date'],r['gamePk']))
    assert all(r['date']<target for r in records)
    grouped={'starter':[],'reliever':[],'unknown':[]}
    for row in records:
        start=row['counts'].get('gamesStarted')
        assert start in (0,1,None)
        grouped['starter' if start==1 else 'reliever' if start==0 else 'unknown'].append(row)
    return {'player_id':value['player_id'],'target_date':target,
        'by_appearance_role':{role:profile(value['player_id'],value['name'],rows,target)['roles']['pitching'] for role,rows in grouped.items()},
        'recent_outings':records[-5:],'expected_target_innings':None,
        'meaning':'Historical starter/relief exposure, not a target-game innings projection. Batter prior PA count is not this pitcher times-through-order.'}


def capture(request_path,manifest_path,out):
    import requests
    req=read(request_path);manifest=read(manifest_path);target=req['target_date'];people=req['players']
    out.mkdir(parents=True,exist_ok=True);ids=sorted(people);batches=[]
    with requests.Session() as session:
        for start in range(0,len(ids),12):
            wanted=ids[start:start+12];path=out/'batches'/f'{start//12:03}.json'
            if path.exists():batch=read(path);assert batch['player_ids']==wanted and batch['target_date']==target
            else:
                r=session.get('https://statsapi.mlb.com/api/v1/people',params={'personIds':','.join(wanted),
                    'hydrate':f'stats(group=[hitting,pitching],type=[gameLog],season={target[:4]},gameType=R)'},timeout=40)
                r.raise_for_status();data=r.json()['people'];assert {str(p['id']) for p in data}==set(wanted)
                logs={};exclusions={}
                for p in data:logs[str(p['id'])],exclusions[str(p['id'])]=normalize(p,target,manifest)
                batch={'player_ids':wanted,'target_date':target,'url':r.url,'retrieved_utc':datetime.now(timezone.utc).isoformat(),
                    'raw_response_sha256':hashlib.sha256(r.content).hexdigest(),
                    'future_rows_not_saved':True,'logs':logs,'exclusions':exclusions}
                save(path,batch)
            batches.append({'path':str(path.resolve()),'sha256':sha(path)})
            for pid in wanted:save(out/'players'/(pid+'.json'),profile(pid,people[pid],batch['logs'][pid],target))
            print(f'Official history checkpoint {min(start+12,len(ids))}/{len(ids)}',flush=True)
    player_refs={pid:{'path':str((out/'players'/(pid+'.json')).resolve()),'sha256':sha(out/'players'/(pid+'.json'))} for pid in ids}
    index={'schema':'mlb.official_counting_history_index.v1','target_date':target,'history_manifest_sha256':sha(manifest_path),
        'players':player_refs,'batches':batches,'later_retrieval_not_vintage':True,'production_changed':False}
    save(out/'INDEX.json',index)
    print({'players':len(ids),'batches':len(batches)},flush=True)


def verified_index(path,target):
    index=read(path)
    assert index.get('verified_counting_history') and index['target_date']==target
    proof=index['validation'];assert sha(proof['path'])==proof['sha256']
    report=read(proof['path']);assert report['ok'] and not report['mismatches']
    raw=index['raw_index'];assert sha(raw['path'])==raw['sha256']==report['index_sha256']
    original=read(raw['path'])
    assert index['players']==original['players'] and original['target_date']==target
    assert len(index['players'])==report['players']
    return index


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--request',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();capture(a.request,a.manifest,a.out)
