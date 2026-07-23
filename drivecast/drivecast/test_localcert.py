"""Tests for localcert: real /usr/bin/openssl cert generation into a temp dir.

These shell out to the system LibreSSL (local, fast, no network — consistent
with the repo's "no network in tests" rule). If openssl is somehow absent the
generation tests are skipped; the failure-mode test runs regardless.
"""
import os
import subprocess

import pytest

from drivecast import localcert

_HAS_OPENSSL = os.path.exists(localcert.OPENSSL)
requires_openssl = pytest.mark.skipif(not _HAS_OPENSSL, reason="no /usr/bin/openssl")


@pytest.fixture
def certs_dir(tmp_path, monkeypatch):
    """Redirect every cert path into a temp dir so the user's real certs are
    never touched."""
    d = tmp_path / "certs"
    monkeypatch.setattr(localcert, "CERTS_DIR", str(d))
    monkeypatch.setattr(localcert, "CA_KEY", str(d / "ca.key"))
    monkeypatch.setattr(localcert, "CA_PEM", str(d / "ca.pem"))
    monkeypatch.setattr(localcert, "LEAF_KEY", str(d / "leaf.key"))
    monkeypatch.setattr(localcert, "LEAF_PEM", str(d / "leaf.pem"))
    return d


@requires_openssl
def test_ensure_certs_generates_valid_chain(certs_dir):
    res = localcert.ensure_certs(lan_ip="192.168.1.50")
    assert res == (localcert.LEAF_PEM, localcert.LEAF_KEY)
    for p in (localcert.CA_KEY, localcert.CA_PEM,
              localcert.LEAF_KEY, localcert.LEAF_PEM):
        assert os.path.exists(p)
    # Private keys are owner-only.
    assert (os.stat(localcert.CA_KEY).st_mode & 0o777) == 0o600
    assert (os.stat(localcert.LEAF_KEY).st_mode & 0o777) == 0o600
    # The leaf verifies against the CA.
    proc = subprocess.run(
        [localcert.OPENSSL, "verify", "-CAfile", localcert.CA_PEM, localcert.LEAF_PEM],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    # All four SANs present (LibreSSL prints IP SANs as "IP Address:").
    text = subprocess.run(
        [localcert.OPENSSL, "x509", "-in", localcert.LEAF_PEM, "-noout", "-text"],
        capture_output=True, text=True).stdout
    assert "DNS:localhost" in text
    assert "IP Address:127.0.0.1" in text
    assert "IP Address:192.168.1.50" in text
    assert localcert._mdns_name() in text


@requires_openssl
def test_second_call_same_ip_does_not_regenerate(certs_dir):
    localcert.ensure_certs(lan_ip="192.168.1.50")
    leaf_before = open(localcert.LEAF_PEM, "rb").read()
    localcert.ensure_certs(lan_ip="192.168.1.50")
    assert open(localcert.LEAF_PEM, "rb").read() == leaf_before


@requires_openssl
def test_new_ip_rotates_leaf_but_ca_is_stable(certs_dir):
    localcert.ensure_certs(lan_ip="192.168.1.50")
    ca_before = open(localcert.CA_PEM, "rb").read()
    leaf_before = open(localcert.LEAF_PEM, "rb").read()
    localcert.ensure_certs(lan_ip="10.0.0.99")
    # The leaf is regenerated to cover the new IP...
    assert open(localcert.LEAF_PEM, "rb").read() != leaf_before
    text = subprocess.run(
        [localcert.OPENSSL, "x509", "-in", localcert.LEAF_PEM, "-noout", "-text"],
        capture_output=True, text=True).stdout
    assert "IP Address:10.0.0.99" in text
    # ...but the CA (the thing devices trust) is byte-for-byte unchanged.
    assert open(localcert.CA_PEM, "rb").read() == ca_before


@requires_openssl
def test_prefix_ip_rotates_leaf(certs_dir):
    # Regression: a new IP that is a string-prefix of the cert's existing IP
    # (192.168.1.5 vs 192.168.1.50) must still rotate the leaf. A substring
    # SAN check would falsely treat .5 as already covered by .50.
    localcert.ensure_certs(lan_ip="192.168.1.50")
    leaf_before = open(localcert.LEAF_PEM, "rb").read()
    assert localcert.ensure_certs(lan_ip="192.168.1.5") is not None
    assert open(localcert.LEAF_PEM, "rb").read() != leaf_before
    text = subprocess.run(
        [localcert.OPENSSL, "x509", "-in", localcert.LEAF_PEM, "-noout", "-text"],
        capture_output=True, text=True).stdout
    assert "IP Address:192.168.1.5\n" in text or "IP Address:192.168.1.5," in text
    # leaf_covers is exact: .5 is covered now, .50 no longer is.
    assert localcert.leaf_covers("192.168.1.5") is True
    assert localcert.leaf_covers("192.168.1.50") is False


@requires_openssl
def test_leaf_covers_reflects_startup_leaf(certs_dir):
    # leaf_covers is SAN-only (ignores the renewal window) and reads the on-disk
    # leaf, which is exactly what the live listener bound at start.
    localcert.ensure_certs(lan_ip="192.168.1.50")
    assert localcert.leaf_covers("192.168.1.50") is True
    assert localcert.leaf_covers("10.0.1.7") is False


@requires_openssl
def test_ca_fingerprint_format(certs_dir):
    localcert.ensure_certs(lan_ip="192.168.1.50")
    fp = localcert.ca_fingerprint()
    assert fp and ":" in fp and "Fingerprint" not in fp
    # Colon-separated hex bytes (SHA-256 => 32 bytes).
    parts = fp.split(":")
    assert len(parts) == 32
    assert all(len(p) == 2 for p in parts)


@requires_openssl
def test_leaf_ok_false_when_expiring_soon(certs_dir):
    # Build a CA, then a leaf that expires in 5 days — inside the 30-day window.
    os.makedirs(str(certs_dir), mode=0o700, exist_ok=True)
    assert localcert._ensure_ca()
    required = localcert._required_sans("192.168.1.50")
    ext = certs_dir / "leaf.ext"
    csr = certs_dir / "leaf.csr"
    ext.write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=%s\n" % ",".join(required))
    subprocess.run([localcert.OPENSSL, "req", "-new", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", localcert.LEAF_KEY, "-out", str(csr), "-subj", "/CN=drivecast"],
                   capture_output=True)
    subprocess.run([localcert.OPENSSL, "x509", "-req", "-in", str(csr),
                    "-CA", localcert.CA_PEM, "-CAkey", localcert.CA_KEY, "-CAcreateserial",
                    "-days", "5", "-sha256", "-extfile", str(ext), "-out", localcert.LEAF_PEM],
                   capture_output=True)
    assert localcert._leaf_ok(required) is False


def test_missing_openssl_returns_none(certs_dir, monkeypatch):
    monkeypatch.setattr(localcert, "OPENSSL", "/nonexistent/openssl")
    assert localcert.ensure_certs(lan_ip="192.168.1.50") is None


def test_ca_pem_bytes_none_when_absent(certs_dir):
    assert localcert.ca_pem_bytes() is None
