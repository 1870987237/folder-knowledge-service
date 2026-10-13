from folder_kb.management import Registry
from tests.test_collaboration import env, account, library, headers, ADMIN
from tests.test_governance import setup


def user_id(c, token):
    return c.get('/portal/api/me', headers=headers(token)).json()['id']


def create(c, token, oid, name, kind='department', parent=None, members=()):
    r = c.post(f'/portal/api/organizations/{oid}/units', headers=headers(token),
               json={'name': name, 'kind': kind, 'parent_id': parent, 'member_ids': list(members)})
    assert r.status_code == 201, r.text
    return r.json()['id']


def edit(c, token, oid, uid, name, parent=None, members=(), revision=1):
    return c.put(f'/portal/api/organizations/{oid}/units/{uid}', headers=headers(token),
                 json={'name': name, 'parent_id': parent, 'member_ids': list(members), 'expected_revision': revision})


def grant(c, token, lid, uid, role='reader', descendants=False, revision=None):
    path = f'/portal/api/libraries/{lid}/unit-grants'
    if revision is None:
        revision = c.get(path, headers=headers(token)).json()['revision']
    return c.put(path + '/' + uid, headers=headers(token), json={
        'role': role, 'include_descendants': descendants, 'expected_revision': revision})


def read(c, token, lid):
    return c.get(f'/portal/api/libraries/{lid}/document?path=README.md', headers=headers(token)).status_code


def test_directory_admin_boundary_parent_validation_and_cas(env):
    c, r = env; a,b,u,oa,ob,lid = setup(c)
    root = create(c, a, oa, '研发')
    child = create(c, a, oa, '算法', parent=root)
    foreign = create(c, b, ob, '外部')
    group = create(c, a, oa, '项目', 'group')
    path = f'/portal/api/organizations/{oa}/units'
    for token in (u,b):
        assert c.get(path, headers=headers(token)).status_code == 403
        assert c.post(path, headers=headers(token), json={'name': '越权', 'kind': 'group'}).status_code == 403
        assert edit(c, token, oa, root, '越权').status_code == 403
        assert c.delete(path+'/'+group+'?expected_revision=1', headers=headers(token)).status_code == 403
    assert edit(c,a,oa,root,'研发',child).json()['error'] == 'department_cycle'
    assert edit(c,a,oa,child,'算法',foreign).json()['error'] == 'invalid_parent'
    assert edit(c,a,oa,group,'项目',root).status_code == 400
    assert edit(c,a,oa,child,'算法',group).status_code == 400
    assert edit(c,a,oa,child,'研发').json()['error'] == 'duplicate_unit'
    assert edit(c,a,oa,child,'模型',root).status_code == 200
    assert edit(c,a,oa,child,'旧保存',root).json()['error'] == 'unit_changed'
    assert c.delete(path+'/'+root+'?expected_revision=1', headers=headers(a)).json()['error'] == 'department_not_empty'
    assert edit(c,a,oa,child,'模型',root,[user_id(c,b)],2).json()['error'] == 'invalid_member'
    assert len(c.get(path, headers=headers(ADMIN)).json()) == 3


