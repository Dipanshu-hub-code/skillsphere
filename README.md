# SkillSphere

SkillSphere is a responsive career and job portal inspired by the supplied screen recording. Its warm, red-accented interface supports job seekers, recruiters, and administrators. The frontend is a dependency-free HTML/CSS/JavaScript client, so it runs without a JavaScript build toolchain; the Python API and SQLite database provide the working application flows.

## Run locally

1. Use Python 3.10 or later.
2. Install the backend requirements:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. For image OCR, install the Tesseract OCR executable and make it available on `PATH`. Text-based PDFs are read directly; scanned PDFs and images are rendered and sent through Tesseract.
4. Start the app from this directory:

   ```powershell
   python -m uvicorn backend.main:app --reload
   ```

5. Open <http://127.0.0.1:8000>.

The API is documented at <http://127.0.0.1:8000/docs>. SQLite data and private resume uploads are stored in `data/`. Set `SKILLSPHERE_DATA_DIR` to move them. Seeded demo accounts and a local-only signing key are enabled in development mode; set `SKILLSPHERE_ENV=production`, a long, unique `SKILLSPHERE_SECRET_KEY`, `SKILLSPHERE_ADMIN_EMAIL`, and `SKILLSPHERE_ADMIN_PASSWORD` before deploying.

### Interview scheduling and joining

From **Applicants**, a recruiter can schedule an interview only for an applicant to one of their own jobs. Enter a future date and start time, an IANA time zone (for example `America/New_York`), duration, meeting platform, HTTPS meeting URL, and optional instructions. Google Meet, Microsoft Teams, Zoom, and other HTTPS meeting links are supported; SkillSphere does not create calendar events or connect to those platforms.

The invitation is stored against the application, moves the application into **Interview**, and sends the candidate an in-app notification and email with the meeting details. In **My Applications**, the candidate sees the schedule in their browser's local time zone and can open the saved link with **Join Interview**. Recruiters can reschedule, replace the meeting link, cancel, or mark an interview completed; each change sends the candidate an updated notification. Recruiters and administrators can see whether the candidate opened the invitation.

Manual verification flow: sign in as a recruiter → open **Applicants** and choose **Schedule Interview** for one of your candidates → enter a future date/time, time zone, duration, platform, HTTPS meeting URL, and optional instructions → verify the candidate receives an email and in-app notification → sign in as that candidate and open **My Applications** → verify the interview details and **Join Interview** appear → click **Join Interview** and confirm the meeting opens in a new tab. Then reschedule the invitation and verify the details and notification update; cancel it and confirm the join action is no longer available; finally mark another interview completed and verify its completed state.

### Job-specific technical assessments

When a candidate applies, SkillSphere stores the required-skill match percentage and matched/missing skills on the application. Applications scoring 70% or higher are shortlisted. Recruiters build each role's question bank in **Question bank** by adding questions manually or importing a PDF. Each question has a technology, Easy/Medium/Hard difficulty, four distinct options, one answer key, and an active/inactive state. Question ownership is restricted to that job's recruiter and administrators.

#### Importing a question-bank PDF

Open **Question bank** for a job and upload a text-based PDF (10 MB maximum, 40 pages maximum). The format repeats a technology and optional difficulty before each question, followed by four A–D options and the correct answer letter. For example:

```text
Technology: Python
Difficulty: Easy
Question: What does len(items) return?
A. The number of items
B. The first item
C. The item's type
D. The last index
Answer: A
```

Difficulty defaults to Medium when omitted. After upload, SkillSphere extracts question blocks and displays valid/invalid counts, technology and difficulty counts, parser errors, and editable fields for every question. Nothing is added to the question bank until a recruiter selects questions and confirms the preview. Invalid blocks can be repaired in the preview or left unselected. A pending preview can be resumed from **Import history** or cancelled; cancellation removes its stored PDF. Confirmed imports and their source PDFs remain available in private storage under `data/question_bank_uploads/`, outside the public frontend. Only recruiters managing that job and administrators can access answer keys or import previews; candidate assessment responses contain question snapshots and choices, never correct answers.

