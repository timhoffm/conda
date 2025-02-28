# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
"""
Strongly related to subdir_data / test_subdir_data.
"""

from __future__ import annotations

import datetime
import json
import math
import sys
import time
from pathlib import Path

import pytest

from conda.base.context import conda_tests_ctxt_mgmt_def_pol, context
from conda.common.io import env_vars
from conda.exceptions import (
    CondaDependencyError,
    CondaHTTPError,
    CondaSSLError,
    ProxyError,
    UnavailableInvalidChannel,
)
from conda.gateways.connection import (
    HTTPError,
    InvalidSchema,
    RequestsProxyError,
    SSLError,
)
from conda.gateways.repodata import (
    RepodataCache,
    RepodataIsEmpty,
    RepodataState,
    conda_http_errors,
)


def test_save(tmp_path):
    """
    Check regular cache save, load operations.
    """
    TEST_DATA = "{}"
    cache = RepodataCache(tmp_path / "lockme", "repodata.json")
    cache.save(TEST_DATA)

    assert cache.load() == TEST_DATA

    state = dict(cache.state)

    json_stat = cache.cache_path_json.stat()

    time.sleep(0.1)  # may be necessary on Windows for time.time_ns() to advance

    # update last-checked-timestamp in .state.json
    cache.refresh()

    # repodata.json's mtime should be equal
    json_stat2 = cache.cache_path_json.stat()
    assert json_stat.st_mtime_ns == json_stat2.st_mtime_ns

    state2 = dict(cache.state)

    assert state2 != state

    # force reload repodata, .state.json from disk
    cache.load()
    state3 = dict(cache.state)

    assert state3 == state2


def test_stale(tmp_path):
    """
    RepodataCache should understand cache-control and modified time versus now.
    """
    TEST_DATA = "{}"
    cache = RepodataCache(tmp_path / "cacheme", "repodata.json")
    MOD = "Thu, 26 Jan 2023 19:34:01 GMT"
    cache.state.mod = MOD
    CACHE_CONTROL = "public, max-age=30"
    cache.state.cache_control = CACHE_CONTROL
    ETAG = '"etag"'
    cache.state.etag = ETAG
    cache.save(TEST_DATA)

    cache.load()
    assert not cache.stale()
    assert (
        29 < cache.timeout() < 30.1
    )  # time difference between record and save timestamp

    # backdate
    cache.state["refresh_ns"] = time.time_ns() - (60 * 10**9)  # type: ignore
    cache.cache_path_state.write_text(json.dumps(dict(cache.state)))
    assert cache.load() == TEST_DATA
    assert cache.stale()

    # lesser backdate.
    # excercise stale paths.
    original_ttl = context.local_repodata_ttl
    try:
        cache.state["refresh_ns"] = time.time_ns() - (31 * 10**9)  # type: ignore
        for ttl, expected in ((0, True), (1, True), (60, False)):
            # < 1 means max-age: 0; 1 means use cache header; >1 means use
            # local_repodata_ttl
            context.local_repodata_ttl = ttl  # type: ignore
            assert cache.stale() is expected
            cache.timeout()
    finally:
        context.local_repodata_ttl = original_ttl

    # since state's mtime_ns matches repodata.json stat(), these will be preserved
    assert cache.state.mod == MOD
    assert cache.state.cache_control == CACHE_CONTROL
    assert cache.state.etag == ETAG

    # XXX rewrite state without replacing repodata.json, assert still stale...

    # mismatched mtime empties cache headers
    state = dict(cache.state)
    assert state["etag"]
    assert cache.state.etag
    state["mtime_ns"] = 0
    cache.cache_path_state.write_text(json.dumps(state))
    cache.load_state()
    assert not cache.state.mod
    assert not cache.state.etag

    # check type problems
    json_types = (None, True, False, 0, 0.5, math.nan, {}, "a string")
    for type in json_types:
        cache.state["cache_control"] = type
        cache.stale()

    # if we don't match stat then load_state will clear the test "mod" value
    json_stat = cache.cache_path_json.stat()

    # change wrongly-typed mod to empty string
    cache.cache_path_state.write_text(
        json.dumps(
            {"mod": None, "mtime_ns": json_stat.st_mtime_ns, "size": json_stat.st_size}
        )
    )
    state = cache.load_state()
    assert state.mod == ""

    # preserve correct mod
    cache.cache_path_state.write_text(
        json.dumps(
            {
                "mod": "some",
                "mtime_ns": json_stat.st_mtime_ns,
                "size": json_stat.st_size,
            }
        )
    )
    state = cache.load_state()
    assert state.mod == "some"


def test_coverage_repodata_state(tmp_path):
    # now these should be loaded through RepodataCache instead.

    # assert invalid state is equal to no state
    state = RepodataState(
        tmp_path / "garbage.json", tmp_path / "garbage.state.json", "repodata.json"
    )
    state.cache_path_state.write_text("not json")
    assert dict(state.load()) == {}


from conda.gateways.connection import (
    HTTPError,
    InvalidSchema,
    RequestsProxyError,
    SSLError,
)
from conda.gateways.repodata import RepodataIsEmpty, conda_http_errors


