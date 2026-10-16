"""Export routing was rewired when the real render domain landed (T10).

D-T71-3 said this fixture-driven route must be replaced once T10 exists: the
live export endpoint is now ``app.domains.render.router`` (POST
/api/v1/render-jobs/{job_id}/export), which calls the size guards below
(check_single_export / pack_outputs_zip) as service functions. This module
kept only the guard constants/helpers and exports no routes.
"""

from fastapi import APIRouter

router = APIRouter()  # intentionally empty; see module docstring
