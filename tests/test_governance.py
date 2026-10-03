from pathlib import Path
import sqlite3

from folder_kb.governance import Governance, Transfer
from folder_kb.management import Registry
from tests.test_collaboration import env, account, library, organization, headers, ADMIN, draft


def setup(c):
    aid,a=account(c,'admin-a'); bid,b=account(c,'admin-b'); uid,u=account(c,'member')
    oa=organization(c,'admin-a','Org A'); ob=organization(c,'admin-b','Org B')
    assert c.put(f'/portal/api/organizations/{oa}/members',headers=headers(a),json={'username':'member','role':'member'}).status_code==200
    lid=library(c,a,oa)
    return a,b,u,oa,ob,lid


def test_org_read_is_opt_in_scoped_and_live_for_keys(env):
    c,r=env; a,b,u,oa,ob,lid=setup(c)
    path=f'/portal/api/libraries/{lid}'
    assert c.get(path+'/document?path=README.md',headers=headers(u)).status_code==403
    assert c.put(path+'/sharing',headers=headers(u),json={'organization_access':'org_read','expected_revision':0}).status_code==403
    assert c.put(path+'/sharing',headers=headers(a),json={'organization_access':'org_read','expected_revision':0}).status_code==200
    assert c.get(path+'/document?path=README.md',headers=headers(u)).status_code==200
    assert c.get(path+'/document?path=README.md',headers=headers(b)).status_code==403
    key=c.post('/portal/api/keys',headers=headers(u),json={'name':'read-agent','library_ids':[lid]}).json()
    assert c.get('/api/v1/read?path=README.md',headers=headers(key['token'])).status_code==200
    assert c.post(path+'/drafts',headers=headers(u),json={'path':'README.md','content':'bad','expected_version':'a'*64}).status_code==403
    assert c.put(path+'/sharing',headers=headers(a),json={'organization_access':'restricted','expected_revision':0}).status_code==409
    assert c.put(path+'/sharing',headers=headers(a),json={'organization_access':'restricted','expected_revision':1}).status_code==200
    assert c.get('/api/v1/read',headers=headers(key['token']),params={'path':'README.md','library_id':lid}).status_code==403
    assert c.get(path+'/document?path=README.md',headers=headers(u)).status_code==403


def test_transfer_detach_reassign_native_preserves_files_clears_all_scopes(env):
    c,r=env; a,b,u,oa,ob,lid=setup(c)
    url=f'/admin/api/libraries/{lid}/governance'
    old=Governance(r).get(lid); root=r.pool.root/old['path']; original=(root/'README.md').read_bytes()
    uid=c.get('/portal/api/me',headers=headers(u)).json()['id']
    c.put(f'/portal/api/libraries/{lid}/members',headers=headers(a),json={'user_id':uid,'role':'publisher'})
    userkey=c.post('/portal/api/keys',headers=headers(u),json={'name':'member-key','library_ids':[lid],'submit_library_ids':[lid]}).json()
    other=library(c,b,ob)
    platformkey=r.create_key('platform-agent',[lid,other],None)
    c.put(f'/portal/api/libraries/{lid}/agent-settings',headers=headers(a),json={'direct_enabled':True})
    assert c.put(url,headers=headers(a),json={'organization_id':ob,'expected_revision':0}).status_code==401
    change=c.put(url,headers=headers(ADMIN),json={'organization_id':ob,'expected_revision':0})
    assert change.status_code==200,change.text
    assert change.json()['revoked_keys']==2 and change.json()['native']
    assert (root/'README.md').read_bytes()==original
    assert c.get(f'/portal/api/libraries/{lid}/document?path=README.md',headers=headers(a)).status_code==403
    assert c.get(f'/portal/api/libraries/{lid}/document?path=README.md',headers=headers(b)).status_code==200
    assert c.get('/api/v1/libraries',headers=headers(userkey['token'])).json()['libraries']==[]
    assert [l['id'] for l in c.get('/api/v1/libraries',headers=headers(platformkey['token'])).json()['libraries']]==[other]
    assert not c.get(f'/portal/api/libraries/{lid}/agent-settings',headers=headers(b)).json()['direct_enabled']
    assert c.put(url,headers=headers(ADMIN),json={'organization_id':None,'expected_revision':0}).status_code==409
    assert c.put(url,headers=headers(ADMIN),json={'organization_id':None,'expected_revision':1}).status_code==200
    assert c.get(f'/portal/api/libraries/{lid}/document?path=README.md',headers=headers(b)).status_code==403
    # Native storage identity survives platform custody, including future edits.
    assert c.put(url,headers=headers(ADMIN),json={'organization_id':oa,'expected_revision':2}).status_code==200
    assert next(l for l in c.get('/portal/api/libraries',headers=headers(a)).json() if l['id']==lid)['editable']
    with r.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM library_members WHERE library_id=?',(lid,)).fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM library_governance_events WHERE library_id=?',(lid,)).fetchone()[0]==3


def test_busy_private_invalid_and_atomic_rejection(env):
    c,r=env; a,b,u,oa,ob,lid=setup(c)
    private=library(c,a)
    assert c.put(f'/admin/api/libraries/{private}/governance',headers=headers(ADMIN),json={'organization_id':ob,'expected_revision':0}).status_code==409
    assert c.put(f'/portal/api/libraries/{private}/sharing',headers=headers(a),json={'organization_access':'org_read','expected_revision':0}).status_code==409
    key=r.create_key('test',[lid],None)
    url=f'/admin/api/libraries/{lid}/governance'
    assert c.put(url,headers=headers(ADMIN),json={'organization_id':'missing','expected_revision':0}).status_code==404
    assert Governance(r).get(lid)['organization_id']==oa
    did=draft(c,a,lid)
    response=c.put(url,headers=headers(ADMIN),json={'organization_id':ob,'expected_revision':0})
    assert response.json()['error']=='transfer_busy'
    assert r.authenticate(key['token'])['library_ids']==[lid]
    assert Governance(r).get(lid)['revision']==0
    # Clear the synthetic pending draft, then independently test an inbox block.
    with r.store.transaction(write=True) as db:
        db.execute("UPDATE document_drafts SET status='rejected' WHERE id=?",(did,))
    from folder_kb.inbox import Inbox,Material
    Inbox(r).grants(key['key']['id'],[lid])
    Inbox(r).submit(key['token'],Material(library_id=lid,submission_id='pending',content={'text':'pending'}))
    assert c.put(url,headers=headers(ADMIN),json={'organization_id':None,'expected_revision':0}).json()['error']=='transfer_busy'


def test_v8_migration_restricted_default_and_existing_grants(env):
    c,r=env; a,b,u,oa,ob,lid=setup(c)
    key=r.create_key('legacy',[lid],None)
    with r.store.transaction(write=True) as db:
        db.execute('DROP TABLE library_governance_events');db.execute('DROP TABLE library_governance');db.execute('PRAGMA user_version=8')
    new=Registry(str(r.pool.root),str(r.directory))
    assert new.authenticate(key['token'])['library_ids']==[lid]
    assert Governance(new).get(lid)['organization_access']=='restricted'
    assert c.get(f'/portal/api/libraries/{lid}/document?path=README.md',headers=headers(u)).status_code==403
