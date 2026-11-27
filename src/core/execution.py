"""Select actions without silently provisioning unselected bootstrap nodes."""
from core.engine import DAGEngine

CONFIG_TAGS = {'guc','hba','users','databases','extensions','config'}
MONITORING = {'pmm','pmm_server'}


def selection(config, tags=None):
    components=config['components']
    requested=set(x.strip() for x in (tags or '').split(',') if x.strip())
    if not requested:
        return {c['name'] for c in components}
    known={tag for c in components for tag in [c['name'], *c.get('tags',[])]}
    if requested-known:
        raise ValueError('Unknown or disabled tags: '+', '.join(sorted(requested-known)))
    selected={c['name'] for c in components
              if requested & ({c['name']} | set(c.get('tags',[])))}
    if not selected:
        raise ValueError('No enabled component matches the requested tags')
    # Monitoring reconfiguration must prepare the existing database before
    # registering exporters. Bootstrap dependencies are verified, not re-run.
    if selected & MONITORING:
        selected.add('db_ready')
    return selected


def execution_batches(config, selected):
    return [[c for c in batch if c['name'] in selected]
            for batch in DAGEngine(config['components']).get_execution_plan()
            if any(c['name'] in selected for c in batch)]


def is_full_scope(config, hosts, tags):
    return not tags and {h['ip'] for h in hosts} == {h['ip'] for h in config['topology']['hosts']}