def test_department_scope_group_union_and_live_key_revocation(env):
    c,r = env; a,b,u,oa,ob,lid = setup(c); member = user_id(c,u)
    root = create(c,a,oa,'研发')
    child = create(c,a,oa,'算法',parent=root,members=[member])
    group = create(c,a,oa,'专项','group',members=[member])
    assert grant(c,a,lid,root).status_code == 200
    assert read(c,u,lid) == 403  # Direct department membership only by default.
    assert grant(c,a,lid,root,descendants=True).status_code == 200
    assert read(c,u,lid) == 200
    key = c.post('/portal/api/keys', headers=headers(u), json={'name':'agent','library_ids':[lid]}).json()['token']
    assert c.get('/api/v1/read',headers=headers(key),params={'library_id':lid,'path':'README.md'}).status_code == 200
    assert grant(c,a,lid,group,'editor').status_code == 200
    assert next(x['role'] for x in c.get('/portal/api/libraries', headers=headers(u)).json() if x['id']==lid) == 2
    assert grant(c,a,lid,root,'remove').status_code == 200
    assert read(c,u,lid) == 200  # Independent group grant survives.
    assert edit(c,a,oa,group,'专项',members=[]).status_code == 200
    assert read(c,u,lid) == 403
    assert c.get('/api/v1/read',headers=headers(key),params={'library_id':lid,'path':'README.md'}).status_code == 403
    assert grant(c,a,lid,root,descendants=True).status_code == 200
    assert read(c,u,lid) == 200
    assert edit(c,a,oa,child,'算法',parent=None,members=[member]).status_code == 200
    assert read(c,u,lid) == 403  # Moving a department changes inherited access.


def test_remove_from_org_cleans_memberships_and_stales_editors(env):
    c,r = env; a,b,u,oa,ob,lid = setup(c); member=user_id(c,u)
    group=create(c,a,oa,'协作','group',members=[member])
    assert grant(c,a,lid,group).status_code == 200
    path=f'/portal/api/organizations/{oa}/members'
    assert c.put(path,headers=headers(a),json={'username':'member','role':'remove'}).status_code == 200
    assert read(c,u,lid) == 403
    assert c.put(path,headers=headers(a),json={'username':'member','role':'member'}).status_code == 200
    assert read(c,u,lid) == 403  # Rejoining doesn't restore prior department/group memberships.
    row=c.get(f'/portal/api/organizations/{oa}/units',headers=headers(a)).json()[0]
    assert row['member_ids']==[] and row['revision']==2
    assert edit(c,a,oa,group,'协作',members=[member]).status_code == 409


def test_grant_is_same_org_private_exclusion_delete_and_transfer_cleanup(env):
    c,r = env; a,b,u,oa,ob,lid = setup(c)
    member=user_id(c,u)
    group=create(c,a,oa,'协作','group',members=[member])
    foreign=create(c,b,ob,'外部','group')
    private=library(c,a)
    assert grant(c,a,lid,foreign).status_code == 400
    assert grant(c,a,lid,group,descendants=True).status_code == 400
    assert grant(c,a,private,group,revision=0).status_code == 409
    assert grant(c,u,lid,group,revision=0).status_code == 403
    assert grant(c,a,lid,group).status_code == 200
    assert grant(c,a,lid,group,'manager',revision=0).status_code == 409
    assert c.delete(f'/portal/api/organizations/{oa}/units/{group}?expected_revision=1',headers=headers(a)).status_code == 200
    assert read(c,u,lid)==403
    assert c.get(f'/portal/api/libraries/{lid}/unit-grants',headers=headers(a)).json()['grants']==[]
    group=create(c,a,oa,'新组','group',members=[member]); grant(c,a,lid,group)
    rev=c.get(f'/admin/api/libraries/{lid}/governance',headers=headers(ADMIN)).json()['revision']
    response=c.put(f'/admin/api/libraries/{lid}/governance',headers=headers(ADMIN),json={'organization_id':ob,'expected_revision':rev})
    assert response.status_code==200,response.text
    with r.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM library_unit_grants WHERE library_id=?',(lid,)).fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM collaboration_events WHERE action LIKE 'unit_%'").fetchone()[0]>=4


def test_new_descendants_disabled_accounts_and_personal_grants(env):
    c,r = env; a,b,u,oa,ob,lid = setup(c); member=user_id(c,u)
    root=create(c,a,oa,'研发')
    grant(c,a,lid,root,descendants=True)
    create(c,a,oa,'新部门',parent=root,members=[member])
    assert read(c,u,lid)==200
    # A direct user grant remains independent when the departmental grant is removed.
    c.put(f'/portal/api/libraries/{lid}/members',headers=headers(a),json={'user_id':member,'role':'editor'})
    grant(c,a,lid,root,'remove')
    assert read(c,u,lid)==200
    c.put('/admin/api/accounts/'+member,headers=headers(ADMIN),json={'status':'disabled'})
    from folder_kb.collaboration import user_role
    with r.store.transaction() as db:
        assert user_role(db,member,lid)==0


