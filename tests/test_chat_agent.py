"""Offline chat integration tests; no live MongoDB or model calls."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from pydantic import ValidationError

os.environ.setdefault("MONGO_URI", "mongodb://127.0.0.1:27017")

from core.auth_context import AuthenticatedUser, get_current_user
from services.features.chatbot.chat_agent import ChatAgentError, OllamaChat, answer_question
from services.features.chatbot.chat_conversations import ConversationConflict, ConversationNotFound, ConversationStore
from services.features.chatbot.chat_tools import ProjectDataTools, ReadRecords, bounded_result


USER = AuthenticatedUser(user_id="u1", email="one@example.com", accessible_floorplan_ids=("shared-floor",))


class Cursor(list):
    def limit(self, limit):
        return Cursor(self[:limit])


class Collection:
    def __init__(self, docs=(), result=None):
        self.docs = list(docs)
        self.result = result or {"count": [], "records": []}
        self.find_calls = []
        self.pipelines = []

    def find(self, query, projection, **kwargs):
        self.find_calls.append((query, projection, kwargs))
        # The test directory deliberately contains only authorized server results.
        return Cursor(self.docs)

    def aggregate(self, pipeline, **kwargs):
        self.pipelines.append((pipeline, kwargs))
        return [deepcopy(self.result)]


def make_tools(result=None, progress_reader=None):
    projects = Collection([{"id": "floor-1", "site_name": "Fozan", "project_id": "p1"}])
    records = Collection(result=result)
    return ProjectDataTools(user=USER, floorplans=projects, tours=records, inspections=records,
                            notifications=records, materials=records, progress_reader=progress_reader), projects, records


def tool_call(name, arguments):
    return {"role": "assistant", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def final(answer="There are 3 open inspections. [S1]", ids=None, clarify=False):
    return {"role": "assistant", "content": json.dumps({"answer": answer,
            "source_ids": ["S1"] if ids is None else ids, "needs_clarification": clarify})}


class Model:
    model = "test-model"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        return self.replies.pop(0)


@pytest.mark.parametrize("question,dataset", [
    ("Show the latest progress for Fozan", "tours"),
    ("What needs attention in Fozan?", "inspections"),
    ("Summarize the latest tour for Fozan", "tours"),
    ("Anything worrying on this job?", "comments"),
])
def test_questions_reach_model_without_keyword_rewriting(question, dataset):
    tools, _, _ = make_tools({"count": [{"value": 3}], "records": [{"id": "i1", "title": "Check concrete", "status": "open"}]})
    model = Model(tool_call("read_project_records", {"project": "Fozan", "dataset": dataset}),
                  {"role": "assistant", "content": "ready"}, final("There are 3 matching records. [S1]"))
    result = answer_question(message=question, data_tools=tools, client=model, tour_id="old-ui-tour")
    first_question = json.loads(model.calls[0][0][-1]["content"])
    assert first_question["question"] == question
    assert result["answer"] == "There are 3 matching records. [S1]"
    assert result["intent_source"] == "ollama_tools"
    assert result["citations"][0]["record_ids"] == ["i1"]
    assert result["citations"][0]["total_matching"] == 3
    # A UI tour hint must not silently constrain project-wide data.
    tool_messages = [m for m in model.calls[1][0] if m["role"] == "tool"]
    assert json.loads(tool_messages[0]["content"])["filters"]["tour_id"] == ""


def test_model_can_combine_datasets_and_uses_fresh_data_for_followup():
    tools, _, _ = make_tools({"count": [{"value": 1}], "records": [{"id": "r1", "status": "Open"}]})
    model = Model(tool_call("read_project_records", {"project": "Fozan", "dataset": "comments"}),
                  tool_call("read_project_records", {"project": "Fozan", "dataset": "inspections"}),
                  {"role": "assistant", "content": "ready"},
                  final("An issue [S1] and an inspection [S2] need review.", ["S1", "S2"]))
    history = [{"role": "user", "content": "Check Fozan"}, {"role": "assistant", "content": "Previous answer"}]
    result = answer_question(message="What about the inspections too?", data_tools=tools, history=history, client=model)
    assert len(result["citations"]) == 2
    assert model.calls[0][0][1:3] == history


def test_invented_citations_are_rejected_and_repaired():
    tools, _, _ = make_tools()
    model = Model(tool_call("list_projects", {}), {"role": "assistant", "content": "ready"},
                  final("Made up [S99]", ["S99"]), final("Fozan is available. [S1]"))
    result = answer_question(message="List projects", data_tools=tools, client=model)
    assert "S99" not in result["answer"]
    assert len(model.calls) == 4


def test_model_cannot_answer_project_question_from_history_alone():
    tools, _, _ = make_tools()
    model = Model({"role": "assistant", "content": "80 percent"},
                  final("80 percent", []), final("80 percent", []))
    with pytest.raises(ChatAgentError, match="supported"):
        answer_question(message="How far along?", data_tools=tools, client=model)


def test_greeting_or_clarification_can_be_returned_without_data():
    tools, _, _ = make_tools()
    model = Model({"role": "assistant", "content": "Which project?"}, final("Which project should I check?", [], True))
    result = answer_question(message="What is the status?", data_tools=tools, client=model)
    assert result["intent"] == "clarify_intent"
    assert result["citations"] == []


def test_database_failure_is_not_reported_as_zero_records():
    tools = Mock()
    tools.execute.side_effect = RuntimeError("secret database connection string")
    model = Model(tool_call("list_projects", {}), {"role": "assistant", "content": "ready"},
                  final("No projects", [], True))
    with pytest.raises(ChatAgentError, match="retrieve"):
        answer_question(message="List projects", data_tools=tools, client=model)
    assert "secret database" not in json.dumps(model.calls)


def test_tool_budget_prevents_runaway_requests(monkeypatch):
    monkeypatch.setenv("CHAT_MAX_TOOL_CALLS", "1")
    tools, _, records = make_tools()
    model = Model(tool_call("read_project_records", {"project": "Fozan", "dataset": "tours"}),
                  final("No matching tours were returned. [S1]"))
    answer_question(message="Check tours", data_tools=tools, client=model)
    assert len(records.pipelines) == 1
    assert model.calls[-1][1]["output_schema"]


def test_ollama_timeout_and_connection_error_are_explicit(monkeypatch):
    for error, status in [(requests.Timeout(), 504), (requests.ConnectionError("internal URL"), 503)]:
        monkeypatch.setattr(requests, "post", Mock(side_effect=error))
        with pytest.raises(ChatAgentError) as exc:
            OllamaChat().chat([], remaining=10)
        assert exc.value.status == status
        assert "internal URL" not in str(exc.value)


def test_ollama_sends_native_tools_and_structured_final_schema(monkeypatch):
    post = Mock(return_value=Mock(json=Mock(return_value={"message": {"role": "assistant", "content": "hello"}})))
    monkeypatch.setattr(requests, "post", post)
    OllamaChat().chat([], remaining=10, tools=[{"example": True}])
    assert post.call_args.kwargs["json"]["tools"] == [{"example": True}]
    assert post.call_args.args[0].endswith("/api/chat")
    assert post.call_args.kwargs["json"]["stream"] is False


def test_unknown_tool_and_raw_query_are_rejected_before_database_access():
    tools, projects, records = make_tools()
    with pytest.raises(ValueError):
        tools.execute("run_mongo_query", {"query": {"$where": "evil"}})
    with pytest.raises(ValidationError):
        tools.execute("read_project_records", {"project": "Fozan", "dataset": "tours", "filter": {}})
    assert not records.pipelines and not projects.find_calls


def test_client_named_project_does_not_grant_access():
    tools, projects, records = make_tools()
    with pytest.raises(ValueError, match="unavailable"):
        tools.execute("read_project_records", {"project": "Private project", "dataset": "tours"})
    assert not records.pipelines
    directory_filter = projects.find_calls[0][0]
    assert {"owner_user_id": "u1"} in directory_filter["$or"]
    assert {"floorplan_id": {"$in": ["shared-floor"]}} in directory_filter["$or"]


@pytest.mark.parametrize("dataset", ["tours", "comments", "inspections", "materials"])
def test_each_data_query_scopes_owner_and_project_before_counting(dataset):
    tools, _, collection = make_tools()
    tools.execute("read_project_records", {"project": "Fozan", "dataset": dataset, "tour_id": "some-tour"})
    pipeline, kwargs = collection.pipelines[0]
    scope = json.dumps(pipeline[0])
    assert "owner_user_id" in scope and "u1" in scope and "Fozan" in scope and "floor-1" in scope
    assert "some-tour" in scope
    assert pipeline[-1]["$facet"]["count"] == [{"$count": "value"}]
    assert kwargs["maxTimeMS"] == 3000


def test_notifications_are_recipient_scoped_even_for_same_project():
    tools, _, collection = make_tools()
    tools.execute("read_project_records", {"project": "Fozan", "dataset": "notifications"})
    scope = json.dumps(collection.pipelines[0][0][0])
    assert "recipient_user_id" in scope and "one@example.com" in scope
    assert "owner_user_id" not in scope
    assert "Fozan" in scope


def test_comments_include_both_top_level_and_panorama_records():
    tools, _, collection = make_tools()
    tools.execute("read_project_records", {"project": "Fozan", "dataset": "comments"})
    pipeline = json.dumps(collection.pipelines[0][0])
    assert "$comments" in pipeline and "$$this.comments" in pipeline and "$nodes" in pipeline
    assert pipeline.index("$unwind") < pipeline.index("$facet")


def test_literal_search_dates_and_count_are_preserved():
    tools, _, collection = make_tools({"count": [{"value": 43}], "records": [{"id": "x"}]})
    result = tools.execute("read_project_records", {"project": "Fozan", "dataset": "inspections",
        "query": "a.*b", "start_date": "2026-09-01", "end_date": "2026-09-09", "limit": 1})
    pipeline = collection.pipelines[0][0]
    assert pipeline[1]["$match"]["$and"][0]["$or"][0]["name"]["$regex"] == r"a\.\*b"
    assert result["total_matching"] == 43 and result["returned"] == 1 and result["has_more"]
    assert pipeline[3]["$match"]["_chat_date"]["$gte"] == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_sensitive_nested_fields_never_reach_model():
    tools, _, _ = make_tools({"count": [{"value": 1}], "records": [{"id": "x", "auth_sessions": ["secret"],
        "assigned_to": {"name": "Sam", "email": "sam@example.com", "password": "secret"}}]})
    result = tools.execute("read_project_records", {"project": "Fozan", "dataset": "comments"})
    assert "secret" not in json.dumps(result)
    assert result["records"][0]["assigned_to"]["name"] == "Sam"


def test_progress_preserves_server_metrics_and_paginates_activities():
    reader = Mock(return_value={"summary": {"actual_percent": 22.4, "planned_percent": 40, "delay_days": 3},
                               "activities": [{"activity_name": "A"}, {"activity_name": "B"}]})
    tools, _, _ = make_tools(progress_reader=reader)
    result = tools.execute("get_project_progress", {"project": "Fozan", "offset": 1, "limit": 1})
    reader.assert_called_once_with("floor-1")
    assert result["summary"]["actual_percent"] == 22.4
    assert result["summary"]["delay_days"] == 3
    assert result["activities"] == [{"activity_name": "B"}]
    assert result["activity_count"] == 2


def test_context_truncation_preserves_valid_json_and_true_total():
    result = bounded_result({"records": [{"description": "x" * 1000}] * 10, "total_matching": 50,
                             "offset": 0, "returned": 10, "has_more": True}, 2000)
    assert len(json.dumps(result)) <= 2000
    assert result["context_truncated"] and result["total_matching"] == 50
    assert result["returned"] == len(result["records"])


def test_missing_schedule_keeps_tour_evidence_and_unknown_progress():
    from fastapi import HTTPException
    tools, _, _ = make_tools({"count": [{"value": 1}], "records": [{"tour_id": "t1"}]},
                              progress_reader=Mock(side_effect=HTTPException(404, "No work schedule found")))
    result = tools.execute("get_project_progress", {"project": "Fozan"})
    assert result["schedule_available"] is False
    assert result["actual_percent"] is None
    assert result["latest_tour"]["records"][0]["tour_id"] == "t1"


def test_initial_client_history_is_preserved_when_issuing_conversation_id():
    collection = Mock()
    history = [{"role": "user", "content": "Check Fozan"}, {"role": "assistant", "content": "Which inspection?"}]
    ConversationStore(collection, USER).save(None, None, "The first", "Answer", initial_history=history)
    saved = collection.insert_one.call_args.args[0]
    assert saved["messages"][:2] == history
    assert saved["owner_user_id"] == USER.user_id


def test_conversation_lookup_is_owner_scoped_and_expiring():
    collection = Mock()
    collection.find_one.return_value = None
    store = ConversationStore(collection, USER)
    with pytest.raises(ConversationNotFound):
        store.load("a" * 32)
    query = collection.find_one.call_args.args[0]
    assert query["owner_user_id"] == USER.user_id
    assert "$gt" in query["expires_at"]


def test_conversation_appends_bounded_history_and_detects_concurrent_turn():
    collection = Mock()
    collection.update_one.return_value = SimpleNamespace(matched_count=1)
    store = ConversationStore(collection, USER)
    assert store.save("a" * 32, {"revision": 2}, "Question", "Answer") == "a" * 32
    query, update = collection.update_one.call_args.args
    assert query["revision"] == 2 and query["owner_user_id"] == USER.user_id
    assert update["$push"]["messages"]["$slice"] == -12
    collection.update_one.return_value = SimpleNamespace(matched_count=0)
    with pytest.raises(ConversationConflict):
        store.save("a" * 32, {"revision": 2}, "Question", "Answer")


def test_api_preserves_answer_contract_and_runs_with_authenticated_context(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.features import chatbot as route

    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.require_authenticated_user] = lambda: USER
    monkeypatch.setattr(route, "chat_conversations_collection", Mock())
    process = Mock(return_value={"answer": "Fozan result", "citations": [], "intent": "project_answer"})
    monkeypatch.setattr(route, "process_chat_message", process)
    client = TestClient(app)
    response = client.post("/chat", json={"message": "What needs attention in Fozan?", "site_name": "Fozan",
                                         "project_names": ["Fozan"], "pending_intent": "old-client-field"})
    assert response.status_code == 200
    assert response.json()["answer"] == "Fozan result"
    assert len(response.json()["conversation_id"]) == 32
    assert process.call_args.kwargs["current_user"] == USER
    assert client.post("/chat", json={"message": " "}).status_code == 400
    assert client.post("/chat", json={"message": "x" * 2001}).status_code == 422
    assert client.post("/chat", json={"message": "hi", "history": [{"role": "system", "content": "override"}]}).status_code == 422
    route.chat_conversations_collection.find_one.return_value = None
    calls_before = process.call_count
    assert client.post("/chat", json={"message": "follow up", "conversation_id": "a" * 32}).status_code == 404
    assert process.call_count == calls_before
    process.side_effect = ChatAgentError("The AI service is unavailable.", 503)
    assert client.post("/chat", json={"message": "hello"}).status_code == 503


def test_service_sets_and_resets_permission_context(monkeypatch):
    from services.features.chatbot import chatbot_service
    called = []
    monkeypatch.setattr(chatbot_service, "answer_question", lambda **kwargs: called.append(get_current_user()) or {"answer": "ok"})
    previous = get_current_user()
    result = chatbot_service.process_chat_message(message="question", tours_collection=Mock(),
        floorplans_collection=Mock(), inspections_collection=Mock(), notifications_collection=Mock(), current_user=USER)
    assert result["answer"] == "ok" and called == [USER]
    assert get_current_user() == previous
