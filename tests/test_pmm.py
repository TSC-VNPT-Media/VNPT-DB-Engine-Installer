import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import main
from core.inventory import InventoryManager
from core.pmm import normalize_pmm, query_guc, validate_live_pmm


def embedded(component):
    shell=(ROOT/'src/components'/component/'run.sh').read_text()
    source=shell.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
    module=ModuleType('fixture_'+component)
    exec(compile(source,str(ROOT/'src/components'/component/'run.sh'),'exec'),module.__dict__)
    return module


class InventoryPmmTests(unittest.TestCase):
    def test_standalone_example_enables_colocated_server_before_client(self):
        cfg=InventoryManager(str(ROOT/'src/config/standalone.example.yml')).load()
        order=[c['name'] for batch in main.DAGEngine(cfg['components']).get_execution_plan() for c in batch]
        self.assertLess(order.index('pmm_server'),order.index('pmm'))
        self.assertLess(order.index('patroni'),order.index('pmm'))
        self.assertEqual(cfg['settings_normalized']['pmm']['server'],'https://127.0.0.1:8443')
        self.assertIn('pg_stat_statements',cfg['effective_config']['guc']['shared_preload_libraries'])
        self.assertIn('pg_stat_statements',cfg['effective_config']['extensions'])

    def test_init_standalone_enables_pmm_without_plaintext_default_password(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            path=Path(tmp)/'cluster.yml'
            main.cmd_init(SimpleNamespace(output=str(path),force=False,ips='192.0.2.11',profile='standalone',name='test'))
            cfg=InventoryManager(str(path)).load()
            self.assertEqual(cfg['settings_normalized']['pmm']['mode'],'local')
            self.assertTrue(cfg['settings_normalized']['pmm']['enabled'])
            self.assertNotIn('password: "admin"',path.read_text())

    def test_disabled_pmm_does_not_add_extension_or_components(self):
        cfg=InventoryManager(str(ROOT/'src/config/vde-cluster.example.yml')).load()
        self.assertNotIn('pmm',[c['name'] for c in cfg['components']])
        self.assertEqual(query_guc({'work_mem':'1MB'},cfg['settings_normalized']['pmm']),{'work_mem':'1MB'})

    def test_preload_preserves_other_libraries_and_host_override(self):
        pmm=normalize_pmm({'pmm':{'enabled':True,'mode':'local'}},'standalone')
        guc=query_guc({'shared_preload_libraries':'pgaudit,pg_stat_statements','track_io_timing':'off'},pmm)
        self.assertEqual(guc['shared_preload_libraries'],'pgaudit,pg_stat_statements')
        self.assertEqual(guc['track_io_timing'],'off')
        inv=InventoryManager(str(ROOT/'src/config/standalone.example.yml'));cfg=inv.load()
        self.assertEqual(inv.host_effective_guc({'parameters':{'shared_preload_libraries':'pgaudit'}})['shared_preload_libraries'],'pgaudit,pg_stat_statements')

    def test_invalid_configuration_rejected(self):
        cases=[{'enabled':'false'}, {'enabled':True,'mode':'wrong'}, {'enabled':True,'version':2},
               {'enabled':True,'query_source':'pgstatstatements'},
               {'enabled':True,'server':'http://pmm'}, {'enabled':True,'server':'https://user:secret@pmm'},
               {'enabled':True,'mode':'local','server_install':{'volume':'bad/name'}},
               {'enabled':True,'mode':'local','server_install':{'https_port':True}},
               {'enabled':True,'mode':'local','server_install':{'password_file':'/etc/shadow'}}]
        for cfg in cases:
            with self.subTest(cfg=cfg),self.assertRaises(ValueError):normalize_pmm({'pmm':cfg},'standalone')
        with self.assertRaises(ValueError):normalize_pmm({'pmm':{'enabled':True,'mode':'local'}},'ha')

    def test_live_external_missing_credentials_stops_before_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest={'cluster':{'name':'test','profile':'standalone'},'topology':{'hosts':[{'ip':'192.0.2.11'}]},'settings':{'pmm':{'enabled':True}}}
            path=Path(tmp)/'test.yml';path.write_text(yaml.safe_dump(manifest))
            # Structural offline planning is allowed before supplying secrets.
            InventoryManager(str(path)).load()
            with patch('main.os.geteuid',return_value=0),patch.object(main,'_discover_hardware') as discover:
                with self.assertRaisesRegex(ValueError,'external PMM requires'):
                    main._load_with_preflight(SimpleNamespace(config=str(path),command='apply',dry_run=False))
                discover.assert_not_called()

    def test_environment_credentials_are_forwarded_separately(self):
        inv=InventoryManager(str(ROOT/'src/config/standalone.example.yml'));cfg=inv.load()
        pmm=cfg['settings_normalized']['pmm'];pmm['password_env']='TEST_PMM_TOKEN'
        with patch.dict(os.environ,{'TEST_PMM_TOKEN':'synthetic-token'}):
            validate_live_pmm(pmm, cfg['settings_normalized']['auth'])
            env=main._build_host_env(cfg,inv,cfg['topology']['hosts'][0])
        self.assertEqual(env['VDE_PMM_PASSWORD'],'synthetic-token')
        self.assertNotIn('synthetic-token',env['VDE_PMM_CONFIG'])

    def test_monitor_password_configures_monitor_user_for_pmm(self):
        raw=yaml.safe_load((ROOT/'src/config/standalone.example.yml').read_text())
        raw['settings']['auth']={'postgres_password':'pg-secret','replicator_password':'rep-secret','monitor_password':'mon-secret'}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.yml';path.write_text(yaml.safe_dump(raw))
            inv=InventoryManager(str(path));cfg=inv.load()
            self.assertEqual(cfg['settings_normalized']['pmm']['database_user'],'monitor')
            validate_live_pmm(cfg['settings_normalized']['pmm'],cfg['settings_normalized']['auth'])
            env=main._build_host_env(cfg,inv,cfg['topology']['hosts'][0])
            self.assertEqual(env['VDE_PMM_DB_PASSWORD'],'mon-secret')
            self.assertIn('CREATE ROLE "monitor" LOGIN',env['VDE_DB_OBJECTS_SQL'])
            self.assertIn('GRANT "pg_monitor" TO "monitor";',env['VDE_DB_OBJECTS_SQL'])

    def test_monitor_user_requires_monitor_password_or_env(self):
        raw=yaml.safe_load((ROOT/'src/config/standalone.example.yml').read_text())
        raw['settings']['auth']={'postgres_password':'pg-secret','replicator_password':'rep-secret'}
        raw['settings']['pmm']['database_user']='monitor'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.yml';path.write_text(yaml.safe_dump(raw))
            inv=InventoryManager(str(path));cfg=inv.load()
            with self.assertRaisesRegex(ValueError,'monitor'):
                validate_live_pmm(cfg['settings_normalized']['pmm'],cfg['settings_normalized']['auth'])
            raw['settings']['pmm']['database_password_env']='TEST_PMM_MON_PASS'
            path.write_text(yaml.safe_dump(raw))
            inv=InventoryManager(str(path));cfg=inv.load()
            with patch.dict(os.environ,{'TEST_PMM_MON_PASS':'env-mon-secret'}):
                validate_live_pmm(cfg['settings_normalized']['pmm'],cfg['settings_normalized']['auth'])
                env=main._build_host_env(cfg,inv,cfg['topology']['hosts'][0])
                self.assertEqual(env['VDE_PMM_DB_PASSWORD'],'env-mon-secret')
                self.assertIn('GRANT "pg_monitor" TO "monitor";',env['VDE_DB_OBJECTS_SQL'])

    def test_omitted_monitor_password_defaults_pmm_to_postgres(self):
        raw=yaml.safe_load((ROOT/'src/config/standalone.example.yml').read_text())
        raw['settings']['auth']={'postgres_password':'pg-secret','replicator_password':'rep-secret'}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.yml';path.write_text(yaml.safe_dump(raw))
            inv=InventoryManager(str(path));cfg=inv.load()
            self.assertEqual(cfg['settings_normalized']['pmm']['database_user'],'postgres')
            env=main._build_host_env(cfg,inv,cfg['topology']['hosts'][0])
            self.assertEqual(env['VDE_PMM_DB_PASSWORD'],'pg-secret')
            self.assertNotIn('CREATE ROLE "monitor"',env['VDE_DB_OBJECTS_SQL'])


class ClientPmmTests(unittest.TestCase):
    def setUp(self):
        self.mod=embedded('pmm')
        self.cfg=normalize_pmm({'pmm':{'enabled':True,'mode':'external','server':'pmm.example:443'}},'standalone')
        self.env={'VDE_PMM_CONFIG':json.dumps(self.cfg),'VDE_PMM_PASSWORD':'test:@/secret',
                  'VDE_PMM_DB_PASSWORD':'db-secret','VDE_NODE_IP':'192.0.2.11','VDE_NODE_NAME':'db1',
                  'VDE_PG_PORT':'5432','VDE_PG_VERSION':'16'}
        self.added=False;self.calls=[];self.connection=True;self.sql='t';self.endpoint='127.0.0.1'
    def call(self,argv,**kwargs):
        self.calls.append(argv)
        out=''
        if argv[:2]==['pmm-admin','--version']:out='Version: 3.10.0'
        if argv[:2]==['pmm-admin','status']:out='Connected : '+str(self.connection).lower()
        if argv[0].endswith('/psql'):out=self.sql
        if argv[:2]==['pmm-admin','list']:
            # Match upstream admin/commands/list.go listResult and listResultService.
            out=json.dumps({'service':[{'service_type':'postgresql','service_name':'db1-pg16','address_port':self.endpoint+':5432','service_id':'/service_id/test'}],'agent':[]} if self.added else {'service':[],'agent':[]})
        if argv[:2]==['pmm-admin','add']:self.added=True
        return SimpleNamespace(stdout=out,stderr='',returncode=0)
    def execute(self):
        with patch.dict(os.environ,self.env),patch.object(self.mod,'call',side_effect=self.call),patch.object(self.mod.shutil,'which',return_value='/usr/bin/pmm-admin'),contextlib.redirect_stdout(io.StringIO()) as out:
            self.mod.main()
        return out.getvalue()
    def test_fresh_registration_encodes_url_and_uses_correct_node_and_query_source(self):
        out=self.execute()
        config=next(x for x in self.calls if x[:2]==['pmm-admin','config'])
        self.assertEqual(config[-3:],['192.0.2.11','generic','db1'])
        self.assertIn('--server-url=https://admin:test%3A%40%2Fsecret@pmm.example:443',config)
        self.assertNotIn('--force',config);self.assertNotIn('--server-insecure-tls',config)
        add=next(x for x in self.calls if x[:2]==['pmm-admin','add'])
        self.assertIn('--query-source=pgstatements',add);self.assertIn('--metrics-mode=push',add)
        self.assertNotIn('db-secret',out)
    def test_rerun_retains_existing_service(self):
        self.added=True;self.execute()
        self.assertFalse(any(x[:2]==['pmm-admin','add'] for x in self.calls))
    def test_disconnected_status_zero_does_not_report_success(self):
        self.connection=False
        with self.assertRaisesRegex(RuntimeError,'not connected'):self.execute()
        self.assertFalse(self.added)
    def test_missing_extension_does_not_add_service(self):
        self.sql='f'
        with self.assertRaisesRegex(RuntimeError,'preload not ready'):self.execute()
        self.assertFalse(self.added)
    def test_existing_wrong_endpoint_is_not_silently_reused(self):
        self.added=True;self.endpoint='192.0.2.99'
        with self.assertRaisesRegex(RuntimeError,'another endpoint'):self.execute()
    def test_command_errors_do_not_echo_secret_arguments(self):
        with patch.object(self.mod.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='synthetic-secret')):
            with self.assertRaises(RuntimeError) as error:self.mod.call(['pmm-admin','config','--server-url=https://admin:synthetic-secret@server'])
        self.assertNotIn('synthetic-secret',str(error.exception))
    def test_missing_client_and_repository_tool_are_installed(self):
        env=dict(self.env,VDE_PACKAGE_SOURCE='percona')
        with patch.dict(os.environ,env),patch.object(self.mod,'call',side_effect=self.call),patch.object(self.mod.shutil,'which',return_value=None),contextlib.redirect_stdout(io.StringIO()):
            self.mod.main()
        self.assertIn(['dnf','install','-y','pmm-client'],self.calls)
        self.assertIn(['percona-release','enable','pmm3-client','release'],self.calls)
    def test_add_success_without_inventory_entry_is_failure(self):
        real=self.call
        def no_registration(argv,**kwargs):
            out=real(argv,**kwargs)
            if argv[:2]==['pmm-admin','add']:self.added=False
            return out
        with patch.dict(os.environ,self.env),patch.object(self.mod,'call',side_effect=no_registration),patch.object(self.mod.shutil,'which',return_value='/usr/bin/pmm-admin'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'absent after registration'):self.mod.main()
    def test_internal_repository_does_not_enable_public_percona_repository(self):
        env=dict(self.env,VDE_PACKAGE_SOURCE='internal')
        with patch.dict(os.environ,env),patch.object(self.mod,'call',side_effect=self.call),patch.object(self.mod.shutil,'which',return_value=None),contextlib.redirect_stdout(io.StringIO()):
            self.mod.main()
        self.assertIn(['dnf','install','-y','pmm-client'],self.calls)
        self.assertFalse(any(x[0]=='percona-release' for x in self.calls))
        self.assertFalse(any(any('repo.percona.com' in a for a in x) for x in self.calls))
    def test_vnpt_repository_does_not_enable_public_percona_repository(self):
        env=dict(self.env,VDE_PACKAGE_SOURCE='vnpt')
        with patch.dict(os.environ,env),patch.object(self.mod,'call',side_effect=self.call),patch.object(self.mod.shutil,'which',return_value=None),contextlib.redirect_stdout(io.StringIO()):
            self.mod.main()
        self.assertIn(['dnf','install','-y','pmm-client'],self.calls)
        self.assertFalse(any(x[0]=='percona-release' for x in self.calls))
        self.assertFalse(any(any('repo.percona.com' in a for a in x) for x in self.calls))