def test_depth_and_v9_migration(env):
    c,r = env; a,b,u,oa,ob,lid = setup(c)
    parent=None
    for i in range(16):
        parent=create(c,a,oa,str(i),parent=parent)
    response=c.post(f'/portal/api/organizations/{oa}/units',headers=headers(a),json={'name':'too-deep','kind':'department','parent_id':parent})
    assert response.json()['error']=='department_depth'
    with r.store.transaction(write=True) as db:
        db.execute('DROP TABLE library_unit_grants');db.execute('DROP TABLE unit_members');db.execute('DROP TABLE organization_units')
        db.execute('PRAGMA user_version=9')
    new=Registry(str(r.pool.root),str(r.directory))
    with new.store.transaction() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==12
        assert db.execute('SELECT count(*) FROM organization_units').fetchone()[0]==0
    assert read(c,a,lid)==200 and read(c,u,lid)==403


def test_member_profile_atomic_scoping_and_live_permissions(env):
    c,r=env; a,b,u,oa,ob,lid=setup(c); uid=user_id(c,u)
    team=create(c,a,oa,'团队',members=[user_id(c,a)])
    group=create(c,a,oa,'协作','group')
    foreign=create(c,b,ob,'外部')
    path=f'/portal/api/organizations/{oa}/members/{uid}/profile'
    initial=c.get(path,headers=headers(a)).json()
    change={'role':'member','unit_ids':[team,group],'expected_role':initial['role'],'expected_unit_ids':initial['unit_ids']}
    for token in (u,b):
        assert c.get(path,headers=headers(token)).status_code==403
        assert c.put(path,headers=headers(token),json=change).status_code==403
    assert c.put(path,headers=headers(a),json={**change,'role':'admin','unit_ids':[foreign]}).status_code==400
    assert c.get(path,headers=headers(a)).json()==initial
    grant(c,a,lid,group,'editor')
    assert c.put(path,headers=headers(a),json=change).status_code==200
    assert read(c,u,lid)==200
    rows=c.get(f'/portal/api/organizations/{oa}/units',headers=headers(a)).json()
    assert set(next(x for x in rows if x['id']==team)['member_ids'])=={uid,user_id(c,a)}
    assert next(x for x in rows if x['id']==group)['revision']==2
    assert edit(c,a,oa,group,'过期表单').status_code==409
    assert c.put(path,headers=headers(a),json=change).status_code==409
    assert c.put(path,headers=headers(a),json={**change,'expected_unit_ids':[team,group],'unit_ids':[]}).status_code==200
    assert read(c,u,lid)==403


def test_member_profile_last_admin_and_removed_member_do_not_partially_save(env):
    c,r=env; a,b,u,oa,ob,lid=setup(c)
    team=create(c,a,oa,'团队')
    path=f'/portal/api/organizations/{oa}/members/{user_id(c,a)}/profile'
    change={'role':'member','unit_ids':[team],'expected_role':'admin','expected_unit_ids':[]}
    assert c.put(path,headers=headers(a),json=change).json()['error']=='last_admin'
    assert c.get(path,headers=headers(a)).json()['unit_ids']==[]
    memberpath=f'/portal/api/organizations/{oa}/members/{user_id(c,u)}/profile'
    c.put(f'/portal/api/organizations/{oa}/members',headers=headers(a),json={'username':'member','role':'remove'})
    assert c.get(memberpath,headers=headers(a)).status_code==404
    assert c.put(memberpath,headers=headers(a),json={**change,'expected_role':'member'}).status_code==404
