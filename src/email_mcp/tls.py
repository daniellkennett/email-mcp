"""TLS trust for outbound SMTP.

Python does not read the macOS Keychain. A uv-managed interpreter verifies against the
OpenSSL bundle at /etc/ssl/cert.pem, which holds only public roots. On a machine behind a
TLS-inspecting proxy -- common on corporate laptops -- the proxy re-signs every connection
with a private root CA that the operating system trusts and that bundle does not, so the
handshake fails with "self-signed certificate in certificate chain" even though nothing is
wrong with the mail server.

`truststore` resolves this by verifying against the OS trust store, which already holds
whatever root the machine's management installed. It also stays correct on an unmanaged
machine, where the OS store holds the same public roots as the bundle.

Order of preference: an explicitly configured CA bundle, then the OS trust store.
Verification is never disabled.
"""

import ssl

import truststore

from email_mcp.config import Settings


def tls_context(settings: Settings) -> ssl.SSLContext:
    """Build the SSL context for an SMTP connection."""
    if settings.ca_bundle is not None:
        return ssl.create_default_context(cafile=str(settings.ca_bundle))
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def trust_source(settings: Settings) -> str:
    """Name the trust source in use, for config_status."""
    if settings.ca_bundle is not None:
        return f"ca_bundle:{settings.ca_bundle}"
    return "os_trust_store"
