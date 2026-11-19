"""Compatibility re-exports for the swarm permission protocol.

The implementation is split by responsibility (ADR-0010 D6):

- :mod:`openharness.swarm.permission_protocol` — request/response types and
  identity helpers;
- :mod:`openharness.swarm.permission_store` — file-based pending/resolved
  storage;
- :mod:`openharness.swarm.permission_transport` — mailbox send/poll/handle.
"""

from __future__ import annotations

from openharness.swarm.permission_protocol import (
    PermissionResolution,
    PermissionResponse,
    SwarmPermissionRequest,
    SwarmPermissionResponse,
    _get_agent_id,
    _get_agent_name,
    _get_team_name,
    _get_teammate_color,
    _is_read_only,
    create_permission_request,
    generate_request_id,
    generate_sandbox_request_id,
    is_swarm_worker,
    is_team_leader,
)
from openharness.swarm.permission_store import (
    _ensure_permission_dirs,
    _get_pending_dir,
    _get_resolved_dir,
    _pending_request_path,
    _resolved_request_path,
    _sync_cleanup_old_resolutions,
    _sync_resolve_permission,
    _sync_write_permission_request,
    cleanup_old_resolutions,
    delete_resolved_permission,
    get_permission_dir,
    poll_for_response,
    read_pending_permissions,
    read_resolved_permission,
    remove_worker_response,
    resolve_permission,
    submit_permission_request,
    write_permission_request,
)
from openharness.swarm.permission_transport import (
    get_leader_name,
    handle_permission_request,
    poll_permission_response,
    send_permission_request,
    send_permission_request_via_mailbox,
    send_permission_response,
    send_permission_response_via_mailbox,
    send_sandbox_permission_request_via_mailbox,
    send_sandbox_permission_response_via_mailbox,
)

__all__ = [
    "PermissionResolution",
    "PermissionResponse",
    "SwarmPermissionRequest",
    "SwarmPermissionResponse",
    "_ensure_permission_dirs",
    "_get_agent_id",
    "_get_agent_name",
    "_get_pending_dir",
    "_get_resolved_dir",
    "_get_team_name",
    "_get_teammate_color",
    "_is_read_only",
    "_pending_request_path",
    "_resolved_request_path",
    "_sync_cleanup_old_resolutions",
    "_sync_resolve_permission",
    "_sync_write_permission_request",
    "cleanup_old_resolutions",
    "create_permission_request",
    "delete_resolved_permission",
    "generate_request_id",
    "generate_sandbox_request_id",
    "get_leader_name",
    "get_permission_dir",
    "handle_permission_request",
    "is_swarm_worker",
    "is_team_leader",
    "poll_for_response",
    "poll_permission_response",
    "read_pending_permissions",
    "read_resolved_permission",
    "remove_worker_response",
    "resolve_permission",
    "send_permission_request",
    "send_permission_request_via_mailbox",
    "send_permission_response",
    "send_permission_response_via_mailbox",
    "send_sandbox_permission_request_via_mailbox",
    "send_sandbox_permission_response_via_mailbox",
    "submit_permission_request",
    "write_permission_request",
]
