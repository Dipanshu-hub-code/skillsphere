const app = document.querySelector("#app");
const API = "/api";
const demoPassword = "Demo123!";

const state = {
  token: localStorage.getItem("skillsphere_token") || "",
  user: null,
  assessmentMode: false,
  publicAssessment: null,
  page: "Dashboard",
  jobs: [],
  applications: [],
  assessments: [],
  upcomingInterviews: [],
  assessmentFilters: {
    result: "", min_score: "", max_score: "", job_id: "", status: "",
    completed_from: "", completed_to: ""
  },
  notifications: [],
  unreadNotifications: 0,
  notificationsOpen: false,
  questionBankJob: null,
  questionBankSummary: null,
  questionBankQuestions: [],
  questionBankImports: [],
  questionImportPreview: null,
  assessmentReview: null,
  assessmentProctoringReview: null,
  proctoringItems: [],
  proctoringFilters: {
    candidate: "", job_id: "", start_date: "", end_date: "", flagged_only: ""
  },
  applicationHistory: null,
  interviews: [],
  currentInterview: null,
  editInterviewId: null,
  interviewPoll: null,
  interviewSocket: null,
  editorSaveTimer: null,
  editorDirty: false,
  editorSaveInFlight: false,
  resume: null,
  analytics: null,
  analyticsRange: "this_week",
  analyticsStart: "",
  analyticsEnd: "",
  adminJobs: [],
  match: null,
  query: "",
  location: "",
  jobType: "",
  experience: "",
  skill: "",
  sortBy: "newest",
  jobPage: 1,
  jobTotal: 0,
  modal: "",
  busy: false
};

