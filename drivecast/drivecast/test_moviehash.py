"""moviehash.py tests -- pure OSDb hashing + the ranged-read wrapper.

Hermetic: the "known vector" test reads two small binary fixtures from disk
(vendored bytes, not a network call) and the "from drive" tests use a fake
drive_api with a fetch_range stub -- no real Drive access, no real httpx.
"""
import asyncio
import os
import struct

from drivecast.moviehash import osdb_hash_from_drive, osdb_hash_from_parts

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "testdata")

# OpenSubtitles' own published OSDb test vector (opensubtitles.github.io/oshash/):
#   breakdance.avi, 12,909,756 bytes -> "8e245d9679d31e12"
# The two fixtures below are the *real* first and last 64 KiB of that exact
# file -- range-fetched (HTTP 206) from
# https://www.opensubtitles.org/addons/avi/breakdance.avi during development
# and confirmed there to hash to the published value, so this test proves
# osdb_hash_from_parts against genuine reference bytes, not just its own
# arithmetic reflected back at itself. Only the head/tail windows are
# vendored (128 KiB total) -- the algorithm never looks at the middle of the
# file, so there is no reason to ship the other ~12 MB.
BREAKDANCE_SIZE = 12909756
BREAKDANCE_HASH = "8e245d9679d31e12"


def _fixture(name):
    with open(os.path.join(_FIXTURE_DIR, name), "rb") as f:
        return f.read()


def test_moviehash_known_vectors():
    head = _fixture("osdb_breakdance_head_64k.bin")
    tail = _fixture("osdb_breakdance_tail_64k.bin")
    assert len(head) == 65536 and len(tail) == 65536
    assert osdb_hash_from_parts(BREAKDANCE_SIZE, head, tail) == BREAKDANCE_HASH

    # Sub-131072-byte special case. No published vector covers this -- the
    # reference implementation just refuses to hash a file this small -- so
    # this half uses a synthetic buffer whose expected hash is computed
    # independently right here (plain Python, not the function under test),
    # per the plan's fallback instruction for an unavailable reference vector.
    size = 40000  # well under 131072; head_64k == tail_64k == whole file
    buf = bytes((i * 7 + 3) % 256 for i in range(size))
    chunk_sum = 0
    for i in range(size // 8):
        chunk_sum += struct.unpack_from("<Q", buf, i * 8)[0]
    # buf is passed as BOTH halves, so its chunk sum is counted twice --
    # exactly what "reuse the whole file for both halves" means arithmetically.
    expected = (size + 2 * chunk_sum) & 0xFFFFFFFFFFFFFFFF
    assert osdb_hash_from_parts(size, buf, buf) == "%016x" % expected


class _FakeDriveAPI:
    """Records every fetch_range call and returns canned bytes."""

    def __init__(self, chunk=b"\x00" * 65536):
        self.calls = []
        self._chunk = chunk

    async def fetch_range(self, file_id, start, end):
        self.calls.append((file_id, start, end))
        return self._chunk[: end - start + 1]


def test_moviehash_from_drive_uses_two_ranged_reads():
    size = 200_000_000  # comfortably above the 128 KiB threshold
    api = _FakeDriveAPI()
    h = asyncio.run(osdb_hash_from_drive(api, "fid", size))
    assert h == osdb_hash_from_parts(size, api._chunk, api._chunk)
    assert api.calls == [
        ("fid", 0, 65535),
        ("fid", size - 65536, size - 1),
    ]


def test_moviehash_from_drive_reuses_one_read_for_small_file():
    size = 50_000  # under 131072
    api = _FakeDriveAPI(chunk=bytes((i * 3) % 256 for i in range(size)))
    h = asyncio.run(osdb_hash_from_drive(api, "fid", size))
    whole = api._chunk[:size]
    assert h == osdb_hash_from_parts(size, whole, whole)
    assert api.calls == [("fid", 0, size - 1)]


def test_moviehash_from_drive_returns_none_on_fetch_failure():
    class _FailingAPI:
        async def fetch_range(self, file_id, start, end):
            return None

    assert asyncio.run(osdb_hash_from_drive(_FailingAPI(), "fid", 999_999_999)) is None


def test_moviehash_from_drive_returns_none_for_unknown_size():
    api = _FakeDriveAPI()
    assert asyncio.run(osdb_hash_from_drive(api, "fid", 0)) is None
    assert asyncio.run(osdb_hash_from_drive(api, "fid", None)) is None
    assert api.calls == []