Text-based PDFs are recommended. For scanned PDFs, install the Tesseract OCR executable and make it available on `PATH`; OCR quality depends on scan clarity. Check that the PDF is not password-protected/corrupted and that each question uses the labels shown above if no question blocks or fields are recognized. PDF uploads are limited to 10 MB and 40 pages.

#### Assessment selection and delivery

Eligible candidates need 20 active questions matching both their resume/profile skills and the job's required skills. When multiple matching technologies are available, SkillSphere weights the topic mix by matching skill evidence and balances Easy/Medium/Hard questions where the bank allows. The selected topics and selection rationale are stored with the assessment. If there are not 20 relevant questions, no incomplete assessment is created: the recruiter receives an in-app and email notice, while the candidate sees that the assessment is being prepared. Adding questions causes the app to retry eligible shortlisted applications. If no relevant topic can be inferred but the job has a complete topic, the recruiter can explicitly choose a topic in **Applicants**.

Every assessment is **Round 1: Technical MCQ Test** and contains exactly 20 job-specific multiple-choice questions. The default passing score is 75%: 15 or more correct answers pass, while 14 or fewer fail. The backend calculates and stores the result, score, correct/incorrect counts, submission time, and time taken; candidates never receive correct answers before submitting. The secure link expires after 48 hours by default, gives 30 minutes after starting, and permits one submission. An assessment that reaches its timer is submitted and graded using the answers saved at that point.

After submission, the application timeline records **Assessment Completed** followed by **Round 1 Passed** or **Round 1 Failed**. Passing does not automatically move someone to Interview. Recruiters can review answer-by-answer results and choose a next stage; candidates can see their Round 1 result in **Assessments**, **Applications**, and notifications. The recruiter review supports result, score, job, assessment-status, and completion-date filters. The Admin Dashboard reports weekly Round 1 completions, pass/fail counts and rate, average score, job-level results, and pass/fail distribution.

#### Consent-based assessment proctoring & webcam recording

Proctoring and video recording are required by default. Before the assessment timer starts, the candidate must consent to webcam recording and grant camera access.

- **Candidate Consent Screen**:
  - The consent screen clearly states that webcam video will be recorded during the assessment, securely stored for integrity review, accessible strictly by authorized administrators, and may capture face orientation and surroundings.
  - Candidates must select the checkbox `I understand and consent to webcam recording during this assessment.`
  - The **Start assessment →** button remains disabled until consent is checked, camera permission is granted, and the live video stream is active.
- **Browser Video Recording (MediaRecorder API)**:
  - When the assessment starts, the browser captures the candidate's camera stream using the `MediaRecorder` API (WebM format).
  - During the test, a live preview and recording indicator (pulsing red dot + `Camera recording active`) remain visible.
  - Recording stops automatically upon test submission, timer expiration, or proctoring policy auto-submission.
- **Secure Cloudinary Upload with Local Development Fallback**:
  - Recordings are securely uploaded to Cloudinary using signed server-side credentials stored only on the backend.
  - **No secrets on frontend**: The Cloudinary API secret is never exposed to the client.
  - **Non-blocking submission resilience**: When a candidate submits answers, their answers and test score are saved immediately. Video upload finalization occurs smoothly in the background, ensuring test submissions are never lost or delayed.
  - **Local Development Fallback**: If Cloudinary environment variables are unset or during offline development, SkillSphere falls back cleanly to local video storage under `data/recordings/` and serves video through an admin-authenticated streaming endpoint (`/api/admin/recordings/{id}/stream`).
  - **Failure Handling**: If an upload fails, the status is marked as `Failed` in the database, test scores remain preserved, and an in-app notification is dispatched to administrators.
