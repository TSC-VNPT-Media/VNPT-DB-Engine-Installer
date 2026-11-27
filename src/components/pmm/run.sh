#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import quote, urlsplit


def call(argv, required=True, **kwargs):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=kwargs.pop('timeout',120),**kwargs)
    if required and result.returncode:
        raise RuntimeError('PMM operation failed: '+Path(argv[0]).name+' '+argv[1]+'; registration not complete')
    return result


def records(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():
            yield from records(child)
    elif isinstance(value,list):
        for child in value:
            yield from records(child)


def registered_service(name):
    data=json.loads(call(['pmm-admin','list','--json']).stdout)
    found=[v for v in records(data) if v.get('service_name')==name]
    if len(found)>1:
        raise RuntimeError('Multiple PMM services have the requested name; reconcile inventory explicitly')
    return found[0] if found else None


def connected():
    status=call(['pmm-admin','status','--wait=30s']).stdout
    if not re.search(r'Connected\s*:\s*true\b',status,re.I):
        raise RuntimeError('PMM Client is not connected to its server')


def main():
    cfg=json.loads(os.environ['VDE_PMM_CONFIG'])
    if not cfg['server']:
        raise RuntimeError('Enabled PMM requires a server; registration cannot be skipped')
    passwd=os.environ.get('VDE_PMM_PASSWORD','')
    if cfg['mode']=='local':
        path=Path(cfg['server_install']['password_file'])
        if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode&0o077:
            raise RuntimeError('Local PMM credential must be root-owned and mode 0600')
        passwd=path.read_text().strip()
    if not passwd:
        raise RuntimeError('PMM credential is missing')
    if not shutil.which('pmm-admin'):
        if os.environ.get('VDE_PACKAGE_SOURCE','vnpt')=='percona' and not shutil.which('percona-release'):
            call(['dnf','install','-y','https://repo.percona.com/yum/percona-release-latest.noarch.rpm'],timeout=300)
        if os.environ.get('VDE_PACKAGE_SOURCE','vnpt')=='percona':
            call(['percona-release','enable','pmm3-client','release'])
        call(['dnf','install','-y','pmm-client'],timeout=600)
    version=call(['pmm-admin','--version']).stdout
    if not re.search(r'\b3\.\d+\.\d+',version):
        raise RuntimeError('Installed PMM Client is not PMM 3; no automatic major upgrade performed')
    url=urlsplit(cfg['server'])
    server_url='https://'+quote(cfg['user'],safe='')+':'+quote(passwd,safe='')+'@'+url.netloc
    flags=['--server-url='+server_url,'--metrics-mode=push']
    if cfg['insecure_tls']:
        flags.append('--server-insecure-tls')
    # No --force: preserve node/service identities and historical metrics on rerun.
    call(['pmm-admin','config',*flags,os.environ['VDE_NODE_IP'],'generic',os.environ['VDE_NODE_NAME']])
    call(['systemctl','enable','--now','pmm-agent'])
    connected()
    if cfg['query_source']=='pgstatements':
        sql="SELECT EXISTS(SELECT FROM pg_extension WHERE extname='pg_stat_statements') AND 'pg_stat_statements'=ANY(string_to_array(replace(current_setting('shared_preload_libraries'),' ',''),','));"
        env=dict(os.environ,PGPASSWORD=os.environ['VDE_PMM_DB_PASSWORD'])
        psql='/usr/pgsql-'+os.environ['VDE_PG_VERSION']+'/bin/psql'
        result=call([psql,'-X','-w','-h','127.0.0.1','-p',os.environ['VDE_PG_PORT'],
                     '-U',cfg['database_user'],'-d','postgres','-At','-v','ON_ERROR_STOP=1','-c',sql],env=env)
        if result.stdout.strip()!='t':
            raise RuntimeError('pg_stat_statements extension/preload not ready; apply PostgreSQL config and restart first')
    name=os.environ['VDE_NODE_NAME']+'-pg'+os.environ['VDE_PG_VERSION']
    existing=registered_service(name)
    if existing:
        if existing.get('address_port')!='127.0.0.1:'+os.environ['VDE_PG_PORT']:
            raise RuntimeError('Existing PMM service points to another endpoint; refusing duplicate registration')
        print('[PMM] PostgreSQL service already registered; existing service retained')
    else:
        call(['pmm-admin','add','postgresql','--username='+cfg['database_user'],
              '--password='+os.environ['VDE_PMM_DB_PASSWORD'],'--query-source='+cfg['query_source'],
              '--metrics-mode=push',name,'127.0.0.1:'+os.environ['VDE_PG_PORT']])
        registered=registered_service(name)
        if not registered:
            raise RuntimeError('PMM PostgreSQL service absent after registration')
        if registered.get('address_port')!='127.0.0.1:'+os.environ['VDE_PG_PORT']:
            raise RuntimeError('PMM PostgreSQL endpoint differs after registration')
    connected()
    print('[+] [PMM] Node connected and PostgreSQL service present at '+cfg['server'])
    print('[PMM] Metrics/exporter Running status and dashboard data still require live acceptance')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print('[ERROR] '+str(exc) if isinstance(exc,RuntimeError) else '[ERROR] PMM failed ('+type(exc).__name__+'); no success reported')
        raise SystemExit(1)
PY
