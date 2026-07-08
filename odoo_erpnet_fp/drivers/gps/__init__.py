# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""GPS / vehicle-tracking drivers.

A ``GpsTracker`` is a long-lived object that delivers ``PositionEvent``
fixes through a listener callback. Subclasses implement the source:
Wialon Remote API polling (``wialon``), a local serial NMEA receiver, or
an ``external`` push endpoint.
"""
