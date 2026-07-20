"""
Layer 6: FINAL OBJECT + Pipeline Orchestration
Internal Brain of Exam OS - Permanent Intelligence Layer
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple, Union

from .preprocessor import clean_junk, detect_language, extract_question_blocks, normalize_spacing, generate_hashes
from .extractor import human_like_extract
from .classifier import classify_subject_chapter, detect_bloom, detect_difficulty, generate_tags, extract_metadata_from_text
from .deduplicator import Deduplicator, normalize_for_comparison


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CanonicalQuestion:
    """
    Future-proof QuestionBankObject inside existing product
    Does NOT replace Question model, but builds payload for it
    """

    def __init__(
        self,
        raw_text: str,
        normalized_question: str,
        question_text: Optional[str],
        question_type: str,
        options: List[Dict],
        correct_answer: Optional[str],
        explanation: Optional[str],
        paragraph: Optional[Dict],
        assertion: Optional[str],
        reason: Optional[str],
        statements: Optional[List[str]],
        classification: Dict,
        metadata: Dict,
        tags: List[str],
        keywords: List[str],
        hashes: Dict,
        language: str,
        semantic_summary: str,
        confidence_score: float,
        needs_review: bool,
        review_reasons: List[str],
        source_meta: Dict,
    ):
        self.id = str(uuid.uuid4())
        self.qid = f"Q_{hashes['fingerprint_hash'][:12].upper()}"  # stable universal qid
        self.version = 1
        self.raw_text = raw_text
        self.normalized_question = normalized_question
        self.question_text = question_text or normalized_question[:2000]
        self.question_type = question_type
        self.options = options
        self.correct_answer = correct_answer  # None if missing - NEVER guess
        self.explanation = explanation or ""
        self.paragraph = paragraph
        self.assertion = assertion
        self.reason = reason
        self.statements = statements
        self.classification = classification
        self.metadata = metadata
        self.tags = tags
        self.keywords = keywords
        self.fingerprint_hash = hashes["fingerprint_hash"]
        self.semantic_hash = hashes["semantic_hash"]
        self.source_hash = hashes["source_hash"]
        self.language_detected = language
        self.semantic_summary = semantic_summary
        self.confidence_score = confidence_score
        self.needs_review = needs_review
        self.review_reasons = review_reasons
        self.source_meta = source_meta
        self.appearance_history: List[Dict] = []
        self.duplicate_info: Dict[str, Any] = {
            "is_duplicate": False,
            "duplicate_of": None,
            "fingerprint_hash": hashes["fingerprint_hash"],
            "semantic_hash": hashes["semantic_hash"],
            "similarity_score": None,
        }

    def to_db_payload_for_question_model(self) -> Dict[str, Any]:
        """
        Convert to existing backend Question model compatible payload
        This is what existing questions API expects
        """
        # Prepare correct_answer encoding
        correct = self.correct_answer
        if isinstance(correct, list):
            correct_encoded = json.dumps(correct)
        else:
            correct_encoded = str(correct) if correct else ""

        # Tags as comma separated
        tags_str = ",".join(self.tags[:10])

        # Classification JSON for new columns
        classification_json = json.dumps({
            "subject": self.classification.get("subject"),
            "chapter": self.classification.get("chapter"),
            "topic": self.classification.get("topic"),
            "subtopic": self.classification.get("subtopic"),
            "micro_topic": self.classification.get("micro_topic"),
            "concepts": self.classification.get("concepts", []),
            "pattern": self.classification.get("pattern"),
            "question_family": self.classification.get("question_family"),
            "bloom_taxonomy": self.classification.get("bloom_taxonomy"),
            "difficulty": self.classification.get("difficulty"),
            "difficulty_score": self.classification.get("difficulty_score"),
            "expected_time_seconds": self.classification.get("expected_time_seconds"),
            "memory_level": self.classification.get("memory_level"),
            "logic_level": self.classification.get("logic_level"),
            "calculation_level": self.classification.get("calculation_level"),
        }, ensure_ascii=False)

        metadata_json = json.dumps({
            "exam_name": self.metadata.get("exam_name"),
            "exam_year": self.metadata.get("exam_year"),
            "shift": self.metadata.get("shift"),
            "organization": self.metadata.get("organization"),
            "source_book": self.metadata.get("source_book"),
            "page_number": self.metadata.get("page_number"),
            "question_number": self.metadata.get("question_number"),
            "source_type": self.metadata.get("source_type"),
            "board": self.metadata.get("board"),
        }, ensure_ascii=False)

        return {
            "question_text": self.question_text,
            "question_type": self.question_type,
            "difficulty": self.classification.get("difficulty", "medium"),
            "correct_answer": correct_encoded,
            "explanation": self.explanation,
            "paragraph_text": self.paragraph.get("text") if self.paragraph else None,
            "options": self.options,
            "tags": tags_str,
            "language": self.language_detected,
            "content_hash": self.fingerprint_hash,
            # New additive fields - stored via media_json or new columns if exist
            "raw_text": self.raw_text,
            "normalized_question": self.normalized_question,
            "semantic_hash": self.semantic_hash,
            "source_hash": self.source_hash,
            "qid": self.qid,
            "semantic_summary": self.semantic_summary,
            "classification_json": classification_json,
            "metadata_json": metadata_json,
            "confidence_score": self.confidence_score,
            "needs_review": self.needs_review,
            "review_reason": ",".join(self.review_reasons) if self.review_reasons else None,
            "search_tokens": " ".join(self.keywords),
            "embeddings_text": self.semantic_summary,
            # Standard scoring
            "marks": self.source_meta.get("marks", 2.0),
            "negative_marks": self.source_meta.get("negative_marks", 0.5),
        }

    def to_frontend_compatible(self) -> Dict[str, Any]:
        """
        For frontend preview - matches existing Exam OS Question type
        """
        return {
            "id": self.id,
            "qid": self.qid,
            "question_text": self.question_text,
            "question_type": self.question_type,
            "difficulty": self.classification.get("difficulty", "medium"),
            "options": self.options,
            "correct_answer": self.correct_answer,
            "explanation": self.explanation,
            "paragraph": self.paragraph,
            "assertion": self.assertion,
            "reason": self.reason,
            "statements": self.statements,
            "subject": self.classification.get("subject"),
            "chapter": self.classification.get("chapter"),
            "topic": self.classification.get("topic"),
            "concepts": self.classification.get("concepts", []),
            "pattern": self.classification.get("pattern"),
            "bloom_taxonomy": self.classification.get("bloom_taxonomy"),
            "expected_time_seconds": self.classification.get("expected_time_seconds"),
            "tags": self.tags,
            "keywords": self.keywords,
            "language": self.language_detected,
            "semantic_summary": self.semantic_summary,
            "metadata": self.metadata,
            "duplicate_info": self.duplicate_info,
            "confidence_score": self.confidence_score,
            "needs_review": self.needs_review,
            "review_reasons": self.review_reasons,
        }

    def to_full_knowledge_object(self) -> Dict[str, Any]:
        """
        Complete future-proof object for direct DB insert / vector indexing
        This is the single source of truth
        """
        return {
            "id": self.id,
            "qid": self.qid,
            "version": self.version,
            "raw_text": self.raw_text,
            "normalized_question": self.normalized_question,
            "semantic_summary": self.semantic_summary,
            "question_text": self.question_text,
            "question_type": self.question_type,
            "options": self.options,
            "correct_answer": self.correct_answer,  # None if missing
            "explanation": self.explanation if self.explanation else None,
            "paragraph": self.paragraph,
            "assertion": self.assertion,
            "reason": self.reason,
            "statements": self.statements,
            "classification": self.classification,
            "metadata": self.metadata,
            "tags": self.tags,
            "keywords": self.keywords,
            "search_tokens": " ".join(self.keywords),
            "embeddings_text": self.semantic_summary,
            "duplicate_info": self.duplicate_info,
            "appearance_history": self.appearance_history,
            "confidence_score": self.confidence_score,
            "needs_human_review": self.needs_review,
            "review_reasons": self.review_reasons,
            "source_meta": self.source_meta,
            "fingerprint_hash": self.fingerprint_hash,
            "semantic_hash": self.semantic_hash,
            "source_hash": self.source_hash,
            "frontend_compatible": self.to_frontend_compatible(),
        }


class KnowledgeEngine:
    """
    6-Layer Internal Pipeline - Brain of Exam OS
    """

    def __init__(self):
        self.deduplicator = Deduplicator()
        self.version = "1.0.0-integrated"

    def _semantic_summary(self, question_text: str, classification: Dict, q_type: str) -> str:
        subj = classification.get("subject", "General")
        chap = classification.get("chapter") or classification.get("topic") or "general"
        core = question_text[:120].replace("\n", " ").strip()
        core = (core[:80] + "...") if len(core) > 80 else core
        return f"{subj} - {chap} - {q_type} - {core}"

    def process_single_block(self, raw_block: str, source_meta: Dict = None) -> CanonicalQuestion:
        source_meta = source_meta or {}

        # Layer 2: Preprocessor
        cleaned, clean_stats = clean_junk(raw_block)
        lang = detect_language(cleaned)
        normalized = normalize_spacing(cleaned) if cleaned else raw_block
        hashes = generate_hashes(normalized)

        # Layer 3: Extractor
        extracted = human_like_extract(cleaned or raw_block)

        # Layer 4: Classifier
        subject_info = classify_subject_chapter(
            extracted.get("question_text", "") or cleaned or raw_block,
            extracted.get("options", [])
        )
        bloom = detect_bloom(
            extracted.get("question_text", "") or cleaned or raw_block,
            extracted.get("options", [])
        )
        difficulty, diff_score, mem, logic, calc, exp_time = detect_difficulty(
            extracted.get("question_text", "") or cleaned or raw_block,
            bloom
        )

        # Build classification dict
        classification = {
            "subject": subject_info.get("subject"),
            "chapter": subject_info.get("chapter"),
            "topic": subject_info.get("topic"),
            "subtopic": subject_info.get("subtopic"),
            "micro_topic": subject_info.get("micro_topic"),
            "concepts": subject_info.get("concepts", []),
            "pattern": subject_info.get("pattern"),
            "question_family": subject_info.get("question_family"),
            "bloom_taxonomy": bloom,
            "difficulty": subject_info.get("difficulty") if "difficulty" in subject_info else difficulty,
            "difficulty_score": diff_score,
            "expected_time_seconds": exp_time,
            "memory_level": mem,
            "logic_level": logic,
            "calculation_level": calc,
        }
        # Override difficulty from classifier if detected
        classification["difficulty"] = difficulty
        classification["difficulty_score"] = diff_score

        # Metadata
        inferred_meta = extract_metadata_from_text(raw_block)
        merged_meta = {**inferred_meta, **source_meta}
        # Ensure defaults
        merged_meta.setdefault("source_type", source_meta.get("source_type", "typed"))
        merged_meta.setdefault("source_book", source_meta.get("source_book"))
        merged_meta.setdefault("language_detected", lang)

        # Tags & Keywords
        from .classifier import generate_tags as gen_tags
        tags, keywords = gen_tags(subject_info, merged_meta, bloom, extracted.get("question_type", "single_choice"))

        # Semantic summary
        semantic_summary = self._semantic_summary(
            extracted.get("question_text", "") or normalized[:100],
            subject_info,
            extracted.get("question_type", "single_choice")
        )

        # Confidence & review
        has_answer = extracted.get("correct_answer") is not None
        has_options = len(extracted.get("options", [])) >= 2
        confidence = 0.9 if (has_answer and has_options) else (0.7 if has_options else 0.5)
        needs_review = not has_answer or not has_options
        review_reasons = []
        if not has_answer:
            review_reasons.append("answer_missing")
        if not has_options and extracted.get("question_type") in ["single_choice", "multiple_choice"]:
            review_reasons.append("options_missing_or_malformed")
        if len(normalized) < 20:
            review_reasons.append("too_short")

        canonical = CanonicalQuestion(
            raw_text=raw_block,
            normalized_question=normalized,
            question_text=extracted.get("question_text"),
            question_type=extracted.get("question_type", "single_choice"),
            options=extracted.get("options", []),
            correct_answer=extracted.get("correct_answer"),  # None if missing - NEVER guess
            explanation=extracted.get("explanation"),
            paragraph=extracted.get("paragraph"),
            assertion=extracted.get("assertion"),
            reason=extracted.get("reason"),
            statements=extracted.get("statements"),
            classification=classification,
            metadata=merged_meta,
            tags=tags,
            keywords=keywords,
            hashes=hashes,
            language=lang,
            semantic_summary=semantic_summary,
            confidence_score=confidence,
            needs_review=needs_review,
            review_reasons=review_reasons,
            source_meta=source_meta,
        )

        # Appearance history initial
        if merged_meta.get("exam_name") or merged_meta.get("source_book"):
            canonical.appearance_history.append({
                "exam_name": merged_meta.get("exam_name"),
                "exam_year": merged_meta.get("exam_year"),
                "exam_date": merged_meta.get("exam_date"),
                "shift": merged_meta.get("shift"),
                "session": merged_meta.get("session"),
                "organization": merged_meta.get("organization"),
                "board": merged_meta.get("board"),
                "source_book": merged_meta.get("source_book"),
                "source_type": merged_meta.get("source_type", "book"),
                "page_number": merged_meta.get("page_number"),
                "question_number": merged_meta.get("question_number"),
                "language_detected": lang,
                "source_hash": hashes["source_hash"],
            })

        # Layer 5: Deduplicator (check DB)
        normalized_for_dedup = normalize_for_comparison(canonical.question_text or normalized)
        is_dup, dup_of, sim = self.deduplicator.check_duplicate_in_db(
            fingerprint_hash=hashes["fingerprint_hash"],
            semantic_hash=hashes["semantic_hash"],
            normalized_text=normalized_for_dedup,
        )
        canonical.duplicate_info["is_duplicate"] = is_dup
        canonical.duplicate_info["duplicate_of"] = dup_of
        canonical.duplicate_info["similarity_score"] = sim

        return canonical

    def process_document(
        self,
        content: str,
        source_meta: Dict = None,
        file_type: str = "txt"
    ) -> List[CanonicalQuestion]:
        source_meta = source_meta or {}
        if not content or not str(content).strip():
            return []

        if isinstance(content, bytes):
            try:
                content = content.decode('utf-8')
            except Exception:
                content = content.decode('latin-1', errors='ignore')

        cleaned_doc, _ = clean_junk(content)
        blocks = extract_question_blocks(cleaned_doc or content)

        results = []
        for idx, block in enumerate(blocks):
            if len(block.strip()) < 20:
                continue
            block_meta = {**source_meta, "question_number": str(idx + 1)}
            try:
                q_obj = self.process_single_block(block, block_meta)
                results.append(q_obj)
            except Exception as e:
                # Never fail whole batch - log and continue
                import logging
                logging.getLogger("exam_os.knowledge_engine").warning(f"Block {idx} failed: {e}")
                continue

        return results

    def to_import_result(
        self,
        questions: List[CanonicalQuestion],
        source_meta: Dict = None
    ) -> Dict[str, Any]:
        total = len(questions)
        dups = [q for q in questions if q.duplicate_info.get("is_duplicate")]
        needs_review = [q for q in questions if q.needs_review]

        return {
            "total_blocks_found": total,
            "questions_created": total - len(dups),
            "duplicates_found": len(dups),
            "needs_review": len(needs_review),
            "questions": [q.to_full_knowledge_object() for q in questions],
            "source_meta": source_meta or {},
        }


# Singleton for use in routes
knowledge_engine = KnowledgeEngine()
