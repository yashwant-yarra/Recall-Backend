from recall.tools import DECLARATIONS, TOOLS


# Every tool Gemini is told about must exist in TOOLS, and vice versa; a typo in either list
# would only show up when the model calls that tool.
def test_declarations_match_tools():
    assert {d.name for d in DECLARATIONS} == set(TOOLS)


# Only these two change data; anything else marked as a write tool (or missing here) is a bug.
def test_write_tools_need_approval():
    assert {name for name, (_, needs_approval) in TOOLS.items() if needs_approval} == {"create_task", "save_draft"}


# The server supplies user_id; the model must never be offered it as an argument.
def test_user_id_is_never_a_parameter():
    for d in DECLARATIONS:
        assert "user_id" not in d.parameters_json_schema.get("properties", {})
