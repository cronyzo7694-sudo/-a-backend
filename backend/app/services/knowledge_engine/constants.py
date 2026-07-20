"""
Constants for Knowledge Engine - Indian Competitive Exam focused
"""
import re

# Junk patterns to remove - ads, footers, page numbers, telegram promos
JUNK_PATTERNS = [
    r"Pinnacle\s+SSC.*|Kiran\s+Publication.*|Lucent.*Publication.*|Arihant\s+.*",
    r"©.*All rights reserved|©.*Copyright.*",
    r"www\.\w+\.com|https?://\S+",
    r"Adda247|Testbook|Oliveboard|Gradeup|Unacademy|BYJU'S",
    r"Free\s+PDF|Download\s+Now|Subscribe\s+Now|Buy\s+Now",
    r"Follow us on.*|Join\s+Telegram|Join\s+Whatsapp|Join\s+YouTube",
    r"Page No\.\s*\d+|Page\s*-\s*\d+|Page\s+\d+\s+of\s+\d+",
    r"SSC\s+CGL.*Answer Key.*|Answer Key.*SSC",
    r"For more.*visit.*|Visit us at.*",
]

HEADER_FOOTER_PATTERNS = [
    r"^\s*\d+\s*$",  # lonely page numbers
    r"^\s*(SSC|UPSC|Banking|Railway|Chapter|Section).*Page.*\d+\s*$",
    r"^\s*(Quantitative|Reasoning|General Awareness|English).*---.*\d+\s*$",
]

# OCR fixes - common mistakes in scanned Indian books
OCR_FIXES = {
    r"\b(\d+)o\b": r"\g<1>0",
    r"\b[Cc]hoos[ec]\b": "Choose",
    r"\bWbich\b": "Which",
    r"\bWhicb\b": "Which",
    r"\bAnswcr\b": "Answer",
    r"\bAnwer\b": "Answer",
    r"\bEollowing\b": "Following",
    r"\bEollowinq\b": "Following",
    r"(\d+)\s*l\s*(\d+)": r"\1.\2",
    r"\b1\s*\)\s*": "1) ",
    r"\bOptlon\b": "Option",
    r"\bQuesilon\b": "Question",
}

# Indian exams patterns for metadata extraction
EXAM_PATTERNS = [
    (r"(SSC\s*(?:CGL|CHSL|CPO|GD|MTS|JE|Selection Post)\s*(?:Tier\s*\d+)?\s*(?:20\d{2})?)", "SSC"),
    (r"(UPSC\s*(?:CSE|CDS|NDA|CAPF|IES)\s*(?:20\d{2})?)", "UPSC"),
    (r"(RRB\s*(?:NTPC|Group D|JE|ALP|RPF)\s*(?:20\d{2})?)", "Railway"),
    (r"(IBPS\s*(?:PO|Clerk|SO|RRB)\s*(?:20\d{2})?)", "Banking"),
    (r"(SBI\s*(?:PO|Clerk)\s*(?:20\d{2})?)", "Banking"),
    (r"(Delhi Police\s*(?:Constable|SI)?\s*(?:20\d{2})?)", "Police"),
    (r"(CUET\s*(?:UG|PG)?\s*(?:20\d{2})?)", "CUET"),
    (r"(CAT\s*(?:20\d{2})?)", "CAT"),
    (r"(GATE\s*(?:20\d{2})?)", "GATE"),
]

# Subject keyword mapping for classification
SUBJECT_KEYWORDS = {
    "Reasoning": [
        "analogy", "blood relation", "direction", "syllogism", "coding decoding", "series", "puzzle",
        "seating arrangement", "inequality", "alphabet", "mirror image", "water image", "paper folding",
        "132 : 156", ":", "::", "related to", "odd one out"
    ],
    "Quantitative Aptitude": [
        "simplification", "profit loss", "time speed distance", "algebra", "geometry", "trigonometry",
        "mensuration", "number system", "percentage", "ratio", "average", "compound interest", "simple interest",
        "132 : 156", "find the value", "calculate", "what is", "solve"
    ],
    "General Awareness": [
        "history", "geography", "polity", "economy", "science", "current affairs", "static gk",
        "capital", "president", "river", "who", "when", "which year", "article"
    ],
    "General Science": [
        "physics", "chemistry", "biology", "unit", "pascal", "joule", "cell", "atom", "pressure : pascal",
        "si unit", "measurement", "disease", "element"
    ],
    "English": [
        "synonym", "antonym", "error spotting", "comprehension", "cloze test", "para jumble", "grammar",
        "sentence improvement", "fill in the blank", "vocabulary"
    ],
}

CHAPTER_MAP = {
    "analogy": ("Reasoning", "Analogy", "Word Analogy"),
    "number analogy": ("Reasoning", "Analogy", "Number Analogy"),
    "blood relation": ("Reasoning", "Blood Relations", "Blood Relation"),
    "direction sense": ("Reasoning", "Direction & Distance", "Direction Sense"),
    "syllogism": ("Reasoning", "Syllogism", "Syllogism"),
    "coding decoding": ("Reasoning", "Coding-Decoding", "Coding Decoding"),
    "profit and loss": ("Quantitative Aptitude", "Profit Loss", "Profit and Loss"),
    "simple interest": ("Quantitative Aptitude", "Interest", "Simple Interest"),
    "history": ("General Awareness", "History", "History"),
    "indian polity": ("General Awareness", "Polity", "Indian Polity"),
    "si unit": ("General Science", "Units and Measurements", "SI Units"),
    "units and measurements": ("General Science", "Units and Measurements", "Units"),
    "pressure": ("General Science", "Units and Measurements", "Pressure Units"),
}

DIFFICULTY_KEYWORDS = {
    "very_easy": ["define", "what is capital", "who is"],
    "easy": ["find", "what is", "which"],
    "medium": ["calculate", "which of the following", "consider the following"],
    "hard": ["assert", "prove", "complex", "paragraph", "passage", "analyze", "evaluate"],
    "very_hard": ["assertion and reason", "match the following with four columns", "case study"],
}

BLOOM_KEYWORDS = {
    "remember": ["define", "what is", "who", "when", "list", "name", "state", "capital"],
    "understand": ["explain", "describe", "summarize", "interpret", "what does", "meaning"],
    "apply": ["solve", "calculate", "apply", "find", "determine", "132 :", "what will be"],
    "analyze": ["compare", "differentiate", "analyze", "why", "relationship", "related to"],
    "evaluate": ["evaluate", "justify", "assess", "which is correct", "which is incorrect"],
    "create": ["create", "design", "formulate", "construct"],
}
