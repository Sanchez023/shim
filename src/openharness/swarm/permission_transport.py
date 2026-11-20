"""Mailbox-based permission transport for swarm leader/worker coordination."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from openharness.swarm.mailbox import (
    MailboxMessage,
    TeammateMailbox,
    create_permission_request_message,
    create_permission_response_message,
    create_sandbox_permission_request_message,
    create_sandbox_permission_response_message,
    write_to_mailbox,
)
from openharness.swarm.permission_protocol import (
    PermissionResolution,
    SwarmPermissionRequest,
    SwarmPermissionResponse,
    _get_agent_id,
    _get_agent_name,
    _get_team_name,
    _get_teammate_color,
    _is_read_only,
)

if TYPE_CHECKING:
    from openharness.permissions.checker import PermissionChecker

# ---------------------------------------------------------------------------
# Leader name lookup
# ---------------------------------------------------------------------------


async def get_leader_name(team_name: str | None = None) -> str | None:
    """Get the leader's agent name from the team file.

    This is needed to address permission requests to the leader's mailbox.

    Args:
        team_name: Team name (falls back to ``CLAUDE_CODE_TEAM_NAME``).

    Returns:
        The leader's name string, or ``None`` if the team file is missing.
        Falls back to ``'team-lead'`` if the lead member is not found.
    """
    from openharness.swarm.team_lifecycle import read_team_file_async

    team = team_name or _get_team_name()
    if not team:
        return None

    team_file = await read_team_file_async(team)
    if not team_file:
        return None

    lead_id = team_file.lead_agent_id
    if lead_id and lead_id in team_file.members:
        return team_file.members[lead_id].name

    return "team-lead"


# ---------------------------------------------------------------------------
# Mailbox-based permission send/receive
# ---------------------------------------------------------------------------


async def send_permission_request_via_mailbox(
    request: SwarmPermissionRequest,
) -> bool:
    """Send a permission request to the leader via the mailbox system.

    This is the mailbox-based approach for forwarding permission requests.
    Writes a ``permission_request`` message to the leader's mailbox.

    Args:
        request: The permission request to send.

    Returns:
        ``True`` if the message was sent successfully.
    """
    leader_name = await get_leader_name(request.team_name)
    if not leader_name:
        return False

    try:
        msg = create_permission_request_message(
            sender=request.worker_name,
            recipient=leader_name,
            request_data={
                "request_id": request.id,
                "agent_id": request.worker_name,
                "tool_name": request.tool_name,
                "tool_use_id": request.tool_use_id,
                "description": request.description,
                "input": request.input,
                "permission_suggestions": request.permission_suggestions,
            },
        )

        await write_to_mailbox(
            leader_name,
            {
                "from": request.worker_name,
                "text": json.dumps(msg.payload),
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                ),
                "color": request.worker_color,
            },
            request.team_name,
        )
        return True
    except OSError:
        return False


async def send_permission_response_via_mailbox(
    worker_name: str,
    resolution: PermissionResolution,
    request_id: str,
    team_name: str | None = None,
) -> bool:
    """Send a permission response to a worker via the mailbox system.

    Called by the leader when approving/denying a permission request.

    Args:
        worker_name: The worker's name to send the response to.
        resolution: The permission resolution.
        request_id: The original request ID.
        team_name: Team name (falls back to ``CLAUDE_CODE_TEAM_NAME``).

    Returns:
        ``True`` if the message was sent successfully.
    """
    team = team_name or _get_team_name()
    if not team:
        return False

    sender_name = _get_agent_name() or "team-lead"
    subtype = "success" if resolution.decision == "approved" else "error"

    try:
        msg = create_permission_response_message(
            sender=sender_name,
            recipient=worker_name,
            response_data={
                "request_id": request_id,
                "subtype": subtype,
                "error": resolution.feedback if subtype == "error" else None,
                "updated_input": resolution.updated_input,
                "permission_updates": resolution.permission_updates,
            },
        )

        await write_to_mailbox(
            worker_name,
            {
                "from": sender_name,
                "text": json.dumps(msg.payload),
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                ),
            },
            team,
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Sandbox permission mailbox helpers
# ---------------------------------------------------------------------------


async def send_sandbox_permission_request_via_mailbox(
    host: str,
    request_id: str,
    team_name: str | None = None,
) -> bool:
    """Send a sandbox permission request to the leader via the mailbox system.

    Called by workers when sandbox runtime needs network access approval.

    Args:
        host: The host requesting network access.
        request_id: Unique ID for this request.
        team_name: Optional team name.

    Returns:
        ``True`` if the message was sent successfully.
    """
    team = team_name or _get_team_name()
    if not team:
        return False

    leader_name = await get_leader_name(team)
    if not leader_name:
        return False

    worker_id = _get_agent_id()
    worker_name = _get_agent_name()
    worker_color = _get_teammate_color()

    if not worker_id or not worker_name:
        return False

    try:
        msg = create_sandbox_permission_request_message(
            sender=worker_name,
            recipient=leader_name,
            request_data={
                "requestId": request_id,
                "workerId": worker_id,
                "workerName": worker_name,
                "workerColor": worker_color,
                "host": host,
            },
        )

        await write_to_mailbox(
            leader_name,
            {
                "from": worker_name,
                "text": json.dumps(msg.payload),
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                ),
                "color": worker_color,
            },
            team,
        )
        return True
    except OSError:
        return False


async def send_sandbox_permission_response_via_mailbox(
    worker_name: str,
    request_id: str,
    host: str,
    allow: bool,
    team_name: str | None = None,
) -> bool:
    """Send a sandbox permission response to a worker via the mailbox system.

    Called by the leader when approving/denying a sandbox network access request.

    Args:
        worker_name: The worker's name to send the response to.
        request_id: The original request ID.
        host: The host that was approved/denied.
        allow: Whether the connection is allowed.
        team_name: Optional team name.

    Returns:
        ``True`` if the message was sent successfully.
    """
    team = team_name or _get_team_name()
    if not team:
        return False

    sender_name = _get_agent_name() or "team-lead"

    try:
        msg = create_sandbox_permission_response_message(
            sender=sender_name,
            recipient=worker_name,
            response_data={
                "requestId": request_id,
                "host": host,
                "allow": allow,
            },
        )

        await write_to_mailbox(
            worker_name,
            {
                "from": sender_name,
                "text": json.dumps(msg.payload),
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                ),
            },
            team,
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Worker helpers: send request / poll response (original mailbox-only approach)
# ---------------------------------------------------------------------------


async def send_permission_request(
    request: SwarmPermissionRequest,
    team_name: str,
    worker_id: str,
    leader_id: str = "leader",
) -> None:
    """Serialize *request* and write it to the leader's mailbox.

    This is the original structured-payload approach.  For new code prefer
    :func:`send_permission_request_via_mailbox`.

    Args:
        request: The permission request to forward.
        team_name: The swarm team name used for mailbox routing.
        worker_id: The sending worker's agent ID.
        leader_id: The leader's agent ID (default ``"leader"``).
    """
    payload: dict[str, Any] = {
        "request_id": request.id,
        "tool_name": request.tool_name,
        "tool_use_id": request.tool_use_id,
        "input": request.input,
        "description": request.description,
        "permission_suggestions": request.permission_suggestions,
        "worker_id": worker_id,
    }
    msg = MailboxMessage(
        id=str(uuid.uuid4()),
        type="permission_request",
        sender=worker_id,
        recipient=leader_id,
        payload=payload,
        timestamp=time.time(),
    )
    leader_mailbox = TeammateMailbox(team_name, leader_id)
    await leader_mailbox.write(msg)


async def poll_permission_response(
    team_name: str,
    worker_id: str,
    request_id: str,
    timeout: float = 60.0,
) -> SwarmPermissionResponse | None:
    """Poll the worker's own mailbox until a matching ``permission_response`` arrives.

    Checks every 0.5 s up to *timeout* seconds.  When a response matching
    *request_id* is found, the message is marked read and the decoded
    :class:`SwarmPermissionResponse` is returned.

    Args:
        team_name: The swarm team name.
        worker_id: The worker's agent ID (owns this mailbox).
        request_id: The ``SwarmPermissionRequest.id`` to match against.
        timeout: Maximum seconds to wait before returning ``None``.

    Returns:
        A :class:`SwarmPermissionResponse`, or ``None`` on timeout.
    """
    worker_mailbox = TeammateMailbox(team_name, worker_id)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        messages = await worker_mailbox.read_all(unread_only=True)
        for msg in messages:
            if msg.type == "permission_response":
                payload = msg.payload
                if payload.get("request_id") == request_id:
                    await worker_mailbox.mark_read(msg.id)
                    return SwarmPermissionResponse(
                        request_id=payload["request_id"],
                        allowed=bool(payload.get("allowed", False)),
                        feedback=payload.get("feedback"),
                        updated_rules=payload.get("updated_rules", []),
                    )
        await asyncio.sleep(0.5)

    return None


# ---------------------------------------------------------------------------
# Leader helper: evaluate and send response
# ---------------------------------------------------------------------------


async def handle_permission_request(
    request: SwarmPermissionRequest,
    checker: PermissionChecker,
) -> SwarmPermissionResponse:
    """Evaluate *request* using the existing :class:`PermissionChecker`.

    Read-only tools are auto-approved without consulting the checker.  For
    all other tools the checker's ``evaluate`` method is called; if the tool
    is allowed or only requires confirmation (and nothing blocks it), it is
    approved; otherwise it is denied.

    Args:
        request: The incoming permission request from a worker.
        checker: An already-configured :class:`~openharness.permissions.checker.PermissionChecker`.

    Returns:
        A :class:`SwarmPermissionResponse` with the decision.
    """
    if _is_read_only(request.tool_name):
        return SwarmPermissionResponse(
            request_id=request.id,
            allowed=True,
            feedback=None,
        )

    file_path: str | None = (
        request.input.get("file_path") or request.input.get("path") or None
    )
    command: str | None = request.input.get("command")

    decision = checker.evaluate(
        request.tool_name,
        is_read_only=False,
        file_path=file_path,
        command=command,
    )

    allowed = decision.allowed
    feedback: str | None = None if allowed else decision.reason

    return SwarmPermissionResponse(
        request_id=request.id,
        allowed=allowed,
        feedback=feedback,
    )


# ---------------------------------------------------------------------------
# Leader helper: write response back to a worker's mailbox
# ---------------------------------------------------------------------------


async def send_permission_response(
    response: SwarmPermissionResponse,
    team_name: str,
    worker_id: str,
    leader_id: str = "leader",
) -> None:
    """Write *response* to the worker's mailbox.

    This is the original structured-payload approach.  For new code prefer
    :func:`send_permission_response_via_mailbox`.

    Args:
        response: The resolution to send.
        team_name: The swarm team name.
        worker_id: The target worker's agent ID.
        leader_id: The sending leader's agent ID (default ``"leader"``).
    """
    payload: dict[str, Any] = {
        "request_id": response.request_id,
        "allowed": response.allowed,
        "feedback": response.feedback,
        "updated_rules": response.updated_rules,
    }
    msg = MailboxMessage(
        id=str(uuid.uuid4()),
        type="permission_response",
        sender=leader_id,
        recipient=worker_id,
        payload=payload,
        timestamp=time.time(),
    )
    worker_mailbox = TeammateMailbox(team_name, worker_id)
    await worker_mailbox.write(msg)
