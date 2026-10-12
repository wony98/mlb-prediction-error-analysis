# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Audit archived official preview candidates; never use recap as pregame news."""
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlparse


def publication_check(document,start_utc):
    def stamp(value):
        d=datetime.fromisoformat(value.replace('Z','+00:00'))
        assert d.tzinfo is not None
        return d
    try:
        start=stamp(start_utc)
        published=stamp(document['datePublished'])
        modified=stamp(document['dateModified'])
    except (KeyError,ValueError,AttributeError,AssertionError):
        return {'eligible':False,'reason':'publication_or_modification_time_unverified'}
    if not published<=modified<start:
        return {'eligible':False,'reason':'content_not_verified_before_start'}
    return {'eligible':True,'reason':'publisher_publication_and_modification_before_start',
        'original_live_snapshot_recovered':False,'publisher_metadata_not_independent_archive':True}


def article_document(html,url,metadata=None):
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(html,'html.parser')
    def articles(value):
        if isinstance(value,list):
            for item in value:yield from articles(item)
        elif isinstance(value,dict):
            kind=value.get('@type',[])
            if 'NewsArticle' in ([kind] if isinstance(kind,str) else kind):yield value
            if '@graph' in value:yield from articles(value['@graph'])
    candidates=[metadata] if metadata is not None else []
    if metadata is None:
        for tag in soup.find_all('script',type='application/ld+json'):
            candidates.extend(articles(json.loads(tag.get_text())))
    assert len(candidates)==1,'Single article metadata required'
    meta=candidates[0]
    blocks=[]
    for element in soup.select('.story-part.markdown'):
        text=element.get_text(' ',strip=True)
        if text and text not in [b['text'] for b in blocks]:
            blocks.append({'text':text,'links':[{'text':a.get_text(' ',strip=True),'url':a.get('href')} for a in element.find_all('a',href=True)]})
    if not blocks and meta.get('articleBody'):
        for text in (meta.get('description'),meta['articleBody']):
            if text:blocks.append({'text':BeautifulSoup(text,'html.parser').get_text(' ',strip=True),'links':[]})
    assert blocks,'Article body not found; navigation is not news'
    return {'url':url,'title':meta.get('headline'),'datePublished':meta.get('datePublished'),
        'dateModified':meta.get('dateModified'),'blocks':blocks,
        'retrieved_at':datetime.now(timezone.utc).isoformat(),'review_status':'PENDING',
        'retrieved_after_target_is_not_publication_time':True}


def capture(url,out,wordpress_post_id=None):
    import requests
    from mlb_ability_inputs import save,sha
    hosts=('mlb.com','www.mlb.com','www.rotowire.com','philliesnation.com')
    assert urlparse(url).hostname in hosts
    response=requests.get(url,timeout=30);response.raise_for_status()
    assert urlparse(response.url).hostname in hosts
    metadata=None;api_content=None
    if wordpress_post_id is not None:
        assert urlparse(url).hostname=='philliesnation.com' and isinstance(wordpress_post_id,int)
        api=requests.get(f'https://philliesnation.com/wp-json/wp/v2/posts/{wordpress_post_id}',timeout=30);api.raise_for_status()
        post=api.json();assert post['link'].rstrip('/')==url.rstrip('/')
        metadata={'headline':post['title']['rendered'],'datePublished':post['date_gmt']+'Z',
            'dateModified':post['modified_gmt']+'Z','articleBody':post['content']['rendered']}
        api_content=post
    document=article_document(response.text,response.url,metadata)
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    raw=out.with_suffix('.html')
    if raw.exists():assert raw.read_bytes()==response.content,'Do not overwrite captured news'
    else:raw.write_bytes(response.content)
    document['html_source']={'path':str(raw.resolve()),'sha256':sha(raw)}
    if api_content is not None:
        api_path=out.with_suffix('.publisher.json');save(api_path,api_content)
        document['metadata_source']={'path':str(api_path.resolve()),'sha256':sha(api_path),'url':api.url}
    save(out,document)
    return document