const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[char]);
const initials = name => String(name || "S").split(/\s+/).slice(0, 2).map(part => part[0] || "").join("").toUpperCase();
const dateLabel = value => value ? new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "Recently";
const dateTimeLabel = value => value ? new Date(value).toLocaleString() : "—";
const icon = (character, label = "") => `<span class="nav-icon" aria-hidden="true">${character}</span>${label ? `<span>${label}</span>` : ""}`;

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API}${path}`, { ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload;
}

function showToast(message, error = false) {
  document.querySelector(".toast")?.remove();
  const toast = document.createElement("div");
  toast.className = `toast${error ? " error" : ""}`;
  toast.setAttribute("role", "status");
  toast.textContent = message;
  document.body.append(toast);
  setTimeout(() => toast.remove(), 3500);
}

function assessmentProctoringPanel(assessment) {
  const proctoring = assessment.data.proctoring;
  const monitor = assessment.proctor;
  if (!proctoring?.required) return "";
  const status = monitor?.status || "Camera is not enabled";
  return `<section class="assessment-proctoring" aria-live="polite">
    <video id="assessment-camera-preview" class="assessment-camera-preview" autoplay muted playsinline ${monitor?.stream ? "" : "hidden"}></video>
    <div><strong>Camera and face-presence check</strong><p>${esc(status)}</p>
    ${monitor?.started ? `<small>Warnings: ${esc(monitor.warningCount)}/${esc(proctoring.max_warnings)}${monitor.flagged ? " · Flagged for recruiter review" : ""}</small>` : ""}</div>
  </section>`;
}

function renderPublicAssessment() {
  const assessment = state.publicAssessment;
  const data = assessment?.data;
  let content = `<div class="assessment-loading"><span class="spinner"></span><p>Loading your assessment…</p></div>`;
  if (assessment?.error) {
    content = `<div class="assessment-message"><span class="eyebrow">Assessment unavailable</span><h1>This link cannot be used</h1><p>${esc(assessment.error)}</p></div>`;
  } else if (assessment?.result) {
    const result = assessment.result;
    content = `<div class="assessment-message"><span class="eyebrow">Assessment completed</span><h1>${esc(result.assessment_name || "Round 1: Technical MCQ Test")}</h1><p>${esc(result.job_title || "")} · ${esc(result.assessment_round || "Round 1")}</p><div class="assessment-score">${esc(result.score_percentage)}<small>%</small></div><p>${esc(result.correct_answer_count)} correct answers out of ${esc(result.total_questions)}</p><p><strong>Result: ${esc(result.result)}</strong></p><p>${esc(result.message)}</p><p>You may close this page now.</p></div>`;
  } else if (data?.status === "Sent") {
    const required = data.proctoring?.required;
    const cameraReady = assessment.proctor?.cameraReady;
    content = `<div class="assessment-welcome"><span class="eyebrow">${esc(data.assessment_round)} · ${esc(data.company)}</span><h1>${esc(data.assessment_name)}</h1><p class="assessment-company">${esc(data.job_title)} · ${esc(data.technology)}</p><div class="assessment-facts"><span><strong>Questions</strong>${esc(data.total_questions)} multiple-choice</span><span><strong>Time limit</strong>${esc(data.duration_minutes)} minutes after starting</span><span><strong>Passing score</strong>${esc(data.pass_percentage)}%</span><span><strong>Link expires</strong>${new Date(data.expires_at).toLocaleString()}</span></div><p>Complete this assessment independently. Once started, the timer cannot be paused and the link can be submitted only once.</p>
      ${required ? `<div class="assessment-consent"><strong>Before you begin</strong><p>Camera access and face-presence checks are required. Video frames are processed locally in your browser and are never uploaded, recorded, or stored. Only monitoring events and warning counts are saved for review.</p><label class="checkbox-field"><input type="checkbox" data-proctor-consent ${assessment.proctor?.consentGiven ? "checked" : ""} /> I consent to the camera-based proctoring described above.</label>${assessment.proctor?.error ? `<p class="assessment-proctor-error" role="alert">${esc(assessment.proctor.error)}</p>` : ""}${assessmentProctoringPanel(assessment)}<button class="btn btn-quiet" data-assessment-action="enable-proctoring" ${assessment.busy ? "disabled" : ""}>${cameraReady ? "Camera enabled" : "Enable camera and continue"}</button></div>` : ""}
      <button class="btn btn-primary" data-assessment-action="start" ${assessment.busy || (required && !cameraReady) ? "disabled" : ""}>Start assessment →</button></div>`;
  } else if (data?.status === "Started") {
    const remaining = Math.max(0, Math.ceil((new Date(data.expires_at).getTime() - Date.now()) / 1000));
    if (data.proctoring?.required && !assessment.proctor?.cameraReady) {
      content = `<div class="assessment-message"><span class="eyebrow">Assessment in progress</span><h1>Reconnect your camera</h1><p>The timer is running. Camera access is required to continue; no video is uploaded or stored.</p>${assessment.proctor?.error ? `<p class="assessment-proctor-error" role="alert">${esc(assessment.proctor.error)}</p>` : ""}${assessmentProctoringPanel(assessment)}<button class="btn btn-primary" data-assessment-action="resume-camera" ${assessment.busy ? "disabled" : ""}>Enable camera and resume</button></div>`;
    } else {
      const currentQuestion = data.questions[assessment.current] || data.questions[0];
      const answered = Object.keys(assessment.answers).length;
      const minutes = Math.floor(remaining / 60);
      const seconds = String(remaining % 60).padStart(2, "0");
      const questionNumber = assessment.current + 1;
      content = `${assessmentProctoringPanel(assessment)}<div class="assessment-test-head"><div><span class="eyebrow">${esc(data.assessment_name)} · ${esc(data.technology)} · ${esc(data.total_questions)} questions · Pass ${esc(data.pass_percentage)}%</span><h1>${esc(data.job_title)}</h1></div><div class="assessment-timer" role="timer" aria-live="polite">${minutes}:${seconds}</div></div><div class="assessment-progress"><span style="width:${questionNumber / data.total_questions * 100}%"></span></div><p class="assessment-progress-label" id="assessment-answer-count">Question ${questionNumber} of ${esc(data.total_questions)} · ${answered} answered</p>${currentQuestion ? `<section class="assessment-question"><h2>${esc(currentQuestion.question)}</h2><fieldset><legend class="sr-only">Choose one answer</legend>${currentQuestion.options.map((option, index) => `<label class="assessment-option${assessment.answers[currentQuestion.id] === index ? " selected" : ""}"><input type="radio" name="${esc(currentQuestion.id)}" value="${index}" data-assessment-answer ${assessment.answers[currentQuestion.id] === index ? "checked" : ""} /><span>${esc(option)}</span></label>`).join("")}</fieldset></section>` : `<p>No questions are available. Contact the hiring team.</p>`}<div class="assessment-controls"><button class="btn btn-quiet" data-assessment-action="previous" ${assessment.current === 0 ? "disabled" : ""}>← Previous</button>${assessment.current < data.questions.length - 1 ? `<button class="btn btn-primary" data-assessment-action="next">Next question →</button>` : `<button class="btn btn-primary" data-assessment-action="review-submit">Submit assessment</button>`}</div>${assessment.confirming ? `<div class="assessment-confirm"><p>You answered ${answered} of ${data.total_questions} questions. Unanswered questions will be marked incorrect. Submit now?</p><button class="btn btn-quiet" data-assessment-action="cancel-submit">Review answers</button><button class="btn btn-primary" data-assessment-action="confirm-submit" ${assessment.busy ? "disabled" : ""}>Confirm submission</button></div>` : ""}`;
    }
  }
  app.innerHTML = `<main class="assessment-page"><a class="brand" href="#" aria-label="SkillSphere"><span class="brand-mark">S</span><span>SkillSphere</span></a><section class="card assessment-card">${content}</section><p class="assessment-footnote">SkillSphere technical assessment</p></main>`;
  const video = document.querySelector("#assessment-camera-preview");
  if (video && assessment?.proctor?.stream) {
    video.srcObject = assessment.proctor.stream;
    assessment.proctor.video = video;
    video.play().catch(error => {
      const monitor = assessment.proctor;
      if (!video.isConnected || !monitor.cameraReady) return;
      monitor.error = `The camera preview could not play: ${error.message}`;
      stopAssessmentProctoring();
      renderPublicAssessment();
    });
  }
}

async function loadPublicAssessment(token) {
  state.assessmentMode = true;
  state.publicAssessment = {
    token, data: null, current: 0, answers: {}, confirming: false, busy: false,
    error: "", result: null, timer: null,
    proctor: {
      stream: null, detector: null, video: null, animationFrame: 0, missingTimer: null,
      missingSince: null, missingReported: false, lastFaceCount: 0, cameraReady: false,
      consentGiven: false, started: false, warningCount: 0, flagged: false,
      status: "Camera is not enabled", error: "", stopping: false, processing: false,
      reportingErrorShown: false, visibilityHandler: null, blurHandler: null, focusHandler: null
    }
  };
  renderPublicAssessment();
  try {
    const assessment = await api(`/assessments/${encodeURIComponent(token)}`);
    state.publicAssessment.data = assessment;
    state.publicAssessment.proctor.warningCount = assessment.proctoring?.warning_count || 0;
    state.publicAssessment.proctor.flagged = Boolean(assessment.proctoring?.flagged);
    state.publicAssessment.proctor.consentGiven = Boolean(assessment.proctoring?.consent_given);
    state.publicAssessment.proctor.started = Boolean(assessment.proctoring?.camera_permission_granted);
    renderPublicAssessment();
    if (assessment.status === "Started") {
      startAssessmentTimer();
      if (assessment.proctoring?.required) await enableAssessmentCamera(true);
    }
  } catch (error) {
    state.publicAssessment.error = error.message;
    renderPublicAssessment();
  }
}

async function reportProctoringEvent(eventType, durationSeconds = null) {
  const assessment = state.publicAssessment;
  const monitor = assessment?.proctor;
  if (!assessment || !monitor?.started) return null;
  try {
    const result = await api(`/assessments/${encodeURIComponent(assessment.token)}/proctoring/events`, {
      method: "POST",
      body: JSON.stringify({ event_type: eventType, duration_seconds: durationSeconds })
    });
    monitor.warningCount = result.warning_count;
    monitor.flagged = result.flagged;
    monitor.status = result.flagged
      ? "Warning threshold reached; the assessment is flagged for review."
      : `Camera active · ${monitor.warningCount}/${result.max_warnings} warnings`;
    const warning = document.querySelector(".assessment-proctoring small");
    if (warning) warning.textContent = `Warnings: ${monitor.warningCount}/${result.max_warnings}${monitor.flagged ? " · Flagged for recruiter review" : ""}`;
    if (result.should_auto_submit && !assessment.busy) {
      await handleAssessmentAction("auto-submit");
    }
    return result;
  } catch (error) {
    monitor.error = `A monitoring event could not be saved: ${error.message}`;
    if (!monitor.reportingErrorShown) {
      monitor.reportingErrorShown = true;
      showToast(monitor.error, true);
    }
    renderPublicAssessment();
    return null;
  }
}

function updateFacePresence(faceCount) {
  const assessment = state.publicAssessment;
  const monitor = assessment?.proctor;
  if (!monitor) return;
  if (faceCount === 0) {
    if (monitor.lastFaceCount !== 0 || !monitor.missingSince) {
      monitor.missingSince = Date.now();
      monitor.missingReported = false;
      clearTimeout(monitor.missingTimer);
      const threshold = assessment.data.proctoring.face_missing_warning_seconds * 1000;
      monitor.missingTimer = setTimeout(() => {
        if (monitor.lastFaceCount === 0 && !monitor.missingReported) {
          monitor.missingReported = true;
          monitor.status = "No face detected; a warning was recorded.";
          reportProctoringEvent("face_missing", Math.round((Date.now() - monitor.missingSince) / 1000));
          const status = document.querySelector(".assessment-proctoring p");
          if (status) status.textContent = monitor.status;
        }
      }, threshold);
    }
  } else {
    clearTimeout(monitor.missingTimer);
    if (monitor.missingReported) reportProctoringEvent("face_returned");
    if (faceCount > 1 && monitor.lastFaceCount <= 1) reportProctoringEvent("multiple_faces");
    if (faceCount === 1 && monitor.lastFaceCount > 1) reportProctoringEvent("single_face_restored");
    monitor.missingSince = null;
    monitor.missingReported = false;
    monitor.status = faceCount > 1 ? "More than one face detected." : "One face detected.";
    const status = document.querySelector(".assessment-proctoring p");
    if (status) status.textContent = monitor.status;
  }
  monitor.lastFaceCount = faceCount;
}

function startFaceDetectionLoop() {
  const assessment = state.publicAssessment;
  const monitor = assessment?.proctor;
  if (!monitor?.detector || monitor.animationFrame) return;
  const processFrame = async () => {
    monitor.animationFrame = 0;
    const video = document.querySelector("#assessment-camera-preview");
    if (video && monitor.stream && video.srcObject !== monitor.stream) video.srcObject = monitor.stream;
    monitor.video = video;
    if (!monitor.cameraReady || monitor.stopping) return;
    if (video && video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && !monitor.processing) {
      monitor.processing = true;
      try {
        await monitor.detector.send({ image: video });
      } catch (error) {
        const message = `Local face detection stopped: ${error.message}`;
        stopAssessmentProctoring();
        monitor.error = message;
        renderPublicAssessment();
        return;
      } finally {
        monitor.processing = false;
      }
    }
    monitor.animationFrame = requestAnimationFrame(processFrame);
  };
  monitor.animationFrame = requestAnimationFrame(processFrame);
}

function attachProctoringActivityListeners() {
  const monitor = state.publicAssessment?.proctor;
  if (!monitor || monitor.visibilityHandler) return;
  monitor.visibilityHandler = () => {
    if (document.visibilityState === "hidden") reportProctoringEvent("tab_hidden");
  };
  monitor.blurHandler = () => {
    if (document.visibilityState === "visible") reportProctoringEvent("window_blur");
  };
  monitor.focusHandler = () => reportProctoringEvent("window_focus");
  document.addEventListener("visibilitychange", monitor.visibilityHandler);
  window.addEventListener("blur", monitor.blurHandler);
  window.addEventListener("focus", monitor.focusHandler);
}

async function enableAssessmentCamera(resume = false) {
  const assessment = state.publicAssessment;
  const monitor = assessment?.proctor;
  if (!assessment || !monitor || monitor.cameraReady) return;
  if (!resume && !document.querySelector("[data-proctor-consent]")?.checked) {
    monitor.error = "Please confirm consent before enabling camera access.";
    renderPublicAssessment();
    return;
  }
  monitor.stopping = false;
  monitor.error = "";
  monitor.status = "Requesting camera access…";
  renderPublicAssessment();
  try {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("Camera access requires a supported browser and a secure HTTPS connection.");
    }
    monitor.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user" },
      audio: false
    });
    renderPublicAssessment();
    const video = document.querySelector("#assessment-camera-preview");
    if (!video) throw new Error("The camera preview could not be initialized.");
    video.srcObject = monitor.stream;
    await video.play();
    if (typeof window.FaceDetection !== "function") {
      throw new Error("The local face detector could not be loaded. Refresh the page or contact the hiring team.");
    }
    monitor.status = "Preparing the local face detector…";
    renderPublicAssessment();
    monitor.detector = new window.FaceDetection({
      locateFile: filename => `/vendor/mediapipe/${filename}`
    });
    monitor.detector.setOptions({ model: "short", selfieMode: true, minDetectionConfidence: 0.6 });
    monitor.detector.onResults(results => updateFacePresence(results.detections?.length || 0));
    await monitor.detector.initialize();
    const registration = await api(`/assessments/${encodeURIComponent(assessment.token)}/proctoring/start`, {
      method: "POST",
      body: JSON.stringify({ consent_given: true, camera_permission_granted: true })
    });
    if (!registration.started && registration.required) {
      throw new Error("Camera consent and permission are required before starting.");
    }
    monitor.consentGiven = true;
    monitor.cameraReady = true;
    monitor.started = Boolean(assessment.data.status === "Started");
    monitor.status = "Camera is active. Frames are processed only in this browser.";
    monitor.error = "";
    if (monitor.started) attachProctoringActivityListeners();
    for (const track of monitor.stream.getVideoTracks()) {
      track.addEventListener("ended", () => {
        if (monitor.stopping) return;
        const message = "Camera access stopped.";
        if (monitor.started) reportProctoringEvent("camera_stopped");
        stopAssessmentProctoring();
        monitor.status = "Camera access stopped.";
        monitor.error = message;
        renderPublicAssessment();
      }, { once: true });
    }
    renderPublicAssessment();
    startFaceDetectionLoop();
  } catch (error) {
    monitor.error = error.message;
    monitor.status = "Camera or face detection is unavailable.";
    let eventErrorMessage = "";
    try {
      await api(`/assessments/${encodeURIComponent(assessment.token)}/proctoring/events`, {
        method: "POST", body: JSON.stringify({ event_type: "camera_denied" })
      });
    } catch (eventError) {
      eventErrorMessage = ` The camera event could not be recorded: ${eventError.message}`;
    }
    stopAssessmentProctoring();
    monitor.error = `${error.message}${eventErrorMessage}`;
    renderPublicAssessment();
  }
}

function stopAssessmentProctoring() {
  const monitor = state.publicAssessment?.proctor;
  if (!monitor) return;
  monitor.stopping = true;
  if (monitor.animationFrame) cancelAnimationFrame(monitor.animationFrame);
  clearTimeout(monitor.missingTimer);
  monitor.animationFrame = 0;
  monitor.missingTimer = null;
  if (monitor.visibilityHandler) document.removeEventListener("visibilitychange", monitor.visibilityHandler);
  if (monitor.blurHandler) window.removeEventListener("blur", monitor.blurHandler);
  if (monitor.focusHandler) window.removeEventListener("focus", monitor.focusHandler);
  monitor.visibilityHandler = null;
  monitor.blurHandler = null;
  monitor.focusHandler = null;
  for (const track of monitor.stream?.getTracks() || []) track.stop();
  monitor.stream = null;
  monitor.cameraReady = false;
  if (monitor.detector) {
    monitor.detector.close().catch(error => console.error("Could not close the local face detector.", error));
    monitor.detector = null;
  }
}

function startAssessmentTimer() {
  const assessment = state.publicAssessment;
  clearInterval(assessment.timer);
  assessment.timer = setInterval(() => {
    const expiresAt = new Date(assessment.data.expires_at).getTime();
    if (Date.now() >= expiresAt) {
      clearInterval(assessment.timer);
      handleAssessmentAction("auto-submit");
    } else {
      const remaining = Math.ceil((expiresAt - Date.now()) / 1000);
      const timer = document.querySelector(".assessment-timer");
      if (timer) timer.textContent = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;
    }
  }, 1000);
}

async function handleAssessmentAction(action) {
  const assessment = state.publicAssessment;
  if (!assessment || assessment.busy) return;
  if (action === "previous") {
    assessment.current = Math.max(0, assessment.current - 1);
    assessment.confirming = false;
    renderPublicAssessment();
    return;
  }
  if (action === "next") {
    assessment.current = Math.min(assessment.data.questions.length - 1, assessment.current + 1);
    renderPublicAssessment();
    return;
  }
  if (action === "review-submit") {
    assessment.confirming = true;
    renderPublicAssessment();
    return;
  }
  if (action === "cancel-submit") {
    assessment.confirming = false;
    renderPublicAssessment();
    return;
  }
  if (action === "enable-proctoring" || action === "resume-camera") {
    assessment.busy = true;
    await enableAssessmentCamera(action === "resume-camera");
    assessment.busy = false;
    renderPublicAssessment();
    return;
  }
  if (action === "start") {
    assessment.busy = true;
    renderPublicAssessment();
    try {
      assessment.data = await api(`/assessments/${encodeURIComponent(assessment.token)}/start`, { method: "POST" });
      assessment.proctor.started = Boolean(assessment.data.proctoring?.required);
      assessment.proctor.warningCount = assessment.data.proctoring?.warning_count || 0;
      assessment.proctor.flagged = Boolean(assessment.data.proctoring?.flagged);
      if (assessment.proctor.started) attachProctoringActivityListeners();
      assessment.busy = false;
      renderPublicAssessment();
      startAssessmentTimer();
    } catch (error) {
      assessment.busy = false;
      assessment.error = error.message;
      renderPublicAssessment();
    }
    return;
  }
  if (action === "confirm-submit" || action === "auto-submit") {
    assessment.busy = true;
    renderPublicAssessment();
    try {
      assessment.result = await api(`/assessments/${encodeURIComponent(assessment.token)}/submit`, {
        method: "POST",
        body: JSON.stringify({ answers: assessment.answers })
      });
      clearInterval(assessment.timer);
      stopAssessmentProctoring();
    } catch (error) {
      assessment.error = error.message;
      clearInterval(assessment.timer);
    } finally {
      assessment.busy = false;
      renderPublicAssessment();
    }
  }
}

function saveToken(token) {
  state.token = token;
  if (token) localStorage.setItem("skillsphere_token", token);
  else localStorage.removeItem("skillsphere_token");
}

function renderAuth(register = false) {
  app.innerHTML = `
    <main class="auth-page">
      <section class="auth-copy">
        <a class="brand" href="#" aria-label="SkillSphere home"><span class="brand-mark">S</span><span>SkillSphere</span></a>
        <span class="eyebrow">A better kind of career platform</span>
        <h1>Find work that <em>feels right.</em></h1>
        <p>Bring your experience, discover opportunities that fit, and make your next move with confidence.</p>
        <div class="auth-note"><span>●</span> Thoughtful matches. Real opportunities. One place.</div>
        <div class="preview-card" aria-hidden="true">
          <div class="preview-head"><strong>Your next move, made clearer</strong><span class="badge badge-green">Good fit</span></div>
          <div class="preview-lines"><span class="preview-line red"></span><span class="preview-line"></span><span class="preview-line"></span><span class="preview-line" style="width:72%"></span></div>
          <div class="preview-job"><strong>Product Designer · Northstar</strong><span style="color:#c83b42;font-weight:700">92% match</span></div>
          <div class="preview-job"><strong>Frontend Engineer · Layer</strong><span style="color:#c83b42;font-weight:700">84% match</span></div>
        </div>
      </section>
      <section class="auth-card">
        <span class="eyebrow">${register ? "Get started" : "Welcome back"}</span>
        <h2>${register ? "Create your account" : "Sign in to SkillSphere"}</h2>
        <p>${register ? "Set up your space and start finding your fit." : "Your next opportunity is closer than you think."}</p>
        <form class="auth-form" id="auth-form">
          ${register ? `<div class="field"><label for="auth-name">Full name</label><input class="input" id="auth-name" name="name" autocomplete="name" placeholder="Alex Morgan" required minlength="2" /></div>` : ""}
          <div class="field"><label for="auth-email">Email address</label><input class="input" id="auth-email" name="email" type="email" autocomplete="email" placeholder="you@example.com" required /></div>
          ${register ? `<div class="field"><label for="auth-role">I am a</label><select class="select" id="auth-role" name="role"><option value="candidate">Job seeker</option><option value="recruiter">Recruiter</option></select></div>` : ""}
          <div class="field"><label for="auth-password">Password</label><input class="input" id="auth-password" name="password" type="password" autocomplete="${register ? "new-password" : "current-password"}" placeholder="${register ? "At least 8 characters" : "Enter your password"}" minlength="${register ? "8" : "1"}" required /></div>
          <button class="btn btn-primary auth-submit" type="submit" ${state.busy ? "disabled" : ""}>${state.busy ? "Please wait…" : register ? "Create account →" : "Sign in →"}</button>
        </form>
        <div class="auth-switch">${register ? "Already have an account?" : "New to SkillSphere?"} <button type="button" data-action="toggle-auth">${register ? "Sign in" : "Create an account"}</button></div>
        ${register ? "" : `<div class="demo-logins"><span>Explore a demo workspace · password: ${demoPassword}</span><div class="demo-buttons"><button data-demo="candidate@skillsphere.dev">Job seeker</button><button data-demo="recruiter@skillsphere.dev">Recruiter</button><button data-demo="admin@skillsphere.dev">Admin</button></div></div>`}
      </section>
    </main>`;
  app.dataset.register = String(register);
}

function navItems() {
  const common = [["Dashboard", "⌂"], ["Find jobs", "⌕"], ["Applications", "▤"], ["Assessments", "✓"], ["My profile", "◉"]];
  if (state.user?.role === "recruiter") return [["Dashboard", "⌂"], ["My jobs", "▣"], ["Applicants", "♙"], ["Assessments", "✓"], ["Question bank", "☷"], ["Interviews", "◷"], ["Company profile", "◉"]];
  if (state.user?.role === "admin") return [["Dashboard", "⌂"], ["Users", "♙"], ["Job listings", "▣"], ["Assessments", "✓"], ["Question bank", "☷"], ["Interviews", "◷"]];
  return [...common, ["Interviews", "◷"]];
}

function updatePageTitle(pageName) {
  const titles = {
    "Dashboard": "Dashboard — SkillSphere",
    "Find jobs": "Find Jobs — SkillSphere",
    "Applications": "My Applications — SkillSphere",
    "Assessments": "Assessments — SkillSphere",
    "My profile": "My Profile — SkillSphere",
    "Applicants": "Applicants — SkillSphere",
    "My jobs": "My Jobs — SkillSphere",
    "Question bank": "Question Bank — SkillSphere",
    "Interviews": "Interviews — SkillSphere",
    "Interview room": "Interview Room — SkillSphere",
    "Company profile": "Company Profile — SkillSphere",
    "Users": "Users — SkillSphere",
    "Job listings": "Job Listings — SkillSphere",
  };
  document.title = titles[pageName] || "SkillSphere — find your next move";
}

function notificationPanel() {
  if (!state.notificationsOpen) return "";
  const rows = state.notifications;
  return `<section class="notification-panel" aria-label="Notifications">
    <div class="notification-head"><div><strong>Notifications</strong><small>${state.unreadNotifications} unread</small></div><button class="text-button" data-action="mark-all-read" ${state.unreadNotifications ? "" : "disabled"}>Mark all read</button></div>
    ${rows.length ? rows.map(item => `<div class="notification-item${item.is_read ? "" : " unread"}">
      <button data-action="open-notification" data-id="${esc(item.id)}"><span class="notification-dot"></span><span><strong>${esc(item.title)}</strong><small>${esc(item.message)}</small><time>${dateLabel(item.created_at)}</time></span></button>
      ${item.action_url && item.type.startsWith("interview_") && item.related_application_id
        ? `<button class="notification-link" data-action="notification-application" data-id="${esc(item.id)}">View application ↗</button>`
        : item.action_url ? `<a class="notification-link" href="${esc(item.action_url)}" target="_blank" rel="noopener noreferrer">Open development link ↗</a>` : ""}
    </div>`).join("") : `<div class="notification-empty">You’re all caught up.</div>`}
  </section>`;
}

function shell(content) {
  updatePageTitle(state.page);
  const active = name => state.page === name ? " active" : "";
  const nav = navItems().map(([name, glyph]) =>
    `<button class="nav-item${active(name)}" data-page="${esc(name)}">${icon(glyph, name)}</button>`).join("");
  app.innerHTML = `
    <div class="app-shell">
      <header class="topbar">
        <a class="brand" href="#" data-page="Dashboard"><span class="brand-mark">S</span><span>SkillSphere</span></a>
        <div class="topbar-right">
          <button class="top-link" data-page="${state.user?.role === "candidate" ? "Find jobs" : "Dashboard"}">${state.user?.role === "candidate" ? "Explore roles" : "Workspace"}</button>
          <button class="icon-button notification-toggle" aria-label="Notifications${state.unreadNotifications ? `, ${state.unreadNotifications} unread` : ""}" data-action="toggle-notifications">♧${state.unreadNotifications ? `<span class="notification-count">${state.unreadNotifications > 9 ? "9+" : state.unreadNotifications}</span>` : ""}</button>
          <div class="user-chip"><div class="avatar">${esc(initials(state.user?.name))}</div><div class="user-copy"><strong>${esc(state.user?.name)}</strong><small>${esc(state.user?.role)}</small></div></div>
          <button class="icon-button" aria-label="Sign out" title="Sign out" data-action="logout">↗</button>
        </div>
      </header>
      <div class="workspace">
        <aside class="sidebar">
          <div class="side-profile"><div class="avatar">${esc(initials(state.user?.name))}</div><div><strong>${esc(state.user?.name)}</strong><small>${esc(state.user?.role)} workspace</small></div></div>
          <div class="nav-caption">Workspace</div><nav class="nav-list" aria-label="Main navigation">${nav}</nav>
          <div class="sidebar-foot"><button data-action="logout">↗ &nbsp; Sign out</button></div>
        </aside>
        <main class="main" id="main-content">${content}</main>
      </div>
    </div>
    ${notificationPanel()}
    ${state.modal ? renderModal() : ""}`;
}

function heading(kicker, title, description, action = "") {
  return `<div class="page-heading"><div><span class="eyebrow">${esc(kicker)}</span><h1>${title}</h1><p>${description}</p></div>${action}</div>`;
}

function stat(label, value, note, glyph) {
  return `<article class="stat-card"><span class="stat-icon">${glyph}</span><span class="stat-label">${label}</span><div class="stat-value">${value}</div><span class="stat-note">${note}</span></article>`;
}

function empty(title, detail, glyph = "✳") {
  return `<div class="empty"><div class="empty-icon">${glyph}</div><strong>${title}</strong><p>${detail}</p></div>`;
}

function jobRow(job, actionLabel = "View role") {
  const action = actionLabel === "Applicants" ? `data-page="Applicants"` : `data-job="${esc(job.id)}"`;
  return `<article class="job-row"><div class="company-mark">${esc(initials(job.company))}</div><div><h3>${esc(job.title)}</h3><p>${esc(job.company)} · ${esc(job.location)}</p><div class="job-meta"><span>${esc(job.job_type || "Full-time")}</span><span>${esc(job.experience || "All levels")}</span>${job.salary ? `<span>${esc(job.salary)}</span>` : ""}</div></div><button class="btn btn-quiet btn-small" ${action}>${actionLabel}</button></article>`;
}

function applicationRows(applications) {
  if (!applications.length) return empty("No applications yet", "Your next opportunity is only a search away.", "↗");
  return applications.slice(0, 5).map(item => `<div class="application-row"><div><strong>${esc(item.title)}</strong><small>${esc(item.company)} · Applied ${dateLabel(item.applied_at)}</small></div><span class="badge ${["Accepted", "Selected"].includes(item.status) ? "badge-green" : item.status === "Rejected" ? "" : "badge-red"}">${esc(item.status)}</span></div>`).join("");
}

function interviewDisplayStatus(interview) {
  if (interview.status === "Cancelled") return "Interview Cancelled";
  if (interview.status === "Completed") return "Interview Completed";
  const endTime = new Date(interview.scheduled_at).getTime() + Number(interview.duration_minutes || 0) * 60_000;
  return endTime <= Date.now() ? "Interview Ended" : "Interview Scheduled";
}

function localTimeZone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
}

function candidateInterviewTime(interview) {
  return `${dateTimeLabel(interview.scheduled_at)} · Your local time (${localTimeZone()}); scheduled time zone: ${interview.timezone}`;
}

function interviewScheduleDetails(interview, includeActions = true) {
  const status = interviewDisplayStatus(interview);
  const join = status === "Interview Scheduled" && /^https:\/\//i.test(interview.meeting_url || "")
    ? `<a class="btn btn-primary btn-small join-interview" href="${esc(interview.meeting_url)}" target="_blank" rel="noopener noreferrer">Join Interview ↗</a>`
    : "";
  return `<div class="interview-details">
    <span class="badge ${status === "Interview Scheduled" ? "badge-green" : status === "Interview Cancelled" ? "badge-red" : "badge-amber"}">${status}</span>
    <small>${esc(candidateInterviewTime(interview))}</small>
    <small>${esc(interview.duration_minutes)} minutes · ${esc(interview.meeting_platform)}</small>
    ${interview.recruiter_message ? `<p>${esc(interview.recruiter_message)}</p>` : ""}
    ${status === "Interview Scheduled" && !interview.meeting_url ? `<small>The recruiter has not added a meeting link yet.</small>` : ""}
    <div class="interview-actions">${join}${includeActions ? `<button class="text-button" data-action="view-interview" data-id="${esc(interview.id)}">View Interview Details</button>` : ""}</div>
  </div>`;
}

function localDateTimeInput(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

function dateTimeInputInZone(value, timeZone) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  try {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
      timeZone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23"
    }).formatToParts(date).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  } catch (error) {
    if (!(error instanceof RangeError)) throw error;
    return localDateTimeInput(value);
  }
}

function currentLocalDateTimeInput() {
  const date = new Date();
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

function candidateDashboard() {
  const candidateApps = state.applications;
  const latest = state.jobs.slice(0, 3);
  const upcomingInterview = state.upcomingInterviews[0];
  return `${heading("Your career, in motion", `Good to see you, ${esc(state.user.name.split(" ")[0])} <span style="color:#c83b42">✳</span>`, "A little progress today can open a big door tomorrow.", `<button class="btn btn-primary" data-page="Find jobs">Find your next role&nbsp; →</button>`)}
    <section class="grid stats-grid">
      ${stat("Active applications", candidateApps.filter(item => !["Rejected", "Withdrawn", "Accepted", "Selected"].includes(item.status)).length, "Your opportunities in progress", "↗")}
      ${stat("Roles to explore", state.jobs.length, "Open roles picked for you", "⌕")}
      ${stat("Resume status", state.resume ? "Ready" : "Add", state.resume ? "Your profile is on file" : "Upload to unlock matching", "▤")}
      ${stat("Profile strength", state.resume ? "78%" : "36%", state.resume ? "Keep your details up to date" : "A resume is a great start", "◉")}
    </section>
    <section class="columns">
      <article class="card"><div class="card-heading"><div><h2>Roles to explore</h2><p>Thoughtful opportunities for your next chapter</p></div><button class="text-button" data-page="Find jobs">See all →</button></div><div class="job-list">${latest.length ? latest.map(job => jobRow(job)).join("") : empty("No open roles yet", "Check back soon for new opportunities.")}</div></article>
      <article class="card"><div class="card-heading"><div><h2>Your applications</h2><p>A quick look at where things stand</p></div><button class="text-button" data-page="Applications">View all →</button></div><div class="application-list">${applicationRows(candidateApps)}</div></article>
    </section>
    <section class="card upcoming-interview-card"><div class="card-heading"><div><h2>Upcoming interviews</h2><p>Your next conversation, all in one place</p></div><button class="text-button" data-page="Interviews">View all →</button></div>
      ${upcomingInterview ? `<article class="upcoming-interview"><span class="eyebrow">${esc(upcomingInterview.company)}</span><h3>${esc(upcomingInterview.title)}</h3><p>${esc(candidateInterviewTime(upcomingInterview))} · ${esc(upcomingInterview.duration_minutes)} minutes</p><div class="interview-actions">${/^https:\/\//i.test(upcomingInterview.meeting_url || "") ? `<a class="btn btn-primary btn-small join-interview" href="${esc(upcomingInterview.meeting_url)}" target="_blank" rel="noopener noreferrer">Join Interview ↗</a>` : `<span class="stat-note">The recruiter has not added a meeting link yet.</span>`}<button class="text-button" data-action="view-interview" data-id="${esc(upcomingInterview.id)}">View Interview Details</button></div></article>` : empty("No upcoming interviews", "When a recruiter schedules an interview, it will appear here.", "◷")}
    </section>
    <section class="card" style="margin-top:16px"><div class="card-heading"><div><h2>${state.resume ? "Your resume is ready" : "Take the guesswork out of your search"}</h2><p>${state.resume ? `${esc(state.resume.filename)} · ${esc(state.resume.processing_status)}` : "Upload a resume to discover which roles match your skills."}</p></div><button class="btn btn-quiet btn-small" data-page="My profile">${state.resume ? "View profile" : "Add your resume"}</button></div>
      <div class="progress-track"><div class="progress-fill" style="width:${state.resume ? "78%" : "36%"}"></div></div>
      ${state.resume?.processing_error ? `<p style="color:#aa3c40;font-size:10px;margin:9px 0 0">${esc(state.resume.processing_error)}</p>` : ""}
    </section>`;
}

function jobCard(job) {
  const skills = (job.skills || []).slice(0, 4).map(skill => `<span class="skill">${esc(skill)}</span>`).join("");
  return `<article class="job-card"><div class="job-card-top"><div><h3>${esc(job.title)}</h3><p>${esc(job.company)} · ${esc(job.location)}</p></div><span class="company-mark">${esc(initials(job.company))}</span></div><div class="job-meta"><span>${esc(job.job_type || "Full-time")}</span><span>${esc(job.experience || "All levels")}</span><span>${esc(dateLabel(job.created_at))}</span></div><div class="skills">${skills}</div><div class="job-card-bottom"><span class="salary">${esc(job.salary || "Salary not listed")}</span><button class="btn btn-primary btn-small" data-job="${esc(job.id)}">View details →</button></div></article>`;
}

function jobsPage() {
  const action = state.user.role === "recruiter" ? `<button class="btn btn-primary" data-action="new-job">＋ Post a role</button>` : "";
  return `${heading("Discover what's next", "Find your next <span style='color:#c83b42'>great fit.</span>", "Explore open roles and focus on the ones that feel right.", action)}
    <section class="card"><div class="filters">
      <div class="filter-input"><input class="input" id="job-query" placeholder="⌕   Search role, company, or skill" value="${esc(state.query)}" aria-label="Search jobs" /></div>
      <div class="filter-field"><input class="input" id="job-location" placeholder="Location" value="${esc(state.location)}" aria-label="Filter by location" /></div>
      <div class="filter-field"><select class="select" id="job-type" aria-label="Filter by job type"><option value="">All job types</option>${["Full-time", "Part-time", "Contract", "Remote"].map(value => `<option ${state.jobType === value ? "selected" : ""}>${value}</option>`).join("")}</select></div>
      <div class="filter-field"><input class="input" id="job-experience" placeholder="Experience" value="${esc(state.experience)}" aria-label="Filter by experience" /></div>
      <div class="filter-field"><input class="input" id="job-skill" placeholder="Required skill" value="${esc(state.skill)}" aria-label="Filter by skill" /></div>
      <div class="filter-field"><select class="select" id="job-sort" aria-label="Sort job results"><option value="newest" ${state.sortBy === "newest" ? "selected" : ""}>Newest first</option><option value="company" ${state.sortBy === "company" ? "selected" : ""}>Company A–Z</option><option value="title" ${state.sortBy === "title" ? "selected" : ""}>Role A–Z</option></select></div>
      <button class="btn btn-quiet" data-action="filter-jobs">Search roles</button>
    </div>
    <div class="card-heading"><div><h2>Open opportunities <span class="badge">${state.jobs.length}</span></h2><p>Roles that match your search</p></div><span class="badge badge-green">Updated today</span></div>
    <div class="job-grid">${state.jobs.length ? state.jobs.map(jobCard).join("") : empty("No roles found", "Try a different search or clear your filters.", "⌕")}</div>
    ${state.jobTotal > 6 ? `<div class="job-card-bottom" style="margin-top:14px"><span class="stat-note">Page ${state.jobPage} of ${Math.ceil(state.jobTotal / 6)}</span><div style="display:flex;gap:7px"><button class="btn btn-quiet btn-small" data-action="prev-page" ${state.jobPage <= 1 ? "disabled" : ""}>← Previous</button><button class="btn btn-quiet btn-small" data-action="next-page" ${state.jobPage >= Math.ceil(state.jobTotal / 6) ? "disabled" : ""}>Next →</button></div></div>` : ""}
    </section>`;
}

function applicationsPage() {
  const rows = state.applications;
  return `${heading("Keep your momentum", "My applications", "See where every opportunity stands, all in one place.")}
    <section class="card"><div class="card-heading"><div><h2>Application history</h2><p>${rows.length} submitted application${rows.length === 1 ? "" : "s"}</p></div></div>
    ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>Role</th><th>Match</th><th>Applied</th><th>Pipeline</th><th>Assessment</th><th>Interview</th><th>Action</th></tr></thead><tbody>${rows.map(item => `<tr>
      <td><strong>${esc(item.title)}</strong><small>${esc(item.company)} · <button class="text-button" data-action="application-history" data-id="${esc(item.id)}">View timeline</button></small></td>
      <td><strong>${item.match_percentage == null ? "—" : `${esc(item.match_percentage)}%`}</strong><details class="skill-details"><summary>Skills</summary><small>Matched: ${esc((item.matched_skills || []).join(", ") || "None")}</small><small>To develop: ${esc((item.missing_skills || []).join(", ") || "None")}</small></details></td>
      <td>${dateLabel(item.applied_at)}</td><td><span class="badge ${["Accepted", "Selected"].includes(item.status) ? "badge-green" : item.status === "Rejected" ? "" : "badge-red"}">${esc(item.status)}</span></td>
      <td>${item.assessment_status ? `<span class="badge ${item.assessment_result === "Passed" ? "badge-green" : item.assessment_result === "Failed" ? "badge-red" : "badge-amber"}">${esc(item.assessment_result || item.assessment_status)}</span>${item.assessment_score_percentage == null ? "" : `<small class="assessment-result">${esc(item.assessment_score_percentage)}% · ${esc(item.assessment_correct_count)}/20</small>`}` : "—"}</td>
      <td>${item.interview ? interviewScheduleDetails(item.interview) : "—"}</td>
      <td>${["Applied", "Under Review", "Shortlisted", "Assessment Sent", "Assessment Started"].includes(item.status) ? `<button class="text-button" data-action="withdraw" data-id="${esc(item.id)}">Withdraw</button>` : "—"}</td>
    </tr>`).join("")}</tbody></table></div>` : empty("Your story starts here", "Find a role that feels right and apply when you're ready.")}
    </section>`;
}

