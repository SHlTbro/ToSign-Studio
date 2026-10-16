"""Extraction domain (T59-R1, batch B5-01): detect-text / extract endpoints.

Algorithm stance: the background-separation and candidate-generation
strategies are the Owner-adjudicated baseline (patch-D §3.2/§11, ruling
2026-09-24: enhanced traditional pipeline); U-Net is the upgrade path via
new change order (artifacts/t6-unet/upgrade_gate.json) - see service.py.
"""
