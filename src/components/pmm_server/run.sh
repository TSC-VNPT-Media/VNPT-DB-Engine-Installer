#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import stat
import subprocess
import time
import urllib.request


def call(argv, required=True, **kwargs):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=kwargs.pop('timeout',300), **kwargs)
    if required and result.returncode:
        # CLI arguments/environment can include credentials; do not echo them.
        raise RuntimeError('PMM Server operation failed: '+Path(argv[0]).name+' '+argv[1])
    return result


def inspect(runtime, kind, name, template):
    args=[runtime]+(['volume'] if kind=='volume' else [])+['inspect','--format',template,name]
    result=call(args,required=False)
    if result.returncode==0:
        return result.stdout.strip()
    if any(text in result.stderr.lower() for text in ('no such','does not exist','no container with','no volume with')):
        return None
    raise RuntimeError('Cannot inspect PMM '+kind+'; refusing to assume it is absent')


def password(path, requested, create):
    if path.is_symlink() or str(path.parent.resolve())!=str(path.parent):
        raise RuntimeError('Unsafe PMM credential path')
    if path.exists():
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid():
            raise RuntimeError('PMM credential must be a root-owned regular file')
        value=path.read_text().strip()
        if not value or (requested and value!=requested):
            raise RuntimeError('PMM credential changed; rotate credentials explicitly')
        path.chmod(0o600)
        return value
    if not create:
        raise RuntimeError('Existing PMM volume/container has no credential file; refusing to reset it')
    path.parent.mkdir(parents=True,exist_ok=True)
    value=requested or secrets.token_urlsafe(32)
    fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:
        f.write(value+'\n')
    return value


