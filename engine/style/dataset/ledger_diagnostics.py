"""Read-only dependency disclosure; never relaxes legacy R04 acceptance."""
from pathlib import Path
from .ledger import LedgerIndex,resolve_active_ledger_path
from .schema import DatasetError

def source_dependency_status(project_root,ledger=None):
    root=Path(project_root).resolve()
    try:
        ledger=ledger or LedgerIndex.load(resolve_active_ledger_path(root))
        checks=ledger.verify_sources(root)
        expected={x['path']:x.get('sha256') for x in ledger.data.get('sources',[])}
        required=[{**x,'expected_sha256':expected.get(x['path'])} for x in checks]
        try:ledger.assert_r04_integrity(root);status='READY';detail=None
        except (DatasetError,OSError,ValueError,KeyError) as exc:status='BLOCKED_DEPENDENCY';detail=str(exc)
        return {'status':status,'ledger_sha256':ledger.sha256,'required_metadata':required,
                'missing_metadata':[x for x in required if not x['exists']],
                'detail':detail,'remediation':'Restore the exact pinned JSON/JSONL metadata files from the original project backup and re-run integrity verification. Do not regenerate receipts, substitute hashes, or add protected images to training.',
                'read_only':True,'protected_images_opened':False,'existing_verified_snapshots_unchanged':True}
    except (DatasetError,OSError,ValueError,KeyError) as exc:
        return {'status':'BLOCKED_DEPENDENCY','required_metadata':[],'missing_metadata':[],
                'detail':str(exc),'remediation':'Restore the original hash-bound R04 promotion and active ledger metadata.','read_only':True,'protected_images_opened':False}
