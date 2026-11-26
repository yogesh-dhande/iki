"""``setup_storage``: the bucket's CORS and lifecycle rules, and its versioning, checked."""

from io import StringIO
from typing import Any

import pytest
from conftest import S3_BUCKET
from django.core.management import CommandError, call_command

from iki_server.files.management.commands import setup_storage


@pytest.fixture
def bucket(s3: Any, settings: Any) -> Any:
    settings.IKI_PUBLIC_URL = "https://iki.example.com"
    settings.IKI_WEB_DEV_SERVER = False
    for clear in (s3.delete_bucket_cors, s3.delete_bucket_lifecycle):
        clear(Bucket=S3_BUCKET)
    return s3


def run(*args: str) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    call_command("setup_storage", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def test_it_sets_browser_uploads_and_cleanup_rules_and_keeps_others(bucket: Any) -> None:
    bucket.put_bucket_versioning(Bucket=S3_BUCKET, VersioningConfiguration={"Status": "Enabled"})
    theirs = {
        "ID": "analytics",
        "AllowedOrigins": ["https://bi.example.com"],
        "AllowedMethods": ["GET"],
    }
    bucket.put_bucket_cors(Bucket=S3_BUCKET, CORSConfiguration={"CORSRules": [theirs]})
    missing = "missing: CORS for browsers' uploads and reads; lifecycle rules"
    with pytest.raises(CommandError, match=missing):
        run("--check")
    out, _ = run()
    assert "CORS for browsers' uploads and reads, lifecycle rules for abandoned uploads" in out
    rules = {r.get("ID"): r for r in bucket.get_bucket_cors(Bucket=S3_BUCKET)["CORSRules"]}
    assert set(rules) == {"analytics", "iki-browser-uploads"}
    assert rules["iki-browser-uploads"]["AllowedOrigins"] == ["https://iki.example.com"]
    assert rules["iki-browser-uploads"]["AllowedMethods"] == ["GET", "PUT"]
    # Previews read large files in ranges, and check which bytes came back (ADR 0227).
    assert sorted(rules["iki-browser-uploads"]["AllowedHeaders"]) == ["content-type", "range"]
    assert rules["iki-browser-uploads"]["ExposeHeaders"] == ["content-range"]
    (rule,) = bucket.get_bucket_lifecycle_configuration(Bucket=S3_BUCKET)["Rules"]
    assert rule["Filter"]["Prefix"].endswith("/uploads/")
    assert rule["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 2}
    assert run("--check")[0].startswith(f"Bucket {S3_BUCKET} is ready")
    assert run()[0].startswith(f"Bucket {S3_BUCKET} is ready")


def test_versioning_is_reported_not_changed(bucket: Any) -> None:
    bucket.put_bucket_versioning(Bucket=S3_BUCKET, VersioningConfiguration={"Status": "Suspended"})
    _, err = run()
    assert "doesn't keep versions" in err
    assert bucket.get_bucket_versioning(Bucket=S3_BUCKET)["Status"] == "Suspended"
    with pytest.raises(CommandError, match="versioning"):
        run("--check")
    out, err = run("--enable-versioning")
    assert "Turned on versioning" in out and not err
    assert bucket.get_bucket_versioning(Bucket=S3_BUCKET)["Status"] == "Enabled"


def test_the_web_app_dev_server_may_upload_too(settings: Any) -> None:
    settings.IKI_PUBLIC_URL = "http://localhost:8000/"
    settings.IKI_WEB_DEV_SERVER = True
    assert setup_storage.origins() == ["http://localhost:8000", "http://localhost:5173"]


def test_it_needs_a_bucket_and_a_public_url(settings: Any) -> None:
    settings.IKI_FILES_BUCKET = ""
    with pytest.raises(CommandError, match="IKI_FILES_BUCKET"):
        run()


def test_it_lets_browsers_read_where_it_let_them_only_upload(bucket: Any) -> None:
    """Buckets set up before the structure viewer read files: their rule only allowed PUT."""
    bucket.put_bucket_versioning(Bucket=S3_BUCKET, VersioningConfiguration={"Status": "Enabled"})
    run()
    old = {**setup_storage.cors_rule(), "AllowedMethods": ["PUT"]}
    bucket.put_bucket_cors(Bucket=S3_BUCKET, CORSConfiguration={"CORSRules": [old]})
    with pytest.raises(CommandError, match=r"missing: CORS for browsers' uploads and reads\.$"):
        run("--check")
    run()
    (rule,) = bucket.get_bucket_cors(Bucket=S3_BUCKET)["CORSRules"]
    assert rule["AllowedMethods"] == ["GET", "PUT"]


def test_it_lets_previews_read_ranges_where_it_let_browsers_read_whole_files(bucket: Any) -> None:
    """Buckets set up before previews read large files in ranges (ADR 0227)."""
    bucket.put_bucket_versioning(Bucket=S3_BUCKET, VersioningConfiguration={"Status": "Enabled"})
    old = {**setup_storage.cors_rule(), "AllowedHeaders": ["content-type"]}
    del old["ExposeHeaders"]
    bucket.put_bucket_cors(Bucket=S3_BUCKET, CORSConfiguration={"CORSRules": [old]})
    run()  # the lifecycle rule, then only CORS is missing
    bucket.put_bucket_cors(Bucket=S3_BUCKET, CORSConfiguration={"CORSRules": [old]})
    with pytest.raises(CommandError, match=r"missing: CORS for browsers' uploads and reads\.$"):
        run("--check")
    run()
    (rule,) = bucket.get_bucket_cors(Bucket=S3_BUCKET)["CORSRules"]
    assert "range" in rule["AllowedHeaders"] and rule["ExposeHeaders"] == ["content-range"]
