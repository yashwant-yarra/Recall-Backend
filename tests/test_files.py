from recall.files import clean_display_name


def test_display_name_keeps_only_the_file_name():
    assert clean_display_name("C:\fakepath\Physics Ch 3.pdf") == "Physics Ch 3.pdf"
    assert clean_display_name("folder/notes.txt") == "notes.txt"


def test_display_name_strips_control_characters_and_caps_length():
    assert clean_display_name("bad\x00\nname.pdf") == "badname.pdf"
    assert len(clean_display_name("a" * 500 + ".pdf")) == 200


def test_display_name_empty_is_none():
    for value in (None, "", "   ", "C:\fakepath\\"):
        assert clean_display_name(value) is None


from recall.storage import content_disposition


def test_content_disposition_is_inline_and_encoded():
    assert content_disposition(None) == "inline"
    assert content_disposition("notes.pdf") == "inline; filename*=UTF-8''notes.pdf"
    # quotes, spaces and non-ASCII can't break out of the header value
    assert content_disposition('a "b".pdf') == "inline; filename*=UTF-8''a%20%22b%22.pdf"
    assert content_disposition("résumé.pdf") == "inline; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"
