"""Additive intake authority tables; no historical row or snapshot is rewritten."""
from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base

class SourceAdmission(Base):
    __tablename__='personal_source_admissions'
    admission_id: Mapped[str]=mapped_column(Text,primary_key=True)
    user_id: Mapped[int]=mapped_column(Integer,nullable=False,index=True)
    style_id: Mapped[int]=mapped_column(Integer,nullable=False,index=True)
    asset_id: Mapped[str]=mapped_column(Text,nullable=False,index=True)
    receipt_json: Mapped[str]=mapped_column(Text,nullable=False)

class HumanGlyphReview(Base):
    __tablename__='personal_human_glyph_reviews'
    review_id: Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True)
    receipt_sha256: Mapped[str]=mapped_column(Text,nullable=False,unique=True)
    user_id: Mapped[int]=mapped_column(Integer,nullable=False,index=True)
    style_id: Mapped[int]=mapped_column(Integer,nullable=False,index=True)
    sample_id: Mapped[str]=mapped_column(Text,nullable=False,index=True)
    receipt_json: Mapped[str]=mapped_column(Text,nullable=False)

class CandidateReviewJob(Base):
    __tablename__='personal_candidate_review_jobs'
    job_id: Mapped[str]=mapped_column(Text,primary_key=True)
    user_id: Mapped[int]=mapped_column(Integer,nullable=False,index=True)
    style_id: Mapped[int]=mapped_column(Integer,nullable=False,index=True)
    package_json: Mapped[str]=mapped_column(Text,nullable=False)
    package_sha256: Mapped[str]=mapped_column(Text,nullable=False)
    status: Mapped[str]=mapped_column(Text,nullable=False,default='pending')
    result_json: Mapped[str | None]=mapped_column(Text,nullable=True)
    imported_bundle_sha256: Mapped[str | None]=mapped_column(Text,nullable=True)

class CandidateReviewIngress(Base):
    __tablename__='personal_candidate_review_ingress'
    request_id: Mapped[str]=mapped_column(Text,primary_key=True)
    job_id: Mapped[str]=mapped_column(Text,nullable=False,unique=True)
    user_id: Mapped[int]=mapped_column(Integer,nullable=False,index=True)
    request_json: Mapped[str]=mapped_column(Text,nullable=False)
    request_sha256: Mapped[str]=mapped_column(Text,nullable=False)
    status: Mapped[str]=mapped_column(Text,nullable=False,default='requested')
    stage_json: Mapped[str | None]=mapped_column(Text,nullable=True)
