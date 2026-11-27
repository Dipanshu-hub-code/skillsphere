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

    def register_and_login(self, name: str, email: str, password: str, role: str = "candidate") -> dict[str, str]:
        reg = self.client.post(
            "/api/auth/register",
            json={"name": name, "email": email, "password": password, "role": role},
        )
        self.assertEqual(reg.status_code, 201, reg.text)
        token = reg.json().get("dev_token")
        self.assertTrue(token, reg.text)
        verify = self.client.post("/api/auth/verify-email", json={"token": token})
        self.assertEqual(verify.status_code, 200, verify.text)
        return self.login(email, password)

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
        other_headers = self.register_and_login("Other Recruiter", "other@company.com", "LongEnough9!", "recruiter")
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
        disguised_eps = self.client.post(
            "/api/resume/upload", headers=candidate,
            files={"file": ("resume.jpg", b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 100\nshowpage", "image/jpeg")},
        )
        self.assertEqual(disguised_eps.status_code, 415)
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
            self.assertNotIn("score", submitted.json())
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

        outsider = self.register_and_login("Other Candidate", "other-candidate@example.com", "LongEnough9!", "candidate")
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
        other_recruiter = self.register_and_login("Other Recruiter", "other-recruiter@example.com", "LongEnough9!", "recruiter")
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

        outsider = self.register_and_login("Other Candidate", "other-candidate@example.com", "LongEnough9!", "candidate")
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
        other_headers = self.register_and_login("Other Recruiter", "other-bank@example.com", "LongEnough9!", "recruiter")
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
        other_headers = self.register_and_login("Other Recruiter", "other-import@example.com", "LongEnough9!", "recruiter")
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
        other_headers = self.register_and_login("Second Recruiter", "second-recruiter@example.com", "LongEnough9!", "recruiter")
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
        self.assertEqual(submitted.json()["status"], "Pending Recruiter Decision")
        self.assertNotIn("score", submitted.json())
        self.assertNotIn("result", submitted.json())
        self.assertEqual(submitted.json()["assessment_round"], "Round 1")
        self.assertIsNotNone(submitted.json()["time_taken_seconds"])
        repeated = self.client.post(f"/api/assessments/{token}/submit", json={"answers": answers})
        self.assertIn(repeated.status_code, {404, 409, 410})
        recruiter_applications = self.client.get("/api/recruiter/applicants", headers=recruiter).json()["items"]
        applicant = next(item for item in recruiter_applications if item["id"] == application["id"])
        self.assertEqual(applicant["assessment_status"], "Pending Recruiter Decision")
        self.assertEqual(applicant["assessment_result"], "Passed")
        self.assertEqual(applicant["status"], "Assessment Completed")
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
        # Candidate submit response does not reveal marks or pass/fail
        self.assertNotIn("score_percentage", result)
        self.assertNotIn("correct_answer_count", result)
        self.assertNotIn("incorrect_answer_count", result)
        self.assertNotIn("result", result)
        self.assertEqual(result["status"], "Pending Recruiter Decision")
        self.assertEqual(
            result["message"],
            "Your assessment has been submitted successfully. The recruiter will review your results and update your application status.",
        )

        with main.connect() as db:
            stored = db.execute(
                """SELECT id, total_questions, correct_answer_count, incorrect_answer_count,
                score_percentage, result, assessment_round, submitted_at, result_processed_at,
                time_taken_seconds, recruiter_decision FROM assessments WHERE application_id = ?""",
                (application["id"],),
            ).fetchone()
            assessment_id = stored["id"]
            self.assertEqual(stored["total_questions"], 20)
            self.assertEqual(stored["correct_answer_count"], 15)
            self.assertEqual(stored["incorrect_answer_count"], 5)
            self.assertEqual(stored["score_percentage"], 75)
            self.assertEqual(stored["result"], "Passed")
            self.assertEqual(stored["assessment_round"], "Round 1")
            self.assertEqual(stored["submitted_at"], stored["result_processed_at"])
            self.assertIsNotNone(stored["time_taken_seconds"])
            self.assertIsNone(stored["recruiter_decision"])
            current_status = db.execute(
                "SELECT status FROM applications WHERE id = ?", (application["id"],)
            ).fetchone()["status"]
        self.assertEqual(current_status, "Assessment Completed")

        # Candidate notification is neutral without marks
        candidate_notifications = self.client.get("/api/notifications", headers=candidate).json()["items"]
        recruiter_notifications = self.client.get("/api/notifications", headers=recruiter).json()["items"]
        admin = self.login("admin@skillsphere.dev")
        admin_notifications = self.client.get("/api/notifications", headers=admin).json()["items"]
        candidate_result = next(item for item in candidate_notifications if item["type"] == "assessment_submitted")
        self.assertEqual(candidate_result["title"], "Assessment submitted")
        self.assertIn(application["title"], candidate_result["message"])
        self.assertNotIn("75%", candidate_result["message"])
        self.assertIn("recruiter will review", candidate_result["message"])

        # Candidate assessment dashboard hides marks and result
        candidate_assessment = self.client.get("/api/assessments", headers=candidate).json()["items"][0]
        self.assertIsNone(candidate_assessment["result"])
        self.assertIsNone(candidate_assessment["score"])
        self.assertIsNone(candidate_assessment["score_percentage"])
        self.assertIsNone(candidate_assessment["correct_answer_count"])
        self.assertNotIn("correct_index", candidate_assessment)

        # Candidate cannot access assessment review directly (403)
        candidate_review = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=candidate)
        self.assertEqual(candidate_review.status_code, 403)

        # Unauthorized recruiter cannot view review (404)
        other_recruiter = self.register_and_login("Second Recruiter", "second-recruiter@example.com", "LongEnough9!", "recruiter")
        self.assertEqual(
            self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=other_recruiter).status_code,
            404,
        )

        # Authorized recruiter sees assessment results
        recruiter_review = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=recruiter)
        self.assertEqual(recruiter_review.status_code, 200)
        self.assertEqual(recruiter_review.json()["score_percentage"], 75)
        self.assertEqual(recruiter_review.json()["result"], "Passed")

        # Recruiter makes manual decision: Pass Candidate
        decision_res = self.client.post(
            f"/api/assessments/{assessment_id}/decision",
            headers=recruiter,
            json={"decision": "Pass", "notes": "Strong candidate"},
        )
        self.assertEqual(decision_res.status_code, 200)
        self.assertEqual(decision_res.json()["recruiter_decision"], "Passed")
        self.assertEqual(decision_res.json()["application_status"], "Round 1 Passed")

        with main.connect() as db:
            app_status = db.execute(
                "SELECT status FROM applications WHERE id = ?", (application["id"],)
            ).fetchone()["status"]
        self.assertEqual(app_status, "Round 1 Passed")

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

    def test_round1_fourteen_of_twenty_fails(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        _, result = self.complete_assessment(application, 14)
        self.assertEqual(result["status"], "Pending Recruiter Decision")
        self.assertNotIn("score_percentage", result)
        self.assertNotIn("result", result)

        with main.connect() as db:
            stored = db.execute("SELECT id, result, score_percentage FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()
            assessment_id = stored["id"]
            self.assertEqual(stored["result"], "Failed")
            self.assertEqual(stored["score_percentage"], 70)

        # Recruiter makes manual decision: Fail Candidate
        decision_res = self.client.post(
            f"/api/assessments/{assessment_id}/decision",
            headers=recruiter,
            json={"decision": "Fail", "notes": "Did not meet passing score"},
        )
        self.assertEqual(decision_res.status_code, 200)
        self.assertEqual(decision_res.json()["recruiter_decision"], "Failed")
        self.assertEqual(decision_res.json()["application_status"], "Round 1 Failed")

        with main.connect() as db:
            app_status = db.execute("SELECT status FROM applications WHERE id = ?", (application["id"],)).fetchone()["status"]
        self.assertEqual(app_status, "Round 1 Failed")

    def test_round1_score_above_threshold_passes(self):
        _, _, application = self.apply_with_match(["Python"], ["Python"])
        _, result = self.complete_assessment(application, 16)
        self.assertEqual(result["status"], "Pending Recruiter Decision")
        with main.connect() as db:
            stored = db.execute("SELECT score_percentage, result FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()
        self.assertEqual(stored["score_percentage"], 80)
        self.assertEqual(stored["result"], "Passed")

    def test_round1_pass_threshold_is_configurable_for_new_assessments(self):
        with patch.object(main, "ROUND1_PASS_PERCENTAGE", 80):
            _, _, application = self.apply_with_match(["Python"], ["Python"])
            _, result = self.complete_assessment(application, 15)
        self.assertEqual(result["status"], "Pending Recruiter Decision")
        with main.connect() as db:
            stored = db.execute("SELECT pass_percentage, score_percentage, result FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()
        self.assertEqual(stored["pass_percentage"], 80)
        self.assertEqual(stored["score_percentage"], 75)
        self.assertEqual(stored["result"], "Failed")

    def test_migration_backfills_existing_completed_assessment_results(self):
        _, _, application = self.apply_with_match(["Python"], ["Python"])
        _, result = self.complete_assessment(application, 15)
        with main.connect() as db:
            stored_res = db.execute("SELECT result FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()
        self.assertEqual(stored_res["result"], "Passed")
        with main.connect() as db:
            db.execute(
                """UPDATE assessments SET result = NULL, score_percentage = NULL,
                correct_answer_count = NULL, incorrect_answer_count = NULL,
                result_processed_at = NULL, status = 'Completed', recruiter_decision = 'Passed' WHERE application_id = ?""",
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
        self.assertEqual(timed_out.json()["status"], "Pending Recruiter Decision")
        self.assertNotIn("score", timed_out.json())
        self.assertNotIn("result", timed_out.json())
        reused = self.client.post(f"/api/assessments/{token}/submit", json={"answers": {}})
        self.assertIn(reused.status_code, {404, 409, 410})
        with main.connect() as db:
            status, submitted_at, result = db.execute(
                "SELECT status, submitted_at, result FROM assessments WHERE application_id = ?", (application["id"],)
            ).fetchone()
        self.assertEqual(status, "Pending Recruiter Decision")
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
        other_recruiter = self.register_and_login("Other Recruiter", "other-recruiter@example.com", "LongEnough9!", "recruiter")
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

        # Candidate cannot access confidential assessment review (403 Forbidden)
        review_res = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=candidate)
        self.assertEqual(review_res.status_code, 403)
        self.assertEqual(review_res.json()["detail"], "Assessment results are confidential and under recruiter review")

        # Recruiter views candidate review and skill gap analysis
        recruiter_res = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=recruiter)
        self.assertEqual(recruiter_res.status_code, 200)
        data = recruiter_res.json()
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

        candidate_b = self.register_and_login("Other Candidate", "other@skillsphere.dev", "LongEnough9!", "candidate")
        forbidden = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=candidate_b)
        self.assertEqual(forbidden.status_code, 403)

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

        # Candidate view: confidential (403)
        candidate_review = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=candidate)
        self.assertEqual(candidate_review.status_code, 403)

        # Recruiter view: correct_answer should always be visible
        recruiter_review = self.client.get(f"/api/assessment-reviews/{assessment_id}", headers=recruiter).json()
        self.assertTrue(recruiter_review["can_view_detailed_answers"])
        for q in recruiter_review["questions"]:
            self.assertIsNotNone(q["correct_answer"])

    def test_job_description_minimum_twenty_characters_validation(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        # Description under 20 characters rejected with exact validation message
        short_res = self.client.post(
            "/api/jobs",
            headers=recruiter,
            json={
                "title": "Software Engineer",
                "company": "Acme Inc",
                "description": "Too short",
                "location": "Remote",
                "experience": "2 years",
                "skills": ["Python"],
            },
        )
        self.assertEqual(short_res.status_code, 422)
        self.assertEqual(
            short_res.json()["detail"],
            "Job description must contain at least 20 characters before you can post this job.",
        )

        # Description with spaces padding but under 20 trimmed chars rejected
        padded_res = self.client.post(
            "/api/jobs",
            headers=recruiter,
            json={
                "title": "Software Engineer",
                "company": "Acme Inc",
                "description": "      under 20      ",
                "location": "Remote",
                "experience": "2 years",
                "skills": ["Python"],
            },
        )
        self.assertEqual(padded_res.status_code, 422)
        self.assertEqual(
            padded_res.json()["detail"],
            "Job description must contain at least 20 characters before you can post this job.",
        )

        # Description with 20 or more characters accepted
        valid_res = self.client.post(
            "/api/jobs",
            headers=recruiter,
            json={
                "title": "Software Engineer",
                "company": "Acme Inc",
                "description": "This is a comprehensive description with over twenty characters.",
                "location": "Remote",
                "experience": "2 years",
                "skills": ["Python"],
            },
        )
        self.assertEqual(valid_res.status_code, 201)
        self.assertEqual(valid_res.json()["title"], "Software Engineer")

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
        self.assertIsNone(c_data["average_score_percentage"])
        self.assertEqual(c_data["strengths"], [])

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

    def test_ai_question_generator_and_batch_add(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        candidate = self.login("candidate@skillsphere.dev")
        job = self.client.get("/api/jobs?mine=true", headers=recruiter).json()["items"][0]

        # Candidate cannot access generator
        cand_res = self.client.post(
            f"/api/jobs/{job['id']}/questions/ai-generate",
            headers=candidate,
            json={"technology": "Python", "count": 5, "difficulty": "Medium"},
        )
        self.assertEqual(cand_res.status_code, 403)

        # Recruiter generates questions
        gen_res = self.client.post(
            f"/api/jobs/{job['id']}/questions/ai-generate",
            headers=recruiter,
            json={"technology": "Python", "count": 5, "difficulty": "Mixed", "subtopic": "Data Structures"},
        )
        self.assertEqual(gen_res.status_code, 200, gen_res.text)
        data = gen_res.json()
        self.assertEqual(data["technology"], "Python")
        self.assertEqual(len(data["questions"]), 5)

        # Recruiter generates questions with null subtopic and null custom_prompt
        gen_null_res = self.client.post(
            f"/api/jobs/{job['id']}/questions/ai-generate",
            headers=recruiter,
            json={"technology": "Python", "count": 5, "difficulty": "Mixed", "subtopic": None, "custom_prompt": None},
        )
        self.assertEqual(gen_null_res.status_code, 200, gen_null_res.text)
        self.assertEqual(len(gen_null_res.json()["questions"]), 5)
        for q in data["questions"]:
            self.assertEqual(q["technology"], "Python")
            self.assertEqual(len(q["options"]), 4)
            self.assertIn(q["correct_index"], [0, 1, 2, 3])
            self.assertTrue(len(q["question"]) > 5)
            self.assertTrue(q["active"])

        # Batch insert questions
        batch_res = self.client.post(
            f"/api/jobs/{job['id']}/questions/batch",
            headers=recruiter,
            json={"questions": data["questions"]},
        )
        self.assertEqual(batch_res.status_code, 201, batch_res.text)
        self.assertEqual(batch_res.json()["created"], 5)
        self.assertEqual(batch_res.json()["count"], 5)

        # Re-saving duplicate questions must not fail with 500 error
        dup_res = self.client.post(
            f"/api/jobs/{job['id']}/questions/batch",
            headers=recruiter,
            json={"questions": data["questions"]},
        )
        self.assertEqual(dup_res.status_code, 201, dup_res.text)
        self.assertEqual(dup_res.json()["created"], 0)
        self.assertEqual(dup_res.json()["count"], 0)
        self.assertEqual(dup_res.json()["skipped"], 5)

        # Validation test: invalid questions must be counted in 'invalid' and not throw 500
        invalid_batch = self.client.post(
            f"/api/jobs/{job['id']}/questions/batch",
            headers=recruiter,
            json={
                "questions": [
                    {"technology": "", "question": "Too short", "options": ["A", "B", "C", "D"], "correct_index": 0},
                    {"technology": "Python", "question": "What is Python?", "options": ["A", "A", "C", "D"], "correct_index": 0},
                    {"technology": "Python", "question": "Valid question text here?", "options": ["1", "2", "3", "4"], "correct_index": 99},
                ]
            },
        )
        self.assertEqual(invalid_batch.status_code, 201)
        self.assertEqual(invalid_batch.json()["invalid"], 3)
        self.assertEqual(invalid_batch.json()["created"], 0)

        # Verify added to question bank
        bank_res = self.client.get(f"/api/jobs/{job['id']}/questions", headers=recruiter)
        self.assertEqual(bank_res.status_code, 200)
        self.assertEqual(bank_res.json()["active_count"], 5)

        # Unauthorized recruiter cannot batch add to this job
        other_recruiter = self.register_and_login("Other Recruiter", "other_rec@skillsphere.dev", "Demo123!", "recruiter")
        other_batch = self.client.post(
            f"/api/jobs/{job['id']}/questions/batch",
            headers=other_recruiter,
            json={"questions": data["questions"]},
        )
        self.assertEqual(other_batch.status_code, 404)

    def test_delete_job_posting_by_recruiter(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        candidate = self.login("candidate@skillsphere.dev")
        admin = self.login("admin@skillsphere.dev")

        # Create a new job
        created = self.client.post(
            "/api/jobs",
            headers=recruiter,
            json={
                "title": "Site Reliability Engineer",
                "company": "Cloud Corp",
                "description": "Looking for an SRE with strong Linux, Kubernetes, and Python automation skills.",
                "location": "Remote",
                "experience": "4+ years",
                "job_type": "Full-time",
                "salary": "$130,000",
                "skills": ["Python", "Kubernetes", "Linux"],
                "assessment_question_count": 5,
                "allow_detailed_results": True,
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        job_id = created.json()["id"]

        # Candidate cannot delete job
        cand_del = self.client.delete(f"/api/jobs/{job_id}", headers=candidate)
        self.assertEqual(cand_del.status_code, 403)

        # Other recruiter cannot delete job
        other_recruiter = self.register_and_login("Other Recruiter", "sre_other_rec@skillsphere.dev", "Demo123!", "recruiter")
        other_del = self.client.delete(f"/api/jobs/{job_id}", headers=other_recruiter)
        self.assertEqual(other_del.status_code, 404)

        # Owner recruiter deletes job successfully
        del_res = self.client.delete(f"/api/jobs/{job_id}", headers=recruiter)
        self.assertEqual(del_res.status_code, 200, del_res.text)
        self.assertTrue(del_res.json()["deleted"])
        self.assertTrue(del_res.json().get("archived"))

        # Job is now 404 for active candidate/public view
        get_res = self.client.get(f"/api/jobs/{job_id}", headers=recruiter)
        self.assertEqual(get_res.status_code, 404)

        # Candidate attempting to apply to archived job gets 404
        apply_res = self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate)
        self.assertEqual(apply_res.status_code, 404)

        # Admin can also delete jobs
        admin_job = self.client.post(
            "/api/jobs",
            headers=recruiter,
            json={
                "title": "Data Analyst Role",
                "company": "Data Corp",
                "description": "Looking for a data analyst with strong SQL, Python, and Tableau experience.",
                "location": "Remote",
                "experience": "2+ years",
                "job_type": "Full-time",
                "salary": "$90,000",
                "skills": ["Python", "SQL"],
                "assessment_question_count": 5,
                "allow_detailed_results": True,
            },
        ).json()
        admin_del = self.client.delete(f"/api/jobs/{admin_job['id']}", headers=admin)
        self.assertEqual(admin_del.status_code, 200, admin_del.text)
        self.assertTrue(admin_del.json()["deleted"])
        self.assertTrue(admin_del.json().get("archived"))

    def test_workspace_bootstrap(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        candidate = self.login("candidate@skillsphere.dev")
        admin = self.login("admin@skillsphere.dev")

        # Candidate bootstrap
        c_boot = self.client.get("/api/workspace/bootstrap", headers=candidate)
        self.assertEqual(c_boot.status_code, 200)
        c_data = c_boot.json()
        self.assertIn("user", c_data)
        self.assertIn("jobs", c_data)
        self.assertIn("applications", c_data)
        self.assertIn("interviews", c_data)
        self.assertIn("notifications", c_data)
        self.assertEqual(c_data["user"]["role"], "candidate")

        # Recruiter bootstrap
        r_boot = self.client.get("/api/workspace/bootstrap", headers=recruiter)
        self.assertEqual(r_boot.status_code, 200)
        r_data = r_boot.json()
        self.assertEqual(r_data["user"]["role"], "recruiter")
        self.assertIn("jobs", r_data)

        # Admin bootstrap
        a_boot = self.client.get("/api/workspace/bootstrap", headers=admin)
        self.assertEqual(a_boot.status_code, 200)
        a_data = a_boot.json()
        self.assertEqual(a_data["user"]["role"], "admin")

    def test_candidate_ai_screening_summary(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"], technology="Python", count=20)
        sent, token = self.send_assessment(application["id"])
        self.start_assessment_proctoring(token)
        self.client.post(f"/api/assessments/{token}/start")

        # Record a proctoring event
        self.client.post(
            f"/api/assessments/{token}/proctoring/events",
            json={"event_type": "tab_hidden", "severity": "warning", "message": "Tab hidden", "duration_seconds": 2},
        )

        with main.connect() as db:
            assessment_id = db.execute("SELECT id FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()["id"]
            questions = db.execute("SELECT id, correct_index FROM assessment_questions WHERE assessment_id = ?", (assessment_id,)).fetchall()
        # Answer all correctly
        self.client.post(f"/api/assessments/{token}/submit", json={"answers": {q["id"]: q["correct_index"] for q in questions}})

        # Recruiter fetches AI screening summary
        summary_res = self.client.get(f"/api/applications/{application['id']}/ai-summary", headers=recruiter)
        self.assertEqual(summary_res.status_code, 200, summary_res.text)
        summary = summary_res.json()
        self.assertIn(summary["recommendation"], ["Strong Hire", "Recommended", "Borderline", "Not Recommended"])
        self.assertTrue(len(summary["summary"]) > 20)
        self.assertIsInstance(summary["strengths"], list)
        self.assertIsInstance(summary["risks"], list)
        self.assertIsInstance(summary["interview_questions"], list)
        self.assertEqual(len(summary["interview_questions"]), 3)
        self.assertEqual(summary["assessment_score_percentage"], 100.0)
        self.assertEqual(summary["proctoring_warning_count"], 1)

        # Refresh AI screening summary
        refresh_res = self.client.post(f"/api/applications/{application['id']}/ai-summary", headers=recruiter)
        self.assertEqual(refresh_res.status_code, 200, refresh_res.text)

        # Admin can view
        admin = self.login("admin@skillsphere.dev")
        admin_res = self.client.get(f"/api/applications/{application['id']}/ai-summary", headers=admin)
        self.assertEqual(admin_res.status_code, 200)

        # Candidate is forbidden
        cand_summary = self.client.get(f"/api/applications/{application['id']}/ai-summary", headers=candidate)
        self.assertEqual(cand_summary.status_code, 403)

        # Other recruiter is forbidden (isolated as 404)
        other_rec = self.register_and_login("Other Recruiter 2", "other_rec2@skillsphere.dev", "Demo123!", "recruiter")
        other_summary = self.client.get(f"/api/applications/{application['id']}/ai-summary", headers=other_rec)
        self.assertEqual(other_summary.status_code, 404)

    def test_proctoring_recording_consent_required_before_assessment_start(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])

        # Attempt to start without consent or camera permission
        res_no_consent = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(res_no_consent.status_code, 409)
        self.assertIn("consent", res_no_consent.json()["detail"].lower())

        # Grant proctoring and recording consent
        proctor_start = self.client.post(
            f"/api/assessments/{token}/proctoring/start",
            json={
                "consent_given": True,
                "camera_permission_granted": True,
                "recording_consent_given": True,
                "recording_consent_version": "1.0",
            },
        )
        self.assertEqual(proctor_start.status_code, 200)
        self.assertTrue(proctor_start.json()["started"])

        # Now start assessment succeeds
        res_start = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(res_start.status_code, 200)
        self.assertEqual(res_start.json()["status"], "Started")

    def test_proctoring_recording_full_lifecycle_and_local_stream(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])

        # Start proctoring with consent
        self.client.post(
            f"/api/assessments/{token}/proctoring/start",
            json={
                "consent_given": True,
                "camera_permission_granted": True,
                "recording_consent_given": True,
                "recording_consent_version": "1.0",
            },
        )
        self.client.post(f"/api/assessments/{token}/start")

        # Start recording endpoint
        rec_start_res = self.client.post(
            f"/api/assessments/{token}/recording/start",
            json={"consent": True, "consent_version": "1.0"},
        )
        self.assertEqual(rec_start_res.status_code, 200)
        recording_id = rec_start_res.json()["recording_id"]

        # Chunk upload
        chunk_data = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01" # sample webm header
        chunk_res = self.client.post(
            f"/api/assessments/{token}/recording/chunk?chunk_index=0",
            files={"chunk": ("chunk_00000.part", chunk_data, "video/webm")},
        )
        self.assertEqual(chunk_res.status_code, 200)

        # Candidate submits answers
        with main.connect() as db:
            assessment_id = db.execute("SELECT id FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()["id"]
            questions = db.execute("SELECT id, correct_index FROM assessment_questions WHERE assessment_id = ?", (assessment_id,)).fetchall()
        sub_res = self.client.post(f"/api/assessments/{token}/submit", json={"answers": {q["id"]: q["correct_index"] for q in questions}})
        self.assertEqual(sub_res.status_code, 200)

        # Video recording completion completes asynchronously after test submission
        complete_res = self.client.post(
            f"/api/assessments/{token}/recording/complete?duration_seconds=45",
            files={"video": ("webcam.webm", chunk_data + b"\x00" * 128, "video/webm")},
        )
        self.assertEqual(complete_res.status_code, 200)
        self.assertEqual(complete_res.json()["recording_status"], "Uploaded")

        # Admin views playback and audit log is created
        admin = self.login("admin@skillsphere.dev")
        playback_res = self.client.get(f"/api/admin/recordings/{recording_id}/playback", headers=admin)
        self.assertEqual(playback_res.status_code, 200)
        p_data = playback_res.json()
        self.assertIn("playback_url", p_data)
        self.assertEqual(p_data["recording_status"], "Uploaded")

        # Verify audit log recorded
        with main.connect() as db:
            audit = db.execute("SELECT * FROM recording_access_logs WHERE recording_id = ?", (recording_id,)).fetchone()
            self.assertIsNotNone(audit)

        # Admin video stream endpoint works
        stream_res = self.client.get(p_data["playback_url"], headers=admin)
        self.assertEqual(stream_res.status_code, 200)

        # Candidate and Recruiter forbidden from accessing playback URL
        rec_forbidden = self.client.get(f"/api/admin/recordings/{recording_id}/playback", headers=recruiter)
        self.assertEqual(rec_forbidden.status_code, 403)
        cand_forbidden = self.client.get(f"/api/admin/recordings/{recording_id}/playback", headers=candidate)
        self.assertEqual(cand_forbidden.status_code, 403)

    def test_proctoring_recording_failure_resilience_and_admin_notification(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])

        self.client.post(
            f"/api/assessments/{token}/proctoring/start",
            json={"consent_given": True, "camera_permission_granted": True, "recording_consent_given": True},
        )
        self.client.post(f"/api/assessments/{token}/start")

        # Simulate Cloudinary upload failure
        with patch.object(main, "init_cloudinary", return_value=True):
            with patch.object(main.cloudinary.uploader, "upload", side_effect=Exception("Cloudinary timeout")):
                fail_res = self.client.post(
                    f"/api/assessments/{token}/recording/complete?duration_seconds=30",
                    files={"video": ("webcam.webm", b"some_video_data", "video/webm")},
                )
                self.assertEqual(fail_res.status_code, 200)
                self.assertEqual(fail_res.json()["recording_status"], "Failed")

        # Verify admin received notification of upload failure
        with main.connect() as db:
            admin_notif = db.execute(
                "SELECT * FROM notifications WHERE type = 'recording_failed'",
            ).fetchone()
            self.assertIsNotNone(admin_notif)

    def test_recording_retention_cleanup(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])

        self.client.post(
            f"/api/assessments/{token}/proctoring/start",
            json={"consent_given": True, "camera_permission_granted": True, "recording_consent_given": True},
        )
        self.client.post(f"/api/assessments/{token}/start")
        self.client.post(
            f"/api/assessments/{token}/recording/complete?duration_seconds=20",
            files={"video": ("webcam.webm", b"video_bytes_retention", "video/webm")},
        )

        admin = self.login("admin@skillsphere.dev")

        # Artificially age the recording by 60 days
        with main.connect() as db:
            past_date = "2020-01-01T00:00:00"
            db.execute("UPDATE assessment_recordings SET created_at = ?", (past_date,))

        # Run retention cleanup
        cleanup_res = self.client.post("/api/admin/recordings/retention-cleanup", headers=admin)
        self.assertEqual(cleanup_res.status_code, 200)
        self.assertGreaterEqual(cleanup_res.json()["cleaned_count"], 1)

    def test_live_assessment_monitoring_full_flow(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        self.create_job_question_bank(application["job_id"])
        _, token = self.send_assessment(application["id"])

        # Candidate gives consent and enables camera
        proctoring = self.start_assessment_proctoring(token)
        self.assertTrue(proctoring["started"])

        # Start assessment
        started = self.client.post(f"/api/assessments/{token}/start")
        self.assertEqual(started.status_code, 200)

        with main.connect() as db:
            assessment_row = db.execute("SELECT id FROM assessments WHERE application_id = ?", (application["id"],)).fetchone()
            assessment_id = assessment_row["id"]

        # 1. Recruiter checks active assessments list
        res = self.client.get("/api/recruiter/live-assessments", headers=recruiter)
        self.assertEqual(res.status_code, 200)
        items = res.json()["items"]
        self.assertTrue(any(item["assessment_id"] == assessment_id for item in items))
        item = next(item for item in items if item["assessment_id"] == assessment_id)
        self.assertEqual(item["status"], "Assessment In Progress")
        self.assertEqual(item["camera_status"], "Monitoring Unavailable")
        self.assertFalse(item["monitoring_available"])

        # 2. Unauthorized recruiter cannot access candidate live monitoring
        unauth_headers = self.register_and_login("Other Recruiter", "other_recruiter@skillsphere.dev", "Password123!", "recruiter")
        unauth_token = unauth_headers["Authorization"].split(" ")[1]

        unauth_list = self.client.get("/api/recruiter/live-assessments", headers=unauth_headers).json()["items"]
        self.assertFalse(any(it["assessment_id"] == assessment_id for it in unauth_list))

        unauth_status = self.client.get(f"/api/assessments/{assessment_id}/live-status", headers=unauth_headers)
        self.assertEqual(unauth_status.status_code, 403)

        # Candidate cannot access recruiter live monitoring endpoint
        candidate_list_res = self.client.get("/api/recruiter/live-assessments", headers=candidate)
        self.assertEqual(candidate_list_res.status_code, 403)

        # 3. Unauthorized recruiter WebSocket is rejected with code 1008
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises((WebSocketDisconnect, Exception)):
            with self.client.websocket_connect(f"/ws/live-monitoring/recruiter/{assessment_id}?token={unauth_token}"):
                pass

        # Candidate with invalid token is rejected
        with self.assertRaises((WebSocketDisconnect, Exception)):
            with self.client.websocket_connect(f"/ws/live-monitoring/candidate/invalid_token_123"):
                pass

        # 4. Candidate connects WebSocket and reports camera connected
        recruiter_jwt = recruiter["Authorization"].split(" ")[1]

        with self.client.websocket_connect(f"/ws/live-monitoring/candidate/{token}") as cand_ws:
            cand_ws.send_json({"type": "camera_status", "connected": True})

            # Check recruiter live status endpoint
            live_status = self.client.get(f"/api/assessments/{assessment_id}/live-status", headers=recruiter).json()
            self.assertTrue(live_status["candidate_connected"])
            self.assertTrue(live_status["camera_connected"])
            self.assertEqual(live_status["camera_status"], "Camera Connected")
            self.assertTrue(live_status["monitoring_available"])

            # 5. Authorized recruiter connects to live monitoring WebSocket
            with self.client.websocket_connect(f"/ws/live-monitoring/recruiter/{assessment_id}?token={recruiter_jwt}") as rec_ws:
                # Recruiter receives initial candidate status
                init_msg = rec_ws.receive_json()
                self.assertEqual(init_msg["type"], "candidate_status")
                self.assertTrue(init_msg["connected"])
                self.assertTrue(init_msg["camera_connected"])

                # Candidate receives viewer_joined event
                joined_msg = cand_ws.receive_json()
                self.assertEqual(joined_msg["type"], "viewer_joined")

                # Recruiter requests stream
                rec_ws.send_json({"type": "request_stream"})

                # Candidate receives request_offer
                req_offer = cand_ws.receive_json()
                self.assertEqual(req_offer["type"], "request_offer")

                # Candidate sends WebRTC offer
                sample_offer_sdp = {"type": "offer", "sdp": "v=0\r\no=candidate 123 2 IN IP4 127.0.0.1..."}
                cand_ws.send_json({"type": "webrtc_offer", "sdp": sample_offer_sdp})

                # Recruiter receives WebRTC offer
                rec_offer = rec_ws.receive_json()
                self.assertEqual(rec_offer["type"], "webrtc_offer")
                self.assertEqual(rec_offer["sdp"], sample_offer_sdp)

                # Recruiter sends WebRTC answer
                sample_answer_sdp = {"type": "answer", "sdp": "v=0\r\no=recruiter 456 2 IN IP4 127.0.0.1..."}
                rec_ws.send_json({"type": "webrtc_answer", "sdp": sample_answer_sdp})

                # Candidate receives WebRTC answer
                cand_answer = cand_ws.receive_json()
                self.assertEqual(cand_answer["type"], "webrtc_answer")
                self.assertEqual(cand_answer["sdp"], sample_answer_sdp)

                # Candidate sends ICE candidate
                sample_ice = {"candidate": "candidate:1 1 UDP 2130706431 192.168.1.1 50000 typ host"}
                cand_ws.send_json({"type": "ice_candidate", "candidate": sample_ice})

                # Recruiter receives ICE candidate
                rec_ice = rec_ws.receive_json()
                self.assertEqual(rec_ice["type"], "ice_candidate")
                self.assertEqual(rec_ice["candidate"], sample_ice)

                # Recruiter stops viewing
                rec_ws.send_json({"type": "stop_viewing"})

            # Candidate receives viewer_left event
            left_msg = cand_ws.receive_json()
            self.assertEqual(left_msg["type"], "viewer_left")

            # Camera disconnection test: candidate sends camera disconnected
            cand_ws.send_json({"type": "camera_status", "connected": False})

            cam_off_status = self.client.get(f"/api/assessments/{assessment_id}/live-status", headers=recruiter).json()
            self.assertFalse(cam_off_status["camera_connected"])
            self.assertEqual(cam_off_status["camera_status"], "Camera Disconnected")
            self.assertFalse(cam_off_status["monitoring_available"])

            # Candidate submits assessment
            cand_ws.send_json({"type": "assessment_submitted"})

        # Candidate WebSocket is now closed; check live status
        final_status = self.client.get(f"/api/assessments/{assessment_id}/live-status", headers=recruiter).json()
        self.assertFalse(final_status["candidate_connected"])

        # Verify no video recording or frames were saved to database for live viewing
        with main.connect() as db:
            video_rows = db.execute("SELECT * FROM assessment_recordings WHERE assessment_id = ?", (assessment_id,)).fetchall()
            self.assertEqual(len(video_rows), 0)

    def test_rule_based_career_chatbot_health_and_faqs(self):
        # Health endpoint is public and reports rule-based engine
        health = self.client.get("/api/chat/health").json()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["engine"], "rule-based")
        self.assertFalse(health["external_ai_required"])
        self.assertGreaterEqual(health["total_intents"], 40)

        # FAQs require candidate role
        recruiter = self.login("recruiter@skillsphere.dev")
        cand_headers = self.login("candidate@skillsphere.dev")
        self.assertEqual(self.client.get("/api/chat/faqs").status_code, 401)
        self.assertEqual(self.client.get("/api/chat/faqs", headers=recruiter).status_code, 403)

        faqs = self.client.get("/api/chat/faqs", headers=cand_headers)
        self.assertEqual(faqs.status_code, 200)
        faq_data = faqs.json()
        self.assertIn("categories", faq_data)
        self.assertGreaterEqual(faq_data["total_faqs"], 40)

    def test_rule_based_career_chatbot_conversation_flow(self):
        candidate = self.login("candidate@skillsphere.dev")

        # 1. Technical concept question (OOP)
        oop_res = self.client.post(
            "/api/chat",
            headers=candidate,
            json={"message": "Explain OOP concepts in simple terms"},
        )
        self.assertEqual(oop_res.status_code, 200)
        oop_data = oop_res.json()
        self.assertEqual(oop_data["intent"], "tech_oop")
        self.assertGreaterEqual(oop_data["confidence"], 0.4)
        self.assertIn("Encapsulation", oop_data["message"])
        self.assertIn("Polymorphism", oop_data["message"])
        session_id = oop_data["session_id"]
        self.assertIsInstance(session_id, int)
        self.assertIsInstance(oop_data["suggested_follow_ups"], list)

        # 2. Context-aware follow up: "Give me an example"
        ex_res = self.client.post(
            "/api/chat",
            headers=candidate,
            json={"message": "Give me an example", "session_id": session_id},
        )
        self.assertEqual(ex_res.status_code, 200)
        ex_data = ex_res.json()
        self.assertEqual(ex_data["intent"], "tech_oop_example")
        self.assertEqual(ex_data["session_id"], session_id)

        # 3. HR Question: "How to answer tell me about yourself?"
        hr_res = self.client.post(
            "/api/chat",
            headers=candidate,
            json={"message": "How to answer tell me about yourself?", "session_id": session_id},
        )
        self.assertEqual(hr_res.status_code, 200)
        hr_data = hr_res.json()
        self.assertEqual(hr_data["intent"], "hr_introduction")
        self.assertIn("Present", hr_data["message"])

        # 4. Unknown question gracefully falls back
        unknown_res = self.client.post(
            "/api/chat",
            headers=candidate,
            json={"message": "xyzzy blorp qwerty 123456", "session_id": session_id},
        )
        self.assertEqual(unknown_res.status_code, 200)
        unknown_data = unknown_res.json()
        self.assertEqual(unknown_data["intent"], "fallback_unknown")
        self.assertIn("popular topics", unknown_data["message"])

        # 5. Backward compatibility endpoint /api/candidate/ai/chat
        compat_res = self.client.post(
            "/api/candidate/ai/chat",
            headers=candidate,
            json={"message": "How do I apply for jobs?", "session_id": session_id},
        )
        self.assertEqual(compat_res.status_code, 200)
        compat_data = compat_res.json()
        self.assertEqual(compat_data["intent"], "platform_apply_job")

        # 6. Session history inspection
        history_res = self.client.get(f"/api/chat/history/{session_id}", headers=candidate)
        self.assertEqual(history_res.status_code, 200)
        history_data = history_res.json()
        self.assertEqual(history_data["session"]["id"], session_id)
        self.assertGreaterEqual(len(history_data["messages"]), 8)

        # 7. Unauthorized recruiter cannot access candidate chat history
        recruiter = self.login("recruiter@skillsphere.dev")
        self.assertEqual(self.client.get(f"/api/chat/history/{session_id}", headers=recruiter).status_code, 403)

        # 8. Another candidate cannot access first candidate's chat history
        cand2_headers = self.register_and_login("Candidate Two", "cand2@example.com", "Password123!", "candidate")
        self.assertEqual(self.client.get(f"/api/chat/history/{session_id}", headers=cand2_headers).status_code, 404)

        # 9. Clear chat history
        del_res = self.client.delete(f"/api/chat/history/{session_id}", headers=candidate)
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.json()["ok"])

        # History should now be empty of messages
        empty_history = self.client.get(f"/api/chat/history/{session_id}", headers=candidate).json()
        self.assertEqual(len(empty_history["messages"]), 0)

    def test_rule_based_career_chatbot_resume_awareness(self):
        # Register fresh candidate without resume
        new_cand_headers = self.register_and_login("Resume Test Cand", "resumetest@example.com", "Password123!", "candidate")

        # Ask about resume when no resume is uploaded
        no_res = self.client.post(
            "/api/chat",
            headers=new_cand_headers,
            json={"message": "What technical skills are in my resume?"},
        )
        self.assertEqual(no_res.status_code, 200)
        self.assertEqual(no_res.json()["intent"], "resume_query_missing")
        self.assertIn("my profile", no_res.json()["message"].lower())

        # Now seed resume with skills and projects
        cand_id = self.client.get("/api/me", headers=new_cand_headers).json()["id"]
        with main.connect() as db:
            db.execute(
                """INSERT INTO resumes (candidate_id, filename, storage_path, content_type,
                                        extracted_text, parsed_profile_json, processing_status, uploaded_at)
                   VALUES (?, 'resume.pdf', 'dummy.pdf', 'application/pdf',
                           'Skills: Python, FastAPI, React, PostgreSQL, Docker\nProjects: Built real-time analytics engine and distributed message queue.',
                           '{"skills": ["Python", "FastAPI", "React", "PostgreSQL", "Docker"]}',
                           'completed', ?)""",
                (cand_id, main.now()),
            )

        # Ask about resume skills
        skills_res = self.client.post(
            "/api/chat",
            headers=new_cand_headers,
            json={"message": "What technical skills are in my resume?"},
        )
        self.assertEqual(skills_res.status_code, 200)
        skills_msg = skills_res.json()["message"]
        self.assertIn("Python", skills_msg)
        self.assertIn("FastAPI", skills_msg)

        # Ask about resume projects
        proj_res = self.client.post(
            "/api/chat",
            headers=new_cand_headers,
            json={"message": "Explain the projects in my resume"},
        )
        self.assertEqual(proj_res.status_code, 200)
        proj_msg = proj_res.json()["message"]
        self.assertIn("analytics", proj_msg.lower())

    def test_turso_wrapper_normalizes_list_parameters(self):
        class MockRawConn:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                if params is not None and not isinstance(params, tuple):
                    raise TypeError("argument 'parameters': 'list' object cannot be converted to 'PyTuple'")
                self.calls.append((sql, params))
                return MockRawCursor()

            def executemany(self, sql, seq):
                for p in seq:
                    if not isinstance(p, tuple):
                        raise TypeError("argument 'parameters': 'list' object cannot be converted to 'PyTuple'")
                self.calls.append((sql, seq))
                return MockRawCursor()

        class MockRawCursor:
            def __init__(self):
                self.description = [("col1",)]

            def fetchall(self):
                return [(1,)]

            def fetchone(self):
                return (1,)

        mock_conn = MockRawConn()
        wrapper = main.TursoConnectionWrapper(mock_conn)

        # Passing list to wrapper.execute should not raise TypeError
        cur = wrapper.execute("SELECT * FROM test WHERE a = ?", [123])
        self.assertEqual(mock_conn.calls[-1][1], (123,))
        rows = cur.fetchall()
        self.assertEqual(len(rows), 1)

        # Passing list of lists to wrapper.executemany
        wrapper.executemany("INSERT INTO test VALUES (?)", [[1], [2]])
        self.assertEqual(mock_conn.calls[-1][1], [(1,), (2,)])

    def test_recruiter_live_assessments_endpoint(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        res = self.client.get("/api/recruiter/live-assessments", headers=recruiter)
        self.assertEqual(res.status_code, 200)
        self.assertIn("items", res.json())

    # =========================================================================
    # TESTS FOR 14 NEW FEATURE SETS (SkillSphere Feature Parity)
    # =========================================================================

    def test_forgot_and_reset_password_flow(self):
        # 1. Forgot password request
        res = self.client.post("/api/auth/forgot-password", json={"email": "candidate@skillsphere.dev"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        token = data.get("dev_token")
        self.assertTrue(token)

        # 2. Reset password with valid token
        res_reset = self.client.post("/api/auth/reset-password", json={"token": token, "new_password": "NewSecretPassword123!"})
        self.assertEqual(res_reset.status_code, 200)

        # 3. Login with newly set password
        res_login = self.client.post("/api/auth/login", json={"email": "candidate@skillsphere.dev", "password": "NewSecretPassword123!"})
        self.assertEqual(res_login.status_code, 200)

        # 4. Expired / invalid token rejects
        res_invalid = self.client.post("/api/auth/reset-password", json={"token": "invalid_fake_token", "new_password": "AnotherPassword123!"})
        self.assertEqual(res_invalid.status_code, 400)

    def test_email_verification_flow(self):
        # 1. Signup creates account in unverified state, blocks login & protected access
        reg = self.client.post(
            "/api/auth/register",
            json={"name": "Verify User", "email": "verify_flow@example.com", "password": "Password123!", "role": "candidate"},
        )
        self.assertEqual(reg.status_code, 201, reg.text)
        reg_data = reg.json()
        self.assertTrue(reg_data["verification_required"])
        self.assertIsNone(reg_data["access_token"])
        self.assertFalse(bool(reg_data["user"]["email_verified"]))
        token = reg_data["dev_token"]
        otp = reg_data["dev_otp"]

        # Unverified user cannot log in
        blocked_login = self.client.post(
            "/api/auth/login",
            json={"email": "verify_flow@example.com", "password": "Password123!"},
        )
        self.assertEqual(blocked_login.status_code, 403)

        # Unverified user cannot access protected endpoints even with a forged/issued JWT
        with main.connect() as db:
            user_row = db.execute("SELECT * FROM users WHERE email = 'verify_flow@example.com'").fetchone()
        raw_jwt = main.encode_token(user_row)
        blocked_me = self.client.get("/api/me", headers={"Authorization": f"Bearer {raw_jwt}"})
        self.assertEqual(blocked_me.status_code, 403)

        # 2. Invalid token/OTP rejected with clear message
        bad_token = self.client.post("/api/auth/verify-email", json={"token": "not-a-real-token-value"})
        self.assertEqual(bad_token.status_code, 400)
        bad_otp = self.client.post(
            "/api/auth/verify-email",
            json={"email": "verify_flow@example.com", "otp": "000000"},
        )
        self.assertEqual(bad_otp.status_code, 400)

        # 3. Expired token rejected
        with main.connect() as db:
            db.execute(
                "UPDATE email_verification_tokens SET expires_at = '2020-01-01T00:00:00+00:00' WHERE user_id = ?",
                (user_row["id"],),
            )
        expired_res = self.client.post("/api/auth/verify-email", json={"token": token})
        self.assertEqual(expired_res.status_code, 400)
        self.assertIn("expired", expired_res.json()["detail"].lower())

        # 4. Change email before verification invalidates old token and sends to new email
        wrong_pw = self.client.post(
            "/api/auth/change-verification-email",
            json={"current_email": "verify_flow@example.com", "password": "WrongPassword!", "new_email": "verify_new@example.com"},
        )
        self.assertEqual(wrong_pw.status_code, 401)
        changed = self.client.post(
            "/api/auth/change-verification-email",
            json={"current_email": "verify_flow@example.com", "password": "Password123!", "new_email": "verify_new@example.com"},
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        new_otp = changed.json()["dev_otp"]
        new_token = changed.json()["dev_token"]

        # Old token/OTP no longer works
        old_after_change = self.client.post("/api/auth/verify-email", json={"token": token})
        self.assertEqual(old_after_change.status_code, 400)

        # 5. Resend verification works, logs email_delivery_logs without DB lock, and enforces rate limit after 3 challenges in 10m
        r1 = self.client.post("/api/auth/resend-verification", json={"email": "verify_new@example.com"})
        self.assertEqual(r1.status_code, 200, r1.text)
        with main.connect() as db:
            resend_logs = db.execute(
                "SELECT * FROM email_delivery_logs WHERE recipient_email = ? AND category = 'verification' ORDER BY id DESC",
                ("verify_new@example.com",),
            ).fetchall()
        self.assertGreaterEqual(len(resend_logs), 2)
        self.assertIn(r1.json()["dev_otp"], resend_logs[0]["body_text"])
        r2 = self.client.post("/api/auth/resend-verification", json={"email": "verify_new@example.com"})
        self.assertEqual(r2.status_code, 429, r2.text)

        # Previous token/OTP before resend is invalidated; latest OTP verifies account
        stale_verify = self.client.post("/api/auth/verify-email", json={"token": new_token})
        self.assertEqual(stale_verify.status_code, 400)
        latest_otp = r1.json()["dev_otp"]
        ok_verify = self.client.post(
            "/api/auth/verify-email",
            json={"email": "verify_new@example.com", "otp": latest_otp},
        )
        self.assertEqual(ok_verify.status_code, 200, ok_verify.text)

        # 6. Reused OTP/token is rejected
        reused = self.client.post(
            "/api/auth/verify-email",
            json={"email": "verify_new@example.com", "otp": latest_otp},
        )
        self.assertEqual(reused.status_code, 400)

        # 7. Verified user can now log in and check verification status
        headers = self.login("verify_new@example.com", "Password123!")
        status_res = self.client.get("/api/auth/verification-status", headers=headers)
        self.assertEqual(status_res.status_code, 200)
        self.assertTrue(status_res.json()["verified"])

    def test_email_notifications_preferences_retries_and_safeguards(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        candidate = self.login("candidate@skillsphere.dev")
        admin = self.login("admin@skillsphere.dev")

        # 1. Major in-app notifications also trigger email delivery logs
        sent_emails = []
        original_send = main.send_email_message

        def spy_send(recipient, subject, body_text):
            sent_emails.append((recipient, subject, body_text))
            return "Sent"

        with patch.object(main, "send_email_message", side_effect=spy_send):
            # Job posted -> account_activity notification
            job_res = self.client.post(
                "/api/jobs",
                headers=recruiter,
                json={
                    "title": "Email Notify Engineer",
                    "company": "SkillSphere",
                    "description": "Build reliable notification systems.",
                    "location": "Remote",
                    "experience": "2 years",
                    "job_type": "Full-time",
                    "salary": "$120k",
                    "skills": ["Python"],
                },
            )
            self.assertEqual(job_res.status_code, 201)
            job_id = job_res.json()["id"]

            # Ensure candidate has a resume so /apply succeeds
            with main.connect() as db:
                cand_row = db.execute("SELECT id FROM users WHERE email = 'candidate@skillsphere.dev'").fetchone()
                db.execute(
                    """INSERT INTO resumes (candidate_id, filename, storage_path, content_type, extracted_text, parsed_profile_json, processing_status, uploaded_at)
                    VALUES (?, 'cv.pdf', 'cv.pdf', 'application/pdf', 'Python developer', ?, 'completed', ?)
                    ON CONFLICT(candidate_id) DO UPDATE SET parsed_profile_json = excluded.parsed_profile_json""",
                    (cand_row["id"], json.dumps({"skills": ["Python"]}), main.now()),
                )

            # Candidate applies -> application notifications to candidate & recruiter
            apply_res = self.client.post(f"/api/jobs/{job_id}/apply", headers=candidate)
            self.assertEqual(apply_res.status_code, 201)
            app_id = apply_res.json()["id"]

            # Recruiter updates status -> application_status notification
            status_res = self.client.patch(
                f"/api/applications/{app_id}/status",
                headers=recruiter,
                json={"status": "Shortlisted"},
            )
            self.assertEqual(status_res.status_code, 200)

            # Recruiter schedules interview -> interview_scheduled notification
            int_res = self.client.post(
                f"/api/applications/{app_id}/interviews",
                headers=recruiter,
                json=self.interview_payload(),
            )
            self.assertEqual(int_res.status_code, 201)

            # Recruiter sends direct message -> new_message notification
            cand_id = self.client.get("/api/me", headers=candidate).json()["id"]
            msg_res = self.client.post(
                "/api/messages",
                headers=recruiter,
                json={"recipient_id": cand_id, "application_id": app_id, "job_id": job_id, "content": "Hello!"},
            )
            self.assertEqual(msg_res.status_code, 200)

        subjects = [s for _, s, _ in sent_emails]
        self.assertTrue(any("New job posted" in s for s in subjects), subjects)
        self.assertTrue(any("Application submitted" in s or "New candidate application" in s for s in subjects), subjects)
        self.assertTrue(any("shortlisted" in s.lower() for s in subjects), subjects)
        self.assertTrue(any("Interview" in s for s in subjects), subjects)
        self.assertTrue(any("New message" in s for s in subjects), subjects)

        # 2. Preferences: disabling non-essential messages suppresses message email, but keeps essential application email enabled
        pref_put = self.client.put(
            "/api/notifications/preferences",
            headers=candidate,
            json={"messages_enabled": False, "account_activity_enabled": False, "system_alerts_enabled": False},
        )
        self.assertEqual(pref_put.status_code, 200)
        self.assertFalse(pref_put.json()["messages_enabled"])
        self.assertIn("application", pref_put.json()["essential_categories"])

        sent_emails.clear()
        with patch.object(main, "send_email_message", side_effect=spy_send):
            # Non-essential message notification -> skipped by preference
            self.client.post(
                "/api/messages",
                headers=recruiter,
                json={"recipient_id": cand_id, "application_id": app_id, "job_id": job_id, "content": "Second msg"},
            )
            # Essential application status update -> still sent!
            status2 = self.client.patch(
                f"/api/applications/{app_id}/status",
                headers=recruiter,
                json={"status": "Accepted"},
            )
            self.assertEqual(status2.status_code, 200, status2.text)
        pref_subjects = [s for _, s, _ in sent_emails]
        self.assertFalse(any("New message" in s for s in pref_subjects), pref_subjects)
        self.assertTrue(any("offer" in s.lower() or "accepted" in s.lower() or "status" in s.lower() for s in pref_subjects), pref_subjects)

        # 3. Safeguard: unverified user never receives notification emails
        with main.connect() as db:
            db.execute(
                "INSERT INTO users (name, email, password_hash, role, email_verified, created_at) VALUES ('Unverified', 'unver_notif@example.com', 'hash', 'candidate', 0, ?)",
                (main.now(),),
            )
            unver_id = db.execute("SELECT id FROM users WHERE email = 'unver_notif@example.com'").fetchone()["id"]
            sent_emails.clear()
            with patch.object(main, "send_email_message", side_effect=spy_send):
                nid = main.notify_user(db, unver_id, "application_status", "Status changed", "Details")
            row = db.execute("SELECT email_delivery_status FROM notifications WHERE id = ?", (nid,)).fetchone()
            self.assertEqual(row["email_delivery_status"], "Skipped_Unverified")
            self.assertEqual(sent_emails, [])

        # 4. Duplicate-event prevention & Failed email delivery + retry behavior
        with main.connect() as db:
            sent_emails.clear()
            with patch.object(main, "send_email_message", side_effect=spy_send):
                n1 = main.notify_user(db, cand_id, "system_alert", "Alert", "Body", event_key="dedup-test-1")
                n2 = main.notify_user(db, cand_id, "system_alert", "Alert", "Body", event_key="dedup-test-1")
            # Re-enable system_alerts first or check essential category
            with patch.object(main, "send_email_message", side_effect=spy_send):
                n3 = main.notify_user(db, cand_id, "application_status", "Essential", "Body", event_key="dedup-ess-1")
                n4 = main.notify_user(db, cand_id, "application_status", "Essential", "Body", event_key="dedup-ess-1")
            r3 = db.execute("SELECT email_delivery_status FROM notifications WHERE id = ?", (n3,)).fetchone()
            r4 = db.execute("SELECT email_delivery_status FROM notifications WHERE id = ?", (n4,)).fetchone()
            self.assertEqual(r3["email_delivery_status"], "Sent")
            self.assertEqual(r4["email_delivery_status"], "Duplicate_Skipped")

            # Simulate failed delivery then retry
            with patch.object(main, "send_email_message", return_value="Failed"):
                n_fail = main.notify_user(db, cand_id, "application_status", "Retry Test", "Body")
            fail_row = db.execute("SELECT email_delivery_status, email_attempts FROM notifications WHERE id = ?", (n_fail,)).fetchone()
            self.assertEqual(fail_row["email_delivery_status"], "Failed")
            self.assertEqual(fail_row["email_attempts"], 1)

        # Candidate cannot call admin retry endpoint
        self.assertEqual(self.client.post("/api/notifications/retry-emails", headers=candidate).status_code, 403)
        with patch.object(main, "send_email_message", side_effect=spy_send):
            retry_res = self.client.post("/api/notifications/retry-emails", headers=admin)
        self.assertEqual(retry_res.status_code, 200, retry_res.text)
        self.assertGreaterEqual(retry_res.json()["succeeded"], 1)
        with main.connect() as db:
            retried_row = db.execute("SELECT email_delivery_status, email_attempts, email_sent_at FROM notifications WHERE id = ?", (n_fail,)).fetchone()
            self.assertEqual(retried_row["email_delivery_status"], "Sent")
            self.assertEqual(retried_row["email_attempts"], 2)
            self.assertIsNotNone(retried_row["email_sent_at"])

        # Candidate can view their own email deliveries
        deliv_res = self.client.get("/api/notifications/deliveries", headers=candidate)
        self.assertEqual(deliv_res.status_code, 200)
        self.assertGreater(len(deliv_res.json()["items"]), 0)

    def test_saved_jobs_and_recommendations(self):
        candidate = self.login("candidate@skillsphere.dev")
        # Get a job ID
        res_jobs = self.client.get("/api/jobs?page=1&page_size=6", headers=candidate)
        jobs = res_jobs.json()["items"]
        self.assertTrue(len(jobs) > 0)
        job_id = jobs[0]["id"]

        # Save job
        res_save = self.client.post(f"/api/jobs/{job_id}/save", headers=candidate)
        self.assertEqual(res_save.status_code, 200)

        # List saved jobs
        res_list = self.client.get("/api/jobs/saved", headers=candidate)
        self.assertEqual(res_list.status_code, 200)
        saved_ids = [j["id"] for j in res_list.json()["items"]]
        self.assertIn(job_id, saved_ids)

        # Remove from saved
        res_delete = self.client.delete(f"/api/jobs/{job_id}/save", headers=candidate)
        self.assertEqual(res_delete.status_code, 200)

        # Verify removed
        res_list_after = self.client.get("/api/jobs/saved", headers=candidate)
        self.assertNotIn(job_id, [j["id"] for j in res_list_after.json()["items"]])

        # Recommended jobs endpoint
        res_rec = self.client.get("/api/jobs/recommended", headers=candidate)
        self.assertEqual(res_rec.status_code, 200)
        self.assertIn("items", res_rec.json())

    def test_resume_analysis_and_versions(self):
        candidate = self.login("candidate@skillsphere.dev")
        # Resume analysis endpoint returns quality score
        res_analysis = self.client.get("/api/resume/analysis", headers=candidate)
        self.assertEqual(res_analysis.status_code, 200)
        data = res_analysis.json()
        self.assertIn("score", data)
        self.assertIn("level", data)

        # Resume versions list endpoint
        res_versions = self.client.get("/api/resume/versions", headers=candidate)
        self.assertEqual(res_versions.status_code, 200)
        self.assertIn("items", res_versions.json())

    def test_recruiter_job_management_and_company_profile(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        # Get one of recruiter's jobs
        res_jobs = self.client.get("/api/jobs?mine=true&page=1&page_size=6", headers=recruiter)
        jobs = res_jobs.json()["items"]
        self.assertTrue(len(jobs) > 0)
        job_id = jobs[0]["id"]

        # Edit job listing
        res_patch = self.client.patch(f"/api/jobs/{job_id}", headers=recruiter, json={
            "title": "Principal Lead Designer",
            "location": "Global Remote",
            "status": "Closed"
        })
        self.assertEqual(res_patch.status_code, 200)
        updated = res_patch.json()
        self.assertEqual(updated["title"], "Principal Lead Designer")
        self.assertEqual(updated["status"], "Closed")

        # Company profile get and update
        res_profile = self.client.get("/api/company/profile", headers=recruiter)
        self.assertEqual(res_profile.status_code, 200)

        res_update_prof = self.client.put("/api/company/profile", headers=recruiter, json={
            "company": "SkillSphere Inc.",
            "description": "Leading career engineering platform",
            "website": "https://skillsphere.dev"
        })
        self.assertEqual(res_update_prof.status_code, 200)
        self.assertEqual(res_update_prof.json()["company"], "SkillSphere Inc.")

    def test_direct_candidate_recruiter_messaging(self):
        candidate = self.login("candidate@skillsphere.dev")
        recruiter = self.login("recruiter@skillsphere.dev")

        # Get candidate ID and recruiter ID from /api/me
        cand_user = self.client.get("/api/me", headers=candidate).json()
        rec_user = self.client.get("/api/me", headers=recruiter).json()

        # Candidate sends message to recruiter
        res_send = self.client.post("/api/messages", headers=candidate, json={
            "recipient_id": rec_user["id"],
            "content": "Hello recruiter! I am interested in your open role."
        })
        self.assertEqual(res_send.status_code, 200)

        # Recruiter gets unread count
        res_unread = self.client.get("/api/messages/unread-count", headers=recruiter)
        self.assertEqual(res_unread.status_code, 200)
        self.assertGreaterEqual(res_unread.json()["unread_count"], 1)

        # Recruiter views thread
        res_thread = self.client.get(f"/api/messages/thread/{cand_user['id']}", headers=recruiter)
        self.assertEqual(res_thread.status_code, 200)
        self.assertTrue(len(res_thread.json()["messages"]) > 0)

        # Recruiter replies to candidate
        res_reply = self.client.post("/api/messages", headers=recruiter, json={
            "recipient_id": cand_user["id"],
            "content": "Thank you for reaching out! We are reviewing your application."
        })
        self.assertEqual(res_reply.status_code, 200)

    def test_question_duplication_and_collections(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        res_jobs = self.client.get("/api/jobs?mine=true&page=1&page_size=6", headers=recruiter)
        job_id = res_jobs.json()["items"][0]["id"]

        # Ensure job has a question
        self.create_job_question_bank(job_id, count=1, headers=recruiter)
        res_q = self.client.get(f"/api/jobs/{job_id}/questions", headers=recruiter)
        questions = res_q.json()["items"]
        q_id = questions[0]["id"]

        # Duplicate question
        res_dup = self.client.post(f"/api/jobs/{job_id}/questions/{q_id}/duplicate", headers=recruiter)
        self.assertEqual(res_dup.status_code, 200)
        self.assertTrue("Copy" in res_dup.json()["question"])

        # Create question collection template
        res_coll = self.client.post("/api/question-collections", headers=recruiter, json={
            "name": "Standard Python Core",
            "description": "Standard Python assessment pack",
            "technology": "Python"
        })
        self.assertEqual(res_coll.status_code, 201)
        coll_id = res_coll.json()["id"]

        # Import collection into another job
        res_import = self.client.post(f"/api/question-collections/{coll_id}/import-to-job/{job_id}", headers=recruiter)
        self.assertEqual(res_import.status_code, 200)

    def test_candidate_technical_practice(self):
        candidate = self.login("candidate@skillsphere.dev")
        # Add practice technology
        res_add = self.client.post("/api/practice/technologies", headers=candidate, json={"technology": "Python"})
        self.assertIn(res_add.status_code, [200, 201])

        # Get practice questions
        res_q = self.client.get("/api/practice/questions?technology=Python&limit=5", headers=candidate)
        self.assertEqual(res_q.status_code, 200)
        questions = res_q.json()["items"]
        self.assertTrue(len(questions) > 0)
        first_q = questions[0]

        # Submit practice answer
        res_sub = self.client.post("/api/practice/submit", headers=candidate, json={
            "question_id": first_q["id"],
            "selected_option_index": 0
        })
        self.assertEqual(res_sub.status_code, 200)
        self.assertIn("is_correct", res_sub.json())

        # Check practice analytics
        res_anal = self.client.get("/api/practice/analytics", headers=candidate)
        self.assertEqual(res_anal.status_code, 200)
        self.assertGreaterEqual(res_anal.json()["total_attempts"], 1)

    def test_assessment_score_adjustment_and_retry(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        # Ensure a job, candidate application and assessment exist
        with main.connect() as db:
            row = db.execute("SELECT id FROM assessments LIMIT 1").fetchone()
            if not row:
                row_job = db.execute("SELECT id, recruiter_id FROM jobs LIMIT 1").fetchone()
                job_id = row_job["id"]
                cand_id = db.execute("SELECT id FROM users WHERE role = 'candidate' LIMIT 1").fetchone()["id"]
                db.execute("""INSERT INTO applications (job_id, candidate_id, status, applied_at)
                    VALUES (?, ?, 'Applied', CURRENT_TIMESTAMP)""", (job_id, cand_id))
                app_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                db.execute("""INSERT INTO assessments 
                    (job_id, candidate_id, application_id, duration_minutes, status, score_percentage, result, match_percentage, created_at)
                    VALUES (?, ?, ?, 45, 'Completed', 65.0, 'Failed', 80.0, CURRENT_TIMESTAMP)""", (job_id, cand_id, app_id))
                assessment_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            else:
                assessment_id = row["id"]

        # Recruiter adjusts score with required audit reason
        res_adj = self.client.post(f"/api/assessments/{assessment_id}/adjust-score", headers=recruiter, json={
            "new_score": 85.0,
            "reason": "Corrected manual verification on question 3"
        })
        self.assertEqual(res_adj.status_code, 200)

        # Recruiter re-invites candidate (retry)
        res_retry = self.client.post(f"/api/assessments/{assessment_id}/retry", headers=recruiter)
        self.assertEqual(res_retry.status_code, 200)

    def test_video_assessment_configuration_and_evaluation(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        res_jobs = self.client.get("/api/jobs?mine=true&page=1&page_size=6", headers=recruiter)
        job_id = res_jobs.json()["items"][0]["id"]

        # Create async video assessment prompt configuration
        res_create = self.client.post("/api/video-assessments", headers=recruiter, json={
            "job_id": job_id,
            "title": "Engineering Video Screening",
            "time_limit_seconds": 120,
            "questions": [
                {"question_text": "Introduce yourself and past achievements", "max_duration_seconds": 120},
                {"question_text": "Describe a challenging bug you fixed", "max_duration_seconds": 120}
            ]
        })
        self.assertEqual(res_create.status_code, 201)
        video_asmnt_id = res_create.json()["id"]

        # Assign to candidate application (ensure application exists first)
        with main.connect() as db:
            app_row = db.execute("SELECT id FROM applications WHERE job_id = ? LIMIT 1", (job_id,)).fetchone()
            if not app_row:
                cand_id = db.execute("SELECT id FROM users WHERE role = 'candidate' LIMIT 1").fetchone()["id"]
                db.execute("""INSERT INTO applications (job_id, candidate_id, status, applied_at)
                    VALUES (?, ?, 'Applied', CURRENT_TIMESTAMP)""", (job_id, cand_id))
                app_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            else:
                app_id = app_row["id"]

        res_assign = self.client.post(f"/api/video-assessments/{video_asmnt_id}/assign?application_id={app_id}", headers=recruiter)
        self.assertEqual(res_assign.status_code, 200)
        token = res_assign.json()["token"]

        # Candidate loads assignment via secure token
        res_load = self.client.get(f"/api/video-assessments/assignment/{token}")
        self.assertEqual(res_load.status_code, 200)

        # Candidate submits video answer
        q_id = res_load.json()["questions"][0]["id"]
        res_sub = self.client.post(f"/api/video-assessments/assignment/{token}/submit", json={
            "responses": [{
                "question_id": q_id,
                "video_url": "https://res.cloudinary.com/demo/video/upload/sample.mp4",
                "duration_seconds": 120
            }]
        })
        self.assertEqual(res_sub.status_code, 200)

    def test_interview_scorecard_and_candidate_dossier(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        # Ensure an interview exists
        with main.connect() as db:
            row = db.execute("SELECT id FROM interviews LIMIT 1").fetchone()
            if not row:
                rec_id = db.execute("SELECT id FROM users WHERE role = 'recruiter' LIMIT 1").fetchone()["id"]
                cand_id = db.execute("SELECT id FROM users WHERE role = 'candidate' LIMIT 1").fetchone()["id"]
                job_id = db.execute("SELECT id FROM jobs WHERE recruiter_id = ? LIMIT 1", (rec_id,)).fetchone()["id"]
                db.execute("""INSERT INTO applications (job_id, candidate_id, status, applied_at)
                    VALUES (?, ?, 'Applied', CURRENT_TIMESTAMP)""", (job_id, cand_id))
                app_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                db.execute("""INSERT INTO interviews (application_id, recruiter_id, scheduled_at, duration_minutes, meeting_platform, meeting_url, status, created_at, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP, 60, 'Google Meet', 'https://meet.google.com/abc-defg-hij', 'Scheduled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""", (app_id, rec_id))
                interview_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            else:
                interview_id = row["id"]

        # Save interview scorecard
        res_sc = self.client.post(f"/api/interviews/{interview_id}/scorecard", headers=recruiter, json={
            "technical_score": 5,
            "communication_score": 4,
            "problem_solving_score": 5,
            "culture_fit_score": 4,
            "recommendation": "Strong Yes",
            "notes": "Demonstrated deep systems engineering principles."
        })
        self.assertEqual(res_sc.status_code, 200)

        # Read scorecard back
        res_get_sc = self.client.get(f"/api/interviews/{interview_id}/scorecard", headers=recruiter)
        self.assertEqual(res_get_sc.status_code, 200)
        self.assertEqual(res_get_sc.json()["scorecard"]["recommendation"], "Strong Yes")

        # Read full candidate dossier
        res_dossier = self.client.get(f"/api/interviews/{interview_id}/dossier", headers=recruiter)
        self.assertEqual(res_dossier.status_code, 200)

    def test_support_tickets_and_escalation(self):
        candidate = self.login("candidate@skillsphere.dev")
        admin = self.login("admin@skillsphere.dev")

        # Candidate creates ticket
        res_ticket = self.client.post("/api/tickets", headers=candidate, json={
            "subject": "Need help with video assessment camera check",
            "category": "Assessment",
            "priority": "High",
            "message": "My camera is displaying blank in the preview."
        })
        self.assertEqual(res_ticket.status_code, 201)
        ticket_id = res_ticket.json()["id"]

        # Candidate replies
        res_msg = self.client.post(f"/api/tickets/{ticket_id}/messages", headers=candidate, json={
            "message": "Update: It works after disabling brave shields."
        })
        self.assertEqual(res_msg.status_code, 200)

        # Admin reads tickets queue
        res_admin_queue = self.client.get("/api/admin/tickets", headers=admin)
        self.assertEqual(res_admin_queue.status_code, 200)
        self.assertTrue(len(res_admin_queue.json()["items"]) > 0)

        # Admin updates ticket status to Resolved
        res_update = self.client.patch(f"/api/admin/tickets/{ticket_id}", headers=admin, json={
            "status": "Resolved"
        })
        self.assertEqual(res_update.status_code, 200)
        self.assertEqual(res_update.json()["status"], "Resolved")

    def test_admin_availability_and_audit_logs(self):
        admin = self.login("admin@skillsphere.dev")
        # Toggle presence
        res_avail = self.client.post("/api/admin/availability?online=true", headers=admin)
        self.assertEqual(res_avail.status_code, 200)
        self.assertTrue(res_avail.json()["is_online"])

        # Check availability
        res_get = self.client.get("/api/admin/availability", headers=admin)
        self.assertEqual(res_get.status_code, 200)

        # View audit logs
        res_audit = self.client.get("/api/admin/audit-logs", headers=admin)
        self.assertEqual(res_audit.status_code, 200)
        self.assertIn("items", res_audit.json())

    def test_job_moderation_by_admin(self):
        admin = self.login("admin@skillsphere.dev")
        res_jobs = self.client.get("/api/jobs?page=1&page_size=6", headers=admin)
        job_id = res_jobs.json()["items"][0]["id"]

        # Moderate job (flag)
        res_flag = self.client.post(f"/api/admin/jobs/{job_id}/moderate", headers=admin, json={
            "action": "flag",
            "note": "Verification required for salary transparency"
        })
        self.assertEqual(res_flag.status_code, 200)
        self.assertTrue(res_flag.json()["flagged"])

        # Unflag job
        res_unflag = self.client.post(f"/api/admin/jobs/{job_id}/moderate", headers=admin, json={
            "action": "unflag"
        })
        self.assertEqual(res_unflag.status_code, 200)
        self.assertFalse(res_unflag.json()["flagged"])

    def test_multilingual_assistant(self):
        candidate = self.login("candidate@skillsphere.dev")
        # Send query in Spanish
        res_es = self.client.post("/api/chat/multilingual", headers=candidate, json={
            "message": "Hola, ¿cómo puedo preparar mi currículum?",
            "preferred_language": "es"
        })
        self.assertEqual(res_es.status_code, 200)
        self.assertIn("reply", res_es.json())

        # Test escalation request trigger
        res_esc = self.client.post("/api/chat/multilingual", headers=candidate, json={
            "message": "I have a problem and want to speak with a human support agent",
            "preferred_language": "en"
        })
        self.assertEqual(res_esc.status_code, 200)
        self.assertTrue(res_esc.json().get("suggest_ticket", False))

    def test_recruiter_can_send_round1_regardless_of_ats_score(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        job_res = self.client.post(
            "/api/jobs",
            headers=recruiter,
            json={
                "title": "Backend Python Developer",
                "company": "SkillSphere Labs",
                "description": "Python FastAPI SQL role.",
                "location": "Remote",
                "experience": "2 years",
                "job_type": "Full-time",
                "salary": "$100k",
                "skills": ["Python", "SQL"],
                "assessment_question_count": 20,
            },
        )
        self.assertEqual(job_res.status_code, 201, job_res.text)
        job_id = job_res.json()["id"]

        questions_payload = [
            {
                "technology": "Python",
                "question": f"Python ATS independence question {idx}?",
                "options": ["Option A", "Option B", "Option C", "Option D"],
                "correct_index": 0,
                "difficulty": "Medium",
            }
            for idx in range(1, 21)
        ]
        batch_res = self.client.post(
            f"/api/jobs/{job_id}/questions/batch",
            headers=recruiter,
            json={"questions": questions_payload},
        )
        self.assertEqual(batch_res.status_code, 201, batch_res.text)

        score_cases = [
            ("ats_high@skillsphere.dev", 85, "Shortlisted"),
            ("ats_low@skillsphere.dev", 25, "Applied"),
            ("ats_zero@skillsphere.dev", 0, "Applied"),
            ("ats_missing@skillsphere.dev", None, "Applied"),
        ]
        app_ids = []
        with main.connect() as db:
            for email, match_pct, app_status in score_cases:
                db.execute(
                    "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, 'candidate', ?)",
                    (email.split("@")[0], email, "hash", main.now()),
                )
                cid = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
                db.execute(
                    """INSERT INTO applications
                    (job_id, candidate_id, status, match_percentage, matched_skills_json, missing_skills_json, applied_at)
                    VALUES (?, ?, ?, ?, '[]', '[]', ?)""",
                    (job_id, cid, app_status, match_pct, main.now()),
                )
                aid = db.execute(
                    "SELECT id FROM applications WHERE job_id = ? AND candidate_id = ?",
                    (job_id, cid),
                ).fetchone()["id"]
                app_ids.append((email, aid))

        # Candidate cannot send assessments
        candidate_headers = self.login("candidate@skillsphere.dev")
        forbidden = self.client.post(
            f"/api/applications/{app_ids[0][1]}/assessment/send",
            headers=candidate_headers,
        )
        self.assertEqual(forbidden.status_code, 403)

        # Recruiter can send and resend for high, low, zero, and missing ATS scores
        for email, aid in app_ids:
            with patch.dict(os.environ, {"SKILLSPHERE_SMTP_HOST": ""}):
                send_res = self.client.post(
                    f"/api/applications/{aid}/assessment/send",
                    headers=recruiter,
                )
            self.assertEqual(send_res.status_code, 200, f"Send failed for {email}: {send_res.text}")
            self.assertEqual(send_res.json()["status"], "Sent")

            with patch.dict(os.environ, {"SKILLSPHERE_SMTP_HOST": ""}):
                reissue_res = self.client.post(
                    f"/api/applications/{aid}/assessment/reissue",
                    headers=recruiter,
                )
            self.assertEqual(reissue_res.status_code, 200, f"Reissue failed for {email}: {reissue_res.text}")
            self.assertEqual(reissue_res.json()["status"], "Sent")

        # Missing/invalid candidate email fails with clear 400 error
        with main.connect() as db:
            db.execute(
                "INSERT INTO users (name, email, password_hash, role, created_at) VALUES ('NoEmail', 'invalid-email', 'hash', 'candidate', ?)",
                (main.now(),),
            )
            bad_cid = db.execute("SELECT id FROM users WHERE email = 'invalid-email'").fetchone()["id"]
            db.execute(
                """INSERT INTO applications
                (job_id, candidate_id, status, match_percentage, matched_skills_json, missing_skills_json, applied_at)
                VALUES (?, ?, 'Applied', 15, '[]', '[]', ?)""",
                (job_id, bad_cid, main.now()),
            )
            bad_aid = db.execute(
                "SELECT id FROM applications WHERE job_id = ? AND candidate_id = ?",
                (job_id, bad_cid),
            ).fetchone()["id"]
        bad_res = self.client.post(f"/api/applications/{bad_aid}/assessment/send", headers=recruiter)
        self.assertEqual(bad_res.status_code, 400)
        self.assertIn("valid registered email", bad_res.json()["detail"])

    def test_multi_topic_8_7_5_assessment_and_below_20_rejection(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        candidate = self.login("candidate@skillsphere.dev")

        # Job with 8 + 6 = 14 questions (< 20) must be rejected with clear count message
        job_small = self.client.post(
            "/api/jobs",
            headers=recruiter,
            json={
                "title": "Insufficient Pool Role",
                "company": "SkillSphere",
                "description": "Role with only 14 questions.",
                "location": "Remote",
                "experience": "2 years",
                "job_type": "Full-time",
                "salary": "$100k",
                "skills": ["TopicA", "TopicB"],
                "assessment_question_count": 20,
            },
        ).json()
        self.create_job_question_bank(job_small["id"], "TopicA", 8)
        self.create_job_question_bank(job_small["id"], "TopicB", 6)

        with main.connect() as db:
            cand_row = db.execute("SELECT id FROM users WHERE email = 'candidate@skillsphere.dev'").fetchone()
            db.execute(
                """INSERT INTO resumes (candidate_id, filename, storage_path, content_type, extracted_text, parsed_profile_json, processing_status, uploaded_at)
                VALUES (?, 'cv.pdf', 'cv.pdf', 'application/pdf', 'TopicA TopicB', ?, 'completed', ?)
                ON CONFLICT(candidate_id) DO UPDATE SET parsed_profile_json = excluded.parsed_profile_json""",
                (cand_row["id"], json.dumps({"skills": ["TopicA", "TopicB"]}), main.now()),
            )
        app_small = self.client.post(f"/api/jobs/{job_small['id']}/apply", headers=candidate).json()
        reject_res = self.client.post(f"/api/applications/{app_small['id']}/assessment/send", headers=recruiter)
        self.assertEqual(reject_res.status_code, 409)
        self.assertIn("Currently available: 14", reject_res.json()["detail"])

        # Job with 8 + 7 + 5 = 20 questions across 3 topics must succeed and sample from all 3
        job_ok = self.client.post(
            "/api/jobs",
            headers=recruiter,
            json={
                "title": "Multi Topic Role",
                "company": "SkillSphere",
                "description": "Role with 8 + 7 + 5 = 20 questions.",
                "location": "Remote",
                "experience": "2 years",
                "job_type": "Full-time",
                "salary": "$110k",
                "skills": ["TopicA", "TopicB", "TopicC"],
                "assessment_question_count": 20,
            },
        ).json()
        self.create_job_question_bank(job_ok["id"], "TopicA", 8)
        self.create_job_question_bank(job_ok["id"], "TopicB", 7)
        self.create_job_question_bank(job_ok["id"], "TopicC", 5)

        app_ok = self.client.post(f"/api/jobs/{job_ok['id']}/apply", headers=candidate).json()
        with patch.dict(os.environ, {"SKILLSPHERE_SMTP_HOST": ""}):
            send_res = self.client.post(f"/api/applications/{app_ok['id']}/assessment/send", headers=recruiter)
        self.assertEqual(send_res.status_code, 200, send_res.text)
        data = send_res.json()
        self.assertEqual(data["status"], "Sent")
        dist = data["topic_distribution"]
        self.assertEqual(dist, {"TopicA": 8, "TopicB": 7, "TopicC": 5})
        self.assertEqual(len(set(data["selected_question_ids"])), 20)

    def test_ai_generate_20_questions_dedup_malformed_and_retries(self):
        recruiter = self.login("recruiter@skillsphere.dev")
        job = self.client.post(
            "/api/jobs",
            headers=recruiter,
            json={
                "title": "FastAPI Backend Role",
                "company": "SkillSphere",
                "description": "Build FastAPI services with full test coverage.",
                "location": "Remote",
                "experience": "2 years",
                "job_type": "Full-time",
                "salary": "$120k",
                "skills": ["FastAPI"],
            },
        ).json()

        # 1. Local/fallback generation for custom technology ("FastAPI") must produce 20 unique valid questions
        gen_20 = self.client.post(
            f"/api/jobs/{job['id']}/questions/ai-generate",
            headers=recruiter,
            json={"technology": "FastAPI", "count": 20, "difficulty": "Mixed"},
        )
        self.assertEqual(gen_20.status_code, 200, gen_20.text)
        gdata = gen_20.json()
        self.assertEqual(gdata["count"], 20)
        self.assertEqual(len(gdata["questions"]), 20)
        unique_prompts = {q["question"].strip().casefold() for q in gdata["questions"]}
        self.assertEqual(len(unique_prompts), 20)

        save_20 = self.client.post(
            f"/api/jobs/{job['id']}/questions/batch",
            headers=recruiter,
            json={"questions": gdata["questions"]},
        )
        self.assertEqual(save_20.status_code, 201, save_20.text)
        self.assertEqual(save_20.json()["created"], 20)
        self.assertTrue(save_20.json()["summary"]["ready"])

        # 2. Simulate LLM returning fewer items, duplicates, malformed items, and a transient failure
        call_responses = [
            # Attempt 1: 4 valid + 1 duplicate + 1 malformed (only 2 options)
            {
                "questions": [
                    {"technology": "GraphQL", "difficulty": "Easy", "question": "GraphQL valid question 1?", "options": ["A", "B", "C", "D"], "correct_index": 0, "explanation": "ok"},
                    {"technology": "GraphQL", "difficulty": "Easy", "question": "GraphQL valid question 1?", "options": ["A", "B", "C", "D"], "correct_index": 0, "explanation": "dup"},
                    {"technology": "GraphQL", "difficulty": "Easy", "question": "Malformed short", "options": ["Only", "Two"], "correct_index": 0},
                    {"technology": "GraphQL", "difficulty": "Medium", "question": "GraphQL valid question 2?", "options": ["A", "B", "C", "D"], "correct_index": 1, "explanation": "ok"},
                    {"technology": "GraphQL", "difficulty": "Hard", "question": "GraphQL valid question 3?", "options": ["A", "B", "C", "D"], "correct_index": 2, "explanation": "ok"},
                    {"technology": "GraphQL", "difficulty": "Medium", "question": "GraphQL valid question 4?", "options": ["A", "B", "C", "D"], "correct_index": 3, "explanation": "ok"},
                ]
            },
            # Attempt 2: transient error
            RuntimeError("LLM timeout"),
            # Attempt 3: 2 more valid questions, remainder filled by local generator
            {
                "questions": [
                    {"technology": "GraphQL", "difficulty": "Medium", "question": "GraphQL valid question 5?", "options": ["A", "B", "C", "D"], "correct_index": 0, "explanation": "ok"},
                    {"technology": "GraphQL", "difficulty": "Hard", "question": "GraphQL valid question 6?", "options": ["A", "B", "C", "D"], "correct_index": 1, "explanation": "ok"},
                ]
            },
        ]
        with patch.object(main, "call_llm_json", side_effect=call_responses):
            retry_res = self.client.post(
                f"/api/jobs/{job['id']}/questions/ai-generate",
                headers=recruiter,
                json={"technology": "GraphQL", "count": 20, "difficulty": "Mixed"},
            )
        self.assertEqual(retry_res.status_code, 200, retry_res.text)
        rdata = retry_res.json()
        self.assertEqual(rdata["count"], 20)
        self.assertEqual(rdata["stats"]["retry_count"], 2)
        self.assertGreaterEqual(rdata["stats"]["rejected_or_duplicate_count"], 2)
        self.assertEqual(len({q["question"].strip().casefold() for q in rdata["questions"]}), 20)

    def test_built_in_live_video_and_coding_interview_room(self):
        candidate, recruiter, application = self.apply_with_match(["Python"], ["Python"])
        outsider = self.register_and_login("Outsider", "outsider_live@example.com", "Password123!", "candidate")
        other_rec = self.register_and_login("Other Rec", "other_rec_live@example.com", "Password123!", "recruiter")

        # 1. Schedule built-in SkillSphere Live interview with coding problem
        sched_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
        sched_res = self.client.post(
            f"/api/applications/{application['id']}/interviews",
            headers=recruiter,
            json={
                "scheduled_at": sched_time,
                "timezone": "UTC",
                "duration_minutes": 45,
                "meeting_platform": "SkillSphere Live",
                "problem_title": "Sum of Even Numbers",
                "problem_prompt": "Write a function that prints the sum of even numbers from 1 to 10.",
                "problem_starter_code": "print(sum(i for i in range(1, 11) if i % 2 == 0))",
                "early_entry_minutes": 15,
            },
        )
        self.assertEqual(sched_res.status_code, 201, sched_res.text)
        interview = sched_res.json()
        self.assertEqual(interview["meeting_platform"], "SkillSphere Live")
        self.assertTrue(interview["room_token"])
        self.assertIn(f"interview_room={interview['room_token']}", interview["meeting_url"])
        self.assertTrue(interview["ice_servers"])
        self.assertTrue(interview["can_enter_room"])

        # 2. Room authorization: authorized candidate & recruiter allowed; outsiders denied
        cand_room = self.client.get(f"/api/interviews/room/{interview['room_token']}", headers=candidate)
        self.assertEqual(cand_room.status_code, 200)
        rec_room = self.client.get(f"/api/interviews/room/{interview['room_token']}", headers=recruiter)
        self.assertEqual(rec_room.status_code, 200)
        self.assertEqual(self.client.get(f"/api/interviews/room/{interview['room_token']}", headers=outsider).status_code, 404)
        self.assertEqual(self.client.get(f"/api/interviews/room/{interview['room_token']}", headers=other_rec).status_code, 404)

        # 3. Read-only interviewer enforcement: recruiter cannot edit/run/submit candidate code
        self.assertEqual(
            self.client.put(
                f"/api/interviews/{interview['id']}/workspace",
                headers=recruiter,
                json={"code": "print('hijack')", "language": "python"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/api/interviews/{interview['id']}/code/run",
                headers=recruiter,
                json={"code": "print(1)", "language": "python"},
            ).status_code,
            403,
        )

        # 4. Sandboxed server-side execution: Python, SQL, blocked network import, and timeout
        py_run = self.client.post(
            f"/api/interviews/{interview['id']}/code/run",
            headers=candidate,
            json={"code": "print(sum(i for i in range(1, 11) if i % 2 == 0))", "language": "python"},
        )
        self.assertEqual(py_run.status_code, 200, py_run.text)
        self.assertEqual(py_run.json()["exit_code"], 0)
        self.assertEqual(py_run.json()["stdout"].strip(), "30")

        sql_run = self.client.post(
            f"/api/interviews/{interview['id']}/code/run",
            headers=candidate,
            json={"code": "SELECT COUNT(*) AS total FROM employees WHERE department = 'Engineering';", "language": "sql"},
        )
        self.assertEqual(sql_run.status_code, 200, sql_run.text)
        self.assertEqual(sql_run.json()["exit_code"], 0)
        self.assertIn("2", sql_run.json()["stdout"])

        blocked_net = self.client.post(
            f"/api/interviews/{interview['id']}/code/run",
            headers=candidate,
            json={"code": "import socket\nprint('net')", "language": "python"},
        )
        self.assertEqual(blocked_net.status_code, 200)
        self.assertNotEqual(blocked_net.json()["exit_code"], 0)
        self.assertIn("disabled in sandbox", blocked_net.json()["stderr"])

        # Timeout test (using execute_interview_code directly with a tight sleep or loop)
        with patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired(cmd="python", timeout=5)):
            timeout_res = self.client.post(
                f"/api/interviews/{interview['id']}/code/run",
                headers=candidate,
                json={"code": "while True: pass", "language": "python"},
            )
        self.assertEqual(timeout_res.status_code, 200)
        self.assertTrue(timeout_res.json()["timed_out"])
        self.assertEqual(timeout_res.json()["exit_code"], 124)

        # 5. WebSocket real-time WebRTC signaling & live code sync
        cand_tok = candidate["Authorization"].split(" ")[1]
        rec_tok = recruiter["Authorization"].split(" ")[1]
        with self.client.websocket_connect(f"/ws/interviews/{interview['id']}?token={rec_tok}") as ws_rec:
            rec_sync = ws_rec.receive_json()
            self.assertEqual(rec_sync["type"], "sync")
        with self.client.websocket_connect(f"/ws/interviews/{interview['id']}?token={cand_tok}") as ws_cand:
            cand_sync = ws_cand.receive_json()
            self.assertEqual(cand_sync["type"], "sync")
            ws_cand.send_json({"type": "code_change", "code": "print(42)", "language": "python"})
            ws_cand.send_json({"type": "webrtc_offer", "sdp": {"type": "offer", "sdp": "v=0..."}})
            ws_cand.send_json({"type": "typing"})

        # 6. Final submission persists and recovers on refresh
        sub_res = self.client.post(
            f"/api/interviews/{interview['id']}/code/submit",
            headers=candidate,
            json={"code": "print(42)", "language": "python"},
        )
        self.assertEqual(sub_res.status_code, 200, sub_res.text)
        self.assertTrue(sub_res.json()["ok"])
        refreshed = self.client.get(f"/api/interviews/{interview['id']}", headers=recruiter).json()
        self.assertTrue(refreshed["code_submitted"])
        self.assertEqual(refreshed["submitted_code"], "print(42)")


if __name__ == "__main__":
    unittest.main()

