import contextlib
import copy
import io
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import main
from core.execution import selection
from core.inventory import InventoryManager
from core.pmm import memory_budget,normalize_pmm
from core.state_enforcer import COMPONENT_INPUTS, IGNORED_INPUTS, StateEnforcer


def baseline_payloads(runner):
    found = []
    prefix = "printf '%s\\n' "
    for name, cmd in runner.commands:
        if name != "ssot-enforce":
            continue
        token = shlex.split(cmd[cmd.index(prefix) + len(prefix):])[0]
        found.append(json.loads(token))
    return found


class Runner:
    dry_run=False
    def __init__(self,pending=False,fail=None,baseline='{}'):
        self.events=[];self.commands=[];self.pending=pending;self.fail=fail;self.baseline=baseline
    def run_command(self,host,command,component_name='core'):
        self.events.append(component_name);self.commands.append((component_name,command))
        if component_name==self.fail:return 17,'failure fixture'
        if component_name=='pending-restart':return 0,'shared_preload_libraries' if self.pending else ''
        if component_name=='rolling-restart':self.pending=False
        if component_name=='db-extensions':return 0,'t'
        if component_name in {'db-config-files','db-config-ack'}:return 0,'t'
        if component_name=='db-config-reload':return 0,'1.0\nt'
        if component_name=='ssot-check':return 0,self.baseline
        return 0,''
    def dispatch_script(self,host,path,env_vars=None,component_name='core'):
        self.events.append(component_name)
        return 19 if component_name==self.fail else 0


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.inv=InventoryManager(str(ROOT/'src/config/standalone.example.yml'))
        self.cfg=self.inv.load();self.hosts=self.cfg['topology']['hosts']
    def execute(self,runner,**kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return main._run_pipeline(self.cfg,self.inv,runner,self.hosts,**kwargs)
    def test_full_db_config_restart_and_extensions_precede_monitoring(self):
        r=Runner(pending=True);result=self.execute(r,allow_restart=True)
        ordered=['patroni','patroni-config-sync','db-config-files','db-config-reload','db-config-ack','rolling-restart','db-objects-sync','db-extensions','pmm_server','pmm','ssot-enforce']
        self.assertEqual([r.events.index(k) for k in ordered],sorted(r.events.index(k) for k in ordered))
        self.assertTrue(result['baseline_written'])
    def test_monitoring_failure_leaves_db_prepared_and_no_full_baseline(self):
        r=Runner(fail='pmm_server')
        with self.assertRaises(SystemExit):self.execute(r)
        self.assertIn('db-objects-sync',r.events);self.assertIn('db-extensions',r.events)
        self.assertNotIn('pmm',r.events);self.assertIn('ssot-enforce',r.events)
        written=baseline_payloads(r)[-1]['components_hash']
        self.assertIn('db_ready',written);self.assertIn('os_baseline',written)
        self.assertNotIn('pmm_server',written);self.assertNotIn('pmm',written)
    def test_database_sync_failure_stops_before_monitoring(self):
        r=Runner(fail='patroni-config-sync')
        with self.assertRaisesRegex(RuntimeError,'patroni-config-sync'):self.execute(r)
        self.assertNotIn('pmm_server',r.events);self.assertIn('ssot-enforce',r.events)
    def test_sql_failure_stops_before_monitoring(self):
        r=Runner(fail='db-objects-sync')
        with self.assertRaisesRegex(RuntimeError,'db-objects-sync'):self.execute(r)
        self.assertNotIn('pmm_server',r.events)
    def test_no_restart_permission_does_not_start_monitoring(self):
        r=Runner(pending=True)
        with self.assertRaisesRegex(RuntimeError,'--restart'):self.execute(r)
        self.assertNotIn('pmm_server',r.events);self.assertIn('ssot-enforce',r.events)
    def test_failed_restart_does_not_create_extensions_or_start_pmm(self):
        r=Runner(pending=True,fail='rolling-restart')
        with self.assertRaisesRegex(RuntimeError,'rolling-restart'):self.execute(r,allow_restart=True)
        self.assertNotIn('db-objects-sync',r.events);self.assertNotIn('pmm_server',r.events)
    def test_tags_pmm_prepare_existing_database_without_bootstrapping(self):
        r=Runner(pending=True);result=self.execute(r,tags='pmm',allow_restart=True)
        self.assertIn('db-ready',r.events);self.assertIn('db-objects-sync',r.events)
        self.assertIn('pmm_server',r.events);self.assertIn('pmm',r.events)
        self.assertNotIn('os_baseline',r.events);self.assertNotIn('etcd',r.events)
        self.assertNotIn('patroni',r.events);self.assertIn('ssot-enforce',r.events)
        self.assertEqual(result['scope'],'partial');self.assertTrue(result['baseline_written'])
    def test_server_only_tag_never_records_full_baseline(self):
        r=Runner();self.execute(r,tags='pmm_server')
        self.assertIn('pmm_server',r.events);self.assertNotIn('pmm',r.events)
        self.assertIn('ssot-enforce',r.events)
        self.assertNotIn('pmm',baseline_payloads(r)[-1]['components_hash'])
    def test_missing_database_for_partial_monitoring_fails_before_container(self):
        r=Runner(fail='db-ready')
        with self.assertRaisesRegex(RuntimeError,'db-ready'):self.execute(r,tags='pmm')
        self.assertNotIn('pmm_server',r.events)
    def test_config_tags_run_sync_without_patroni_bootstrap_or_monitoring(self):
        r=Runner();self.execute(r,tags='guc,extensions')
        self.assertIn('patroni-config-sync',r.events);self.assertNotIn('patroni',r.events)
        self.assertNotIn('pmm_server',r.events);self.assertIn('ssot-enforce',r.events)
        self.assertEqual(set(baseline_payloads(r)[-1]['components_hash']), {'db_ready'})
    def test_unknown_mixed_tags_rejected(self):
        with self.assertRaisesRegex(ValueError,'typo'):selection(self.cfg,'pmm,typo')
    def test_limit_subset_does_not_record_cluster_full_baseline(self):
        # Use HA topology so one selected node is a true subset.
        inv=InventoryManager(str(ROOT/'src/config/vde-cluster.example.yml'));cfg=inv.load();r=Runner()
        with contextlib.redirect_stdout(io.StringIO()):
            result=main._run_pipeline(cfg,inv,r,cfg['topology']['hosts'][:1])
        self.assertEqual(result['scope'],'partial');self.assertIn('ssot-enforce',r.events)
        self.assertTrue(result['baseline_written'])
    def test_unrelated_tag_does_not_require_external_pmm_secret(self):
        raw=yaml.safe_load((ROOT/'src/config/standalone.example.yml').read_text())
        raw['settings']['pmm']={'enabled':True,'mode':'external','server':'https://pmm.example','password_env':'ABSENT_EXECUTION_TEST_TOKEN'}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.yml';path.write_text(yaml.safe_dump(raw))
            with patch('main.os.geteuid',return_value=0),patch.dict(os.environ,{'ABSENT_EXECUTION_TEST_TOKEN':''}),patch.object(main,'_discover_hardware',return_value={}) as discover:
                main._load_with_preflight(SimpleNamespace(config=str(path),command='apply',dry_run=False,tags='haproxy'))
                discover.assert_called_once()
    def test_baseline_write_failure_is_not_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError,'ssot-enforce'):self.execute(Runner(fail='ssot-enforce'))
    def test_missing_enabled_script_is_not_skipped(self):
        original=main.os.path.exists
        with patch('main.os.path.exists',side_effect=lambda p:False if str(p).endswith('/pmm/run.sh') else original(p)):
            r=Runner()
            with self.assertRaises(FileNotFoundError):self.execute(r)
            self.assertIn('ssot-enforce',r.events)
            self.assertNotIn('pmm',baseline_payloads(r)[-1]['components_hash'])
    def test_replica_only_selection_does_not_claim_sql_sync(self):
        r=Runner()
        original=r.run_command
        def replica(host,command,component_name='core'):
            if component_name=='patroni-role':return 1,''
            return original(host,command,component_name)
        r.run_command=replica
        with self.assertRaisesRegex(RuntimeError,'No writable primary'):self.execute(r,tags='pmm')
        self.assertNotIn('pmm_server',r.events)
    def test_haproxy_tag_updates_only_that_component_hash(self):
        enforcer=StateEnforcer(self.cfg);host=self.hosts[0]
        env=main._build_host_env(self.cfg,self.inv,host)
        desired=enforcer.component_hashes(host,env)
        observed=dict(desired);observed['haproxy']='f'*64
        r=Runner(baseline=json.dumps({'schema_version':3,'components_hash':observed}))
        result=self.execute(r,tags='haproxy')
        self.assertTrue(result['baseline_written']);self.assertEqual(result['scope'],'partial')
        self.assertNotIn('patroni',r.events);self.assertNotIn('os_baseline',r.events)
        written=baseline_payloads(r)[-1]['components_hash']
        self.assertEqual(written['haproxy'],desired['haproxy'])
        self.assertEqual(written['os_baseline'],desired['os_baseline'])
        self.assertEqual(set(written),set(desired))
    def test_matching_schema2_upgrades_to_every_component_hash(self):
        enforcer=StateEnforcer(self.cfg);host=self.hosts[0]
        names=[c['name'] for c in self.cfg['components']]
        baseline={'schema_version':2,'scope':'full','manifest_hash':enforcer.get_desired_hash(),'completed_components':names}
        r=Runner(baseline=json.dumps(baseline))
        self.execute(r,tags='haproxy')
        desired=enforcer.component_hashes(host,main._build_host_env(self.cfg,self.inv,host))
        self.assertEqual(baseline_payloads(r)[-1]['components_hash'],desired)
    def test_stale_schema2_records_only_the_component_just_applied(self):
        enforcer=StateEnforcer(self.cfg)
        names=[c['name'] for c in self.cfg['components']]
        baseline={'schema_version':2,'scope':'full','manifest_hash':'0'*64,'completed_components':names}
        r=Runner(baseline=json.dumps(baseline))
        self.execute(r,tags='haproxy')
        self.assertEqual(set(baseline_payloads(r)[-1]['components_hash']),{'haproxy'})
    def test_component_failure_still_records_finished_hashes(self):
        r=Runner(fail='haproxy')
        with self.assertRaises(SystemExit):self.execute(r)
        written=baseline_payloads(r)[-1]['components_hash']
        self.assertIn('os_baseline',written);self.assertIn('patroni',written)
        self.assertNotIn('haproxy',written)
    def test_baseline_write_failure_keeps_the_original_component_error(self):
        class FailWrite(Runner):
            def run_command(self,host,command,component_name='core'):
                if component_name=='ssot-enforce':return 17,'no'
                return super().run_command(host,command,component_name)
        with self.assertRaises(SystemExit):self.execute(FailWrite(fail='haproxy'))
    def test_dry_run_does_not_write_component_baseline(self):
        r=Runner();r.dry_run=True
        result=self.execute(r,tags='haproxy')
        self.assertFalse(result['baseline_written']);self.assertNotIn('ssot-enforce',r.events)


