import json

import pytest
import responses

from meta_harness.trackers.linear import LinearAPIError, LinearClient

API_URL = "https://api.linear.app/graphql"


@responses.activate
def test_get_teams():
    responses.add(
        responses.POST,
        API_URL,
        json={"data": {"teams": {"nodes": [{"id": "t1", "name": "Sigo", "key": "SIG"}]}}},
        status=200,
    )
    client = LinearClient(api_key="fake-key")

    teams = client.get_teams()

    assert teams == [{"id": "t1", "name": "Sigo", "key": "SIG"}]


@responses.activate
def test_get_team_states():
    responses.add(
        responses.POST,
        API_URL,
        json={"data": {"team": {"states": {"nodes": [{"id": "s1", "name": "Todo", "type": "unstarted"}]}}}},
        status=200,
    )
    client = LinearClient(api_key="fake-key")

    states = client.get_team_states("t1")

    assert states == [{"id": "s1", "name": "Todo", "type": "unstarted"}]


@responses.activate
def test_get_team_members():
    responses.add(
        responses.POST,
        API_URL,
        json={"data": {"team": {"members": {"nodes": [{"id": "u1", "name": "Ana", "email": "a@x.com"}]}}}},
        status=200,
    )
    client = LinearClient(api_key="fake-key")

    members = client.get_team_members("t1")

    assert members[0]["email"] == "a@x.com"


@responses.activate
def test_get_issues():
    responses.add(
        responses.POST,
        API_URL,
        json={"data": {"team": {"issues": {"nodes": [{"id": "i1", "identifier": "SIG-1", "title": "Fix"}]}}}},
        status=200,
    )
    client = LinearClient(api_key="fake-key")

    issues = client.get_issues("t1")

    assert issues[0]["identifier"] == "SIG-1"


@responses.activate
def test_create_issue_returns_created_issue():
    responses.add(
        responses.POST,
        API_URL,
        json={"data": {"issueCreate": {"success": True, "issue": {"id": "i1", "identifier": "SIG-1"}}}},
        status=200,
    )
    client = LinearClient(api_key="fake-key")

    issue = client.create_issue("t1", "Do the thing")

    assert issue["identifier"] == "SIG-1"


@responses.activate
def test_create_issue_raises_when_not_successful():
    responses.add(
        responses.POST,
        API_URL,
        json={"data": {"issueCreate": {"success": False, "issue": None}}},
        status=200,
    )
    client = LinearClient(api_key="fake-key")

    with pytest.raises(LinearAPIError, match="issue creation failed"):
        client.create_issue("t1", "Do the thing")


@responses.activate
def test_update_issue_state():
    responses.add(
        responses.POST,
        API_URL,
        json={
            "data": {
                "issueUpdate": {
                    "success": True,
                    "issue": {"id": "i1", "identifier": "SIG-1", "state": {"id": "s2", "name": "Done"}},
                }
            }
        },
        status=200,
    )
    client = LinearClient(api_key="fake-key")

    issue = client.update_issue_state("i1", "s2")

    assert issue["state"]["name"] == "Done"


@responses.activate
def test_graphql_errors_in_200_body_raise():
    # GraphQL reports failures inside a 200 response, so a non-error status
    # alone must not be treated as success.
    responses.add(
        responses.POST,
        API_URL,
        json={"errors": [{"message": "Authentication required"}]},
        status=200,
    )
    client = LinearClient(api_key="fake-key")

    with pytest.raises(LinearAPIError, match="GraphQL error"):
        client.get_teams()


@responses.activate
def test_http_error_raises():
    responses.add(responses.POST, API_URL, json={"error": "nope"}, status=401)
    client = LinearClient(api_key="fake-key")

    with pytest.raises(LinearAPIError, match="401"):
        client.get_teams()


