"""КЕП (qualified electronic signature) driver — токенът като устройство.

Експонира КЕП операциите на хардуерния токен през проксито:
- mutual-TLS заявки към НОИ ЕРБЛ (GetDataForEGN / ePCLoadSaveXML / ePCLKKLoadSaveXML)
- PKCS7 detached подпис (.p7s) за НАП-стил подаване (заменя StampIT LSManager)

Доказан механизъм (spike 2026-06-20): curl + OpenSSL pkcs11 engine + PIN,
mutual-TLS чрез TLS renegotiation (gnutls НЕ става). Виж
project_l10n_bg_api_nssi_erbl.
"""
from .pkcs11_client import KepClient, KepError

__all__ = ["KepClient", "KepError"]