def ensure_image(runtime, image, policy, pull_timeout=2400):
    result=call([runtime,'image','inspect',image],required=False)
    if result.returncode and not any(t in result.stderr.lower() for t in ('no such','not known','does not exist','image not found')):
        raise RuntimeError('Cannot inspect cached PMM image')
    if policy=='never' and result.returncode:
        raise RuntimeError('PMM image is not cached and pull_policy is never')
    if policy=='always' or result.returncode:
        try:
            call([runtime,'pull',image],timeout=pull_timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError('PMM image pull timed out after '+str(pull_timeout)+' seconds') from None


def main():
    if os.geteuid()!=0:
        raise RuntimeError('PMM Server installation requires root')
    cfg=json.loads(os.environ['VDE_PMM_CONFIG'])
    local=cfg['server_install']
    if cfg['mode']!='local' or os.environ.get('VDE_PROFILE')!='standalone':
        raise RuntimeError('PMM Server component requires local standalone mode')
    runtime=local['runtime']
    if not shutil.which(runtime):
        if runtime!='podman':
            raise RuntimeError('Docker is missing; install it explicitly or select runtime: podman')
        call(['dnf','install','-y','podman'])
    runtime=shutil.which(runtime)
    if not runtime:
        raise RuntimeError('Container runtime installation failed')
    if local['runtime']=='docker':
        call(['systemctl','enable','--now','docker'])
    call([runtime,'info'])
    name,volume=local['container_name'],local['volume']
    # A pull deadline affects execution only; preserve ownership labels on
    # existing resources when the timeout is added or changed.
    identity={k:v for k,v in local.items() if k!='pull_timeout'}
    fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    owner={'io.vde.component':'pmm-server','io.vde.pmm-config':fingerprint}
    existing=inspect(runtime,'container',name,'{{json .Config.Labels}}')
    volume_labels=inspect(runtime,'volume',volume,'{{json .Labels}}')
    for raw in (existing,volume_labels):
        if raw is not None:
            labels=json.loads(raw) or {}
            if any(labels.get(k)!=v for k,v in owner.items()):
                raise RuntimeError('Existing PMM resource is unowned or configuration changed; refusing replacement')
    if existing is not None and volume_labels is None:
        raise RuntimeError('Existing PMM container lost its persistent volume')
    unit=Path('/etc/systemd/system/vde-pmm-server.service')
    if unit.exists() and '# Managed by vde-install PMM' not in unit.read_text():
        raise RuntimeError('Existing PMM systemd unit is not managed by this installer')
    passwd=password(Path(local['password_file']),os.environ.get('VDE_PMM_PASSWORD',''),
                    create=existing is None and volume_labels is None)
    if volume_labels is None:
        call([runtime,'volume','create',*[a for k,v in owner.items() for a in ['--label',k+'='+v]],volume])
    if existing is None:
        ensure_image(runtime,local['image'],local['pull_policy'],local['pull_timeout'])
        env=dict(os.environ,GF_SECURITY_ADMIN_PASSWORD=passwd,GF_SECURITY_ADMIN_USER='admin')
        call([runtime,'create','--name',name,
              *[a for k,v in owner.items() for a in ['--label',k+'='+v]],
              '--publish',local['listen_address']+':'+str(local['https_port'])+':8443',
              '--volume',volume+':/srv','--memory',local['memory'],
              '--env','GF_SECURITY_ADMIN_USER','--env','GF_SECURITY_ADMIN_PASSWORD',
              '--env','PMM_DATA_RETENTION='+local['retention'],local['image']],env=env)
    else:
        # Labels alone do not prove that the named volume is actually attached.
        mounts=json.loads(inspect(runtime,'container',name,'{{json .Mounts}}'))
        if not any(m.get('Name')==volume and m.get('Destination')=='/srv' and m.get('RW',False) for m in mounts):
            raise RuntimeError('PMM persistent volume attachment differs from configured volume')
        image=inspect(runtime,'container',name,'{{.Config.Image}}')
        ports=json.loads(inspect(runtime,'container',name,'{{json .HostConfig.PortBindings}}'))
        expected=[{'HostIp':local['listen_address'],'HostPort':str(local['https_port'])}]
        if image!=local['image'] or (ports or {}).get('8443/tcp')!=expected:
            raise RuntimeError('Existing PMM image or port binding differs; refusing implicit replacement')
    docker_requires='Requires=docker.service\nAfter=docker.service\n' if local['runtime']=='docker' else ''
    unit.write_text('# Managed by vde-install PMM\n[Unit]\nDescription=VDE local PMM Server\n'
                    'Wants=network-online.target\nAfter=network-online.target\n'+docker_requires+
                    '[Service]\nType=simple\nExecStart='+runtime+' start --attach '+name+'\n'
                    'ExecStop='+runtime+' stop --time 30 '+name+'\nRestart=on-failure\n'
                    'RestartSec=5\nTimeoutStopSec=60\nKillMode=none\n'
                    '[Install]\nWantedBy=multi-user.target\n')
    call(['systemctl','daemon-reload'])
    call(['systemctl','enable','--now','vde-pmm-server.service'])
    deadline=time.monotonic()+local['ready_timeout']
    context=ssl._create_unverified_context() if cfg['insecure_tls'] else ssl.create_default_context()
    auth=base64.b64encode(('admin:'+passwd).encode()).decode()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
    health_config=json.loads(inspect(runtime,'container',name,'{{json .Config.Healthcheck}}'))
    health_test=(health_config or {}).get('Test') or []
    has_healthcheck=bool(health_config) and not (health_test and health_test[0]=='NONE')
    while time.monotonic()<deadline:
        state=json.loads(inspect(runtime,'container',name,'{{json .State}}'))
        health=state.get('Health') or state.get('Healthcheck') or {}
        if state.get('Running') is True and (not has_healthcheck or health.get('Status')=='healthy'):
            try:
                with opener.open(cfg['server']+'/v1/readyz',timeout=5) as response:
                    if response.status != 200:
                        raise RuntimeError('PMM readiness endpoint not ready')
                req=urllib.request.Request(cfg['server']+'/graph/api/user',headers={'Authorization':'Basic '+auth})
                with opener.open(req,timeout=5) as response:
                    if json.load(response).get('login')=='admin':
                        call(['systemctl','is-active','--quiet','vde-pmm-server.service'])
                        print('[+] [PMM Server] Healthy; admin authenticated; UI: '+cfg['server'])
                        print('[PMM] Root-only credentials: '+local['password_file'])
                        return
            except Exception:
                pass
        time.sleep(2)
    raise RuntimeError('PMM Server readiness/authentication timed out; existing data retained')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # Exception text is constructed above, never dump commands or secrets.
        if isinstance(exc,RuntimeError):
            print('[ERROR] '+str(exc))
        else:
            print('[ERROR] PMM Server failed ('+type(exc).__name__+'); no success reported')
        raise SystemExit(1)
PY
