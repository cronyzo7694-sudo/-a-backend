# file_bank.py - Real Test Jaisa System - Chapter wise, Topic wise, Subject wise, Full Mock
import os, re, json
from pathlib import Path
from collections import Counter

# Check multiple possible locations for questions_data folder
POSSIBLE_BASES = [
    Path(__file__).parent.parent.parent / "questions_data",  # backend/questions_data (correct)
    Path(__file__).parent.parent / "questions_data",  # backend/app/questions_data (fallback)
    Path(__file__).parent / "questions_data",  # backend/app/services/questions_data
    Path(__file__).parent.parent.parent.parent / "questions_data",  # root/questions_data
]

BASE = None
for b in POSSIBLE_BASES:
    if b.exists():
        BASE = b
        break
if BASE is None:
    BASE = Path(__file__).parent.parent.parent / "questions_data"

try:
    from app.services.knowledge_engine.classifier import classify_subject_chapter as local_classify, detect_bloom, detect_difficulty
    KNOWLEDGE_AVAILABLE = True
except Exception:
    KNOWLEDGE_AVAILABLE = False
    def local_classify(q, opts):
        ql = q.lower()
        if "analogy" in ql or (":" in q and "::" in q):
            return {"subject": "Reasoning", "chapter": "Analogy", "topic": "Word Analogy", "concepts": ["Analogy"], "pattern": "A:B::C:?", "question_family": "Analogy"}
        if any(x in ql for x in ["number", "series", "132", "156"]):
            return {"subject": "Reasoning", "chapter": "Analogy", "topic": "Number Analogy", "concepts": ["Number Analogy"], "pattern": "Number Relation", "question_family": "Analogy"}
        return {"subject": "Reasoning", "chapter": "Analogy", "topic": "General", "concepts": [], "pattern": None, "question_family": "Analogy"}

try:
    from app.services.knowledge_engine.free_ai_chain import classify_with_free_ai_chain
    FREE_AI_AVAILABLE = True
except Exception:
    FREE_AI_AVAILABLE = False

def smart_classify(question_text, options=None):
    options = options or []
    if FREE_AI_AVAILABLE:
        try:
            if any([os.getenv("GEMINI_API_KEY"), os.getenv("DEEPSEEK_API_KEY"), os.getenv("OPENROUTER_API_KEY"), os.getenv("GROQ_API_KEY")]):
                ai_result = classify_with_free_ai_chain(question_text, local_classify)
                if ai_result and ai_result.get("subject"):
                    local = local_classify(question_text, options)
                    merged = {**local, **ai_result}
                    return merged
        except Exception:
            pass
    try:
        return local_classify(question_text, options)
    except Exception:
        return {"subject": "Reasoning", "chapter": "Analogy", "topic": "General", "concepts": [], "pattern": None, "question_family": "Analogy"}

