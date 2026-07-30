# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""Europlacer raw-material OUT producer driver.

The FIRST *producer* driver in the proxy: unlike every existing driver
(readers / scales / cameras / cfx / gps / access / biometric — all
passive consumers/ingest), this one is kicked off synchronously from an
HTTP route, WRITES a ready material-order XML into the machine's shared
folder, then WAITS for the machine's ``.ans`` answer file, reads
``<Error>N</Error>`` and retries or completes per the error-code table.

It is fully isolated (own driver package + route + registry + config
section) so the CFX consumer path (ADR-ERPNET-0003 "proxy = thin relay")
stays byte-identical — an absent ``europlacer:`` section is a no-op with
zero extra imports.

See ADR-ERPNET-0005 (monolithic Europlacer material transfer + CFX
control) — this is Phase 1 (transport + ``.ans`` retry state machine).
"""

from .common import (
    AnswerMalformed,
    AnswerNotReady,
    EuroplacerOrder,
    EuroplacerOutcome,
    STATE_FATAL,
    STATE_SUCCESS,
    STATE_TIMEOUT,
    STATE_TRANSIENT,
    classify,
    meaning,
    parse_answer,
)
from .producer import EuroplacerFileProducer

__all__ = [
    "EuroplacerFileProducer",
    "EuroplacerOrder",
    "EuroplacerOutcome",
    "AnswerMalformed",
    "AnswerNotReady",
    "STATE_SUCCESS",
    "STATE_FATAL",
    "STATE_TRANSIENT",
    "STATE_TIMEOUT",
    "classify",
    "meaning",
    "parse_answer",
]