class ServerPmmTests(unittest.TestCase):
    def setUp(self):
        self.mod=embedded('pmm_server')
        self.cfg=normalize_pmm({'pmm':{'enabled':True,'mode':'local'}},'standalone')
        self.local=self.cfg['server_install'];self.calls=[]
    def test_password_is_generated_once_private_and_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'password'
            first=self.mod.password(p,'',True)
            self.assertGreater(len(first),30)
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertEqual(self.mod.password(p,'',False),first)
            with self.assertRaisesRegex(RuntimeError,'rotate'):self.mod.password(p,'different',False)
    def test_missing_password_for_existing_volume_is_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError,'refusing to reset'):
                self.mod.password(Path(tmp)/'missing','',False)
    def test_password_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'link';p.symlink_to(Path(tmp)/'other')
            with self.assertRaisesRegex(RuntimeError,'Unsafe'):self.mod.password(p,'',True)
    def execute(self,existing=False,foreign=False,healthy=True,missing=False,no_healthcheck=False,
                running=True,health_status='healthy',ready_status=200,login='admin',unit_active=True):
        fingerprint=hashlib.sha256(json.dumps({k:v for k,v in self.local.items() if k!='pull_timeout'},sort_keys=True).encode()).hexdigest()
        labels=json.dumps({'foreign':'value'} if foreign else {'io.vde.component':'pmm-server','io.vde.pmm-config':fingerprint})
        def inspect(runtime,kind,name,template):
            if kind=='volume':return labels if existing else None
            if '.Config.Labels' in template:return labels if existing else None
            if '.Mounts' in template:return json.dumps([{'Name':self.local['volume'],'Destination':'/srv','RW':True}])
            if '.Config.Image' in template:return self.local['image']
            if '.Config.Healthcheck' in template:return json.dumps(None if no_healthcheck else {'Test':['CMD','check']})
            if '.PortBindings' in template:return json.dumps({'8443/tcp':[{'HostIp':'127.0.0.1','HostPort':'8443'}]})
            return json.dumps({'Running':running,'Healthcheck':None if no_healthcheck else {'Status':health_status}})
        def call(argv,**kwargs):
            if argv[:3]==['systemctl','is-active','--quiet'] and not unit_active:
                raise RuntimeError('inactive systemd service')
            self.calls.append((argv,kwargs));return SimpleNamespace(stdout='',stderr='',returncode=0)
        with tempfile.TemporaryDirectory() as tmp:
            unit=Path(tmp)/'unit.service'
            path=lambda value:unit if str(value)=='/etc/systemd/system/vde-pmm-server.service' else Path(value)
            def open_url(request, **kwargs):
                url=request if isinstance(request,str) else request.full_url
                self.calls.append(('http',url))
                response=io.StringIO(json.dumps({'login':login}))
                response.status=ready_status if url.endswith('/v1/readyz') else 200
                return contextlib.nullcontext(response)
            opener=SimpleNamespace(open=open_url)
            with patch.dict(os.environ,{'VDE_PMM_CONFIG':json.dumps(self.cfg),'VDE_PROFILE':'standalone'}),patch.object(self.mod.os,'geteuid',return_value=0),patch.object(self.mod.shutil,'which',return_value='/usr/bin/podman',side_effect=[None,'/usr/bin/podman'] if missing else None),patch.object(self.mod,'Path',side_effect=path),patch.object(self.mod,'inspect',side_effect=inspect),patch.object(self.mod,'password',return_value='synthetic-password') as password,patch.object(self.mod,'call',side_effect=call),patch.object(self.mod.urllib.request,'build_opener',return_value=opener),patch.object(self.mod.time,'monotonic',side_effect=[0,1,1000] if healthy else [0,1000]),patch.object(self.mod.time,'sleep'),contextlib.redirect_stdout(io.StringIO()) as out:
                self.mod.main()
                self.assertIn('WantedBy=multi-user.target',unit.read_text())
                self.assertNotIn('synthetic-password',unit.read_text())
                self.assertEqual(password.call_args.kwargs['create'],not existing)
            return out.getvalue()
    def test_fresh_server_persists_volume_and_keeps_password_out_of_argv(self):
        self.execute()
        create,kwargs=next((a,k) for a,k in self.calls if len(a)>1 and a[1]=='create')
        self.assertIn('vde-pmm-data:/srv',create)
        self.assertIn('127.0.0.1:8443:8443',create)
        self.assertNotIn('synthetic-password',' '.join(create))
        self.assertEqual(kwargs['env']['GF_SECURITY_ADMIN_PASSWORD'],'synthetic-password')
    def test_owned_rerun_does_not_recreate_container_or_volume(self):
        self.execute(existing=True)
        self.assertFalse(any(len(a)>1 and a[1] in ('create','pull','volume') for a,k in self.calls))
    def test_running_image_without_healthcheck_uses_ready_endpoint(self):
        self.assertIn('admin authenticated', self.execute(existing=True,no_healthcheck=True))
    def test_healthy_container_also_requires_readiness(self):
        with self.assertRaisesRegex(RuntimeError,'timed out'):
            self.execute(existing=True,ready_status=503)
        self.assertTrue(any(a=='http' and b.endswith('/v1/readyz') for a,b in self.calls))
        self.assertFalse(any(a=='http' and b.endswith('/graph/api/user') for a,b in self.calls))
    def test_each_readiness_gate_must_pass(self):
        for opts in ({'running':False}, {'health_status':'unhealthy'}, {'health_status':'starting'},
                     {'health_status':None}, {'no_healthcheck':True,'ready_status':503},
                     {'login':'other'}, {'unit_active':False}):
            with self.subTest(opts=opts), self.assertRaisesRegex(RuntimeError,'timed out'):
                self.execute(existing=True,**opts)
    def test_missing_podman_is_installed_from_os_repository(self):
        self.execute(missing=True)
        self.assertIn(['dnf','install','-y','podman'],[a for a,k in self.calls])
    def test_foreign_resources_are_not_adopted(self):
        with self.assertRaisesRegex(RuntimeError,'refusing replacement'):self.execute(existing=True,foreign=True)
    def test_readiness_deadline_fails_without_resetting_data(self):
        with self.assertRaisesRegex(RuntimeError,'timed out'):self.execute(healthy=False)
        self.assertFalse(any('rm' in a for a,k in self.calls))
    def test_cached_image_missing_policy_does_not_contact_registry(self):
        with patch.object(self.mod,'call',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as call:
            self.mod.ensure_image('podman','internal.registry/pmm:3','missing')
            self.assertEqual(call.call_count,1)
            self.assertEqual(call.call_args.args[0][1:3],['image','inspect'])
    def test_offline_policy_requires_a_cached_image(self):
        with patch.object(self.mod,'call',return_value=SimpleNamespace(returncode=1,stdout='',stderr='image not known')) as call:
            with self.assertRaisesRegex(RuntimeError,'not cached'):self.mod.ensure_image('podman','internal.registry/pmm:3','never')
            self.assertEqual(call.call_count,1)
    def test_missing_image_is_pulled_when_policy_allows(self):
        with patch.object(self.mod,'call',side_effect=[SimpleNamespace(returncode=1,stderr='no such image'),SimpleNamespace(returncode=0)]) as call:
            self.mod.ensure_image('podman','internal.registry/pmm:3','missing',1800)
            self.assertEqual(call.call_args.args[0],['podman','pull','internal.registry/pmm:3'])
            self.assertEqual(call.call_args.kwargs['timeout'],1800)
    def test_pull_timeout_does_not_change_owned_resource_identity(self):
        self.local['pull_timeout']=5000
        self.execute(existing=True)
        self.assertFalse(any(len(a)>1 and a[1] in ('create','pull','volume') for a,k in self.calls))
    def test_pull_timeout_reports_the_step_and_limit(self):
        with patch.object(self.mod,'call',side_effect=[SimpleNamespace(returncode=1,stderr='no such image'),
                                                       self.mod.subprocess.TimeoutExpired('podman', 2400)]):
            with self.assertRaisesRegex(RuntimeError,'image pull timed out after 2400 seconds'):
                self.mod.ensure_image('podman','image','missing',2400)
    def test_invalid_pull_timeout_is_rejected(self):
        for value in (0,7201,True,'2400'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError,'pull timeout'):
                normalize_pmm({'pmm':{'enabled':True,'mode':'local','server_install':{'pull_timeout':value}}},'standalone')


if __name__=='__main__':unittest.main()
