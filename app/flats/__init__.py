"""Flat hunter: rental listings from several portals, checked by the local model.

Same CT, same SQLite file and the same Kleinanzeigen rate limiter as the deals
finder, but its own tables (flat_*), so neither side can disturb the other.
Nothing here pushes anywhere; the dashboard's /wohnungen page is the only reader.
"""
