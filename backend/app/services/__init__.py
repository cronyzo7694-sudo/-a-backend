"""Business logic services for Exam OS (CBT platform).

Core engines (configuration-driven)::

    config_engine       — env + file + defaults
    feature_flags       — ENABLE_* flags
    permission_engine   — access decisions
    monetization_engine — ads / plans / payments abstraction
    maintenance_engine  — maintenance / read-only / emergency
    rule_engine         — per-exam CBT rules
    analytics_engine    — attempt / user insights
    scoring             — answer evaluation
    question_hash       — duplicate fingerprints
    schema_upgrade      — additive schema patches
    seed                — demo data
    notification_engine — queue + pluggable providers (telegram/email/...)

Import stable callables from their modules — do not duplicate logic in routes.
"""
