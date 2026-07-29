"""Verify a pasted list of team member emails against the real ClickUp
workspace roster, and randomly assign verified members to proposed tickets.

Verification is real, not a format check: an email only counts as verified
if it matches an actual member of a real ClickUp team the harness can see.
"""

from __future__ import annotations

import random
import re
from dataclasses import replace
from typing import List, Optional, Sequence, Tuple

from meta_harness.clickup_bridge import list_clickup_teams
from meta_harness.ticket_generator import ProposedTicket

_EMAIL_SPLIT_RE = re.compile(r"[,;\s]+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_emails(text: str) -> List[str]:
    """Split a freeform pasted block (newline/comma/semicolon/whitespace
    separated) into a deduplicated list of syntactically-plausible emails,
    preserving first-seen order."""
    seen = set()
    emails: List[str] = []
    for token in _EMAIL_SPLIT_RE.split(text or ""):
        candidate = token.strip().lower()
        if not candidate or candidate in seen or not _EMAIL_RE.match(candidate):
            continue
        seen.add(candidate)
        emails.append(candidate)
    return emails


def list_team_members() -> List[dict]:
    """Every real member across every ClickUp team the harness can see,
    deduplicated by user id. Each entry: {clickup_id, email, username}."""
    teams = list_clickup_teams()
    by_id: dict = {}
    for team in teams:
        for member in team.get("members", []):
            user = member.get("user") or {}
            user_id = user.get("id")
            email = user.get("email")
            if user_id is None or not email:
                continue
            by_id[user_id] = {
                "clickup_id": user_id,
                "email": str(email).lower(),
                "username": user.get("username") or email,
            }
    return list(by_id.values())


def verify_team_emails(
    emails: Sequence[str], *, members: Optional[Sequence[dict]] = None
) -> Tuple[List[dict], List[str]]:
    """Cross-reference pasted emails against a member roster — the real
    ClickUp workspace roster by default, or an already-fetched `members`
    list (e.g. a Linear team's roster) when given. Returns (verified
    members, emails with no matching member), both in the same order as
    `emails`."""
    if members is None:
        members = list_team_members()
    members_by_email = {m["email"]: m for m in members}
    verified: List[dict] = []
    not_found: List[str] = []
    for email in emails:
        member = members_by_email.get(email.lower())
        if member:
            verified.append(member)
        else:
            not_found.append(email)
    return verified, not_found


def merge_member_rosters(clickup_members: Sequence[dict], linear_members: Sequence[dict]) -> List[dict]:
    """Merge a ClickUp roster ({clickup_id, email, username}) and a raw
    Linear team roster ({id, name, email}) by email, so a person present in
    either (or both) trackers ends up as one entry carrying whichever
    tracker-specific ids were found: {email, username, clickup_id?,
    linear_id?}. This is what lets one "verify team" pass resolve the
    right assignee id for whichever tracker a ticket eventually gets
    created in.
    """
    by_email: dict = {}
    for member in clickup_members:
        entry = by_email.setdefault(member["email"], {"email": member["email"], "username": member["username"]})
        entry["clickup_id"] = member["clickup_id"]
    for member in linear_members:
        email = str(member.get("email") or "").lower()
        if not email:
            continue
        entry = by_email.setdefault(email, {"email": email, "username": member.get("name") or email})
        entry["linear_id"] = member.get("id")
    return list(by_email.values())


def assign_random_members(tickets: Sequence[ProposedTicket], members: Sequence[dict]) -> List[ProposedTicket]:
    """Randomly assign one verified member to each ticket (independent
    draws — the same member can end up on more than one ticket). Sets
    whichever tracker-specific id(s) each member entry carries
    (clickup_id/linear_id), so the ticket carries the right assignee id no
    matter which tracker it's later created in. Tickets are returned
    unchanged if there are no verified members to assign."""
    if not members:
        return list(tickets)
    assigned = []
    for ticket in tickets:
        member = random.choice(list(members))
        assigned.append(
            replace(
                ticket,
                assignee_clickup_id=member.get("clickup_id"),
                assignee_linear_id=member.get("linear_id"),
                assignee_email=member["email"],
                assignee_name=member["username"],
            )
        )
    return assigned
