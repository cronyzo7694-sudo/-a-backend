# file_bank.py - Put question file with code, AI will show tests directly
import os, re
from pathlib import Path

BASE = Path(__file__).parent.parent / "questions_data"

def load_questions_from_files():
    questions = []
    if not BASE.exists():
        BASE.mkdir(parents=True, exist_ok=True)
        return questions
    
    for txt_file in BASE.glob("*.txt"):
        text = txt_file.read_text(encoding="utf-8", errors="ignore")
        # Simple parse Q.1. (a) (b) (c) (d)
        blocks = re.split(r'\n\s*Q\.\d+\.', text)
        for i, block in enumerate(blocks[1:], 1):
            # Extract options (a) (b) (c) (d)
            opts = re.findall(r'\(\s*([a-d])\s*\)\s*([^\(]+)', block, re.I)
            if len(opts) >= 2:
                q_text = block.split("(a)")[0][:500]
                questions.append({
                    "id": f"file_{txt_file.stem}_{i}",
                    "question_text": q_text.strip(),
                    "options": [{"option_key": k.upper(), "option_text": v.strip()[:200]} for k,v in opts[:4]],
                    "subject": "Reasoning",
                    "chapter": "Analogy",
                    "source": txt_file.name
                })
    return questions

FILE_QUESTIONS = load_questions_from_files()
