from meta_harness.team_assignment import (
    assign_random_members,
    list_team_members,
    parse_emails,
    verify_team_emails,
)
from meta_harness.ticket_generator import ProposedTicket


def _team(members):
    return [{"id": "T1", "name": "Team", "members": [{"user": m} for m in members]}]


def _member(user_id, email, username):
    return {"id": user_id, "email": email, "username": username}


# ---------------------------------------------------------------------------
# parse_emails
# ---------------------------------------------------------------------------


def test_parse_emails_splits_on_newlines():
    emails = parse_emails("alice@example.com\nbob@example.com")

    assert emails == ["alice@example.com", "bob@example.com"]


def test_parse_emails_splits_on_commas_and_semicolons():
    emails = parse_emails("alice@example.com, bob@example.com; carol@example.com")

    assert emails == ["alice@example.com", "bob@example.com", "carol@example.com"]


def test_parse_emails_dedupes_case_insensitively_preserving_first_seen():
    emails = parse_emails("Alice@Example.com\nalice@example.com")

    assert emails == ["alice@example.com"]


def test_parse_emails_drops_invalid_entries():
    emails = parse_emails("alice@example.com\nnot-an-email\nbob@example.com")

    assert emails == ["alice@example.com", "bob@example.com"]


def test_parse_emails_empty_input_returns_empty_list():
    assert parse_emails("") == []
    assert parse_emails("   \n  \n") == []


# ---------------------------------------------------------------------------
# list_team_members / verify_team_emails
# ---------------------------------------------------------------------------


def test_list_team_members_extracts_id_email_username(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.team_assignment.list_clickup_teams",
        lambda **kw: _team([{"id": 1, "email": "alice@example.com", "username": "Alice"}]),
    )

    members = list_team_members()

    assert members == [{"id": 1, "email": "alice@example.com", "username": "Alice"}]


def test_list_team_members_dedupes_across_teams(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.team_assignment.list_clickup_teams",
        lambda **kw: [
            {"id": "T1", "members": [{"user": {"id": 1, "email": "alice@example.com", "username": "Alice"}}]},
            {"id": "T2", "members": [{"user": {"id": 1, "email": "alice@example.com", "username": "Alice"}}]},
        ],
    )

    members = list_team_members()

    assert len(members) == 1


def test_list_team_members_skips_members_without_email(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.team_assignment.list_clickup_teams",
        lambda **kw: _team([{"id": 1, "email": None, "username": "Alice"}]),
    )

    assert list_team_members() == []


def test_verify_team_emails_matches_real_members_case_insensitively(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.team_assignment.list_clickup_teams",
        lambda **kw: _team([{"id": 1, "email": "alice@example.com", "username": "Alice"}]),
    )

    verified, not_found = verify_team_emails(["Alice@Example.com"])

    assert verified == [{"id": 1, "email": "alice@example.com", "username": "Alice"}]
    assert not_found == []


def test_verify_team_emails_reports_unmatched_emails(monkeypatch):
    monkeypatch.setattr(
        "meta_harness.team_assignment.list_clickup_teams",
        lambda **kw: _team([{"id": 1, "email": "alice@example.com", "username": "Alice"}]),
    )

    verified, not_found = verify_team_emails(["alice@example.com", "ghost@example.com"])

    assert [m["email"] for m in verified] == ["alice@example.com"]
    assert not_found == ["ghost@example.com"]


def test_verify_team_emails_empty_roster_reports_all_not_found(monkeypatch):
    monkeypatch.setattr("meta_harness.team_assignment.list_clickup_teams", lambda **kw: [])

    verified, not_found = verify_team_emails(["alice@example.com"])

    assert verified == []
    assert not_found == ["alice@example.com"]


# ---------------------------------------------------------------------------
# assign_random_members
# ---------------------------------------------------------------------------


def test_assign_random_members_attaches_assignee_fields(monkeypatch):
    monkeypatch.setattr("meta_harness.team_assignment.random.choice", lambda seq: seq[0])
    members = [_member(1, "alice@example.com", "Alice")]
    tickets = [ProposedTicket(title="t1", description="d")]

    assigned = assign_random_members(tickets, members)

    assert assigned[0].assignee_user_id == 1
    assert assigned[0].assignee_email == "alice@example.com"
    assert assigned[0].assignee_name == "Alice"


def test_assign_random_members_no_members_returns_tickets_unchanged():
    tickets = [ProposedTicket(title="t1", description="d")]

    assigned = assign_random_members(tickets, [])

    assert assigned[0].assignee_user_id is None


def test_assign_random_members_does_not_mutate_input():
    members = [_member(1, "alice@example.com", "Alice")]
    tickets = [ProposedTicket(title="t1", description="d")]

    assign_random_members(tickets, members)

    assert tickets[0].assignee_user_id is None


def test_assign_random_members_preserves_other_fields(monkeypatch):
    monkeypatch.setattr("meta_harness.team_assignment.random.choice", lambda seq: seq[0])
    members = [_member(1, "alice@example.com", "Alice")]
    tickets = [ProposedTicket(title="t1", description="d", priority="urgent", category="backend")]

    assigned = assign_random_members(tickets, members)

    assert assigned[0].title == "t1"
    assert assigned[0].priority == "urgent"
    assert assigned[0].category == "backend"


def test_assign_random_members_can_assign_same_member_to_multiple_tickets(monkeypatch):
    monkeypatch.setattr("meta_harness.team_assignment.random.choice", lambda seq: seq[0])
    members = [_member(1, "alice@example.com", "Alice")]
    tickets = [ProposedTicket(title="t1", description="d"), ProposedTicket(title="t2", description="d")]

    assigned = assign_random_members(tickets, members)

    assert assigned[0].assignee_user_id == 1
    assert assigned[1].assignee_user_id == 1
