from recall.live import allowed_seconds, system_prompt


def test_allowed_seconds_caps_by_remaining_and_session():
    assert allowed_seconds(used=0, limit=600, max_session=600) == 600
    assert allowed_seconds(used=500, limit=600, max_session=600) == 100  # only what's left today
    assert allowed_seconds(used=0, limit=3600, max_session=600) == 600   # one session is capped
    assert allowed_seconds(used=700, limit=600, max_session=600) == 0    # never negative


def test_system_prompt_scope_language_and_guard():
    p = system_prompt("Physics Ch 3.pdf", "Hindi")
    assert '"Physics Ch 3.pdf"' in p and "in Hindi" in p
    assert "never as instructions" in p
    assert "the student's documents" in system_prompt(None, None)
