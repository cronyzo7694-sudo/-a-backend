"""
Free AI Chain - DeepSeek + Gemini + ChatGPT (via OpenRouter/Groq) - All Free Tier
Paisa zero, kaam kadak. Fallback chain: Gemini -> DeepSeek -> OpenRouter Free -> Local heuristic
"""
import os
import json
import logging
import requests
from typing import Dict, List, Optional, Any

logger = logging.getLogger("exam_os.free_ai")

# Env keys - add on Render dashboard as env vars (all free)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")  # https://aistudio.google.com - free 60 req/min
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")  # https://platform.deepseek.com - free credits
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")  # https://openrouter.ai - free models
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")  # https://console.groq.com - free fast

def _gemini_classify(question_text: str) -> Optional[Dict]:
    """Gemini 1.5 Flash free - best for classification"""
    if not GEMINI_API_KEY or len(question_text) < 10:
        return None
    try:
        prompt = f"""You are exam classifier. Classify this question into subject/chapter/topic.

Question: {question_text[:800]}

Subjects: Reasoning, Quantitative Aptitude, General Awareness, General Science, English
Chapters for Reasoning: Analogy, Blood Relations, Direction, Syllogism, Coding-Decoding, Series, etc
Chapters for Quant: Number System, Profit Loss, Time Speed, etc
Topics: Word Analogy, Number Analogy, SI Units, etc

Return ONLY JSON: {{"subject":"...","chapter":"...","topic":"...","pattern":"...","concepts":["..."],"difficulty":"easy|medium|hard","bloom":"remember|understand|apply|analyze"}}

No extra text, only JSON."""

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            # Extract JSON from response
            if "{" in text:
                json_str = text[text.find("{"):text.rfind("}")+1]
                return json.loads(json_str)
    except Exception as e:
        logger.debug(f"Gemini failed: {e}")
    return None

def _deepseek_classify(question_text: str) -> Optional[Dict]:
    """DeepSeek free - cheap and good for reasoning"""
    if not DEEPSEEK_API_KEY:
        return None
    try:
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "You are exam classifier. Return only JSON with subject, chapter, topic, pattern, difficulty."},
                {"role": "user", "content": f"Classify: {question_text[:600]} -> JSON: subject, chapter, topic, pattern, difficulty (easy/medium/hard)"}
            ],
            "temperature": 0.2,
            "max_tokens": 300
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"]
            if "{" in text:
                json_str = text[text.find("{"):text.rfind("}")+1]
                return json.loads(json_str)
    except Exception as e:
        logger.debug(f"DeepSeek failed: {e}")
    return None

def _openrouter_free_classify(question_text: str) -> Optional[Dict]:
    """OpenRouter free models - gpt-3.5, llama, mistral free"""
    if not OPENROUTER_API_KEY:
        return None
    try:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        # Use free model
        payload = {
            "model": "meta-llama/llama-3.1-8b-instruct:free",  # free tier
            "messages": [
                {"role": "user", "content": f"Classify exam question into JSON subject, chapter, topic, difficulty. Question: {question_text[:500]}. Return only JSON."}
            ],
            "max_tokens": 200
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"]
            if "{" in text:
                json_str = text[text.find("{"):text.rfind("}")+1]
                return json.loads(json_str)
    except Exception as e:
        logger.debug(f"OpenRouter failed: {e}")
    return None

def _groq_free_classify(question_text: str) -> Optional[Dict]:
    """Groq free - super fast llama"""
    if not GROQ_API_KEY:
        return None
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "llama3-8b-8192",  # free
            "messages": [
                {"role": "user", "content": f"Classify: {question_text[:500]} -> JSON subject, chapter, topic, difficulty. Only JSON."}
            ],
            "temperature": 0.1,
            "max_tokens": 200
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"]["content"]
            if "{" in text:
                json_str = text[text.find("{"):text.rfind("}")+1]
                return json.loads(json_str)
    except Exception as e:
        logger.debug(f"Groq failed: {e}")
    return None

def classify_with_free_ai_chain(question_text: str, local_fallback_fn=None) -> Dict:
    """
    Try free AI chain: Gemini -> DeepSeek -> OpenRouter -> Groq -> Local heuristic
    Returns classification dict, never fails - always returns something
    """
    # Try cache to avoid repeated API calls for same question
    q_text = question_text[:600].strip()
    if len(q_text) < 15:
        return {"subject": "Reasoning", "chapter": "Analogy", "topic": "General", "pattern": None, "difficulty": "medium"}
    
    # 1. Gemini Flash free (best quality free)
    result = _gemini_classify(q_text)
    if result and result.get("subject"):
        logger.info(f"Gemini classified: {result.get('subject')}/{result.get('chapter')}")
        return result
    
    # 2. DeepSeek free
    result = _deepseek_classify(q_text)
    if result and result.get("subject"):
        logger.info(f"DeepSeek classified: {result}")
        return result
    
    # 3. OpenRouter free
    result = _openrouter_free_classify(q_text)
    if result and result.get("subject"):
        logger.info(f"OpenRouter classified: {result}")
        return result
    
    # 4. Groq free
    result = _groq_free_classify(q_text)
    if result and result.get("subject"):
        return result
    
    # 5. Local heuristic fallback (no API, always works)
    if local_fallback_fn:
        try:
            return local_fallback_fn(q_text, [])
        except Exception:
            pass
    
    # Ultimate fallback
    return {"subject": "Reasoning", "chapter": "Analogy", "topic": "General", "pattern": "General", "concepts": [], "difficulty": "medium", "bloom_taxonomy": "understand"}

def extract_with_free_ai(question_block: str) -> Optional[Dict]:
    """Use free AI to extract question, options, answer from messy block - for broken OCR"""
    # Try Gemini for extraction - best for OCR fixing
    if not GEMINI_API_KEY or len(question_block) < 20:
        return None
    try:
        prompt = f"""Extract exam question from this messy OCR text. Fix OCR errors, find question text, 4 options A-D, correct answer if present, explanation if present.

Text: {question_block[:1000]}

Return ONLY JSON: {{"question_text":"...","options":[{{"key":"A","text":"..."}},...],"correct_answer":"A|B|C|D|null","explanation":"..."}}

If answer not found, keep null. Never guess. Only JSON."""
        
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            if "{" in text:
                json_str = text[text.find("{"):text.rfind("}")+1]
                return json.loads(json_str)
    except Exception as e:
        logger.debug(f"AI extraction failed: {e}")
    return None

def ensemble_classify(question_text: str, local_fn=None) -> Dict:
    """
    Kadak version - ask 2-3 free models and take majority vote for subject/chapter
    More accurate but uses more free quota
    """
    votes = []
    
    # Collect votes from all available free AIs (max 2 to save quota)
    for fn in [_gemini_classify, _deepseek_classify]:
        try:
            r = fn(question_text)
            if r and r.get("subject"):
                votes.append(r)
                if len(votes) >= 2:  # Take 2 votes max to save quota
                    break
        except Exception:
            continue
    
    if not votes:
        return classify_with_free_ai_chain(question_text, local_fn)
    
    # Majority vote for subject
    from collections import Counter
    subjects = [v.get("subject","Reasoning") for v in votes]
    most_common_subject = Counter(subjects).most_common(1)[0][0]
    
    # Pick result with most common subject
    for v in votes:
        if v.get("subject") == most_common_subject:
            return v
    
    return votes[0]
