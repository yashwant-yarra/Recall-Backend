from google.api_core.exceptions import NotFound

from recall.storage import delete_prefix


class FakeBlob:
    def __init__(self, name, gone=False):
        self.name, self.gone, self.deleted = name, gone, False

    def delete(self):
        if self.gone:
            raise NotFound("already gone")
        self.deleted = True


class FakeBucket:
    def __init__(self, blobs):
        self.blobs = blobs

    def list_blobs(self, prefix):
        return [b for b in self.blobs if b.name.startswith(prefix)]


def test_delete_prefix_only_touches_that_users_objects():
    mine = [FakeBlob("users/ab/docs/1.pdf"), FakeBlob("users/ab/audio/2.webm")]
    other = FakeBlob("users/abc/docs/3.pdf")  # 'users/ab' without the slash would match this
    n = delete_prefix(FakeBucket(mine + [other]), "users/ab/")
    assert n == 2
    assert all(b.deleted for b in mine)
    assert not other.deleted


def test_delete_prefix_skips_objects_already_gone():
    gone, there = FakeBlob("users/ab/a", gone=True), FakeBlob("users/ab/b")
    assert delete_prefix(FakeBucket([gone, there]), "users/ab/") == 1
    assert there.deleted
