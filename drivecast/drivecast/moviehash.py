"""OSDb "moviehash" — OpenSubtitles' content hash, used to look up subtitles
by exact byte-content instead of a parsed title (see the moviehash-first step
in ``subtitles._from_opensubtitles``).

Algorithm: file size + the 64-bit little-endian sum of every 8-byte chunk in
the file's first 64 KiB and last 64 KiB, wrapped modulo 2**64, printed as 16
lowercase hex digits. The hash is deliberately blind to everything between
those two windows, so a re-mux or a different release of the same rip often
still matches.

Verified against OpenSubtitles' own published reference vector
(breakdance.avi, 12,909,756 bytes -> "8e245d9679d31e12", documented at
https://opensubtitles.github.io/oshash/) in test_moviehash.py — the exact
head/tail 64 KiB windows of that file are vendored as binary fixtures there
rather than re-derived, so the test proves this module against real bytes,
not just its own arithmetic.

Pure math lives here with no I/O. ``osdb_hash_from_drive`` is the only piece
that touches the network, and it does so with two ranged reads
(``drive_api.fetch_range``) rather than downloading the whole file — the
entire point of a "first/last 64 KiB" algorithm is to avoid that.
"""
import struct

CHUNK = 65536  # 64 KiB — the head/tail window size the algorithm hashes.
_MASK64 = 0xFFFFFFFFFFFFFFFF


def _sum_le_uint64(buf):
    """Sum of every complete little-endian uint64 chunk in buf (unmasked).

    A trailing partial chunk (fewer than 8 bytes left) is dropped rather than
    padded -- it never occurs for a real 64 KiB window (65536 is a multiple
    of 8), and only shows up for a deliberately short synthetic test buffer,
    where dropping matches what the reference C implementation's fixed-size
    reads would have done too.
    """
    n = len(buf) // 8
    return sum(struct.unpack_from("<Q", buf, i * 8)[0] for i in range(n))


def osdb_hash_from_parts(size, head_64k, tail_64k):
    """16 lowercase hex chars. Pure -- no I/O, no validation of buffer length.

    The official reference implementation refuses files under 131072 bytes
    outright (it needs two non-overlapping 64 KiB windows and there simply
    isn't room). This function has no opinion on that: it just sums whatever
    two buffers it's handed plus size, mod 2**64. The "files under 131072
    bytes reuse the whole file for both halves" convention lives one layer up,
    in ``osdb_hash_from_drive`` -- callers who want a hash for a small file
    pass the same whole-file buffer as both head_64k and tail_64k, which
    naturally produces a hash instead of raising the reference impl's error.
    """
    total = size + _sum_le_uint64(head_64k) + _sum_le_uint64(tail_64k)
    return "%016x" % (total & _MASK64)


async def osdb_hash_from_drive(drive_api, file_id, size):
    """Compute the OSDb hash for a Drive file via ranged reads. None on
    any fetch failure -- moviehash is one optional signal in a fallback
    chain, never worth blocking or retrying hard for.

    Two ``fetch_range`` calls for a file at or above the 128 KiB the real
    algorithm needs (head 0-65535, tail size-65536..size-1); exactly one,
    reused for both halves, below that threshold -- see
    ``osdb_hash_from_parts`` for why that's a safe substitution.
    """
    if not size or size <= 0:
        return None
    if size < 2 * CHUNK:
        whole = await drive_api.fetch_range(file_id, 0, size - 1)
        if whole is None:
            return None
        return osdb_hash_from_parts(size, whole, whole)
    head = await drive_api.fetch_range(file_id, 0, CHUNK - 1)
    tail = await drive_api.fetch_range(file_id, size - CHUNK, size - 1)
    if head is None or tail is None:
        return None
    return osdb_hash_from_parts(size, head, tail)
