"""Opt-in read-only checks of public RSS and GitHub Releases endpoints."""
import os

import pytest

from signal_radar.core import HttpClient, RadarStore

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")]


def test_public_rss_populates_temporary_database(tmp_path):
    store = RadarStore(str(tmp_path / "live-rss.sqlite3"))
    store.add_source("Hacker News front page", "rss", {"url": "https://hnrss.org/frontpage"})
    result = store.collect(HttpClient(timeout=15, retries=0))
    assert result["failed"] == []
    assert result["inserted"] > 0
    assert store.items()[0]["canonical_url"].startswith("http")


def test_public_github_releases_populate_temporary_database(tmp_path):
    store = RadarStore(str(tmp_path / "live-github.sqlite3"))
    store.add_source("Click releases", "github", {"repo": "pallets/click"})
    result = store.collect(HttpClient(timeout=15, retries=0))
    assert result["failed"] == []
    assert result["inserted"] > 0
    assert "github.com/pallets/click/releases/" in store.items()[0]["canonical_url"]
