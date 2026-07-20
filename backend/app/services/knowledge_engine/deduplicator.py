"""
Layer 5: DEDUPLICATOR - Duplicate-safe permanent bank
Fixed for Render low-memory + UTF8 safe
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Dict, Tuple, Optional
import logging

logger = logging.getLogger("exam_os.knowledge_engine.dedup")

def normalize_for_comparison(text: str) -> str:
    if not text:
        return ""
    try:
        text = text.lower()
        text = re.sub(r'^\s*(?:q\.?\s*no\.?\s*|q\.)?\s*\d+[\.\)\]]\s*', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\W+', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:500]  # limit for memory
    except Exception:
        return ""

def semantic_similarity(text1: str, text2: str) -> float:
    if not text1 or not text2:
        return 0.0
    try:
        # Limit length for SequenceMatcher to avoid OOM
        t1 = text1[:400]
        t2 = text2[:400]
        return SequenceMatcher(None, t1, t2).ratio()
    except Exception:
        return 0.0

class Deduplicator:
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
        Returns (is_duplicate, duplicate_of_id, similarity)
        Memory-safe version for Render free tier (512MB)
        """
        try:
            from app.models.question import Question
            from app.extensions import db

            # 1. Exact fingerprint match - only load id, not full object
            if fingerprint_hash:
                try:
                    q = db.session.query(Question.id).filter_by(content_hash=fingerprint_hash)
                    if exclude_id:
                        q = q.filter(Question.id != exclude_id)
                    row = q.first()
                    if row:
                        dup_id = row[0] if isinstance(row, tuple) else getattr(row, 'id', row)
                        return True, int(dup_id), 1.0
                except Exception as e:
                    logger.warning(f"fingerprint check failed: {e}")

            # 2. Semantic hash check - only if column exists, load id + text minimal
            if semantic_hash:
                try:
                    # Check if column exists via query that may fail gracefully
                    q = db.session.query(Question.id, Question.question_text).filter_by(semantic_hash=semantic_hash)
                    if exclude_id:
                        q = q.filter(Question.id != exclude_id)
                    rows = q.limit(3).all()
                    cur_norm = normalize_for_comparison(normalized_text)
                    for r in rows:
                        try:
                            rid = r[0] if isinstance(r, (tuple, list)) else r.id
                            rtext = r[1] if isinstance(r, (tuple, list)) and len(r) > 1 else getattr(r, 'question_text', '')
                            existing_norm = normalize_for_comparison(rtext or "")
                            sim = semantic_similarity(cur_norm, existing_norm)
                            if sim >= 0.80:
                                return True, int(rid), sim
                        except Exception:
                            continue
                except Exception as e:
                    # Column may not exist or other error - skip
                    logger.debug(f"semantic_hash check skipped: {e}")

            # 3. Fallback similarity - VERY limited for memory safety
            # Only check last 30 questions, only id + truncated text, no full objects
            try:
                cur_norm = normalize_for_comparison(normalized_text)
                if len(cur_norm) < 10:
                    return False, None, 0.0

                # Use with_entities to avoid loading full TEXT columns like raw_text, media_json
                recent = (
                    db.session.query(Question.id, Question.question_text)
                    .order_by(Question.id.desc())
                    .limit(30)
                    .all()
                )
                for r in recent:
                    try:
                        rid = r[0] if isinstance(r, (tuple, list)) else r.id
                        if exclude_id and int(rid) == int(exclude_id):
                            continue
                        rtext = r[1] if isinstance(r, (tuple, list)) and len(r) > 1 else getattr(r, 'question_text', '')
                        if not rtext:
                            continue
                        existing_norm = normalize_for_comparison(rtext)
                        if not existing_norm:
                            continue
                        # Quick length filter to avoid expensive similarity
                        if abs(len(cur_norm) - len(existing_norm)) > len(cur_norm) * 0.5:
                            continue
                        sim = semantic_similarity(cur_norm, existing_norm)
                        if sim >= 0.92:
                            return True, int(rid), sim
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"fallback similarity check failed: {e}")

        except Exception as e:
            logger.warning(f"check_duplicate_in_db failed safely: {e}")

        return False, None, 0.0

    def merge_appearance(self, question_id: int, new_appearance: Dict) -> bool:
        try:
            from app.models.knowledge import QuestionAppearance
            from app.extensions import db

            # Check existing with minimal query
            try:
                existing = (
                    db.session.query(QuestionAppearance.id)
                    .filter_by(
                        question_id=question_id,
                        exam_name=new_appearance.get("exam_name"),
                        exam_year=new_appearance.get("exam_year"),
                        source_book=new_appearance.get("source_book"),
                    )
                    .first()
                )
                if existing:
                    return True
            except Exception:
                pass

            appearance = QuestionAppearance(
                question_id=question_id,
                exam_name=(new_appearance.get("exam_name") or "")[:255] if new_appearance.get("exam_name") else None,
                exam_year=new_appearance.get("exam_year"),
                exam_date=(new_appearance.get("exam_date") or "")[:32] if new_appearance.get("exam_date") else None,
                shift=(new_appearance.get("shift") or "")[:64] if new_appearance.get("shift") else None,
                session=(new_appearance.get("session") or "")[:64] if new_appearance.get("session") else None,
                organization=(new_appearance.get("organization") or "")[:255] if new_appearance.get("organization") else None,
                board=(new_appearance.get("board") or "")[:128] if new_appearance.get("board") else None,
                source_book=(new_appearance.get("source_book") or "")[:255] if new_appearance.get("source_book") else None,
                source_type=(new_appearance.get("source_type") or "book")[:32],
                page_number=new_appearance.get("page_number"),
                question_number=(new_appearance.get("question_number") or "")[:32] if new_appearance.get("question_number") else None,
                language_detected=(new_appearance.get("language_detected") or "en")[:16],
                source_hash=(new_appearance.get("source_hash") or "")[:64] if new_appearance.get("source_hash") else None,
            )
            db.session.add(appearance)
            db.session.flush()
            return False

        except Exception as e:
            logger.warning(f"merge_appearance failed: {e}")
            try:
                from app.extensions import db
                db.session.rollback()
            except Exception:
                pass
            return False
