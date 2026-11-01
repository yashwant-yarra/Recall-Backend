from recall.quiz import QuizQuestion, keep_valid, public, spread


def q(type_, options=(), answer="A"):
    return QuizQuestion(type=type_, question="?", options=list(options), answer=answer, explanation="e", page=1)


def test_spread_includes_ends_and_keeps_order():
    assert spread(list(range(100)), 5) == [0, 25, 50, 74, 99]
    assert spread([1, 2, 3], 12) == [1, 2, 3]


def test_keep_valid_drops_broken_mcq():
    good, no_answer, one_option = q("mcq", "ABCD"), q("mcq", "BCDE", "A"), q("mcq", "A")
    assert keep_valid([good, no_answer, one_option], "mixed", 5) == [good]


def test_keep_valid_respects_kind_and_count():
    qs = [q("mcq", "ABCD"), q("short"), q("short"), q("short")]
    assert [x.type for x in keep_valid(qs, "short", 2)] == ["short", "short"]


def test_public_hides_answer_and_explanation():
    shown = public(q("mcq", "ABCD").model_dump())
    assert "answer" not in shown and "explanation" not in shown
