from google.genai import types

from recall.agent import build_contents


def test_goal_only():
    contents = build_contents("What's new?", [])
    assert len(contents) == 1
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "What's new?"


def test_step_round_trip_keeps_thought_signature():
    call = types.Part(
        function_call=types.FunctionCall(name="list_voice_notes", args={"limit": 1}),
        thought_signature=b"sig",
    ).model_dump(mode="json", exclude_none=True)  # how the call is stored in agent_steps.call
    steps = [{"tool": "list_voice_notes", "call": call, "output": {"voice_notes": []}}]

    contents = build_contents("goal", steps)

    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].thought_signature == b"sig"
    assert contents[2].parts[0].function_response.response == {"voice_notes": []}
