"""Flamegraph folded-stack export, not a claim of automatic semantic segmentation."""
from collections import defaultdict

from allox.workspace.store import WorkspaceError


def folded(spans, measure='calls'):
    if measure not in {'calls', 'self_ns', 'tokens'}:
        raise WorkspaceError('measure must be calls, self_ns or tokens')
    from allox_agent_monitor.profile import profile
    weights = defaultdict(int)
    skipped = [row['span_id'] for row in spans if row['status'] == 'running']
    if measure == 'self_ns':
        groups = profile(spans)['groups']
        for group in groups:
            if group['self_ns'] is None:
                skipped.append(group['name'])
                continue
            weights[tuple(group['semantic_path'])+(group['kind']+':'+group['name'],)] += group['self_ns']
    else:
        for row in spans:
            if row['status'] == 'running':
                skipped.append(row['span_id'])
                continue
            value = 1
            if measure == 'tokens':
                if row['kind'] != 'llm':
                    continue
                metrics = row['metrics']
                values = [metrics.get(a, metrics.get(b)) for a, b in (
                    ('provider.input_tokens', 'provider.prompt_tokens'),
                    ('provider.output_tokens', 'provider.completion_tokens'))]
                if any(type(v) is not int or v < 0 for v in values):
                    skipped.append(row['span_id']+': token usage unavailable or noninteger')
                    continue
                value = sum(values)
            weights[tuple(row['semantic_path'])+(row['kind']+':'+row['name'],)] += value
    def escape(frame):
        return frame.replace('\\', '\\\\').replace(';', '\\x3b').replace('\n', '\\n').replace('\r', '\\r')
    lines = [';'.join(escape(frame) for frame in stack)+' '+str(value)
             for stack, value in sorted(weights.items()) if value]
    return {'format': 'folded', 'measure': measure, 'data': '\n'.join(lines),
            'total_weight': sum(weights.values()), 'skipped': skipped,
            'complete': not skipped, 'semantics': 'explicit semantic paths; operation-count/token/self-time weights'}
