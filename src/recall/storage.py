from datetime import timedelta
from urllib.parse import quote

from google.api_core.exceptions import NotFound
from google.auth.transport.requests import Request

from .config import settings


def sign_upload_url(
    bucket, credentials, gcs_path: str, content_type: str, size_bytes: int
) -> tuple[str, dict[str, str]]:
    """Signs via IAM signBlob (email + token) so it works on Cloud Run without a key file."""
    if not credentials.valid:
        credentials.refresh(Request())
    # Exact-size range: GCS rejects any upload whose length differs from what the client declared.
    headers = {
        "x-goog-content-length-range": f"{size_bytes},{size_bytes}",
        "x-goog-if-generation-match": "0",
    }

    url = bucket.blob(gcs_path).generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=settings.upload_url_ttl_seconds),
        method="PUT",
        content_type=content_type,
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
        # The library adds "Host" to the dict it receives, so pass a copy.
        headers=dict(headers),
    )
    return url, {**headers, "Content-Type": content_type}


def content_disposition(filename: str | None) -> str:
    """'inline' (show in the browser, don't download), with the name encoded per RFC 5987 so
    quotes or non-ASCII characters in a user's file name can't break the header."""
    return f"inline; filename*=UTF-8''{quote(filename, safe='')}" if filename else "inline"


def sign_download_url(bucket, credentials, gcs_path: str, content_type: str, filename: str | None) -> str:
    """A short-lived V4 GET URL, so the browser fetches the file straight from Cloud Storage.

    The URL acts with the signing service account's permissions, so recall-api needs read access
    to the bucket. The response headers are set by GCS from the signed parameters.
    """
    if not credentials.valid:
        credentials.refresh(Request())
    return bucket.blob(gcs_path).generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=settings.download_url_ttl_seconds),
        method="GET",
        response_type=content_type,
        response_disposition=content_disposition(filename),
        service_account_email=credentials.service_account_email,
        access_token=credentials.token,
    )


def delete_object(bucket, gcs_path: str) -> None:
    """Delete a stored file. Already gone (never uploaded, or deleted before) is fine."""
    try:
        bucket.blob(gcs_path).delete()
    except NotFound:
        pass


def delete_prefix(bucket, prefix: str) -> int:
    """Delete every object whose path starts with prefix (one at a time, so one failure doesn't stop
    the rest); returns how many were deleted. The prefix must end with '/'."""
    deleted = 0
    for blob in bucket.list_blobs(prefix=prefix):
        try:
            blob.delete()
            deleted += 1
        except NotFound:
            pass
    return deleted