function statusOptions(current) {
  const allowed = {
    Applied: ["Under Review", "Shortlisted", "Rejected"],
    "Under Review": ["Shortlisted", "Rejected"],
    Shortlisted: ["Interview", "Accepted", "Rejected"],
    "Assessment Sent": ["Interview", "Accepted", "Rejected"],
    "Assessment Started": ["Interview", "Rejected"],
    "Assessment Completed": ["Interview", "Accepted", "Rejected"],
    "Assessment Expired": ["Interview", "Accepted", "Rejected"],
    "Round 1 Passed": ["Interview", "Accepted", "Rejected"],
    "Round 1 Failed": ["Accepted", "Rejected"],
    Interview: ["Accepted", "Rejected"],
    Accepted: [],
    Rejected: [],
    Withdrawn: []
  };
  const selected = current === "Selected" ? "Accepted" : current;
  return [...new Set([selected, ...(allowed[selected] || [])])];
}

function assessmentsPage() {
  const rows = state.assessments;
  const filters = state.user.role === "candidate" ? "" : `<form id="assessment-filter-form" class="filters assessment-filters">
      <select class="select" name="result" aria-label="Filter by result"><option value="">Passed and failed</option><option value="Passed" ${state.assessmentFilters.result === "Passed" ? "selected" : ""}>Passed</option><option value="Failed" ${state.assessmentFilters.result === "Failed" ? "selected" : ""}>Failed</option></select>
      <input class="input" type="number" min="0" max="100" step="0.1" name="min_score" value="${esc(state.assessmentFilters.min_score)}" placeholder="Min score %" aria-label="Minimum score percentage" />
      <input class="input" type="number" min="0" max="100" step="0.1" name="max_score" value="${esc(state.assessmentFilters.max_score)}" placeholder="Max score %" aria-label="Maximum score percentage" />
      <select class="select" name="job_id" aria-label="Filter by job"><option value="">All jobs</option>${(state.user.role === "admin" ? state.adminJobs : state.jobs).map(job => `<option value="${esc(job.id)}" ${String(state.assessmentFilters.job_id) === String(job.id) ? "selected" : ""}>${esc(job.title)}</option>`).join("")}</select>
      <select class="select" name="status" aria-label="Filter by assessment status"><option value="">All assessment statuses</option>${["Sent", "Started", "Completed", "Expired"].map(status => `<option value="${status}" ${state.assessmentFilters.status === status ? "selected" : ""}>${status}</option>`).join("")}</select>
      <input class="input" type="date" name="completed_from" value="${esc(state.assessmentFilters.completed_from)}" aria-label="Completion date from" />
      <input class="input" type="date" name="completed_to" value="${esc(state.assessmentFilters.completed_to)}" aria-label="Completion date to" />
      <button class="btn btn-quiet btn-small" type="submit">Apply filters</button>
    </form>`;
  return `${heading("Round 1 assessment pipeline", "Assessments", "Review Round 1 results and decide which passed candidates move forward.")}
    <section class="card"><div class="card-heading"><div><h2>Round 1: Technical MCQ Test</h2><p>${rows.length} assessment${rows.length === 1 ? "" : "s"}</p></div></div>
    ${filters}
    ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>Candidate</th><th>Role</th><th>Assessment</th><th>Score</th><th>Result</th><th>Completed</th>${state.user.role === "candidate" ? "" : "<th>Pipeline stage</th><th>Next step</th><th>Review</th>"}</tr></thead><tbody>${rows.map(item => `<tr>
      <td><strong>${esc(state.user.role === "candidate" ? "You" : item.candidate_name)}</strong>${state.user.role !== "candidate" ? `<small>${esc(item.candidate_email)}</small>` : ""}</td>
      <td>${esc(item.title)}<small>${esc(item.company)}</small></td>
      <td>${esc(item.assessment_name || "Round 1: Technical MCQ Test")}<small>${esc(item.technology || "Topic not selected")}</small><small>${esc(item.total_questions)} questions · ${esc(item.duration_minutes)} min · Pass ${esc(item.pass_percentage)}%</small></td>
      <td>${item.score_percentage == null ? "—" : `<strong>${esc(item.score_percentage)}%</strong><small>${esc(item.correct_answer_count ?? item.score)} correct / ${esc(item.total_questions)}</small>`}</td>
      <td>${item.result ? `<span class="badge ${item.result === "Passed" ? "badge-green" : "badge-red"}">${esc(item.result)}</span>${state.user.role === "candidate" ? `<small>${item.result === "Passed" ? "Congratulations, you have cleared Round 1." : "Thank you for completing Round 1. You did not reach the required passing score."}</small>` : ""}` : `<span class="badge badge-amber">${esc(item.assessment_status)}</span>`}</td>
      <td>${dateTimeLabel(item.submitted_at)}</td>
      ${state.user.role === "candidate" ? "" : `<td>${esc(item.application_status)}</td><td>${item.result === "Passed" ? `<select class="status-select" data-assessment-pipeline="${esc(item.application_id)}" aria-label="Move ${esc(item.candidate_name)} to next stage"><option value="">Choose next stage</option>${statusOptions(item.application_status).filter(value => ["Interview", "Accepted", "Rejected"].includes(value)).map(value => `<option>${value}</option>`).join("")}</select>` : "—"}</td><td>${item.assessment_status === "Completed" ? `<button class="text-button" data-action="review-assessment" data-id="${esc(item.id)}">Review answers</button>` : "—"}</td>`}
    </tr>`).join("")}</tbody></table></div>` : empty("No assessments found", state.user.role === "candidate" ? "Assessment details and results will appear here when available." : "Assessments matching your filters will appear here.", "✓")}
    </section>`;
}

function questionBankPage() {
  const jobs = state.user.role === "admin" ? state.adminJobs : state.jobs;
  if (!state.questionBankJob) {
    return `${heading("Job-specific assessments", "Question bank", "Build and maintain a separate technical question bank for each job.")}<section class="card">
      <div class="card-heading"><div><h2>Choose a job</h2><p>Each assessment requires 20 active questions across relevant skill topics.</p></div></div>
      <div class="job-list">${jobs.map(job => `<article class="job-row"><div class="company-mark">${esc(initials(job.company))}</div><div><h3>${esc(job.title)}</h3><p>${esc(job.company)} · ${esc(job.status || "Open")}</p></div><button class="btn btn-quiet btn-small" data-action="manage-question-bank" data-id="${esc(job.id)}">Manage bank</button></article>`).join("") || empty("No jobs available", "Post a job before adding its assessment questions.")}</div>
    </section>`;
  }
  const summary = state.questionBankSummary || { active_count: 0, topics: [] };
  const counts = summary.topics || [];
  const preview = state.questionImportPreview;
  return `${heading("Question bank", esc(state.questionBankJob.title), `${esc(state.questionBankJob.company)} · Assessments use 20 active questions across relevant skill topics.`, `<button class="btn btn-quiet btn-small" data-action="back-question-banks">← All jobs</button>`)}
    <section class="grid stats-grid bank-topics">${counts.map(topic => stat(topic.technology, topic.active_count, topic.ready ? "Ready for assessments" : `${topic.additional_needed} more active questions needed`, topic.ready ? "✓" : "＋")).join("") || stat("No topics yet", 0, "Add questions to create a topic", "＋")}</section>
    <section class="card import-card"><div class="card-heading"><div><h2>Import a question-bank PDF</h2><p>Questions remain a preview until you review and confirm the selections.</p></div><span class="badge ${preview ? "badge-amber" : "badge-green"}">${preview ? "Review required" : "Private upload"}</span></div>
      <form id="question-bank-import-form" class="import-upload">
        <div class="field"><label for="question-bank-pdf">PDF file (10 MB maximum)</label><input class="input" id="question-bank-pdf" name="pdf" type="file" accept="application/pdf,.pdf" required /></div>
        <button class="btn btn-primary" type="submit" ${state.busy ? "disabled" : ""}>Upload for review →</button>
      </form>
      <details class="import-template"><summary>PDF format and example</summary><p>Repeat the Technology, Difficulty, and Question labels for each question. Use four A–D options and an Answer line containing the correct letter.</p><pre>Technology: Python
