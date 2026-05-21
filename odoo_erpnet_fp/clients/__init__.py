# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""Outbound HTTP clients — the proxy talks to remote Odoo services.

Each module here is a thin client that emits a request and never
consumes one. Inbound HTTP (routes/*) and outbound HTTP (clients/*)
sit on opposite sides of the proxy's network surface.
"""