class BudgetTests(unittest.TestCase):
    def test_init_uses_remaining_db_ram_not_fixed_four_gib(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            path=Path(tmp)/'init.yml'
            main.cmd_init(SimpleNamespace(output=str(path),force=False,ips='192.0.2.11',profile='standalone',name='test'))
            inv=InventoryManager(str(path));cfg=inv.load()
            cfg=inv.apply_hardware_facts({'192.0.2.11':{'ram_mb':7680,'vcpu':4}})
            self.assertEqual(cfg['smart_defaults']['memory_budget']['database_ram_mb'],2560)
            self.assertEqual(cfg['effective_config']['guc']['shared_buffers'],'640MB')
    def test_oversized_override_and_port_collision_rejected(self):
        for changes in [{'ports':{'postgresql':8443}},{'parameters':{'shared_buffers':'16GB'}}]:
            raw=yaml.safe_load((ROOT/'src/config/standalone.example.yml').read_text())
            raw['settings'].update(changes)
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'config.yml';path.write_text(yaml.safe_dump(raw))
                with self.assertRaises(ValueError):InventoryManager(str(path)).load()
    def test_host_override_cannot_bypass_ram_budget(self):
        raw=yaml.safe_load((ROOT/'src/config/standalone.example.yml').read_text())
        raw['topology']['hosts'][0]['parameters']={'shared_buffers':'16GB'}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.yml';path.write_text(yaml.safe_dump(raw))
            with self.assertRaisesRegex(ValueError,'RAM budget'):InventoryManager(str(path)).load()
    def test_small_non_pmm_hosts_preserve_existing_tiny_support(self):
        cfg=normalize_pmm({},'standalone')
        self.assertEqual(memory_budget(512,cfg)['database_ram_mb'],512)
    def test_insufficient_colocated_memory_rejected(self):
        cfg=normalize_pmm({'pmm':{'enabled':True,'mode':'local'}},'standalone')
        with self.assertRaisesRegex(ValueError,'Insufficient RAM'):memory_budget(4096,cfg)
    def test_archive_command_follows_stanza_and_pgbackrest_enabled_flag(self):
        raw=yaml.safe_load((ROOT/'src/config/standalone.example.yml').read_text())
        raw['settings']['pgbackrest']={'enabled':True,'stanza':'prod_db'}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.yml';path.write_text(yaml.safe_dump(raw))
            cfg=InventoryManager(str(path)).load()
            self.assertEqual(cfg['effective_config']['guc']['archive_mode'],'on')
            self.assertEqual(cfg['effective_config']['guc']['archive_command'],'pgbackrest --stanza=prod_db archive-push %p')
            raw['settings']['pgbackrest']={'enabled':False,'stanza':'prod_db'}
            raw['settings']['auto_tune']=False
            path.write_text(yaml.safe_dump(raw))
            cfg_off=InventoryManager(str(path)).load()
            self.assertEqual(cfg_off['effective_config']['guc']['archive_mode'],'off')
            self.assertEqual(cfg_off['effective_config']['guc']['archive_command'],'')
            raw['settings']['pgbackrest']={'enabled':True,'stanza':'bad stanza'}
            path.write_text(yaml.safe_dump(raw))
            with self.assertRaisesRegex(ValueError,'stanza'):InventoryManager(str(path)).load()


class BaselineAndCommandTests(unittest.TestCase):
    def setUp(self):
        self.inv=InventoryManager(str(ROOT/'src/config/standalone.example.yml'))
        self.cfg=self.inv.load()
        self.enforcer=StateEnforcer(self.cfg)
        self.host_obj=self.cfg['topology']['hosts'][0]
        self.host=self.host_obj['ip']
        self.env=main._build_host_env(self.cfg,self.inv,self.host_obj)
    def test_baseline_is_private_valid_json_and_excludes_passwords(self):
        self.cfg['effective_config']['users']=[{'name':'test','password':'synthetic-secret'}]
        r=Runner();self.enforcer.enforce_baseline(self.host,r,{'db_ready':'abc','haproxy':'def'})
        command=r.commands[-1][1]
        self.assertNotIn('synthetic-secret',command)
        # Execute only the baseline writer with every /etc/vde path remapped
        # into a temporary directory; no privileged filesystem changes.
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['bash','-c',command.replace('/etc/vde',tmp)],check=True)
            path=Path(tmp)/'baseline.json';value=json.loads(path.read_text())
            self.assertEqual(value['schema_version'],3)
            self.assertEqual(value['components_hash'],{'db_ready':'abc','haproxy':'def'})
            self.assertNotIn('manifest_hash',value);self.assertNotIn('scope',value)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_old_hash_only_baseline_requires_full_apply(self):
        old=json.dumps({'manifest_hash':self.enforcer.get_desired_hash()})
        with patch.object(Runner,'run_command',return_value=(0,old)):
            state=self.enforcer.inspect_state(self.host,Runner(),self.env)
        self.assertTrue(state['has_drift'])
        self.assertEqual(state['missing'],sorted(state['desired']))
    def test_current_incomplete_baseline_requires_full_apply(self):
        value={'schema_version':2,'scope':'full','manifest_hash':self.enforcer.get_desired_hash(),'completed_components':['pmm_server']}
        with patch.object(Runner,'run_command',return_value=(0,json.dumps(value))):
            state=self.enforcer.inspect_state(self.host,Runner(),self.env)
        self.assertTrue(state['has_drift'])
        self.assertEqual(state['missing'],sorted(state['desired']))
    def test_matching_schema2_baseline_counts_as_every_component(self):
        names=[c['name'] for c in self.cfg['components'] if self.enforcer._component_applies(c,self.host)]
        value={'schema_version':2,'scope':'full','manifest_hash':self.enforcer.get_desired_hash(),'completed_components':names}
        with patch.object(Runner,'run_command',return_value=(0,json.dumps(value))):
            state=self.enforcer.inspect_state(self.host,Runner(),self.env)
        self.assertFalse(state['has_drift']);self.assertEqual(state['drifted'],[])
    def test_schema3_reports_missing_and_changed_components(self):
        desired=self.enforcer.component_hashes(self.host_obj,self.env)
        recorded=dict(desired);del recorded['haproxy'];recorded['etcd']='f'*64
        raw={'schema_version':3,'components_hash':recorded}
        with patch.object(Runner,'run_command',return_value=(0,json.dumps(raw))):
            state=self.enforcer.inspect_state(self.host,Runner(),self.env)
        self.assertEqual(state['missing'],['haproxy'])
        self.assertEqual(state['changed'],['etcd'])
        self.assertNotIn('patroni',state['drifted'])
    def test_sql_and_patch_commands_fail_closed_and_have_valid_shell_syntax(self):
        r=Runner()
        self.enforcer._psql(r,self.host,"SELECT 'value with quote';",db="db'quoted")
        self.enforcer.reconcile_live_config(self.host,r)
        self.enforcer.sync_pgbouncer_userlist(self.host,r)
        for name,cmd in r.commands:
            subprocess.run(['bash','-n','-c',cmd],check=True)
            self.assertNotIn('|| true',cmd)
            if name=='db-objects-sync':self.assertIn('ON_ERROR_STOP=1',cmd)
    def test_extension_readiness_failure_propagates(self):
        with self.assertRaisesRegex(RuntimeError,'db-extensions'):
            self.enforcer.wait_extensions(self.host,Runner(fail='db-extensions'))
    def test_user_roles_emit_idempotent_grants(self):
        self.cfg['effective_config']['users']=[{'name':'custom_mon','password':'secret','roles':['pg_monitor','pg_read_all_stats']}]
        sql=StateEnforcer(self.cfg).build_objects_sql()
        self.assertIn('CREATE ROLE "custom_mon" LOGIN;',sql)
        self.assertIn('GRANT "pg_monitor" TO "custom_mon";',sql)
        self.assertIn('GRANT "pg_read_all_stats" TO "custom_mon";',sql)


