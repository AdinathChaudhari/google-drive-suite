"""Trusted LAN HTTPS: generate a local CA + leaf cert so iPhones/iPads can reach
the plain-Wi-Fi URL over HTTPS (Safari's HTTPS-Only behavior rejects http://).

We shell out to the system LibreSSL (/usr/bin/openssl) rather than pull in a
C-extension crypto dependency — it's a once-every-few-years operation and matches
the codebase's "shell out to a system binary, silent-fail" pattern (see
server.py:_tailscale_ip). Everything lives under USER_DIR (never config.json),
and any failure degrades silently to plain HTTP exactly as before.

The CA is generated ONCE and then frozen — the user trusts it on each device a
single time (Settings -> VPN & Device Management -> full-trust toggle), so its
stability is the whole point. Only the leaf rotates (new LAN IP via DHCP, or
approaching expiry); a rotated leaf is invisible because the root is unchanged.
"""
import os
import socket
import subprocess

from . import config

OPENSSL = "/usr/bin/openssl"

CERTS_DIR = os.path.join(config.USER_DIR, "certs")
CA_KEY = os.path.join(CERTS_DIR, "ca.key")
CA_PEM = os.path.join(CERTS_DIR, "ca.pem")
LEAF_KEY = os.path.join(CERTS_DIR, "leaf.key")
LEAF_PEM = os.path.join(CERTS_DIR, "leaf.pem")

# Apple's iOS-13+ server-cert rules: RSA-2048+, SHA-256, SAN mandatory,
# serverAuth EKU, and <=825-day validity for the leaf.
_LEAF_DAYS = 820
_CA_DAYS = 3650
_RENEW_SECONDS = 30 * 24 * 3600  # regenerate the leaf if it expires within 30 days


def _run(args):
    """Run an openssl command. Returns True on success, False on any failure
    (missing binary, non-zero exit, timeout). Never raises."""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30.0)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _lan_ip():
    """Best-effort LAN IP via the UDP-connect trick. Failure-silent (None).

    A private copy of server.py:_lan_ip — this module must NOT import
    drivecast.server (server imports localcert).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _mdns_name():
    """The Bonjour name of this Mac, e.g. "adinaths-mbp.local"."""
    host = socket.gethostname().lower()
    if host.endswith(".local"):
        host = host[: -len(".local")]
    return "%s.local" % host


def _required_sans(lan_ip):
    """Subject Alternative Names the leaf must cover, in openssl-input form."""
    sans = ["DNS:localhost", "DNS:%s" % _mdns_name(), "IP:127.0.0.1"]
    if lan_ip:
        sans.append("IP:%s" % lan_ip)
    return sans


def _ensure_ca():
    """Generate the CA once (never regenerate — device trust depends on it).
    Returns True if the CA exists (already or freshly made)."""
    if os.path.exists(CA_KEY) and os.path.exists(CA_PEM):
        return True
    host = socket.gethostname()
    ok = _run([
        OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-sha256",
        "-days", str(_CA_DAYS), "-nodes",
        "-keyout", CA_KEY, "-out", CA_PEM,
        "-subj", "/CN=drivecast CA (%s)" % host,
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        # SKI is required so strict validators (OpenSSL 3.x, iOS Security
        # framework) can build the chain via the leaf's matching AKI.
        "-addext", "subjectKeyIdentifier=hash",
    ])
    if ok:
        _chmod_key(CA_KEY)
    return ok


def _cert_sans():
    """The set of SANs on the on-disk leaf, normalized to our "IP:"/"DNS:"
    input form, or None if the cert can't be read. LibreSSL prints the SANs on
    the line after the "Subject Alternative Name" header as a comma-separated
    list (IP SANs as "IP Address:1.2.3.4")."""
    if not (os.path.exists(LEAF_KEY) and os.path.exists(LEAF_PEM)):
        return None
    try:
        proc = subprocess.run([OPENSSL, "x509", "-in", LEAF_PEM, "-noout", "-text"],
                              capture_output=True, text=True, timeout=30.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or "").splitlines()
    for i, line in enumerate(lines):
        if "Subject Alternative Name" in line and i + 1 < len(lines):
            raw = lines[i + 1].replace("IP Address:", "IP:")
            # Exact, comma-separated tokens — never a substring match, or a
            # required SAN that is a prefix of an existing one (e.g. 192.168.1.5
            # vs 192.168.1.50) would falsely pass and the leaf never rotate.
            return {tok.strip() for tok in raw.split(",") if tok.strip()}
    return set()


def _leaf_covers_sans(required_sans):
    """True if the on-disk leaf's SANs cover every required SAN (exact match,
    ignoring expiry)."""
    have = _cert_sans()
    if have is None:
        return False
    return all(san in have for san in required_sans)


def _leaf_ok(required_sans):
    """True if the existing leaf covers every required SAN and isn't near expiry."""
    if not _leaf_covers_sans(required_sans):
        return False
    # -checkend N: non-zero exit if the cert expires within N seconds.
    return _run([OPENSSL, "x509", "-in", LEAF_PEM, "-noout",
                 "-checkend", str(_RENEW_SECONDS)])


