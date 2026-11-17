"""
Certificati HTTPS locali per iPad Second Screen.

- Crea una piccola autorità di certificazione (CA) personale, valida SOLO per
  indirizzi IP di rete locale (10.x, 172.16-31.x, 192.168.x) grazie ai
  "name constraints": anche se la chiave venisse rubata, non potrebbe essere
  usata per falsificare siti internet.
- Crea il certificato del server per gli IP attuali del PC e lo rigenera da
  solo quando cambiano (es. passando dal Wi-Fi all'hotspot).

La chiave della CA (certs/ca.key) resta sul PC: non condividerla.
"""

import datetime as dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CERT_DIR = Path(__file__).parent / "certs"
CA_KEY = CERT_DIR / "ca.key"
CA_CRT = CERT_DIR / "ca.crt"
SRV_KEY = CERT_DIR / "server.key"
SRV_CRT = CERT_DIR / "server.crt"

PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
]


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _write_key(path, key):
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))


def _load_key(path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def ensure_ca():
    CERT_DIR.mkdir(exist_ok=True)
    if CA_KEY.exists() and CA_CRT.exists():
        cert = x509.load_pem_x509_certificate(CA_CRT.read_bytes())
        if cert.not_valid_after_utc > _now() + dt.timedelta(days=30):
            return _load_key(CA_KEY), cert

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "iPad Second Screen - CA locale"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "iPad Second Screen"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(days=1))
        .not_valid_after(_now() + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True,
            content_commitment=False, key_encipherment=False, data_encipherment=False,
            key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        # Limita la CA ai soli IP di rete locale
        .add_extension(x509.NameConstraints(
            permitted_subtrees=[x509.IPAddress(n) for n in PRIVATE_NETS],
            excluded_subtrees=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(CA_KEY, key)
    CA_CRT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    # nuova CA => il vecchio certificato server non è più valido
    SRV_CRT.unlink(missing_ok=True)
    print("[cert] creata nuova CA locale: va (re)installata sull'iPad")
    return key, cert


def ca_mobileconfig():
    """Profilo .mobileconfig (formato Apple) che contiene il certificato della CA."""
    import plistlib
    import uuid

    _, cert = ensure_ca()
    der = cert.public_bytes(serialization.Encoding.DER)
    # UUID stabili derivati dal certificato: reinstallare sostituisce il profilo
    fp = cert.fingerprint(hashes.SHA256()).hex()
    payload_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "ipad-second-screen/cert/" + fp)).upper()
    profile_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "ipad-second-screen/profile")).upper()
    profile = {
        "PayloadContent": [{
            "PayloadCertificateFileName": "iPad-Second-Screen-CA.cer",
            "PayloadContent": der,
            "PayloadDescription": "Certificato per la connessione sicura al PC",
            "PayloadDisplayName": "iPad Second Screen - CA locale",
            "PayloadIdentifier": "local.ipad-second-screen.ca." + payload_uuid,
            "PayloadType": "com.apple.security.root",
            "PayloadUUID": payload_uuid,
            "PayloadVersion": 1,
        }],
        "PayloadDescription": "Permette all'iPad di collegarsi al PC in HTTPS "
                              "(valido solo per indirizzi di rete locale).",
        "PayloadDisplayName": "iPad Second Screen",
        "PayloadIdentifier": "local.ipad-second-screen",
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": profile_uuid,
        "PayloadVersion": 1,
    }
    return plistlib.dumps(profile)


def _server_cert_ok(ips):
    if not (SRV_KEY.exists() and SRV_CRT.exists()):
        return False
    cert = x509.load_pem_x509_certificate(SRV_CRT.read_bytes())
    if cert.not_valid_after_utc < _now() + dt.timedelta(days=7):
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    have = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    return set(ips) <= have


def ensure_server_cert(ips):
    """Restituisce (cert_path, key_path) validi per gli IP indicati."""
    ca_key, ca_cert = ensure_ca()
    ips = sorted({ip for ip in ips if any(ipaddress.ip_address(ip) in n for n in PRIVATE_NETS)}
                 | {"127.0.0.1"})
    if _server_cert_ok(ips):
        return SRV_CRT, SRV_KEY

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ips[0])]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - dt.timedelta(days=1))
        # iOS rifiuta certificati server con validità oltre ~398 giorni
        .not_valid_after(_now() + dt.timedelta(days=397))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=False, key_cert_sign=False,
            crl_sign=False, content_commitment=False, data_encipherment=False,
            key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                       critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(SRV_KEY, key)
    SRV_CRT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"[cert] certificato server creato per: {', '.join(ips)}")
    return SRV_CRT, SRV_KEY
