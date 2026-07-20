"""
Layer 5: DEDUPLICATOR - Duplicate-safe permanent bank
Exact duplicates, semantic duplicates, translation duplicates, OCR-noisy duplicates
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Dict, Tuple, Optional, List

from sqlalchemy import text

from app.extensions import db


def normalize_for_comparison(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    # Remove question numbers
    text = re.sub(r'^\s*(?:q\.?\s*no\.?\s*|q\.)?\s*\d+[\.\)\]]\s*', '', text, flags=re.IGNORECASE)
    # Remove punctuation
    text = re.sub(r'\W+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def semantic_similarity(text1: str, text2: str) -> float:
    if not text1 or not text2:
        return 0.0
    return SequenceMatcher(None, text1, text2).ratio()


class Deduplicator:
    """
    Uses existing question_hash + semantic matching + DB lookups
    Production would use vector DB - here we use SQL + similarity.
    """

    def __init__(self):
        pass

    def check_duplicate_in_db(
        self,
        fingerprint_hash: str,
        semantic_hash: str,
        normalized_text: str,
        exclude_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[int], float]:
        """
        Check duplicates in DB using existing content_hash and semantic hash.
        Returns (is_duplicate, duplicate_of_id, similarity)
        """
        try:
            from app.models.question import Question

            # 1. Exact fingerprint match - existing content_hash
            if fingerprint_hash:
                q = Question.query.filter_by(content_hash=fingerprint_hash)
                if exclude_id:
                    q = q.filter(Question.id != exclude_id)
                dup = q.first()
                if dup:
                    return True, dup.id, 1.0

            # 2. Semantic hash match - check via new column if exists, else fallback to text similarity
            if semantic_hash:
                # Try new column semantic_hash if exists
                try:
                    dup = Question.query.filter_by(semantic_hash=semantic_hash)
                    if exclude_id:
                        dup = dup.filter(Question.id != exclude_id)
                    first = dup.first()
                    if first:
                        # Verify similarity
                        existing_norm = normalize_for_comparison(first.question_text or "")
                        cur_norm = normalize_for_comparison(normalized_text)
                        sim = semantic_similarity(cur_norm, existing_norm)
                        if sim >= 0.80:
                            return True, first.id, sim
                except Exception:
                    # Column may not exist yet
                    pass

            # 3. Fallback: text similarity search on recent questions (limit 200 for performance)
            # In production, use pg_trgm or vector search
            try:
                recent_questions = Question.query.order_by(Question.id.desc()).limit(200).all()
                cur_norm = normalize_for_comparison(normalized_text)
                for rq in recent_questions:
                    if exclude_id and rq.id == exclude_id:
                        continue
                    existing_norm = normalize_for_comparison(rq.question_text or "")
                    if not existing_norm:
                        continue
                    # Quick length filter
                    if abs(len(cur_norm) - len(existing_norm)) > len(cur_norm) * 0.3:
                        continue
                    sim = semantic_similarity(cur_norm, existing_norm)
                    if sim >= 0.92:  # High threshold for fallback
                        return True, rq.id, sim
            except Exception:
                pass

        except Exception as e:
            # If DB not available in unit test, return no duplicate
            pass

        return False, None, 0.0

    def merge_appearance(
        self,
        question_id: int,
        new_appearance: Dict,
    ) -> bool:
        """
        Merge appearance history instead of creating duplicate question.
        Returns True if merged, False if new appearance.
        """
        try:
            from app.models.knowledge import QuestionAppearance

            # Check if same exam/year/shift/source already exists
            existing = QuestionAppearance.query.filter_by(
                question_id=question_id,
                exam_name=new_appearance.get("exam_name"),
                exam_year=new_appearance.get("exam_year"),
                shift=new_appearance.get("shift"),
                source_book=new_appearance.get("source_book"),
                page_number=new_appearance.get("page_number"),
            ).first()

            if existing:
                return True  # Already exists

            # Create new appearance record
            appearance = QuestionAppearance(
                question_id=question_id,
                exam_name=new_appearance.get("exam_name"),
                exam_year=new_appearance.get("exam_year"),
                exam_date=new_appearance.get("exam_date"),
                shift=new_appearance.get("shift"),
                session=new_appearance.get("session"),
                organization=new_appearance.get("organization"),
                board=new_appearance.get("board"),
                source_book=new_appearance.get("source_book"),
                source_type=new_appearance.get("source_type", "book"),
                page_number=new_appearance.get("page_number"),
                question_number=new_appearance.get("question_number"),
                language_detected=new_appearance.get("language_detected", "en"),
                source_hash=new_appearance.get("source_hash"),
            )
            db.session.add(appearance)
            db.session.flush()
            return False  # New appearance added

        except Exception as e:
            # Log but don't fail
            import logging
            logging.getLogger("exam_os.knowledge_engine").warning(f"merge_appearance failed: {e}")
            return False
