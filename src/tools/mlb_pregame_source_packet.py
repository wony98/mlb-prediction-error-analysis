# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Connect known pregame source families without hiding missing or late data."""
import argparse
import copy
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from mlb_ability_inputs import read, save, sha, ROOT

SOURCE_IDS = ('roster','environment','market','forecast','direct_bvp','bullpen',
    'pbp_player_profiles','statcast_full_detail','trajectory','umpire_prior','park_prior',
    'minor_prior','transactions','health_news','advanced_leaderboards','model_criteria')


def reference(path):
    return {'path':str(Path(path).resolve()),'sha256':sha(path)}


def norm(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',value).lower() if c.isalnum())


def rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))


def distinct_observations(records):
    grouped={}
    for row in records:
        key=json.dumps(row,sort_keys=True,ensure_ascii=False)
        if key not in grouped:grouped[key]={'record':row,'source_occurrences':0}
        grouped[key]['source_occurrences']+=1
    return {'rows':[v['record'] for v in grouped.values()],
        'duplicate_rows_removed':len(records)-len(grouped),
        'duplicate_groups':[v for v in grouped.values() if v['source_occurrences']>1]}


def official_bullpen_usage(bullpen,official_refs,target):
    from mlb_official_player_history import workload
    value=copy.deepcopy(bullpen)
    assert value['target_date']==target
    for players in value['teams'].values():
        for player in players:
            pid=str(player['player_id']);ref=official_refs[pid]
            assert sha(ref['path'])==ref['sha256']
            profile=read(ref['path']);assert profile['player_id']==pid
            obsolete=('last_app_date','days_rest','pitches_1d','pitches_2d','pitches_3d','apps_3d','apps_7d',
                'b2b','three_in_3','long_relief_3d','availability','availability_penalty','recent_usage_score')
            player['legacy_relief_only_usage_not_active']={k:player.pop(k) for k in obsolete if k in player}
            player.update(workload(profile,target))
            player['official_workload_source']=ref
            player['role_history_scope']='legacy relief-only role estimate; not appearance or availability proof'
    value['official_workload_connected']=True
    return value


