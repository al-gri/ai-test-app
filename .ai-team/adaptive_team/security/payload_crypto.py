"""Versioned AES-256-GCM envelope encryption with per-write random DEKs.

The encrypted DEK and ciphertext may be stored together; the wrapping key must
remain external. Associated data authenticates journal/project/operation/field.
An exposed DEK compromises its own plaintext; AAD does not prevent that exposure.
"""
import base64
import json
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from ..models import PolicyError, canonical
from .key_provider import FileKeyProvider


class PayloadIntegrityError(PolicyError):
    pass


class PayloadCipher:
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, provider=None):
        self.provider = provider if provider is not None else FileKeyProvider.from_environment()

    @staticmethod
    def _aad(context):
        return canonical({'format': 'adaptive-envelope-1', **context}).encode()

    def encrypt(self, value, context):
        plain = canonical(value).encode()
        if len(plain) > self.MAX_BYTES:
            raise PolicyError('Journal payload size limit exceeded')
        aad = self._aad(context)
        key_id = self.provider.active_id
        dek, nonce, wrap_nonce = os.urandom(32), os.urandom(12), os.urandom(12)
        ciphertext = AESGCM(dek).encrypt(nonce, plain, aad)
        wrapped = AESGCM(self.provider.get(key_id)).encrypt(wrap_nonce, dek, aad + b'\0wrap\0' + key_id.encode())
        b64 = lambda data: base64.b64encode(data).decode('ascii')
        return canonical({'v': 1, 'key_id': key_id, 'nonce': b64(nonce),
            'wrap_nonce': b64(wrap_nonce), 'wrapped_dek': b64(wrapped), 'ciphertext': b64(ciphertext)})

    def decrypt(self, encoded, context):
        try:
            if not isinstance(encoded, str) or len(encoded) > 2*self.MAX_BYTES:
                raise ValueError()
            value = json.loads(encoded)
            if set(value) != {'v','key_id','nonce','wrap_nonce','wrapped_dek','ciphertext'} or value['v'] != 1:
                raise ValueError()
            un64 = lambda name: base64.b64decode(value[name], validate=True)
            nonce, wn, wrapped = un64('nonce'), un64('wrap_nonce'), un64('wrapped_dek')
            if len(nonce) != 12 or len(wn) != 12 or len(wrapped) != 48:
                raise ValueError()
            aad, kid = self._aad(context), value['key_id']
            dek = AESGCM(self.provider.get(kid)).decrypt(wn, wrapped, aad + b'\0wrap\0' + kid.encode())
            return json.loads(AESGCM(dek).decrypt(nonce, un64('ciphertext'), aad))
        except (InvalidTag, ValueError, TypeError, KeyError, UnicodeError):
            raise PayloadIntegrityError('Encrypted payload authentication failed') from None