Difficulty: Easy
Question: What does len(items) return?
A. The number of items
B. The first item
C. The item's type
D. The last index
Answer: A</pre><p>Difficulty may be Easy, Medium, or Hard. Text PDFs are recommended. Scanned PDFs require Tesseract OCR to be installed.</p></details>
    </section>
    ${preview ? `<section class="card import-preview"><div class="card-heading"><div><h2>Review ${esc(preview.filename)}</h2><p>${esc(preview.valid_count)} valid · ${esc(preview.invalid_count)} need attention · ${esc(preview.total_extracted)} extracted</p></div><span class="badge badge-amber">Not saved yet</span></div>
      ${Object.keys(preview.technology_counts || {}).length ? `<p class="import-counts">${Object.entries(preview.technology_counts).map(([topic, count]) => `${esc(topic)}: ${esc(count)}`).join(" · ")}</p>` : ""}
      ${Object.keys(preview.difficulty_counts || {}).length ? `<p class="import-counts">By difficulty: ${Object.entries(preview.difficulty_counts).map(([level, count]) => `${esc(level)}: ${esc(count)}`).join(" · ")}</p>` : ""}
      <form id="question-import-review-form" novalidate><div class="import-question-list">${preview.questions.map(item => `<fieldset class="import-question" data-import-question data-source-index="${esc(item.source_index)}">
        <div class="import-question-head"><label><input type="checkbox" name="include" ${item.include ? "checked" : ""} /> Import this question</label><span class="eyebrow">Question ${esc(item.source_index + 1)}</span></div>
        ${item.errors?.length ? `<ul class="import-errors">${item.errors.map(message => `<li>${esc(message)}</li>`).join("")}</ul>` : ""}
        <div class="import-fields"><div class="field"><label>Technology</label><input class="input" name="technology" maxlength="50" value="${esc(item.technology)}" required /></div>
        <div class="field"><label>Difficulty</label><select class="select" name="difficulty">${["Easy", "Medium", "Hard"].map(level => `<option ${item.difficulty === level ? "selected" : ""}>${level}</option>`).join("")}</select></div>
        <div class="field span-2"><label>Question</label><textarea class="textarea" name="question" minlength="10" maxlength="1000" required>${esc(item.question)}</textarea></div>
        ${item.options.map((option, index) => `<div class="field"><label>Option ${String.fromCharCode(65 + index)}</label><input class="input" name="option_${index}" value="${esc(option)}" required /></div>`).join("")}
        <div class="field"><label>Correct answer</label><select class="select" name="correct_index">${[0, 1, 2, 3].map(index => `<option value="${index}" ${item.correct_index === index ? "selected" : ""}>Option ${String.fromCharCode(65 + index)}</option>`).join("")}</select></div></div>
      </fieldset>`).join("") || empty("No question blocks found", "Use the PDF format example above and upload the document again.")}</div>
      <div class="import-actions"><button class="btn btn-quiet" type="button" data-action="cancel-question-import" ${state.busy ? "disabled" : ""}>Cancel preview</button><button class="btn btn-primary" type="submit" ${state.busy ? "disabled" : ""}>Confirm selected questions →</button></div></form>
    </section>` : ""}
    ${state.questionBankImports.length ? `<section class="card import-history"><div class="card-heading"><div><h2>Import history</h2><p>Recent PDF uploads for this job</p></div></div><div class="import-history-list">${state.questionBankImports.map(item => `<div class="import-history-row"><div><strong>${esc(item.filename)}</strong><small>${esc(item.total_extracted)} extracted · ${esc(item.valid_count)} imported/valid${item.invalid_count ? ` · ${esc(item.invalid_count)} issue(s)` : ""}</small></div><div class="import-history-actions"><span class="badge ${item.upload_status === "Confirmed" ? "badge-green" : item.upload_status === "Preview" ? "badge-amber" : ""}">${esc(item.upload_status)}</span>${item.upload_status === "Preview" ? `<button class="text-button" data-action="resume-question-import" data-id="${esc(item.id)}">Review</button>` : ""}</div></div>`).join("")}</div></section>` : ""}
    <section class="card"><div class="card-heading"><div><h2>Add a question</h2><p>Questions and answer keys are visible only to job managers.</p></div><span class="badge ${summary.ready ? "badge-green" : "badge-amber"}">${summary.ready ? "20+ active questions" : `${summary.additional_needed} questions needed`}</span></div>
      <form id="question-form" class="profile-form">
        <div class="field"><label>Technology</label><input class="input" name="technology" list="question-topics" maxlength="50" required placeholder="e.g. Python" /><datalist id="question-topics">${counts.map(topic => `<option value="${esc(topic.technology)}"></option>`).join("")}</datalist></div>
        <div class="field"><label>Difficulty</label><select class="select" name="difficulty"><option>Easy</option><option>Medium</option><option>Hard</option></select></div>
        <div class="field span-2"><label>Question</label><textarea class="textarea" name="question" minlength="10" maxlength="1000" required></textarea></div>
        ${[0, 1, 2, 3].map(index => `<div class="field"><label>Option ${index + 1}</label><input class="input" name="option_${index}" required /></div>`).join("")}
        <div class="field"><label>Correct option</label><select class="select" name="correct_index">${[0, 1, 2, 3].map(index => `<option value="${index}">Option ${index + 1}</option>`).join("")}</select></div>
        <div class="span-2"><button class="btn btn-primary" type="submit">Add question →</button></div>
      </form>
    </section>
    <section class="card" style="margin-top:16px"><div class="card-heading"><div><h2>Questions</h2><p>${state.questionBankQuestions.length} total questions · ${summary.active_count || 0} active</p></div></div>
      ${state.questionBankQuestions.length ? `<div class="question-list">${state.questionBankQuestions.map(question => `<article class="question-item"><div><span class="eyebrow">${esc(question.technology)} · ${esc(question.difficulty)} · ${question.active ? "Active" : "Inactive"}</span><strong>${esc(question.question)}</strong><small>${question.options.map((option, index) => `${index === question.correct_index ? "✓ " : ""}${esc(option)}`).join(" · ")}</small></div><div class="question-actions"><button class="text-button" data-action="edit-question" data-id="${esc(question.id)}">Edit</button><button class="text-button" data-action="toggle-question" data-id="${esc(question.id)}">${question.active ? "Deactivate" : "Activate"}</button><button class="text-button danger-text" data-action="delete-question" data-id="${esc(question.id)}">Delete</button></div></article>`).join("")}</div>` : empty("No questions yet", "Add job-specific questions above to make this assessment bank ready.", "☷")}
    </section>`;
}
function profilePage() {
  const resume = state.resume;
  const parsed = { ...(resume?.parsed_profile || {}), ...(state.user.profile || {}) };
  return `${heading("Put your best foot forward", "My profile", "Your experience, on your terms. Review and update it whenever you like.")}
    <div class="profile-grid">
      <section class="card"><div class="card-heading"><div><h2>Your details</h2><p>Help employers understand what you bring</p></div></div>
        <form id="profile-form" class="profile-form">
          <div class="field"><label>Full name</label><input class="input" name="name" value="${esc(state.user.name)}" required /></div>
          <div class="field"><label>Email address</label><input class="input" type="email" value="${esc(state.user.email)}" disabled /></div>
          <div class="field"><label>Phone</label><input class="input" name="phone" value="${esc(parsed.phone || "")}" placeholder="Add a contact number" /></div>
          <div class="field"><label>Location</label><input class="input" name="location" value="${esc(parsed.location || "")}" placeholder="City, country" /></div>
          <div class="field span-2"><label>Skills</label><input class="input" name="skills" value="${esc((parsed.skills || []).join(", "))}" placeholder="e.g. React, product design, SQL" /></div>
          <div class="field span-2"><label>About you</label><textarea class="textarea" name="summary" placeholder="A short introduction about your experience and what you're looking for.">${esc(parsed.summary || "")}</textarea></div>
          <div class="span-2"><button class="btn btn-primary" type="submit">Save changes</button></div>
        </form>
      </section>
      <aside class="card"><div class="card-heading"><div><h2>Your resume</h2><p>PDF or image, up to 10 MB</p></div></div>
        <form id="resume-form" class="resume-panel">
          <label class="upload-drop" for="resume-file"><span class="upload-mark">↑</span><strong>${resume ? esc(resume.filename) : "Upload your resume"}</strong><p>${resume ? `Last updated ${dateLabel(resume.uploaded_at)}` : "PDF, JPG, JPEG, or PNG · Max 10 MB"}</p><span class="btn btn-quiet btn-small">Choose a file</span><input id="resume-file" name="file" type="file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png" hidden /></label>
        </form>
        ${resume ? `<div style="margin-top:16px"><span class="badge ${resume.processing_status === "completed" ? "badge-green" : "badge-amber"}">${esc(resume.processing_status)}</span>${resume.processing_error ? `<p style="margin:9px 0 0;color:#a33d40;font-size:10px">${esc(resume.processing_error)}</p>` : ""}</div>
        <div style="margin-top:15px"><div class="card-heading"><div><h2 style="font-size:12px">Extracted details</h2></div></div>
          <div class="match-score"><div><strong>Name</strong><p>${esc(parsed.name || "Not provided")}</p></div></div>
          <div class="match-score"><div><strong>Email</strong><p>${esc(parsed.email || "Not provided")}</p></div></div>
          <div class="match-score"><div><strong>Phone</strong><p>${esc(parsed.phone || "Not provided")}</p></div></div>
          <div class="skills">${(parsed.skills || []).map(skill => `<span class="skill">${esc(skill)}</span>`).join("") || `<span class="skill">No skills detected</span>`}</div>
        </div>` : ""}
      </aside>
    </div>`;
}

function companyProfilePage() {
  const profile = state.user.profile || {};
  return `${heading("A little more about your team", "Company profile", "Share a few details to help candidates get to know you.")}
    <section class="card"><div class="card-heading"><div><h2>Company details</h2><p>These details make your job listings feel more personal.</p></div></div>
      <form id="company-form" class="profile-form">
        <div class="field"><label>Company name</label><input class="input" name="company" value="${esc(profile.company || "")}" placeholder="Your company" required /></div>
        <div class="field"><label>Contact name</label><input class="input" name="name" value="${esc(state.user.name)}" required /></div>
        <div class="field span-2"><label>About your team</label><textarea class="textarea" name="about" placeholder="What are you building? What makes your team a great place to grow?">${esc(profile.about || "")}</textarea></div>
        <div class="field"><label>Website</label><input class="input" name="website" type="url" value="${esc(profile.website || "")}" placeholder="https://example.com" /></div>
        <div class="field"><label>Location</label><input class="input" name="location" value="${esc(profile.location || "")}" placeholder="City, country or remote" /></div>
        <div class="span-2"><button class="btn btn-primary" type="submit">Save company details</button></div>
      </form>
    </section>`;
}

function recruiterDashboard() {
  const ownJobs = state.jobs;
  const applicants = state.applications;
  return `${heading("Your hiring workspace", `Welcome, ${esc(state.user.name.split(" ")[0])}`, "Build your team with people who are ready to make a difference.", `<button class="btn btn-primary" data-action="new-job">＋ Post a role</button>`)}
    <section class="grid stats-grid">${stat("Open roles", ownJobs.length, "Roles published by your team", "▣")}${stat("Applicants", applicants.length, "People interested in your roles", "♙")}${stat("In review", applicants.filter(row => row.status === "Under Review").length, "Applications to look at", "◷")}${stat("Shortlisted", applicants.filter(row => row.status === "Shortlisted").length, "Strong profiles to revisit", "✳")}</section>
    <section class="columns"><article class="card"><div class="card-heading"><div><h2>Your open roles</h2><p>Manage the opportunities you’ve shared</p></div><button class="text-button" data-page="My jobs">Manage →</button></div><div class="job-list">${ownJobs.slice(0, 4).map(job => jobRow(job, "Applicants")).join("") || empty("No roles posted", "Create your first role to meet great candidates.")}</div></article>
      <article class="card"><div class="card-heading"><div><h2>Hiring snapshot</h2><p>A pulse check on your team’s pipeline</p></div></div><div class="application-list">${applicants.slice(0, 4).map(item => `<div class="application-row"><div><strong>${esc(item.candidate_name)}</strong><small>${esc(item.title)}</small></div><span class="badge badge-red">${esc(item.status)}</span></div>`).join("") || empty("Your applicant list is quiet", "New applicants will show up here.", "♙")}</div></article>
    </section>`;
}

function recruiterJobsPage() {
  return `${heading("Manage your openings", "My jobs", "Create, update, and keep track of the roles you’ve posted.", `<button class="btn btn-primary" data-action="new-job">＋ Post a role</button>`)}
    <section class="card"><div class="card-heading"><div><h2>Published roles</h2><p>${state.jobs.length} open opportunit${state.jobs.length === 1 ? "y" : "ies"}</p></div></div>
    <div class="job-list">${state.jobs.length ? state.jobs.map(job => jobRow(job, "Applicants")).join("") : empty("Nothing posted yet", "Post a role and find the right person for your team.")}</div></section>`;
}

function applicantsPage() {
  const rows = state.applications;
  return `${heading("Meet the people behind the profiles", "Applicants", "Review candidates and keep your hiring process moving.")}
    <section class="card"><div class="card-heading"><div><h2>Applications to review</h2><p>${rows.length} candidate${rows.length === 1 ? "" : "s"} across your open roles</p></div><button class="btn btn-quiet btn-small" data-page="Interviews">View interviews</button></div>
    ${rows.length ? `<datalist id="assessment-topics">${["Python", "Java", "JavaScript/React", "SQL"].map(value => `<option value="${esc(value)}"></option>`).join("")}</datalist><div class="table-wrap"><table><thead><tr><th>Candidate</th><th>Role</th><th>Match</th><th>Applied</th><th>Resume</th><th>Status</th><th>Assessment</th><th>Topic</th><th>Email sent</th><th>Score</th><th>Completed</th><th>Actions</th></tr></thead><tbody>${rows.map(item => {
      const eligible = Number(item.match_percentage) >= 70;
      const assessmentStatus = item.assessment_status || (eligible ? "Not Sent" : "Not eligible");
      const assessmentAction = !eligible ? "—"
        : assessmentStatus === "Not Sent"
          ? `<button class="text-button" data-action="send-assessment" data-id="${esc(item.id)}">Send assessment</button>`
          : ["Sent", "Expired"].includes(assessmentStatus)
            ? `<button class="text-button" data-action="reissue-assessment" data-id="${esc(item.id)}">Re-send link</button>`
            : assessmentStatus === "Started" ? "In progress"
              : assessmentStatus === "Completed" ? "Completed"
                : assessmentStatus === "Manual Topic Required" ? "Choose topic"
                  : assessmentStatus === "Expired" ? "Expired"
                    : "—";
      const topic = assessmentStatus === "Manual Topic Required"
        ? `<input class="status-select assessment-topic" list="assessment-topics" data-assessment-topic="${esc(item.id)}" aria-label="Choose assessment topic for ${esc(item.candidate_name)}" placeholder="Choose topic" />`
        : esc(item.assessment_topic || "—");
      const currentInterview = item.interview;
      const canSchedule = !["Rejected", "Withdrawn", "Accepted", "Selected"].includes(item.status);
      const interviewAction = !canSchedule ? ""
        : currentInterview && ["Scheduled", "Rescheduled"].includes(currentInterview.status)
          ? `<button class="text-button" data-action="edit-interview" data-id="${esc(currentInterview.id)}">Reschedule Interview</button>`
          : `<button class="text-button" data-action="schedule-interview" data-id="${esc(item.id)}">Schedule Interview</button>`;
      return `<tr><td><strong>${esc(item.candidate_name)}</strong><small>${esc(item.candidate_email)}</small></td><td>${esc(item.title)}</td><td><strong>${item.match_percentage == null ? "—" : `${esc(item.match_percentage)}%`}</strong><details class="skill-details"><summary>Skills</summary><small>Matched: ${esc((item.matched_skills || []).join(", ") || "None")}</small><small>Missing: ${esc((item.missing_skills || []).join(", ") || "None")}</small></details></td><td>${dateLabel(item.applied_at)}</td><td><button class="text-button" data-action="download-resume" data-id="${esc(item.id)}">Open resume ↗</button></td><td><select class="status-select" data-status="${esc(item.id)}" aria-label="Update status for ${esc(item.candidate_name)}">${statusOptions(item.status).map(status => `<option ${status === (item.status === "Selected" ? "Accepted" : item.status) ? "selected" : ""}>${esc(status)}</option>`).join("")}</select></td><td>${assessmentAction}</td><td>${topic}</td><td>${item.assessment_email_sent_at ? dateLabel(item.assessment_email_sent_at) : "—"}</td><td>${item.assessment_score == null ? "—" : `${esc(item.assessment_score)}/20`}${item.assessment_result ? `<small>${esc(item.assessment_score_percentage)}% · ${esc(item.assessment_result)}</small>` : ""}</td><td>${item.assessment_submitted_at ? dateLabel(item.assessment_submitted_at) : "—"}</td><td><div class="inline-actions"><button class="text-button" data-action="application-history" data-id="${esc(item.id)}">History</button><button class="text-button" data-action="edit-note" data-id="${esc(item.id)}">Notes</button>${item.assessment_id && assessmentStatus === "Completed" ? `<button class="text-button" data-action="review-assessment" data-id="${esc(item.assessment_id)}">Review test</button>` : ""}${interviewAction}</div></td></tr>`;
    }).join("")}</tbody></table></div>` : empty("No applicants yet", "Candidate applications for your jobs will appear here.", "♙")}
    </section>`;
}

function interviewListPage() {
  const rows = state.interviews;
  const canManage = ["recruiter", "admin"].includes(state.user.role);
  return `${heading("Meet face to face", "Interviews", "Review interview invitations and join scheduled meetings.", state.user.role === "recruiter" ? `<button class="btn btn-primary" data-page="Applicants">＋ Schedule interview</button>` : "")}
    <section class="interview-list">${rows.map(item => `<article class="card interview-card">
      <div><span class="eyebrow">${esc(item.company)}</span><h2>${esc(item.title)}</h2><p>${canManage ? `Candidate: ${esc(item.candidate_name)}` : `Interviewer: ${esc(item.recruiter_name)}`}</p><p>${dateTimeLabel(item.scheduled_at)} · ${canManage ? esc(item.timezone) : `Your local time (${esc(localTimeZone())})`} · ${esc(item.duration_minutes)} minutes</p><p><span class="badge ${item.status === "Cancelled" ? "badge-red" : item.status === "Completed" ? "badge-green" : "badge-amber"}">${canManage ? esc(item.status) : interviewDisplayStatus(item)}</span> · ${esc(item.meeting_platform)}</p>${item.recruiter_message ? `<p>${esc(item.recruiter_message)}</p>` : ""}${canManage ? `<p>Invitation viewed: ${item.candidate_viewed_at ? dateTimeLabel(item.candidate_viewed_at) : "Not yet"}</p>` : ""}</div>
      <div class="interview-actions">${interviewDisplayStatus(item) === "Interview Scheduled" && /^https:\/\//i.test(item.meeting_url || "") ? `<a class="btn btn-primary btn-small" href="${esc(item.meeting_url)}" target="_blank" rel="noopener noreferrer">Join Interview ↗</a>` : ""}${state.user.role !== "admin" ? `<button class="btn btn-quiet btn-small" data-action="open-interview" data-id="${esc(item.id)}">Open coding room</button>` : ""}${canManage && ["Scheduled", "Rescheduled"].includes(item.status) ? `<button class="btn btn-quiet btn-small" data-action="edit-interview" data-id="${esc(item.id)}">Reschedule Interview</button><button class="text-button" data-action="cancel-interview" data-id="${esc(item.id)}">Cancel</button><button class="text-button" data-action="complete-interview" data-id="${esc(item.id)}">Mark completed</button>` : ""}</div>
    </article>`).join("") || empty("No interviews scheduled", canManage ? "Choose an applicant and schedule an interview to get started." : "Your recruiter will share interview details here.", "◷")}</section>`;
}

function interviewRoomPage() {
  const interview = state.currentInterview;
  if (!interview) return `<div class="loading"><span class="spinner"></span></div>`;
  const editable = ["candidate", "recruiter"].includes(state.user.role);
  return `${heading("Shared interview workspace", `${esc(interview.candidate_name)} · ${esc(interview.title)}`, `${new Date(interview.scheduled_at).toLocaleString()} · Changes sync to the interviewer while you work.`)}
    <section class="card interview-room">
      <div class="interview-room-toolbar"><div><strong>Live code editor</strong><small>Saved privately to this interview on SkillSphere</small></div>
      <div class="interview-actions">${/^https:\/\//i.test(interview.meeting_url || "") && !["Cancelled", "Completed"].includes(interview.status) ? `<a class="btn btn-quiet btn-small" href="${esc(interview.meeting_url)}" target="_blank" rel="noopener noreferrer">Join Interview ↗</a>` : ""}<label class="sr-only" for="editor-language">Language</label><select class="select editor-language" id="editor-language" ${editable ? "" : "disabled"}>${["python", "javascript", "typescript", "java", "cpp"].map(language => `<option value="${language}" ${interview.language === language ? "selected" : ""}>${language}</option>`).join("")}</select></div></div>
      <textarea class="code-editor" id="interview-code" spellcheck="false" aria-label="Shared interview code editor" ${editable ? "" : "readonly"}>${esc(interview.code)}</textarea>
      <div class="editor-footer"><span id="editor-sync-status">Saved</span><span>Code execution is disabled</span></div>
    </section>`;
}

function proctoringEventTimeline(events) {
  return events.length
    ? `<ol class="timeline">${events.map(item => `<li><span class="timeline-marker"></span><div><strong>${esc(item.event_type.replace(/_/g, " "))} · ${esc(item.severity)}</strong><small>${dateTimeLabel(item.created_at)}${item.duration_seconds == null ? "" : ` · ${esc(item.duration_seconds)} seconds`}</small></div></li>`).join("")}</ol>`
    : `<p class="stat-note">No monitoring events recorded.</p>`;
}

function adminProctoringPanel() {
  const filters = state.proctoringFilters;
  const items = state.proctoringItems;
  return `<section class="card admin-proctoring"><div class="card-heading"><div><h2>Assessment integrity</h2><p>Review browser and face-presence events. Flags are review signals, not proof of misconduct.</p></div><span class="badge ${items.some(item => item.flagged) ? "badge-amber" : "badge-green"}">${items.filter(item => item.flagged).length} flagged</span></div>
    <form id="admin-proctoring-filter-form" class="filters proctoring-filters">
      <input class="input" name="candidate" value="${esc(filters.candidate)}" placeholder="Candidate name or email" aria-label="Filter by candidate" />
      <select class="select" name="job_id" aria-label="Filter by job"><option value="">All jobs</option>${state.adminJobs.map(job => `<option value="${esc(job.id)}" ${String(filters.job_id) === String(job.id) ? "selected" : ""}>${esc(job.title)}</option>`).join("")}</select>
      <input class="input" type="date" name="start_date" value="${esc(filters.start_date)}" aria-label="Start date" />
      <input class="input" type="date" name="end_date" value="${esc(filters.end_date)}" aria-label="End date" />
      <select class="select" name="flagged_only" aria-label="Filter by flag status"><option value="">All records</option><option value="true" ${filters.flagged_only === "true" ? "selected" : ""}>Flagged only</option><option value="false" ${filters.flagged_only === "false" ? "selected" : ""}>Not flagged</option></select>
      <button class="btn btn-quiet btn-small" type="submit">Apply filters</button>
    </form>
    ${items.length ? `<div class="table-wrap"><table><thead><tr><th>Candidate</th><th>Job and recruiter</th><th>Started</th><th>Warnings</th><th>Review status</th><th>Timeline</th></tr></thead><tbody>${items.map(item => `<tr>
      <td><strong>${esc(item.candidate_name)}</strong><small>${esc(item.candidate_email)}</small></td>
      <td>${esc(item.title)}<small>${esc(item.company)} · ${esc(item.recruiter_name)}</small></td>
      <td>${dateTimeLabel(item.proctoring_started_at)}</td>
      <td>${esc(item.proctoring_warning_count)}</td>
      <td><span class="badge ${item.flagged ? "badge-amber" : "badge-green"}">${item.flagged ? "Flagged for review" : esc(item.status)}</span></td>
      <td><details class="proctoring-timeline"><summary>${item.events.length} event${item.events.length === 1 ? "" : "s"}</summary>${proctoringEventTimeline(item.events)}</details></td>
    </tr>`).join("")}</tbody></table></div>` : empty("No proctoring records found", "Assessment monitoring events will appear here when available.")}
  </section>`;
}

function adminDashboard() {
  const data = state.analytics || {};
  const weekly = data.weekly || {};
  const peak = Math.max(1, ...(weekly.daily_trend || []).map(day => day.applications));
  return `${heading("Platform overview", "The whole picture", "A live snapshot of the SkillSphere community.")}
    <section class="grid stats-grid">${stat("People", data.users ?? "—", "Registered members", "♙")}${stat("Recruiters", data.recruiters ?? "—", "Teams hiring on SkillSphere", "◉")}${stat("Open roles", data.open_jobs ?? "—", "Published opportunities", "▣")}${stat("Applications", data.applications ?? "—", "Connections in motion", "↗")}</section>
    <section class="card weekly-report"><div class="card-heading"><div><h2>Weekly hiring report</h2><p>${esc(weekly.start_date || "")} – ${esc(weekly.end_date || "")}</p></div><div class="report-range">
      <label class="sr-only" for="analytics-range">Report period</label><select class="select" id="analytics-range"><option value="this_week" ${state.analyticsRange === "this_week" ? "selected" : ""}>This week</option><option value="last_week" ${state.analyticsRange === "last_week" ? "selected" : ""}>Last week</option><option value="custom" ${state.analyticsRange === "custom" ? "selected" : ""}>Custom</option></select>
      ${state.analyticsRange === "custom" ? `<input class="input report-date" id="analytics-start" type="date" value="${esc(state.analyticsStart)}" aria-label="Report start date" /><input class="input report-date" id="analytics-end" type="date" value="${esc(state.analyticsEnd)}" aria-label="Report end date" /><button class="btn btn-quiet btn-small" data-action="apply-custom-report">Apply</button>` : ""}
    </div></div>
      <div class="grid stats-grid report-stats">${stat("Applications", weekly.applications ?? 0, "In selected period", "↗")}${stat("Assessments sent", weekly.assessments_sent ?? 0, "Email delivery count", "✉")}${stat("Completion rate", `${weekly.completion_rate ?? 0}%`, "Of assessments sent in period", "✓")}${stat("Average score", weekly.average_score_percentage == null ? "—" : `${weekly.average_score_percentage}%`, "Completed assessments", "◉")}</div>
      <h3 class="report-subhead">Round 1 results</h3>
      <div class="grid stats-grid report-stats">${stat("Completed", weekly.round1_assessments_completed ?? 0, "Round 1 this period", "✓")}${stat("Passed", weekly.round1_passed ?? 0, "Candidates cleared Round 1", "↑")}${stat("Failed", weekly.round1_failed ?? 0, "Candidates below threshold", "↓")}${stat("Pass rate", `${weekly.round1_pass_rate ?? 0}%`, "Of completed Round 1 tests", "◉")}${stat("Average Round 1 score", weekly.round1_average_score_percentage == null ? "—" : `${weekly.round1_average_score_percentage}%`, "Score percentage", "★")}</div>
      <div class="columns report-columns"><div><h3 class="report-subhead">Applications by day</h3><div class="trend-chart">${(weekly.daily_trend || []).map(day => `<div class="trend-day"><span class="trend-value">${day.applications}</span><div class="trend-bar" style="height:${Math.max(4, day.applications / peak * 100)}%"></div><small>${new Date(`${day.date}T00:00:00`).toLocaleDateString(undefined, { weekday: "short" })}</small></div>`).join("") || empty("No data", "Applications appear here over time.")}</div></div>
      <div class="report-breakdown"><h3 class="report-subhead">Pipeline statuses</h3>${Object.entries(weekly.status_counts || {}).map(([status, count]) => `<div class="breakdown-row"><span>${esc(status)}</span><strong>${esc(count)}</strong></div>`).join("") || `<p class="stat-note">No applications in this period.</p>`}<h3 class="report-subhead">Assessment activity</h3>${Object.entries(weekly.assessments || {}).map(([status, count]) => `<div class="breakdown-row"><span>${esc(status)}</span><strong>${esc(count)}</strong></div>`).join("") || `<p class="stat-note">No assessments created in this period.</p>`}</div></div>
      <div class="columns report-columns report-top-lists"><div><h3 class="report-subhead">Top jobs</h3>${(weekly.top_jobs || []).map(job => `<div class="breakdown-row"><span>${esc(job.title)} · ${esc(job.company)}</span><strong>${esc(job.applications)}</strong></div>`).join("") || `<p class="stat-note">No applications in this period.</p>`}</div><div><h3 class="report-subhead">Top technologies</h3>${(weekly.top_technologies || []).map(topic => `<div class="breakdown-row"><span>${esc(topic.technology)}</span><strong>${esc(topic.assessments)}</strong></div>`).join("") || `<p class="stat-note">No assessments in this period.</p>`}</div></div>
      <div class="columns report-columns report-top-lists"><div><h3 class="report-subhead">Round 1 passed by job</h3>${(weekly.round1_passed_by_job || []).map(job => `<div class="breakdown-row"><span>${esc(job.title)} · ${esc(job.company)}</span><strong>${esc(job.candidates)}</strong></div>`).join("") || `<p class="stat-note">No Round 1 passes in this period.</p>`}</div><div><h3 class="report-subhead">Round 1 failed by job</h3>${(weekly.round1_failed_by_job || []).map(job => `<div class="breakdown-row"><span>${esc(job.title)} · ${esc(job.company)}</span><strong>${esc(job.candidates)}</strong></div>`).join("") || `<p class="stat-note">No Round 1 failures in this period.</p>`}</div><div class="report-breakdown"><h3 class="report-subhead">Round 1 pass/fail distribution</h3>${Object.entries(weekly.round1_pass_fail_distribution || {}).map(([result, count]) => `<div class="breakdown-row"><span>${esc(result)}</span><strong>${esc(count)}</strong></div>`).join("")}</div></div>
    </section>
    ${adminProctoringPanel()}
    <section class="card"><div class="card-heading"><div><h2>Platform health</h2><p>Everything you need to keep the community moving</p></div><span class="badge badge-green">All systems running</span></div>
      <div class="grid stats-grid" style="margin:0">${stat("Candidates", data.candidates ?? "—", "Job-seeker accounts", "♙")}${stat("Total jobs", data.jobs ?? "—", "Across all employers", "▣")}${stat("Resumes", data.resumes ?? "—", "Profiles with resumes", "▤")}${stat("In review", data.in_review ?? "—", "Applications awaiting updates", "◷")}</div>
    </section>
    <section class="card" style="margin-top:16px"><div class="card-heading"><div><h2>Recent applications</h2><p>Activity across the platform</p></div></div><div class="application-list">${(data.recent_applications || []).map(item => `<div class="application-row"><div><strong>${esc(item.candidate_name)} · ${esc(item.title)}</strong><small>${esc(item.company)} · ${dateLabel(item.applied_at)}</small></div><span class="badge badge-red">${esc(item.status)}</span></div>`).join("") || empty("No activity to show", "Applications will appear here as people find their fit.")}</div></section>`;
}

function adminUsersPage() {
  const users = state.analytics?.users_list || [];
  return `${heading("Community management", "People on SkillSphere", "Review the members, employers, and teams in the community.")}
    <section class="card"><div class="card-heading"><div><h2>Registered members</h2><p>${users.length} accounts</p></div></div>
      ${users.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Joined</th><th>Account</th></tr></thead><tbody>${users.map(user => `<tr><td><strong>${esc(user.name)}</strong></td><td>${esc(user.email)}</td><td><span class="badge">${esc(user.role)}</span></td><td>${dateLabel(user.created_at)}</td><td>${user.id === state.user.id ? `<span class="badge badge-green">Your account</span>` : `<select class="status-select" data-user-status="${esc(user.id)}" aria-label="Update account for ${esc(user.name)}"><option value="true" ${user.active ? "selected" : ""}>Active</option><option value="false" ${user.active ? "" : "selected"}>Disabled</option></select>`}</td></tr>`).join("")}</tbody></table></div>` : empty("No members found", "There are no accounts to display.")}
    </section>`;
}

function adminJobListingsPage() {
  const jobs = state.adminJobs || [];
  return `${heading("Keep opportunities thoughtful", "Job listings", "Review open roles and close listings that are no longer active.")}
    <section class="card"><div class="card-heading"><div><h2>All listings</h2><p>${jobs.length} total listing${jobs.length === 1 ? "" : "s"}</p></div></div>
      ${jobs.length ? `<div class="table-wrap"><table><thead><tr><th>Role</th><th>Company</th><th>Location</th><th>Posted</th><th>Status</th><th>Question bank</th></tr></thead><tbody>${jobs.map(job => `<tr><td><strong>${esc(job.title)}</strong></td><td>${esc(job.company)}</td><td>${esc(job.location)}</td><td>${dateLabel(job.created_at)}</td><td><select class="status-select" data-job-status="${esc(job.id)}" aria-label="Update listing for ${esc(job.title)}"><option ${job.status === "Open" ? "selected" : ""}>Open</option><option ${job.status === "Closed" ? "selected" : ""}>Closed</option></select></td><td><button class="text-button" data-action="manage-question-bank" data-id="${esc(job.id)}">Manage</button></td></tr>`).join("")}</tbody></table></div>` : empty("No listings found", "New opportunities will appear here.")}
    </section>`;
}

function currentContent() {
  if (state.user.role === "recruiter") {
    if (state.page === "My jobs") return recruiterJobsPage();
    if (state.page === "Applicants") return applicantsPage();
    if (state.page === "Assessments") return assessmentsPage();
    if (state.page === "Question bank") return questionBankPage();
    if (state.page === "Interviews") return interviewListPage();
    if (state.page === "Interview room") return interviewRoomPage();
    if (state.page === "Company profile") return companyProfilePage();
    return recruiterDashboard();
  }
  if (state.user.role === "admin") {
    if (state.page === "Users") return adminUsersPage();
    if (state.page === "Job listings") return adminJobListingsPage();
    if (state.page === "Assessments") return assessmentsPage();
    if (state.page === "Question bank") return questionBankPage();
    if (state.page === "Interviews") return interviewListPage();
    return adminDashboard();
  }
  if (state.page === "Find jobs") return jobsPage();
  if (state.page === "Applications") return applicationsPage();
  if (state.page === "Assessments") return assessmentsPage();
  if (state.page === "Interviews") return interviewListPage();
  if (state.page === "Interview room") return interviewRoomPage();
  if (state.page === "My profile") return profilePage();
  return candidateDashboard();
}

function renderModal() {
  if (state.modal === "new-job") return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <div class="modal-head"><div><span class="eyebrow">Grow your team</span><h2 id="modal-title">Post a new role</h2><p>Share the details that help the right people find you.</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
    <form id="job-form" class="profile-form"><div class="field span-2"><label>Role title</label><input class="input" name="title" placeholder="e.g. Product Designer" required /></div>
    <div class="field"><label>Company</label><input class="input" name="company" placeholder="Your company" required /></div><div class="field"><label>Location</label><input class="input" name="location" placeholder="Remote, New York..." required /></div>
    <div class="field"><label>Experience</label><input class="input" name="experience" placeholder="e.g. 2–4 years" required /></div><div class="field"><label>Job type</label><select class="select" name="job_type"><option>Full-time</option><option>Part-time</option><option>Contract</option><option>Remote</option></select></div>
    <div class="field span-2"><label>Salary range</label><input class="input" name="salary" placeholder="e.g. $90k – $120k" /></div>
    <div class="field span-2"><label>Required skills, comma-separated</label><input class="input" name="skills" placeholder="e.g. Figma, UX research, prototyping" required /></div>
    <div class="field span-2"><label>Role description</label><textarea class="textarea" name="description" required placeholder="What will this person do? What makes your team a great place to grow?"></textarea></div>
    <div class="span-2"><button class="btn btn-primary" type="submit">Publish role →</button></div></form>
    </section></div>`;
  if (state.modal?.startsWith("job:")) {
    const job = state.jobs.find(item => String(item.id) === state.modal.slice(4));
    if (!job) return "";
    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><div><span class="eyebrow">${esc(job.company)} · ${esc(job.location)}</span><h2 id="modal-title">${esc(job.title)}</h2><p>${esc(job.experience)} · ${esc(job.job_type)} · ${esc(job.salary || "Salary not listed")}</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
      <p style="color:#706966;font-size:12px;line-height:1.75">${esc(job.description)}</p><div class="skills">${(job.skills || []).map(skill => `<span class="skill">${esc(skill)}</span>`).join("")}</div>
      ${state.user.role === "candidate" ? `<div class="match-score"><div><strong>Ready to apply?</strong><p>${state.resume ? "Your resume is on file and ready to share." : "Upload a resume from your profile before applying."}</p></div><button class="btn btn-primary btn-small" data-action="apply-job" data-id="${esc(job.id)}">Apply now →</button></div>` : ""}
      ${state.user.role === "candidate" ? `<div class="match-score"><div><strong>See how your skills line up</strong><p>Compare your resume with this role’s requirements.</p></div><button class="btn btn-quiet btn-small" data-action="check-match" data-id="${esc(job.id)}">Check match</button></div>${state.match?.job_id === job.id ? `<div class="resume-panel"><div class="card-heading"><div><h2>Skills match</h2><p>${esc(state.match.recommendation)}</p></div><div class="score-ring">${esc(state.match.match_percentage)}%</div></div><strong style="font-size:10px">Matched skills</strong><div class="skills">${state.match.matched_skills.map(skill => `<span class="skill" style="background:#e9f7f0;color:#308366">${esc(skill)}</span>`).join("") || `<span class="skill">No exact matches found</span>`}</div><strong style="display:block;margin-top:12px;font-size:10px">Skills to explore</strong><div class="skills">${state.match.missing_skills.map(skill => `<span class="skill">${esc(skill)}</span>`).join("") || `<span class="skill">You match every listed skill</span>`}</div></div>` : ""}` : ""}
    </section></div>`;
  }
  if (state.modal?.startsWith("interview:")) {
    const applicationId = state.modal.slice(10);
    const application = state.applications.find(item => String(item.id) === applicationId);
    const interview = state.interviews.find(item => String(item.id) === String(state.editInterviewId));
    if (!application && !interview) return "";
    const title = application?.title || interview?.title || "Interview";
    const company = application?.company || interview?.company || "";
    const candidateName = application?.candidate_name || interview?.candidate_name || "";
    const dateTimeValue = interview && new Date(interview.scheduled_at) > new Date()
      ? dateTimeInputInZone(interview.scheduled_at, interview.timezone) : "";
    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><div><span class="eyebrow">Interview setup</span><h2 id="modal-title">${interview ? "Reschedule Interview" : "Schedule Interview"}</h2><p>${esc(candidateName)}${candidateName ? " · " : ""}${esc(title)} · ${esc(company)}</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
      <form id="interview-form" class="profile-form"><input type="hidden" name="application_id" value="${esc(application?.id || interview.application_id)}" />
        <div class="field span-2"><label for="interview-time">Interview date and start time</label><input class="input" id="interview-time" type="datetime-local" name="scheduled_at" min="${currentLocalDateTimeInput()}" value="${dateTimeValue}" required /></div>
        <div class="field"><label for="interview-timezone">Time zone</label><input class="input" id="interview-timezone" name="timezone" value="${esc(interview?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC")}" maxlength="80" required /></div>
        <div class="field"><label for="interview-duration">Duration (minutes)</label><input class="input" id="interview-duration" type="number" name="duration_minutes" min="5" max="480" step="5" value="${esc(interview?.duration_minutes || 60)}" required /></div>
        <div class="field"><label for="interview-platform">Meeting platform</label><select class="select" id="interview-platform" name="meeting_platform">${["Google Meet", "Microsoft Teams", "Zoom", "Other"].map(platform => `<option ${platform === (interview?.meeting_platform || "Google Meet") ? "selected" : ""}>${platform}</option>`).join("")}</select></div>
        <div class="field"><label for="interview-url">Secure meeting URL (HTTPS)</label><input class="input" id="interview-url" type="url" name="meeting_url" value="${esc(interview?.meeting_url || "")}" placeholder="https://..." required /></div>
        <div class="field span-2"><label for="interview-message">Recruiter instructions (optional)</label><textarea class="textarea" id="interview-message" name="recruiter_message" maxlength="2000">${esc(interview?.recruiter_message || "")}</textarea></div>
        <div class="span-2"><button class="btn btn-primary" type="submit">${interview ? "Save interview changes" : "Send interview invitation"}</button></div>
      </form></section></div>`;
  }
  if (state.modal?.startsWith("interview-details:")) {
    const interviewId = state.modal.slice("interview-details:".length);
    const application = state.applications.find(item => String(item.interview?.id) === interviewId);
    if (!application?.interview) return "";
    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><div><span class="eyebrow">${esc(application.company)}</span><h2 id="modal-title">${esc(application.title)}</h2><p>Interview invitation details</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
      ${interviewScheduleDetails(application.interview, false)}
    </section></div>`;
  }
  if (state.modal?.startsWith("question:")) {
    const question = state.questionBankQuestions.find(item => String(item.id) === state.modal.slice(9));
    if (!question) return "";
    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><div><span class="eyebrow">Question bank · ${esc(state.questionBankJob?.title || "")}</span><h2 id="modal-title">Edit question</h2></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
      <form id="question-edit-form" class="profile-form"><input type="hidden" name="question_id" value="${esc(question.id)}" />
        <div class="field"><label>Technology</label><input class="input" name="technology" value="${esc(question.technology)}" required /></div>
        <div class="field"><label>Difficulty</label><select class="select" name="difficulty">${["Easy", "Medium", "Hard"].map(level => `<option ${level === question.difficulty ? "selected" : ""}>${level}</option>`).join("")}</select></div>
        <div class="field span-2"><label>Question</label><textarea class="textarea" name="question" minlength="10" required>${esc(question.question)}</textarea></div>
        ${question.options.map((option, index) => `<div class="field"><label>Option ${index + 1}</label><input class="input" name="option_${index}" value="${esc(option)}" required /></div>`).join("")}
        <div class="field"><label>Correct option</label><select class="select" name="correct_index">${question.options.map((_, index) => `<option value="${index}" ${index === question.correct_index ? "selected" : ""}>Option ${index + 1}</option>`).join("")}</select></div>
        <label class="checkbox-field span-2"><input type="checkbox" name="active" ${question.active ? "checked" : ""} /> Active question</label>
        <div class="span-2"><button class="btn btn-primary" type="submit">Save question</button></div>
      </form></section></div>`;
  }
  if (state.modal?.startsWith("history:")) {
    const application = state.applications.find(item => String(item.id) === state.modal.slice(8));
    const history = state.applicationHistory?.items || [];
    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><div><span class="eyebrow">Application timeline</span><h2 id="modal-title">${esc(application?.title || "Application")}</h2><p>${esc(application?.company || "")}</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
      ${history.length ? `<ol class="timeline">${history.map(item => `<li><span class="timeline-marker"></span><div><strong>${esc(item.status)}</strong><small>${new Date(item.created_at).toLocaleString()} · ${esc(item.actor_role)}</small>${item.note ? `<p>${esc(item.note)}</p>` : ""}</div></li>`).join("")}</ol>` : empty("No history available", "Pipeline updates will show here.")}
    </section></div>`;
  }
  if (state.modal?.startsWith("note:")) {
    const application = state.applications.find(item => String(item.id) === state.modal.slice(5));
    if (!application) return "";
    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><div><span class="eyebrow">Recruiter-only</span><h2 id="modal-title">Internal notes</h2><p>${esc(application.candidate_name)} · ${esc(application.title)}</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
      <form id="application-note-form"><label class="field"><span>Notes for your hiring team</span><textarea class="textarea" name="note" maxlength="4000">${esc(application.internal_notes || "")}</textarea></label><button class="btn btn-primary" type="submit">Save notes</button></form>
    </section></div>`;
  }
  if (state.modal?.startsWith("review:")) {
    const review = state.assessmentReview;
    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal review-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><div><span class="eyebrow">Assessment review</span><h2 id="modal-title">${review ? `${esc(review.candidate_name)} · ${esc(review.title)}` : "Loading assessment…"}</h2><p>${review ? `${esc(review.assessment_name)} · ${esc(review.technology)}` : ""}</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
      ${review ? `<div class="assessment-review-summary"><strong>${esc(review.score_percentage)}% · ${esc(review.result)}</strong><span>${esc(review.correct_answer_count)} correct / ${esc(review.total_questions)} · pass mark ${esc(review.pass_percentage)}%</span><span>Completed ${dateTimeLabel(review.submitted_at)} · ${esc(review.time_taken_seconds)} seconds</span><span>Current pipeline stage: ${esc(review.application_status)}</span></div>` : ""}
      ${state.assessmentProctoringReview ? `<section class="proctoring-review"><h3>Proctoring review${state.assessmentProctoringReview.flagged ? ` · <span class="badge badge-amber">Flagged for review</span>` : ""}</h3><p>${esc(state.assessmentProctoringReview.proctoring_warning_count)} warning(s) recorded. Events are review signals, not proof of misconduct.</p>${proctoringEventTimeline(state.assessmentProctoringReview.events)}</section>` : ""}
      ${review ? `<div class="review-questions">${review.questions.map(question => `<article class="review-question"><strong>${question.order}. ${esc(question.question)}</strong><p class="${question.is_correct ? "answer-correct" : "answer-wrong"}">${question.selected_answer == null ? "No answer" : `Candidate answer: ${esc(question.options[question.selected_answer])}`}</p><small>Correct answer: ${esc(question.options[question.correct_answer])}</small></article>`).join("")}</div>` : `<div class="loading"><span class="spinner"></span></div>`}
    </section></div>`;
  }
  return "";
}

