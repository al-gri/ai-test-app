"""Persisted trace version boundary: new UUID runs, recoverable legacy tickets."""
import re
from ..models import PolicyError, digest


def execution_trace(ticket, project=None):
    run = ticket['inputs']['execution'].get('run_uuid')
    if run is not None:
        if (run != ticket['id'] or ticket.get('run_uuid') != run
                or not re.fullmatch('[0-9a-f]{32}',run) or int(run,16)==0):
            raise PolicyError('Invalid bound execution UUID')
        return run
    if 'run_uuid' in ticket:
        raise PolicyError('Unbound execution UUID')
    # Never rewrite trace provenance of an already captured RC1 Git artifact.
    # Only old tickets use this branch; Team.dispatch always issues the marker.
    project = project or ticket['workspace_id'].split('/')[0]
    return digest({'project':project,'task':ticket['task_id']})[:32]
