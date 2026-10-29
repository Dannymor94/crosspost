"""Public media route — serves signed, expiring media tokens for Meta/Instagram.

GET /public-media/{token}

No auth required: the signed token is the permission. Token comes from
sign_media_path(); verify_media_token() checks signature and expiry.
Every failure path returns 404 with no detail (no info leakage).
"""

from __future__ import annotations

import logging
import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from crosspost.web.media import _media_root
from crosspost.web.signed_media import verify_media_token

logger = logging.getLogger(__name__)
router = APIRouter()


@router.api_route("/public-media/{token}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_public_media(token: str) -> FileResponse:
    # Never log the full token.
    token_prefix = token[:8] + "…" if len(token) > 8 else token

    relative_path = verify_media_token(token)
    if relative_path is None:
        logger.debug("public-media: invalid/expired token %s", token_prefix)
        raise HTTPException(status_code=404)

    media_root = _media_root().resolve()
    try:
        candidate = (media_root / relative_path).resolve()
        candidate.relative_to(media_root)  # confinement check — raises ValueError if outside
    except Exception:
        logger.debug("public-media: path outside media_root for token %s", token_prefix)
        raise HTTPException(status_code=404) from None

    if not candidate.is_file():
        logger.debug("public-media: file not found for token %s", token_prefix)
        raise HTTPException(status_code=404)

    media_type, _ = mimetypes.guess_type(str(candidate))
    return FileResponse(candidate, media_type=media_type or "application/octet-stream")
