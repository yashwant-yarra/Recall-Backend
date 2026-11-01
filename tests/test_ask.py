from recall.ask import source_label


def test_doc_label():
    assert source_label({"kind": "doc", "page": 2}) == "page 2"


def test_voice_note_label():
    assert source_label({"kind": "audio", "page": None}) == "voice note"


def test_label_includes_document_name():
    assert source_label({"kind": "doc", "page": 3, "name": "Physics Ch 3.pdf"}) == "Physics Ch 3.pdf, page 3"
