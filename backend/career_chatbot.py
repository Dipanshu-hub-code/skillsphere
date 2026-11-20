"""
SkillSphere Rule-Based Candidate Career Assistant Engine.
Provides deterministic, fast, offline conversational guidance on resumes,
technical interviews, HR preparation, and platform navigation.
No external LLMs or third-party API dependencies.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KNOWLEDGE_BASE_PATH = Path(__file__).resolve().parent / "chatbot_knowledge.json"


class CareerChatbotEngine:
    def __init__(self, kb_path: Path | str | None = None) -> None:
        self.kb_path = Path(kb_path or KNOWLEDGE_BASE_PATH)
        self.categories: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self.synonyms: dict[str, str] = {}
        self.intent_map: dict[str, dict[str, Any]] = {}
        self.load_knowledge_base()

    def load_knowledge_base(self) -> None:
        try:
            if self.kb_path.exists():
                with open(self.kb_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.categories = data.get("categories", [])
                    self.entries = data.get("entries", [])
                    self.synonyms = data.get("synonyms", {})
                    self.intent_map = {e["intent"]: e for e in self.entries}
                logger.info("CareerChatbotEngine loaded %d entries from %s", len(self.entries), self.kb_path)
            else:
                logger.warning("Chatbot knowledge base file not found at %s", self.kb_path)
        except Exception as e:
            logger.error("Failed to load chatbot knowledge base: %s", e)

    def normalize_text(self, text: str) -> str:
        """
        Normalize text: lower-case, expand contractions, strip punctuation,
        apply synonym replacements, and collapse whitespace.
        """
        if not text:
            return ""

        t = text.lower().strip()

        # Contraction expansions
        contractions = {
            r"\bwhat's\b": "what is",
            r"\bhow's\b": "how is",
            r"\bwhere's\b": "where is",
            r"\bwho's\b": "who is",
            r"\bi'm\b": "i am",
            r"\byou're\b": "you are",
            r"\bwe're\b": "we are",
            r"\bthey're\b": "they are",
            r"\bit's\b": "it is",
            r"\bthat's\b": "that is",
            r"\bdon't\b": "do not",
            r"\bdoesn't\b": "does not",
            r"\bdidn't\b": "did not",
            r"\bcan't\b": "cannot",
            r"\bcouldn't\b": "could not",
            r"\bwon't\b": "will not",
            r"\bwouldn't\b": "would not",
            r"\bshouldn't\b": "should not",
            r"\bhasn't\b": "has not",
            r"\bhaven't\b": "have not",
        }
        for pattern, replacement in contractions.items():
            t = re.sub(pattern, replacement, t)

        # Remove special characters / punctuation except alphanumeric and spaces
        t = re.sub(r"[^\w\s]", " ", t)

        # Normalize whitespace
        tokens = t.split()

        # Apply synonym replacement
        normalized_tokens = []
        for token in tokens:
            normalized_tokens.append(self.synonyms.get(token, token))

        return " ".join(normalized_tokens)

    def extract_tokens(self, normalized_text: str) -> set[str]:
        return set(normalized_text.split())

    def calculate_match_score(self, query_norm: str, query_tokens: set[str], entry: dict[str, Any]) -> float:
        """
        Compute similarity score between user query and knowledge base entry.
        Combines pattern exact/fuzzy match, token overlap (Jaccard), and keyword hits.
        """
        patterns = entry.get("patterns", [])
        keywords = set(entry.get("keywords", []))

        best_pattern_score = 0.0
        for p in patterns:
            p_norm = self.normalize_text(p)
            if not p_norm:
                continue

            # Exact match
            if query_norm == p_norm:
                return 1.0

            # Substring match
            if p_norm in query_norm or query_norm in p_norm:
                sub_score = min(len(p_norm), len(query_norm)) / max(len(p_norm), len(query_norm))
                best_pattern_score = max(best_pattern_score, 0.75 + 0.20 * sub_score)

            # SequenceMatcher ratio
            ratio = difflib.SequenceMatcher(None, query_norm, p_norm).ratio()
            best_pattern_score = max(best_pattern_score, ratio)

            # Token overlap Jaccard
            p_tokens = self.extract_tokens(p_norm)
            if p_tokens:
                intersection = query_tokens.intersection(p_tokens)
                jaccard = len(intersection) / len(query_tokens.union(p_tokens))
                best_pattern_score = max(best_pattern_score, jaccard)

        # Keyword density score
        keyword_score = 0.0
        if keywords:
            matched_keywords = query_tokens.intersection(keywords)
            keyword_score = len(matched_keywords) / len(keywords)

        # Weighted blend
        final_score = (0.70 * best_pattern_score) + (0.30 * min(keyword_score * 1.5, 1.0))
        return min(max(final_score, 0.0), 1.0)

    def is_resume_query(self, query_norm: str) -> bool:
        """
        Check if user query is asking to inspect their personal uploaded resume data.
        Distinguishes general resume advice ('how to improve resume') from personal resume inspection ('what skills are in my resume').
        """
        advice_triggers = [
            "how do i improve", "how can i improve", "suggest improvements",
            "what should i change", "is my cv good enough", "how to format",
            "make my resume better", "tips for resume", "ats systems",
            "common resume mistakes", "describe academic projects", "how should i describe"
        ]
        if any(tr in query_norm for tr in advice_triggers):
            return False

        inspection_triggers = [
            "in my resume", "from my resume", "on my resume", "according to my resume",
            "skills mentioned in my", "skills in my resume", "my skills from resume",
            "programming languages do i know", "programming languages in my",
            "projects have i listed", "projects in my resume", "my projects",
            "technologies have i mentioned", "technologies in my resume",
            "explain my resume", "summarize my resume", "what is on my resume",
            "what skills are mentioned", "what skills do i have in my resume"
        ]
        return any(tr in query_norm for tr in inspection_triggers)

    def handle_resume_query(
        self,
        query_norm: str,
        resume_row: Any | None,
        candidate_name: str = "Candidate"
    ) -> dict[str, Any]:
        """
        Generate factual, customized response based on candidate's parsed resume.
        Never fabricates skills or qualifications.
        """
        if resume_row is not None and not isinstance(resume_row, dict):
            try:
                resume_row = dict(resume_row)
            except Exception:
                pass

        if not resume_row or not (resume_row.get("extracted_text") or resume_row.get("parsed_profile_json")):
            return {
                "response": (
                    "**No Resume Found on File**\n\n"
                    "I don't see an uploaded resume associated with your candidate profile yet.\n\n"
                    "To get personalized resume analysis and skill recommendations:\n"
                    "1. Go to **My Profile** from the left navigation menu.\n"
                    "2. In the **Your resume** section, click **'Choose a file'** and upload your document (PDF, Word, or image up to 10 MB).\n"
                    "3. Once uploaded, return here and ask me *\"What skills are mentioned in my resume?\"* or *\"What projects have I listed?\"*."
                ),
                "intent": "resume_query_missing",
                "confidence": 0.95,
                "suggested_next": ["How do I upload my resume?", "How do I improve my resume?"]
            }

        parsed_profile: dict[str, Any] = {}
        try:
            raw_json = resume_row["parsed_profile_json"] or "{}"
            parsed_profile = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        except Exception:
            parsed_profile = {}

        extracted_text = (resume_row["extracted_text"] or "").strip()
        filename = resume_row.get("filename", "resume")
        skills = parsed_profile.get("skills", [])
        experience = parsed_profile.get("experience_years") or parsed_profile.get("experience")
        location = parsed_profile.get("location", "")
        summary = parsed_profile.get("summary", "")

        # 1. Asking about Programming Languages & Skills
        if any(w in query_norm for w in ["skill", "skills", "language", "languages", "tech", "technologies", "know"]):
            if skills:
                skill_list = "\n".join(f"- **{s}**" for s in skills)
                resp = (
                    f"### Skills Detected in Your Resume (`{filename}`)\n\n"
                    f"Based on your uploaded resume, here are the technical skills and technologies recognized in your profile:\n\n"
                    f"{skill_list}\n\n"
                    f"**Next Steps:**\n"
                    f"- Make sure to check the **Jobs** tab to see your automated **Skill Match Percentage** against open recruiter postings!\n"
                    f"- If you have acquired new skills recently, update your resume and re-upload it in **My Profile**."
                )
            else:
                resp = (
                    f"### Skills in Your Resume (`{filename}`)\n\n"
                    f"I scanned your resume text, but did not detect a clearly delineated Technical Skills section.\n\n"
                    f"**Recommendation:** Add a prominent **Technical Skills** heading grouped by category (e.g. Languages, Frameworks, Databases) so both recruiters and automated ATS screeners can index your competencies."
                )
            return {
                "response": resp,
                "intent": "candidate_resume_skills",
                "confidence": 0.98,
                "suggested_next": ["How should I describe my technical skills?", "How do I make my resume suitable for ATS systems?"]
            }

        # 2. Asking about Projects
        if any(w in query_norm for w in ["project", "projects", "academic", "portfolio"]):
            project_match = re.search(r"(?:projects?|academic\s+projects?)[\s:\-]+([\s\S]{20,1000}?)(?:\n\s*[A-Z][a-zA-Z\s]{3,20}\n|\Z)", extracted_text, re.I)
            if project_match:
                snippet = project_match.group(1).strip()
                resp = (
                    f"### Projects Identified in Your Resume\n\n"
                    f"Here is an excerpt of the projects section found in `{filename}`:\n\n"
                    f"> {snippet[:500]}...\n\n"
                    f"**Advice for Explaining Projects in Interviews:**\n"
                    f"- Structure your explanation using: **Problem -> Technical Architecture -> Your Exact Role -> Measurable Outcome**.\n"
                    f"- Be prepared to discuss technical trade-offs, database indexing, and how you handled errors or edge cases."
                )
            else:
                resp = (
                    f"### Projects in Your Resume (`{filename}`)\n\n"
                    f"I did not find a dedicated 'Projects' section in your uploaded document text.\n\n"
                    f"**Recommendation:** Highlighting 2 to 3 practical or academic projects is one of the strongest factors for junior and mid-level applications. Include the tech stack, your key contributions, and GitHub or demo links."
                )
            return {
                "response": resp,
                "intent": "candidate_resume_projects",
                "confidence": 0.95,
                "suggested_next": ["How should I describe academic projects?", "How do I build a good GitHub portfolio?"]
            }

        # 3. Asking about Summary
        if any(w in query_norm for w in ["summary", "objective", "bio", "profile"]):
            if summary:
                resp = (
                    f"### Your Current Resume Summary\n\n"
                    f"*\"{summary}\"*\n\n"
                    f"**Tips to Elevate This Summary:**\n"
                    f"1. Ensure it states your target title and years/level of practical experience.\n"
                    f"2. Mention your top 3 core technical specializations.\n"
                    f"3. Conclude with a strong value proposition (e.g. *'Passionate about scalable async backends and clean architectures'*)."
                )
            else:
                resp = (
                    f"### Professional Summary (`{filename}`)\n\n"
                    f"Your uploaded resume does not currently have an explicit summary paragraph.\n\n"
                    f"**Suggested 3-sentence summary template:**\n"
                    f"*\"Motivated Software Developer with strong foundational knowledge in Python, SQL, and modern web frameworks. Experienced in building responsive web applications and REST APIs with a focus on code quality and performance. Eager to contribute to innovative development teams at SkillSphere.\"*"
                )
            return {
                "response": resp,
                "intent": "candidate_resume_summary",
                "confidence": 0.95,
                "suggested_next": ["What should I include in my professional summary?", "How do I improve my resume?"]
            }

        # General Resume Overview
        skills_str = ", ".join(skills[:8]) if skills else "No specific skills tagged yet"
        exp_str = f"{experience} years" if experience else "Not specified"
        loc_str = location if location else "Not specified"

        resp = (
            f"### Overview of Your Uploaded Resume (`{filename}`)\n\n"
            f"- **Candidate Name**: {candidate_name}\n"
            f"- **Detected Skills**: {skills_str}\n"
            f"- **Experience Detected**: {exp_str}\n"
            f"- **Location**: {loc_str}\n\n"
            f"**What would you like to explore next?**\n"
            f"- *\"What skills are mentioned in my resume?\"*\n"
            f"- *\"What projects have I listed?\"*\n"
            f"- *\"How can I improve my professional summary?\"*"
        )
        return {
            "response": resp,
            "intent": "candidate_resume_overview",
            "confidence": 0.95,
            "suggested_next": ["What skills are mentioned in my resume?", "How do I make my resume suitable for ATS systems?"]
        }

    def is_follow_up_phrase(self, query_norm: str) -> bool:
        phrases = [
            "tell me more", "more details", "elaborate", "can you elaborate",
            "give me an example", "show an example", "code example", "sample",
            "explain this again", "explain again", "say that again", "simpler",
            "rephrase", "what about the second one", "next one", "what else",
            "example", "more", "again", "details"
        ]
        return any(query_norm == p or query_norm.startswith(p) for p in phrases)

    def handle_follow_up(self, query_norm: str, last_intent: str | None) -> dict[str, Any] | None:
        """Handle conversational follow-ups like 'tell me more', 'give me an example', 'explain again'."""
        if not self.is_follow_up_phrase(query_norm):
            return None

        if not last_intent or last_intent not in self.intent_map:
            return {
                "response": "Could you please specify which topic you'd like more details or an example for? For instance, try asking: *'Explain OOP with an example'* or *'Tell me more about database normalization'*.",
                "intent": "follow_up_unspecified",
                "confidence": 0.85,
                "suggested_next": ["Explain OOP concepts", "What is database normalization?", "How should I introduce myself in an interview?"]
            }

        entry = self.intent_map[last_intent]
        follow_ups = entry.get("follow_ups", {})
        title = entry.get("title", "this topic")

        # 1. Request for examples
        if any(w in query_norm for w in ["example", "sample", "code", "show"]):
            if "example" in follow_ups:
                return {
                    "response": f"### Practical Example for {title}:\n\n{follow_ups['example']}",
                    "intent": f"{last_intent}_example",
                    "confidence": 0.92,
                    "suggested_next": entry.get("suggested_next", [])
                }
            else:
                return {
                    "response": f"### Practical Application of {title}:\n\nIn real-world projects, {title} is applied during system architecture and code design to maintain high modularity and fault tolerance.\n\n{follow_ups.get('more', '')}",
                    "intent": f"{last_intent}_example",
                    "confidence": 0.90,
                    "suggested_next": entry.get("suggested_next", [])
                }

        # 2. Request for more details / elaborate
        if any(w in query_norm for w in ["tell me more", "more", "elaborate", "expand", "details", "what else"]):
            if "more" in follow_ups:
                return {
                    "response": f"### Additional In-Depth Insights on {title}:\n\n{follow_ups['more']}",
                    "intent": f"{last_intent}_more",
                    "confidence": 0.92,
                    "suggested_next": entry.get("suggested_next", [])
                }
            else:
                return {
                    "response": f"### Further Guidelines on {title}:\n\nMake sure to review the core principles and practice applying them in sample interview questions or real-world coding problems.\n\n{entry.get('response', '')}",
                    "intent": f"{last_intent}_more",
                    "confidence": 0.90,
                    "suggested_next": entry.get("suggested_next", [])
                }

        # 3. Request to explain again / simpler
        if any(w in query_norm for w in ["again", "simpler", "rephrase", "repeat", "simple"]):
            first_paragraph = entry.get("response", "").split("\n\n")[0]
            return {
                "response": (
                    f"### Key Summary for {title}:\n\n"
                    f"{first_paragraph}\n\n"
                    f"{follow_ups.get('more', '')}"
                ),
                "intent": f"{last_intent}_rephrase",
                "confidence": 0.90,
                "suggested_next": entry.get("suggested_next", [])
            }

        return None

    def process_message(
        self,
        message: str,
        resume_row: Any | None = None,
        last_intent: str | None = None,
        candidate_name: str = "Candidate"
    ) -> dict[str, Any]:
        """
        Main pipeline:
        1. Normalize user message.
        2. Check for personal resume lookup.
        3. Check for context-aware follow-ups.
        4. Match against Knowledge Base entries with score thresholding.
        5. Fallback if confidence is low.
        """
        raw_msg = (message or "").strip()
        if not raw_msg:
            return {
                "response": "Please type a question or choose from the suggested career topics below.",
                "intent": "empty_query",
                "confidence": 1.0,
                "suggested_next": ["Improve my resume", "Explain my technical skills", "How do I apply for jobs?"]
            }

        query_norm = self.normalize_text(raw_msg)
        query_tokens = self.extract_tokens(query_norm)

        # Step 1: Check for resume-specific inspection
        if self.is_resume_query(query_norm):
            return self.handle_resume_query(query_norm, resume_row, candidate_name)

        # Step 2: Check for context follow-ups
        follow_up_res = self.handle_follow_up(query_norm, last_intent)
        if follow_up_res:
            return follow_up_res

        # Step 3: Match against knowledge base
        best_entry: dict[str, Any] | None = None
        best_score = 0.0

        for entry in self.entries:
            score = self.calculate_match_score(query_norm, query_tokens, entry)
            if score > best_score:
                best_score = score
                best_entry = entry

        # Confidence Threshold: 0.38
        if best_entry and best_score >= 0.38:
            return {
                "response": best_entry["response"],
                "intent": best_entry["intent"],
                "confidence": round(best_score, 2),
                "suggested_next": best_entry.get("suggested_next", [
                    "Improve my resume",
                    "Help me prepare for a technical interview",
                    "How do I apply for jobs?"
                ])
            }

        # Step 4: Graceful Fallback
        fallback_resp = (
            "I'm not completely certain I understood your question. Could you please rephrase it, or explore one of these popular topics?\n\n"
            "- **Resume Improvement**: *\"How do I improve my resume?\"* or *\"How do I make my resume ATS-friendly?\"*\n"
            "- **Technical Prep**: *\"Explain OOP concepts\"*, *\"What is Python?\"*, or *\"What is SQL?\"*\n"
            "- **HR Interview**: *\"How should I introduce myself?\"* or *\"What are common weaknesses?\"*\n"
            "- **SkillSphere Help**: *\"How do I apply for a job?\"* or *\"How do technical assessments work?\"*"
        )
        return {
            "response": fallback_resp,
            "intent": "fallback_unknown",
            "confidence": round(best_score, 2),
            "suggested_next": [
                "Improve my resume",
                "Explain my technical skills",
                "Prepare me for an HR interview",
                "Help me prepare for a technical interview",
                "How do I prepare for placements?",
                "How do I apply for jobs?"
            ]
        }


# Singleton engine instance
career_chatbot = CareerChatbotEngine()