- **Admin Review & Synchronized Playback**:
  - **Access Control**: Only authorized users with the **Admin** role can access recording playback and streaming endpoints. Recruiters and candidates see only safe status badges and summary metadata without playback URLs.
  - **Audit Logging**: Every admin video view is logged with administrator ID, timestamp, IP address, and user agent.
  - **Playback Modal**: Admins can watch the webcam video with standard controls (play, pause, seek, speed) accompanied by a synchronized proctoring timeline. Admins can jump directly to the timestamp of flagged events (e.g., face absent, multiple faces, tab switch, window blur).
- **Configurable Retention Policy**:
  - Recordings expire after a configurable retention window (default: 30 days via `SKILLSPHERE_RECORDING_RETENTION_DAYS`).
  - Admins can run retention cleanup from the integrity panel (`POST /api/admin/recordings/retention-cleanup`) to purge expired recordings from Cloudinary/disk while preserving proctoring logs and assessment scores. Flagged assessments are preserved by default.

Configure Cloudinary and proctoring recording with these environment variables:

```powershell
# Cloudinary Configuration (Optional in local dev; falls back to data/recordings/)
$env:CLOUDINARY_CLOUD_NAME = "your-cloud-name"
$env:CLOUDINARY_API_KEY = "your-api-key"
$env:CLOUDINARY_API_SECRET = "your-api-secret"
$env:CLOUDINARY_UPLOAD_FOLDER = "skillsphere/recordings"

# Proctoring & Recording Settings
$env:SKILLSPHERE_RECORDING_REQUIRED = "true"
$env:SKILLSPHERE_RECORDING_RETENTION_DAYS = "30"
$env:SKILLSPHERE_RECORDING_MAX_UPLOAD_MB = "150"
$env:SKILLSPHERE_PROCTORING_REQUIRED = "true"
$env:SKILLSPHERE_PROCTORING_FACE_MISSING_SECONDS = "10"
$env:SKILLSPHERE_PROCTORING_MAX_WARNINGS = "3"
$env:SKILLSPHERE_PROCTORING_AUTO_SUBMIT = "false"
```

Manual verification flow:
1. Send an assessment as a recruiter → open the candidate link.
2. Confirm the consent screen displays webcam recording guidelines and the consent checkbox.
3. Verify **Start assessment →** remains disabled until the consent checkbox is checked and camera permission is granted.
4. Click **Start assessment →**: verify the live camera preview displays the red pulsing dot and `Camera recording active`.
5. Complete questions and submit: verify the test submission succeeds immediately with score and pass/fail result.
6. Sign in as Admin → open **Assessment integrity & recordings**:
   - Check that the recording status badge displays `📹 Ready`.
   - Click `▶ Play Video`: verify the playback modal opens with HTML5 player and synchronized proctoring events timeline.
   - Click `Jump to ⏱` on a timeline event to verify video seeks to the event offset.
7. Verify that signing in as candidate or recruiter blocks direct access to `/api/admin/recordings/{id}/playback` with HTTP 403 Forbidden.

Configure outbound email and the public link origin with these environment variables:

```powershell
$env:SKILLSPHERE_SMTP_HOST = "smtp.example.com"
$env:SKILLSPHERE_SMTP_PORT = "587"
$env:SKILLSPHERE_SMTP_USERNAME = "your-smtp-user"
$env:SKILLSPHERE_SMTP_PASSWORD = "your-smtp-password"
$env:SKILLSPHERE_SMTP_FROM = "assessments@example.com"
$env:SKILLSPHERE_PUBLIC_BASE_URL = "https://your-app.example.com"
$env:SKILLSPHERE_ASSESSMENT_TTL_HOURS = "48"
$env:SKILLSPHERE_ASSESSMENT_DURATION_MINUTES = "30"
$env:SKILLSPHERE_ROUND1_PASS_PERCENTAGE = "75"
```

`SKILLSPHERE_ROUND1_PASS_PERCENTAGE` accepts a number from 0 to 100 and defaults to `75`. The value is recorded on each newly generated or reissued assessment so results use the threshold in effect when that assessment was sent.

