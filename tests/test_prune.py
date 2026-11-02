from datetime import datetime, timedelta, timezone

from recall.prune import deletion_date, files_to_delete

MB = 1024 * 1024
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def f(name: str, mb: int, days_ago: int):
    return (name, mb * MB, NOW - timedelta(days=days_ago))


def test_keeps_the_newest_files_that_fit():
    files = [f("old", 10, 30), f("new", 10, 1), f("mid", 10, 10)]
    assert files_to_delete(files, max_files=2, max_bytes=100 * MB) == ["old"]  # only the count is over
    assert files_to_delete(files, max_files=5, max_bytes=20 * MB) == ["old"]  # only the size is over
    assert files_to_delete(files, max_files=5, max_bytes=100 * MB) == []  # within both: nothing goes


def test_a_big_new_file_is_skipped_but_smaller_older_ones_still_fit():
    # Newest first: the 60 MB file can't fit in 50 MB, so it goes; the older small ones stay.
    files = [f("big-new", 60, 1), f("a", 10, 5), f("b", 10, 6)]
    assert files_to_delete(files, max_files=5, max_bytes=50 * MB) == ["big-new"]


def test_deletion_date_is_90_days_after_access_ended():
    paid, trial = NOW - timedelta(days=10), NOW - timedelta(days=40)
    # The later of the pass and the trial counts: access ended when the last of them ended.
    assert deletion_date(paid, trial, grace_days=90) == paid + timedelta(days=90)
    assert deletion_date(None, trial, grace_days=90) == trial + timedelta(days=90)
    assert deletion_date(None, None, grace_days=90) is None  # never paid or trialled: never over a paid limit


def test_storage_state():
    from recall.prune import storage_state
    ended = NOW - timedelta(days=5)
    over = storage_state(on_free=True, used=60 * MB, files=3, paid_until=ended, trial_ends_at=None,
                         max_files=5, max_bytes=50 * MB, grace_days=90)
    assert over == {"used_bytes": 60 * MB, "files": 3, "over_free_limit": True,
                    "delete_after": (ended + timedelta(days=90)).isoformat()}
    # Too many files counts as over too.
    assert storage_state(on_free=True, used=MB, files=6, paid_until=ended, trial_ends_at=None,
                         max_files=5, max_bytes=50 * MB, grace_days=90)["over_free_limit"]
    # Within the limits, or still on a pass or trial: nothing is scheduled.
    within = storage_state(on_free=True, used=MB, files=2, paid_until=ended, trial_ends_at=None,
                           max_files=5, max_bytes=50 * MB, grace_days=90)
    assert not within["over_free_limit"] and within["delete_after"] is None
    paying = storage_state(on_free=False, used=900 * MB, files=40, paid_until=NOW + timedelta(days=9),
                           trial_ends_at=None, max_files=5, max_bytes=50 * MB, grace_days=90)
    assert not paying["over_free_limit"] and paying["delete_after"] is None