def assemble(frozen_path, ability_index, out, static_root=None, transaction_path=None, official_index=None, historical_news_index=None):
    frozen=read(frozen_path);target=frozen['target_us_date'];gp=str(frozen['gamePk'])
    for value in frozen['files'].values():assert sha(value['path'])==value['sha256']
    source_files=frozen['files'];roster=read(source_files['roster']['path'])
    assert roster['target_date']==target and str(roster['gamePk'])==gp
    start=datetime.fromisoformat((roster['first_pitch_utc'] or roster['scheduled_start_utc']).replace('Z','+00:00'))
    people={p['id']:p for t in roster['teams'].values() for p in t['players']}
    entries=[]
    def add(name,status,reason,data=None,refs=None):
        path=out/(name+'.json')
        save(path,{'source_id':name,'gamePk':gp,'target_date':target,'status':status,'reason':reason,
            'data':data,'source_refs':refs or []})
        entries.append({'id':name,'status':status,'path':path.name,'sha256':sha(path)})
    for name,key in [('roster','roster'),('environment','environment'),('market','market'),('forecast','forecast'),('direct_bvp','bvp')]:
        item=source_files[key]
        add(name,'available','Frozen original pregame input; original source limitations remain.',read(item['path']),[item])
    profiles=[v for k,v in source_files.items() if k.startswith('player:')]
    assert len(profiles)==len(people)
    official_refs={};official_status='missing'
    if official_index:
        from mlb_official_player_history import verified_index
        official=verified_index(official_index,target);assert set(people)<=set(official['players'])
        for pid in people:
            ref=official['players'][pid];assert sha(ref['path'])==ref['sha256']
            value=read(ref['path']);assert value['player_id']==pid and (value['source_max_date'] is None or value['source_max_date']<target)
            official_refs[pid]=ref
        official_status='available'
    bullpen=read(source_files['bullpen']['path'])
    if official_refs:bullpen=official_bullpen_usage(bullpen,official_refs,target)
    add('bullpen',official_status,'Official all-appearance workload. Legacy relief-only role estimates remain separately labelled; no automatic available/unavailable judgment.',bullpen,
        [source_files['bullpen']]+list(official_refs.values()))
    fielding_running={}
    for item in profiles:
        p=read(item['path'])
        fielding_running[p['player_id']]={k:p.get(k) for k in ('fielding_by_position','runner_windows','defensive_start_coordinates')}
    starter_roles={}
    if official_refs:
        from mlb_official_player_history import appearance_role_context
        for side,team in roster['teams'].items():
            pid=str(team['probable_pitcher']['id'])
            starter_roles[side]=appearance_role_context(read(official_refs[pid]['path']),target)
    add('pbp_player_profiles',official_status,
        'Active official PA/counting totals come from official game logs, not excluded or last-pitch-owned legacy PA cells. Physical pitches are in statcast_full_detail. Legacy PBP supplies fielding/running only.',
        {'official_counting_profiles':official_refs,'fielding_running':fielding_running,
         'starting_pitcher_appearance_roles':starter_roles,
         'legacy_pa_pitch_cells_not_active':True,'all_raw_pbp_fields_interpreted':False},
        profiles+list(official_refs.values())+([reference(official_index)] if official_index else []))
    from mlb_ability_inputs import attach
    attached=attach(ability_index,target,list(people),start.isoformat(),history_through=frozen['history_through'])
    add('statcast_full_detail','available' if attached.get('full_evidence') else 'incomplete',
        'Full mechanics/contact/context profiles required; shared events are not independent duplicate support.',attached,[attached['index']])
    trajectory=ROOT/f'_narr_trajectory_ledger_{target}.json'
    if trajectory.exists():
        raw=read(trajectory);assert raw['as_of']==target
        normalized={}
        for pid,p in people.items():normalized.setdefault(norm(p['name']),[]).append(pid)
        selected=[];unmatched=[]
        for role in ('batters','relievers','starters'):
            for name,value in raw.get(role,{}).items():
                ids=normalized.get(norm(name),[])
                if len(ids)!=1:continue
                if not value.get('last_date') or value['last_date']>=target:
                    unmatched.append({'name':name,'role':role,'reason':'missing_or_invalid_last_date'});continue
                selected.append({'player_id':ids[0],'source_name':name,'role':role,
                    'values':{k:v for k,v in value.items() if k not in ('label','emoji')}})
        add('trajectory','available','Dated numeric trajectories; automatic labels/emoji omitted, not new independent events.',
            {'rows':selected,'unverified_rows':unmatched,'missing_roster_ids':sorted(set(people)-{r['player_id'] for r in selected})},[reference(trajectory)])
    else:add('trajectory','missing','No exact-date snapshot. Never use the current rolling file as a historical substitute.')
    ump=ROOT/'mlb_ump_hist.csv';env=read(source_files['environment']['path'])
    ids={str(o['official']['id']) for o in env.get('officials',[]) if o['officialType']=='Home Plate'}
    if ump.exists():
        matched=[r for r in rows(ump) if str(int(float(r['ump_id']))) in ids]
        add('umpire_prior','available','2021-25 assignment-outcome averages, not causal zone effects. Missing row is not zero influence.',matched,[reference(ump)])
    else:add('umpire_prior','missing','Stored historical umpire source is absent.')
    park=Path(static_root)/'park_factors_2025.csv' if static_root else None
    if park and park.exists():
        matched=[r for r in rows(park) if norm(r['Venue'])==norm(env['venue']['name'])]
        assert all(r['Year']=='2025' for r in matched)
        add('park_prior','available' if matched else 'incomplete','Prior completed season; exact venue match only; no team-name-only relocation fallback.',matched,[reference(park)])
    else:add('park_prior','missing','Prior-season park source not supplied.')
    minor=[];minor_refs=[];rejected=[]
    if static_root:
        for name in ('minor_batters.csv','minor_pitchers.csv'):
            path=Path(static_root)/name
            if not path.exists():continue
            minor_refs.append(reference(path))
            for r in rows(path):
                if str(r.get('선수ID')) not in people:continue
                years=re.findall(r'20\d{2}',r.get('출처',''))
                if years and max(years)<target[:4]:minor.append(r)
                else:rejected.append({'player_id':r.get('선수ID'),'reason':'unverified_asof_in_source_description'})
    add('minor_prior','available' if minor_refs else 'missing','Only explicitly completed prior seasons. Identical repeated rows are one observation, not additional PA. Minor and MLB performance are not directly equivalent.',{**distinct_observations(minor),'rejected':rejected},minor_refs)
    if transaction_path and Path(transaction_path).exists():
        tx=read(transaction_path);assert tx['through']<target
        teams={str(t['id']) for t in roster['teams'].values()}
        selected=[]
        for r in tx['transactions']:
            if r.get('date','')>=target or r.get('effectiveDate','')>=target:continue
            if str((r.get('person') or {}).get('id')) in people or {str((r.get(k) or {}).get('id')) for k in ('fromTeam','toTeam')} & teams:
                selected.append({k:v for k,v in r.items() if k!='resolutionDate'})
        add('transactions','available','Historical announced/effective dates only. Not proof of current IL state or medical condition; later retrieval is not vintage.',selected,[reference(transaction_path)])
    else:add('transactions','missing','No dated transaction snapshot supplied; current roster alone does not explain injury/return history.')
    news=ROOT/f'_narr_news_{target}.json'
    value=read(news).get(gp) if news.exists() else None
    if historical_news_index:
        from mlb_news_recovery_audit import validated_historical_news
        recovered,refs=validated_historical_news(historical_news_index,roster)
        add('health_news','available','Reviewed historical web recovery using publisher publication/modification times, not retrieval time. Coverage is the declared sources, not all public news.',recovered,[reference(historical_news_index)]+refs)
    elif value:
        stamp=datetime.fromisoformat(value['collected_at'].replace('Z','+00:00'))
        if stamp.tzinfo is not None and stamp<start and value.get('leak_free'):
            add('health_news','available','Pregame frozen news; source URLs and article claims still need reading.',value,[reference(news)])
        else:add('health_news','missing','Existing news did not pass collection-time cutoff; do not substitute postgame news.')
    else:add('health_news','missing','No pregame frozen news for this game. This does NOT mean no injury/scratch/manager information existed.')
    context=read(attached['source_context']['path'])
    tables=context.get('tables',[])
    if tables:
        add('advanced_leaderboards','available','Source context and per-player advanced_tables are linked; official qualification, sample, role and date limitations must be read.',
            {'table_sources':tables,'player_profiles':attached['players']},[attached['source_context']])
    else:add('advanced_leaderboards','excluded_temporal','This historical input did not contain timely leaderboard snapshots. Do not use the later 2026-09-11 tables.',{'later_capture_not_eligible':True})
    add('model_criteria','excluded_owner_scope','Current owner-approved direct experiment excludes model predictions and criterion-bank decisions; do not silently reintroduce them.')
    assert {e['id'] for e in entries}==set(SOURCE_IDS)
    manifest={'schema':'mlb.pregame_sources.v1','gamePk':gp,'target_date':target,'entries':entries,
        'source_ids':list(SOURCE_IDS),'unlisted_required_sources':[],
        'data_complete':not any(e['status'] in ('missing','incomplete') for e in entries),
        'not_a_review_receipt':True,'all_public_baseball_information_claimed':False}
    save(out/'INDEX.json',manifest)
    save(out/'REVIEW_PENDING.json',{'index_sha256':sha(out/'INDEX.json'),
        'sources':[{'id':e['id'],'status':None,'WIN_LOSS_reason':None,'RUN_TOTAL_reason':None} for e in entries]})
    print({'gamePk':gp,'families':len(entries),'missing':[e['id'] for e in entries if e['status'] in ('missing','incomplete')]})
    return manifest