Port 587 uses STARTTLS; port 465 uses implicit TLS. In development only, when SMTP is not configured, the backend logs the generated assessment URL and provides it to the recruiter in the development notification/UI. Production requires SMTP and an explicitly configured public base URL; production URLs are not returned to the browser or logged, and token hashes are never exposed.

Manual verification flow: sign in as a recruiter, open **Question bank** for a job, upload a PDF using the format above, edit/approve questions in the preview, and confirm → verify the questions appear in the bank and the import is in history → apply as a candidate whose resume/profile matches the job requirements → confirm a Round 1 link is emailed (or open the development link from recruiter notifications) → answer 15 of 20 correctly and verify the candidate sees 75% / Passed and the pipeline becomes **Round 1 Passed** → verify the recruiter can review the answer details and explicitly move the candidate to Interview → repeat with 14 correct answers and verify 70% / Failed and **Round 1 Failed**. Also verify the Admin Dashboard's Round 1 weekly totals and per-job breakdowns. Upload a PDF with a missing answer, repair that question in the preview, and confirm it is saved only after confirmation.

### Candidate assessment-result review and skill-gap analysis

After submitting an assessment, candidates can review their performance in detail from **Assessments** or **My Applications** via **Performance review →**:
- **Summary Metrics**: Job title, company, assessment round, score percentage, pass/fail result, required passing score, time taken in seconds, and completion timestamp.
- **Strengths and Areas to Improve**: Automated diagnostic categorizing tested technologies into Strengths (score ≥ 70%) and Areas for Growth (score < 70%).
- **Topic-Level Breakdown**: Visual progress bars and exact scores for every evaluated skill/technology (e.g. SQL 8/10 80%, Python 4/7 57%).
- **Difficulty Breakdown**: Score percentages across Easy, Medium, and Hard questions.
- **Actionable Recommendations**: Topic-tailored study recommendations highlighting key concepts to practice (e.g. SQL joins and window functions, pandas data cleaning, async JavaScript).
- **Question-by-Question Review**: Questions answered incorrectly, candidate's selected choice, and (if permitted) the correct answer, explanation, and learning resource links.
- **Candidate Privacy & Recruiter Control**: A recruiter setting (`Allow detailed candidate results`, default ON) controls whether candidates can see exact answer keys and explanations after testing. When disabled, candidates see scores, topic breakdowns, and study recommendations, but exact correct answers remain concealed. Recruiters and administrators can always view full answer keys and question reviews for authorized jobs.

### Live candidate video monitoring for recruiters

SkillSphere allows recruiters and administrators to view a candidate's live camera feed in real time while the candidate is taking an assessment:
- **Candidate Consent & Privacy**:
  - The proctoring consent screen explicitly informs the candidate that authorized recruiters may view their live webcam video in real time.
  - Transparent candidate indicators: A persistent badge indicates whether live monitoring is active (`🟢 Live monitoring active`) and when an authorized recruiter is actively observing the video (`🔴 Recruiter viewing live video`).
  - No camera frames or video streams are routed through or stored in the database. Video is streamed purely peer-to-peer using WebRTC.
- **Recruiter Live Assessment Monitoring**:
  - A dedicated section on the **Recruiter Dashboard** and a **Live monitoring** navigation tab display all active assessments for the recruiter's jobs.
  - Active candidate metadata is surfaced: candidate name, email, job title, assessment round/topic, and start time.
  - Clear real-time statuses: `Assessment In Progress`, `Camera Connected`, `Camera Disconnected`, `Assessment Submitted`, and `Monitoring Unavailable`.
  - Recruiter can click **▶ View Live Video** on any connected candidate to open a secure WebRTC video viewing panel.
  - Recruiters can stop viewing at any time via **Stop Viewing**, gracefully releasing peer connections and informing the candidate.
