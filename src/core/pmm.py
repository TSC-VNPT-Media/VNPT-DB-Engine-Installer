"""PMM 3 configuration shared by inventory and execution preflight."""
import ipaddress
import os
import re
from urllib.parse import urlsplit


def normalize_pmm(settings, profile, auth=None):
    raw = settings.get('pmm')
    if raw is None:
        raw = settings.get('monitoring') or {}
    if not isinstance(raw, dict):
        raise ValueError('settings.pmm must be a mapping')
    cfg = dict(raw)
    cfg.setdefault('enabled', False)
    if not isinstance(cfg['enabled'], bool):
        raise ValueError('settings.pmm.enabled must be a YAML boolean')
    cfg.setdefault('mode', 'external')
    cfg.setdefault('version', 3)
    cfg.setdefault('query_source', 'pgstatements')
    cfg.setdefault('os_reserve_mb', 1024)
    cfg.setdefault('user', 'admin')
    cfg.setdefault('password', '')
    cfg.setdefault('password_env', '')
    default_db_user = 'monitor' if (auth or {}).get('monitor_password') else 'postgres'
    cfg.setdefault('database_user', default_db_user)
    cfg.setdefault('database_password_env', '')
    cfg.setdefault('insecure_tls', cfg['mode'] == 'local')
    local = dict(cfg.get('server_install') or {})
    defaults = {'runtime':'podman', 'image':'docker.io/percona/pmm-server:3.10.0',
                'container_name':'vde-pmm-server', 'volume':'vde-pmm-data',
                'listen_address':'127.0.0.1', 'https_port':8443,
                'password_file':'/etc/vde/pmm-admin.password',
                'memory':'4g', 'retention':'168h', 'ready_timeout':300, 'pull_timeout':2400, 'pull_policy':'missing'}
    local = {**defaults, **local}
    cfg['server_install'] = local
    cfg.setdefault('server', 'https://127.0.0.1:8443' if cfg['mode']=='local' else '')
    if not cfg['enabled']:
        return cfg
    if cfg['version'] != 3:
        raise ValueError('This PMM integration requires PMM 3; migrate existing PMM 2 explicitly')
    if cfg['mode'] not in ('local','external'):
        raise ValueError('settings.pmm.mode must be local or external')
    if cfg['mode']=='local' and profile!='standalone':
        raise ValueError('Local PMM Server is supported only for standalone; HA uses external PMM')
    if cfg['query_source'] not in ('pgstatements','none'):
        raise ValueError('settings.pmm.query_source must be pgstatements or none')
    if not isinstance(cfg['insecure_tls'], bool):
        raise ValueError('settings.pmm.insecure_tls must be a YAML boolean')
    for key in ('password_env','database_password_env'):
        if cfg[key] and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', str(cfg[key])):
            raise ValueError('Invalid PMM environment variable reference')
    if cfg['mode']=='local':
        if cfg['user']!='admin':
            raise ValueError('Local PMM bootstrap uses the admin account')
        if local['pull_policy'] not in ('missing','always','never'):
            raise ValueError('PMM pull_policy must be missing, always or never')
        if not isinstance(cfg['os_reserve_mb'],int) or isinstance(cfg['os_reserve_mb'],bool) or cfg['os_reserve_mb'] < 512:
            raise ValueError('PMM os_reserve_mb must be at least 512 MiB')
        if local['runtime'] not in ('podman','docker'):
            raise ValueError('PMM server runtime must be podman or docker')
        for key in ('container_name','volume'):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', str(local[key])):
                raise ValueError('Invalid PMM container or volume name')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./:@-]*',str(local['image'])):
            raise ValueError('Invalid PMM server image')
        address=ipaddress.ip_address(str(local['listen_address']))
        if address.version!=4:
            raise ValueError('PMM listen_address must be an IPv4 address')
        if not isinstance(local['https_port'],int) or isinstance(local['https_port'],bool) or not 1024<=local['https_port']<=65535:
            raise ValueError('PMM HTTPS port must be between 1024 and 65535')
        if not re.fullmatch(r'[1-9][0-9]*[mMgG]',str(local['memory'])) or not re.fullmatch(r'[1-9][0-9]*h',str(local['retention'])):
            raise ValueError('Invalid PMM memory or retention value')
        if not isinstance(local['ready_timeout'],int) or not 1<=local['ready_timeout']<=600:
            raise ValueError('Invalid PMM readiness timeout')
        if not isinstance(local['pull_timeout'],int) or isinstance(local['pull_timeout'],bool) or not 1<=local['pull_timeout']<=7200:
            raise ValueError('Invalid PMM image pull timeout; expected 1-7200 seconds')
        if not re.fullmatch(r'/etc/vde/[A-Za-z0-9_.-]+',str(local['password_file'])):
            raise ValueError('Local PMM password_file must be a file under /etc/vde')
        # The host client always reaches its colocated Server over loopback.
        cfg['server']='https://127.0.0.1:'+str(local['https_port'])
    elif cfg['server']:
        url=urlsplit(cfg['server'] if '://' in cfg['server'] else 'https://'+cfg['server'])
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:
            raise ValueError('PMM server must be an HTTPS host[:port] without credentials or path')
        try: url.port
        except ValueError: raise ValueError('Invalid PMM server port') from None
        cfg['server']='https://'+url.netloc
    return cfg


def secret_value(cfg, field='password'):
    name=cfg.get(field+'_env')
    return os.environ.get(name,'') if name else cfg.get(field,'')


def validate_live_pmm(cfg, auth=None):
    if not cfg['enabled']:
        return
    if cfg['mode']=='external' and (not cfg['server'] or not cfg['user'] or not secret_value(cfg)):
        raise ValueError('Enabled external PMM requires server, user and password/password_env before apply')
    if cfg.get('password_env') and not secret_value(cfg):
        raise ValueError('PMM password_env is not set on the installer control node')
    if cfg.get('database_password_env') and not os.environ.get(cfg['database_password_env']):
        raise ValueError('PMM database_password_env is not set on the installer control node')
    if cfg.get('database_user') == 'monitor' and not cfg.get('database_password_env') and not (auth or {}).get('monitor_password'):
        raise ValueError('PMM database_user "monitor" requires settings.auth.monitor_password or settings.pmm.database_password_env')


def query_guc(guc, cfg):
    guc=dict(guc)
    if cfg['enabled'] and cfg['query_source']=='pgstatements':
        libraries=[x.strip() for x in str(guc.get('shared_preload_libraries','')).split(',') if x.strip()]
        if 'pg_stat_statements' not in libraries:
            libraries.append('pg_stat_statements')
        guc['shared_preload_libraries']=','.join(libraries)
        guc.setdefault('pg_stat_statements.track','all')
        guc.setdefault('track_activity_query_size',2048)
        guc.setdefault('track_io_timing','on')
    return guc


def memory_budget(total_mb, cfg):
    pmm_mb = os_mb = 0
    if cfg['enabled'] and cfg['mode']=='local':
        value=cfg['server_install']['memory']
        pmm_mb=int(value[:-1]) * (1024 if value[-1].lower()=='g' else 1)
        os_mb=cfg['os_reserve_mb']
    database_mb=total_mb-pmm_mb-os_mb
    if pmm_mb and database_mb < 1024:
        raise ValueError('Insufficient RAM: reserve PMM + OS and at least 1024 MiB for the database')
    return {'total_ram_mb':total_mb,'pmm_reserved_mb':pmm_mb,'os_reserved_mb':os_mb,'database_ram_mb':database_mb}
