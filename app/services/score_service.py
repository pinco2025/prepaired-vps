"""
Score calculation service — ported from the legacy Render backend.
"""

import json
import logging
import base64
import httpx
from pathlib import Path
from typing import Any, Dict

from app.core.config import settings

logger = logging.getLogger(__name__)

class ScoreService:
    def __init__(self):
        self.chapter_topics_map = self._load_chapter_topics()

    def _load_chapter_topics(self):
        try:
            # Assuming chapters.json is in the root backend directory
            base_path = Path(__file__).resolve().parent.parent.parent
            chapters_path = base_path / "chapters.json"
            if not chapters_path.exists():
                logger.warning(f"chapters.json not found at {chapters_path}")
                return {}

            with open(chapters_path, "r") as f:
                data = json.load(f)

            mapping = {}
            for subject, chapters in data.items():
                for chapter in chapters:
                    code = chapter.get("code")
                    topics = chapter.get("topics", {})
                    if code:
                        mapping[code] = topics
            return mapping
        except Exception as e:
            logger.error(f"Error loading chapters.json: {e}")
            return {}

    @staticmethod
    def _infer_section_type(name: str) -> str:
        """Derive question type from section name (e.g. 'Physics - Multi Correct' → 'MultiCorrect')."""
        n = (name or '').lower()
        if 'multi correct' in n:
            return 'MultiCorrect'
        if 'numerical' in n:
            return 'Numerical'
        if 'matrix match' in n:
            return 'MatrixMatch'
        return 'MCQ'

    def calculate_score(self, ppt_data: dict, response_data: dict) -> dict:
        """
        Calculates scores based on the provided PPT data and user response data.
        """
        # Process sections to get marking scheme and inferred question type
        sections_config = {}
        for section in ppt_data.get('sections', []):
            name = section.get('name')
            positive_marks = section.get('marksPerQuestion', 0)
            negative_marks = section.get('negagiveMarksPerQuestion', 0)
            if not negative_marks:
                 negative_marks = section.get('negativeMarksPerQuestion', 0)

            sections_config[name] = {
                'positive': positive_marks,
                'negative': negative_marks,
                'type': self._infer_section_type(name),
            }

        attempt_comparison = []
        section_scores = {}
        chapter_scores = {}

        # New metadata stats structure
        metadata_stats = {
            "correct": {"difficulty": {}, "relevance": {}, "scary": {}, "lengthy": {}, "topics": {}},
            "incorrect": {"difficulty": {}, "relevance": {}, "scary": {}, "lengthy": {}, "topics": {}},
            "unattempted": {"difficulty": {}, "relevance": {}, "scary": {}, "lengthy": {}, "topics": {}}
        }

        # Initialize score aggregators
        for sec_name in sections_config:
            section_scores[sec_name] = {
                'score': 0, 'correct': 0, 'incorrect': 0, 'unattempted': 0, 'partial': 0, 'total_questions': 0
            }

        questions = ppt_data.get('questions', [])

        # Total stats aggregators
        total_score = 0
        total_correct = 0
        total_incorrect = 0
        total_unattempted = 0
        total_questions_count = 0

        for q in questions:
            uuid = q.get('uuid')
            q_id = q.get('id')
            section_name = q.get('section')
            correct_ans = q.get('correctAnswer')

            tags = q.get('tags', {})
            chapter_tag = q.get('chapterCode')
            if not chapter_tag:
                chapter_tag = tags.get('tag2', 'Unknown')

            user_ans = response_data.get(uuid)

            status = 'Unattempted'
            marks = 0

            section_cfg = sections_config.get(section_name, {'positive': 0, 'negative': 0})

            if chapter_tag not in chapter_scores:
                chapter_scores[chapter_tag] = {
                    'score': 0, 'correct': 0, 'incorrect': 0, 'unattempted': 0, 'partial': 0, 'total_questions': 0
                }

            if section_name in section_scores:
                section_scores[section_name]['total_questions'] += 1
            chapter_scores[chapter_tag]['total_questions'] += 1
            total_questions_count += 1

            # Detect question type — explicit questionType takes precedence,
            # then fall back to the type inferred from the section name in the JSON.
            q_type = q.get('questionType') or (q.get('tags') or {}).get('type', '')
            is_multi_correct = (
                q_type == 'MultiCorrect'
                or section_cfg.get('type') == 'MultiCorrect'
            )

            if is_multi_correct:
                # div8 partial marking algorithm
                correct_set = set(str(correct_ans).upper().replace(' ', '').split(',')) if correct_ans else set()

                # User answer may arrive as a list (JSONB array) or comma-separated string
                if isinstance(user_ans, list):
                    user_set = set(a.upper().strip() for a in user_ans if a)
                elif user_ans:
                    user_set = set(str(user_ans).upper().replace(' ', '').split(','))
                else:
                    user_set = set()

                if not user_set:
                    status = 'Unattempted'
                    marks = 0
                elif user_set - correct_set:
                    # Any wrong option selected → full negative
                    status = 'Incorrect'
                    marks = section_cfg['negative']
                elif user_set == correct_set:
                    # All correct options selected, no wrong → full marks
                    status = 'Correct'
                    marks = section_cfg['positive']
                else:
                    # Partial: subset of correct options only, no wrong → +1 per correct selected
                    status = 'Partial'
                    marks = len(user_set & correct_set)
            else:
                # div1 (MCQ) and div2 (Integer): exact string comparison
                if user_ans is not None:
                    if str(user_ans).strip().lower() == str(correct_ans).strip().lower():
                        status = 'Correct'
                        marks = section_cfg['positive']
                    else:
                        status = 'Incorrect'
                        marks = section_cfg['negative']
                else:
                    status = 'Unattempted'
                    marks = 0

            if section_name in section_scores:
                section_scores[section_name]['score'] += marks
                if status == 'Correct':
                    section_scores[section_name]['correct'] += 1
                elif status == 'Incorrect':
                    section_scores[section_name]['incorrect'] += 1
                elif status == 'Partial':
                    section_scores[section_name]['partial'] += 1
                else:
                    section_scores[section_name]['unattempted'] += 1

            chapter_scores[chapter_tag]['score'] += marks
            if status == 'Correct':
                chapter_scores[chapter_tag]['correct'] += 1
            elif status == 'Incorrect':
                chapter_scores[chapter_tag]['incorrect'] += 1
            elif status == 'Partial':
                chapter_scores[chapter_tag]['partial'] += 1
            else:
                chapter_scores[chapter_tag]['unattempted'] += 1

            total_score += marks
            if status == 'Correct':
                total_correct += 1
            elif status == 'Incorrect':
                total_incorrect += 1
            elif status == 'Partial':
                total_correct += 1   # count partial as attempted-correct for totals
            else:
                total_unattempted += 1

            blunder = (status == 'Incorrect' and str(q.get('difficulty', '')).strip().upper() == 'E')

            attempt_comparison.append({
                "question_uuid": uuid,
                "question_id": q_id,
                "section": section_name,
                "chapter_tag": chapter_tag,
                "question_type": q_type or "MCQ",
                "user_response": user_ans,
                "correct_response": correct_ans,
                "status": status,
                "marks_awarded": marks,
                "blunder": blunder
            })

            bin_key = status.lower()

            def update_meta(field, val):
                if val is not None:
                    val_str = str(val)
                    metadata_stats[bin_key][field][val_str] = metadata_stats[bin_key][field].get(val_str, 0) + 1

            update_meta('difficulty', q.get('difficulty'))
            update_meta('relevance', q.get('jeeMainsRelevance'))
            update_meta('scary', q.get('scary'))
            update_meta('lengthy', q.get('lengthy'))

            topic_tags = q.get('topicTags')
            if topic_tags and isinstance(topic_tags, list):
                if chapter_tag and chapter_tag in self.chapter_topics_map:
                    chapter_topics = self.chapter_topics_map[chapter_tag]
                    for t_id in topic_tags:
                        t_name = chapter_topics.get(str(t_id))
                        if t_name:
                            update_meta('topics', f"{chapter_tag}-{t_id}")

        output = {}

        for key, value in ppt_data.items():
            if key != 'questions':
                output[key] = value

        section_order = {s.get('name'): i for i, s in enumerate(ppt_data.get('sections', []))}
        attempt_comparison.sort(key=lambda x: section_order.get(x['section'], 999))

        output["attempt_comparison"] = attempt_comparison
        output["section_scores"] = section_scores
        output["chapter_scores"] = chapter_scores
        output["metadata_stats"] = metadata_stats
        output["total_stats"] = {
            "total_score": total_score,
            "total_questions": total_questions_count,
            "total_attempted": total_correct + total_incorrect,
            "total_correct": total_correct,
            "total_wrong": total_incorrect,
            "total_unattempted": total_unattempted
        }

        return output

    async def push_to_github(self, data: dict, filename: str) -> str:
        """
        Pushes the data to a GitHub repository using the async HTTP client.
        Returns the URL of the pushed file.
        """
        if not settings.GITHUB_TOKEN or not settings.GITHUB_REPO:
            # If not configured, we might not want to upload in development, but raising error is original behavior
            # We will just log and return a dummy URL instead if not configured.
            logger.warning("GITHUB_TOKEN or GITHUB_REPO not set. Skipping GitHub push.")
            return "https://dummy_url_no_github_config_set"

        base_url = f"https://api.github.com/repos/{settings.GITHUB_REPO}/contents/{filename}"
        headers = {
            "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }

        content_str = json.dumps(data, indent=4)
        content_encoded = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")

        message = f"Add score results for {filename}"

        async with httpx.AsyncClient() as client:
            sha = None
            try:
                get_response = await client.get(base_url, headers=headers)
                if get_response.status_code == 200:
                    sha = get_response.json().get("sha")
            except Exception as e:
                logger.warning(f"Failed to check if file exists: {e}")

            payload = {
                "message": message,
                "content": content_encoded
            }
            if sha:
                payload["sha"] = sha

            response = await client.put(base_url, headers=headers, json=payload)
            response.raise_for_status()

            resp_data = response.json()
            return resp_data.get("content", {}).get("download_url")

score_service = ScoreService()
