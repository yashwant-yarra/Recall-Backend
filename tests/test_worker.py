from uuid import UUID, uuid4
from recall.worker import file_id_from_object_name, unclaimed_response

def test_doc_path():
    u = uuid4()
    assert file_id_from_object_name(f"users/user1/docs/{u}.txt") == u
def test_audio_path():
    u = uuid4()
    assert file_id_from_object_name(f"users/user1/audio/{u}.mp3") == u
def test_unknown_folder_ignored():
    # Only docs/ and audio/ are processed; any other folder must still be ignored.
    u = uuid4()
    assert file_id_from_object_name(f"users/user1/images/{u}.png") is None
def test_wrong_shape_ignored():
    assert file_id_from_object_name("derived/abc/text.txt") is None
def test_bad_uuid_ignored():
    assert file_id_from_object_name("users/user1/docs/not-a-uuid.txt") is None

# A live claim must not be acknowledged: a 2xx would stop Eventarc redelivering,
# and a crashed worker's file would stay 'processing' forever.
def test_live_claim_asks_for_retry():
    code, body = unclaimed_response("processing")
    assert code >= 300 and body == {"status": "in_progress"}
def test_finished_file_acknowledged():
    for status in ("ready", "failed"):
        assert unclaimed_response(status) == (200, {"status": "skipped"})
def test_unknown_file_acknowledged():
    assert unclaimed_response(None) == (200, {"status": "skipped"})
