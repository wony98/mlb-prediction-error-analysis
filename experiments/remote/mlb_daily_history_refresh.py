# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Data-only day increment on one Colab CPU; pending lineups do not block history."""
from datetime import datetime, timedelta, timezone
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1] if '__file__' in globals() else Path('/content')
OWNER = '019ff651-3222-7630-afad-c5b8d5fad622'
BASE_FILES = ['mlb_2026_sc_recent.parquet', '_narr2026.parquet', '_box2026_game.parquet', '_box2026_bat.parquet']
DERIVED_FILES = ['_narr_bullpen_stints.parquet', '_hitter_gamelog_2026.parquet', '_batter_underlying_gamelog.parquet', '_sp_underlying_gamelog.parquet']
EXTRA_FILES = {'_mlb_id_name_2026.csv': '_mlb_id_name_2026.csv',
               'bvp_pair_daily.csv': 'postmortem/fullread_v4/bvp_matchup/mlb_bvp_pair_daily.csv',
               'bullpen_stints.csv': 'postmortem/fullread_v4/mlb_fullread_v4_bullpen_stints.csv'}
CODE_FILES = ['_mlb_recent_sc.py','_narr_data_update.py','_narr_pbp.py','_narr_bullpen_ledger.py',
              '_mlb_player_detail_stats.py','_mlb_raw_pbp_event_ledger_build.py','_mlb_bvp_matchup_ledger.py',
              '_mlb_fullread_v4_bullpen_ledger.py','_mlb_raw_pbp_refresh.py','_mlb_fullread_v4_bullpen_appearance_projection.py',
              '_narr_hitter_traj.py','_narr_underlying_lead.py','_narr_sp_underlying.py','_narr_trajectory_ledger.py']


def read(p):
    return json.loads(Path(p).read_text(encoding='utf-8'))


