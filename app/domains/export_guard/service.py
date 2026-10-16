"""Export size guards (patch-C section 11.6).

Default caps are contract constants; YY_EXPORT_CAP / YY_ZIP_CAP exist only so
tests can exercise the guard paths with small files. Probe code asserts the
default constant values separately.
"""
import os
import zipfile

from app.core.errors import ApiError

SINGLE_EXPORT_CAP = 56_000_000
ZIP_CAP = 500_000_000


def _single_export_cap() -> int:
    return int(os.environ.get("YY_EXPORT_CAP", SINGLE_EXPORT_CAP))


def _zip_cap() -> int:
    return int(os.environ.get("YY_ZIP_CAP", ZIP_CAP))


def check_single_export(size_bytes: int) -> None:
    cap = _single_export_cap()
    if size_bytes > cap:
        raise ApiError(
            413,
            "EXPORT_TOO_LARGE",
            f"single export artifact is {size_bytes} bytes, cap is {cap}",
        )


def pack_outputs_zip(file_paths: list, dest: str) -> str:
    """Stream files into ``dest`` as a ZIP, enforcing both caps.

    Pre-checks the summed source sizes, then re-checks the cumulative size
    after each member while writing; any failure removes the partial archive
    so no half-written zip is ever left behind.
    """
    for path in file_paths:
        check_single_export(os.path.getsize(path))
    zip_cap = _zip_cap()
    planned = sum(os.path.getsize(p) for p in file_paths)
    if planned > zip_cap:
        raise ApiError(
            413,
            "ZIP_TOO_LARGE",
            f"planned zip payload is {planned} bytes, cap is {zip_cap}",
        )
    tmp_dest = dest + ".part"
    cumulative = 0
    try:
        with zipfile.ZipFile(tmp_dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in file_paths:
                zf.write(path, arcname=os.path.basename(path))
                cumulative += os.path.getsize(path)
                if cumulative > zip_cap:
                    raise ApiError(
                        413,
                        "ZIP_TOO_LARGE",
                        f"zip payload exceeded {zip_cap} bytes while packing",
                    )
        os.replace(tmp_dest, dest)
    except BaseException:
        if os.path.exists(tmp_dest):
            os.remove(tmp_dest)
        raise
    return dest