def validated_historical_news(index_path,roster):
    from mlb_ability_inputs import read,sha
    index_path=Path(index_path);index=read(index_path)
    assert index['schema']=='mlb.historical_news_review.v1'
    assert index['target_date']==roster['target_date']
    reviewed=index['review_source'];assert sha(reviewed['path'])==reviewed['sha256']
    manual=read(reviewed['path']);assert manual['target_date']==index['target_date']
    game=index['games'][str(roster['gamePk'])]
    start=roster.get('first_pitch_utc') or roster['scheduled_start_utc']
    assert game['cutoff_utc']==start and game['review_status']=='reviewed'
    assert game['coverage_scope']=='official_team_injury_bulletins_and_linked_reports'
    assert not game['all_public_news_claimed']
    teams={str(t['id']) for t in roster['teams'].values()}
    assert set(game['covered_team_ids'])==teams
    documents={};refs=[reviewed];expected_claims=[]
    for key,ref in game['documents'].items():
        original=manual['documents'][key]
        assert ref['team_id']==original['team_id'] and ref['reviewed_block_ids']==original['reviewed_block_ids']
        expected_claims.extend({**c,'document_id':key,'temporal_check':'pre_start_publication_verified'} for c in original['claims'])
        assert str(ref['team_id']) in teams
        assert sha(ref['path'])==ref['sha256']
        d=read(ref['path']);assert publication_check(d,start)['eligible']
        source=d['html_source'];assert sha(source['path'])==source['sha256']
        documents[key]=d;refs.extend([ref,source])
        if d.get('metadata_source'):
            source=d['metadata_source'];assert sha(source['path'])==source['sha256'];refs.append(source)
    assert {str(r['team_id']) for r in game['documents'].values()}==teams
    assert game['claims'] and game['limitations']
    assert game['claims']==expected_claims,'News claims changed after manual review'
    identity=index['identity_source'];assert sha(identity['path'])==identity['sha256']
    registry=read(identity['path']);refs.append(identity)
    assert registry['through']<index['target_date']
    people={str(r['person']['id']) for r in registry['transactions'] if r.get('person')}
    people|={str(p['id']) for t in roster['teams'].values() for p in t['players']}
    for key,ref in game['documents'].items():
        assert set(ref['reviewed_block_ids'])==set(range(len(documents[key]['blocks'])))
    for claim in game['claims']:
        assert claim['document_id'] in documents and claim['text'].strip()
        assert claim['support_block_ids'] and claim['player_ids']
        assert set(claim['player_ids'])<=people,'Unverified news player identity'
        d=documents[claim['document_id']]
        for b in claim['support_block_ids']:
            assert isinstance(b,int) and 0<=b<len(d['blocks'])
        assert claim['temporal_check']=='pre_start_publication_verified'
    assert {str(game['documents'][c['document_id']]['team_id']) for c in game['claims']}==teams
    return game,refs


def preview_candidates(content,start_utc):
    start=datetime.fromisoformat(start_utc.replace('Z','+00:00'))
    assert start.tzinfo is not None
    preview=(content.get('editorial') or {}).get('preview') or {}
    documents=[preview] if preview.get('date') else [v for v in preview.values() if isinstance(v,dict)]
    accepted=[];rejected=[]
    for doc in documents:
        stamp=doc.get('date') or doc.get('publishDate')
        updated=doc.get('lastUpdated') or doc.get('lastUpdatedDate')
        try:
            published=datetime.fromisoformat(stamp.replace('Z','+00:00')) if stamp else None
            modified=datetime.fromisoformat(updated.replace('Z','+00:00')) if updated else None
        except (ValueError,AttributeError):
            rejected.append('invalid_timestamp');continue
        if published is None or published.tzinfo is None or published>=start:
            rejected.append('publication_not_before_start');continue
        if modified is not None and (modified.tzinfo is None or modified>=start):
            rejected.append('updated_after_start');continue
        accepted.append({'document':doc,'publication_before_start':True,
            'content_last_update_verified':modified is not None,'requires_claim_review':True,
            'original_live_snapshot_recovered':False})
    return {'candidates':accepted,'rejected':rejected,'recap_ignored':True,
        'health_news_complete':False,'empty_preview_does_not_prove_no_news':True}


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--url',required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();d=capture(a.url,a.out)
    print({k:d[k] for k in ('title','datePublished','dateModified')})