class ComponentHashTests(unittest.TestCase):
    def setUp(self):
        self.inv=InventoryManager(str(ROOT/'src/config/standalone.example.yml'))
        self.cfg=self.inv.load()
        self.host=self.cfg['topology']['hosts'][0]
        self.env=main._build_host_env(self.cfg,self.inv,self.host)
    def hashes(self,cfg,env=None):
        return StateEnforcer(cfg).component_hashes(self.host,env or main._build_host_env(cfg,self.inv,self.host))
    def test_guc_change_drifts_only_db_ready(self):
        before=self.hashes(self.cfg,self.env)
        cfg=copy.deepcopy(self.cfg)
        cfg['effective_config']['guc']=dict(cfg['effective_config']['guc'],work_mem='99MB')
        changed={name for name in before if before[name]!=self.hashes(cfg)[name]}
        self.assertEqual(changed,{'db_ready'})
    def test_postgres_password_drifts_patroni_db_ready_and_pgbouncer(self):
        before=self.hashes(self.cfg,self.env)
        cfg=copy.deepcopy(self.cfg)
        cfg['settings_normalized']['auth']=dict(cfg['settings_normalized']['auth'],postgres_password='rotated-secret')
        changed={name for name in before if before[name]!=self.hashes(cfg)[name]}
        self.assertEqual(changed,{'patroni','db_ready','pgbouncer'})
    def test_missing_pmm_secret_does_not_change_component_hashes(self):
        cfg=copy.deepcopy(self.cfg)
        cfg['settings_normalized']['pmm']=dict(cfg['settings_normalized']['pmm'],password_env='VDE_TEST_PMM_TOKEN',password='')
        with patch.dict(os.environ,{'VDE_TEST_PMM_TOKEN':'aaa'}):
            env_a=main._build_host_env(cfg,self.inv,self.host)
        with patch.dict(os.environ,{'VDE_TEST_PMM_TOKEN':''}):
            env_b=main._build_host_env(cfg,self.inv,self.host)
        self.assertNotEqual(env_a['VDE_PMM_PASSWORD'],env_b['VDE_PMM_PASSWORD'])
        self.assertEqual(self.hashes(cfg,env_a),self.hashes(cfg,env_b))
    def test_script_change_drifts_only_that_component(self):
        enforcer=StateEnforcer(self.cfg)
        before=enforcer.component_hashes(self.host,self.env)
        original=enforcer._script_digest
        def changed(name):
            return 'changed-script' if name=='haproxy' else original(name)
        with patch.object(enforcer,'_script_digest',side_effect=changed):
            after=enforcer.component_hashes(self.host,self.env)
        self.assertEqual({name for name in before if before[name]!=after[name]},{'haproxy'})
    def test_every_script_variable_is_an_input_or_explicitly_ignored(self):
        root=ROOT/'src'/'components'
        declared_components={path.parent.name for path in root.glob('*/run.sh')}
        self.assertEqual(set(COMPONENT_INPUTS),declared_components)
        for script in sorted(root.glob('*/run.sh')):
            used=set(re.findall(r'VDE_[A-Z0-9_]+',script.read_text()))
            declared=set(COMPONENT_INPUTS[script.parent.name]) | set(IGNORED_INPUTS)
            self.assertTrue(used<=declared,script.parent.name+' undeclared '+', '.join(sorted(used-declared)))
    def test_plan_prints_per_component_hashes(self):
        args=SimpleNamespace(config=str(ROOT/'src/config/standalone.example.yml'),offline=False,dry_run=False,
                             user=None,port=None,key=None,sudo=False,timeout=30)
        with patch.object(main,'_discover_hardware',return_value={}),contextlib.redirect_stdout(io.StringIO()) as out:
            main.cmd_plan(args)
        text=out.getvalue()
        self.assertIn('Component hashes',text);self.assertIn('haproxy:',text)
    def test_enforce_reconciles_only_drifted_components_and_hosts(self):
        inv=InventoryManager(str(ROOT/'src/config/vde-cluster.example.yml'));cfg=inv.load()
        enforcer=StateEnforcer(cfg)
        baselines={}
        for host in cfg['topology']['hosts']:
            desired=enforcer.component_hashes(host,main._build_host_env(cfg,inv,host))
            observed=dict(desired)
            if host['ip']=='10.10.20.11': observed['haproxy']='a'*64
            elif host['ip']=='10.10.20.12': observed['patroni']='b'*64
            baselines[host['ip']]=json.dumps({'schema_version':3,'components_hash':observed})
        class ByHost(Runner):
            def run_command(self,host,command,component_name='core'):
                if component_name=='ssot-check': return 0,baselines[host]
                return super().run_command(host,command,component_name)
        calls=[]
        def capture(cfg,inv,runner,hosts,tags=None,force=False,allow_restart=False):
            calls.append((tags,[h['ip'] for h in hosts],force))
            return {'scope':'partial','completed':{},'baseline_written':False}
        args=SimpleNamespace(config='ignored',limit=None,tags=None,restart=False,force=False)
        with patch.object(main,'_load_with_preflight',return_value=(inv,cfg,ByHost())), \
             patch.object(main,'_run_pipeline',side_effect=capture), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            main.cmd_enforce(args)
        self.assertEqual(calls,[('haproxy',['10.10.20.11'],True),('patroni',['10.10.20.12'],True)])
        text=out.getvalue()
        self.assertIn('[DRIFT] 10.10.20.11 haproxy',text)
        self.assertIn('[OK] 10.10.20.13',text)
        self.assertNotIn('[DRIFT] 10.10.20.13',text)


if __name__=='__main__':unittest.main()
