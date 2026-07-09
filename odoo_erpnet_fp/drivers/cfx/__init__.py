# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""CFX-IPC (IPC-2591) machine-data ingest drivers.

A ``CfxAmqpIngest`` is a long-lived object that consumes CFX-IPC 1.7
messages from one or more AMQP endpoints (RabbitMQ broker and/or
AMQP 1.0 peer-to-peer) and delivers each as a normalised ``CfxEvent``
into the proxy's existing Odoo links (bus_inject live + signed audit
POST). It mirrors the MQTT ingest (``drivers/cameras/mqtt_listener.py``)
but embeds the Phase-0 ipc-cfx SDK ``CFXPlugin`` façade instead of
paho-mqtt — a single station can run broker + P2P endpoints in parallel.

CFX is ONE standard for ALL machines (Europlacer placement, PARMI
SPI/AOI, reflow ovens, lasers); the machine differences live in the CFX
message content + the Odoo-side REST stat routing, not in separate
drivers here. The heavy ipc-cfx / rabbitmq-amqp-python-client deps are
lazy-imported (optional ``[cfx]`` extra) so pure-fiscal deployments never
load them.
"""