def test_repodata_state_has_format():
    # wrong has_zst format
    state = RepodataState(
        "", "", "", dict={"has_zst": {"last_checked": "Tuesday", "value": 0}}
    )
    value, dt = state.has_format("zst")
    assert value is False
    assert isinstance(dt, datetime.datetime)
    assert not "has_zst" in state

    # no has_zst information
    state = RepodataState("", "", "")
    value, dt = state.has_format("zst")
    assert value is True
    assert dt is None  # is this non-datetime type what we want?

    state.set_has_format("zst", True)
    value, dt = state.has_format("zst")
    assert value is True
    assert isinstance(dt, datetime.datetime)
    assert "has_zst" in state

    state.set_has_format("zst", False)
    value, dt = state.has_format("zst")
    assert value is False
    assert isinstance(dt, datetime.datetime)
    assert "has_zst" in state


def test_coverage_conda_http_errors():
    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    with pytest.raises(ProxyError), conda_http_errors(
        "https://conda.anaconda.org", "repodata.json"
    ):
        raise RequestsProxyError()

    with pytest.raises(CondaDependencyError), conda_http_errors(
        "https://conda.anaconda.org", "repodata.json"
    ):
        raise InvalidSchema("SOCKS")

    with pytest.raises(InvalidSchema), conda_http_errors(
        "https://conda.anaconda.org", "repodata.json"
    ):
        raise InvalidSchema("shoes")  # not a SOCKS problem

    with pytest.raises(CondaSSLError), conda_http_errors(
        "https://conda.anaconda.org", "repodata.json"
    ):
        raise SSLError()

    # strange url-ends-with-noarch-specific behavior
    with pytest.raises(UnavailableInvalidChannel), conda_http_errors(
        "https://conda.anaconda.org/noarch", "repodata.json"
    ):
        raise HTTPError(response=Response(404))

    with pytest.raises(RepodataIsEmpty), env_vars(
        {"CONDA_ALLOW_NON_CHANNEL_URLS": "1"},
        stack_callback=conda_tests_ctxt_mgmt_def_pol,
    ), conda_http_errors("https://conda.anaconda.org/noarch", "repodata.json"):
        raise HTTPError(response=Response(404))

    # A variety of helpful error messages should follow
    with pytest.raises(CondaHTTPError, match="invalid credentials"), conda_http_errors(
        "https://conda.anaconda.org/noarch", "repodata.json"
    ):
        raise HTTPError(response=Response(401))

    # A (random uuid) token should trigger a different message.
    with pytest.raises(CondaHTTPError, match="token"), conda_http_errors(
        "/t/dh-73683400-b3ee-4f87-ade8-37de6d395bdb/conda-forge/noarch", "repodata.json"
    ):
        raise HTTPError(response=Response(401))

    # env_vars plus a harmless option to reset context on exit
    with pytest.raises(CondaHTTPError, match="The credentials"), env_vars(
        {"CONDA_ALLOW_NON_CHANNEL_URLS": "1"},
        stack_callback=conda_tests_ctxt_mgmt_def_pol,
    ), conda_http_errors("https://conda.anaconda.org/noarch", "repodata.json"):
        context.channel_alias.location = "xyzzy"
        raise HTTPError(response=Response(401))

    # was the context reset properly?
    assert context.channel_alias.location != "xyzzy"

    # Oh no
    with pytest.raises(CondaHTTPError, match="A 500-type"), conda_http_errors(
        "https://repo.anaconda.com/main/linux-64", "repodata.json"
    ):
        raise HTTPError(response=Response(500))

    # Ask to unblock URL
    with pytest.raises(CondaHTTPError, match="blocked"), conda_http_errors(
        "https://repo.anaconda.com/main/linux-64", "repodata.json"
    ):
        raise HTTPError(response=Response(418))

    # Just an error
    with pytest.raises(CondaHTTPError, match="An HTTP error"), conda_http_errors(
        "https://example.org/main/linux-64", "repodata.json"
    ):
        raise HTTPError(response=Response(418))

    # Don't know how to configure "context.channel_alias not in url"


def test_ssl_unavailable_error_message():
    try:
        # OpenSSL appears to be unavailable
        with pytest.raises(CondaSSLError, match="unavailable"), conda_http_errors(
            "https://conda.anaconda.org", "repodata.json"
        ):
            sys.modules["ssl"] = None  # type: ignore
            raise SSLError()
    finally:
        del sys.modules["ssl"]


def test_cache_json(tmp_path: Path):
    """
    Load and save standardized field names, from internal matches-legacy
    underscore-prefixed field names. Assert state is only loaded if it matches
    cached json.
    """
    cache_json = tmp_path / "cached.json"
    cache_state = tmp_path / "cached.state.json"

    cache_json.write_text("{}")

    RepodataState(cache_json, cache_state, "repodata.json").save()

    state = RepodataState(cache_json, cache_state, "repodata.json").load()

    mod = "last modified time"

    state = RepodataState(cache_json, cache_state, "repodata.json")
    state.mod = mod  # this is the last-modified header not mtime_ns
    state.cache_control = "cache control"
    state.etag = "etag"
    state.save()

    on_disk_format = json.loads(cache_state.read_text())
    print("disk format", on_disk_format)
    assert on_disk_format["mod"] == mod
    assert on_disk_format["cache_control"]
    assert on_disk_format["etag"]
    assert isinstance(on_disk_format["size"], int)
    assert isinstance(on_disk_format["mtime_ns"], int)

    state2 = RepodataState(cache_json, cache_state, "repodata.json").load()
    assert state2.mod == mod
    assert state2.cache_control
    assert state2.etag

    assert state2["mod"] == state2.mod
    assert state2["etag"] == state2.etag
    assert state2["cache_control"] == state2.cache_control

    cache_json.write_text("{ }")  # now invalid due to size

    state_invalid = RepodataState(cache_json, cache_state, "repodata.json").load()
    assert state_invalid.get("mod") == ""
