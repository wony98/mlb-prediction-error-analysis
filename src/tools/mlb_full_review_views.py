# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Section-sized views for full evidence. Rendering is not a read attestation."""
import argparse
import json
from pathlib import Path
from mlb_ability_inputs import read,sha
from mlb_evidence_field_contract import PHYSICS,SWING,CONTACT,DOC_URL

FAMILIES={
    'contact_quality': {'counts':('fair_bbe','hard_hit','barrels','barrel_class_n'),
        'metrics':tuple(dict.fromkeys(('ev','launch_angle','contact_xwoba',*CONTACT)))},
    'pitch_physics': {'counts':('pitches','spin_axis_n','spin_axis_circular_mean','spin_axis_resultant_length'),
        'metrics':tuple(dict.fromkeys(('speed','spin','plate_x','plate_z',*PHYSICS)))},
    'swing_mechanics': {'counts':('swings','whiffs','whiff_per_swing','unknown_calls'),
        'metrics':tuple(SWING)}}


def required_review_fields(family):
    spec=FAMILIES[family]
    fields=list(spec['counts'])+[m+suffix for m in spec['metrics'] for suffix in ('_mean','_n')]
    return sorted({role+'.'+field for role in ('pitcher','batter') for field in fields})


def metric_view(stats,family):
    spec=FAMILIES[family]
    return {'counts':{k:stats.get(k) for k in spec['counts']},
        'metrics':{k:{'value':stats.get(k+'_mean'),'observations':stats.get(k+'_n',0),
            'unit':PHYSICS.get(k,SWING.get(k,CONTACT.get(k,'see source field contract')))} for k in spec['metrics']}}


def render(index_path,pitcher_id,family,window,batter_id=None):
    index=read(index_path)
    assert index['full_evidence'], 'Legacy four-count inputs cannot support a full-field view'
    meta=index['pitchers'][pitcher_id];path=Path(index_path).parent/meta['path']
    assert sha(path)==meta['sha256']
    pairs=read(path)['pairs']
    if batter_id is not None:pairs=[p for p in pairs if p['batter_id']==batter_id]
    assert pairs
    view={'gamePk':index['gamePk'],'family':family,'window':window,'source_sha256':sha(path),
        'required_review_fields':required_review_fields(family),
        'definitions':DOC_URL,'role_note':'Pitcher physics=his pitches; pitcher swing/contact=opponents faced. Batter physics=pitches faced; batter swing/contact=his own. Means describe observed groups, not matchup probabilities.',
        'pairs':[{'id':p['id'],'pitcher_name':p['pitcher_name'],'batter_name':p['batter_name'],
            'cells':[{'id':c['id'],'status':c['status'],'pitcher':metric_view(c['pitcher'],family),
                'batter':metric_view(c['batter'],family)} for c in p['cells'] if c['window']==window]} for p in pairs],
        'human_or_llm_review_complete':False}
    return view


def render_context(index_path,player_id,role,window):
    index=read(index_path);ref=index['player_context'];base=Path(index_path).parent.resolve();path=(base/ref['path']).resolve()
    assert path.is_relative_to(base) and sha(path)==ref['sha256']
    item=next(p for p in read(path)['players'] if p['id']==player_id+':'+role)
    return {'gamePk':index['gamePk'],'player_context_id':item['id'],'source':item['source'],
        'status':item['status'],'window':window,
        'rows':[{'row_index':i,**r} for i,r in enumerate(item['rows']) if r['window']==window],
        'interpretation':'Historical situations, not target-game states; shared pitches across groups are not independent evidence.',
        'human_or_llm_review_complete':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--index',type=Path,required=True)
    p.add_argument('--pitcher');p.add_argument('--batter');p.add_argument('--family',choices=FAMILIES)
    p.add_argument('--context-player');p.add_argument('--context-role',choices=('pitcher','batter'))
    p.add_argument('--window',choices=('season','30d','14d'),required=True);p.add_argument('--out',type=Path)
    a=p.parse_args()
    if a.context_player:
        assert a.context_role and not (a.pitcher or a.batter or a.family)
        view=render_context(a.index,a.context_player,a.context_role,a.window)
    else:
        assert a.pitcher and a.family and not a.context_role
        view=render(a.index,a.pitcher,a.family,a.window,a.batter)
    if a.out:
        from mlb_ability_inputs import save
        save(a.out,view);print({'path':str(a.out),'scope':view.get('family',view.get('player_context_id'))})
    else:print(json.dumps(view,ensure_ascii=False,separators=(',',':')))