- **WebRTC & WebSocket Signaling Architecture**:
  - Direct peer-to-peer WebRTC video streaming backed by public STUN servers.
  - Authenticated WebSocket signaling through FastAPI (`/ws/live-monitoring/candidate/{token}` and `/ws/live-monitoring/recruiter/{assessment_id}`).
  - Strict role and job-ownership authorization: Only recruiters who own the candidate's job (or administrators) can access the live feed; unauthorized requests are rejected immediately with WebSocket code 1008.
- **Graceful Lifecycle Management**:
  - Candidate camera disconnections, browser closures, test submissions, and timer expirations are signaled in real time to the recruiter view without video stutter or server memory leaks.

### Recruiter-controlled assessment pass/fail decisions

- **Confidential Results**: Candidates complete assessments normally with internal score calculation, but results (marks, percentages, pass/fail status, and correct answers) are never displayed to candidates or exposed through candidate-facing APIs. After test submission, candidates see a neutral confirmation: *"Your assessment has been submitted successfully. The recruiter will review your results and update your application status."*
- **Recruiter Evaluation & Manual Decision**: Recruiters view full score percentages, correct/incorrect breakdowns, and question-by-question review in the Assessment Review modal. Recruiters make the final hiring decision manually using explicit **✓ Pass Candidate** or **✕ Fail Candidate** controls. The system does not automatically pass or fail candidates solely based on score percentage.
- **Ownership & Audit**: Only the recruiter who owns the job (or an admin) can record a decision. Decisions are tracked with timestamp, decision-maker ID, and optional recruiter notes.

### Job description minimum character validation

- Job descriptions must contain at least 20 non-whitespace characters before publishing.
- Enforced on both frontend and backend with the exact message: *"Job description must contain at least 20 characters before you can post this job."* Entered form inputs are preserved without clearing if validation fails.

### Job seeker assessments section

Job seekers (candidates) have dedicated visibility of their technical assessments across the platform:
- **Candidate Dashboard**: Surfaces assigned assessments directly with a pending assessment action banner (`Take assessment now →`), current evaluation status, and quick links to completed test reviews.
- **My Assessments Page** (`Assessments` navigation tab): A candidate-specific view displaying evaluation overview stats (Total evaluations, Pending tests, Completed, Average score), active test callouts, and an evaluation table with role, company, topic, question format, score, outcome, and direct actions (`Take test →`, `Resume test →`, or `Performance review →`).
- **My Applications**: The application pipeline table displays live assessment status, scores, direct test launch links for active evaluations, and performance review shortcuts for completed tests.

### Recruiter-configurable assessment question counts

When creating or editing a job, recruiters can specify the number of questions for technical assessments:
- **Configurable Range**: Between 5 and 50 questions (default remains 20).
- **Dynamic Sampling**: The assessment generation engine samples exactly the configured number of questions, maintaining topic balance across candidate matched skills.
- **Question Bank Readiness Checks**: If a job bank does not yet have enough active questions to satisfy the configured count (e.g., set to 30 but only 25 questions exist), assessments cannot be sent. The system alerts the recruiter with the exact count of additional questions needed and creates in-app/email readiness notifications.
- **Settings API**: Recruiters and admins can update assessment configurations anytime via `PATCH /api/jobs/{job_id}/assessment-settings` (`assessment_question_count` and `allow_detailed_results`).

### Accessible password visibility toggle

An accessible password toggle (👁 / 🙈) is integrated into all password inputs across the application (Sign in, Job seeker registration, Recruiter registration). Candidates and recruiters can toggle password visibility on demand without clearing input fields or submitting forms prematurely.

### Comprehensive role-based analytics dashboards

- **Candidate Analytics** (`GET /api/candidate/analytics`): Personal overview featuring active applications, scheduled interviews, completed tests, average test scores, and demonstrated skill strengths vs. areas for improvement.
- **Recruiter Hiring Analytics** (`GET /api/recruiter/analytics`): Filterable by range (`this_week`, `last_week`, `this_month`, `last_month`, `all`, `custom`) featuring platform conversion funnels (Applied → Under Review → Shortlisted → Assessment Completed → Round 1 Passed → Interview → Accepted), applicant counts, top skills in the applicant pool, and question bank readiness status.
- **Admin Platform Analytics** (`GET /api/admin/analytics`): Updated with month filters (`this_month`, `last_month`), cross-platform candidate conversion funnels, and identification of the most challenging technical topics based on test results.

