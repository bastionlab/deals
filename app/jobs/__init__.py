"""Job hunter: postings from several portals, read against the user's CV.

Same CT and SQLite file as the deals finder, own tables (job_*), own per-host
limiters. Nothing here pushes anywhere; the dashboard's /jobs page is the only
reader. The model reads (CV profile, fit, how to apply); code decides the filters.
"""