function proctoringListPath() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state.proctoringFilters)) {
    if (value) params.set(key, value);
  }
  const query = params.toString();
  return `/admin/proctoring${query ? `?${query}` : ""}`;
}

function assessmentListPath() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(state.assessmentFilters)) {
    if (value) params.set(key, value);
  }
  const query = params.toString();
  return `/assessments${query ? `?${query}` : ""}`;
}

async function loadAssessments() {
  try {
    state.assessments = (await api(assessmentListPath())).items || [];
    renderWorkspace();
  } catch (error) { showToast(error.message, true); }
}

async function loadProctoring() {
  try {
    state.proctoringItems = (await api(proctoringListPath())).items || [];
    renderWorkspace();
  } catch (error) { showToast(error.message, true); }
}

async function loadWorkspace() {
  if (!state.user) return;
  try {
    const jobsPath = state.user.role === "recruiter" ? "/jobs?mine=true&page_size=100" : "/jobs?page_size=6";
    const [jobData, applicationData, interviewData, upcomingData, assessmentData, notificationData] = await Promise.all([
      api(jobsPath),
      state.user.role === "admin" ? Promise.resolve({ items: [] }) : api("/applications"),
      api("/interviews"),
      state.user.role === "candidate" ? api("/interviews/upcoming") : Promise.resolve({ items: [] }),
      api(assessmentListPath()),
      api("/notifications")
    ]);
    state.jobs = jobData.items || [];
    state.jobTotal = jobData.total ?? state.jobs.length;
    state.applications = applicationData.items || [];
    state.interviews = interviewData.items || [];
    state.upcomingInterviews = upcomingData.items || [];
    state.assessments = assessmentData.items || [];
    state.notifications = notificationData.items || [];
    state.unreadNotifications = notificationData.unread_count || 0;
    if (state.user.role === "candidate") state.resume = await api("/resume");
    if (state.user.role === "admin") {
      const [analytics, adminJobs] = await Promise.all([
        api(`/admin/analytics?report_range=${encodeURIComponent(state.analyticsRange)}`),
        api("/admin/jobs")
      ]);
      state.analytics = analytics;
      state.adminJobs = adminJobs.items || [];
      state.proctoringItems = (await api(proctoringListPath())).items || [];
    }
  } catch (error) {
    showToast(error.message, true);
    if (/token|authentication|authorization/i.test(error.message)) signOut(false);
  }
  renderWorkspace();
}

