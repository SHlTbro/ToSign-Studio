"""Actual session review evidence, bound to one immutable server review job.

This is not a reusable online vision provider and never invents model calls.
A different package, nonce, panel or user cannot consume the bundle.
"""
import copy,json,re
from pathlib import Path
from .pipeline import sha
from .policy import load_policy
from engine.style.m2_adapter.trainer_resume_protocol import native_path

ROOT=Path(__file__).resolve().parents[3]
REVIEW_SOURCE_FILES=('engine/style/visual_review/offline.py','app/domains/styles/m2_jobs.py',
                    'engine/style/visual_review/pipeline.py','engine/style/visual_review/contracts.py',
                    'engine/style/visual_review/policy.py',
                    'config/candidate-review-import-policy.json','config/personal-acceptance-policy.json')

def review_source_hashes():
    """Source/policy identity for the exact consumer; never contains credentials."""
    return {rel:sha(native_path(ROOT/rel)) for rel in REVIEW_SOURCE_FILES}


class BoundSessionReviewer:
    provider_mode='BOUND_OFFLINE_SESSION_EVIDENCE'
    def __init__(self,bundle_path,package_path,user_id,run_id,*,expected_sha256=None):
        from app.domains.training_sets.candidate_review_jobs import reviewer_allowed,strict_json
        if type(user_id) is not int or user_id<=0 or type(run_id) is not int or run_id<=0:
            raise ValueError('positive authenticated user/run identities required')
        self.bundle_path=native_path(bundle_path);self.package_path=native_path(package_path)
        raw=self.bundle_path.read_bytes()
        if len(raw)>2_000_000:raise ValueError('session review bundle too large')
        self.bundle=strict_json(raw.decode('utf-8'))
        self.package=json.loads(self.package_path.read_text(encoding='utf-8'))
        policy=load_policy(user_id)
        # Actual AI review and owner acceptance are separate gates. OWNER_REQUIRED
        # does not prohibit authorized AI evidence or become a human waiver here.
        if not policy or policy['human_blind_review'].get('requires_actual_ai_blind_review') is not True:
            raise ValueError('current user AI review authorization required')
        if self.package.get('user_id')!=user_id or self.package.get('run_id')!=run_id:
            raise ValueError('authoritative review package belongs to another user/run')
        expected={'package_sha256':sha(self.package_path),'nonce':self.package['nonce'],'user_id':user_id,'run_id':run_id}
        if any(self.bundle.get(k)!=v for k,v in expected.items()):raise ValueError('session review bundle belongs to a different job/version')
        if self.bundle.get('authorization_message_id')!=policy['human_blind_review']['authorization']['message_id']:
            raise ValueError('session review authorization mismatch')
        reviewer={'provider':self.bundle.get('provider','OpenAI' if self.bundle.get('model_id')=='gpt-6-astra' else None),
                  'model_id':self.bundle.get('model_id'),'reviewer_kind':self.bundle.get('reviewer_kind','vision_model')}
        configured=json.loads((ROOT/'config/candidate-review-import-policy.json').read_text(encoding='utf-8'))
        if not reviewer_allowed(configured,reviewer,user_id,self.bundle.get('reviewer_task_id')):
            raise ValueError('reviewer identity is not configured for this user/session')
        self.local=reviewer=={'provider':'CodexLocal','model_id':'current-session','reviewer_kind':'vision_model'}
        self.source_hashes=review_source_hashes()
        self.bundle_sha256=sha(self.bundle_path);self.package_sha256=expected['package_sha256']
        if expected_sha256 is not None and (not isinstance(expected_sha256,str) or re.fullmatch('[0-9a-f]{64}',expected_sha256) is None or self.bundle_sha256!=expected_sha256):
            raise ValueError('independent operator session bundle hash differs')
        if self.local and (expected_sha256 is None or self.bundle.get('review_consumer_sha256')!=self.source_hashes):
            raise ValueError('current-session review requires independent operator hash pin and current consumer source identity')
        if self.bundle.get('actual_pixels_reviewed') is not True:
            raise ValueError('actual pixel-review evidence required')
        panels=self.package['panels'];responses=self.bundle.get('responses') or {}
        if set(responses)!=set(panels):raise ValueError('every exact job panel needs an actual review response')
        self.by_target={};self.panel_paths={}
        for key,panel in panels.items():
            if panel.get('target_char')!=key:raise ValueError('review panel target differs from package')
            response=responses[key]
            if response.get('image_sha256')!=panel['panel_sha256'] or response.get('pixels_inspected') is not True:
                raise ValueError('review response does not bind displayed pixels')
            legacy_without_provider=(reviewer=={'provider':'OpenAI','model_id':'gpt-6-astra','reviewer_kind':'vision_model'}
                                     and 'provider' not in response)
            if any(response.get(k)!=v for k,v in reviewer.items() if not (legacy_without_provider and k=='provider')):
                raise ValueError('reviewer identity missing')
            panel_path=native_path(panel['panel_path'])
            if sha(panel_path)!=panel['panel_sha256']:raise ValueError('displayed review panel bytes changed')
            identity=(key,panel['panel_sha256'])
            self.by_target[identity]=response
            self.panel_paths[identity]=panel_path
    def __call__(self,request):
        image_hash=request.get('panel_sha256') or request.get('image_sha256')
        identity=(request.get('target_char'),image_hash)
        if identity not in self.by_target:raise ValueError('offline evidence cannot review new or changed target/pixels')
        if sha(self.bundle_path)!=self.bundle_sha256 or sha(self.package_path)!=self.package_sha256:
            raise ValueError('frozen session review bundle/package changed')
        if self.local and review_source_hashes()!=self.source_hashes:raise ValueError('session review consumer source changed')
        if sha(self.panel_paths[identity])!=image_hash:raise ValueError('displayed review panel bytes changed')
        return copy.deepcopy(self.by_target[identity])
