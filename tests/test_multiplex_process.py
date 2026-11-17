"""Two real SoWork tenants in one native gateway: no shared secrets or journal state."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import sqlite3
import tempfile
import threading

import pytest
import hermes_yaml as yaml

from e2e_support import SoWorkServer
from tests.e2e.core.platforms._helpers import Director, GatewayUnderTest
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, write_hermes_home
from tests.fakes.platforms._standin import wait_until

pytestmark = [pytest.mark.spawns_gateway_lookalike,
              pytest.mark.skipif(importlib.util.find_spec("gateway.run_profile_reconcile") is None,
                                 reason="requires the native shared-gateway profile lifecycle")]


@pytest.fixture(scope="module")
def mux():
    with tempfile.TemporaryDirectory(prefix="sw-mux-") as directory:
        root=Path(directory)
        shim=root/'shim';shim.mkdir()
        shutil.copyfile(Path(__file__).with_name('e2e_sitecustomize.py'),shim/'sitecustomize.py')
        tenants={}
        for name in ('alpha','beta'):
            api=SoWorkServer(tenant=name+'-').start()
            director=Director();llm=FakeLLMServer(director);llm.start()
            tenants[name]=SimpleNamespace(api=api,director=director,llm=llm)
        gw=GatewayUnderTest(root/'gateway',llm_base_url=tenants['alpha'].llm.base_url,
            config={'gateway':{'multiplex_profiles':True,'auto_multiplex_migration':False},
                    'plugins':{'enabled':[]},'platforms':{}},
            env={'PYTHONPATH_PREPEND':str(shim),'HERMES_GATEWAY_LOCK_DIR':str(root/'locks')},
            ready=lambda:all(t.api.stream_cursors for t in tenants.values()))
        for name,t in tenants.items():
            home=gw.hermes_home/'profiles'/name
            write_hermes_home(home,t.llm.base_url,api_key='synthetic-model-'+name)
            extra={'subscription_id':t.api.subscription_id,'workspace_id':'workspace',
                   'app_user_id':t.api.app_user_id,'allowed_user_id':'human','channel_id':'dm',
                   'allow_from':['human'],'proactive_enabled':True,'threads_enabled':True}
            config=yaml.safe_load((home/'config.yaml').read_text())
            config.update(plugins={'enabled':['sowork-platform']},
                          platforms={'sowork':{'enabled':True,'extra':extra}},
                          approvals={'mode':'manual','destructive_slash_confirm':False},
                          updates={'check':False})
            (home/'config.yaml').write_text(yaml.safe_dump(config))
            with (home/'.env').open('a') as f:
                f.write(f'SOWORK_USERNAME={t.api.client_id}\nSOWORK_CREDENTIAL={t.api.client_secret}\nSOWORK_INSTALLATION_ID={t.api.installation_id}\n')
            plugins=home/'plugins';plugins.mkdir()
            (plugins/'sowork-platform').symlink_to(Path(__file__).resolve().parents[1],target_is_directory=True)
            t.home=home
            scope=t.api.installation_id+':'+t.api.subscription_id
            t.journal=home/'sowork'/(hashlib.sha256(scope.encode()).hexdigest()[:24]+'.sqlite3')
        gw._env['SOWORK_TEST_APIS']=json.dumps({str(t.home.resolve()):t.api.url for t in tenants.values()})
        try:
            gw.start(timeout=90)
            yield SimpleNamespace(gw=gw,tenants=tenants)
        finally:
            for t in tenants.values():
                if t.api.delivery_gate is not None:t.api.delivery_gate.set()
            gw.stop()
            for t in tenants.values():t.api.close();t.llm.stop()


def delivered(mux,name,token,*,mid=None):
    t=mux.tenants[name]
    event=t.api.message('[in:'+token+']',mid=mid)
    wait_until(lambda:any('reply to '+token in m['body'].get('text','') for m in t.api.visible()),
               name+' response '+token,timeout=45,on_timeout=mux.gw.tail)
    wait_until(lambda:rows(t,'SELECT status FROM inbox WHERE message_id=?',(event['data']['message']['id'],))==[('complete',)],
               name+' durable completion '+token,timeout=30,on_timeout=mux.gw.tail)
    return event


def rows(t,sql,params=()):
    with closing(sqlite3.connect(t.journal.as_uri()+'?mode=ro',uri=True)) as db:
        return db.execute(sql,params).fetchall()


def test_shared_gateway_isolates_credentials_history_cursor_and_outbox(mux):
    a,b=mux.tenants['alpha'],mux.tenants['beta']
    first=delivered(mux,'alpha','alpha-first',mid='same-inbound-id')
    delivered(mux,'beta','beta-first',mid='same-inbound-id')
    delivered(mux,'alpha','alpha-next')
    delivered(mux,'beta','beta-next')
    # Same message IDs and channel IDs still belong to distinct tenant journals.
    assert a.journal!=b.journal and a.journal.exists() and b.journal.exists()
    for own,foreign in ((a,b),(b,a)):
        request=json.dumps(own.director.requests[next(k for k in own.director.requests if k.endswith('-next'))][0])
        assert ('alpha-first' if own is a else 'beta-first') in request
        assert ('beta-first' if own is a else 'alpha-first') not in request
        assert rows(own,"SELECT value FROM meta WHERE key='scope'")==[(own.api.installation_id+':'+own.api.subscription_id,)]
        assert rows(own,"SELECT count(*) FROM inbox WHERE status='complete'")[0][0]>=2
        assert not rows(own,"SELECT logical_id FROM outbox WHERE status='pending'")
        assert set(own.api.deliveries).isdisjoint(foreign.api.deliveries)
    # A valid foreign envelope sent to beta must never reach beta's model.
    b.api.replay(first)
    a.api.replay(first)
    delivered(mux,'alpha','alpha-barrier')
    delivered(mux,'beta','beta-barrier')
    assert a.director.turns['alpha-first']==1
    assert b.director.turns.get('alpha-first',0)==0
    b_cursor=rows(b,"SELECT value FROM meta WHERE key='cursor'")
    delivered(mux,'alpha','alpha-cursor-only')
    assert rows(a,"SELECT value FROM meta WHERE key='cursor'")==[(a.api.events[-1]['logId'],)]
    assert rows(b,"SELECT value FROM meta WHERE key='cursor'")==b_cursor
    snapshot=json.loads((mux.gw.hermes_home/'gateway_state.json').read_text())
    assert {'alpha','beta'}.issubset(snapshot['served_profiles'])
    assert mux.gw.proc.poll() is None
    for name in mux.tenants:
        result=mux.gw.run_cli('--profile',name,'sowork','--json','status')
        assert result.returncode==0,result.stdout+result.stderr
        report=json.loads(result.stdout[result.stdout.find('{'):])
        assert report['gateway']['fresh'] and report['gateway']['state']=='connected'
        assert not report['issues']


def test_shared_gateway_crash_recovers_only_pending_outbox_without_model_replay(mux):
    a,b=mux.tenants['alpha'],mux.tenants['beta']
    a.api.delivery_gate=threading.Event()
    token='alpha-accepted-before-crash'
    a.api.message('[in:'+token+']')
    wait_until(lambda:any(token in m['body'].get('text','') for m in a.api.visible()),
               'server accepted before receipt persisted',timeout=45,on_timeout=mux.gw.tail)
    assert rows(a,"SELECT count(*) FROM outbox WHERE status='pending'")==[(1,)]
    b_cursor=rows(b,"SELECT value FROM meta WHERE key='cursor'")
    cursors={name:len(t.api.stream_cursors) for name,t in mux.tenants.items()}
    proc=mux.gw.proc
    # Deliberate crash of this fixture's isolated child/process group only.
    assert mux.gw.root.is_relative_to(Path(tempfile.gettempdir())) and proc.poll() is None
    os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=10)
    a.api.delivery_gate.set();a.api.delivery_gate=None
    mux.gw._ready=lambda:all(len(t.api.stream_cursors)>cursors[name] for name,t in mux.tenants.items())
    mux.gw.start(timeout=90)
    wait_until(lambda:not rows(a,"SELECT logical_id FROM outbox WHERE status='pending'"),
               'pending receipt reconciled',timeout=30,on_timeout=mux.gw.tail)
    assert sum(token in m['body'].get('text','') for m in a.api.visible())==1
    assert a.director.turns[token]==1
    assert b_cursor==rows(b,"SELECT value FROM meta WHERE key='cursor'")
    for name,t in mux.tenants.items():
        assert t.api.stream_cursors[-1] is not None
        delivered(mux,name,name+'-after-restart')
    assert b.director.turns.get(token,0)==0