def write(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(p)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def remote():
    import os
    base = Path('/content/mlb_daily_history')
    out = base / 'outputs'
    out.mkdir(parents=True, exist_ok=True)
    report = {'errors': [], 'mode': 'history_only_with_provisional_rosters', 'llm_invoked': False, 'predictions_changed': False, 'notification_sent': False,
              'all_inputs_complete': False, 'game_inputs_frozen': False}
    try:
        assert sha('/content/daily_package.zip') == read('/content/daily_job.json')['package_sha256']
        with zipfile.ZipFile('/content/daily_package.zip') as z:
            for name in z.namelist():
                assert not Path(name).is_absolute() and '..' not in Path(name).parts
            z.extractall(base)
        os.chdir(base)
        sys.path.insert(0, str(base))
        import pandas as pd
        import source_helpers as q
        import _mlb_bvp_matchup_ledger as bvp
        import _mlb_fullread_v4_bullpen_ledger as bullpen
        from _mlb_raw_pbp_event_ledger_build import build_game
        from _mlb_player_detail_stats import DetailStats
        request = read(base / 'request.json')
        target, through = request['target_date'], request['history_through']
        cutoff = datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        q.BASE, q.OUT, q.REQUEST = base, out, {'narr_start': through}
        q.REPORT = {'target_date': target, 'history_through': through, 'cutoff_utc': cutoff, 'errors': []}
        report.update(target_date=target, history_through=through, cutoff_utc=cutoff, package_sha256=sha('/content/daily_package.zip'))
        assert sha(base / 'baseline/ledger.jsonl') == request['ledger_sha256']
        for name in BASE_FILES:
            assert sha(base / 'baseline' / name) == request['baseline_hashes'][name]
        schedule = q.schedule(through, through)
        games = {gp:g for gp,g in schedule.items() if q.is_completed(g) and g['date'] < target}
        assert games, 'No completed source games'
        history = read(base / 'history_manifest.json')
        history.update(games)
        q.save(out / 'HISTORY_MANIFEST.json', history)
        rows = []
        for gp, game in games.items():
            rows.append(q.fetch_history(game))
            print(f'New history persisted {len(rows)}/{len(games)}: {gp}', flush=True)
        q.save(out / 'FETCHED_HISTORY.json', rows)
        q.update_narr(rows)
        q.update_sc({'statcast_start': through})
        assert not q.REPORT['errors'], q.REPORT['errors']
        report['base_tables'] = q.REPORT['narrative_box']
        report['statcast'] = q.REPORT['statcast']
        for name in BASE_FILES:
            frame = pd.read_parquet(out / 'updated' / name)
            gp_col, day_col = ('game_pk','game_date') if name.startswith('mlb_') else ('gamePk','date')
            assert set(frame[gp_col].astype(int).astype(str)) == set(history), 'Game coverage: ' + name
            assert frame[day_col].astype(str).str[:10].max() < target
            if name in ('_narr2026.parquet','_box2026_game.parquet'):
                assert not frame.duplicated('gamePk').any()
            elif name == '_box2026_bat.parquet':
                assert not frame.duplicated(['gamePk','side','batter_id']).any()
            shutil.copyfile(out / 'updated' / name, base / name)
        names = read(base / 'player_pool_seed.json')
        current = []
        for gp,g in q.schedule(target,target).items():
            url = f'https://statsapi.mlb.com/api/v1.1/game/{gp}/feed/live?fields=gamePk,gameData,datetime,officialDate,dateTime,status,abstractGameState,detailedState,probablePitchers,players,id,fullName,batSide,pitchHand,code,liveData,boxscore,teams,home,away,team,name,battingOrder,person,position,abbreviation'
            live = q.get(url)
            assert str(live['gamePk']) == gp
            item = {'gamePk':gp,'target_date':target,'scheduled_utc':g['scheduled_utc'],'captured_at_utc':datetime.now(timezone.utc).isoformat(),
                    'status':live.get('gameData',{}).get('status'), 'teams':{}, 'not_a_final_analysis_snapshot':True}
            for side in ('home','away'):
                team = live.get('liveData',{}).get('boxscore',{}).get('teams',{}).get(side,{})
                people = []
                for p in team.get('players',{}).values():
                    person = p.get('person') or {}
                    if person.get('id'):
                        pid = str(person['id'])
                        names[pid] = person['fullName']
                        people.append({'id':pid,'name':person['fullName'],'position':p.get('position'),'batting_order':p.get('battingOrder')})
                item['teams'][side] = {'name':g[side],'id':g[side+'_id'],'players':people,
                      'batting_order':[str(i) for i in team.get('battingOrder',[])],
                      'probable_pitcher':live.get('gameData',{}).get('probablePitchers',{}).get(side)}
            current.append(item)
        write(out / 'PROVISIONAL_ROSTERS.json', current)
        write(out / 'PLAYER_POOL_NAMES.json', names)
        name_frame = pd.read_csv(base / '_mlb_id_name_2026.csv')
        nm = {str(int(r.id)):r.name for r in name_frame.itertuples()}
        nm.update(names)
        pd.DataFrame([{'id':int(pid),'name':name} for pid,name in nm.items()]).to_csv(base / '_mlb_id_name_2026.csv', index=False)
        source_games, stints = [], []
        for g in rows:
            gp, day = g['gamePk'],g['date']
            path = out / 'raw_pbp' / day / (gp+'.json')
            record = build_game(path, history, sha(path))
            q.save(out / 'event_games' / day / (gp+'.json'), record)
            source_games.append(bvp.SourceGame(day,gp,path,'daily_official_pbp',target[:4]))
            values,error = bullpen.parse_game_stints({**g,'season':int(target[:4])},read(path))
            assert error is None, error
            stints.extend(values)
        daily,*_ = bvp.build_ledger(source_games,target)
        old = pd.read_csv(base / 'bvp_pair_daily.csv')
        assert old.date.max() < through, 'BvP same-day rerun needs per-game provenance; refusing blind append'
        bvp_frame = pd.concat([old,pd.DataFrame(daily)],ignore_index=True)
        bvp_frame.to_csv(out / 'updated/mlb_bvp_pair_daily.csv',index=False)
        old_stints = pd.read_csv(base / 'bullpen_stints.csv',dtype={'gamePk':str})
        all_stints = pd.concat([old_stints[~old_stints.gamePk.isin(games)],pd.DataFrame(stints)],ignore_index=True)
        all_stints.to_csv(out / 'updated/mlb_fullread_v4_bullpen_stints.csv',index=False)
        for script in ('_narr_bullpen_ledger.py','_narr_hitter_traj.py','_narr_underlying_lead.py','_narr_sp_underlying.py','_narr_trajectory_ledger.py'):
            cmd = [sys.executable,script] + (['--asof',target] if script=='_narr_trajectory_ledger.py' else [])
            run = subprocess.run(cmd,capture_output=True,text=True,timeout=300)
            (out/(script+'.log')).write_text(run.stdout+'\n'+run.stderr,encoding='utf-8')
            assert run.returncode == 0, script + ': ' + run.stderr[-500:]
            print('Derived current:',script,flush=True)
        for name in [*DERIVED_FILES,'_mlb_id_name_2026.csv',f'_narr_trajectory_ledger_{target}.json',f'_narr_trajectory_ledger_{target}.md']:
            shutil.copyfile(base/name,out/'updated'/name)
        stats = DetailStats(target,cutoff,names)
        seen = set()
        with (base/'baseline/ledger.jsonl').open(encoding='utf-8-sig') as handle:
            for line in handle:
                record = json.loads(line)
                gp = str(record['identity']['gamePk'])
                if gp in games:
                    continue
                assert gp in history and gp not in seen and record['identity']['date'] == history[gp]['date']
                stats.add(record)
                seen.add(gp)
                if len(seen)%400==0:
                    print(f'Detail history read {len(seen)}/{len(history)}',flush=True)
        for g in rows:
            stats.add(read(out/'event_games'/g['date']/(g['gamePk']+'.json')))
            seen.add(g['gamePk'])
        assert seen == set(history)
        (out/'player_pool').mkdir(exist_ok=True)
        for pid,name in sorted(names.items()):
            value = stats.player(pid)
            value.update(name=name,target_date=target,history_through=through)
            q.save(out/'player_pool'/(pid+'.json'),value)
        q.save(out/'player_pool/sources.json',stats.sources)
        report.update(new_games=len(rows),history_games=len(history),players=len(names),
                      source_max_date=max(g['date'] for g in history.values()),detail_coverage=dict(stats.coverage),
                      provisional_games=len(current),lineup_ready_games=[x['gamePk'] for x in current if all(len(t['batting_order'])==9 for t in x['teams'].values())],
                      bvp_daily_rows=len(bvp_frame),bullpen_stints=len(all_stints))
    except Exception as e:
        import traceback
        traceback.print_exc()
        report['errors'].append(type(e).__name__+': '+str(e))
    finally:
        report['limitations'] = ['No LLM recap/criteria generation.', 'Provisional rosters must be refreshed/frozen after lineups are confirmed.',
          'Existing mid-PA attribution exclusions, legacy EV definitions and availability heuristics remain; source refresh is not perfect metric completeness.']
        write(out/'REPORT.json',report)
        write(out/'FILES.json',{p.relative_to(out).as_posix():{'sha256':sha(p),'bytes':p.stat().st_size} for p in out.rglob('*') if p.is_file() and p.name!='FILES.json'})
        with zipfile.ZipFile('/content/daily_results.zip','w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as z:
            for path in out.rglob('*'):
                if path.is_file():z.write(path,path.relative_to(out))
        print(json.dumps(report),flush=True)


def local(args):
    sys.path[:0] = [str(ROOT),str(ROOT/'_pydeps')]
    from _colab_drive_pipeline import run_colab, win_path_to_wsl
    target = args.date
    datetime.strptime(target,'%Y-%m-%d')
    previous = ROOT/'postmortem/pregame_refresh'/args.previous
    out = ROOT/'postmortem/pregame_refresh'/target
    out.mkdir(parents=True,exist_ok=True)
    assert not(out/'RUN.json').exists(), 'Existing run; use recovery rather than a second worker'
    through = (datetime.fromisoformat(target)-timedelta(days=1)).date().isoformat()
    old_receipt = read(previous/'EVENT_LEDGER_PROMOTION.json')
    assert old_receipt['history_max_date'] == (datetime.fromisoformat(through)-timedelta(days=1)).date().isoformat(), 'Daily mode requires a contiguous one-day increment'
    ledger = Path(old_receipt['path'])
    history_source = previous/'attempt_03/results/HISTORY_MANIFEST.json'
    if not history_source.exists():history_source = previous/'results/HISTORY_MANIFEST.json'
    player_source = previous/'attempt_03/results/ROSTERS.json'
    if player_source.exists():
        pool = {p['id']:p['name'] for r in read(player_source).values() for t in r['teams'].values() for p in t['players']}
    else:pool = read(previous/'results/PLAYER_POOL_NAMES.json')
    if (previous/'PLAYER_POOL_INDEX.json').exists():
        pool.update({pid:item['name'] for pid,item in read(previous/'PLAYER_POOL_INDEX.json')['players'].items()})
    promotions = {name:name for name in BASE_FILES+DERIVED_FILES+['_mlb_id_name_2026.csv']}
    promotions.update({'mlb_bvp_pair_daily.csv':EXTRA_FILES['bvp_pair_daily.csv'],'mlb_fullread_v4_bullpen_stints.csv':EXTRA_FILES['bullpen_stints.csv']})
    for suffix in ('json','md'):promotions[f'_narr_trajectory_ledger_{target}.{suffix}']=f'_narr_trajectory_ledger_{target}.{suffix}'
    request = {'target_date':target,'history_through':through,'ledger_sha256':old_receipt['sha256'], 'promotion_targets':promotions,
               'baseline_hashes':{relative:sha(ROOT/relative) if (ROOT/relative).exists() else None for relative in promotions.values()}}
    package = out/'package.zip'
    with zipfile.ZipFile(package,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as z:
        for name in CODE_FILES:z.write(ROOT/name,name)
        z.write(ROOT/'tools/mlb_pregame_refresh_remote.py','source_helpers.py')
        for name in BASE_FILES:z.write(ROOT/name,'baseline/'+name)
        for name,relative in EXTRA_FILES.items():z.write(ROOT/relative,name)
        z.write(history_source,'history_manifest.json')
        z.writestr('player_pool_seed.json',json.dumps(pool,ensure_ascii=False))
        z.writestr('request.json',json.dumps(request))
        h = hashlib.sha256()
        with ledger.open('rb') as f,z.open('baseline/ledger.jsonl','w',force_zip64=True) as dest:
            for block in iter(lambda:f.read(1048576),b''):
                h.update(block);dest.write(block)
        assert h.hexdigest()==request['ledger_sha256'], 'Canonical ledger changed since last receipt'
    write(out/'REQUEST.json',request)
    session = 'daily-history-'+target.replace('-','')
    job = {'package_sha256':sha(package)}
    write(out/'JOB.json',job)
    run = {'session':session,'thread_id':OWNER,'cycle_id':session,'keep':False,'state':'starting','stop_method':'colab stop -s '+session}
    write(out/'RUN.json',run)
    started = dispatched = complete = False
    try:
        run_colab(['new','-s',session],timeout=180);started=True
        transport={'package_sha256':job['package_sha256'],'parts':[]}
        parts=out/'transport';parts.mkdir(exist_ok=True)
        with package.open('rb') as f:
            i=0
            while data:=f.read(32*1024*1024):
                name=f'daily_part_{i:03d}.bin';part=parts/name;part.write_bytes(data)
                transport['parts'].append({'name':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
                run_colab(['upload','-s',session,win_path_to_wsl(part),'/content/'+name],timeout=180)
                i+=1
        write(out/'TRANSPORT.json',transport)
        for path,remote_path in ((out/'TRANSPORT.json','/content/daily_transport.json'),(out/'JOB.json','/content/daily_job.json')):
            run_colab(['upload','-s',session,win_path_to_wsl(path),remote_path],timeout=120)
        run_colab(['exec','-s',session,'--timeout','180','-f',win_path_to_wsl(ROOT/'tools/mlb_daily_history_reassemble.py')],timeout=240)
        dispatched=True;run['state']='running';write(out/'RUN.json',run)
        run_colab(['exec','-s',session,'--timeout','2400','-f',win_path_to_wsl(Path(__file__))],timeout=2600,check=False)
        result = out/'results.zip'
        run_colab(['download','-s',session,'/content/daily_results.zip',win_path_to_wsl(result)],timeout=600)
        with zipfile.ZipFile(result) as z:
            for name in z.namelist():assert not Path(name).is_absolute() and '..' not in Path(name).parts and '\\' not in name
            files = json.loads(z.read('FILES.json'))
            for name,meta in files.items():
                data=z.read(name);assert len(data)==meta['bytes'] and hashlib.sha256(data).hexdigest()==meta['sha256'],name
            report=json.loads(z.read('REPORT.json'))
            assert report['package_sha256']==job['package_sha256']
            z.extractall(out/'results')
        complete=not report['errors'];run['state']='downloaded_verified' if complete else 'retained_for_repair'
        print(json.dumps(report),flush=True)
    finally:
        if started and (complete or not dispatched):
            run_colab(['stop','-s',session],timeout=150);run['runtime_stopped']=True
        elif started:run['state']='retained_for_same_handle_recovery'
        write(out/'RUN.json',run)


def retry_upload(target, source_resume=False):
    sys.path[:0]=[str(ROOT),str(ROOT/'_pydeps')]
    from _colab_drive_pipeline import run_colab,win_path_to_wsl
    out=ROOT/'postmortem/pregame_refresh'/target
    old=read(out/'RUN.json')
    expected_state='waiting_source_publication_remote_stopped' if source_resume else 'starting'
    assert old.get('runtime_stopped') and old['state']==expected_state, 'Existing run is not eligible for this recovery mode'
    if source_resume:
        from _mlb_recent_sc import fetch_day
        request=read(out/'REQUEST.json');day=request['history_through']
        assert day < target and request['target_date']==target
        frame=fetch_day(day)
        if frame is None or frame.empty:
            status={'state':'waiting_source_publication','day':day,'remote_created':False}
            write(out/'SOURCE_RETRY_STATUS.json',status);print(json.dumps(status),flush=True)
            return
        assert set(frame.game_date.astype(str).str[:10])=={day}
        fetched=read(out/'results/FETCHED_HISTORY.json')
        expected={str(game['gamePk']) for game in fetched}
        assert set(frame.game_pk.astype(int).astype(str))==expected,'Published day does not cover the source games'
    package=out/'package.zip';job=read(out/'JOB.json')
    assert sha(package)==job['package_sha256']
    transport={'package_sha256':job['package_sha256'],'parts':[]}
    parts=out/'transport';parts.mkdir(exist_ok=True)
    with package.open('rb') as f:
        i=0
        while data:=f.read(32*1024*1024):
            name=f'daily_part_{i:03d}.bin';path=parts/name;path.write_bytes(data)
            transport['parts'].append({'name':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()});i+=1
    write(out/'TRANSPORT.json',transport)
    write(out/('RUN_before_source_resume.json' if source_resume else 'RUN_upload_failed.json'),old)
    session='daily-history-'+target.replace('-','')+('-resume-'+datetime.now().strftime('%H%M%S') if source_resume else '-r1')
    run={**old,'session':session,'runtime_stopped':False,'state':'starting_chunked_upload','stop_method':'colab stop -s '+session}
    write(out/'RUN.json',run)
    started=dispatched=complete=False
    try:
        run_colab(['new','-s',session],timeout=180);started=True
        for item in transport['parts']:
            run_colab(['upload','-s',session,win_path_to_wsl(parts/item['name']),'/content/'+item['name']],timeout=180)
        for local_path,remote_path in ((out/'TRANSPORT.json','/content/daily_transport.json'),(out/'JOB.json','/content/daily_job.json')):
            run_colab(['upload','-s',session,win_path_to_wsl(local_path),remote_path],timeout=120)
        run_colab(['exec','-s',session,'--timeout','180','-f',win_path_to_wsl(ROOT/'tools/mlb_daily_history_reassemble.py')],timeout=240)
        dispatched=True;run['state']='running';write(out/'RUN.json',run)
        run_colab(['exec','-s',session,'--timeout','2400','-f',win_path_to_wsl(Path(__file__))],timeout=2600,check=False)
        result=out/'results.zip'
        run_colab(['download','-s',session,'/content/daily_results.zip',win_path_to_wsl(result)],timeout=600)
        with zipfile.ZipFile(result) as z:
            for name in z.namelist():assert not Path(name).is_absolute() and '..' not in Path(name).parts and '\\' not in name
            files=json.loads(z.read('FILES.json'))
            for name,meta in files.items():
                data=z.read(name);assert len(data)==meta['bytes'] and hashlib.sha256(data).hexdigest()==meta['sha256'],name
            report=json.loads(z.read('REPORT.json'));assert report['package_sha256']==job['package_sha256']
            z.extractall(out/'results')
        complete=not report['errors'];run['state']='downloaded_verified' if complete else 'retained_for_repair'
        print(json.dumps(report),flush=True)
    finally:
        if started and (complete or not dispatched):
            run_colab(['stop','-s',session],timeout=150);run['runtime_stopped']=True
        elif started:run['state']='retained_for_same_handle_recovery'
        write(out/'RUN.json',run)


def promote(target):
    sys.path[:0] = [str(ROOT), str(ROOT/'_pydeps')]
    import pyarrow.parquet as pq
    out = ROOT/'postmortem/pregame_refresh'/target
    src = out/'results'
    report, request = read(src/'REPORT.json'),read(out/'REQUEST.json')
    assert not report['errors'] and report['target_date']==target
    files = read(src/'FILES.json')
    for name,meta in files.items():
        assert (src/name).stat().st_size==meta['bytes'] and sha(src/name)==meta['sha256'],name
    history = read(src/'HISTORY_MANIFEST.json')
    sources = read(src/'player_pool/sources.json')
    assert set(history)==set(sources)
    assert all(row['date']==history[gp]['date'] and row['date']<target for gp,row in sources.items())
    counts = {}
    for name in BASE_FILES:
        path = src/'updated'/name
        pf=pq.ParquetFile(path)
        id_col,date_col=('game_pk','game_date') if name.startswith('mlb_') else ('gamePk','date')
        ids,dates=set(),set()
        for batch in pf.iter_batches(batch_size=8192,columns=[id_col,date_col]):
            for row in batch.to_pylist():
                gp=str(int(row[id_col]));day=str(row[date_col])[:10]
                assert gp in history and day==history[gp]['date'] and day<target,(name,gp,day)
                ids.add(gp);dates.add(day)
        assert ids==set(history),name
        counts[name]={'rows':pf.metadata.num_rows,'games':len(ids),'max_date':max(dates)}
    names=read(src/'PLAYER_POOL_NAMES.json')
    pool={}
    for pid,name in names.items():
        path=src/'player_pool'/(pid+'.json');d=read(path)
        assert d['player_id']==pid and d['target_date']==target and d['history_through']==report['history_through']
        assert set(d['source_gamePks'])<=set(history)
        pool[pid]={'name':name,'path':str(path),'sha256':sha(path)}
    for row in read(src/'PROVISIONAL_ROSTERS.json'):
        for t in row['teams'].values():
            assert {p['id'] for p in t['players']}<=set(pool)
    pending=[]
    for name,relative in request['promotion_targets'].items():
        source,dest=src/'updated'/name,ROOT/relative
        current=sha(dest) if dest.exists() else None
        assert current in (request['baseline_hashes'][relative],sha(source)),'Concurrent data change: '+relative
        pending.append((source,dest))
    done=[]
    for source,dest in pending:
        if not dest.exists() or sha(dest)!=sha(source):
            backup=out/'backups'/dest.relative_to(ROOT)
            backup.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists() and not backup.exists():shutil.copy2(dest,backup)
            tmp=dest.with_suffix(dest.suffix+'.daily.tmp');shutil.copyfile(source,tmp)
            assert sha(tmp)==sha(source);tmp.replace(dest)
        done.append({'path':str(dest),'sha256':sha(dest)})
        write(out/'PROMOTION_RECEIPT.json',{'state':'promoting_data_only','files':done})
    for directory,destination in [('raw_pbp',ROOT/'_deepread_v3/raw_plays'),('boxscore',ROOT/'postmortem/t075_boxscore_cache'),('event_games',ROOT/'postmortem/raw_pbp_event_ledger/games')]:
        for p in (src/directory).glob('*/*.json'):
            dest=destination/p.parent.name/p.name
            if not dest.exists():
                dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
                assert sha(dest)==sha(p)
    event_receipt=out/'EVENT_LEDGER_PROMOTION.json'
    if not event_receipt.exists():
        subprocess.run([sys.executable,'-X','utf8',str(ROOT/'tools/mlb_pregame_refresh_merge_events.py'),
          '--date',target,'--source',str(src),'--expected-old-sha256',request['ledger_sha256']],check=True,timeout=300)
    events=read(event_receipt)
    assert events['games']==len(history) and not events['missing']
    assert sha(Path(events['path']))==events['sha256']
    write(out/'PLAYER_POOL_INDEX.json',{'target_date':target,'history_through':report['history_through'],'players':pool,
          'source_manifest':str(src/'player_pool/sources.json'),'not_a_confirmed_game_input':True})
    receipt={'target_date':target,'history_through':report['history_through'],'new_games':report['new_games'],
      'history_games':len(history),'players':len(pool),'tables':counts,'event_ledger_sha256':events['sha256'],
      'source_refresh_verified':True,'all_inputs_complete':False,'analysis_started':False,'predictions_changed':False,'notification_sent':False,
      'llm_recap_or_criteria_generated':False,'lineup_and_game_inputs_must_be_refreshed_before_analysis':True,
      'detail_coverage':report['detail_coverage'],'player_pool_index':str(out/'PLAYER_POOL_INDEX.json'),
      'known_limitations':report['limitations'],'owned_colab_stopped':read(out/'RUN.json').get('runtime_stopped',False)}
    assert receipt['owned_colab_stopped']
    write(out/'PROMOTION_RECEIPT.json',{'state':'completed_data_only','files':done,'event_ledger':events['path']})
    write(out/'FINAL_RECEIPT.json',receipt)
    print(json.dumps(receipt,ensure_ascii=False,indent=2))


if __name__=='__main__':
    if Path('/content/daily_package.zip').exists():remote()
    else:
        p=argparse.ArgumentParser();p.add_argument('--date',required=True);p.add_argument('--previous');p.add_argument('--promote',action='store_true');p.add_argument('--retry-upload',action='store_true');p.add_argument('--resume-source',action='store_true')
        args=p.parse_args()
        if args.promote:promote(args.date)
        elif args.resume_source:retry_upload(args.date,source_resume=True)
        elif args.retry_upload:retry_upload(args.date)
        else:
            assert args.previous,'--previous is required for dispatch'
            local(args)