@responses.activate
def test_create_issue_sends_parent_id_for_subissues():
    captured = {}

    def callback(request):
        captured.update(json.loads(request.body))
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps({"data": {"issueCreate": {"success": True, "issue": {"id": "i2", "identifier": "SIG-2"}}}}),
        )

    responses.add_callback(responses.POST, API_URL, callback=callback, content_type="application/json")

    client = LinearClient("lin_api_test")
    client.create_issue("team-1", "Child issue", "desc", parent_id="i1")

    assert captured["variables"]["input"]["parentId"] == "i1"


@responses.activate
def test_create_issue_omits_parent_id_when_not_given():
    captured = {}

    def callback(request):
        captured.update(json.loads(request.body))
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps({"data": {"issueCreate": {"success": True, "issue": {"id": "i1"}}}}),
        )

    responses.add_callback(responses.POST, API_URL, callback=callback, content_type="application/json")

    client = LinearClient("lin_api_test")
    client.create_issue("team-1", "Top level issue")

    assert "parentId" not in captured["variables"]["input"]


@responses.activate
def test_update_issue_sends_only_the_given_fields():
    captured = {}

    def callback(request):
        captured.update(json.loads(request.body))
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps({"data": {"issueUpdate": {"success": True, "issue": {"id": "i1"}}}}),
        )

    responses.add_callback(responses.POST, API_URL, callback=callback, content_type="application/json")

    LinearClient("lin_api_test").update_issue("i1", description="nuevo cuerpo")

    assert captured["variables"]["input"] == {"description": "nuevo cuerpo"}


@responses.activate
def test_update_issue_raises_when_not_successful():
    responses.add(
        responses.POST, API_URL,
        json={"data": {"issueUpdate": {"success": False, "issue": None}}}, status=200,
    )

    with pytest.raises(LinearAPIError, match="issue update failed"):
        LinearClient("lin_api_test").update_issue("i1", title="x")


def test_update_issue_requires_at_least_one_field():
    with pytest.raises(ValueError, match="at least one field"):
        LinearClient("lin_api_test").update_issue("i1")


# ---------------------------------------------------------------------------
# Pagination — a single request returns at most PAGE_SIZE issues, so a team
# larger than one page was silently truncated before this.
# ---------------------------------------------------------------------------


def _issue_page(nodes, has_next, cursor):
    return {
        "data": {
            "team": {
                "issues": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                }
            }
        }
    }


@responses.activate
def test_get_issues_follows_every_page():
    pages = [
        _issue_page([{"id": f"i{n}"} for n in range(250)], True, "cur1"),
        _issue_page([{"id": f"j{n}"} for n in range(250)], True, "cur2"),
        _issue_page([{"id": "k0"}], False, None),
    ]
    seen_cursors = []

    def callback(request):
        body = json.loads(request.body)
        seen_cursors.append(body["variables"]["after"])
        return (200, {"Content-Type": "application/json"}, json.dumps(pages[len(seen_cursors) - 1]))

    responses.add_callback(responses.POST, API_URL, callback=callback, content_type="application/json")

    issues = LinearClient("lin_api_test").get_issues("team-1")

    assert len(issues) == 501
    # first request has no cursor, then it follows endCursor each time
    assert seen_cursors == [None, "cur1", "cur2"]


@responses.activate
def test_get_issues_stops_at_the_limit():
    def callback(request):
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps(_issue_page([{"id": f"i{n}"} for n in range(250)], True, "cur1")),
        )

    responses.add_callback(responses.POST, API_URL, callback=callback, content_type="application/json")

    issues = LinearClient("lin_api_test").get_issues("team-1", limit=10)

    assert len(issues) == 10
    assert len(responses.calls) == 1


@responses.activate
def test_get_issues_single_page_makes_one_request():
    responses.add(responses.POST, API_URL, json=_issue_page([{"id": "i1"}], False, None), status=200)

    issues = LinearClient("lin_api_test").get_issues("team-1")

    assert len(issues) == 1
    assert len(responses.calls) == 1
