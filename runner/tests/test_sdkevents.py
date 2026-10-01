from claude_agent_sdk import (
    AssistantMessage,
    ConversationResetMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    UserMessage,
)

from ctrunner.sdkevents import normalize, session_id_of


def result(is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s1",
    )


def test_assistant() -> None:
    m = AssistantMessage(content=[TextBlock(text="hi")], model="m", parent_tool_use_id="tu1")
    assert normalize(m) == {
        "type": "assistant",
        "parent_tool_use_id": "tu1",
        "message": {"model": "m", "content": [{"text": "hi"}]},
    }


def test_user() -> None:
    m = UserMessage(content="go")
    expected = {"type": "user", "parent_tool_use_id": None, "message": {"content": "go"}}
    assert normalize(m) == expected


def test_system_passes_data() -> None:
    data: dict[str, object] = {"type": "system", "subtype": "task_started", "task_id": "a"}
    m = SystemMessage(subtype="task_started", data=data)
    assert normalize(m) == m.data


def test_result() -> None:
    n = normalize(result())
    assert n is not None
    assert n["type"] == "result"
    assert n["session_id"] == "s1"


def test_conversation_reset() -> None:
    m = ConversationResetMessage(new_conversation_id="c2", uuid="u", session_id="s1")
    n = normalize(m)
    assert n is not None
    assert n["type"] == "conversation_reset"


def test_stream_event_is_not_normalized() -> None:
    assert normalize(StreamEvent(uuid="u", session_id="s1", event={})) is None


def test_session_id() -> None:
    assert session_id_of(result()) == "s1"
    assert session_id_of(UserMessage(content="go")) is None