def load_questions_from_files():
    questions = []
    found_bases = []
    for b in POSSIBLE_BASES:
        if b.exists():
            found_bases.append(b)
    if BASE not in found_bases and BASE.exists():
        found_bases.append(BASE)
    if not found_bases:
        try:
            BASE.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return questions

    for base_dir in found_bases:
        for txt_file in base_dir.glob("*.txt"):
            try:
                text = txt_file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            blocks = re.split(r'\n\s*Q\.\d+\.', text)
            for i, block in enumerate(blocks[1:], 1):
                if len(block.strip()) < 30:
                    continue
                opts = re.findall(r'\(\s*([a-d])\s*\)\s*([^\(]+?)(?=\(\s*[a-d]\s*\)|$)', block, re.I | re.DOTALL)
                if len(opts) < 2:
                    opts = re.findall(r'\(\s*([a-d])\s*\)\s*([^\n\(]+)', block, re.I)
                    if len(opts) < 2:
                        continue
                q_text = re.split(r'\(\s*a\s*\)', block, flags=re.I)[0] if re.search(r'\(\s*a\s*\)', block, re.I) else block[:500]
                q_text = re.sub(r'www\.ssccglpinnacle\.com.*?\n', '', q_text, flags=re.I)
                q_text = re.sub(r'Download Pinnacle.*?\n', '', q_text, flags=re.I)
                q_text = re.sub(r'Search on TG.*?\n', '', q_text, flags=re.I)
                q_text = re.sub(r'Pinnacle\s+Day:.*?\n', '', q_text)
                q_text = re.sub(r'\s+', ' ', q_text).strip()[:1000]
                if len(q_text) < 10:
                    continue
                try:
                    classification = smart_classify(q_text, [{"option_text": o[1]} for o in opts])
                except Exception:
                    classification = {"subject": "Reasoning", "chapter": "Analogy", "topic": "General", "concepts": [], "pattern": None, "question_family": "Analogy"}
                try:
                    if KNOWLEDGE_AVAILABLE:
                        bloom = detect_bloom(q_text, [])
                        diff, score, mem, logic, calc, exp_time = detect_difficulty(q_text, bloom)
                    else:
                        diff = "medium"
                        exp_time = 60
                except Exception:
                    diff = "medium"
                    exp_time = 60

                questions.append({
                    "id": f"file_{txt_file.stem}_{i}",
                    "qnum": i,
                    "question_text": q_text,
                    "options": [{"option_key": k.upper(), "option_text": v.strip()[:300]} for k,v in opts[:4]],
                    "subject": classification.get("subject", "Reasoning"),
                    "chapter": classification.get("chapter", "Analogy"),
                    "topic": classification.get("topic", "General"),
                    "subtopic": classification.get("subtopic"),
                    "concepts": classification.get("concepts", []),
                    "pattern": classification.get("pattern"),
                    "question_family": classification.get("question_family"),
                    "difficulty": diff,
                    "expected_time": exp_time,
                    "source": txt_file.name,
                    "exam_hint": re.search(r'(SSC\s+\w+.*?\(.*?\))', block).group(1) if re.search(r'(SSC\s+\w+.*?\(.*?\))', block) else None
                })
    return questions

FILE_QUESTIONS = load_questions_from_files()

def get_stats():
    if not FILE_QUESTIONS:
        return {"total": 0, "by_subject": {}, "by_chapter": {}, "by_topic": {}, "by_difficulty": {}}
    by_subject = Counter([q.get("subject","Unknown") for q in FILE_QUESTIONS])
    by_chapter = Counter([q.get("chapter","Unknown") for q in FILE_QUESTIONS])
    by_topic = Counter([q.get("topic","Unknown") for q in FILE_QUESTIONS])
    by_difficulty = Counter([q.get("difficulty","medium") for q in FILE_QUESTIONS])
    return {
        "total": len(FILE_QUESTIONS),
        "by_subject": dict(by_subject),
        "by_chapter": dict(by_chapter),
        "by_topic": dict(by_topic),
        "by_difficulty": dict(by_difficulty),
        "sample_topics": list(by_topic.keys())[:10]
    }

def filter_questions(subject=None, chapter=None, topic=None, difficulty=None, count=20):
    filtered = FILE_QUESTIONS
    if subject:
        filtered = [q for q in filtered if subject.lower() in q.get("subject","").lower()]
    if chapter:
        filtered = [q for q in filtered if chapter.lower() in q.get("chapter","").lower() or chapter.lower() in q.get("topic","").lower()]
    if topic:
        topic_l = topic.lower()
        filtered = [q for q in filtered if topic_l in q.get("topic","").lower() or topic_l in (q.get("subtopic") or "").lower() or topic_l in (q.get("pattern") or "").lower() or any(topic_l in c.lower() for c in q.get("concepts",[]))]
    if difficulty:
        filtered = [q for q in filtered if q.get("difficulty")==difficulty]
    import random
    if len(filtered) > count:
        random.shuffle(filtered)
    return filtered[:count]
