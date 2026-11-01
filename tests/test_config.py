import pytest
from pydantic import ValidationError

from recall.config import Settings


def test_bucket_name_with_space_rejected():
    # The real incident: an unquoted comma in PowerShell made this the bucket name on Cloud Run.
    with pytest.raises(ValidationError):
        Settings(gcs_bucket="yash-gc-bucket GOOGLE_CLOUD_PROJECT=genai-511308")


def test_cors_origins_from_comma_list():
    s = Settings(gcs_bucket="yash-gc-bucket", cors_origins="http://localhost:3000, https://recall.vercel.app/")
    assert s.cors_origins == ["http://localhost:3000", "https://recall.vercel.app"]  # trimmed, no trailing slash


def test_valid_bucket_name_accepted():
    assert Settings(gcs_bucket="yash-gc-bucket").gcs_bucket == "yash-gc-bucket"
