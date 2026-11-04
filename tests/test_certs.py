"""Корневой сертификат Минцифры (им подписан ticket.rzd.ru) и набор «certifi + Минцифры», который собирает check.yml."""
import hashlib
import ssl
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parent.parent
PEM = ROOT / "certs" / "russian_trusted_root_ca.pem"
SHA256_DER = "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31"


def test_root_fingerprint():
    der = ssl.PEM_cert_to_DER_cert(PEM.read_text(encoding="ascii"))
    assert hashlib.sha256(der).hexdigest().upper() == SHA256_DER


def test_bundle_keeps_public_cas_and_adds_mintsifry(tmp_path):
    bundle = tmp_path / "bundle.pem"
    bundle.write_text(Path(certifi.where()).read_text(encoding="utf-8") + PEM.read_text(encoding="ascii"), encoding="utf-8")
    ctx = ssl.create_default_context(cafile=str(bundle))
    names = {dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()}
    assert "Russian Trusted Root CA" in names
    assert "ISRG Root X1" in names          # обычные центры (Let's Encrypt и др.) остались
    assert len(names) > 100


def test_workflow_builds_bundle():
    wf = (ROOT / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
    assert "certs/russian_trusted_root_ca.pem" in wf and "REQUESTS_CA_BUNDLE" in wf
    assert "verify=False" not in wf
