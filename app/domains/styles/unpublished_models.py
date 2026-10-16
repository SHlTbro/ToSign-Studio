"""Read the existing offline-import registry; this grants no model authority."""
from datetime import datetime
import re

from fastapi import Depends, Request
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import get_session
from app.core.errors import ApiError
from app.core.identity import get_current_user_id
from installer.unpublished_artifacts.unpublished_artifact_import import TABLE

_COLUMNS = {'import_id', 'user_id', 'adapter_sha256', 'manifest_sha256',
            'input_fingerprint_sha256', 'relative_directory', 'receipt_sha256', 'status', 'created_at'}


def session_account(request: Request, session=Depends(get_session)) -> int:
    # This private-material reader never accepts the development X-User-Id shim.
    if not request.cookies.get('session'):
        raise ApiError(401, 'UNAUTHORIZED', '请先登录后查看已导入的模型')
    return get_current_user_id(request, session)


def _public_row(row):
    if not isinstance(row['import_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', row['import_id']):
        raise ValueError('invalid import identity')
    for field in ('adapter_sha256', 'manifest_sha256', 'input_fingerprint_sha256', 'receipt_sha256'):
        if not isinstance(row[field], str) or not re.fullmatch(r'[0-9a-f]{64}', row[field]):
            raise ValueError('invalid import digest')
    if datetime.fromisoformat(row['created_at']).tzinfo is None:
        raise ValueError('invalid import time')
    return {
        'import_id': row['import_id'], 'adapter_sha256': row['adapter_sha256'],
        'manifest_sha256': row['manifest_sha256'], 'created_at': row['created_at'],
        'source_summary': '官方离线导入（模型包和资料包）',
        'input_fingerprint_sha256': row['input_fingerprint_sha256'],
        'publication_status': 'UNPUBLISHED', 'runtime_status': 'NOT_REGISTERED',
        'inference_ready': False, 'publish_ready': False, 'model_quality_approval': False,
        'verification_scope': 'IMPORT_RECORD_ONLY',
    }


def list_models(session, user_id: int, *, page: int = 1, page_size: int = 20):
    """Read imports and recheck separately selected inference; never publishes."""
    try:
        connection = session.connection()
        schema = inspect(connection)
        if not schema.has_table(TABLE):
            return {'items': [], 'total': 0, 'page': page, 'page_size': page_size,
                    'registry_status': 'not_installed'}
        if not _COLUMNS.issubset({column['name'] for column in schema.get_columns(TABLE)}):
            raise ValueError('unsupported import registry schema')
        params = {'user_id': user_id, 'limit': page_size, 'offset': (page - 1) * page_size}
        total = connection.execute(text(f'SELECT COUNT(*) FROM {TABLE} '
                                        "WHERE user_id=:user_id AND status='unpublished'"), params).scalar_one()
        rows = connection.execute(text(
            f'SELECT import_id,adapter_sha256,manifest_sha256,input_fingerprint_sha256,receipt_sha256,created_at '
            f"FROM {TABLE} WHERE user_id=:user_id AND status='unpublished' "
            'ORDER BY created_at DESC,import_id DESC LIMIT :limit OFFSET :offset'), params).mappings()
        from .imported_inference import public_state
        items = [{**_public_row(row), **public_state(session, user_id, row['import_id'])} for row in rows]
        return {'items': items, 'total': total,
                'page': page, 'page_size': page_size, 'registry_status': 'ready'}
    except (SQLAlchemyError, ValueError, TypeError, KeyError) as exc:
        raise ApiError(503, 'UNPUBLISHED_MODELS_UNAVAILABLE',
                       '未发布模型记录暂时无法读取，请联系维护人员检查导入记录', retryable=False) from exc