def _make_leaf(required_sans):
    """Create a fresh leaf key + cert signed by the CA. Returns True on success."""
    ext_path = os.path.join(CERTS_DIR, "leaf.ext")
    csr_path = os.path.join(CERTS_DIR, "leaf.csr")
    ext = (
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        # AKI + SKI: strict validators (OpenSSL 3.x, iOS) reject a leaf whose
        # AKI doesn't tie back to the CA's SKI.
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid:always\n"
        "subjectAltName=%s\n" % ",".join(required_sans)
    )
    try:
        with open(ext_path, "w") as f:
            f.write(ext)
    except OSError:
        return False
    ok = _run([
        OPENSSL, "req", "-new", "-newkey", "rsa:2048", "-nodes",
        "-keyout", LEAF_KEY, "-out", csr_path, "-subj", "/CN=drivecast",
    ]) and _run([
        OPENSSL, "x509", "-req", "-in", csr_path,
        "-CA", CA_PEM, "-CAkey", CA_KEY, "-CAcreateserial",
        "-days", str(_LEAF_DAYS), "-sha256", "-extfile", ext_path, "-out", LEAF_PEM,
    ])
    if ok:
        _chmod_key(LEAF_KEY)
    return ok


def _chmod_key(path):
    """Lock a private key to owner-only (0600). Best-effort."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def ensure_certs(lan_ip=None):
    """Ensure a CA + a leaf covering localhost, the .local name and the LAN IP.

    Returns (leaf_pem_path, leaf_key_path) on success, or None on any failure
    (missing openssl, subprocess error) — the caller then runs plain HTTP.
    """
    if not os.path.exists(OPENSSL):
        return None
    try:
        os.makedirs(CERTS_DIR, mode=0o700, exist_ok=True)
    except OSError:
        return None
    if lan_ip is None:
        lan_ip = _lan_ip()
    if not _ensure_ca():
        return None
    required = _required_sans(lan_ip)
    if not _leaf_ok(required):
        if not _make_leaf(required):
            return None
    return (LEAF_PEM, LEAF_KEY)


def leaf_covers(lan_ip):
    """True if the on-disk leaf covers this LAN IP (plus localhost/.local),
    SAN-match only — expiry is ignored on purpose.

    The live HTTPS listener loaded the leaf at bind time and nothing rotates it
    mid-run, so the on-disk SANs are exactly what the listener serves. Callers
    use this at request time to decide whether the running listener can actually
    serve `lan_ip` before advertising the HTTPS URL.
    """
    return _leaf_covers_sans(_required_sans(lan_ip))


def ca_pem_bytes():
    """The CA certificate bytes for the /api/remote/ca download, or None."""
    try:
        with open(CA_PEM, "rb") as f:
            return f.read()
    except OSError:
        return None


def ca_fingerprint():
    """The CA's SHA-256 fingerprint (colon-separated hex), or None.

    Shown in the desktop UI and console so the user can compare it against the
    fingerprint iOS displays under "More Details" before installing the profile
    — the check that turns the plain-HTTP CA download into something an on-path
    attacker can't silently substitute.
    """
    if not os.path.exists(CA_PEM):
        return None
    try:
        proc = subprocess.run(
            [OPENSSL, "x509", "-in", CA_PEM, "-noout", "-fingerprint", "-sha256"],
            capture_output=True, text=True, timeout=30.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    # LibreSSL prints "SHA256 Fingerprint=AB:CD:...".
    _, _, rest = (proc.stdout or "").partition("=")
    fp = rest.strip()
    return fp or None