async function loadQuestionBank(jobId) {
  const jobs = state.user.role === "admin" ? state.adminJobs : state.jobs;
  const sameJob = state.questionBankJob && String(state.questionBankJob.id) === String(jobId);
  state.questionBankJob = jobs.find(job => String(job.id) === String(jobId)) || null;
  state.questionBankSummary = null;
  state.questionBankQuestions = [];
  state.questionBankImports = [];
  if (!sameJob) state.questionImportPreview = null;
  state.page = "Question bank";
  renderWorkspace();
  try {
    const [result, imports] = await Promise.all([
      api(`/jobs/${encodeURIComponent(jobId)}/questions`),
      api(`/jobs/${encodeURIComponent(jobId)}/question-bank/imports`)
    ]);
    state.questionBankQuestions = result.items || [];
    state.questionBankSummary = result;
    state.questionBankImports = imports.items || [];
    renderWorkspace();
  } catch (error) {
    showToast(error.message, true);
    state.questionBankJob = null;
    renderWorkspace();
  }
}

async function uploadQuestionBankPDF(file) {
  if (!file || !state.questionBankJob) return;
  const form = new FormData();
  form.append("file", file);
  state.busy = true;
  renderWorkspace();
  try {
    state.questionImportPreview = await api(
      `/jobs/${encodeURIComponent(state.questionBankJob.id)}/question-bank/imports`,
      { method: "POST", body: form }
    );
    const imports = await api(`/jobs/${encodeURIComponent(state.questionBankJob.id)}/question-bank/imports`);
    state.questionBankImports = imports.items || [];
    showToast("PDF preview ready. Nothing has been added to the bank yet.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.busy = false;
    renderWorkspace();
  }
}

async function confirmQuestionImport(form) {
  if (!state.questionImportPreview || !state.questionBankJob) return;
  const questions = [...form.querySelectorAll("[data-import-question]")]
    .filter(group => group.querySelector('[name="include"]').checked)
    .map(group => ({
      source_index: Number(group.dataset.sourceIndex),
      technology: group.querySelector('[name="technology"]').value.trim(),
      difficulty: group.querySelector('[name="difficulty"]').value,
      question: group.querySelector('[name="question"]').value.trim(),
      options: [0, 1, 2, 3].map(index => group.querySelector(`[name="option_${index}"]`).value.trim()),
      correct_index: Number(group.querySelector('[name="correct_index"]').value),
      include: true
    }));
  if (!questions.length) {
    showToast("Select at least one question to import.", true);
    return;
  }
  state.busy = true;
  renderWorkspace();
  try {
    const result = await api(
      `/jobs/${encodeURIComponent(state.questionBankJob.id)}/question-bank/imports/${encodeURIComponent(state.questionImportPreview.id)}/confirm`,
      { method: "POST", body: JSON.stringify({ questions }) }
    );
    state.questionImportPreview = null;
    await loadQuestionBank(state.questionBankJob.id);
    showToast(`Imported ${result.imported_count} question${result.imported_count === 1 ? "" : "s"}.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.busy = false;
    renderWorkspace();
  }
}

async function loadAnalytics(reportRange = state.analyticsRange, startDate = "", endDate = "") {
  const params = new URLSearchParams({ report_range: reportRange });
  if (reportRange === "custom") {
    params.set("start_date", startDate);
    params.set("end_date", endDate);
  }
  try {
    state.analyticsRange = reportRange;
    state.analyticsStart = startDate;
    state.analyticsEnd = endDate;
    state.analytics = await api(`/admin/analytics?${params}`);
    renderWorkspace();
  } catch (error) { showToast(error.message, true); }
}

function renderWorkspace() {
  if (!state.user) return;
  shell(currentContent());
  // Scroll main content area back to top on page changes (mobile-friendly)
  window.scrollTo({ top: 0, behavior: "instant" });
}

async function signOut(announce = true) {
  disconnectInterviewWebSocket();
  clearTimeout(state.editorSaveTimer);
  state.editorSaveTimer = null;
  state.currentInterview = null;
  state.editorDirty = false;
  saveToken("");
  state.user = null;
  state.busy = false;
  state.jobs = [];
  state.applications = [];
  state.assessmentFilters = {
    result: "", min_score: "", max_score: "", job_id: "", status: "",
    completed_from: "", completed_to: ""
  };
  state.interviews = [];
  state.upcomingInterviews = [];
  state.editInterviewId = null;
  state.resume = null;
  state.analytics = null;
  state.adminJobs = [];
  if (announce) showToast("You’ve signed out. See you soon!");
  renderAuth();
}

async function authenticate(email, password, register, name, role) {
  state.busy = true;
  renderAuth(register);
  try {
    const result = await api(register ? "/auth/register" : "/auth/login", {
      method: "POST",
      body: JSON.stringify(register ? { name, email, password, role } : { email, password })
    });
    state.busy = false;
    saveToken(result.access_token);
    state.user = result.user;
    state.page = "Dashboard";
    await loadWorkspace();
    showToast(register ? "Your SkillSphere account is ready." : "Welcome back!");
  } catch (error) {
    state.busy = false;
    renderAuth(register);
    showToast(error.message, true);
  }
}

async function searchJobs() {
  state.query = document.querySelector("#job-query")?.value.trim() || "";
  state.location = document.querySelector("#job-location")?.value.trim() || "";
  state.jobType = document.querySelector("#job-type")?.value || "";
  state.experience = document.querySelector("#job-experience")?.value.trim() || "";
  state.skill = document.querySelector("#job-skill")?.value.trim() || "";
  state.sortBy = document.querySelector("#job-sort")?.value || "newest";
  state.jobPage = 1;
  await loadJobsPage();
}

async function loadJobsPage() {
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  if (state.location) params.set("location", state.location);
  if (state.jobType) params.set("job_type", state.jobType);
  if (state.experience) params.set("experience", state.experience);
  if (state.skill) params.set("skill", state.skill);
  params.set("sort_by", state.sortBy);
  params.set("page", String(state.jobPage));
  params.set("page_size", "6");
  try {
    const result = await api(`/jobs?${params}`);
    state.jobs = result.items || [];
    state.jobTotal = result.total;
    renderWorkspace();
  } catch (error) { showToast(error.message, true); }
}

async function openJob(id) {
  state.modal = `job:${id}`;
  renderWorkspace();
  try {
    const job = await api(`/jobs/${encodeURIComponent(id)}`);
    const index = state.jobs.findIndex(item => String(item.id) === String(id));
    if (index >= 0) state.jobs[index] = job;
    renderWorkspace();
  } catch (error) { showToast(error.message, true); }
}

async function applyToJob(id) {
  try {
    const result = await api(`/jobs/${encodeURIComponent(id)}/apply`, { method: "POST" });
    state.applications.unshift(result);
    state.modal = "";
    renderWorkspace();
    showToast("Application submitted. Good luck!");
  } catch (error) { showToast(error.message, true); }
}

async function downloadApplicantResume(id) {
  const headers = new Headers({ Authorization: `Bearer ${state.token}` });
  try {
    const response = await fetch(`${API}/applications/${encodeURIComponent(id)}/resume`, { headers });
    if (!response.ok) {
      let payload = {};
      try { payload = await response.json(); } catch { payload = {}; }
      throw new Error(payload.detail || `Resume download failed (${response.status})`);
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || "candidate-resume";
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  } catch (error) { showToast(error.message, true); }
}

async function uploadResume(file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    showToast("Uploading and reviewing your resume…");
    const result = await api("/resume/upload", { method: "POST", body: form });
    state.resume = result;
    state.page = "My profile";
    await loadWorkspace();
    showToast(result.processing_error || "Your resume was uploaded.");
  } catch (error) { showToast(error.message, true); }
}

app.addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target;
  if (form.id === "assessment-filter-form") {
    const values = Object.fromEntries(new FormData(form));
    state.assessmentFilters = {
      result: values.result, min_score: values.min_score, max_score: values.max_score,
      job_id: values.job_id, status: values.status,
      completed_from: values.completed_from, completed_to: values.completed_to
    };
    await loadAssessments();
    return;
  }
  if (form.id === "admin-proctoring-filter-form") {
    const values = Object.fromEntries(new FormData(form));
    state.proctoringFilters = {
      candidate: values.candidate.trim(), job_id: values.job_id,
      start_date: values.start_date, end_date: values.end_date,
      flagged_only: values.flagged_only
    };
    await loadProctoring();
    return;
  }
  if (form.id === "question-bank-import-form") {
    await uploadQuestionBankPDF(form.querySelector('[name="pdf"]').files[0]);
    return;
  }
  if (form.id === "question-import-review-form") {
    await confirmQuestionImport(form);
    return;
  }
  if (form.id === "auth-form") {
    const values = new FormData(form);
    await authenticate(values.get("email"), values.get("password"), app.dataset.register === "true", values.get("name"), values.get("role"));
  }
  if (form.id === "job-form") {
    const values = Object.fromEntries(new FormData(form));
    values.skills = values.skills.split(",").map(skill => skill.trim()).filter(Boolean);
    try {
      await api("/jobs", { method: "POST", body: JSON.stringify(values) });
      state.modal = "";
      await loadWorkspace();
      showToast("Your role is live. Here’s to finding a great fit!");
    } catch (error) { showToast(error.message, true); }
  }
  if (form.id === "question-form" || form.id === "question-edit-form") {
    const values = Object.fromEntries(new FormData(form));
    const payload = {
      technology: values.technology.trim(),
      question: values.question.trim(),
      options: [0, 1, 2, 3].map(index => values[`option_${index}`].trim()),
      correct_index: Number(values.correct_index),
      difficulty: values.difficulty,
      active: form.id === "question-form" || values.active === "on"
    };
    const editing = form.id === "question-edit-form";
    const path = `/jobs/${state.questionBankJob.id}/questions${editing ? `/${encodeURIComponent(values.question_id)}` : ""}`;
    try {
      await api(path, { method: editing ? "PATCH" : "POST", body: JSON.stringify(payload) });
      state.modal = "";
      await loadQuestionBank(state.questionBankJob.id);
      showToast(editing ? "Question updated." : "Question added.");
    } catch (error) { showToast(error.message, true); }
  }
  if (form.id === "application-note-form") {
    const note = new FormData(form).get("note");
    const applicationId = state.modal.slice(5);
    try {
      await api(`/applications/${encodeURIComponent(applicationId)}/notes`, {
        method: "PATCH", body: JSON.stringify({ note })
      });
      const application = state.applications.find(item => String(item.id) === applicationId);
      if (application) application.internal_notes = note;
      state.modal = "";
      renderWorkspace();
      showToast("Internal notes saved.");
    } catch (error) { showToast(error.message, true); }
  }
  if (form.id === "interview-form") {
    const values = Object.fromEntries(new FormData(form));
    const applicationId = values.application_id;
    const interviewId = state.editInterviewId;
    const path = interviewId
      ? `/applications/${encodeURIComponent(applicationId)}/interviews/${encodeURIComponent(interviewId)}`
      : `/applications/${encodeURIComponent(applicationId)}/interviews`;
    try {
      await api(path, {
        method: interviewId ? "PATCH" : "POST",
        body: JSON.stringify({
          scheduled_at: values.scheduled_at,
          timezone: values.timezone.trim(),
          duration_minutes: Number(values.duration_minutes),
          meeting_platform: values.meeting_platform,
          meeting_url: values.meeting_url.trim(),
          recruiter_message: values.recruiter_message.trim()
        })
      });
      state.modal = "";
      state.editInterviewId = null;
      await loadWorkspace();
      showToast(interviewId ? "Interview updated. The candidate has been notified." : "Interview invitation sent.");
    } catch (error) { showToast(error.message, true); }
  }
  if (form.id === "profile-form") {
    const values = Object.fromEntries(new FormData(form));
    values.skills = values.skills.split(",").map(skill => skill.trim()).filter(Boolean);
    try {
      state.user = await api("/profile", { method: "PATCH", body: JSON.stringify({ name: values.name, profile: values }) });
      renderWorkspace();
      showToast("Your profile has been updated.");
    } catch (error) { showToast(error.message, true); }
  }
  if (form.id === "company-form") {
    const values = Object.fromEntries(new FormData(form));
    const { name, ...profile } = values;
    try {
      state.user = await api("/profile", { method: "PATCH", body: JSON.stringify({ name, profile }) });
      renderWorkspace();
      showToast("Your company profile has been updated.");
    } catch (error) { showToast(error.message, true); }
  }
});

app.addEventListener("change", async event => {
  if (state.assessmentMode && event.target.matches("[data-assessment-answer]")) {
    const assessment = state.publicAssessment;
    assessment.answers[event.target.name] = Number(event.target.value);
    document.querySelectorAll(".assessment-option").forEach(option => option.classList.remove("selected"));
    event.target.closest(".assessment-option")?.classList.add("selected");
    const counter = document.querySelector("#assessment-answer-count");
    if (counter) counter.textContent = `Question ${assessment.current + 1} of ${assessment.data.total_questions} · ${Object.keys(assessment.answers).length} answered`;
    return;
  }
  if (event.target.matches("[data-assessment-topic]")) {
    if (!event.target.value) return;
    try {
      await api(`/applications/${encodeURIComponent(event.target.dataset.assessmentTopic)}/assessment/topic`, {
        method: "PATCH", body: JSON.stringify({ technology: event.target.value })
      });
      await loadWorkspace();
      showToast("Assessment topic selected. You can now send the assessment.");
    } catch (error) { showToast(error.message, true); await loadWorkspace(); }
    return;
  }
  if (event.target.id === "resume-file") await uploadResume(event.target.files[0]);
  if (event.target.matches("[data-status]")) {
    try {
      const result = await api(`/applications/${encodeURIComponent(event.target.dataset.status)}/status`, {
        method: "PATCH", body: JSON.stringify({ status: event.target.value })
      });
      const index = state.applications.findIndex(item => String(item.id) === String(result.id));
      if (index >= 0) state.applications[index].status = result.status;
      showToast("Application status updated.");
    } catch (error) { showToast(error.message, true); await loadWorkspace(); }
  }
  if (event.target.matches("[data-assessment-pipeline]")) {
    if (!event.target.value) return;
    try {
      await api(`/applications/${encodeURIComponent(event.target.dataset.assessmentPipeline)}/status`, {
        method: "PATCH", body: JSON.stringify({ status: event.target.value })
      });
      await loadWorkspace();
      showToast(`Candidate moved to ${event.target.value}.`);
    } catch (error) { showToast(error.message, true); await loadWorkspace(); }
  }
  if (event.target.matches("[data-user-status]")) {
    try {
      const user = await api(`/admin/users/${encodeURIComponent(event.target.dataset.userStatus)}/status`, {
        method: "PATCH", body: JSON.stringify({ active: event.target.value === "true" })
      });
      const row = state.analytics.users_list.find(item => String(item.id) === String(user.id));
      if (row) row.active = Number(user.active);
      renderWorkspace();
      showToast(user.active ? "Account enabled." : "Account disabled.");
    } catch (error) { showToast(error.message, true); await loadWorkspace(); }
  }
  if (event.target.id === "analytics-range") {
    if (event.target.value === "custom") {
      state.analyticsRange = "custom";
      renderWorkspace();
    } else await loadAnalytics(event.target.value);
  }
  if (event.target.matches("[data-job-status]")) {
    try {
      const job = await api(`/admin/jobs/${encodeURIComponent(event.target.dataset.jobStatus)}/status`, {
        method: "PATCH", body: JSON.stringify({ status: event.target.value })
      });
      const row = state.adminJobs.find(item => String(item.id) === String(job.id));
      if (row) row.status = job.status;
      renderWorkspace();
      showToast(`Listing ${job.status.toLowerCase()}.`);
    } catch (error) { showToast(error.message, true); await loadWorkspace(); }
  }
  if (event.target.id === "editor-language") {
    state.editorDirty = true;
    clearTimeout(state.editorSaveTimer);
    await saveInterviewWorkspace();
  }
});

function connectInterviewWebSocket(interviewId) {
  disconnectInterviewWebSocket();
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws/interviews/${encodeURIComponent(interviewId)}?token=${encodeURIComponent(state.token)}`;
  try {
    const socket = new WebSocket(wsUrl);
    state.interviewSocket = socket;
    socket.addEventListener("open", () => {
      const status = document.querySelector("#editor-sync-status");
      if (status) status.textContent = "Live · Real-time";
      clearInterval(state.interviewPoll);
      state.interviewPoll = null;
    });
    socket.addEventListener("message", event => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === "sync" || msg.type === "code_change") {
          const editor = document.querySelector("#interview-code");
          const language = document.querySelector("#editor-language");
          if (!state.editorDirty && editor) {
            if (msg.code !== undefined && editor.value !== msg.code) editor.value = msg.code;
            if (msg.language && language && language.value !== msg.language) language.value = msg.language;
            const status = document.querySelector("#editor-sync-status");
            if (status) status.textContent = "Live · Synced";
          }
        }
      } catch {}
    });
    socket.addEventListener("close", () => {
      if (state.page === "Interview room" && !state.interviewPoll) {
        state.interviewPoll = setInterval(refreshInterviewWorkspace, 2000);
      }
    });
    socket.addEventListener("error", () => {
      if (state.page === "Interview room" && !state.interviewPoll) {
        state.interviewPoll = setInterval(refreshInterviewWorkspace, 2000);
      }
    });
  } catch {
    state.interviewPoll = setInterval(refreshInterviewWorkspace, 1500);
  }
}

