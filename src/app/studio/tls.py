"""Self-signed HTTPS certificate for the studio's LAN address.

Browsers only allow microphone access on secure pages (https:// or localhost). A certificate
generated here, for this Mac's own host names and addresses, lets the recorder work from other
computers. It is self-signed: the browser shows a one-time warning until the certificate is
trusted on that computer (the studio offers the certificate for download with instructions).
The private key never leaves config/tls (mode 0600).
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
from pathlib import Path

from .util import atomic_write_bytes

VALID_DAYS = 825


def _names(ctx) -> tuple[list[str], list[str]]:
    from .web.security import local_names

    raw = set(local_names()) | {h.lower() for h in ctx.settings().server.allowed_hosts}
    dns, ips = set(), set()
    for n in raw:
        n = n.strip("[]")
        if not n or n in ("0.0.0.0",):
            continue
        try:
            ips.add(str(ipaddress.ip_address(n)))
        except ValueError:
            if "." in n or n == "localhost" or n.replace("-", "").isalnum():
                dns.add(n)
    ips |= {"127.0.0.1"}
    return sorted(dns), sorted(ips)


def tls_dir(ctx) -> Path:
    d = ctx.config_dir / "tls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_certificate(ctx) -> tuple[Path, Path]:
    """Return (cert, key), (re)generating when missing, expiring soon, or missing a current host name/IP."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = tls_dir(ctx)
    cert_p, key_p = d / "studio-certificate.pem", d / "studio-key.pem"
    dns, ips = _names(ctx)
    if cert_p.exists() and key_p.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have_dns = set(san.get_values_for_type(x509.DNSName))
            have_ip = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
            remaining = cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
            if set(dns) <= have_dns and set(ips) <= have_ip and remaining > dt.timedelta(days=30):
                return cert_p, key_p
        except Exception:
            pass
    key = ec.generate_private_key(ec.SECP256R1())
    host = dns[0] if dns else "audiobook-studio"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Audiobook Studio (self-signed)")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False, data_encipherment=False, key_agreement=True,
                                         key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    atomic_write_bytes(key_p, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    try:
        os.chmod(key_p, 0o600)
    except OSError:
        pass
    atomic_write_bytes(cert_p, cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p


def fingerprint_sha256(cert_path: Path) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    h = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))
