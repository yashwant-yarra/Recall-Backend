from recall.ask import source_label

def test_doc_label():
    assert source_label({"kind": "doc", "page": 2}) == "page 2"

def test_voice_note_label():
    assert source_label({"kind": "audio", "page": None}) == "voice note"