function disconnectInterviewWebSocket() {
  if (state.interviewSocket) {
    try { state.interviewSocket.close(); } catch {}
    state.interviewSocket = null;
  }
  clearInterval(state.interviewPoll);
  state.interviewPoll = null;
}

async function saveInterviewWorkspace() {
  const editor = document.querySelector("#interview-code");
  const language = document.querySelector("#editor-language");
  if (!editor || !language || !state.currentInterview) return;
  const code = editor.value;
  const selectedLanguage = language.value;
  const status = document.querySelector("#editor-sync-status");

  // Send over real-time WebSocket if connected
  if (state.interviewSocket && state.interviewSocket.readyState === WebSocket.OPEN) {
    try {
      state.interviewSocket.send(JSON.stringify({ type: "code_change", code, language: selectedLanguage }));
      state.editorDirty = false;
      if (status) status.textContent = "Live · Saved";
      return;
    } catch {}
  }

  // Fallback to HTTP PUT
  if (state.editorSaveInFlight) {
    clearTimeout(state.editorSaveTimer);
    state.editorSaveTimer = setTimeout(saveInterviewWorkspace, 150);
    return;
  }
  state.editorSaveInFlight = true;
  if (status) status.textContent = "Saving…";
  try {
    const result = await api(`/interviews/${encodeURIComponent(state.currentInterview.id)}/workspace`, {
      method: "PUT",
      body: JSON.stringify({ code, language: selectedLanguage })
    });
    state.currentInterview = result;
    state.editorDirty = editor.value !== code || language.value !== selectedLanguage;
    if (status) status.textContent = state.editorDirty ? "Unsaved changes" : "Saved";
    if (state.editorDirty) state.editorSaveTimer = setTimeout(saveInterviewWorkspace, 100);
  } catch (error) {
    if (status) status.textContent = "Save failed";
    showToast(error.message, true);
  } finally {
    state.editorSaveInFlight = false;
  }
}

