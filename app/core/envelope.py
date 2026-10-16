"""Success response envelope helpers (contract v0.2 section 2)."""


def ok(data: dict, warnings: list | None = None) -> dict:
    """Build the frozen success envelope: data / meta / warnings."""
    return {
        "data": data,
        "meta": {"api_version": "v1", "schema_version": "0.4"},
        "warnings": warnings or [],
    }