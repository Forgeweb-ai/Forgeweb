"""
forge_server/api/forge_img_routes.py
=====================================
DEPRECATED — DO NOT REGISTER.

Removed 2026-06-04. The /forge-img/<slot> resolver was the seam between
the project iframe and forge-server, and it generated four bugs in three
days. Replaced by writing generated images directly into the project's
workspace (public/images/<slot>.png), which the project's own dev server
serves natively — no forge-server route needed.

Kept as an empty module so a stale import or running uvicorn worker that
hasn't fully reloaded doesn't ImportError. Delete this file once a release
ships that we know everything has been bounced through.
"""
from fastapi import APIRouter

router = APIRouter()