### Demo accounts

| Workspace | Email | Password |
|---|---|---|
| Job seeker | candidate@skillsphere.dev | Demo123! |
| Recruiter | recruiter@skillsphere.dev | Demo123! |
| Admin | admin@skillsphere.dev | Demo123! |

## What works

- Candidate and recruiter registration, sign-in, and role-protected API access.
- Job search by keyword, location, and job type; recruiters can post roles and see their own listings.
- Resume uploads accept PDF, JPG, JPEG, and PNG, enforce a 10 MB limit, validate file signatures, store files outside the public web root, extract text, and retain editable parsed profile fields.
- Resume-to-job comparisons use explicit skill-name matches and missing-skill lists; the score is the matched required skills divided by all required skills.
- Candidates can apply once to a role after uploading a resume, withdraw eligible applications, and review their application history.
- Recruiters can review candidates for their own roles, access submitted resumes, review matched/missing skills, update application status, keep private notes, and see an application status timeline. Candidates see the same timeline without recruiter-only notes.
- Candidates and recruiters receive in-app notifications and email results after Round 1 processing; passed candidates are told the recruiter will review their profile, and recruiters are given the candidate, job, score, and result. Other application, assessment, and interview notifications remain enabled.
- Candidate applications persist their match percentage and matched/missing skills; 70% or higher is shortlisted. Assessment questions are job-specific, links are securely tokenized, and eligible candidates are automatically sent a link when the matching bank is ready.
- Recruiters can import multi-technology question-bank PDFs through an editable preview-before-confirm workflow; pending imports are resumable/cancellable, PDFs are private, and candidate APIs do not expose correct answers.
- Recruiters can manage question banks, track Round 1 results, filter the Assessment Review dashboard, move passed candidates to Interview, Accepted, or Rejected, and review submitted answers. Administrators can manage banks across jobs and view weekly or custom-range reports for applications, pipeline status, assessments, completion rates, scores, Round 1 pass/fail results, top jobs, and technologies.
- Recruiters can schedule, reschedule, cancel, and complete application-linked interviews with secure HTTPS meeting links, email and in-app notifications, candidate invitation-view tracking, and candidate-only joining from **My Applications**. The existing shared interview coding workspace remains available; the candidate and recruiter can both edit, with updates stored by the app and shown to the other participant within about 1.5 seconds.
- Administrators can view platform totals and recent activity, disable accounts, and close job listings.

OCR results are best-effort: files remain available for review if extraction is unsuccessful, and the UI presents the reason instead of inventing missing resume details. The current build stores application data in SQLite; production deployments should add operational controls such as rate limiting, email delivery, database backups, and a deployment-grade secret manager.

The interview coding workspace stores and shares source text only; it does not execute submitted code on the application server. The assessment proctoring camera is processed locally in the candidate's browser; microphone access is not requested.

## Tests

Run the API test suite with:

```powershell
python -m unittest discover -s backend\tests -v
```

Assessment link expiry and test duration default to 48 hours and 30 minutes; the Round 1 pass percentage defaults to 75. Proctoring is required by default, flags at 3 warnings, and does not auto-submit. Existing SQLite databases are upgraded on startup with guarded migrations for assessment, proctoring, and interview fields. Previously completed scored assessments are backfilled as Round 1 and their application pipeline is advanced to the matching result stage without duplicate history entries. The supported pipeline includes Assessment Sent, Assessment Started, Assessment Completed, Round 1 Passed, Round 1 Failed, Interview, Accepted, and Rejected. Legacy global technical questions are retained only for SQLite foreign-key compatibility; assessments use job-specific question banks and do not fall back to the legacy bank.