async function refreshInterviewWorkspace() {
  if (state.page !== "Interview room" || !state.currentInterview) return;
  try {
    const latest = await api(`/interviews/${encodeURIComponent(state.currentInterview.id)}`);
    const editor = document.querySelector("#interview-code");
    const language = document.querySelector("#editor-language");
    state.currentInterview = latest;
    if (!state.editorDirty && editor && (editor.value !== latest.code || (language && language.value !== latest.language))) {
      editor.value = latest.code;
      if (language) language.value = latest.language;
      const status = document.querySelector("#editor-sync-status");
      if (status) status.textContent = "Live";
    }
  } catch (error) {
    clearInterval(state.interviewPoll);
    state.interviewPoll = null;
    showToast(error.message, true);
  }
}

app.addEventListener("click", async event => {
  if (state.assessmentMode) {
    const assessmentAction = event.target.closest("[data-assessment-action]")?.dataset.assessmentAction;
    if (assessmentAction) await handleAssessmentAction(assessmentAction);
    return;
  }
  const target = event.target.closest("[data-action],[data-page],[data-job],[data-demo]");
  if (!target) return;
  if (target.dataset.demo) {
    document.querySelector("#auth-email").value = target.dataset.demo;
    document.querySelector("#auth-password").value = demoPassword;
    document.querySelector("#auth-password").focus();
    return;
  }
  if (target.dataset.page) {
    event.preventDefault();
    disconnectInterviewWebSocket();
    state.currentInterview = null;
    state.page = target.dataset.page;
    state.modal = "";
    state.notificationsOpen = false;
    if (state.page === "Question bank") state.questionBankJob = null;
    if (state.page === "Find jobs" && state.user.role === "candidate") {
      state.query = ""; state.location = ""; state.jobType = ""; state.experience = ""; state.skill = ""; state.jobPage = 1;
      try {
        const result = await api("/jobs?page_size=6");
        state.jobs = result.items || [];
        state.jobTotal = result.total;
      } catch (error) { showToast(error.message, true); }
    }
    renderWorkspace();
    return;
  }
  if (target.dataset.job) { await openJob(target.dataset.job); return; }
  if (target.dataset.action === "toggle-auth") { renderAuth(app.dataset.register !== "true"); return; }
  if (target.dataset.action === "logout") { await signOut(); return; }
  if (target.dataset.action === "filter-jobs") { await searchJobs(); return; }
  if (target.dataset.action === "prev-page" || target.dataset.action === "next-page") {
    state.jobPage += target.dataset.action === "next-page" ? 1 : -1;
    await loadJobsPage();
    return;
  }
  if (target.dataset.action === "new-job") { state.modal = "new-job"; renderWorkspace(); return; }
  if (target.dataset.action === "toggle-notifications") {
    state.notificationsOpen = !state.notificationsOpen;
    if (state.notificationsOpen) {
      try {
        const result = await api("/notifications");
        state.notifications = result.items || [];
        state.unreadNotifications = result.unread_count || 0;
      } catch (error) { showToast(error.message, true); }
    }
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "mark-all-read") {
    try { await api("/notifications/read-all", { method: "PATCH" }); await loadWorkspace(); }
    catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "open-notification") {
    try {
      await api(`/notifications/${encodeURIComponent(target.dataset.id)}/read`, {
        method: "PATCH", body: JSON.stringify({ is_read: true })
      });
      state.notificationsOpen = false;
      state.page = state.user.role === "candidate" ? "Applications" : state.user.role === "recruiter" ? "Applicants" : "Dashboard";
      await loadWorkspace();
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "notification-application") {
    try {
      await api(`/notifications/${encodeURIComponent(target.dataset.id)}/read`, {
        method: "PATCH", body: JSON.stringify({ is_read: true })
      });
      state.notificationsOpen = false;
      state.page = "Applications";
      await loadWorkspace();
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "manage-question-bank") {
    await loadQuestionBank(target.dataset.id);
    return;
  }
  if (target.dataset.action === "back-question-banks") {
    state.questionBankJob = null;
    state.questionBankQuestions = [];
    state.questionBankSummary = null;
    state.questionBankImports = [];
    state.questionImportPreview = null;
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "resume-question-import") {
    try {
      state.questionImportPreview = await api(
        `/jobs/${encodeURIComponent(state.questionBankJob.id)}/question-bank/imports/${encodeURIComponent(target.dataset.id)}`
      );
      renderWorkspace();
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "cancel-question-import") {
    if (!state.questionImportPreview || !state.questionBankJob) return;
    state.busy = true;
    renderWorkspace();
    try {
      await api(
        `/jobs/${encodeURIComponent(state.questionBankJob.id)}/question-bank/imports/${encodeURIComponent(state.questionImportPreview.id)}`,
        { method: "DELETE" }
      );
      state.questionImportPreview = null;
      await loadQuestionBank(state.questionBankJob.id);
      showToast("PDF preview cancelled.");
    } catch (error) { showToast(error.message, true); }
    finally { state.busy = false; renderWorkspace(); }
    return;
  }
  if (target.dataset.action === "edit-question") {
    state.modal = `question:${target.dataset.id}`;
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "toggle-question") {
    const question = state.questionBankQuestions.find(item => String(item.id) === String(target.dataset.id));
    if (!question) return;
    try {
      await api(`/jobs/${state.questionBankJob.id}/questions/${question.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          technology: question.technology, question: question.question, options: question.options,
          correct_index: question.correct_index, difficulty: question.difficulty, active: !question.active
        })
      });
      await loadQuestionBank(state.questionBankJob.id);
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "delete-question") {
    if (!window.confirm("Delete this question from the job bank?")) return;
    try {
      await api(`/jobs/${state.questionBankJob.id}/questions/${target.dataset.id}`, { method: "DELETE" });
      await loadQuestionBank(state.questionBankJob.id);
      showToast("Question deleted.");
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "application-history") {
    try {
      state.applicationHistory = await api(`/applications/${encodeURIComponent(target.dataset.id)}/history`);
      state.modal = `history:${target.dataset.id}`;
      renderWorkspace();
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "edit-note") {
    state.modal = `note:${target.dataset.id}`;
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "review-assessment") {
    state.modal = `review:${target.dataset.id}`;
    state.assessmentReview = null;
    state.assessmentProctoringReview = null;
    renderWorkspace();
    try {
      [state.assessmentReview, state.assessmentProctoringReview] = await Promise.all([
        api(`/assessment-reviews/${encodeURIComponent(target.dataset.id)}`),
        api(`/assessment-reviews/${encodeURIComponent(target.dataset.id)}/proctoring`)
      ]);
      renderWorkspace();
    } catch (error) { state.modal = ""; showToast(error.message, true); renderWorkspace(); }
    return;
  }
  if (target.dataset.action === "apply-custom-report") {
    const start = document.querySelector("#analytics-start")?.value || "";
    const end = document.querySelector("#analytics-end")?.value || "";
    if (!start || !end) { showToast("Choose both report dates.", true); return; }
    await loadAnalytics("custom", start, end);
    return;
  }
  if (target.dataset.action === "schedule-interview") {
    state.modal = `interview:${target.dataset.id}`;
    state.editInterviewId = null;
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "edit-interview") {
    const interview = state.interviews.find(item => String(item.id) === String(target.dataset.id));
    if (!interview) return;
    state.editInterviewId = interview.id;
    state.modal = `interview:${interview.application_id}`;
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "view-interview") {
    try {
      await api(`/interviews/${encodeURIComponent(target.dataset.id)}/viewed`, { method: "POST" });
      state.modal = `interview-details:${target.dataset.id}`;
      renderWorkspace();
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "cancel-interview" || target.dataset.action === "complete-interview") {
    const interview = state.interviews.find(item => String(item.id) === String(target.dataset.id));
    if (!interview) return;
    const action = target.dataset.action === "cancel-interview" ? "cancel" : "complete";
    if (!window.confirm(action === "cancel" ? "Cancel this interview invitation?" : "Mark this interview as completed?")) return;
    try {
      await api(`/applications/${encodeURIComponent(interview.application_id)}/interviews/${encodeURIComponent(interview.id)}/${action}`, { method: "POST" });
      await loadWorkspace();
      showToast(action === "cancel" ? "Interview cancelled. The candidate has been notified." : "Interview marked as completed.");
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "send-assessment" || target.dataset.action === "reissue-assessment") {
    const action = target.dataset.action === "send-assessment" ? "send" : "reissue";
    try {
      const result = await api(`/applications/${encodeURIComponent(target.dataset.id)}/assessment/${action}`, { method: "POST" });
      await loadWorkspace();
      showToast(result.delivery === "development"
        ? "Assessment link is available to the candidate in their notifications."
        : "Assessment email sent to the candidate.");
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "open-interview") {
    try {
      state.currentInterview = await api(`/interviews/${encodeURIComponent(target.dataset.id)}`);
      state.page = "Interview room";
      state.editorDirty = false;
      renderWorkspace();
      connectInterviewWebSocket(target.dataset.id);
    } catch (error) { showToast(error.message, true); }
    return;
  }
  if (target.dataset.action === "close-modal") {
    if (target.classList.contains("modal-backdrop") && event.target !== target) return;
    state.modal = "";
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "apply-job") { await applyToJob(target.dataset.id); }
  if (target.dataset.action === "download-resume") { await downloadApplicantResume(target.dataset.id); }
  if (target.dataset.action === "withdraw") {
    try {
      await api(`/applications/${encodeURIComponent(target.dataset.id)}/withdraw`, { method: "PATCH" });
      await loadWorkspace();
      showToast("Application withdrawn.");
    } catch (error) { showToast(error.message, true); }
  }
  if (target.dataset.action === "check-match") {
    try {
      state.match = await api(`/matches/${encodeURIComponent(target.dataset.id)}`);
      renderWorkspace();
    } catch (error) { showToast(error.message, true); }
  }
});

app.addEventListener("input", event => {
  if (event.target.id !== "interview-code") return;
  state.editorDirty = true;
  const status = document.querySelector("#editor-sync-status");
  if (status) status.textContent = "Unsaved changes";
  clearTimeout(state.editorSaveTimer);
  state.editorSaveTimer = setTimeout(() => {
    state.editorSaveTimer = null;
    saveInterviewWorkspace();
  }, 450);
});

app.addEventListener("keydown", event => {
  if (event.target.id === "job-query" && event.key === "Enter") searchJobs();
  if (event.key === "Escape" && state.modal) { state.modal = ""; renderWorkspace(); }
});

async function start() {
  const assessmentToken = new URLSearchParams(window.location.search).get("assessment");
  if (assessmentToken) {
    await loadPublicAssessment(assessmentToken);
    return;
  }
  if (!state.token) { renderAuth(); return; }
  try {
    const result = await api("/me");
    state.user = result;
    await loadWorkspace();
  } catch (error) {
    saveToken("");
    renderAuth();
    showToast(/token|authentication|authorization|expired/i.test(error.message)
      ? "Your session has expired. Please sign in again."
      : `Could not restore your session: ${error.message}`, true);
  }
}

start();

window.addEventListener("pagehide", stopAssessmentProctoring);