def check_sources(index_path, review):
    index=read(index_path);errors=[]
    entry_ids=[e['id'] for e in index['entries']]
    if len(entry_ids)!=len(set(entry_ids)) or set(entry_ids)!=set(SOURCE_IDS):errors.append('source_manifest_coverage_mismatch')
    complete=not any(e['status'] in ('missing','incomplete') for e in index['entries'])
    if index['data_complete']!=complete:errors.append('source_completeness_flag_mismatch')
    if review.get('index_sha256')!=sha(index_path):errors.append('source_index_hash_mismatch')
    rows=review.get('sources',[]);ids=[r.get('id') for r in rows]
    if len(ids)!=len(set(ids)) or set(ids)!=set(SOURCE_IDS):errors.append('missing_source_reviews')
    by_id={r.get('id'):r for r in rows}
    for entry in index['entries']:
        path=(Path(index_path).parent/entry['path']).resolve()
        assert path.is_relative_to(Path(index_path).parent.resolve()) and sha(path)==entry['sha256']
        source=read(path)
        if source['status']!=entry['status'] or source['source_id']!=entry['id']:errors.append('source_entry_mismatch:'+entry['id'])
        for ref in source.get('source_refs',[]):
            if sha(ref['path'])!=ref['sha256']:errors.append('source_file_changed:'+entry['id'])
        row=by_id.get(entry['id'],{})
        expected='reviewed' if entry['status']=='available' else ('excluded' if entry['status'].startswith('excluded') else 'unavailable')
        if row.get('status')!=expected:errors.append('incorrect_source_disposition:'+entry['id'])
        for axis in ('WIN_LOSS_reason','RUN_TOTAL_reason'):
            if not isinstance(row.get(axis),str) or not row[axis].strip():errors.append('missing_source_reason:'+entry['id']+':'+axis)
    if not complete:errors.append('pregame_sources_incomplete')
    return {'ok':not errors,'errors':errors,'data_complete':complete,'semantic_correctness_proven':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--frozen',type=Path,required=True);p.add_argument('--ability-index',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--static-root',type=Path);p.add_argument('--transactions',type=Path);p.add_argument('--official-index',type=Path)
    p.add_argument('--historical-news-index',type=Path)
    a=p.parse_args();assemble(a.frozen,a.ability_index,a.out,a.static_root,a.transactions,a.official_index,a.historical_news_index)
