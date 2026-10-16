"""Bounded project/user delegation; absent or invalid policy means no waiver."""
import hashlib
import json
from pathlib import Path

POLICY_PATH=Path(__file__).resolve().parents[3]/'config/personal-acceptance-policy.json'


def load_policy(user_id, path=None):
    target=Path(path) if path is not None else POLICY_PATH
    try:
        raw=target.read_bytes();p=json.loads(raw.decode('utf-8'))
        if p.get('schema')!='dyYY-personal-acceptance-policy/2' or p.get('project')!='dyYY' or p.get('personal_noncommercial') is not True:return None
        if isinstance(user_id,bool) or p.get('user_id')!=int(user_id):return None
        for name in ('human_blind_review','candidate_confirmation'):
            authority=p[name]['authorization']
            if not all(isinstance(authority.get(k),str) and authority[k] for k in ('message_id','created_at','user_statement')):return None
        p['_sha256']=hashlib.sha256(raw).hexdigest()
        return p
    except (OSError,ValueError,KeyError,TypeError):return None


def human_waiver(user_id):
    p=load_policy(user_id)
    return p if p and p['human_blind_review'].get('mode')=='WAIVED_BY_USER' and p['human_blind_review'].get('requires_actual_ai_blind_review') is True else None


def candidate_delegation(user_id):
    p=load_policy(user_id)
    return p if p and p['candidate_confirmation'].get('mode')=='AI_AUTHORIZED' else None

DEFAULT_VISUAL_THRESHOLDS={'style_similarity':3,'glyph_correctness':4,'artifact_quality':4,'human_likeness':3,'consistency':3}


def bind_threshold_policy(config,user_id):
    """Server policy is authoritative; clients cannot weaken/stamp their own."""
    out=dict(config)
    if out.get('engine')!='m2':return out
    policy=load_policy(user_id)
    expected=dict(policy['visual_thresholds'] if policy else DEFAULT_VISUAL_THRESHOLDS)
    if 'visual_thresholds' in out and out['visual_thresholds']!=expected:
        raise ValueError('visual thresholds must equal the server acceptance policy')
    expected_hash=policy['_sha256'] if policy else None
    if 'acceptance_policy_sha256' in out and out['acceptance_policy_sha256']!=expected_hash:
        raise ValueError('acceptance policy hash does not match current user authorization')
    out['visual_thresholds']=expected
    if policy:out['acceptance_policy_sha256']=expected_hash
    else:out.pop('acceptance_policy_sha256',None)
    return out


def validate_threshold_policy(config,user_id):
    expected=bind_threshold_policy(config,user_id)
    if config.get('engine')!='m2' or config.get('visual_thresholds')!=expected['visual_thresholds']:
        raise ValueError('M2 configuration must bind the server visual policy')
    if config.get('acceptance_policy_sha256')!=expected.get('acceptance_policy_sha256'):
        raise ValueError('M2 configuration is not bound to current user authorization')
