import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from backend import main


class SkillSphereApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name)
        main.DATA_DIR = data_dir
        main.UPLOAD_DIR = data_dir / "uploads"
        main.QUESTION_BANK_UPLOAD_DIR = data_dir / "question_bank_uploads"
        main.DB_PATH = data_dir / "test.sqlite3"
        main.initialize_database()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def login(self, email="candidate@skillsphere.dev", password="Demo123!"):
        response = self.client.post("/api/auth/login", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def create_job_question_bank(self, job_id, technology="Python", count=20, headers=None):
        if headers is not None:
            for index in range(count):
                response = self.client.post(
                    f"/api/jobs/{job_id}/questions",
                    headers=headers,
                    json={
                        "technology": technology,
                        "question": f"How does {technology} feature number {index + 1} work?",
                        "options": [f"Option {option} for question {index + 1}" for option in range(4)],
                        "correct_index": index % 4,
                        "difficulty": ("Easy", "Medium", "Hard")[index % 3],
                        "active": True,
                    },
                )
                self.assertEqual(response.status_code, 201, response.text)
            return
        with main.connect() as db:
            recruiter_id = db.execute(
                "SELECT recruiter_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()["recruiter_id"]
            db.executemany(
                """INSERT INTO job_questions
                (job_id, owner_id, technology, question, options_json, correct_index,
                 difficulty, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                [
                    (
                        job_id, recruiter_id, technology,
                        f"How does {technology} feature number {index + 1} work?",
                        json.dumps([f"Option {option} for question {index + 1}" for option in range(4)]),
                        index % 4, ("Easy", "Medium", "Hard")[index % 3], main.now(), main.now(),
                    )
                    for index in range(count)
                ],
            )

    def create_pdf_upload(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        pdf = BytesIO()
        writer.write(pdf)
        return pdf.getvalue()

    def apply_with_match(
        self, required_skills, candidate_skills, resume_text=None, job_index=0, question_bank=None
    ):
        candidate = self.login()
        job_id = self.client.get("/api/jobs", headers=candidate).json()["items"][job_index]["id"]
        with main.connect() as db:
            db.execute("UPDATE jobs SET skills_json = ? WHERE id = ?", (json.dumps(required_skills), job_id))
            db.execute(
                """INSERT INTO resumes
                (candidate_id, filename, storage_path, content_type, extracted_text,
                 parsed_profile_json, processing_status, uploaded_at)
                VALUES (1, 'resume.pdf', 'private.pdf', 'application/pdf', ?, ?, 'completed', ?)
                ON CONFLICT(candidate_id) DO UPDATE SET extracted_text = excluded.extracted_text,
                parsed_profile_json = excluded.parsed_profile_json""",
                (
                    resume_text or " ".join(candidate_skills),
                    json.dumps({"skills": candidate_skills}),
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        for technology, count in (question_bank or {}).items():
            self.create_job_question_bank(job_id, technology, count)
        response = self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate)
        self.assertEqual(response.status_code, 201, response.text)
        return candidate, self.login("recruiter@skillsphere.dev"), response.json()

    def send_assessment(self, application_id):
        with patch.dict(os.environ, {"SKILLSPHERE_SMTP_HOST": ""}):
            response = self.client.post(
                f"/api/applications/{application_id}/assessment/send",
                headers=self.login("recruiter@skillsphere.dev"),
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["delivery"], "development")
        with main.connect() as db:
            candidate_id = db.execute(
                "SELECT candidate_id FROM applications WHERE id = ?", (application_id,)
            ).fetchone()["candidate_id"]
            candidate_email = db.execute(
                "SELECT email FROM users WHERE id = ?", (candidate_id,)
            ).fetchone()["email"]
        candidate_notifications = self.client.get(
            "/api/notifications", headers=self.login(candidate_email)
        ).json()["items"]
        candidate_link = next(
            item for item in candidate_notifications
            if item["type"] == "assessment_sent"
            and item["related_application_id"] == application_id
            and item["action_url"]
        )
        recruiter_notifications = self.client.get(
            "/api/notifications", headers=self.login("recruiter@skillsphere.dev")
        ).json()["items"]
        self.assertFalse(any(
            item["related_application_id"] == application_id and item["action_url"]
            for item in recruiter_notifications
        ))
        self.assertNotIn("assessment_url", response.json())
        return response.json(), parse_qs(urlparse(candidate_link["action_url"]).query)["assessment"][0]

    def complete_assessment(self, application, correct_count):
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])
        welcome = self.client.get(f"/api/assessments/{token}")
        self.assertEqual(welcome.status_code, 200, welcome.text)
        self.assertEqual(welcome.json()["title"], "Round 1: Technical MCQ Test")
        self.assertEqual(welcome.json()["pass_percentage"], main.ROUND1_PASS_PERCENTAGE)
        proctoring = self.start_assessment_proctoring(token)
        self.assertTrue(proctoring["started"])
        started = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(started.status_code, 200, started.text)
        with main.connect() as db:
            question_rows = db.execute(
                """SELECT id, correct_index FROM assessment_questions
                WHERE assessment_id = (SELECT id FROM assessments WHERE application_id = ?)
                ORDER BY question_order""",
                (application["id"],),
            ).fetchall()
            answers = {
                row["id"]: row["correct_index"] if index < correct_count else (row["correct_index"] + 1) % 4
                for index, row in enumerate(question_rows)
            }
        response = self.client.post(
            f"/api/assessments/{token}/submit", json={"answers": answers}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return token, response.json()

    def start_assessment_proctoring(self, token, consent=True, camera=True):
        response = self.client.post(
            f"/api/assessments/{token}/proctoring/start",
            json={"consent_given": consent, "camera_permission_granted": camera},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def create_interview_application(self, candidate_email="candidate@skillsphere.dev", job_index=0):
        candidate = self.login(candidate_email)
        with main.connect() as db:
            candidate_id = db.execute(
                "SELECT id FROM users WHERE email = ?", (candidate_email,)
            ).fetchone()["id"]
            job = db.execute(
                "SELECT id FROM jobs WHERE recruiter_id = 2 ORDER BY id LIMIT 1 OFFSET ?",
                (job_index,),
            ).fetchone()
            cursor = db.execute(
                """INSERT INTO applications (candidate_id, job_id, status, applied_at)
                VALUES (?, ?, 'Applied', ?)""",
                (candidate_id, job["id"], main.now()),
            )
        return candidate, {"id": cursor.lastrowid, "job_id": job["id"]}

    def interview_payload(self, **overrides):
        payload = {
            "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=3)).replace(
                second=0, microsecond=0
            ).isoformat(),
            "timezone": "UTC",
            "duration_minutes": 45,
            "meeting_platform": "Google Meet",
            "meeting_url": "https://meet.google.com/example-room",
            "recruiter_message": "Please bring a portfolio.",
        }
        payload.update(overrides)
        return payload

    def test_seeded_accounts_authenticate_without_exposing_password_hash(self):
        headers = self.login()
        response = self.client.get("/api/me", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "candidate")
        self.assertNotIn("password_hash", response.json())
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_registration_rejects_duplicate_email_and_admin_role(self):
        payload = {"name": "New Member", "email": "new@example.com", "password": "LongEnough9!", "role": "candidate"}
        self.assertEqual(self.client.post("/api/auth/register", json=payload).status_code, 201)
        self.assertEqual(self.client.post("/api/auth/register", json=payload).status_code, 409)
        payload["email"] = "other@example.com"
        payload["role"] = "admin"
        self.assertEqual(self.client.post("/api/auth/register", json=payload).status_code, 422)

    def test_jobs_can_be_filtered_and_recruiter_can_only_see_own(self):
        candidate = self.login()
        response = self.client.get(
            "/api/jobs?q=designer&location=New%20York&experience=years&skill=Figma",
            headers=candidate,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["items"]), 1)
        self.assertEqual(response.json()["items"][0]["title"], "Product Designer")
        self.assertEqual(self.client.get("/api/jobs?mine=true", headers=candidate).status_code, 403)
        self.assertEqual(self.client.get("/api/jobs?sort_by=salary", headers=candidate).status_code, 422)
        recruiter = self.login("recruiter@skillsphere.dev")
        own_jobs = self.client.get("/api/jobs?mine=true", headers=recruiter)
        self.assertEqual(own_jobs.status_code, 200)
        self.assertTrue(all(job["recruiter_id"] == 2 for job in own_jobs.json()["items"]))

    def test_candidate_cannot_apply_without_resume_or_submit_duplicate(self):
        candidate = self.login()
        job_id = self.client.get("/api/jobs", headers=candidate).json()["items"][0]["id"]
        response = self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate)
        self.assertEqual(response.status_code, 409)

        with main.connect() as db:
            db.execute(
                """INSERT INTO resumes
                (candidate_id, filename, storage_path, content_type, parsed_profile_json, processing_status, uploaded_at)
                VALUES (1, 'resume.pdf', 'private.pdf', 'application/pdf', '{}', 'completed', '2026-01-01T00:00:00+00:00')"""
            )
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate).status_code, 201)
        duplicate = self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate)
        self.assertEqual(duplicate.status_code, 409)

    def test_recruiter_cannot_update_an_application_for_another_team(self):
        candidate = self.login()
        second_recruiter = self.client.post(
            "/api/auth/register",
            json={"name": "Other Recruiter", "email": "other@company.com", "password": "LongEnough9!", "role": "recruiter"},
        )
        self.assertEqual(second_recruiter.status_code, 201)
        other_headers = {"Authorization": f"Bearer {second_recruiter.json()['access_token']}"}
        posted_job = self.client.post(
            "/api/jobs",
            headers=other_headers,
            json={
                "title": "Community Manager", "company": "Other Team",
                "description": "Help our team build a welcoming community for our customers.",
                "location": "Remote", "experience": "2–4 years", "job_type": "Full-time",
                "salary": "$75k", "skills": ["Communication", "Community"],
            },
        )
        self.assertEqual(posted_job.status_code, 201, posted_job.text)
        job_id = posted_job.json()["id"]
        resume_path = main.UPLOAD_DIR / "candidate-resume.pdf"
        resume_path.write_bytes(b"%PDF-1.7\nsample")
        with main.connect() as db:
            db.execute(
                """INSERT INTO resumes
                (candidate_id, filename, storage_path, content_type, parsed_profile_json, processing_status, uploaded_at)
                VALUES (1, 'resume.pdf', ?, 'application/pdf', '{}', 'completed', '2026-01-01T00:00:00+00:00')""",
                (str(resume_path),),
            )
        application = self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate).json()
        recruiter = self.login("recruiter@skillsphere.dev")
        changed = self.client.patch(
            f"/api/applications/{application['id']}/status",
            headers=recruiter,
            json={"status": "Shortlisted"},
        )
        self.assertEqual(changed.status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/applications/{application['id']}/resume", headers=recruiter).status_code,
            404,
        )
        changed = self.client.patch(
            f"/api/applications/{application['id']}/status",
            headers=other_headers,
            json={"status": "Shortlisted"},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["status"], "Shortlisted")
        resume_response = self.client.get(f"/api/applications/{application['id']}/resume", headers=other_headers)
        self.assertEqual(resume_response.status_code, 200)
        self.assertEqual(resume_response.content, b"%PDF-1.7\nsample")
        invalid = self.client.patch(
            f"/api/applications/{application['id']}/status",
            headers=other_headers,
            json={"status": "Make offer"},
        )
        self.assertEqual(invalid.status_code, 422)

    def test_resume_upload_rejects_unsupported_and_corrupt_file_signatures(self):
        candidate = self.login()
        unsupported = self.client.post(
            "/api/resume/upload", headers=candidate,
            files={"file": ("resume.txt", b"not a resume", "text/plain")},
        )
        self.assertEqual(unsupported.status_code, 415)
        corrupt_pdf = self.client.post(
            "/api/resume/upload", headers=candidate,
            files={"file": ("resume.pdf", b"%PDF-1.7\ninvalid", "application/pdf")},
        )
        self.assertEqual(corrupt_pdf.status_code, 422)
        corrupt_image = self.client.post(
            "/api/resume/upload", headers=candidate,
            files={"file": ("resume.png", b"\x89PNG\r\n\x1a\nnot an image", "image/png")},
        )
        self.assertEqual(corrupt_image.status_code, 422)
        too_large = self.client.post(
            "/api/resume/upload", headers=candidate,
            files={"file": ("resume.pdf", b"%PDF-" + b"x" * (10 * 1024 * 1024 + 1), "application/pdf")},
        )
        self.assertEqual(too_large.status_code, 413)
        no_resume = self.client.get("/api/resume", headers=candidate)
        self.assertEqual(no_resume.status_code, 200)
        self.assertIsNone(no_resume.json())

    def test_text_pdf_is_parsed_and_match_score_explains_skill_gaps(self):
        candidate = self.login()
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }))
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 14 Tf 50 700 Td (Alex Morgan alex@example.com Figma UX research) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        pdf = BytesIO()
        writer.write(pdf)

        uploaded = self.client.post(
            "/api/resume/upload", headers=candidate,
            files={"file": ("resume.pdf", pdf.getvalue(), "application/pdf")},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertEqual(uploaded.json()["processing_status"], "completed")
        self.assertEqual(uploaded.json()["parsed_profile"]["email"], "alex@example.com")
        self.assertNotIn("storage_path", uploaded.json())
        self.assertNotIn("extracted_text", uploaded.json())
        job = self.client.get("/api/jobs?q=Product%20Designer", headers=candidate).json()["items"][0]
        match = self.client.get(f"/api/matches/{job['id']}", headers=candidate)
        self.assertEqual(match.status_code, 200)
        self.assertEqual(match.json()["matched_skills"], ["Figma", "UX research"])
        self.assertEqual(match.json()["missing_skills"], ["Prototyping", "Design systems"])
        self.assertEqual(match.json()["match_percentage"], 50)

    def test_roles_are_enforced_on_admin_analytics(self):
        candidate = self.login()
        self.assertEqual(self.client.get("/api/admin/analytics", headers=candidate).status_code, 403)
        admin = self.login("admin@skillsphere.dev")
        report = self.client.get("/api/admin/analytics", headers=admin)
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["users"], 3)

    def test_application_history_notes_and_notifications_are_scoped(self):
        candidate, recruiter, application = self.apply_with_match(["Python", "SQL"], ["Python"])
        changed = self.client.patch(
            f"/api/applications/{application['id']}/status",
            headers=recruiter,
            json={"status": "Under Review", "note": "Resume reviewed"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        candidate_history = self.client.get(
            f"/api/applications/{application['id']}/history", headers=candidate
        ).json()["items"]
        recruiter_history = self.client.get(
            f"/api/applications/{application['id']}/history", headers=recruiter
        ).json()["items"]
        self.assertEqual([item["status"] for item in candidate_history], ["Applied", "Under Review"])
        self.assertTrue(all(not item["note"] for item in candidate_history))
        self.assertEqual(recruiter_history[-1]["note"], "Resume reviewed")

        saved = self.client.patch(
            f"/api/applications/{application['id']}/notes",
            headers=recruiter,
            json={"note": "Follow up next week"},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            self.client.patch(
                f"/api/applications/{application['id']}/notes",
                headers=candidate,
                json={"note": "Should not be allowed"},
            ).status_code,
            403,
        )

        own_notifications = self.client.get("/api/notifications", headers=candidate).json()
        team_notifications = self.client.get("/api/notifications", headers=recruiter).json()
        self.assertTrue(any(item["type"] == "application_submitted" for item in own_notifications["items"]))
        self.assertTrue(any(item["type"] == "new_application" for item in team_notifications["items"]))
        self.assertEqual(
            self.client.patch(
                f"/api/notifications/{team_notifications['items'][0]['id']}/read",
                headers=candidate,
                json={"is_read": True},
            ).status_code,
            404,
        )
        self.assertGreaterEqual(
            self.client.get("/api/notifications/unread-count", headers=candidate).json()["unread_count"],
            1,
        )
        self.assertGreaterEqual(
            self.client.patch("/api/notifications/read-all", headers=candidate).json()["updated"],
            1,
        )

    def test_admin_analytics_supports_weekly_and_custom_date_ranges(self):
        _, _, application = self.apply_with_match(["Python", "SQL"], ["Python"])
        with main.connect() as db:
            db.execute(
                "UPDATE applications SET applied_at = ? WHERE id = ?",
                ("2024-02-14T12:00:00+00:00", application["id"]),
            )
        admin = self.login("admin@skillsphere.dev")
        report = self.client.get(
            "/api/admin/analytics?report_range=custom&start_date=2024-02-14&end_date=2024-02-14",
            headers=admin,
        )
        self.assertEqual(report.status_code, 200, report.text)
        weekly = report.json()["weekly"]
        self.assertEqual(weekly["applications"], 1)
        self.assertEqual(weekly["start_date"], "2024-02-14")
        self.assertEqual(weekly["end_date"], "2024-02-14")
        self.assertEqual(weekly["daily_trend"], [{"date": "2024-02-14", "applications": 1}])
        self.assertEqual(
            self.client.get(
                "/api/admin/analytics?report_range=custom&start_date=not-a-date&end_date=2024-02-14",
                headers=admin,
            ).status_code,
            422,
        )

    def test_candidate_assessment_dashboard_hides_recruiter_review_data(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        sent, token = self.send_assessment(application["id"])
        self.assertEqual(sent["status"], "Sent")
        candidate_dashboard = self.client.get("/api/assessments", headers=candidate)
        self.assertEqual(candidate_dashboard.status_code, 200)
        self.assertEqual(candidate_dashboard.json()["items"][0]["assessment_status"], "Sent")
        self.assertNotIn("correct_index", candidate_dashboard.text)
        self.assertEqual(self.client.get("/api/assessment-reviews/1", headers=candidate).status_code, 403)
        self.start_assessment_proctoring(token)
        started = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(started.status_code, 200, started.text)
        with main.connect() as db:
            answers = {
                row["id"]: row["correct_index"]
                for row in db.execute(
                    """SELECT id, correct_index FROM assessment_questions
                    WHERE assessment_id = (SELECT id FROM assessments WHERE application_id = ?)""",
                    (application["id"],),
                )
            }
        with patch.dict(os.environ, {"SKILLSPHERE_ASSESSMENT_SHOW_SCORE": "false"}):
            submitted = self.client.post(
                f"/api/assessments/{token}/submit", json={"answers": answers}
            )
            self.assertEqual(submitted.status_code, 200, submitted.text)
            self.assertIsNone(submitted.json()["score"])
            hidden_dashboard = self.client.get("/api/assessments", headers=candidate).json()["items"][0]
            hidden_application = next(
                item for item in self.client.get("/api/applications", headers=candidate).json()["items"]
                if item["id"] == application["id"]
            )
        self.assertIsNone(hidden_dashboard["score"])
        self.assertIsNone(hidden_application["assessment_score"])
        recruiter_dashboard = self.client.get("/api/assessments", headers=recruiter)
        self.assertEqual(recruiter_dashboard.status_code, 200)
        self.assertEqual(recruiter_dashboard.json()["items"][0]["candidate_email"], "candidate@skillsphere.dev")
        self.assertEqual(self.client.get(f"/api/assessments/{token}").status_code, 404)

    def test_admin_can_disable_accounts_and_close_job_listings(self):
        admin = self.login("admin@skillsphere.dev")
        candidate = self.login()
        self.assertEqual(
            self.client.patch("/api/admin/users/3/status", headers=admin, json={"active": False}).status_code,
            409,
        )
        disabled = self.client.patch("/api/admin/users/1/status", headers=admin, json={"active": False})
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(
            self.client.post("/api/auth/login", json={"email": "candidate@skillsphere.dev", "password": "Demo123!"}).status_code,
            401,
        )
        self.assertEqual(self.client.get("/api/jobs", headers=candidate).status_code, 401)
        self.assertEqual(self.client.patch("/api/admin/users/1/status", headers=admin, json={"active": True}).status_code, 200)

        job = self.client.get("/api/jobs", headers=admin).json()["items"][0]
        closed = self.client.patch(f"/api/admin/jobs/{job['id']}/status", headers=admin, json={"status": "Closed"})
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.json()["status"], "Closed")
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}", headers=admin).status_code, 404)

    def test_candidate_can_withdraw_and_reapply_but_not_after_interview(self):
        candidate = self.login()
        job_id = self.client.get("/api/jobs", headers=candidate).json()["items"][0]["id"]
        with main.connect() as db:
            db.execute(
                """INSERT INTO resumes
                (candidate_id, filename, storage_path, content_type, parsed_profile_json, processing_status, uploaded_at)
                VALUES (1, 'resume.pdf', 'private.pdf', 'application/pdf', '{}', 'completed', '2026-01-01T00:00:00+00:00')"""
            )
        application = self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate).json()
        withdrawn = self.client.patch(f"/api/applications/{application['id']}/withdraw", headers=candidate)
        self.assertEqual(withdrawn.status_code, 200)
        self.assertEqual(withdrawn.json()["status"], "Withdrawn")
        reapplied = self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate)
        self.assertEqual(reapplied.status_code, 201)
        recruiter = self.login("recruiter@skillsphere.dev")
        shortlisted = self.client.patch(
            f"/api/applications/{application['id']}/status",
            headers=recruiter,
            json={"status": "Shortlisted"},
        )
        self.assertEqual(shortlisted.status_code, 200, shortlisted.text)
        self.assertEqual(
            self.client.patch(
                f"/api/applications/{application['id']}/status",
                headers=recruiter,
                json={"status": "Interview"},
            ).status_code,
            200,
        )
        blocked = self.client.patch(f"/api/applications/{application['id']}/withdraw", headers=candidate)
        self.assertEqual(blocked.status_code, 409)

    def test_interview_room_access_and_shared_workspace(self):
        candidate = self.login()
        job_id = self.client.get("/api/jobs", headers=candidate).json()["items"][0]["id"]
        with main.connect() as db:
            db.execute(
                """INSERT INTO resumes
                (candidate_id, filename, storage_path, content_type, parsed_profile_json, processing_status, uploaded_at)
                VALUES (1, 'resume.pdf', 'private.pdf', 'application/pdf', '{}', 'completed', '2026-01-01T00:00:00+00:00')"""
            )
        application = self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate).json()
        recruiter = self.login("recruiter@skillsphere.dev")
        interview_body = {
            "application_id": application["id"],
            "scheduled_at": "2026-11-12T10:00:00+05:30",
            "teams_url": "https://teams.microsoft.com/l/meetup-join/example",
        }
        created = self.client.post("/api/interviews", headers=recruiter, json=interview_body)
        self.assertEqual(created.status_code, 201, created.text)
        interview = created.json()
        self.assertEqual(interview["candidate_name"], "Alex Morgan")
        self.assertEqual(interview["scheduled_at"], "2026-11-12T04:30:00+00:00")
        self.assertEqual(interview["teams_url"], interview_body["teams_url"])
        self.assertEqual(self.client.get("/api/applications", headers=candidate).json()["items"][0]["status"], "Interview")
        self.assertEqual(self.client.get("/api/interviews", headers=candidate).json()["items"][0]["id"], interview["id"])
        self.assertEqual(self.client.get("/api/interviews", headers=recruiter).json()["items"][0]["id"], interview["id"])

        updated = self.client.put(
            f"/api/interviews/{interview['id']}/workspace",
            headers=candidate,
            json={"code": "print('hello')", "language": "python"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["code"], "print('hello')")
        shared = self.client.get(f"/api/interviews/{interview['id']}", headers=recruiter)
        self.assertEqual(shared.json()["code"], "print('hello')")

        outsider_registration = self.client.post(
            "/api/auth/register",
            json={"name": "Other Candidate", "email": "other-candidate@example.com", "password": "LongEnough9!", "role": "candidate"},
        )
        outsider = {"Authorization": f"Bearer {outsider_registration.json()['access_token']}"}
        self.assertEqual(self.client.get(f"/api/interviews/{interview['id']}", headers=outsider).status_code, 404)
        self.assertEqual(
            self.client.post(
                "/api/interviews",
                headers=recruiter,
                json={
                    "application_id": application["id"],
                    "scheduled_at": "2026-11-12T10:00:00",
                    "teams_url": interview_body["teams_url"],
                },
            ).status_code,
            422,
        )
        for invalid_url in (
            "http://teams.microsoft.com/l/meetup-join/example",
            "https://example.com/meeting",
            "https://teams.microsoft.com.evil.example/meeting",
        ):
            invalid = self.client.post(
                "/api/interviews",
                headers=recruiter,
                json={**interview_body, "teams_url": invalid_url},
            )
            self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(
            self.client.put(
                f"/api/interviews/{interview['id']}/workspace",
                headers=candidate,
                json={"code": "x" * 50_001, "language": "python"},
            ).status_code,
            422,
        )

    def test_recruiter_schedules_owned_interview_and_updates_pipeline_and_notifications(self):
        candidate, application = self.create_interview_application()
        recruiter = self.login("recruiter@skillsphere.dev")
        payload = self.interview_payload()
        created = self.client.post(
            f"/api/applications/{application['id']}/interviews",
            headers=recruiter,
            json=payload,
        )
        self.assertEqual(created.status_code, 201, created.text)
        interview = created.json()
        self.assertEqual(interview["meeting_url"], payload["meeting_url"])
        self.assertEqual(interview["meeting_platform"], "Google Meet")
        self.assertEqual(interview["duration_minutes"], 45)
        self.assertEqual(interview["status"], "Scheduled")
        self.assertEqual(self.client.get("/api/applications", headers=candidate).json()["items"][0]["status"], "Interview")
        with main.connect() as db:
            history = db.execute(
                """SELECT status, note FROM application_status_history
                WHERE application_id = ? ORDER BY id DESC LIMIT 1""",
                (application["id"],),
            ).fetchone()
            notification = db.execute(
                """SELECT type, title, message, action_url, email_delivery_status
                FROM notifications WHERE related_application_id = ?
                AND user_id = 1 ORDER BY id DESC LIMIT 1""",
                (application["id"],),
            ).fetchone()
        self.assertEqual(history["status"], "Interview")
        self.assertIn("scheduled", history["note"])
        self.assertEqual(notification["type"], "interview_scheduled")
        self.assertEqual(notification["title"], "Interview scheduled")
        self.assertIn(payload["meeting_url"], notification["message"])
        self.assertIn("45 minutes", notification["message"])
        self.assertEqual(notification["action_url"], "/")
        self.assertEqual(notification["email_delivery_status"], "Development")

    def test_other_recruiter_cannot_schedule_or_edit_an_interview(self):
        _, application = self.create_interview_application()
        recruiter = self.login("recruiter@skillsphere.dev")
        interview = self.client.post(
            f"/api/applications/{application['id']}/interviews",
            headers=recruiter,
            json=self.interview_payload(),
        ).json()
        registered = self.client.post(
            "/api/auth/register",
            json={
                "name": "Other Recruiter",
                "email": "other-recruiter@example.com",
                "password": "LongEnough9!",
                "role": "recruiter",
            },
        )
        other_recruiter = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        path = f"/api/applications/{application['id']}/interviews"
        self.assertEqual(
            self.client.post(path, headers=other_recruiter, json=self.interview_payload()).status_code,
            404,
        )
        update_path = f"{path}/{interview['id']}"
        self.assertEqual(
            self.client.patch(update_path, headers=other_recruiter, json={"recruiter_message": "Changed"}).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/api/interviews/{interview['id']}", headers=other_recruiter).status_code,
            404,
        )

    def test_candidate_interview_link_is_only_returned_to_the_application_owner(self):
        candidate, application = self.create_interview_application()
        recruiter = self.login("recruiter@skillsphere.dev")
        payload = self.interview_payload()
        created = self.client.post(
            f"/api/applications/{application['id']}/interviews",
            headers=recruiter,
            json=payload,
        )
        interview = created.json()
        owner_applications = self.client.get("/api/applications", headers=candidate).json()["items"]
        owner_interview = next(item["interview"] for item in owner_applications if item["id"] == application["id"])
        self.assertEqual(owner_interview["meeting_url"], payload["meeting_url"])

        outsider_registration = self.client.post(
            "/api/auth/register",
            json={
                "name": "Other Candidate",
                "email": "other-candidate@example.com",
                "password": "LongEnough9!",
                "role": "candidate",
            },
        )
        outsider = {"Authorization": f"Bearer {outsider_registration.json()['access_token']}"}
        self.assertEqual(self.client.get("/api/applications", headers=outsider).json()["items"], [])
        self.assertEqual(
            self.client.get(f"/api/interviews/{interview['id']}", headers=outsider).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/api/interviews/{interview['id']}/viewed", headers=outsider).status_code,
            404,
        )
        self.assertEqual(created.status_code, 201)

    def test_upcoming_interviews_excludes_past_and_non_scheduled_invites(self):
        candidate, first_application = self.create_interview_application(job_index=0)
        _, second_application = self.create_interview_application(job_index=1)
        recruiter = self.login("recruiter@skillsphere.dev")
        first = self.client.post(
            f"/api/applications/{first_application['id']}/interviews",
            headers=recruiter,
            json=self.interview_payload(),
        ).json()
        second = self.client.post(
            f"/api/applications/{second_application['id']}/interviews",
            headers=recruiter,
            json=self.interview_payload(),
        ).json()
        with main.connect() as db:
            db.execute(
                "UPDATE interviews SET scheduled_at = ? WHERE id = ?",
                ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), second["id"]),
            )
        upcoming = self.client.get("/api/interviews/upcoming", headers=candidate)
        self.assertEqual(upcoming.status_code, 200, upcoming.text)
        self.assertEqual([item["id"] for item in upcoming.json()["items"]], [first["id"]])
        cancelled = self.client.post(
            f"/api/applications/{first_application['id']}/interviews/{first['id']}/cancel",
            headers=recruiter,
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(self.client.get("/api/interviews/upcoming", headers=candidate).json()["items"], [])

    def test_rescheduling_updates_invitation_and_notifies_candidate_again(self):
        candidate, application = self.create_interview_application()
        recruiter = self.login("recruiter@skillsphere.dev")
        original = self.client.post(
            f"/api/applications/{application['id']}/interviews",
            headers=recruiter,
            json=self.interview_payload(),
        ).json()
        changed_time = (datetime.now(timezone.utc) + timedelta(days=7)).replace(
            second=0, microsecond=0
        ).isoformat()
        changed_url = "https://zoom.us/j/example-meeting"
        updated = self.client.patch(
            f"/api/applications/{application['id']}/interviews/{original['id']}",
            headers=recruiter,
            json={
                "scheduled_at": changed_time,
                "timezone": "UTC",
                "duration_minutes": 60,
                "meeting_platform": "Zoom",
                "meeting_url": changed_url,
                "recruiter_message": "Please join five minutes early.",
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["status"], "Rescheduled")
        self.assertEqual(updated.json()["meeting_url"], changed_url)
        self.assertEqual(updated.json()["duration_minutes"], 60)
        notifications = self.client.get("/api/notifications", headers=candidate).json()["items"]
        self.assertEqual(
            [item["type"] for item in notifications if item["type"].startswith("interview_")][:2],
            ["interview_rescheduled", "interview_scheduled"],
        )
        self.assertIn(changed_url, notifications[0]["message"])
        viewed = self.client.post(f"/api/interviews/{original['id']}/viewed", headers=candidate)
        self.assertEqual(viewed.status_code, 200, viewed.text)
        self.assertTrue(viewed.json()["candidate_viewed_at"])

    def test_cancellation_hides_candidate_join_link_and_notifies_candidate(self):
        candidate, application = self.create_interview_application()
        recruiter = self.login("recruiter@skillsphere.dev")
        interview = self.client.post(
            f"/api/applications/{application['id']}/interviews",
            headers=recruiter,
            json=self.interview_payload(),
        ).json()
        cancelled = self.client.post(
            f"/api/applications/{application['id']}/interviews/{interview['id']}/cancel",
            headers=recruiter,
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["status"], "Cancelled")
        candidate_interview = self.client.get("/api/applications", headers=candidate).json()["items"][0]["interview"]
        self.assertEqual(candidate_interview["status"], "Cancelled")
        self.assertEqual(candidate_interview["meeting_url"], "")
        closed_details = self.client.get(f"/api/interviews/{interview['id']}", headers=candidate).json()
        self.assertEqual(closed_details["meeting_url"], "")
        self.assertEqual(closed_details["teams_url"], "")
        notifications = self.client.get("/api/notifications", headers=candidate).json()["items"]
        self.assertTrue(any(item["type"] == "interview_cancelled" for item in notifications))

    def test_interview_rejects_non_https_links_and_past_schedules(self):
        _, application = self.create_interview_application()
        recruiter = self.login("recruiter@skillsphere.dev")
        path = f"/api/applications/{application['id']}/interviews"
        for invalid_url in (
            "http://meet.google.com/example",
            "javascript:alert(1)",
            "https://user:pass@example.com/meeting",
        ):
            response = self.client.post(
                path,
                headers=recruiter,
                json=self.interview_payload(meeting_url=invalid_url),
            )
            self.assertEqual(response.status_code, 422, response.text)
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        response = self.client.post(
            path,
            headers=recruiter,
            json=self.interview_payload(scheduled_at=past),
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_admin_can_view_and_manage_interviews_across_recruiters(self):
        _, application = self.create_interview_application()
        admin = self.login("admin@skillsphere.dev")
        created = self.client.post(
            f"/api/applications/{application['id']}/interviews",
            headers=admin,
            json=self.interview_payload(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        interview = created.json()
        admin_interviews = self.client.get("/api/interviews", headers=admin).json()["items"]
        self.assertTrue(any(item["id"] == interview["id"] for item in admin_interviews))
        completed = self.client.post(
            f"/api/applications/{application['id']}/interviews/{interview['id']}/complete",
            headers=admin,
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["status"], "Completed")

    def test_legacy_interview_table_is_migrated_and_existing_meeting_links_are_preserved(self):
        _, application = self.create_interview_application()
        legacy_url = "https://teams.microsoft.com/l/meetup-join/legacy"
        with main.connect() as db:
            db.execute("DROP TABLE interviews")
            db.execute(
                """CREATE TABLE interviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                    recruiter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    scheduled_at TEXT NOT NULL,
                    teams_url TEXT NOT NULL DEFAULT '',
                    code TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT 'python',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """INSERT INTO interviews
                (application_id, recruiter_id, scheduled_at, teams_url, created_at, updated_at)
                VALUES (?, 2, ?, ?, ?, ?)""",
                (application["id"], "2027-01-01T10:00:00+00:00", legacy_url, main.now(), main.now()),
            )
        main.initialize_database()
        with main.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(interviews)")}
            migrated = db.execute(
                "SELECT meeting_url, meeting_platform, status FROM interviews WHERE application_id = ?",
                (application["id"],),
            ).fetchone()
            index_names = {row["name"] for row in db.execute("PRAGMA index_list(interviews)")}
        self.assertTrue({"meeting_url", "duration_minutes", "timezone", "status", "candidate_viewed_at"} <= columns)
        self.assertEqual(migrated["meeting_url"], legacy_url)
        self.assertEqual(migrated["meeting_platform"], "Microsoft Teams")
        self.assertEqual(migrated["status"], "Scheduled")
        self.assertIn("idx_interviews_application_schedule", index_names)

    def test_job_question_banks_are_owned_and_topic_readiness_requires_twenty_active_questions(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        job = self.client.get("/api/jobs?mine=true", headers=recruiter).json()["items"][0]
        self.create_job_question_bank(job["id"], "Python", 19, headers=recruiter)
        summary = self.client.get(
            f"/api/jobs/{job['id']}/question-bank-summary", headers=recruiter
        ).json()
        self.assertFalse(summary["ready"])
        self.assertEqual(summary["additional_needed"], 1)

        payload = {
            "technology": "Python", "question": "What does this example demonstrate?",
            "options": ["A", "B", "C", "D"], "correct_index": 0,
            "difficulty": "Medium", "active": True,
        }
        added = self.client.post(f"/api/jobs/{job['id']}/questions", headers=recruiter, json=payload)
        self.assertEqual(added.status_code, 201, added.text)
        self.assertTrue(added.json()["summary"]["ready"])
        deactivated = self.client.patch(
            f"/api/jobs/{job['id']}/questions/{added.json()['id']}",
            headers=recruiter,
            json={**payload, "active": False},
        )
        self.assertEqual(deactivated.status_code, 200, deactivated.text)
        self.assertFalse(deactivated.json()["active"])
        self.assertFalse(self.client.get(
            f"/api/jobs/{job['id']}/question-bank-summary", headers=recruiter
        ).json()["ready"])
        reactivated = self.client.patch(
            f"/api/jobs/{job['id']}/questions/{added.json()['id']}",
            headers=recruiter,
            json=payload,
        )
        self.assertTrue(reactivated.json()["active"])
        self.assertEqual(
            self.client.post(
                f"/api/jobs/{job['id']}/questions",
                headers=recruiter,
                json={**payload, "question": "Duplicate question text", "options": ["same"] * 4},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/jobs/{job['id']}/questions/{added.json()['id']}", headers=recruiter
            ).status_code,
            200,
        )
        self.assertFalse(self.client.get(
            f"/api/jobs/{job['id']}/question-bank-summary", headers=recruiter
        ).json()["ready"])
        other = self.client.post(
            "/api/auth/register",
            json={"name": "Other Recruiter", "email": "other-bank@example.com", "password": "LongEnough9!", "role": "recruiter"},
        ).json()
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}
        self.assertEqual(
            self.client.get(f"/api/jobs/{job['id']}/questions", headers=other_headers).status_code,
            404,
        )

    def test_pdf_question_bank_import_stays_private_and_unsaved_until_confirmed(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        job = self.client.get("/api/jobs?mine=true", headers=recruiter).json()["items"][0]
        text = """Technology: Python
Difficulty: Easy
Question: What does len(items) return?
A. The number of items
B. The first item
C. The item's type
D. The last index
Answer: A
Technology: SQL
Difficulty: Hard
Question: Which clause filters grouped rows?
A. WHERE
B. HAVING
C. ORDER BY
D. LIMIT
Answer: B
Technology: Python
Question: Which expression creates a list?
A. []
B. {}
C. ()
D. None
"""
        with patch.object(main, "extract_question_bank_pdf_text", return_value=text):
            preview_response = self.client.post(
                f"/api/jobs/{job['id']}/question-bank/imports",
                headers=recruiter,
                files={"file": ("../bank.pdf", self.create_pdf_upload(), "application/pdf")},
            )
        self.assertEqual(preview_response.status_code, 201, preview_response.text)
        preview = preview_response.json()
        self.assertEqual(preview["valid_count"], 2)
        self.assertEqual(preview["invalid_count"], 1)
        self.assertEqual(preview["filename"], "bank.pdf")
        self.assertNotIn("storage_path", preview)
        self.assertEqual(preview["technology_counts"], {"Python": 2, "SQL": 1})
        self.assertTrue(preview["questions"][2]["errors"])
        candidate = self.login()
        self.assertEqual(
            self.client.get(
                f"/api/jobs/{job['id']}/question-bank/imports", headers=candidate
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                f"/api/jobs/{job['id']}/question-bank/imports/{preview['id']}",
                headers=candidate,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(f"/api/jobs/{job['id']}/questions", headers=candidate).status_code,
            403,
        )
        private_files = list(main.QUESTION_BANK_UPLOAD_DIR.glob("*.pdf"))
        self.assertEqual(len(private_files), 1)
        with main.connect() as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM job_questions WHERE job_id = ?", (job["id"],)
            ).fetchone()[0], 0)

        approved = [
            {
                "source_index": item["source_index"],
                "technology": item["technology"],
                "difficulty": item["difficulty"],
                "question": item["question"],
                "options": item["options"],
                "correct_index": item["correct_index"],
                "include": True,
            }
            for item in preview["questions"] if item["valid"]
        ]
        approved[0]["question"] = "What does len(values) return?"
        approved.append({
            "source_index": 2,
            "technology": "Python",
            "difficulty": "Medium",
            "question": "Which expression creates a list?",
            "options": ["[]", "{}", "()", "None"],
            "correct_index": 0,
            "include": True,
        })
        confirmed = self.client.post(
            f"/api/jobs/{job['id']}/question-bank/imports/{preview['id']}/confirm",
            headers=recruiter,
            json={"questions": approved},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["imported_count"], 3)
        self.assertEqual(confirmed.json()["skipped_count"], 0)
        self.assertFalse(confirmed.json()["summary"]["ready"])
        recruiter_notifications = self.client.get(
            "/api/notifications", headers=recruiter
        ).json()["items"]
        self.assertTrue(any(
            item["type"] == "question_bank_incomplete"
            and "add 17 more" in item["message"]
            for item in recruiter_notifications
        ))
        questions = self.client.get(f"/api/jobs/{job['id']}/questions", headers=recruiter).json()["items"]
        self.assertEqual(len(questions), 3)
        self.assertIn("What does len(values) return?", [item["question"] for item in questions])
        self.assertIn("Which expression creates a list?", [item["question"] for item in questions])
        self.assertTrue(all(item["source_type"] == "pdf_import" for item in questions))
        history = self.client.get(
            f"/api/jobs/{job['id']}/question-bank/imports", headers=recruiter
        ).json()["items"]
        self.assertEqual(history[0]["upload_status"], "Confirmed")
        self.assertEqual(history[0]["invalid_count"], 0)
        self.assertNotIn("storage_path", history[0])

    def test_pending_pdf_import_can_be_cancelled_and_other_recruiter_cannot_preview_job_uploads(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        job = self.client.get("/api/jobs?mine=true", headers=recruiter).json()["items"][0]
        text = """Technology: Python
Difficulty: Medium
Question: What is a Python tuple?
A. An immutable sequence
B. A key-value map
C. A module
D. A loop
Answer: A
"""
        other = self.client.post(
            "/api/auth/register",
            json={"name": "Other Recruiter", "email": "other-import@example.com", "password": "LongEnough9!", "role": "recruiter"},
        ).json()
        other_headers = {"Authorization": f"Bearer {other['access_token']}"}
        with patch.object(main, "extract_question_bank_pdf_text", side_effect=AssertionError("Must authorize before parsing")):
            denied = self.client.post(
                f"/api/jobs/{job['id']}/question-bank/imports",
                headers=other_headers,
                files={"file": ("bank.pdf", self.create_pdf_upload(), "application/pdf")},
            )
        self.assertEqual(denied.status_code, 404)

        with patch.object(main, "extract_question_bank_pdf_text", return_value=text):
            preview = self.client.post(
                f"/api/jobs/{job['id']}/question-bank/imports",
                headers=recruiter,
                files={"file": ("bank.pdf", self.create_pdf_upload(), "application/pdf")},
            ).json()
        stored_pdf = next(main.QUESTION_BANK_UPLOAD_DIR.glob("*.pdf"))
        cancelled = self.client.delete(
            f"/api/jobs/{job['id']}/question-bank/imports/{preview['id']}", headers=recruiter
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertFalse(stored_pdf.exists())
        self.assertEqual(
            self.client.get(
                f"/api/jobs/{job['id']}/question-bank/imports/{preview['id']}", headers=recruiter
            ).status_code,
            409,
        )

    def test_existing_database_migration_adds_match_column(self):
        with main.connect() as db:
            db.execute("ALTER TABLE applications DROP COLUMN match_percentage")
        main.initialize_database()
        with main.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(applications)")}
        self.assertIn("match_percentage", columns)

    def test_exact_seventy_percent_shortlists_and_lower_score_does_not_create_assessment(self):
        required = ["Python", "Java", "React", "SQL", "FastAPI", "Docker", "AWS", "TypeScript", "HTML", "CSS"]
        _, _, eligible = self.apply_with_match(required, required[:7], question_bank={"Python": 20})
        self.assertEqual(eligible["match_percentage"], 70)
        self.assertEqual(eligible["status"], "Assessment Sent")
        with main.connect() as db:
            assessment = db.execute(
                "SELECT status, technology FROM assessments WHERE application_id = ?", (eligible["id"],)
            ).fetchone()
        self.assertEqual(dict(assessment), {"status": "Sent", "technology": "Python"})
        recruiter = self.login("recruiter@skillsphere.dev")
        notifications = self.client.get("/api/notifications", headers=recruiter).json()["items"]
        self.assertTrue(any(
            item["type"] == "assessment_sent"
            and item["related_application_id"] == eligible["id"]
            and not item["action_url"]
            for item in notifications
        ))
        candidate_notifications = self.client.get(
            "/api/notifications", headers=self.login()
        ).json()["items"]
        self.assertTrue(any(
            item["type"] == "assessment_sent"
            and item["related_application_id"] == eligible["id"]
            and item["action_url"]
            for item in candidate_notifications
        ))

        _, _, below = self.apply_with_match(required, required[:6], job_index=1)
        self.assertEqual(below["match_percentage"], 60)
        self.assertEqual(below["status"], "Applied")
        with main.connect() as db:
            self.assertIsNone(db.execute(
                "SELECT id FROM assessments WHERE application_id = ?", (below["id"],)
            ).fetchone())
        candidate, recruiter, eligible_without_bank = self.apply_with_match(required, required[:7], job_index=2)
        self.assertEqual(eligible_without_bank["status"], "Shortlisted")
        with main.connect() as db:
            self.assertIsNone(db.execute(
                "SELECT id FROM assessments WHERE application_id = ?", (eligible_without_bank["id"],)
            ).fetchone())
        candidate_application = next(
            item for item in self.client.get("/api/applications", headers=candidate).json()["items"]
            if item["id"] == eligible_without_bank["id"]
        )
        self.assertEqual(candidate_application["assessment_status"], "Pending Question Bank")
        self.assertTrue(any(
            item["type"] == "assessment_pending"
            for item in self.client.get("/api/notifications", headers=candidate).json()["items"]
        ))
        self.assertTrue(any(
            item["type"] == "question_bank_incomplete" and item["email_delivery_status"] == "Development"
            for item in self.client.get("/api/notifications", headers=recruiter).json()["items"]
        ))

    def test_completing_a_question_bank_auto_sends_assessments_to_existing_shortlisted_candidates(self):
        candidate, recruiter, application = self.apply_with_match(
            ["Python"], ["Python"], question_bank={"Python": 19}
        )
        self.assertEqual(application["status"], "Shortlisted")
        payload = {
            "technology": "Python",
            "question": "What does this final Python question test?",
            "options": ["Option one", "Option two", "Option three", "Option four"],
            "correct_index": 2,
            "difficulty": "Hard",
            "active": True,
        }
        added = self.client.post(
            f"/api/jobs/{application['job_id']}/questions", headers=recruiter, json=payload
        )
        self.assertEqual(added.status_code, 201, added.text)
        self.assertTrue(added.json()["summary"]["ready"])
        with main.connect() as db:
            assessment = db.execute(
                "SELECT status, technology FROM assessments WHERE application_id = ?", (application["id"],)
            ).fetchone()
            status = db.execute(
                "SELECT status FROM applications WHERE id = ?", (application["id"],)
            ).fetchone()["status"]
        self.assertEqual(dict(assessment), {"status": "Sent", "technology": "Python"})
        self.assertEqual(status, "Assessment Sent")
        self.assertTrue(any(
            item["type"] == "assessment_sent"
            for item in self.client.get("/api/notifications", headers=candidate).json()["items"]
        ))

    def test_assessment_selects_twenty_relevant_questions_across_candidate_topics(self):
        candidate, recruiter, application = self.apply_with_match(
            ["Python", "SQL"],
            ["Python", "SQL", "SQL", "SQL"],
            "Python SQL SQL SQL",
            question_bank={"Python": 20, "SQL": 20},
        )
        with main.connect() as db:
            assessment = db.execute(
                """SELECT status, technology, question_topics_json,
                question_topic_weights_json, selection_reasoning
                FROM assessments WHERE application_id = ?""",
                (application["id"],),
            ).fetchone()
            selected = db.execute(
                """SELECT t.technology FROM assessment_questions q
                JOIN assessments x ON x.id = q.assessment_id
                JOIN technical_questions t ON t.id = q.question_id
                WHERE x.application_id = ?""",
                (application["id"],),
            ).fetchall()
        self.assertEqual(assessment["status"], "Sent")
        self.assertEqual(json.loads(assessment["question_topics_json"]), ["Python", "SQL"])
        self.assertEqual(
            json.loads(assessment["question_topic_weights_json"]),
            {"Python": 3, "SQL": 7},
        )
        self.assertIn("candidate's resume/profile", assessment["selection_reasoning"])
        self.assertEqual(len(selected), 20)
        self.assertEqual(
            {topic: sum(row["technology"] == topic for row in selected) for topic in ("Python", "SQL")},
            {"Python": 6, "SQL": 14},
        )
        candidate_assessments = self.client.get("/api/assessments", headers=candidate).json()["items"]
        self.assertNotIn("selection_reasoning", candidate_assessments[0])
        notification = next(
            item for item in self.client.get("/api/notifications", headers=candidate).json()["items"]
            if item["type"] == "assessment_sent" and item["related_application_id"] == application["id"]
            and item["action_url"]
        )
        token = parse_qs(urlparse(notification["action_url"]).query)["assessment"][0]
        self.start_assessment_proctoring(token)
        started = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(len(started.json()["questions"]), 20)
        self.assertTrue(all("correct_index" not in item for item in started.json()["questions"]))

    def test_primary_topic_detection_and_manual_topic_selection(self):
        _, recruiter, matched = self.apply_with_match(
            ["Python", "SQL"], ["Python", "SQL", "SQL", "SQL"], "Python SQL SQL SQL",
            question_bank={"SQL": 20},
        )
        with main.connect() as db:
            topic = db.execute(
                "SELECT technology FROM assessments WHERE application_id = ?", (matched["id"],)
            ).fetchone()["technology"]
        self.assertEqual(topic, "SQL")

        _, recruiter, manual = self.apply_with_match(
            ["Figma"], ["Figma"], job_index=1, question_bank={"Java": 20}
        )
        with main.connect() as db:
            status = db.execute(
                "SELECT status FROM assessments WHERE application_id = ?", (manual["id"],)
            ).fetchone()["status"]
        self.assertEqual(status, "Manual Topic Required")
        self.assertEqual(
            self.client.post(
                f"/api/applications/{manual['id']}/assessment/send", headers=recruiter
            ).status_code,
            409,
        )
        selected = self.client.patch(
            f"/api/applications/{manual['id']}/assessment/topic",
            headers=recruiter,
            json={"technology": "Java"},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual(selected.json(), {"technology": "Java", "status": "Not Sent"})

        _, _, unrelated = self.apply_with_match(["Java"], ["JavaScript"], job_index=2)
        self.assertEqual(unrelated["match_percentage"], 0)
        self.assertEqual(unrelated["status"], "Applied")

    def test_assessment_start_requires_camera_permission_and_consent(self):
        candidate, _, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])
        welcome = self.client.get(f"/api/assessments/{token}")
        self.assertTrue(welcome.json()["proctoring"]["required"])
        self.assertEqual(self.client.post(f"/api/assessments/{token}/start").status_code, 409)

        denied = self.start_assessment_proctoring(token, consent=True, camera=False)
        self.assertFalse(denied["started"])
        self.assertEqual(self.client.post(f"/api/assessments/{token}/start").status_code, 409)
        with main.connect() as db:
            untouched = db.execute(
                "SELECT status, starts_at, proctoring_consent_at FROM assessments WHERE application_id = ?",
                (application["id"],),
            ).fetchone()
            self.assertEqual(db.execute(
                """SELECT event_type FROM assessment_proctoring_events
                WHERE assessment_id = (SELECT id FROM assessments WHERE application_id = ?)""",
                (application["id"],),
            ).fetchone()["event_type"], "camera_denied")
        self.assertEqual(untouched["status"], "Sent")
        self.assertIsNone(untouched["starts_at"])
        self.assertIsNone(untouched["proctoring_consent_at"])

        self.start_assessment_proctoring(token)
        started = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["status"], "Started")
        self.assertEqual(started.json()["proctoring"]["warning_count"], 0)
        history = self.client.get(
            f"/api/applications/{application['id']}/history", headers=candidate
        ).json()["items"]
        self.assertEqual(history[-1]["status"], "Assessment Started")

    def test_proctoring_events_are_reviewable_only_by_authorized_recruiter_or_admin(self):
        _, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])
        self.start_assessment_proctoring(token)
        self.assertEqual(self.client.post(f"/api/assessments/{token}/start").status_code, 200)

        too_short = self.client.post(
            f"/api/assessments/{token}/proctoring/events",
            json={"event_type": "face_missing", "duration_seconds": 9},
        )
        self.assertEqual(too_short.status_code, 422)
        missing = self.client.post(
            f"/api/assessments/{token}/proctoring/events",
            json={"event_type": "face_missing", "duration_seconds": 10},
        )
        self.assertEqual(missing.status_code, 200, missing.text)
        returned = self.client.post(
            f"/api/assessments/{token}/proctoring/events",
            json={"event_type": "face_returned"},
        )
        self.assertEqual(returned.status_code, 200, returned.text)
        for event_type in ("tab_hidden", "multiple_faces", "window_blur"):
            recorded = self.client.post(
                f"/api/assessments/{token}/proctoring/events",
                json={"event_type": event_type},
            )
            self.assertEqual(recorded.status_code, 200, recorded.text)
        status = self.client.get(
            f"/api/assessments/{token}/proctoring/status"
        )
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["warning_count"], 4)
        self.assertTrue(status.json()["flagged"])
        self.assertEqual(self.client.get(
            f"/api/assessments/not-a-token/proctoring/status"
        ).status_code, 404)

        with main.connect() as db:
            assessment_id = db.execute(
                "SELECT id FROM assessments WHERE application_id = ?", (application["id"],)
            ).fetchone()["id"]
        review = self.client.get(
            f"/api/assessment-reviews/{assessment_id}/proctoring", headers=recruiter
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertTrue(review.json()["flagged"])
        self.assertEqual(len(review.json()["events"]), 7)

        admin = self.login("admin@skillsphere.dev")
        other_recruiter = self.client.post(
            "/api/auth/register",
            json={"name": "Second Recruiter", "email": "second-recruiter@example.com", "password": "LongEnough9!", "role": "recruiter"},
        ).json()
        other_headers = {"Authorization": f"Bearer {other_recruiter['access_token']}"}
        self.assertEqual(self.client.get(
            f"/api/assessment-reviews/{assessment_id}/proctoring", headers=other_headers
        ).status_code, 404)
        self.assertEqual(self.client.get(
            f"/api/assessment-reviews/{assessment_id}/proctoring", headers=self.login()
        ).status_code, 403)
        admin_view = self.client.get(
            f"/api/admin/proctoring?job_id={application['job_id']}&flagged_only=true",
            headers=admin,
        )
        self.assertEqual(admin_view.status_code, 200, admin_view.text)
        self.assertEqual(len(admin_view.json()["items"]), 1)
        self.assertEqual(admin_view.json()["items"][0]["candidate_name"], "Alex Morgan")
        self.assertEqual(self.client.get(
            f"/api/admin/proctoring?job_id={application['job_id']}&flagged_only=false",
            headers=admin,
        ).json()["items"], [])
        notifications = self.client.get("/api/notifications", headers=admin).json()["items"]
        self.assertTrue(any(item["type"] == "assessment_proctoring_flagged" for item in notifications))

    def test_proctoring_can_be_disabled_for_new_assessments(self):
        with patch.object(main, "PROCTORING_REQUIRED", False):
            _, _, application = self.apply_with_match(["Python"], ["Python"])
            self.create_job_question_bank(application["job_id"])
            _, token = self.send_assessment(application["id"])
        welcome = self.client.get(f"/api/assessments/{token}")
        self.assertFalse(welcome.json()["proctoring"]["required"])
        started = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(started.status_code, 200, started.text)

    def test_proctoring_migration_does_not_interrupt_legacy_started_assessments(self):
        _, _, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])
        with main.connect() as db:
            db.execute(
                """UPDATE assessments SET status = 'Started', starts_at = ?,
                proctoring_required = 1, proctoring_consent_at = NULL,
                camera_permission_granted_at = NULL, proctoring_started_at = NULL
                WHERE application_id = ?""",
                (main.now(), application["id"]),
            )
        main.initialize_database()
        with main.connect() as db:
            required = db.execute(
                "SELECT proctoring_required FROM assessments WHERE application_id = ?",
                (application["id"],),
            ).fetchone()["proctoring_required"]
        self.assertEqual(required, 0)
        resumed = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertFalse(resumed.json()["proctoring"]["required"])

    def test_assessment_token_is_hashed_questions_are_safe_and_scoring_is_server_side(self):
        _, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        sent, token = self.send_assessment(application["id"])
        self.assertEqual(sent["delivery"], "development")
        self.assertNotIn("assessment_url", sent)
        with main.connect() as db:
            stored = db.execute(
                "SELECT id, token_hash FROM assessments WHERE application_id = ?", (application["id"],)
            ).fetchone()
            assessment_id = stored["id"]
            stored_hash = stored["token_hash"]
        self.assertNotEqual(stored_hash, token)
        self.assertEqual(len(stored_hash), 64)

        welcome = self.client.get(f"/api/assessments/{token}")
        self.assertEqual(welcome.status_code, 200, welcome.text)
        self.assertEqual(welcome.json()["status"], "Sent")
        self.assertEqual(welcome.json()["assessment_name"], "Round 1: Technical MCQ Test")
        self.assertEqual(welcome.json()["total_questions"], 20)
        self.assertEqual(welcome.json()["pass_percentage"], 75)
        self.assertEqual(welcome.json()["questions"], [])
        self.start_assessment_proctoring(token)
        started = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(started.status_code, 200, started.text)
        questions = started.json()["questions"]
        self.assertEqual(len(questions), 20)
        self.assertTrue(all(len(question["options"]) == 4 for question in questions))
        self.assertTrue(all("correct_index" not in question and "answer" not in question for question in questions))
        recruiter_review_before_submit = self.client.get(
            f"/api/assessment-reviews/{assessment_id}", headers=recruiter
        )
        self.assertEqual(recruiter_review_before_submit.status_code, 409)
        with main.connect() as db:
            answers = {
                row["id"]: row["correct_index"]
                for row in db.execute(
                    "SELECT id, correct_index FROM assessment_questions WHERE assessment_id = "
                    "(SELECT id FROM assessments WHERE application_id = ?)",
                    (application["id"],),
                )
            }
        submitted = self.client.post(f"/api/assessments/{token}/submit", json={"answers": answers})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["status"], "Completed")
        self.assertEqual(submitted.json()["score"], 20)
        self.assertEqual(submitted.json()["score_percentage"], 100)
        self.assertEqual(submitted.json()["correct_answer_count"], 20)
        self.assertEqual(submitted.json()["incorrect_answer_count"], 0)
        self.assertEqual(submitted.json()["result"], "Passed")
        self.assertEqual(submitted.json()["assessment_round"], "Round 1")
        self.assertIsNotNone(submitted.json()["time_taken_seconds"])
        repeated = self.client.post(f"/api/assessments/{token}/submit", json={"answers": answers})
        self.assertIn(repeated.status_code, {404, 409, 410})
        recruiter_applications = self.client.get("/api/recruiter/applicants", headers=recruiter).json()["items"]
        applicant = next(item for item in recruiter_applications if item["id"] == application["id"])
        self.assertEqual(applicant["assessment_status"], "Completed")
        self.assertEqual(applicant["assessment_result"], "Passed")
        self.assertEqual(applicant["status"], "Round 1 Passed")
        self.assertEqual(applicant["assessment_score"], 20)
        self.assertIsNotNone(applicant["assessment_submitted_at"])
        review = self.client.get(
            f"/api/assessment-reviews/{assessment_id}", headers=recruiter
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["result"], "Passed")

    def test_round1_fifteen_of_twenty_passes_and_processes_results_once(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        token, result = self.complete_assessment(application, 15)
        self.assertEqual(result["score_percentage"], 75)
        self.assertEqual(result["correct_answer_count"], 15)
        self.assertEqual(result["incorrect_answer_count"], 5)
        self.assertEqual(result["result"], "Passed")
        self.assertEqual(result["message"], "Congratulations, you have cleared Round 1.")

        with main.connect() as db:
            stored = db.execute(
                """SELECT total_questions, correct_answer_count, incorrect_answer_count,
                score_percentage, result, assessment_round, submitted_at, result_processed_at,
                time_taken_seconds FROM assessments WHERE application_id = ?""",
                (application["id"],),
            ).fetchone()
            self.assertEqual(stored["total_questions"], 20)
            self.assertEqual(stored["correct_answer_count"], 15)
            self.assertEqual(stored["incorrect_answer_count"], 5)
            self.assertEqual(stored["score_percentage"], 75)
            self.assertEqual(stored["result"], "Passed")
            self.assertEqual(stored["assessment_round"], "Round 1")
            self.assertEqual(stored["submitted_at"], stored["result_processed_at"])
            self.assertIsNotNone(stored["time_taken_seconds"])
            current_status = db.execute(
                "SELECT status FROM applications WHERE id = ?", (application["id"],)
            ).fetchone()["status"]
        self.assertEqual(current_status, "Round 1 Passed")

        timeline = self.client.get(
            f"/api/applications/{application['id']}/history", headers=candidate
        ).json()["items"]
        statuses = [item["status"] for item in timeline]
        self.assertEqual(statuses[-2:], ["Assessment Completed", "Round 1 Passed"])
        self.assertIn("75% (15/20 correct)", timeline[-1]["note"])
        candidate_notifications = self.client.get("/api/notifications", headers=candidate).json()["items"]
        recruiter_notifications = self.client.get("/api/notifications", headers=recruiter).json()["items"]
        admin = self.login("admin@skillsphere.dev")
        admin_notifications = self.client.get("/api/notifications", headers=admin).json()["items"]
        candidate_result = next(item for item in candidate_notifications if item["type"] == "round1_result")
        self.assertEqual(candidate_result["title"], "You passed Round 1")
        self.assertIn(application["title"], candidate_result["message"])
        self.assertIn("75%", candidate_result["message"])
        self.assertIn("recruiter will review", candidate_result["message"])
        recruiter_result = next(
            item for item in recruiter_notifications if item["type"] == "round1_assessment_completed"
        )
        self.assertIn("Alex Morgan", recruiter_result["message"])
        self.assertIn("75% (15/20): Passed", recruiter_result["message"])
        self.assertTrue(any(item["type"] == "round1_assessment_completed" for item in admin_notifications))

        assessment_id = self.client.get(
            "/api/assessments?result=Passed&min_score=75", headers=recruiter
        ).json()["items"][0]["id"]
        other_user = self.client.post(
            "/api/auth/register",
            json={"name": "Second Recruiter", "email": "second-recruiter@example.com", "password": "LongEnough9!", "role": "recruiter"},
        ).json()
        other_recruiter = {"Authorization": f"Bearer {other_user['access_token']}"}
        self.assertEqual(
            self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=other_recruiter).status_code,
            404,
        )
        admin_review = self.client.get(
            f"/api/assessment-reviews/{assessment_id}", headers=admin
        )
        self.assertEqual(admin_review.status_code, 200, admin_review.text)
        candidate_assessment = self.client.get("/api/assessments", headers=candidate).json()["items"][0]
        self.assertEqual(candidate_assessment["result"], "Passed")
        self.assertEqual(candidate_assessment["correct_answer_count"], 15)
        self.assertNotIn("correct_index", candidate_assessment)
        moved = self.client.patch(
            f"/api/applications/{application['id']}/status",
            headers=recruiter,
            json={"status": "Interview"},
        )
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["status"], "Interview")

        duplicate = self.client.post(
            f"/api/assessments/{token}/submit", json={"answers": {}}
        )
        self.assertIn(duplicate.status_code, {404, 409, 410})
        with main.connect() as db:
            result_notifications = db.execute(
                """SELECT COUNT(*) FROM notifications
                WHERE related_application_id = ? AND type = 'round1_result'""",
                (application["id"],),
            ).fetchone()[0]
            round_status_rows = db.execute(
                """SELECT COUNT(*) FROM application_status_history
                WHERE application_id = ? AND status = 'Round 1 Passed'""",
                (application["id"],),
            ).fetchone()[0]
        self.assertEqual(result_notifications, 1)
        self.assertEqual(round_status_rows, 1)
        self.assertEqual(
            self.client.get(
                f"/api/applications/{application['id']}/history", headers=candidate
            ).json()["items"][-1]["status"],
            "Interview",
        )

        weekly = self.client.get("/api/admin/analytics", headers=admin).json()["weekly"]
        self.assertGreaterEqual(weekly["round1_assessments_completed"], 1)
        self.assertGreaterEqual(weekly["round1_passed"], 1)
        self.assertEqual(weekly["round1_pass_fail_distribution"]["Passed"], weekly["round1_passed"])
        self.assertTrue(any(item["job_id"] == application["job_id"] for item in weekly["round1_passed_by_job"]))

    def test_round1_fourteen_of_twenty_fails(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        _, result = self.complete_assessment(application, 14)
        self.assertEqual(result["score_percentage"], 70)
        self.assertEqual(result["correct_answer_count"], 14)
        self.assertEqual(result["incorrect_answer_count"], 6)
        self.assertEqual(result["result"], "Failed")
        self.assertEqual(
            result["message"],
            "Thank you for completing Round 1. You did not reach the required passing score.",
        )
        self.assertEqual(
            self.client.get(
                f"/api/applications/{application['id']}/history", headers=candidate
            ).json()["items"][-1]["status"],
            "Round 1 Failed",
        )
        failed_filter = self.client.get(
            f"/api/assessments?result=Failed&min_score=70&max_score=70&status=Completed&job_id={application['job_id']}",
            headers=recruiter,
        )
        self.assertEqual(failed_filter.status_code, 200, failed_filter.text)
        self.assertEqual(len(failed_filter.json()["items"]), 1)
        self.assertEqual(failed_filter.json()["items"][0]["result"], "Failed")
        admin = self.login("admin@skillsphere.dev")
        weekly = self.client.get("/api/admin/analytics", headers=admin).json()["weekly"]
        self.assertEqual(weekly["round1_failed"], 1)
        self.assertEqual(weekly["round1_pass_fail_distribution"]["Failed"], 1)
        self.assertTrue(any(item["job_id"] == application["job_id"] for item in weekly["round1_failed_by_job"]))

    def test_round1_score_above_threshold_passes(self):
        _, _, application = self.apply_with_match(["Python"], ["Python"])
        _, result = self.complete_assessment(application, 16)
        self.assertEqual(result["score_percentage"], 80)
        self.assertEqual(result["result"], "Passed")

    def test_round1_pass_threshold_is_configurable_for_new_assessments(self):
        with patch.object(main, "ROUND1_PASS_PERCENTAGE", 80):
            _, _, application = self.apply_with_match(["Python"], ["Python"])
            _, result = self.complete_assessment(application, 15)
        self.assertEqual(result["pass_percentage"], 80)
        self.assertEqual(result["score_percentage"], 75)
        self.assertEqual(result["result"], "Failed")

    def test_migration_backfills_existing_completed_assessment_results(self):
        _, _, application = self.apply_with_match(["Python"], ["Python"])
        _, result = self.complete_assessment(application, 15)
        self.assertEqual(result["result"], "Passed")
        with main.connect() as db:
            db.execute(
                """UPDATE assessments SET result = NULL, score_percentage = NULL,
                correct_answer_count = NULL, incorrect_answer_count = NULL,
                result_processed_at = NULL WHERE application_id = ?""",
                (application["id"],),
            )
            db.execute(
                "UPDATE applications SET status = 'Assessment Completed' WHERE id = ?",
                (application["id"],),
            )
        main.initialize_database()
        with main.connect() as db:
            stored = db.execute(
                """SELECT assessment_round, result, score_percentage, correct_answer_count,
                incorrect_answer_count, result_processed_at FROM assessments
                WHERE application_id = ?""",
                (application["id"],),
            ).fetchone()
            status_count = db.execute(
                """SELECT COUNT(*) FROM application_status_history
                WHERE application_id = ? AND status = 'Round 1 Passed'""",
                (application["id"],),
            ).fetchone()[0]
            pipeline_status = db.execute(
                "SELECT status FROM applications WHERE id = ?", (application["id"],)
            ).fetchone()["status"]
        self.assertEqual(stored["assessment_round"], "Round 1")
        self.assertEqual(stored["result"], "Passed")
        self.assertEqual(stored["score_percentage"], 75)
        self.assertEqual(stored["correct_answer_count"], 15)
        self.assertEqual(stored["incorrect_answer_count"], 5)
        self.assertIsNotNone(stored["result_processed_at"])
        self.assertEqual(status_count, 1)
        self.assertEqual(pipeline_status, "Round 1 Passed")

    def test_reissue_invalidates_the_previous_link(self):
        _, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, old_token = self.send_assessment(application["id"])
        with patch.dict(os.environ, {"SKILLSPHERE_SMTP_HOST": ""}):
            reissued = self.client.post(
                f"/api/applications/{application['id']}/assessment/reissue", headers=recruiter
            )
        self.assertEqual(reissued.status_code, 200, reissued.text)
        self.assertNotIn("assessment_url", reissued.json())
        candidate_notifications = self.client.get(
            "/api/notifications", headers=self.login()
        ).json()["items"]
        candidate_link = next(
            item for item in candidate_notifications
            if item["type"] == "assessment_sent"
            and item["related_application_id"] == application["id"]
            and item["action_url"]
        )
        new_token = parse_qs(urlparse(candidate_link["action_url"]).query)["assessment"][0]
        self.assertNotEqual(old_token, new_token)
        self.assertEqual(self.client.get(f"/api/assessments/{old_token}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/assessments/{new_token}").status_code, 200)

    def test_timed_out_started_assessment_is_marked_expired_and_cannot_be_reused(self):
        _, _, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])
        self.start_assessment_proctoring(token)
        started = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(started.status_code, 200)
        with main.connect() as db:
            db.execute(
                "UPDATE assessments SET expires_at = ? WHERE application_id = ?",
                ("2000-01-01T00:00:00+00:00", application["id"]),
            )
        timed_out = self.client.post(f"/api/assessments/{token}/submit", json={"answers": {}})
        self.assertEqual(timed_out.status_code, 200, timed_out.text)
        self.assertEqual(timed_out.json()["status"], "Completed")
        self.assertEqual(timed_out.json()["score"], 0)
        self.assertEqual(timed_out.json()["result"], "Failed")
        reused = self.client.post(f"/api/assessments/{token}/submit", json={"answers": {}})
        self.assertIn(reused.status_code, {404, 409, 410})
        with main.connect() as db:
            status, submitted_at, result = db.execute(
                "SELECT status, submitted_at, result FROM assessments WHERE application_id = ?", (application["id"],)
            ).fetchone()
        self.assertEqual(status, "Completed")
        self.assertEqual(result, "Failed")
        self.assertIsNotNone(submitted_at)

    def test_expired_and_withdrawn_assessments_reject_their_links(self):
        candidate, _, expired_application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(expired_application["job_id"])
        _, expired_token = self.send_assessment(expired_application["id"])
        with main.connect() as db:
            db.execute(
                "UPDATE assessments SET expires_at = ? WHERE application_id = ?",
                ("2000-01-01T00:00:00+00:00", expired_application["id"]),
            )
        expired = self.client.get(f"/api/assessments/{expired_token}")
        self.assertEqual(expired.status_code, 410)
        with main.connect() as db:
            status = db.execute(
                "SELECT status FROM assessments WHERE application_id = ?", (expired_application["id"],)
            ).fetchone()["status"]
        self.assertEqual(status, "Expired")

        candidate, _, withdrawn_application = self.apply_with_match(["Python"], ["Python"], job_index=1)
        self.create_job_question_bank(withdrawn_application["job_id"])
        _, revoked_token = self.send_assessment(withdrawn_application["id"])
        withdrawn = self.client.patch(
            f"/api/applications/{withdrawn_application['id']}/withdraw", headers=candidate
        )
        self.assertEqual(withdrawn.status_code, 200)
        self.assertEqual(self.client.get(f"/api/assessments/{revoked_token}").status_code, 404)

    def test_recruiter_cannot_manage_another_recruiters_assessment(self):
        _, _, application = self.apply_with_match(["Python"], ["Python"])
        other_registration = self.client.post(
            "/api/auth/register",
            json={"name": "Other Recruiter", "email": "other-recruiter@example.com", "password": "LongEnough9!", "role": "recruiter"},
        )
        self.assertEqual(other_registration.status_code, 201, other_registration.text)
        other_recruiter = {"Authorization": f"Bearer {other_registration.json()['access_token']}"}
        denied = self.client.post(
            f"/api/applications/{application['id']}/assessment/send", headers=other_recruiter
        )
        self.assertEqual(denied.status_code, 404)

    def test_health_endpoint_returns_db_status(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["database"], "ok")
        self.assertEqual(data["version"], "1.0.0")

    def test_cors_headers_configured(self):
        response = self.client.options(
            "/api/jobs",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access-control-allow-origin", response.headers)

    def test_detect_profile_expanded_skills(self):
        text = "Experienced Senior Developer with deep knowledge in FastAPI, React, Next.js, Docker, Kubernetes, PostgreSQL, Go, Rust, PyTorch, and Tailwind CSS."
        profile = main.detect_profile(text)
        detected = {s.lower() for s in profile["skills"]}
        expected_subset = {"fastapi", "react", "next.js", "docker", "kubernetes", "postgresql", "go", "rust", "pytorch", "tailwind"}
        for skill in expected_subset:
            self.assertIn(skill, detected, f"Expected {skill} to be detected in resume text")

    def test_auth_token_tampering_and_expiration(self):
        candidate = self.login()
        token = candidate["Authorization"].split(" ")[1]
        parts = token.split(".")
        # Tamper signature
        tampered_token = f"{parts[0]}.{parts[1]}.tampered_signature"
        tampered_res = self.client.get("/api/me", headers={"Authorization": f"Bearer {tampered_token}"})
        self.assertEqual(tampered_res.status_code, 401)
        self.assertIn("Invalid or expired authentication token", tampered_res.json()["detail"])

        # Expired token
        header, body, _ = parts
        import base64
        body_data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        body_data["exp"] = int(main.time.time()) - 3600  # 1 hour ago
        new_body = base64.urlsafe_b64encode(json.dumps(body_data, separators=(",", ":")).encode()).rstrip(b"=").decode()
        new_sig = main.hmac.new(main.TOKEN_SECRET.encode(), f"{header}.{new_body}".encode(), main.hashlib.sha256).digest()
        expired_token = f"{header}.{new_body}.{base64.urlsafe_b64encode(new_sig).rstrip(b'=').decode()}"
        expired_res = self.client.get("/api/me", headers={"Authorization": f"Bearer {expired_token}"})
        self.assertEqual(expired_res.status_code, 401)
        self.assertIn("Invalid or expired authentication token", expired_res.json()["detail"])

    def test_disabled_user_token_rejected(self):
        candidate = self.login()
        with main.connect() as db:
            db.execute("UPDATE users SET active = 0 WHERE email = 'candidate@skillsphere.dev'")
        res = self.client.get("/api/me", headers=candidate)
        self.assertEqual(res.status_code, 401)
        self.assertIn("Account no longer exists", res.json()["detail"])

    def test_notifications_mark_read_and_read_all(self):
        candidate = self.login()
        with main.connect() as db:
            user_id = db.execute("SELECT id FROM users WHERE email = 'candidate@skillsphere.dev'").fetchone()["id"]
            n1 = main.add_notification(db, user_id, "test_1", "Test Notice 1", "Message 1")
            n2 = main.add_notification(db, user_id, "test_2", "Test Notice 2", "Message 2")

        res_before = self.client.get("/api/notifications", headers=candidate)
        self.assertEqual(res_before.status_code, 200)
        self.assertGreaterEqual(res_before.json()["unread_count"], 2)

        # Mark single read
        patch_res = self.client.patch(f"/api/notifications/{n1}/read", headers=candidate, json={"is_read": True})
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["is_read"], 1)

        # Mark all read
        read_all_res = self.client.patch("/api/notifications/read-all", headers=candidate)
        self.assertEqual(read_all_res.status_code, 200)

        res_after = self.client.get("/api/notifications", headers=candidate)
        self.assertEqual(res_after.json()["unread_count"], 0)

    def test_proctoring_flagged_at_threshold_and_auto_submit(self):
        _, _, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])
        self.start_assessment_proctoring(token)
        self.client.post(f"/api/assessments/{token}/start")

        # Emit face_missing warnings up to threshold (default max 3)
        for i in range(1, 3):
            res = self.client.post(
                f"/api/assessments/{token}/proctoring/events",
                json={"event_type": "face_missing", "duration_seconds": 12},
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["warning_count"], i)
            self.assertFalse(res.json()["flagged"])

        # 3rd warning reaches threshold
        with patch.object(main, "PROCTORING_AUTO_SUBMIT", True):
            res_flagged = self.client.post(
                f"/api/assessments/{token}/proctoring/events",
                json={"event_type": "face_missing", "duration_seconds": 15},
            )
            self.assertEqual(res_flagged.status_code, 200)
            self.assertEqual(res_flagged.json()["warning_count"], 3)
            self.assertTrue(res_flagged.json()["flagged"])
            self.assertTrue(res_flagged.json()["should_auto_submit"])

    def test_job_status_closed_filter(self):
        admin = self.login("admin@skillsphere.dev")
        candidate = self.login("candidate@skillsphere.dev")
        jobs = self.client.get("/api/jobs", headers=candidate).json()["items"]
        first_job = jobs[0]

        # Close the job as admin
        patch_res = self.client.patch(
            f"/api/admin/jobs/{first_job['id']}/status",
            headers=admin,
            json={"status": "Closed"},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["status"], "Closed")

        # Candidate should no longer see it in active jobs
        active_jobs = self.client.get("/api/jobs", headers=candidate).json()["items"]
        self.assertNotIn(first_job["id"], [j["id"] for j in active_jobs])

    def test_interview_realtime_websocket_workspace(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        schedule = self.client.post(
            f"/api/applications/{application['id']}/interviews",
            headers=recruiter,
            json={
                "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                "timezone": "UTC",
                "duration_minutes": 60,
                "meeting_platform": "Google Meet",
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "recruiter_message": "Looking forward to it",
            },
        )
        self.assertEqual(schedule.status_code, 201)
        interview_id = schedule.json()["id"]
        candidate_token = candidate["Authorization"].split(" ")[1]
        recruiter_token = recruiter["Authorization"].split(" ")[1]

        # Connect candidate to the websocket room
        with self.client.websocket_connect(f"/ws/interviews/{interview_id}?token={candidate_token}") as ws:
            init = ws.receive_json()
            self.assertEqual(init["type"], "sync")

            # Candidate sends code change
            ws.send_json({
                "type": "code_change",
                "code": "def solution():\n    return 42",
                "language": "python",
            })

        # Check DB persistence
        with main.connect() as db:
            row = db.execute("SELECT code, language FROM interviews WHERE id = ?", (interview_id,)).fetchone()
            self.assertEqual(row["code"], "def solution():\n    return 42")
            self.assertEqual(row["language"], "python")

        # Test invalid token rejected
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises((WebSocketDisconnect, Exception)):
            with self.client.websocket_connect(f"/ws/interviews/{interview_id}?token=invalid.token.here"):
                pass

    def test_candidate_can_view_own_completed_assessment_review_and_skill_gap_analysis(self):
        candidate, recruiter, application = self.apply_with_match(["Python", "SQL"], ["Python", "SQL"])
        self.create_job_question_bank(application["job_id"], technology="Python", count=10)
        self.create_job_question_bank(application["job_id"], technology="SQL", count=10)
        sent, token = self.send_assessment(application["id"])
        self.start_assessment_proctoring(token)
        self.client.post(f"/api/assessments/{token}/start")
        with main.connect() as db:
            assessment = db.execute("SELECT id FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()
            assessment_id = assessment["id"]
            questions = db.execute(
                "SELECT id, correct_index, technology FROM assessment_questions WHERE assessment_id = ?",
                (assessment_id,),
            ).fetchall()
        # Answer Python questions correctly, SQL questions incorrectly
        answers = {}
        for q in questions:
            if q["technology"] == "Python":
                answers[q["id"]] = q["correct_index"]
            else:
                answers[q["id"]] = (q["correct_index"] + 1) % 4
        submitted = self.client.post(f"/api/assessments/{token}/submit", json={"answers": answers})
        self.assertEqual(submitted.status_code, 200)

        # Candidate views own review
        review_res = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=candidate)
        self.assertEqual(review_res.status_code, 200)
        data = review_res.json()
        self.assertEqual(data["id"], assessment_id)
        self.assertIn("Python", data["strengths"])
        self.assertIn("SQL", data["areas_to_improve"])
        self.assertTrue(len(data["topic_breakdown"]) >= 2)
        self.assertTrue(len(data["recommendations"]) >= 1)
        self.assertTrue(len(data["questions"]) == 20)

    def test_candidate_cannot_view_another_candidates_assessment_review(self):
        candidate_a, recruiter, app_a = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(app_a["job_id"], technology="Python", count=20)
        sent, token = self.send_assessment(app_a["id"])
        self.start_assessment_proctoring(token)
        self.client.post(f"/api/assessments/{token}/start")
        with main.connect() as db:
            assessment_id = db.execute("SELECT id FROM assessments WHERE application_id = ?", (app_a["id"],)).fetchone()["id"]
            questions = db.execute("SELECT id, correct_index FROM assessment_questions WHERE assessment_id = ?", (assessment_id,)).fetchall()
        self.client.post(f"/api/assessments/{token}/submit", json={"answers": {q["id"]: q["correct_index"] for q in questions}})

        reg = self.client.post(
            "/api/auth/register",
            json={"name": "Other Candidate", "email": "other@skillsphere.dev", "password": "LongEnough9!", "role": "candidate"},
        )
        candidate_b = {"Authorization": f"Bearer {reg.json()['access_token']}"}
        forbidden = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=candidate_b)
        self.assertEqual(forbidden.status_code, 404)

    def test_detailed_results_setting_hides_correct_answers_from_candidate(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"], technology="Python", count=20)
        # Recruiter disables detailed candidate results
        patch_res = self.client.patch(
            f"/api/jobs/{application['job_id']}/assessment-settings",
            headers=recruiter,
            json={"assessment_question_count": 20, "allow_detailed_results": False},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["allow_detailed_results"], 0)

        sent, token = self.send_assessment(application["id"])
        self.start_assessment_proctoring(token)
        self.client.post(f"/api/assessments/{token}/start")
        with main.connect() as db:
            assessment_id = db.execute("SELECT id FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()["id"]
            questions = db.execute("SELECT id, correct_index FROM assessment_questions WHERE assessment_id = ?", (assessment_id,)).fetchall()
        # Submit all wrong answers
        self.client.post(f"/api/assessments/{token}/submit", json={"answers": {q["id"]: (q["correct_index"] + 1) % 4 for q in questions}})

        # Candidate view: correct_answer should be hidden (None)
        candidate_review = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=candidate).json()
        self.assertFalse(candidate_review["can_view_detailed_answers"])
        for q in candidate_review["questions"]:
            self.assertIsNone(q["correct_answer"])

        # Recruiter view: correct_answer should always be visible
        recruiter_review = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=recruiter).json()
        self.assertTrue(recruiter_review["can_view_detailed_answers"])
        for q in recruiter_review["questions"]:
            self.assertIsNotNone(q["correct_answer"])

    def test_recruiter_configurable_assessment_question_count(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        job_id = application["job_id"]
        # Validation bounds check (must be between 5 and 50)
        bad_low = self.client.patch(f"/api/jobs/{job_id}/assessment-settings", headers=recruiter, json={"assessment_question_count": 4, "allow_detailed_results": True})
        self.assertEqual(bad_low.status_code, 422)
        bad_high = self.client.patch(f"/api/jobs/{job_id}/assessment-settings", headers=recruiter, json={"assessment_question_count": 51, "allow_detailed_results": True})
        self.assertEqual(bad_high.status_code, 422)

        # Set to 10 questions
        set_10 = self.client.patch(f"/api/jobs/{job_id}/assessment-settings", headers=recruiter, json={"assessment_question_count": 10, "allow_detailed_results": True})
        self.assertEqual(set_10.status_code, 200)
        self.assertEqual(set_10.json()["assessment_question_count"], 10)

        # Create 12 questions (which satisfies >= 10)
        self.create_job_question_bank(job_id, technology="Python", count=12)
        summary = self.client.get(f"/api/jobs/{job_id}/question-bank-summary", headers=recruiter).json()
        self.assertEqual(summary["configured_question_count"], 10)
        self.assertTrue(summary["ready"])

        sent, token = self.send_assessment(application["id"])
        public_data = self.client.get(f"/api/assessments/{token}").json()
        self.assertEqual(public_data["total_questions"], 10)

        self.start_assessment_proctoring(token)
        started = self.client.post(f"/api/assessments/{token}/start").json()
        self.assertEqual(len(started["questions"]), 10)

    def test_question_bank_readiness_blocks_assessment_when_insufficient_questions(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        job_id = application["job_id"]
        # Set assessment question count to 25
        self.client.patch(f"/api/jobs/{job_id}/assessment-settings", headers=recruiter, json={"assessment_question_count": 25, "allow_detailed_results": True})
        # Add only 20 questions
        self.create_job_question_bank(job_id, technology="Python", count=20)
        summary = self.client.get(f"/api/jobs/{job_id}/question-bank-summary", headers=recruiter).json()
        self.assertEqual(summary["configured_question_count"], 25)
        self.assertFalse(summary["ready"])
        self.assertEqual(summary["additional_needed"], 5)

        # Sending assessment must fail with 409
        send_res = self.client.post(f"/api/applications/{application['id']}/assessment/send", headers=recruiter)
        self.assertEqual(send_res.status_code, 409)
        self.assertIn("Add 5 more active questions", send_res.json()["detail"])

    def test_candidate_and_recruiter_analytics_endpoints(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"], technology="Python", count=20)
        sent, token = self.send_assessment(application["id"])
        self.start_assessment_proctoring(token)
        self.client.post(f"/api/assessments/{token}/start")
        with main.connect() as db:
            assessment_id = db.execute("SELECT id FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()["id"]
            questions = db.execute("SELECT id, correct_index FROM assessment_questions WHERE assessment_id = ?", (assessment_id,)).fetchall()
        self.client.post(f"/api/assessments/{token}/submit", json={"answers": {q["id"]: q["correct_index"] for q in questions}})

        # Candidate analytics
        cand_analytics = self.client.get("/api/candidate/analytics", headers=candidate)
        self.assertEqual(cand_analytics.status_code, 200)
        c_data = cand_analytics.json()
        self.assertEqual(c_data["total_applications"], 1)
        self.assertEqual(c_data["assessments_completed"], 1)
        self.assertEqual(c_data["average_score_percentage"], 100.0)
        self.assertIn("Python", c_data["strengths"])

        # Recruiter analytics
        rec_analytics = self.client.get("/api/recruiter/analytics?report_range=this_week", headers=recruiter)
        self.assertEqual(rec_analytics.status_code, 200)
        r_data = rec_analytics.json()
        self.assertTrue(len(r_data["conversion_funnel"]) >= 5)
        self.assertTrue(r_data["summary"]["total_jobs"] >= 1)

        # Month filter check on admin analytics
        admin = self.login("admin@skillsphere.dev")
        admin_analytics = self.client.get("/api/admin/analytics?report_range=this_month", headers=admin)
        self.assertEqual(admin_analytics.status_code, 200)
        self.assertIn("conversion_funnel", admin_analytics.json()["weekly"])
        self.assertIn("weak_technologies", admin_analytics.json()["weekly"])


if __name__ == "__main__":
    unittest.main()
