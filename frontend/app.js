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
  busy: false,
  aiGeneratedQuestions: [],
  aiGenerating: false,
  aiSummaryData: null,
  aiSummaryLoading: false,
  aiSummaryAppId: null,
  aiTab: "chat",
  aiSessions: [],
  aiActiveSessionId: null,
  aiMessages: [],
  aiChatBusy: false,
  aiResumeLoaded: false,
  aiResumeName: "",
  mockInterview: null,
  mockInterviewsHistory: [],
  mockInterviewBusy: false,
  mockViewingHistoryItem: null
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
  const fetchOptions = { ...options, headers };
  if (!fetchOptions.method || fetchOptions.method.toUpperCase() === "GET") {
    fetchOptions.cache = "no-store";
  }
  const response = await fetch(`${API}${path}`, fetchOptions);
  let payload = {};
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    if (typeof payload.detail === "string") {
      message = payload.detail;
    } else if (Array.isArray(payload.detail)) {
      message = payload.detail.map(d => {
        const field = Array.isArray(d.loc) ? d.loc[d.loc.length - 1] : "";
        return field ? `${field}: ${d.msg}` : (d.msg || JSON.stringify(d));
      }).join("; ");
    } else if (payload.detail && typeof payload.detail === "object") {
      message = JSON.stringify(payload.detail);
    }
    throw new Error(message);
  }
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
  const proctoring = assessment?.data?.proctoring;
  const monitor = assessment?.proctor;
  if (!proctoring?.required) return "";
  const status = monitor?.status || "Camera is not enabled";
  const isRecording = Boolean(assessment?.recorder?.isRecording || (monitor?.started && monitor?.stream));
  const isViewing = Boolean(assessment?.liveMonitoring?.isViewing);
  const isLiveActive = Boolean(assessment?.liveMonitoring?.connected);

  return `<section class="assessment-proctoring" aria-live="polite">
    <video id="assessment-camera-preview" class="assessment-camera-preview" autoplay muted playsinline ${monitor?.stream ? "" : "hidden"}></video>
    <div>
      <div class="proctoring-header-line">
        <strong>Camera & Proctoring Check</strong>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;">
          ${isRecording ? `<span class="recording-badge"><span class="recording-dot"></span> Camera recording active</span>` : ""}
          ${monitor?.started ? (
            isViewing
              ? `<span class="live-monitoring-badge viewing"><span class="live-dot-pulse"></span> Recruiter viewing live video</span>`
              : isLiveActive
                ? `<span class="live-monitoring-badge active"><span class="live-dot"></span> Live monitoring active</span>`
                : `<span class="live-monitoring-badge ready"><span class="live-dot-ready"></span> Live monitoring ready</span>`
          ) : ""}
        </div>
      </div>
      <p>${esc(status)}</p>
      ${monitor?.started ? `<small>Warnings: ${esc(monitor.warningCount)}/${esc(proctoring.max_warnings)}${monitor.flagged ? " · Flagged for recruiter review" : ""}</small>` : ""}
    </div>
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
    const msg = result.message || "Your assessment has been submitted successfully. The recruiter will review your results and update your application status.";
    content = `<div class="assessment-message">
      <span class="eyebrow">Assessment completed</span>
      <h1>${esc(result.assessment_name || "Assessment Completed")}</h1>
      <p>${esc(result.job_title || "")}${result.assessment_round ? " · " + esc(result.assessment_round) : ""}</p>
      <div class="assessment-status-banner" style="margin:24px 0;padding:16px;background:#f4f6f8;border-radius:8px;border-left:4px solid #4a6ee0;">
        <p style="margin:0;font-size:15px;color:#1e293b;line-height:1.5;">${esc(msg)}</p>
      </div>
      <p style="color:#64748b;font-size:13px;">You may close this page now.</p>
    </div>`;
  } else if (data?.status === "Sent") {
    const required = data.proctoring?.required;
    const cameraReady = Boolean(assessment.proctor?.cameraReady);
    const recConsent = Boolean(assessment.proctor?.recordingConsentGiven);
    const canStart = !required || (cameraReady && recConsent && Boolean(assessment.proctor?.stream));
    content = `<div class="assessment-welcome"><span class="eyebrow">${esc(data.assessment_round)} · ${esc(data.company)}</span><h1>${esc(data.assessment_name)}</h1><p class="assessment-company">${esc(data.job_title)} · ${esc(data.technology)}</p><div class="assessment-facts"><span><strong>Questions</strong>${esc(data.total_questions)} multiple-choice</span><span><strong>Time limit</strong>${esc(data.duration_minutes)} minutes after starting</span><span><strong>Passing score</strong>${esc(data.pass_percentage)}%</span><span><strong>Link expires</strong>${new Date(data.expires_at).toLocaleString()}</span></div><p>Complete this assessment independently. Once started, the timer cannot be paused and the link can be submitted only once.</p>
      ${required ? `<div class="assessment-consent">
        <strong>Proctoring, Video Recording & Live Monitoring Consent</strong>
        <p>This assessment uses real-time webcam proctoring to protect test integrity:</p>
        <ul class="assessment-consent-points">
          <li><strong>Camera access is required</strong> throughout the duration of the assessment.</li>
          <li><strong>Webcam video will be recorded</strong> during the test and securely uploaded for cheating and integrity review.</li>
          <li><strong>Live recruiter monitoring:</strong> Authorized recruiters may view your live webcam stream in real time during the assessment.</li>
          <li><strong>Secure and restricted storage:</strong> Recordings and live sessions are strictly restricted to authorized recruiters and administrators.</li>
          <li><strong>Video scope:</strong> The camera stream may include your face, head orientation, and immediate surroundings.</li>
          <li><strong>Automated checks:</strong> The browser monitors face-presence, tab switching, and window focus.</li>
          <li><strong>Consent required:</strong> The assessment timer will start only after you confirm consent and camera verification succeeds.</li>
        </ul>
        <label class="checkbox-field" style="margin-bottom:12px;cursor:pointer">
          <input type="checkbox" data-recording-consent ${recConsent ? "checked" : ""} />
          <span>I understand and consent to webcam recording and live recruiter monitoring during this assessment.</span>
        </label>
        ${assessment.proctor?.error ? `<p class="assessment-proctor-error" role="alert">${esc(assessment.proctor.error)}</p>` : ""}
        ${assessmentProctoringPanel(assessment)}
        <div style="margin-top:12px">
          <button class="btn btn-quiet" data-assessment-action="enable-proctoring" ${assessment.busy || !recConsent ? "disabled" : ""}>
            ${cameraReady ? "✓ Camera stream active" : "Enable camera and verify stream"}
          </button>
        </div>
      </div>` : ""}
      <button class="btn btn-primary" data-assessment-action="start" ${assessment.busy || !canStart ? "disabled" : ""}>Start assessment →</button></div>`;
  } else if (data?.status === "Started") {
    const remaining = Math.max(0, Math.ceil((new Date(data.expires_at).getTime() - Date.now()) / 1000));
    if (data.proctoring?.required && !assessment.proctor?.cameraReady) {
      content = `<div class="assessment-message"><span class="eyebrow">Assessment in progress</span><h1>Reconnect your camera</h1><p>The timer is running. Continuous camera access and recording are required to proceed.</p>${assessment.proctor?.error ? `<p class="assessment-proctor-error" role="alert">${esc(assessment.proctor.error)}</p>` : ""}${assessmentProctoringPanel(assessment)}<button class="btn btn-primary" data-assessment-action="resume-camera" ${assessment.busy ? "disabled" : ""}>Enable camera and resume</button></div>`;
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
      stopAssessmentRecording();
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
    recorder: {
      mediaRecorder: null, chunks: [], startTime: 0, isRecording: false, mimeType: "video/webm"
    },
    proctor: {
      stream: null, detector: null, video: null, animationFrame: 0, missingTimer: null,
      missingSince: null, missingReported: false, lastFaceCount: 0, cameraReady: false,
      consentGiven: false, recordingConsentGiven: false, started: false, warningCount: 0, flagged: false,
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
    state.publicAssessment.proctor.recordingConsentGiven = Boolean(assessment.proctoring?.recording_consent_given || assessment.proctoring?.consent_given);
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
  if (!resume && !assessment.proctor?.recordingConsentGiven) {
    monitor.error = "Please check the recording consent box before enabling camera access.";
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
      body: JSON.stringify({
        consent_given: true,
        camera_permission_granted: true,
        recording_consent_given: true,
        recording_consent_version: "1.0"
      })
    });
    if (!registration.started && registration.required) {
      throw new Error("Camera consent and permission are required before starting.");
    }
    monitor.consentGiven = true;
    monitor.recordingConsentGiven = true;
    monitor.cameraReady = true;
    monitor.started = Boolean(assessment.data.status === "Started");
    monitor.status = "Camera is active. Real-time proctoring enabled.";
    monitor.error = "";
    if (monitor.started) {
      attachProctoringActivityListeners();
      if (!assessment.recorder?.isRecording && monitor.stream) {
        startAssessmentRecording(monitor.stream);
      }
      if (!assessment.liveMonitoring?.connected && monitor.stream) {
        startCandidateLiveMonitoring(assessment.token, monitor.stream);
      }
    }
    for (const track of monitor.stream.getVideoTracks()) {
      track.addEventListener("ended", () => {
        if (monitor.stopping) return;
        const message = "Camera access stopped.";
        if (monitor.started) reportProctoringEvent("camera_stopped");
        stopCandidateLiveMonitoring();
        stopAssessmentRecording();
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
    stopAssessmentRecording();
    stopAssessmentProctoring();
    monitor.error = `${error.message}${eventErrorMessage}`;
    renderPublicAssessment();
  }
}

function startAssessmentRecording(stream) {
  const assessment = state.publicAssessment;
  if (!assessment || !stream) return;
  if (typeof window.MediaRecorder === "undefined") {
    console.warn("MediaRecorder is not supported in this browser.");
    return;
  }
  let mimeType = "video/webm";
  if (MediaRecorder.isTypeSupported("video/webm;codecs=vp9")) mimeType = "video/webm;codecs=vp9";
  else if (MediaRecorder.isTypeSupported("video/webm;codecs=vp8")) mimeType = "video/webm;codecs=vp8";
  else if (MediaRecorder.isTypeSupported("video/webm")) mimeType = "video/webm";
  else if (MediaRecorder.isTypeSupported("video/mp4")) mimeType = "video/mp4";

  assessment.recorder = {
    mediaRecorder: null,
    chunks: [],
    startTime: Date.now(),
    isRecording: false,
    mimeType
  };

  try {
    const recorder = new MediaRecorder(stream, { mimeType });
    assessment.recorder.mediaRecorder = recorder;

    recorder.ondataavailable = event => {
      if (event.data && event.data.size > 0) {
        assessment.recorder.chunks.push(event.data);
      }
    };

    recorder.onerror = event => {
      console.warn("MediaRecorder error:", event.error);
    };

    recorder.onstop = () => {
      finalizeAssessmentRecordingUpload();
    };

    recorder.start(4000);
    assessment.recorder.isRecording = true;

    api(`/assessments/${encodeURIComponent(assessment.token)}/recording/start`, {
      method: "POST",
      body: JSON.stringify({ consent: true, consent_version: "1.0" })
    }).catch(err => console.warn("Recording start endpoint registration note:", err));
  } catch (err) {
    console.warn("Could not start MediaRecorder:", err);
  }
}

function stopAssessmentRecording() {
  const rec = state.publicAssessment?.recorder;
  if (rec && rec.mediaRecorder && rec.mediaRecorder.state !== "inactive") {
    try {
      rec.isRecording = false;
      rec.mediaRecorder.stop();
    } catch (e) {
      console.warn("Error stopping MediaRecorder:", e);
    }
  }
}

async function finalizeAssessmentRecordingUpload() {
  const assessment = state.publicAssessment;
  const rec = assessment?.recorder;
  if (!assessment || !rec || !rec.chunks.length) return;
  const chunksToUpload = [...rec.chunks];
  rec.chunks = [];
  const duration = Math.max(1, Math.round((Date.now() - (rec.startTime || Date.now())) / 1000));
  const blob = new Blob(chunksToUpload, { type: rec.mimeType || "video/webm" });

  const formData = new FormData();
  formData.append("video", blob, `assessment_${assessment.data?.id || "rec"}.webm`);
  formData.append("duration_seconds", String(duration));

  showToast("Finalizing secure video recording upload…");
  try {
    const response = await fetch(`/api/assessments/${encodeURIComponent(assessment.token)}/recording/complete?duration_seconds=${duration}`, {
      method: "POST",
      body: formData
    });
    if (response.ok) {
      showToast("Webcam recording securely stored.");
    } else {
      console.warn("Recording completion status:", response.status);
    }
  } catch (err) {
    console.warn("Error uploading recording:", err);
  }
}

function startCandidateLiveMonitoring(token, stream) {
  const assessment = state.publicAssessment;
  if (!assessment || !token) return;
  stopCandidateLiveMonitoring();

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws/live-monitoring/candidate/${encodeURIComponent(token)}`;

  assessment.liveMonitoring = {
    ws: null,
    pc: null,
    activeViewers: 0,
    connected: false,
    isViewing: false,
  };

  try {
    const ws = new WebSocket(wsUrl);
    assessment.liveMonitoring.ws = ws;

    ws.onopen = () => {
      if (!assessment.liveMonitoring) return;
      assessment.liveMonitoring.connected = true;
      const camActive = Boolean(stream && stream.active && stream.getVideoTracks().some(t => t.readyState === "live"));
      ws.send(JSON.stringify({ type: "camera_status", connected: camActive }));
      renderPublicAssessment();
    };

    ws.onmessage = async (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.type === "viewer_joined") {
          if (!assessment.liveMonitoring) return;
          assessment.liveMonitoring.activeViewers = msg.viewers_count || 1;
          assessment.liveMonitoring.isViewing = true;
          renderPublicAssessment();
        } else if (msg.type === "viewer_left") {
          if (!assessment.liveMonitoring) return;
          const remaining = msg.viewers_count || 0;
          assessment.liveMonitoring.activeViewers = remaining;
          assessment.liveMonitoring.isViewing = remaining > 0;
          if (remaining === 0 && assessment.liveMonitoring.pc) {
            try { assessment.liveMonitoring.pc.close(); } catch (e) {}
            assessment.liveMonitoring.pc = null;
          }
          renderPublicAssessment();
        } else if (msg.type === "request_offer") {
          if (!assessment.liveMonitoring) return;
          assessment.liveMonitoring.isViewing = true;
          renderPublicAssessment();
          await createWebRtcOfferForRecruiter(ws, stream);
        } else if (msg.type === "webrtc_answer") {
          const pc = assessment.liveMonitoring?.pc;
          if (pc && msg.sdp) {
            await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
          }
        } else if (msg.type === "ice_candidate") {
          const pc = assessment.liveMonitoring?.pc;
          if (pc && msg.candidate) {
            await pc.addIceCandidate(new RTCIceCandidate(msg.candidate)).catch(() => {});
          }
        }
      } catch (err) {
        console.warn("Live monitoring candidate error:", err);
      }
    };

    ws.onclose = () => {
      if (assessment.liveMonitoring) {
        assessment.liveMonitoring.connected = false;
        assessment.liveMonitoring.isViewing = false;
      }
      renderPublicAssessment();
    };

    ws.onerror = (err) => {
      console.warn("Candidate live monitoring WS error:", err);
    };
  } catch (err) {
    console.warn("Candidate live monitoring init error:", err);
  }
}

async function createWebRtcOfferForRecruiter(ws, stream) {
  const assessment = state.publicAssessment;
  if (!assessment || !ws || ws.readyState !== WebSocket.OPEN) return;

  if (assessment.liveMonitoring?.pc) {
    try { assessment.liveMonitoring.pc.close(); } catch (e) {}
  }

  const rtcConfig = {
    iceServers: [
      { urls: "stun:stun.l.google.com:19302" },
      { urls: "stun:stun1.l.google.com:19302" }
    ]
  };

  try {
    const pc = new RTCPeerConnection(rtcConfig);
    assessment.liveMonitoring.pc = pc;

    if (stream) {
      stream.getTracks().forEach(track => {
        pc.addTrack(track, stream);
      });
    }

    pc.onicecandidate = (event) => {
      if (event.candidate && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ice_candidate", candidate: event.candidate }));
      }
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    ws.send(JSON.stringify({ type: "webrtc_offer", sdp: pc.localDescription }));
  } catch (err) {
    console.warn("Failed to create WebRTC offer:", err);
  }
}

function stopCandidateLiveMonitoring() {
  const assessment = state.publicAssessment;
  if (!assessment || !assessment.liveMonitoring) return;
  const { ws, pc } = assessment.liveMonitoring;
  if (pc) {
    try { pc.close(); } catch (e) {}
  }
  if (ws) {
    if (ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "assessment_submitted" })); } catch (e) {}
    }
    try { ws.close(); } catch (e) {}
  }
  assessment.liveMonitoring = null;
}

function stopAssessmentProctoring() {
  const monitor = state.publicAssessment?.proctor;
  if (!monitor) return;
  stopCandidateLiveMonitoring();
  stopAssessmentRecording();
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
      if (assessment.proctor.started) {
        attachProctoringActivityListeners();
        if (assessment.proctor.stream) {
          startAssessmentRecording(assessment.proctor.stream);
          startCandidateLiveMonitoring(assessment.token, assessment.proctor.stream);
        }
      }
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
      stopCandidateLiveMonitoring();
      stopAssessmentRecording();
      stopAssessmentProctoring();
    } catch (error) {
      assessment.error = error.message;
      clearInterval(assessment.timer);
      stopCandidateLiveMonitoring();
      stopAssessmentRecording();
      stopAssessmentProctoring();
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
          <div class="field"><label for="auth-password">Password</label><div class="password-wrap"><input class="input" id="auth-password" name="password" type="password" autocomplete="${register ? "new-password" : "current-password"}" placeholder="${register ? "At least 8 characters" : "Enter your password"}" minlength="${register ? "8" : "1"}" required /><button type="button" class="password-toggle" data-toggle-password="auth-password" aria-label="Show password" title="Show password">👁</button></div></div>
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
  if (state.user?.role === "recruiter") return [["Dashboard", "⌂"], ["My jobs", "▣"], ["Applicants", "♙"], ["Assessments", "✓"], ["Live monitoring", "◎"], ["Question bank", "☷"], ["Interviews", "◷"], ["Company profile", "◉"]];
  if (state.user?.role === "admin") return [["Dashboard", "⌂"], ["Users", "♙"], ["Job listings", "▣"], ["Assessments", "✓"], ["Live monitoring", "◎"], ["Question bank", "☷"], ["Interviews", "◷"]];
  return [...common, ["Interviews", "◷"], ["Career Assistant", "💬"]];
}

function updatePageTitle(pageName) {
  const titles = {
    "Dashboard": "Dashboard — SkillSphere",
    "Find jobs": "Find Jobs — SkillSphere",
    "Applications": "My Applications — SkillSphere",
    "Assessments": "Assessments — SkillSphere",
    "Live monitoring": "Live Assessment Monitoring — SkillSphere",
    "My profile": "My Profile — SkillSphere",
    "Career Assistant": "SkillSphere Career Assistant",
    "AI Career Assistant": "SkillSphere Career Assistant",
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

function recruiterJobRow(job) {
  return `<article class="job-row">
    <div class="company-mark">${esc(initials(job.company))}</div>
    <div style="flex:1">
      <div style="display:flex;align-items:center;gap:8px;">
        <h3 style="margin:0;cursor:pointer;" data-job="${esc(job.id)}">${esc(job.title)}</h3>
        <span class="badge ${job.status === "Open" ? "badge-green" : "badge-gray"}">${esc(job.status || "Open")}</span>
      </div>
      <p style="margin:2px 0 6px;">${esc(job.company)} · ${esc(job.location)}</p>
      <div class="job-meta">
        <span>${esc(job.job_type || "Full-time")}</span>
        <span>${esc(job.experience || "All levels")}</span>
        ${job.salary ? `<span>${esc(job.salary)}</span>` : ""}
      </div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <button class="btn btn-quiet btn-small" data-page="Applicants">Applicants</button>
      <button class="btn btn-quiet btn-small" data-action="manage-question-bank" data-id="${esc(job.id)}">Question Bank</button>
      <button class="btn btn-danger btn-small" data-action="delete-job" data-id="${esc(job.id)}" data-title="${esc(job.title)}" style="color:#dc2626;border-color:#fca5a5;background:transparent;cursor:pointer;">Delete</button>
    </div>
  </article>`;
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

function candidateAssessmentsListHtml(assessments) {
  if (!assessments.length) {
    return empty("No assessments assigned yet", "When you apply for a role with a 70% or higher match, your technical assessment will appear here.", "✓");
  }
  return `<div class="application-list">
    ${assessments.slice(0, 4).map(item => `
      <div class="application-row" style="display:flex;align-items:center;justify-content:space-between;padding:11px 0;border-bottom:1px solid var(--line,#f0f0f0);gap:12px;flex-wrap:wrap">
        <div>
          <strong>${esc(item.title)}</strong>
          <small style="display:block;color:var(--muted,#666);margin-top:2px">${esc(item.company)} · ${esc(item.technology || "General")} (${esc(item.total_questions || 20)} questions · ${esc(item.duration_minutes || 20)} min)</small>
        </div>
        <div style="display:flex;align-items:center;gap:10px">
          ${["Completed", "Pending Recruiter Decision", "Submitted"].includes(item.assessment_status)
            ? `<span class="badge badge-blue">Submitted · Under Review</span>`
            : item.assessment_status === "Expired"
            ? `<span class="badge badge-red">Link expired</span>`
            : `<span class="badge badge-amber">${esc(item.assessment_status || "Pending")}</span>`}
          ${["Sent", "Started"].includes(item.assessment_status)
            ? `<button class="btn btn-primary btn-small" data-action="take-assessment" data-url="${esc(item.action_url || "")}">${item.assessment_status === "Started" ? "Resume test →" : "Start test →"}</button>`
            : ""}
        </div>
      </div>
    `).join("")}
  </div>`;
}

function candidateDashboard() {
  const candidateApps = state.applications;
  const latest = state.jobs.slice(0, 3);
  const upcomingInterview = state.upcomingInterviews[0];
  const analytics = state.candidateAnalytics || {};
  const assessments = state.assessments || [];
  const pendingAssessments = assessments.filter(item => ["Sent", "Started"].includes(item.assessment_status));

  return `${heading("Your career, in motion", `Good to see you, ${esc(state.user.name.split(" ")[0])} <span style="color:#c83b42">✳</span>`, "A little progress today can open a big door tomorrow.", `<button class="btn btn-primary" data-page="Find jobs">Find your next role&nbsp; →</button>`)}
    <section class="grid stats-grid">
      ${stat("Active applications", analytics.active_applications ?? candidateApps.filter(item => !["Rejected", "Withdrawn", "Accepted", "Selected"].includes(item.status)).length, "Your opportunities in progress", "↗")}
      ${stat("Technical tests", assessments.length, pendingAssessments.length ? `${pendingAssessments.length} pending action` : "Assigned evaluations", "✓")}
      ${stat("Completed tests", candidateApps.filter(item => ["Completed", "Pending Recruiter Decision", "Submitted"].includes(item.assessment_status)).length, "Submitted for review", "◉")}
      ${stat("Interviews", analytics.interviews_scheduled ?? state.interviews.length, "Conversations in pipeline", "◷")}
    </section>
    ${pendingAssessments.length ? `
      <div class="pending-assessment-banner">
        <div>
          <strong>⚡ Action required: ${esc(pendingAssessments[0].title)} assessment is ready</strong>
          <p>${esc(pendingAssessments[0].company)} · ${esc(pendingAssessments[0].technology)} · ${esc(pendingAssessments[0].total_questions)} questions (${esc(pendingAssessments[0].duration_minutes)} min) · Camera proctored</p>
        </div>
        <button class="btn btn-primary btn-small" data-action="take-assessment" data-url="${esc(pendingAssessments[0].action_url || "")}">${pendingAssessments[0].assessment_status === "Started" ? "Resume test →" : "Take assessment now →"}</button>
      </div>
    ` : ""}
    <section class="columns">
      <article class="card"><div class="card-heading"><div><h2>Roles to explore</h2><p>Thoughtful opportunities for your next chapter</p></div><button class="text-button" data-page="Find jobs">See all →</button></div><div class="job-list">${latest.length ? latest.map(job => jobRow(job)).join("") : empty("No open roles yet", "Check back soon for new opportunities.")}</div></article>
      <article class="card"><div class="card-heading"><div><h2>Your applications</h2><p>A quick look at where things stand</p></div><button class="text-button" data-page="Applications">View all →</button></div><div class="application-list">${applicationRows(candidateApps)}</div></article>
    </section>
    <section class="card" style="margin-top:16px">
      <div class="card-heading"><div><h2>Technical assessments <span class="badge">${assessments.length}</span></h2><p>Assigned evaluations and status</p></div><button class="text-button" data-page="Assessments">View all →</button></div>
      ${candidateAssessmentsListHtml(assessments)}
    </section>
    <section class="card upcoming-interview-card" style="margin-top:16px"><div class="card-heading"><div><h2>Upcoming interviews</h2><p>Your next conversation, all in one place</p></div><button class="text-button" data-page="Interviews">View all →</button></div>
      ${upcomingInterview ? `<article class="upcoming-interview"><span class="eyebrow">${esc(upcomingInterview.company)}</span><h3>${esc(upcomingInterview.title)}</h3><p>${esc(candidateInterviewTime(upcomingInterview))} · ${esc(upcomingInterview.duration_minutes)} minutes</p><div class="interview-actions">${/^https:\/\//i.test(upcomingInterview.meeting_url || "") ? `<a class="btn btn-primary btn-small join-interview" href="${esc(upcomingInterview.meeting_url)}" target="_blank" rel="noopener noreferrer">Join Interview ↗</a>` : `<span class="stat-note">The recruiter has not added a meeting link yet.</span>`}<button class="text-button" data-action="view-interview" data-id="${esc(upcomingInterview.id)}">View Interview Details</button></div></article>` : empty("No upcoming interviews", "When a recruiter schedules an interview, it will appear here.", "◷")}
    </section>
    <section class="card" style="margin-top:16px"><div class="card-heading"><div><h2>${state.resume ? "Your resume is ready" : "Take the guesswork out of your search"}</h2><p>${state.resume ? `${esc(state.resume.filename)} · ${esc(state.resume.processing_status)}` : "Upload a resume to discover which roles match your skills."}</p></div><button class="btn btn-quiet btn-small" data-page="My profile">${state.resume ? "View profile" : "Add your resume"}</button></div>
      <div class="progress-track"><div class="progress-fill" style="width:${state.resume ? "78%" : "36%"}"></div></div>
      ${state.resume?.processing_error ? `<p style="color:#aa3c40;font-size:10px;margin:9px 0 0">${esc(state.resume.processing_error)}</p>` : ""}
    </section>
    <section class="card" style="margin-top:16px;background:linear-gradient(135deg, #fff7ed 0%, #fff 100%);border-color:#fed7aa;">
      <div class="card-heading">
        <div>
          <h2>💬 SkillSphere Career Assistant</h2>
          <p>Get personalized guidance on your resume, prepare for technical &amp; HR interviews, and explore career advice.</p>
        </div>
        <button class="btn btn-primary btn-small" data-page="Career Assistant">Launch Assistant →</button>
      </div>
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
      <td>${item.assessment_status ? `<span class="badge ${['Completed', 'Pending Recruiter Decision', 'Submitted'].includes(item.assessment_status) ? 'badge-blue' : item.assessment_status === 'Expired' ? 'badge-red' : 'badge-amber'}">${['Completed', 'Pending Recruiter Decision', 'Submitted'].includes(item.assessment_status) ? 'Submitted · Under Review' : esc(item.assessment_status)}</span>${["Sent", "Assessment Sent", "Started", "Assessment Started"].includes(item.assessment_status) ? `<button class="btn btn-primary btn-small" style="display:inline-block;margin-top:4px" data-action="take-assessment" data-url="${esc(item.assessment_action_url || "")}">${item.assessment_status.includes("Started") ? "Resume test →" : "Take test →"}</button>` : ""}` : "—"}</td>
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

  if (state.user.role === "candidate") {
    const active = rows.filter(item => ["Sent", "Started"].includes(item.assessment_status));
    const submitted = rows.filter(item => ["Completed", "Pending Recruiter Decision", "Submitted"].includes(item.assessment_status));

    return `${heading("Your technical assessments", "My Assessments", "View your assigned evaluations and take active assessments.")}
      <section class="grid stats-grid" style="margin-bottom:16px">
        ${stat("Total assessments", rows.length, "Assigned to your applications", "✓")}
        ${stat("Pending tests", active.length, active.length ? "Ready to take" : "All caught up", active.length ? "⚡" : "★")}
        ${stat("Submitted tests", submitted.length, "Under recruiter review", "◉")}
        ${stat("Status", submitted.length ? "Under review" : "In progress", "Application pipeline", "◷")}
      </section>

      ${active.length ? `
        <div class="pending-assessment-banner">
          <div>
            <strong>⚡ Active assessment ready: ${esc(active[0].title)} · ${esc(active[0].company)}</strong>
            <p>${esc(active[0].technology || "Technical Evaluation")} · ${esc(active[0].total_questions)} questions · ${esc(active[0].duration_minutes)} minutes</p>
          </div>
          <button class="btn btn-primary" data-action="take-assessment" data-url="${esc(active[0].action_url || "")}">${active[0].assessment_status === "Started" ? "Resume assessment →" : "Take assessment now →"}</button>
        </div>
      ` : ""}

      <section class="card">
        <div class="card-heading">
          <div>
            <h2>Assigned technical assessments</h2>
            <p>${rows.length} assessment${rows.length === 1 ? "" : "s"} across your job applications</p>
          </div>
        </div>
        ${rows.length ? `<div class="table-wrap"><table>
          <thead>
            <tr>
              <th>Role & Company</th>
              <th>Assessment & Topic</th>
              <th>Format</th>
              <th>Status</th>
              <th>Date</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(item => `
              <tr>
                <td><strong>${esc(item.title)}</strong><small>${esc(item.company)}</small></td>
                <td><strong>${esc(item.assessment_name || "Round 1: Technical MCQ Test")}</strong><small>${esc(item.technology || "Topic not selected")}</small></td>
                <td>${esc(item.total_questions)} questions<small>${esc(item.duration_minutes)} min</small></td>
                <td><span class="badge ${['Completed', 'Pending Recruiter Decision', 'Submitted'].includes(item.assessment_status) ? 'badge-blue' : item.assessment_status === 'Expired' ? 'badge-red' : 'badge-amber'}">${['Completed', 'Pending Recruiter Decision', 'Submitted'].includes(item.assessment_status) ? 'Submitted · Under Review' : esc(item.assessment_status)}</span></td>
                <td>${dateTimeLabel(item.submitted_at || item.email_sent_at || item.starts_at)}</td>
                <td>
                  ${["Completed", "Pending Recruiter Decision", "Submitted"].includes(item.assessment_status)
                    ? `<span class="stat-note" style="color:#2563eb;font-weight:600;">Under Recruiter Review</span>`
                    : ["Sent", "Started"].includes(item.assessment_status)
                    ? `<button class="btn btn-primary btn-small" data-action="take-assessment" data-url="${esc(item.action_url || "")}">${item.assessment_status === "Started" ? "Resume test →" : "Take test →"}</button>`
                    : item.assessment_status === "Expired"
                    ? `<span class="badge badge-red">Link expired</span>`
                    : `<span class="stat-note">${esc(item.assessment_status || "Pending")}</span>`}
                </td>
              </tr>
            `).join("")}
          </tbody>
        </table></div>` : empty("No assessments found", "When you apply for a role with a 70% or higher match, your technical assessment will appear here.", "✓")}
      </section>`;
  }

  const filters = `<form id="assessment-filter-form" class="filters assessment-filters">
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
    ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>Candidate</th><th>Role</th><th>Assessment</th><th>Score</th><th>Result</th><th>Completed</th><th>Pipeline stage</th><th>Next step</th><th>Review</th></tr></thead><tbody>${rows.map(item => `<tr>
      <td><strong>${esc(item.candidate_name)}</strong><small>${esc(item.candidate_email)}</small></td>
      <td>${esc(item.title)}<small>${esc(item.company)}</small></td>
      <td>${esc(item.assessment_name || "Round 1: Technical MCQ Test")}<small>${esc(item.technology || "Topic not selected")}</small><small>${esc(item.total_questions)} questions · ${esc(item.duration_minutes)} min · Pass ${esc(item.pass_percentage)}%</small></td>
      <td>${item.score_percentage == null ? "—" : `<strong>${esc(item.score_percentage)}%</strong><small>${esc(item.correct_answer_count ?? item.score)} correct / ${esc(item.total_questions)}</small>`}</td>
      <td>${item.result ? `<span class="badge ${item.result === "Passed" ? "badge-green" : "badge-red"}">${esc(item.result)}</span>` : `<span class="badge badge-amber">${esc(item.assessment_status)}</span>`}</td>
      <td>${dateTimeLabel(item.submitted_at)}</td>
      <td>${esc(item.application_status)}</td><td>${item.result === "Passed" ? `<select class="status-select" data-assessment-pipeline="${esc(item.application_id)}" aria-label="Move ${esc(item.candidate_name)} to next stage"><option value="">Choose next stage</option>${statusOptions(item.application_status).filter(value => ["Interview", "Accepted", "Rejected"].includes(value)).map(value => `<option>${value}</option>`).join("")}</select>` : "—"}</td><td>${item.assessment_status === "Completed" ? `<button class="text-button" data-action="review-assessment" data-id="${esc(item.id)}">Review answers</button>` : item.assessment_status === "Started" ? `<button class="text-button" data-action="view-live-video" data-id="${esc(item.id)}" style="color:#16a34a;font-weight:600;">◎ Live Video</button>` : "—"}</td>
    </tr>`).join("")}</tbody></table></div>` : empty("No assessments found", "Assessments matching your filters will appear here.", "✓")}
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
  const qCount = summary.configured_assessment_question_count || 20;
  return `${heading("Question bank", esc(state.questionBankJob.title), `${esc(state.questionBankJob.company)} · Assessments use ${qCount} active questions across relevant skill topics.`, `<div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn btn-primary btn-small" data-action="open-ai-question-generator">⚡ Generate with AI</button><button class="btn btn-quiet btn-small" data-action="back-question-banks">← All jobs</button></div>`)}
    <section class="grid stats-grid bank-topics">${counts.map(topic => stat(topic.technology, topic.active_count, topic.ready ? "Ready for assessments" : `${topic.additional_needed} more active questions needed`, topic.ready ? "✓" : "＋")).join("") || stat("No topics yet", 0, "Add questions to create a topic", "＋")}</section>
    <section class="card" style="background:linear-gradient(135deg,#ffffff 0%,#fff8f7 100%);border-color:#f8dedb;margin-bottom:16px;">
      <div class="card-heading" style="margin-bottom:0">
        <div>
          <span class="eyebrow" style="color:var(--red)">⚡ AI Assessment Builder</span>
          <h2 style="margin:4px 0 6px">Instant Technical Question Generator</h2>
          <p>Generate targeted, job-aligned MCQs with verified correct answers, explanations, and difficulty levels in seconds.</p>
        </div>
        <button class="btn btn-primary" data-action="open-ai-question-generator">⚡ Generate with AI →</button>
      </div>
    </section>
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
      <aside class="card"><div class="card-heading"><div><h2>Your resume</h2><p>PDF, Word, or image, up to 10 MB</p></div></div>
        <form id="resume-form" class="resume-panel">
          <label class="upload-drop" for="resume-file"><span class="upload-mark">↑</span><strong>${resume ? esc(resume.filename) : "Upload your resume"}</strong><p>${resume ? `Last updated ${dateLabel(resume.uploaded_at)}` : "PDF, DOCX, JPG, or PNG · Max 10 MB"}</p><span class="btn btn-quiet btn-small">Choose a file</span><input id="resume-file" name="file" type="file" accept=".pdf,.docx,.jpg,.jpeg,.png,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,image/jpeg,image/png" hidden /></label>
        </form>
        ${resume ? `<div style="margin-top:16px"><span class="badge ${resume.processing_status === "completed" ? "badge-green" : "badge-amber"}">${esc(resume.processing_status)}</span>${resume.processing_error ? `<p style="margin:9px 0 0;color:#a33d40;font-size:11px;line-height:1.4">${esc(resume.processing_error)}</p>` : ""}</div>
        <div style="margin-top:15px"><div class="card-heading"><div><h2 style="font-size:12px">Extracted details</h2></div></div>
          <div class="match-score"><div><strong>Name</strong><p>${esc(parsed.name || "Not provided")}</p></div></div>
          <div class="match-score"><div><strong>Email</strong><p>${esc(parsed.email || "Not provided")}</p></div></div>
          <div class="match-score"><div><strong>Phone</strong><p>${esc(parsed.phone || "Not provided")}</p></div></div>
          <div class="skills">${(parsed.skills || []).map(skill => `<span class="skill">${esc(skill)}</span>`).join("") || `<span class="skill">No skills detected</span>`}</div>
        </div>` : ""}
      </aside>
    </div>`;
}

// =============================================================================
// AI CAREER ASSISTANT & MOCK INTERVIEW IMPLEMENTATION
// =============================================================================

function formatAiMessage(text) {
  if (!text) return "";
  let escaped = esc(text);

  const codeBlocks = [];
  escaped = escaped.replace(/```(?:([a-zA-Z0-9_+-]+)?\n)?([\s\S]*?)```/g, (match, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push(`<pre><code class="${lang ? `language-${lang}` : ""}">${code.trim()}</code></pre>`);
    return `__CODE_BLOCK_${idx}__`;
  });

  escaped = escaped.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  escaped = escaped.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  escaped = escaped.replace(/(^|[^*])\*([^*]+)\*([^*]|$)/g, "$1<em>$2</em>$3");

  const lines = escaped.split("\n");
  const resultLines = [];
  let inList = false;
  let listType = "ul";

  for (let line of lines) {
    const ulMatch = line.match(/^(\s*)[-*•]\s+(.*)$/);
    const olMatch = line.match(/^(\s*)\d+\.\s+(.*)$/);

    if (ulMatch) {
      if (!inList || listType !== "ul") {
        if (inList) resultLines.push(`</${listType}>`);
        resultLines.push("<ul>");
        inList = true;
        listType = "ul";
      }
      resultLines.push(`<li>${ulMatch[2]}</li>`);
    } else if (olMatch) {
      if (!inList || listType !== "ol") {
        if (inList) resultLines.push(`</${listType}>`);
        resultLines.push("<ol>");
        inList = true;
        listType = "ol";
      }
      resultLines.push(`<li>${olMatch[2]}</li>`);
    } else {
      if (inList) {
        resultLines.push(`</${listType}>`);
        inList = false;
      }
      resultLines.push(line);
    }
  }
  if (inList) resultLines.push(`</${listType}>`);

  const content = resultLines.join("\n");
  const paragraphs = content.split(/\n\s*\n/).map(p => {
    p = p.trim();
    if (!p) return "";
    if (p.startsWith("<pre>") || p.startsWith("<ul>") || p.startsWith("<ol>")) return p;
    return `<p>${p.replace(/\n/g, "<br>")}</p>`;
  }).filter(Boolean).join("");

  return paragraphs.replace(/__CODE_BLOCK_(\d+)__/g, (_, idx) => codeBlocks[Number(idx)] || "");
}

async function loadAiSessions() {
  try {
    const data = await api("/candidate/ai/sessions");
    state.aiSessions = data.sessions || [];
    state.aiResumeLoaded = !!data.has_resume;
    state.aiResumeName = data.resume_name || "";
    if (!state.aiActiveSessionId && state.aiSessions.length > 0) {
      await loadAiSessionMessages(state.aiSessions[0].id);
    }
  } catch (error) {
    console.warn("Could not load AI sessions:", error);
  }
}

async function loadAiSessionMessages(sessionId) {
  try {
    const data = await api(`/chat/history/${sessionId}`);
    state.aiActiveSessionId = sessionId;
    state.aiMessages = data.messages || [];
    renderWorkspace();
    scrollAiChatToBottom();
  } catch (error) {
    showToast(error.message, true);
  }
}

function scrollAiChatToBottom() {
  setTimeout(() => {
    const stream = document.querySelector("#ai-chat-stream");
    if (stream) stream.scrollTop = stream.scrollHeight;
  }, 50);
}

async function sendAiChatMessage(userMessage) {
  const text = (userMessage || "").trim();
  if (!text) return;

  state.aiMessages.push({
    id: "temp-" + Date.now(),
    sender: "candidate",
    message: text,
    created_at: new Date().toISOString()
  });
  state.aiChatBusy = true;
  renderWorkspace();
  scrollAiChatToBottom();

  try {
    const payload = { message: text };
    if (state.aiActiveSessionId) payload.session_id = state.aiActiveSessionId;

    const res = await api("/chat", {
      method: "POST",
      body: JSON.stringify(payload)
    });

    state.aiActiveSessionId = res.session_id;
    state.aiFollowUpSuggestions = res.suggested_follow_ups || [];
    state.aiMessages.push({
      id: "bot-" + Date.now(),
      sender: "assistant",
      message: res.message,
      created_at: res.created_at,
      intent: res.intent,
      confidence: res.confidence
    });
    state.aiChatBusy = false;
    await loadAiSessions();
    renderWorkspace();
    scrollAiChatToBottom();
  } catch (error) {
    state.aiChatBusy = false;
    showToast(error.message, true);
    renderWorkspace();
  }
}

async function deleteAiSession(sessionId) {
  if (!confirm("Are you sure you want to delete this conversation?")) return;
  try {
    await api(`/candidate/ai/sessions/${sessionId}`, { method: "DELETE" });
    if (state.aiActiveSessionId === sessionId) {
      state.aiActiveSessionId = null;
      state.aiMessages = [];
      state.aiFollowUpSuggestions = null;
    }
    await loadAiSessions();
    renderWorkspace();
    showToast("Conversation deleted");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function clearAiChat() {
  if (!confirm("Are you sure you want to clear this conversation?")) return;
  if (state.aiActiveSessionId) {
    try {
      await api(`/chat/history/${state.aiActiveSessionId}`, { method: "DELETE" });
    } catch (e) {
      console.warn("Could not delete chat history on server:", e);
    }
  }
  state.aiActiveSessionId = null;
  state.aiMessages = [];
  state.aiFollowUpSuggestions = null;
  await loadAiSessions();
  renderWorkspace();
  showToast("Conversation cleared");
}

async function startMockInterview(jobRole, interviewType, difficulty) {
  state.mockInterviewBusy = true;
  state.mockViewingHistoryItem = null;
  renderWorkspace();
  try {
    const res = await api("/candidate/ai/mock-interview/start", {
      method: "POST",
      body: JSON.stringify({
        job_role: jobRole,
        interview_type: interviewType,
        difficulty: difficulty,
        total_questions: 5
      })
    });
    state.mockInterview = res;
    state.mockInterviewBusy = false;
    renderWorkspace();
    showToast("Mock interview started! Question 1 is ready.");
  } catch (error) {
    state.mockInterviewBusy = false;
    renderWorkspace();
    showToast(error.message, true);
  }
}

async function submitMockAnswer(answerText) {
  if (!state.mockInterview?.interview?.id || !state.mockInterview?.current_question) return;
  state.mockInterviewBusy = true;
  renderWorkspace();
  try {
    const res = await api("/candidate/ai/mock-interview/answer", {
      method: "POST",
      body: JSON.stringify({
        mock_interview_id: state.mockInterview.interview.id,
        question_index: state.mockInterview.current_question.question_index,
        answer: answerText
      })
    });
    if (res.is_complete) {
      state.mockInterview = {
        ...state.mockInterview,
        is_complete: true,
        final_report: res.final_report,
        last_feedback: res.feedback
      };
      showToast("Mock interview complete! Performance report generated 🎉");
    } else {
      state.mockInterview = {
        ...state.mockInterview,
        current_question: res.next_question,
        last_feedback: res.feedback,
        last_answered_index: res.question_index
      };
      showToast(`Answer recorded. Moving to Question ${res.next_question.question_index}.`);
    }
    state.mockInterviewBusy = false;
    renderWorkspace();
  } catch (error) {
    state.mockInterviewBusy = false;
    renderWorkspace();
    showToast(error.message, true);
  }
}

async function loadMockInterviewsHistory() {
  try {
    const res = await api("/candidate/ai/mock-interviews");
    state.mockInterviewsHistory = res.items || [];
    renderWorkspace();
  } catch (error) {
    console.warn("Could not load mock interviews history:", error);
  }
}

async function viewMockHistoryDetail(id) {
  try {
    const res = await api(`/candidate/ai/mock-interviews/${id}`);
    state.mockViewingHistoryItem = res;
    state.aiTab = "mock";
    renderWorkspace();
  } catch (error) {
    showToast(error.message, true);
  }
}

function aiAssistantPage() {
  const activeTab = state.aiTab || "chat";
  const resumeBadge = state.aiResumeLoaded
    ? `<span class="badge badge-green">✓ Resume Loaded (${esc(state.aiResumeName || "Active")})</span>`
    : `<span class="badge badge-amber" style="cursor:pointer;" data-page="My profile" title="Upload a resume in My Profile for personalized insights">⚠️ No Resume Uploaded — Upload in Profile</span>`;

  return `
    ${heading(
      "Career Assistant",
      "SkillSphere Career Assistant",
      "Prepare for interviews, analyze your resume, explore technical concepts, and receive offline career guidance.",
      `<div style="display:flex;align-items:center;gap:10px;">
        <span class="ai-status-online"><span class="ai-status-dot"></span> Online</span>
        ${resumeBadge}
      </div>`
    )}

    <div class="ai-assistant-page">
      <nav class="ai-nav-tabs" aria-label="Career Assistant Views">
        <button class="ai-tab-btn ${activeTab === "chat" ? "active" : ""}" data-action="switch-ai-tab" data-tab="chat">💬 Career &amp; Resume Chat</button>
        <button class="ai-tab-btn ${activeTab === "mock" ? "active" : ""}" data-action="switch-ai-tab" data-tab="mock">🎯 Start Mock Interview</button>
        <button class="ai-tab-btn ${activeTab === "history" ? "active" : ""}" data-action="switch-ai-tab" data-tab="history">📜 Past Practice &amp; Reports</button>
      </nav>

      ${activeTab === "chat" ? renderAiChatView() : activeTab === "mock" ? renderAiMockView() : renderAiHistoryView()}
    </div>
  `;
}

function renderAiChatView() {
  const activeSession = state.aiSessions.find(s => s.id === state.aiActiveSessionId);
  const activeTitle = activeSession ? activeSession.session_title : "New Conversation";
  const messages = state.aiMessages || [];

  const defaultPrompts = [
    "Improve my resume.",
    "Explain my technical skills.",
    "Prepare me for an HR interview.",
    "Help me prepare for a technical interview.",
    "How do I prepare for placements?",
    "How do I apply for jobs?"
  ];
  const suggestedPrompts = (state.aiFollowUpSuggestions && state.aiFollowUpSuggestions.length > 0)
    ? state.aiFollowUpSuggestions
    : defaultPrompts;

  return `
    <div class="ai-chat-layout">
      <aside class="ai-sessions-sidebar">
        <div class="ai-sidebar-head">
          <span>Conversations</span>
          <button class="btn btn-quiet btn-small" data-action="new-ai-session" title="Start a fresh chat">+ New</button>
        </div>
        <div class="ai-session-list">
          <button class="ai-session-item ${!state.aiActiveSessionId ? "active" : ""}" data-action="new-ai-session">
            <span class="ai-session-title">💬 New Conversation</span>
          </button>
          ${state.aiSessions.map(s => `
            <div class="ai-session-item ${state.aiActiveSessionId === s.id ? "active" : ""}" data-action="select-ai-session" data-id="${s.id}">
              <span class="ai-session-title" title="${esc(s.session_title)}">${esc(s.session_title)}</span>
              <button class="ai-session-del-btn" data-action="delete-ai-session" data-id="${s.id}" title="Delete conversation">×</button>
            </div>
          `).join("")}
        </div>
      </aside>

      <section class="ai-chat-main">
        <header class="ai-chat-header">
          <div>
            <div style="display:flex;align-items:center;gap:8px;">
              <h3>${esc(activeTitle)}</h3>
              <span class="ai-status-online"><span class="ai-status-dot"></span> Online</span>
            </div>
            <small style="color:var(--muted);">${state.aiResumeLoaded ? `Using ${esc(state.aiResumeName)} as context` : "Upload a resume in My Profile for personalized insights"}</small>
          </div>
          <button class="text-button" data-action="clear-ai-chat" title="Clear messages in current chat">Clear conversation</button>
        </header>

        <div class="ai-chat-stream" id="ai-chat-stream">
          ${messages.length === 0 ? `
            <div class="ai-msg ai-msg-assistant">
              <div class="ai-msg-avatar">💬</div>
              <div class="ai-msg-bubble">
                <p><strong>Hello! Welcome to SkillSphere Career Assistant.</strong></p>
                <p>I can help you understand your resume, prepare for interviews, improve your technical knowledge and get career guidance. What would you like to know?</p>
              </div>
            </div>
          ` : messages.map(m => `
            <div class="ai-msg ${m.sender === "candidate" ? "ai-msg-candidate" : "ai-msg-assistant"}">
              <div class="ai-msg-avatar">${m.sender === "candidate" ? esc(initials(state.user.name)) : "💬"}</div>
              <div class="ai-msg-bubble">
                ${formatAiMessage(m.message)}
                <small style="display:block;margin-top:6px;font-size:10px;opacity:0.6;text-align:${m.sender === "candidate" ? "right" : "left"};">${m.created_at ? new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ""}</small>
              </div>
            </div>
          `).join("")}

          ${state.aiChatBusy ? `
            <div class="ai-typing-indicator">
              <div class="ai-dot"></div><div class="ai-dot"></div><div class="ai-dot"></div>
              <span>SkillSphere Assistant is typing…</span>
            </div>
          ` : ""}
        </div>

        <div class="ai-prompt-chips">
          ${suggestedPrompts.map(p => `
            <button type="button" class="ai-chip" data-action="use-ai-prompt" data-prompt="${esc(p)}">${esc(p)}</button>
          `).join("")}
        </div>

        <form class="ai-chat-input-box" id="ai-chat-form">
          <textarea id="ai-chat-input" class="ai-chat-textarea" placeholder="Ask anything about your resume, interview prep, technical concepts... (Enter to send, Shift+Enter for new line)" required ${state.aiChatBusy ? "disabled" : ""}></textarea>
          <button class="btn btn-primary" type="submit" id="ai-chat-send-btn" ${state.aiChatBusy ? "disabled" : ""}>Send →</button>
        </form>
      </section>
    </div>
  `;
}

function renderAiMockView() {
  if (state.mockViewingHistoryItem) {
    const item = state.mockViewingHistoryItem.interview;
    const exchanges = state.mockViewingHistoryItem.exchanges || [];
    const report = item.final_feedback || {};

    return `
      <div class="card" style="margin-bottom:16px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
          <button class="btn btn-quiet btn-small" data-action="close-mock-history-view">← Back to Practice</button>
          <span class="badge badge-green">${esc(item.status)}</span>
        </div>
        ${renderMockFinalReport(report, item)}
        <hr style="border:0;border-top:1px solid var(--line);margin:20px 0;" />
        <h3>Q&amp;A Transcript</h3>
        <div style="display:flex;flex-direction:column;gap:14px;margin-top:12px;">
          ${exchanges.map(e => `
            <div class="card" style="background:#faf8f6;padding:14px;">
              <strong>Question ${e.question_index}:</strong>
              <p style="margin:4px 0 8px;">${esc(e.question)}</p>
              <div style="background:white;padding:10px 12px;border-radius:8px;border:1px solid var(--line);margin-bottom:8px;">
                <strong style="font-size:11px;color:var(--muted);">Your Answer:</strong>
                <p style="margin:2px 0 0;font-size:13px;">${esc(e.answer || "No answer recorded")}</p>
              </div>
              ${e.feedback_obj?.feedback ? `
                <div class="mock-feedback-box ${e.feedback_obj.score < 7 ? "needs-work" : ""}">
                  <strong>Evaluation (Score: ${esc(e.feedback_obj.score)}/10):</strong>
                  <p style="margin:4px 0 0;">${esc(e.feedback_obj.feedback)}</p>
                </div>
              ` : ""}
            </div>
          `).join("")}
        </div>
      </div>
    `;
  }

  const mock = state.mockInterview;
  if (!mock) {
    return `
      <section class="card mock-setup-card">
        <div class="card-heading">
          <div>
            <h2>🎯 Start an Interactive Mock Interview</h2>
            <p>Practice one question at a time, receive instant feedback, and get an extensive performance evaluation.</p>
          </div>
        </div>

        <form id="start-mock-form" class="profile-form">
          <div class="field span-2">
            <label>Target Role or Technology</label>
            <input class="input" name="job_role" id="mock-role-input" list="mock-role-suggestions" placeholder="e.g. Python Developer, Full Stack Engineer, React Specialist" required value="Python Developer" />
            <datalist id="mock-role-suggestions">
              <option value="Python Developer"></option>
              <option value="Frontend Developer (React / JavaScript)"></option>
              <option value="Full Stack Software Engineer"></option>
              <option value="Java Backend Developer"></option>
              <option value="Node.js & FastAPI Backend Developer"></option>
              <option value="Data Scientist & Machine Learning"></option>
              <option value="Cloud & DevOps Engineer"></option>
              <option value="SQL & Database Developer"></option>
            </datalist>
          </div>

          <div class="field">
            <label>Interview Type</label>
            <select class="select" name="interview_type" id="mock-type-select">
              <option value="Technical">Technical (Coding, Systems, Architecture)</option>
              <option value="HR">HR &amp; Behavioral (STAR Method, Communication)</option>
              <option value="Resume-Based" ${state.aiResumeLoaded ? "" : "disabled"}>Resume-Based (From your uploaded projects)${!state.aiResumeLoaded ? " — Upload resume first" : ""}</option>
            </select>
          </div>

          <div class="field">
            <label>Difficulty Level</label>
            <select class="select" name="difficulty">
              <option value="Beginner">Beginner (Fundamentals &amp; Core Concepts)</option>
              <option value="Intermediate" selected>Intermediate (Industry Standard &amp; Problem Solving)</option>
              <option value="Advanced">Advanced (Senior System Design &amp; Edge Cases)</option>
            </select>
          </div>

          <div class="span-2" style="margin-top:10px;">
            <button class="btn btn-primary" type="submit" ${state.mockInterviewBusy ? "disabled" : ""}>
              ${state.mockInterviewBusy ? "Preparing Question 1…" : "Start Interactive Interview →"}
            </button>
          </div>
        </form>

        <div class="mock-disclaimer">
          <strong>ℹ️ Practice Disclaimer:</strong> Mock interview evaluations and scores are practice guidance provided for your personal learning. They do not affect your real job applications, recruiter reviews, or technical test results.
        </div>
      </section>
    `;
  }

  if (mock.is_complete || mock.interview?.status === "Completed") {
    const report = mock.final_report || mock.interview?.final_feedback || {};
    return `
      <div class="card" style="max-width:820px;margin:0 auto;">
        ${renderMockFinalReport(report, mock.interview)}
        <div style="display:flex;gap:12px;margin-top:20px;justify-content:center;">
          <button class="btn btn-primary" data-action="reset-mock-interview">Start Another Mock Interview</button>
          <button class="btn btn-quiet" data-action="switch-ai-tab" data-tab="history">View Past Reports</button>
        </div>
      </div>
    `;
  }

  const currQ = mock.current_question || { question_index: 1, total_questions: 5, question: "" };
  const interviewInfo = mock.interview || {};
  const progressPercent = Math.round((currQ.question_index / currQ.total_questions) * 100);

  return `
    <div class="card" style="max-width:760px;margin:0 auto;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <div style="display:flex;gap:6px;align-items:center;">
          <span class="badge badge-red">${esc(interviewInfo.job_role || "Role")}</span>
          <span class="badge badge-blue">${esc(interviewInfo.interview_type || "Technical")}</span>
          <span class="badge badge-neutral">${esc(interviewInfo.difficulty || "Intermediate")}</span>
        </div>
        <button class="text-button danger-text" data-action="reset-mock-interview">End Interview</button>
      </div>

      <div style="display:flex;justify-content:space-between;font-size:12px;font-weight:700;color:var(--muted);margin-top:12px;">
        <span>Question ${currQ.question_index} of ${currQ.total_questions}</span>
        <span>${progressPercent}% Complete</span>
      </div>
      <div class="mock-progress-bar">
        <div class="mock-progress-fill" style="width:${progressPercent}%"></div>
      </div>

      <div style="background:#faf8f6;border:1px solid var(--line);border-radius:12px;padding:18px;margin:16px 0;">
        <span class="mock-q-badge" style="color:var(--red);">Question ${currQ.question_index}</span>
        <h3 style="margin:8px 0 0;font-size:16px;line-height:1.45;">${esc(currQ.question)}</h3>
      </div>

      ${mock.last_feedback ? `
        <div class="mock-feedback-box ${mock.last_feedback.score < 7 ? "needs-work" : ""}">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <strong>Feedback on Question ${mock.last_answered_index || currQ.question_index - 1}:</strong>
            <span class="badge ${mock.last_feedback.score >= 7 ? "badge-green" : "badge-amber"}">Score: ${esc(mock.last_feedback.score)}/10</span>
          </div>
          <p style="margin:6px 0 0;">${esc(mock.last_feedback.feedback)}</p>
        </div>
      ` : ""}

      <form id="mock-answer-form" style="margin-top:16px;">
        <div class="field">
          <label>Your Answer</label>
          <textarea class="textarea" name="answer" rows="6" placeholder="Take your time to articulate your thoughts clearly. For technical questions, discuss approaches and tradeoffs. For HR questions, use the Situation, Task, Action, Result (STAR) structure." required minlength="5" ${state.mockInterviewBusy ? "disabled" : ""}></textarea>
        </div>
        <div style="display:flex;justify-content:flex-end;margin-top:12px;">
          <button class="btn btn-primary" type="submit" ${state.mockInterviewBusy ? "disabled" : ""}>
            ${state.mockInterviewBusy ? "Evaluating your answer…" : (currQ.question_index === currQ.total_questions ? "Submit Final Answer & Generate Report →" : "Submit Answer & Next Question →")}
          </button>
        </div>
      </form>
    </div>
  `;
}

function renderMockFinalReport(report, interview) {
  const score = report.overall_score ?? 75;
  const level = report.performance_level || "Proficient";
  const strengths = report.strengths || [];
  const improvements = report.areas_for_improvement || [];
  const topics = report.recommended_study_topics || [];
  const questions = report.suggested_practice_questions || [];

  return `
    <div style="display:flex;align-items:center;gap:18px;margin-bottom:16px;">
      <div class="mock-score-circle ${score < 70 ? "needs-work" : ""}">
        ${score}
      </div>
      <div>
        <span class="badge ${score >= 75 ? "badge-green" : "badge-amber"}" style="font-size:11px;">${esc(level)}</span>
        <h2 style="margin:4px 0 2px;">Mock Interview Performance Report</h2>
        <p style="margin:0;color:var(--muted);font-size:12px;">${esc(interview?.job_role || "Practice")} · ${esc(interview?.interview_type || "Technical")} · ${esc(interview?.difficulty || "Intermediate")}</p>
      </div>
    </div>

    ${report.summary ? `<p style="font-size:13px;line-height:1.5;background:#f8fafc;padding:12px 16px;border-radius:10px;border:1px solid #e2e8f0;margin:14px 0;">${esc(report.summary)}</p>` : ""}

    <div class="mock-report-grid">
      <div class="card" style="padding:14px;background:#fcfcfc;">
        <strong style="display:block;font-size:13px;color:var(--ink);margin-bottom:6px;">💻 Technical Knowledge</strong>
        <p style="font-size:12px;color:#475569;margin:0;line-height:1.45;">${esc(report.technical_evaluation || "Solid grasp of foundational topics.")}</p>
      </div>
      <div class="card" style="padding:14px;background:#fcfcfc;">
        <strong style="display:block;font-size:13px;color:var(--ink);margin-bottom:6px;">🗣️ Communication &amp; Structure</strong>
        <p style="font-size:12px;color:#475569;margin:0;line-height:1.45;">${esc(report.communication_evaluation || "Clear delivery and structured reasoning.")}</p>
      </div>
    </div>

    <div class="mock-report-grid">
      <div class="card" style="padding:14px;background:#f0fdf4;border-color:#bbf7d0;">
        <strong style="display:block;font-size:13px;color:#166534;margin-bottom:8px;">✓ Key Strengths</strong>
        <ul class="mock-report-list">
          ${strengths.map(s => `<li><span style="color:#16a34a;">✔</span> <span>${esc(s)}</span></li>`).join("")}
        </ul>
      </div>

      <div class="card" style="padding:14px;background:#fffbeb;border-color:#fde68a;">
        <strong style="display:block;font-size:13px;color:#92400e;margin-bottom:8px;">⚡ Areas for Improvement</strong>
        <ul class="mock-report-list">
          ${improvements.map(i => `<li><span style="color:#d97706;">→</span> <span>${esc(i)}</span></li>`).join("")}
        </ul>
      </div>
    </div>

    ${topics.length ? `
      <div style="margin-top:14px;">
        <strong style="font-size:12px;display:block;margin-bottom:6px;">📚 Recommended Topics to Review:</strong>
        <div style="display:flex;flex-wrap:wrap;gap:6px;">
          ${topics.map(t => `<span class="badge badge-neutral" style="font-size:11px;padding:4px 10px;">${esc(t)}</span>`).join("")}
        </div>
      </div>
    ` : ""}

    ${questions.length ? `
      <div style="margin-top:16px;">
        <strong style="font-size:12px;display:block;margin-bottom:6px;">💡 Suggested Follow-Up Practice Questions:</strong>
        <ul style="margin:4px 0 0 20px;padding:0;font-size:12px;line-height:1.5;">
          ${questions.map(q => `<li style="margin-bottom:4px;">${esc(q)}</li>`).join("")}
        </ul>
      </div>
    ` : ""}

    <div class="mock-disclaimer" style="margin-top:18px;">
      <strong>Practice Disclaimer:</strong> ${esc(report.disclaimer || "Mock interview scores are practice feedback and do not affect actual job applications, recruiter decisions or technical assessment results.")}
    </div>
  `;
}

function renderAiHistoryView() {
  const history = state.mockInterviewsHistory || [];

  return `
    <section class="card">
      <div class="card-heading">
        <div>
          <h2>📜 Mock Interview Practice History</h2>
          <p>Review your previous practice sessions, ratings, and feedback.</p>
        </div>
        <button class="btn btn-quiet btn-small" data-action="refresh-mock-history">↻ Refresh</button>
      </div>

      ${history.length === 0 ? empty("No past mock interviews", "Start a mock interview in the 'Start Mock Interview' tab to test your readiness and track your progress.", "🎯") : `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Target Role</th>
                <th>Type</th>
                <th>Difficulty</th>
                <th>Status</th>
                <th>Overall Score</th>
                <th>Date</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              ${history.map(item => {
                const score = item.final_feedback?.overall_score;
                return `
                  <tr>
                    <td><strong>${esc(item.job_role)}</strong></td>
                    <td><span class="badge ${item.interview_type === "Technical" ? "badge-blue" : "badge-neutral"}">${esc(item.interview_type)}</span></td>
                    <td><span class="badge badge-neutral">${esc(item.difficulty)}</span></td>
                    <td><span class="badge ${item.status === "Completed" ? "badge-green" : "badge-amber"}">${esc(item.status)}</span></td>
                    <td>${score !== undefined ? `<strong style="color:${score >= 75 ? "#16a34a" : "#d97706"};">${score}/100</strong>` : "—"}</td>
                    <td><small>${dateLabel(item.created_at)}</small></td>
                    <td>
                      <button class="btn btn-quiet btn-small" data-action="view-mock-history" data-id="${item.id}">View Report →</button>
                    </td>
                  </tr>
                `;
              }).join("")}
            </tbody>
          </table>
        </div>
      `}
    </section>
  `;
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

function liveAssessmentMonitoringSection(activeList) {
  const items = activeList || [];
  const inProgressCount = items.filter(x => x.assessment_status === "Started").length;

  return `
    <section class="card" style="margin-bottom:16px;">
      <div class="card-heading">
        <div>
          <div style="display:flex;align-items:center;gap:8px;">
            <h2 style="margin:0;">Live Assessment Monitoring</h2>
            ${inProgressCount > 0 ? `<span class="badge badge-green" style="font-size:11px;"><span class="live-dot-pulse"></span> ${inProgressCount} In Progress</span>` : ""}
          </div>
          <p>Monitor candidates currently taking assessments for your roles in real time via live camera streaming</p>
        </div>
        <div style="display:flex;gap:8px;align-items:center;">
          <button class="btn btn-quiet btn-small" data-action="refresh-live-monitoring">↻ Refresh</button>
          <button class="text-button" data-page="Live monitoring">Full view →</button>
        </div>
      </div>

      ${items.length === 0 ? empty("No active assessments", "Candidates currently taking assessments for your jobs will appear here with live camera status.", "◎") : `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Candidate</th>
                <th>Role</th>
                <th>Assessment</th>
                <th>Started</th>
                <th>Assessment Status</th>
                <th>Camera Status</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              ${items.map(item => {
                const isStarted = item.assessment_status === "Started";
                const isConnected = item.camera_status === "Camera Connected";
                return `
                  <tr>
                    <td>
                      <strong>${esc(item.candidate_name)}</strong>
                      <small style="display:block;color:#64748b;">${esc(item.candidate_email)}</small>
                    </td>
                    <td>
                      <span>${esc(item.job_title)}</span>
                      <small style="display:block;color:#64748b;">${esc(item.company)}</small>
                    </td>
                    <td>
                      <span>${esc(item.assessment_name)}</span>
                      <small style="display:block;color:#64748b;">${esc(item.assessment_round || "Round 1")} · ${esc(item.technology || "")}</small>
                    </td>
                    <td>
                      <small>${item.starts_at ? new Date(item.starts_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : "—"}</small>
                    </td>
                    <td>
                      <span class="badge ${isStarted ? 'badge-blue' : 'badge-neutral'}">${esc(item.status)}</span>
                    </td>
                    <td>
                      <span class="badge ${isConnected ? 'badge-green' : item.camera_status === 'Camera Disconnected' ? 'badge-red' : 'badge-amber'}">
                        ${isConnected ? '<span class="live-dot-pulse"></span> ' : ''}${esc(item.camera_status)}
                      </span>
                    </td>
                    <td>
                      ${item.monitoring_available ? `
                        <button class="btn btn-primary btn-small" data-action="view-live-video" data-id="${esc(item.assessment_id)}">
                          ▶ View Live Video
                        </button>
                      ` : `
                        <button class="btn btn-quiet btn-small" disabled title="${esc(item.camera_status)}">
                          View Live Video
                        </button>
                      `}
                    </td>
                  </tr>
                `;
              }).join("")}
            </tbody>
          </table>
        </div>
      `}
    </section>
  `;
}

function liveMonitoringPage() {
  const items = state.liveMonitoringAssessments || [];
  const inProgress = items.filter(x => x.assessment_status === "Started");
  const connected = items.filter(x => x.camera_connected);

  return `${heading("Real-time Proctoring", "Live Assessment Monitoring", "Observe candidates taking assessments for your roles in real-time via peer-to-peer WebRTC live streaming.", `<button class="btn btn-quiet btn-small" data-action="refresh-live-monitoring">↻ Refresh feeds</button>`)}
    <section class="grid stats-grid" style="margin-bottom:16px;">
      ${stat("Active Assessments", inProgress.length, "Candidates currently testing", "◎")}
      ${stat("Cameras Connected", connected.length, "Active camera streams", "◉")}
      ${stat("Total Sessions", items.length, "Recent and active assessments", "✓")}
    </section>

    ${liveAssessmentMonitoringSection(items)}
  `;
}

function recruiterDashboard() {
  const ownJobs = state.jobs;
  const applicants = state.applications;
  const analytics = state.recruiterAnalytics || {};
  const summary = analytics.summary || {};
  const funnel = analytics.conversion_funnel || [];
  const readiness = analytics.question_bank_readiness || {};
  const topSkills = analytics.top_skills || [];

  return `${heading("Your hiring workspace", `Welcome, ${esc(state.user.name.split(" ")[0])}`, "Build your team with people who are ready to make a difference.", `<button class="btn btn-primary" data-action="new-job">＋ Post a role</button>`)}
    <section class="grid stats-grid">
      ${stat("Open roles", summary.total_jobs ?? ownJobs.length, "Roles published by your team", "▣")}
      ${stat("Applicants", summary.total_applications ?? applicants.length, "People interested in your roles", "♙")}
      ${stat("Assessments passed", summary.assessments_completed ?? 0, "Technical tests completed", "✓")}
      ${stat("Interviews scheduled", summary.interviews_scheduled ?? 0, "Conversations arranged", "◷")}
    </section>

    ${liveAssessmentMonitoringSection(state.liveMonitoringAssessments)}

    <section class="card" style="margin-bottom:16px">
      <div class="card-heading">
        <div>
          <h2>Hiring Pipeline & Funnel</h2>
          <p>Track candidate progression from initial application to offer</p>
        </div>
        <div class="report-range">
          <label class="sr-only" for="recruiter-analytics-range">Period</label>
          <select class="select" id="recruiter-analytics-range">
            <option value="this_week" ${state.recruiterAnalyticsRange === "this_week" ? "selected" : ""}>This week</option>
            <option value="last_week" ${state.recruiterAnalyticsRange === "last_week" ? "selected" : ""}>Last week</option>
            <option value="this_month" ${state.recruiterAnalyticsRange === "this_month" ? "selected" : ""}>This month</option>
            <option value="last_month" ${state.recruiterAnalyticsRange === "last_month" ? "selected" : ""}>Last month</option>
            <option value="all" ${state.recruiterAnalyticsRange === "all" ? "selected" : ""}>All time</option>
          </select>
        </div>
      </div>

      ${readiness.incomplete_jobs > 0 ? `
        <div class="recommendation-list" style="margin-bottom:14px">
          <strong style="color:#b45309">⚠️ Question Bank Attention Needed:</strong>
          <span>${readiness.incomplete_jobs} role(s) need additional active questions before assessments can be dispatched automatically.</span>
          <button class="text-button" style="display:inline-block;margin-left:8px" data-page="Question bank">Manage Question Banks →</button>
        </div>
      ` : ""}

      <div class="funnel-container">
        ${funnel.map(step => `
          <div class="funnel-step">
            <div class="funnel-step-left">
              <span>${esc(step.stage)}</span>
            </div>
            <div class="funnel-step-right">
              <strong>${step.count} candidates</strong>
              <span>(${step.conversion_rate}%)</span>
            </div>
          </div>
        `).join("")}
      </div>

      ${topSkills.length ? `
        <div style="margin-top:12px">
          <strong style="font-size:11px;color:var(--muted);display:block;margin-bottom:6px">Most Common Candidate Skills:</strong>
          <div class="skills">
            ${topSkills.map(sk => `<span class="skill">${esc(sk.skill)} (${sk.count})</span>`).join("")}
          </div>
        </div>
      ` : ""}
    </section>

    <section class="columns">
      <article class="card">
        <div class="card-heading">
          <div><h2>Your open roles</h2><p>Manage the opportunities you’ve shared</p></div>
          <button class="text-button" data-page="My jobs">Manage →</button>
        </div>
        <div class="job-list">${ownJobs.slice(0, 4).map(job => recruiterJobRow(job)).join("") || empty("No roles posted", "Create your first role to meet great candidates.")}</div>
      </article>
      <article class="card">
        <div class="card-heading"><div><h2>Recent applicants</h2><p>A pulse check on your team’s pipeline</p></div></div>
        <div class="application-list">${applicants.slice(0, 4).map(item => `<div class="application-row"><div><strong>${esc(item.candidate_name)}</strong><small>${esc(item.title)}</small></div><span class="badge badge-red">${esc(item.status)}</span></div>`).join("") || empty("Your applicant list is quiet", "New applicants will show up here.", "♙")}</div>
      </article>
    </section>`;
}

function recruiterJobsPage() {
  return `${heading("Manage your openings", "My jobs", "Create, update, and keep track of the roles you’ve posted.", `<button class="btn btn-primary" data-action="new-job">＋ Post a role</button>`)}
    <section class="card"><div class="card-heading"><div><h2>Published roles</h2><p>${state.jobs.length} open opportunit${state.jobs.length === 1 ? "y" : "ies"}</p></div></div>
    <div class="job-list">${state.jobs.length ? state.jobs.map(job => recruiterJobRow(job)).join("") : empty("Nothing posted yet", "Post a role and find the right person for your team.")}</div></section>`;
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
            : assessmentStatus === "Started" ? `<button class="text-button" data-action="view-live-video" data-id="${esc(item.assessment_id || item.id)}" style="color:#16a34a;font-weight:600;">◎ Live Video</button>`
              : ["Completed", "Pending Recruiter Decision"].includes(assessmentStatus) ? (item.status.startsWith("Round 1 ") ? `<span class="badge ${item.status === 'Round 1 Passed' ? 'badge-green' : 'badge-red'}">${esc(item.status)}</span>` : `<span class="badge badge-amber">Pending Decision</span>`)
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
      return `<tr><td><strong>${esc(item.candidate_name)}</strong><small>${esc(item.candidate_email)}</small></td><td>${esc(item.title)}</td><td><strong>${item.match_percentage == null ? "—" : `${esc(item.match_percentage)}%`}</strong><details class="skill-details"><summary>Skills</summary><small>Matched: ${esc((item.matched_skills || []).join(", ") || "None")}</small><small>Missing: ${esc((item.missing_skills || []).join(", ") || "None")}</small></details></td><td>${dateLabel(item.applied_at)}</td><td><button class="text-button" data-action="download-resume" data-id="${esc(item.id)}">Open resume ↗</button></td><td><select class="status-select" data-status="${esc(item.id)}" aria-label="Update status for ${esc(item.candidate_name)}">${statusOptions(item.status).map(status => `<option ${status === (item.status === "Selected" ? "Accepted" : item.status) ? "selected" : ""}>${esc(status)}</option>`).join("")}</select></td><td>${assessmentAction}</td><td>${topic}</td><td>${item.assessment_email_sent_at ? dateLabel(item.assessment_email_sent_at) : "—"}</td><td>${item.assessment_score == null ? "—" : `${esc(item.assessment_score)}/20`}${item.assessment_result ? `<small>${esc(item.assessment_score_percentage)}% · ${esc(item.assessment_result)}</small>` : ""}</td><td>${item.assessment_submitted_at ? dateLabel(item.assessment_submitted_at) : "—"}</td><td><div class="inline-actions"><button class="text-button" data-action="view-ai-summary" data-id="${esc(item.id)}" title="Synthesize AI screening evaluation">⚡ AI Summary</button><button class="text-button" data-action="application-history" data-id="${esc(item.id)}">History</button><button class="text-button" data-action="edit-note" data-id="${esc(item.id)}">Notes</button>${item.assessment_id && ["Completed", "Pending Recruiter Decision"].includes(assessmentStatus) ? `<button class="text-button" data-action="review-assessment" data-id="${esc(item.assessment_id)}">Review & Decide</button>` : ""}${interviewAction}</div></td></tr>`;
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
  return `<section class="card admin-proctoring"><div class="card-heading"><div><h2>Assessment integrity & recordings</h2><p>Review candidate browser events, face-presence logs, and verified webcam recordings.</p></div><div style="display:flex;gap:8px;align-items:center"><button class="btn btn-quiet btn-small" data-action="admin-retention-cleanup" title="Purge recordings older than retention policy">🧹 Retention cleanup</button><span class="badge ${items.some(item => item.flagged) ? "badge-amber" : "badge-green"}">${items.filter(item => item.flagged).length} flagged</span></div></div>
    <form id="admin-proctoring-filter-form" class="filters proctoring-filters">
      <input class="input" name="candidate" value="${esc(filters.candidate)}" placeholder="Candidate name or email" aria-label="Filter by candidate" />
      <select class="select" name="job_id" aria-label="Filter by job"><option value="">All jobs</option>${state.adminJobs.map(job => `<option value="${esc(job.id)}" ${String(filters.job_id) === String(job.id) ? "selected" : ""}>${esc(job.title)}</option>`).join("")}</select>
      <input class="input" type="date" name="start_date" value="${esc(filters.start_date)}" aria-label="Start date" />
      <input class="input" type="date" name="end_date" value="${esc(filters.end_date)}" aria-label="End date" />
      <select class="select" name="flagged_only" aria-label="Filter by flag status"><option value="">All records</option><option value="true" ${filters.flagged_only === "true" ? "selected" : ""}>Flagged only</option><option value="false" ${filters.flagged_only === "false" ? "selected" : ""}>Not flagged</option></select>
      <button class="btn btn-quiet btn-small" type="submit">Apply filters</button>
    </form>
    ${items.length ? `<div class="table-wrap"><table><thead><tr><th>Candidate</th><th>Job and recruiter</th><th>Started</th><th>Warnings</th><th>Review status</th><th>Webcam Video</th><th>Timeline</th><th>Action</th></tr></thead><tbody>${items.map(item => {
      const recStatus = item.recording_status;
      const recBadge = recStatus === "Uploaded"
        ? `<span class="badge badge-green">📹 Ready ${item.recording_duration ? `(${item.recording_duration}s)` : ""}</span>`
        : recStatus === "Uploading" || recStatus === "Pending"
          ? `<span class="badge badge-amber">⏳ ${recStatus}</span>`
          : recStatus === "Failed"
            ? `<span class="badge badge-red" title="${esc(item.recording_error || "Upload failed")}">⚠️ Failed</span>`
            : `<span class="badge" style="background:#f3f4f6;color:#6b7280">—</span>`;
      return `<tr>
        <td><strong>${esc(item.candidate_name)}</strong><small>${esc(item.candidate_email)}</small></td>
        <td>${esc(item.title)}<small>${esc(item.company)} · ${esc(item.recruiter_name)}</small></td>
        <td>${dateTimeLabel(item.proctoring_started_at)}</td>
        <td>${esc(item.proctoring_warning_count)}</td>
        <td><span class="badge ${item.flagged ? "badge-amber" : "badge-green"}">${item.flagged ? "Flagged for review" : esc(item.status)}</span></td>
        <td>${recBadge}</td>
        <td><details class="proctoring-timeline"><summary>${item.events.length} event${item.events.length === 1 ? "" : "s"}</summary>${proctoringEventTimeline(item.events)}</details></td>
        <td>${item.recording_id && recStatus === "Uploaded"
          ? `<button class="btn btn-quiet btn-small" data-action="view-recording" data-id="${esc(item.recording_id)}">▶ Play Video</button>`
          : recStatus === "Failed"
            ? `<small style="color:#b91c1c">${esc(item.recording_error || "Failed")}</small>`
            : `<small style="color:#9ca3af">—</small>`}</td>
      </tr>`;
    }).join("")}</tbody></table></div>` : empty("No proctoring records found", "Assessment monitoring events will appear here when available.")}
  </section>`;
}

function adminDashboard() {
  const data = state.analytics || {};
  const weekly = data.weekly || {};
  const peak = Math.max(1, ...(weekly.daily_trend || []).map(day => day.applications));
  return `${heading("Platform overview", "The whole picture", "A live snapshot of the SkillSphere community.")}
    <section class="grid stats-grid">${stat("People", data.users ?? "—", "Registered members", "♙")}${stat("Recruiters", data.recruiters ?? "—", "Teams hiring on SkillSphere", "◉")}${stat("Open roles", data.open_jobs ?? "—", "Published opportunities", "▣")}${stat("Applications", data.applications ?? "—", "Connections in motion", "↗")}</section>
    <section class="card weekly-report"><div class="card-heading"><div><h2>Weekly hiring report</h2><p>${esc(weekly.start_date || "")} – ${esc(weekly.end_date || "")}</p></div><div class="report-range">
      <label class="sr-only" for="analytics-range">Report period</label><select class="select" id="analytics-range"><option value="this_week" ${state.analyticsRange === "this_week" ? "selected" : ""}>This week</option><option value="last_week" ${state.analyticsRange === "last_week" ? "selected" : ""}>Last week</option><option value="this_month" ${state.analyticsRange === "this_month" ? "selected" : ""}>This month</option><option value="last_month" ${state.analyticsRange === "last_month" ? "selected" : ""}>Last month</option><option value="custom" ${state.analyticsRange === "custom" ? "selected" : ""}>Custom</option></select>
      ${state.analyticsRange === "custom" ? `<input class="input report-date" id="analytics-start" type="date" value="${esc(state.analyticsStart)}" aria-label="Report start date" /><input class="input report-date" id="analytics-end" type="date" value="${esc(state.analyticsEnd)}" aria-label="Report end date" /><button class="btn btn-quiet btn-small" data-action="apply-custom-report">Apply</button>` : ""}
    </div></div>
      <div class="grid stats-grid report-stats">${stat("Applications", weekly.applications ?? 0, "In selected period", "↗")}${stat("Assessments sent", weekly.assessments_sent ?? 0, "Email delivery count", "✉")}${stat("Completion rate", `${weekly.completion_rate ?? 0}%`, "Of assessments sent in period", "✓")}${stat("Average score", weekly.average_score_percentage == null ? "—" : `${weekly.average_score_percentage}%`, "Completed assessments", "◉")}</div>
      <h3 class="report-subhead">Round 1 results</h3>
      <div class="grid stats-grid report-stats">${stat("Completed", weekly.round1_assessments_completed ?? 0, "Round 1 this period", "✓")}${stat("Passed", weekly.round1_passed ?? 0, "Candidates cleared Round 1", "↑")}${stat("Failed", weekly.round1_failed ?? 0, "Candidates below threshold", "↓")}${stat("Pass rate", `${weekly.round1_pass_rate ?? 0}%`, "Of completed Round 1 tests", "◉")}${stat("Average Round 1 score", weekly.round1_average_score_percentage == null ? "—" : `${weekly.round1_average_score_percentage}%`, "Score percentage", "★")}</div>
      ${(weekly.conversion_funnel || []).length ? `
        <h3 class="report-subhead">Platform Hiring Funnel</h3>
        <div class="funnel-container">
          ${weekly.conversion_funnel.map(step => `
            <div class="funnel-step">
              <div class="funnel-step-left"><span>${esc(step.stage)}</span></div>
              <div class="funnel-step-right"><strong>${step.count} candidates</strong><span>(${step.conversion_rate}%)</span></div>
            </div>
          `).join("")}
        </div>
      ` : ""}
      ${(weekly.weak_technologies || []).length ? `
        <h3 class="report-subhead">Most Challenging Technologies (Highest Candidate Fails)</h3>
        <div class="chips-row">
          ${weekly.weak_technologies.map(t => `<span class="chip-amber">▲ ${esc(t.technology)}: ${t.failed_count} failure(s)</span>`).join("")}
        </div>
      ` : ""}
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
    if (state.page === "Live monitoring") return liveMonitoringPage();
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
    if (state.page === "Live monitoring") return liveMonitoringPage();
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
  if (state.page === "Career Assistant" || state.page === "AI Career Assistant") return aiAssistantPage();
  return candidateDashboard();
}

function renderModal() {
  if (state.modal === "new-job") return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <div class="modal-head"><div><span class="eyebrow">Grow your team</span><h2 id="modal-title">Post a new role</h2><p>Share the details that help the right people find you.</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
    <form id="job-form" class="profile-form"><div class="field span-2"><label>Role title</label><input class="input" name="title" placeholder="e.g. Product Designer" required minlength="2" maxlength="120" /></div>
    <div class="field"><label>Company</label><input class="input" name="company" placeholder="Your company" required minlength="2" maxlength="100" /></div><div class="field"><label>Location</label><input class="input" name="location" placeholder="Remote, New York..." required minlength="2" maxlength="100" /></div>
    <div class="field"><label>Experience</label><input class="input" name="experience" placeholder="e.g. 2–4 years" required minlength="2" maxlength="80" /></div><div class="field"><label>Job type</label><select class="select" name="job_type"><option>Full-time</option><option>Part-time</option><option>Contract</option><option>Remote</option></select></div>
    <div class="field span-2"><label>Salary range</label><input class="input" name="salary" placeholder="e.g. $90k – $120k" maxlength="80" /></div>
    <div class="field span-2"><label>Required skills, comma-separated</label><input class="input" name="skills" placeholder="e.g. Python, SQL, Docker" required /></div>
    <div class="field"><label>Assessment questions (5–50)</label><input class="input" type="number" name="assessment_question_count" min="5" max="50" value="20" required /></div>
    <div class="field"><label>Candidate results</label><label class="checkbox-field" style="display:flex;align-items:center;gap:8px;font-size:11px;margin-top:6px;cursor:pointer;"><input type="checkbox" name="allow_detailed_results" checked /> Allow detailed question explanations after test</label></div>
    <div class="field span-2"><label>Role description (minimum 20 characters)</label><textarea class="textarea" name="description" required minlength="20" placeholder="Describe the role responsibilities and requirements (at least 20 characters)..."></textarea></div>
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
      ${["recruiter", "admin"].includes(state.user.role) ? `<div style="margin-top:20px;padding-top:12px;border-top:1px solid #e2e8f0;display:flex;justify-content:space-between;align-items:center;"><button class="btn btn-quiet btn-small" data-action="manage-question-bank" data-id="${esc(job.id)}">Question Bank</button><button class="btn btn-danger btn-small" data-action="delete-job" data-id="${esc(job.id)}" data-title="${esc(job.title)}" style="color:#dc2626;border-color:#fca5a5;background:transparent;cursor:pointer;">Delete job posting</button></div>` : ""}
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
    if (!review) {
      return `<div class="modal-backdrop" data-action="close-modal"><section class="modal review-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <div class="modal-head"><div><span class="eyebrow">Assessment review</span><h2 id="modal-title">Loading assessment…</h2></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
        <div class="loading"><span class="spinner"></span></div>
      </section></div>`;
    }
    const topicBreakdown = review.topic_breakdown || [];
    const diffBreakdown = review.difficulty_breakdown || {};
    const strengths = review.strengths || [];
    const weakAreas = review.areas_to_improve || [];
    const recommendations = review.recommendations || [];
    const questions = review.questions || [];
    const isCandidate = state.user?.role === "candidate";

    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal review-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head">
        <div>
          <span class="eyebrow">${esc(review.company)} · ${esc(review.title)}</span>
          <h2 id="modal-title">${isCandidate ? "Your Performance Review" : `${esc(review.candidate_name)} · Review`}</h2>
          <p>${esc(review.assessment_name || "Round 1: Technical MCQ Test")}${review.technology ? ` · ${esc(review.technology)}` : ""}</p>
        </div>
        <button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button>
      </div>

      <div class="assessment-review-summary">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <div>
            <strong>${esc(review.score_percentage ?? 0)}%</strong>
            <span class="badge ${review.result === "Passed" ? "badge-green" : "badge-red"}" style="margin-left:6px">${esc(review.result || "Completed")}</span>
          </div>
          <span style="font-size:11px">${esc(review.correct_answer_count ?? review.score)} correct / ${esc(review.total_questions)} total · Pass requirement: ${esc(review.pass_percentage)}%</span>
        </div>
        <div class="review-stat-grid">
          <div class="review-stat-card"><span>Completion date</span><strong>${dateLabel(review.submitted_at)}</strong></div>
          <div class="review-stat-card"><span>Time taken</span><strong>${review.time_taken_seconds != null ? `${review.time_taken_seconds}s` : "—"}</strong></div>
          <div class="review-stat-card"><span>Correct answers</span><strong style="color:#2f855a">${esc(review.correct_answer_count ?? review.score)}</strong></div>
          <div class="review-stat-card"><span>Incorrect answers</span><strong style="color:#c53030">${esc(review.incorrect_answer_count ?? (review.total_questions - (review.score || 0)))}</strong></div>
        </div>
      </div>

      <div class="recruiter-decision-panel" style="margin:16px 0;padding:16px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
          <div>
            <h3 style="margin:0 0 4px;font-size:14px;color:#0f172a;">Recruiter Final Decision</h3>
            ${review.recruiter_decision ? `
              <p style="margin:0;font-size:12px;color:#475569;">
                Status: <span class="badge ${review.recruiter_decision === 'Passed' ? 'badge-green' : 'badge-red'}">${esc(review.recruiter_decision)}</span>
                ${review.recruiter_decision_at ? ` · Decided ${dateTimeLabel(review.recruiter_decision_at)}` : ''}
                ${review.recruiter_decision_notes ? ` · Note: "${esc(review.recruiter_decision_notes)}"` : ''}
              </p>
            ` : `
              <p style="margin:0;font-size:12px;color:#d97706;">
                <span class="badge badge-amber">Pending Recruiter Decision</span>
                Automatic evaluation completed. Review the results below and manually Pass or Fail this candidate.
              </p>
            `}
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button class="btn btn-small" data-action="make-decision" data-id="${esc(review.id)}" data-decision="Pass" style="background:#16a34a;border-color:#16a34a;color:#fff;cursor:pointer;">
              ✓ Pass Candidate
            </button>
            <button class="btn btn-small" data-action="make-decision" data-id="${esc(review.id)}" data-decision="Fail" style="background:#dc2626;border-color:#dc2626;color:#fff;cursor:pointer;">
              ✕ Fail Candidate
            </button>
          </div>
        </div>
      </div>

      <section class="skill-analysis-card">
        <h3>Strengths & Areas to Improve</h3>
        <p>A skill-gap diagnostic based on candidate answers across tested technologies.</p>
        
        <strong style="font-size:11px;display:block;margin-top:6px">Demonstrated Strengths (Score ≥ 70%)</strong>
        <div class="chips-row">
          ${strengths.length ? strengths.map(s => `<span class="chip-green">★ Strong in ${esc(s)}</span>`).join("") : `<span class="stat-note">Keep practicing to build your core strengths.</span>`}
        </div>

        <strong style="font-size:11px;display:block;margin-top:8px">Areas for Growth (Score &lt; 70%)</strong>
        <div class="chips-row">
          ${weakAreas.length ? weakAreas.map(w => `<span class="chip-amber">▲ Needs improvement: ${esc(w)}</span>`).join("") : `<span class="chip-green">✓ Great foundation across all topics!</span>`}
        </div>

        ${topicBreakdown.length ? `
          <strong style="font-size:11px;display:block;margin-top:10px">Topic-Level Breakdown</strong>
          <div class="topic-bars-list">
            ${topicBreakdown.map(t => `
              <div class="topic-bar-row">
                <span class="topic-bar-name">${esc(t.technology)}</span>
                <div class="topic-bar-track">
                  <div class="topic-bar-fill ${t.percentage >= 70 ? "topic-bar-fill-high" : t.percentage >= 50 ? "topic-bar-fill-mid" : "topic-bar-fill-low"}" style="width:${Math.max(4, t.percentage)}%"></div>
                </div>
                <span class="topic-bar-score">${t.correct}/${t.total} (${t.percentage}%)</span>
              </div>
            `).join("")}
          </div>
        ` : ""}

        <strong style="font-size:11px;display:block;margin-top:10px">Difficulty Breakdown</strong>
        <div style="display:flex;gap:12px;margin-top:4px;flex-wrap:wrap">
          ${["Easy", "Medium", "Hard"].map(level => {
            const d = diffBreakdown[level];
            if (!d || !d.total) return "";
            return `<span style="font-size:11px;color:var(--muted)"><strong>${level}:</strong> ${d.correct}/${d.total} (${d.percentage}%)</span>`;
          }).join("")}
        </div>

        ${recommendations.length ? `
          <div class="recommendation-list">
            <strong style="font-size:11px;color:#78350f;display:block;margin-bottom:4px">Practical Recommendations:</strong>
            <ul>
              ${recommendations.map(r => `<li>${esc(r)}</li>`).join("")}
            </ul>
          </div>
        ` : ""}
      </section>

      ${state.assessmentProctoringReview ? `
        <section class="proctoring-review">
          <h3>Proctoring review${state.assessmentProctoringReview.flagged ? ` · <span class="badge badge-amber">Flagged for review</span>` : ""}</h3>
          <p>${esc(state.assessmentProctoringReview.proctoring_warning_count)} warning(s) recorded. Events are review signals, not proof of misconduct.</p>
          ${proctoringEventTimeline(state.assessmentProctoringReview.events)}
        </section>
      ` : ""}

      ${!isCandidate && review.application_id ? `
        <div style="margin-top:14px;padding:12px 14px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
          <div>
            <strong style="color:#166534;font-size:12px;display:block">⚡ AI Screening Summary & Interview Guide</strong>
            <p style="margin:2px 0 0;font-size:11px;color:#15803d">Synthesized evaluation combining resume fit, Round 1 test accuracy, and proctoring integrity.</p>
          </div>
          <button class="btn btn-primary btn-small" data-action="view-ai-summary" data-id="${esc(review.application_id)}">Open AI Summary →</button>
        </div>
      ` : ""}

      <section style="margin-top:14px">
        <div class="card-heading">
          <div>
            <h3 style="font-size:12px;margin:0">Question-by-Question Review</h3>
            <p style="font-size:11px;color:var(--muted)">Review candidate selections${review.can_view_detailed_answers ? " and correct answers" : ""}.</p>
          </div>
        </div>
        <div class="review-questions">
          ${questions.map(q => {
            const isCorrect = q.is_correct === true;
            return `<article class="review-question">
              <div class="review-question-header">
                <div>
                  <span class="eyebrow">${esc(q.technology || "Topic")} · ${esc(q.difficulty || "Medium")}${q.subtopic ? ` · ${esc(q.subtopic)}` : ""}</span>
                  <strong>${q.order}. ${esc(q.question)}</strong>
                </div>
                <span class="badge ${isCorrect ? "badge-green" : "badge-red"}">${isCorrect ? "Correct" : "Incorrect"}</span>
              </div>
              <p class="${isCorrect ? "answer-correct" : "answer-wrong"}">
                ${q.selected_answer == null ? "Candidate did not answer" : `Selected: ${esc(q.options[q.selected_answer])}`}
              </p>
              ${q.correct_answer != null ? `
                <small style="color:#2f855a;font-weight:600">Correct answer: ${esc(q.options[q.correct_answer])}</small>
                ${q.explanation ? `<div class="review-explanation"><strong>Explanation:</strong> ${esc(q.explanation)}</div>` : ""}
                ${q.learning_resource_url ? `<a class="learning-link" href="${esc(q.learning_resource_url)}" target="_blank" rel="noopener noreferrer">Study Resource ↗</a>` : ""}
              ` : `
                ${!isCorrect ? `<div class="review-hidden-notice">Detailed answer explanations for this test are hidden by the recruiter. Review the recommendations above for guidance.</div>` : ""}
              `}
            </article>`;
          }).join("")}
        </div>
      </section>
    </section></div>`;
  }
  if (state.modal === "ai-questions") {
    const job = state.questionBankJob;
    const isGenerating = state.aiGenerating;
    const questions = state.aiGeneratedQuestions || [];
    const suggestedTech = (job?.skills && job.skills[0]) || "Python";
    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal ai-modal-wide" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head">
        <div>
          <span class="eyebrow">⚡ AI Assessment Builder · ${esc(job?.title || "Job")}</span>
          <h2 id="modal-title">AI Question Generator</h2>
          <p>Generate high-quality technical MCQs powered by Gemini / OpenAI or smart local generation.</p>
        </div>
        <button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button>
      </div>

      <form id="ai-question-generate-form">
        <div class="ai-generator-form">
          <div class="field">
            <label>Technology / Skill Topic</label>
            <input class="input" name="technology" list="ai-tech-suggestions" value="${esc(suggestedTech)}" required placeholder="e.g. Python, SQL, React" ${isGenerating ? "disabled" : ""} />
            <datalist id="ai-tech-suggestions">
              ${(job?.skills || []).map(s => `<option value="${esc(s)}"></option>`).join("")}
              <option value="Python"></option><option value="JavaScript"></option><option value="React"></option><option value="SQL"></option><option value="Java"></option><option value="TypeScript"></option><option value="Docker"></option><option value="AWS"></option>
            </datalist>
          </div>
          <div class="field">
            <label>Subtopic / Domain (Optional)</label>
            <input class="input" name="subtopic" placeholder="e.g. Joins, Async/Await, Hooks" ${isGenerating ? "disabled" : ""} />
          </div>
          <div class="field">
            <label>Difficulty Level</label>
            <select class="select" name="difficulty" ${isGenerating ? "disabled" : ""}>
              <option value="Mixed">Mixed (Easy, Medium, Hard)</option>
              <option value="Easy">Easy</option>
              <option value="Medium">Medium</option>
              <option value="Hard">Hard</option>
            </select>
          </div>
          <div class="field">
            <label>Question Count (1–20)</label>
            <select class="select" name="count" ${isGenerating ? "disabled" : ""}>
              <option value="5" selected>5 Questions</option>
              <option value="10">10 Questions</option>
              <option value="15">15 Questions</option>
              <option value="20">20 Questions (Complete Bank)</option>
            </select>
          </div>
          <div class="field span-2">
            <label>Custom Instructions / Context (Optional)</label>
            <textarea class="textarea" name="custom_prompt" rows="2" placeholder="e.g. Focus on practical debugging scenarios, common production mistakes, and clean code principles." ${isGenerating ? "disabled" : ""}></textarea>
          </div>
        </div>
        <div style="display:flex;justify-content:flex-end;gap:10px;align-items:center">
          <button class="btn btn-primary" type="submit" ${isGenerating ? "disabled" : ""}>
            ${isGenerating ? `<span class="spinner" style="width:14px;height:14px;border-width:2px;display:inline-block"></span> Generating questions…` : `✨ Generate Questions →`}
          </button>
        </div>
      </form>

      ${questions.length ? `
        <div class="ai-preview-container">
          <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--line)">
            <div>
              <strong style="font-size:13px">${questions.length} Generated Questions Ready for Review</strong>
              <small style="display:block;color:var(--muted);font-size:10px">Review, tweak, or discard before saving to the question bank.</small>
            </div>
            <button class="btn btn-primary btn-small" data-action="save-ai-questions">✓ Save All (${questions.length}) to Bank →</button>
          </div>

          ${questions.map((q, idx) => `
            <article class="ai-question-card" data-ai-question-index="${idx}">
              <div class="ai-question-card-head">
                <div class="ai-question-badges">
                  <span class="eyebrow" style="font-size:10px">Q${idx + 1}</span>
                  <span class="badge badge-amber">${esc(q.technology)}</span>
                  <span class="badge ${q.difficulty === "Easy" ? "badge-green" : q.difficulty === "Hard" ? "badge-red" : "badge-amber"}">${esc(q.difficulty)}</span>
                  ${q.subtopic ? `<span class="chip-green" style="font-size:9px;padding:2px 6px">${esc(q.subtopic)}</span>` : ""}
                </div>
                <button class="text-button danger-text" data-action="remove-ai-question" data-index="${idx}" title="Discard this question">Discard ✕</button>
              </div>

              <div class="field" style="margin:0">
                <label style="font-size:10px">Question Prompt</label>
                <textarea class="textarea ai-q-prompt" data-idx="${idx}" style="min-height:55px;font-size:11px" required>${esc(q.question)}</textarea>
              </div>

              <div class="ai-options-grid">
                ${(q.options || []).map((opt, optIdx) => `
                  <div class="ai-option-item">
                    <input type="radio" name="ai_correct_${idx}" data-idx="${idx}" value="${optIdx}" ${optIdx === q.correct_index ? "checked" : ""} title="Mark as correct answer" />
                    <input class="input ai-q-option" data-idx="${idx}" data-opt-idx="${optIdx}" value="${esc(opt)}" style="font-size:11px;padding:5px 8px" placeholder="Option ${String.fromCharCode(65 + optIdx)}" required />
                  </div>
                `).join("")}
              </div>

              <div class="field" style="margin:0">
                <label style="font-size:10px">Explanation (Shown in performance review after submission)</label>
                <input class="input ai-q-explanation" data-idx="${idx}" value="${esc(q.explanation || "")}" placeholder="Explanation for correct answer" style="font-size:11px;padding:5px 8px" />
              </div>
            </article>
          `).join("")}

          <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 0 4px;border-top:1px solid var(--line);margin-top:8px">
            <button class="btn btn-quiet btn-small" data-action="clear-ai-questions">Discard All</button>
            <button class="btn btn-primary" data-action="save-ai-questions">✓ Save All (${questions.length}) to Question Bank →</button>
          </div>
        </div>
      ` : ""}
    </section></div>`;
  }
  if (state.modal?.startsWith("ai-summary:")) {
    const loading = state.aiSummaryLoading;
    const summary = state.aiSummaryData;
    const appId = state.modal.slice("ai-summary:".length);

    if (loading || !summary) {
      return `<div class="modal-backdrop" data-action="close-modal"><section class="modal ai-modal-wide" role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <div class="modal-head">
          <div>
            <span class="eyebrow">⚡ AI Screening Summary</span>
            <h2 id="modal-title">Analyzing Candidate Profile…</h2>
            <p>Synthesizing resume qualifications, Round 1 test performance, and proctoring logs.</p>
          </div>
          <button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button>
        </div>
        <div class="loading" style="padding:48px 0"><span class="spinner"></span><p style="margin-top:14px;color:var(--muted);font-size:12px">Generating AI executive evaluation & interview guide…</p></div>
      </section></div>`;
    }

    const recClass = summary.recommendation === "Strong Hire" ? "ai-rec-strong"
      : summary.recommendation === "Recommended" ? "ai-rec-recommended"
      : summary.recommendation === "Borderline" ? "ai-rec-borderline"
      : "ai-rec-not-recommended";

    const recIcon = summary.recommendation === "Strong Hire" ? "★"
      : summary.recommendation === "Recommended" ? "✓"
      : summary.recommendation === "Borderline" ? "⚠"
      : "✕";

    const strengths = summary.strengths || [];
    const risks = summary.risks || [];
    const questions = summary.interview_questions || [];

    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal ai-modal-wide" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head">
        <div>
          <span class="eyebrow">⚡ Recruiter AI Evaluation · ${esc(summary.company)}</span>
          <h2 id="modal-title">${esc(summary.candidate_name)}</h2>
          <p>${esc(summary.job_title)} · Synthesized candidate screening report</p>
        </div>
        <button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button>
      </div>

      <div class="ai-rec-banner ${recClass}">
        <div>
          <span style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;display:block;opacity:0.85">AI Recommendation</span>
          <strong style="font-size:17px;letter-spacing:-0.3px">${recIcon} ${esc(summary.recommendation)}</strong>
        </div>
        <span class="ai-rec-badge">${esc(summary.recommendation)}</span>
      </div>

      <div class="ai-summary-metrics">
        <div class="ai-summary-metric-card">
          <small>Resume Match</small>
          <strong>${summary.resume_match_percentage == null ? "—" : `${summary.resume_match_percentage}%`}</strong>
        </div>
        <div class="ai-summary-metric-card">
          <small>Round 1 Score</small>
          <strong>${summary.assessment_score_percentage == null ? "Pending" : `${summary.assessment_score_percentage}%`}</strong>
        </div>
        <div class="ai-summary-metric-card">
          <small>Test Status</small>
          <strong>${esc(summary.assessment_result || summary.assessment_status || "Not Started")}</strong>
        </div>
        <div class="ai-summary-metric-card">
          <small>Proctoring</small>
          <strong style="color:${summary.proctoring_flagged ? "#c53030" : "#2f855a"}">${summary.proctoring_flagged ? `Flagged (${summary.proctoring_warning_count})` : `${summary.proctoring_warning_count || 0} warnings`}</strong>
        </div>
      </div>

      <div class="ai-exec-summary">
        <strong style="display:block;margin-bottom:4px;font-size:11px;color:var(--red-dark);text-transform:uppercase;letter-spacing:0.6px">Executive Screening Summary</strong>
        ${esc(summary.summary)}
      </div>

      <div class="ai-insights-grid">
        <div class="ai-insight-box strengths">
          <h4><span>★</span> Key Candidate Strengths</h4>
          <ul>
            ${strengths.length ? strengths.map(s => `<li>${esc(s)}</li>`).join("") : `<li>Profile meets baseline requirements.</li>`}
          </ul>
        </div>
        <div class="ai-insight-box risks">
          <h4><span>⚠️</span> Risk Flags & Skill Gaps</h4>
          <ul>
            ${risks.length ? risks.map(r => `<li>${esc(r)}</li>`).join("") : `<li>No critical risk flags detected.</li>`}
          </ul>
        </div>
      </div>

      <div class="ai-interview-questions-section">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div>
            <h3 style="font-size:13px;margin:0">🎯 Targeted Round 2 Interview Questions</h3>
            <p style="font-size:11px;color:var(--muted);margin:2px 0 0">Customized questions to probe weak areas and verify practical depth.</p>
          </div>
          <span class="badge badge-green" style="font-size:9px">${questions.length} Questions</span>
        </div>

        ${questions.map((item, qIdx) => `
          <div class="ai-interview-question-card">
            <div class="ai-interview-question-head">
              <span class="eyebrow" style="font-size:9px">Question ${qIdx + 1}${item.focus_area ? ` · ${esc(item.focus_area)}` : ""}</span>
            </div>
            <div class="ai-interview-question-text">${esc(item.question)}</div>
            ${item.what_to_look_for ? `<div class="ai-interview-question-lookfor"><strong>What to evaluate:</strong> ${esc(item.what_to_look_for)}</div>` : ""}
          </div>
        `).join("")}
      </div>

      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:18px;padding-top:14px;border-top:1px solid var(--line)">
        <button class="btn btn-quiet btn-small" data-action="refresh-ai-summary" data-id="${esc(appId)}">↻ Re-analyze with AI</button>
        <button class="btn btn-primary btn-small" data-action="close-modal">Done</button>
      </div>
    </section></div>`;
  }
  if (state.modal?.startsWith("admin-recording:")) {
    const loading = state.adminRecordingLoading;
    const rec = state.adminRecordingData;

    if (loading || !rec) {
      return `<div class="modal-backdrop" data-action="close-modal"><section class="modal admin-recording-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <div class="modal-head">
          <div>
            <span class="eyebrow">📹 Proctoring Video Review</span>
            <h2 id="modal-title">Loading Candidate Recording…</h2>
            <p>Fetching authorized stream and synchronized proctoring logs.</p>
          </div>
          <button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button>
        </div>
        <div class="loading" style="padding:48px 0"><span class="spinner"></span><p style="margin-top:14px;color:var(--muted);font-size:12px">Authorizing secure stream access…</p></div>
      </section></div>`;
    }

    const events = rec.events || [];

    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal admin-recording-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head">
        <div>
          <span class="eyebrow">📹 Secure Assessment Recording · #${esc(rec.recording_id)}</span>
          <h2 id="modal-title">${esc(rec.candidate_name)}</h2>
          <p>Candidate webcam recording and proctoring integrity timeline</p>
        </div>
        <button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button>
      </div>

      <div class="admin-video-player-wrap">
        <video id="admin-recording-video" class="admin-video-player" controls playsinline controlsList="nodownload">
          <source src="${esc(rec.playback_url)}" type="video/webm">
          Your browser does not support HTML5 video playback.
        </video>
      </div>

      <div class="admin-video-details-grid">
        <div class="admin-video-detail-box">
          <small>Duration</small>
          <strong>${rec.duration_seconds ? `${rec.duration_seconds}s` : "—"}</strong>
        </div>
        <div class="admin-video-detail-box">
          <small>Format</small>
          <strong>${esc(rec.format || "WebM")}</strong>
        </div>
        <div class="admin-video-detail-box">
          <small>Status</small>
          <strong style="color:#059669">${esc(rec.recording_status)}</strong>
        </div>
        <div class="admin-video-detail-box">
          <small>Events Logged</small>
          <strong>${events.length} event${events.length === 1 ? "" : "s"}</strong>
        </div>
      </div>

      <div class="admin-recording-sync-timeline">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <h3 style="font-size:13px;margin:0">⏱ Synchronized Proctoring Timeline</h3>
          <span class="badge ${events.some(e => e.severity === 'warning') ? 'badge-amber' : 'badge-green'}" style="font-size:10px">
            ${events.filter(e => e.severity === 'warning').length} Warning(s)
          </span>
        </div>
        ${events.length ? events.map((e, idx) => {
          const isWarn = e.severity === "warning";
          const desc = e.event_type === "face_missing" ? "Candidate face not detected"
            : e.event_type === "multiple_faces" ? "Multiple faces detected in frame"
            : e.event_type === "tab_hidden" ? "Candidate switched browser tab"
            : e.event_type === "window_blur" ? "Assessment window lost focus"
            : e.event_type === "camera_stopped" ? "Webcam stream stopped unexpectedly"
            : e.event_type === "warning_limit_reached" ? "Suspicious warning threshold exceeded"
            : e.event_type.replace(/_/g, " ");
          const offset = rec.duration_seconds ? Math.min(rec.duration_seconds, Math.max(0, idx * 5)) : 0;
          return `<div class="admin-timeline-event ${isWarn ? 'event-warning' : ''}">
            <div>
              <strong>${esc(desc)}</strong>
              <small style="display:block;color:var(--muted)">${new Date(e.created_at).toLocaleTimeString()}${e.duration_seconds ? ` · ${e.duration_seconds}s duration` : ""}</small>
            </div>
            <div style="display:flex;gap:6px;align-items:center">
              <span class="badge ${isWarn ? 'badge-amber' : 'badge-green'}" style="font-size:9px">${esc(e.severity)}</span>
              ${rec.duration_seconds ? `<button class="admin-timeline-seek-btn" data-action="seek-video" data-time="${offset}">Jump to ⏱</button>` : ""}
            </div>
          </div>`;
        }).join("") : `<p class="stat-note">No suspicious events or warnings recorded during this session.</p>`}
      </div>

      <div style="display:flex;justify-content:flex-end;margin-top:16px;padding-top:12px;border-top:1px solid var(--line)">
        <button class="btn btn-primary btn-small" data-action="close-modal">Close</button>
      </div>
    </section></div>`;
  }
  if (state.modal === "live-video-monitoring" && state.liveMonitoringTarget) {
    const target = state.liveMonitoringTarget;
    return `<div class="modal-backdrop" data-action="close-modal"><section class="modal live-modal-wide" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head">
        <div>
          <span class="eyebrow">Real-Time Proctoring Feed · ${esc(target.company || "SkillSphere")}</span>
          <h2 id="modal-title">Live Assessment Monitoring</h2>
          <p>${esc(target.candidate_name)} · ${esc(target.job_title)} · ${esc(target.assessment_name || "Technical Assessment")}</p>
        </div>
        <button class="icon-button" aria-label="Close dialog" data-action="close-live-monitoring">×</button>
      </div>

      <div class="live-monitoring-body" style="margin-top:16px;">
        <div class="live-video-player-frame" style="position:relative;background:#090d16;border-radius:12px;overflow:hidden;min-height:360px;display:flex;align-items:center;justify-content:center;box-shadow:inset 0 0 40px rgba(0,0,0,0.8);">
          <video id="live-recruiter-video" autoplay playsinline style="width:100%;height:auto;max-height:460px;object-fit:contain;display:none;"></video>
          
          <div id="live-video-overlay" style="position:absolute;top:12px;left:12px;display:flex;gap:8px;align-items:center;z-index:10;">
            <span class="live-badge" style="background:#dc2626;color:#fff;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;display:inline-flex;align-items:center;gap:5px;letter-spacing:0.5px;">
              <span class="live-dot-pulse" style="width:7px;height:7px;border-radius:50%;background:#fff;"></span> LIVE
            </span>
            <span id="live-video-status-text" style="background:rgba(15,23,42,0.85);color:#f8fafc;padding:3px 10px;border-radius:12px;font-size:11px;border:1px solid rgba(255,255,255,0.1);">
              ${esc(state.recruiterLiveStatus || "Connecting feed…")}
            </span>
          </div>

          <div id="live-video-placeholder" style="color:#94a3b8;text-align:center;padding:50px 20px;">
            <div class="spinner" style="margin:0 auto 16px;"></div>
            <p id="live-video-placeholder-text" style="margin:0;font-size:14px;color:#cbd5e1;">${esc(state.recruiterLiveStatus || "Connecting to candidate's camera stream…")}</p>
            <small style="color:#64748b;display:block;margin-top:6px;">Direct peer-to-peer WebRTC video stream</small>
          </div>
        </div>

        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:16px;padding-top:14px;border-top:1px solid var(--line);">
          <div style="display:flex;gap:12px;align-items:center;">
            <div style="width:36px;height:36px;border-radius:50%;background:#e0e7ff;color:#4338ca;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;">
              ${esc((target.candidate_name || "C").charAt(0).toUpperCase())}
            </div>
            <div>
              <strong style="display:block;font-size:13px;color:var(--ink);">${esc(target.candidate_name)}</strong>
              <small style="color:#64748b;">${esc(target.candidate_email || "")} · Started ${target.starts_at ? new Date(target.starts_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : "recently"}</small>
            </div>
          </div>
          <div style="display:flex;gap:10px;align-items:center;">
            <button class="btn btn-quiet btn-small" data-action="stop-live-monitoring">Stop Viewing</button>
            <button class="btn btn-primary btn-small" data-action="close-live-monitoring">Done</button>
          </div>
        </div>
      </div>
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
    if (state.user.role === "candidate") {
      const [resume, candidateAnalytics] = await Promise.all([
        api("/resume"),
        api("/candidate/analytics").catch(() => null)
      ]);
      state.resume = resume;
      state.candidateAnalytics = candidateAnalytics;
      if (state.page === "Career Assistant" || state.page === "AI Career Assistant") {
        await loadAiSessions();
        await loadMockInterviewsHistory();
      }
    }
    if (state.user.role === "recruiter") {
      try {
        state.recruiterAnalytics = await api(`/recruiter/analytics?report_range=${encodeURIComponent(state.recruiterAnalyticsRange || "this_week")}`);
      } catch (e) { state.recruiterAnalytics = null; }
    }
    if (state.user.role === "admin") {
      const [analytics, adminJobs] = await Promise.all([
        api(`/admin/analytics?report_range=${encodeURIComponent(state.analyticsRange)}`),
        api("/admin/jobs")
      ]);
      state.analytics = analytics;
      state.adminJobs = adminJobs.items || [];
      state.proctoringItems = (await api(proctoringListPath())).items || [];
    }
    if (state.user.role === "recruiter" || state.user.role === "admin") {
      try {
        const liveData = await api("/recruiter/live-assessments");
        state.liveMonitoringAssessments = liveData.items || [];
      } catch (e) {
        state.liveMonitoringAssessments = [];
      }
    }
  } catch (error) {
    showToast(error.message, true);
    if (/token|authentication|authorization/i.test(error.message)) signOut(false);
  }
  renderWorkspace();
}

async function loadLiveAssessments() {
  if (!state.user || (state.user.role !== "recruiter" && state.user.role !== "admin")) return;
  try {
    const liveData = await api("/recruiter/live-assessments");
    state.liveMonitoringAssessments = liveData.items || [];
    renderWorkspace();
  } catch (e) {
    console.warn("Could not load live assessments:", e);
  }
}

function connectRecruiterLiveMonitoring(assessmentId) {
  stopRecruiterLiveMonitoring();

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws/live-monitoring/recruiter/${encodeURIComponent(assessmentId)}?token=${encodeURIComponent(state.token)}`;

  state.recruiterLiveStatus = "Connecting to signaling server…";
  updateRecruiterLiveStatusUI();

  try {
    const ws = new WebSocket(wsUrl);
    state.recruiterLiveWs = ws;

    const rtcConfig = {
      iceServers: [
        { urls: "stun:stun.l.google.com:19302" },
        { urls: "stun:stun1.l.google.com:19302" }
      ]
    };
    const pc = new RTCPeerConnection(rtcConfig);
    state.recruiterLivePc = pc;

    pc.ontrack = (event) => {
      const video = document.getElementById("live-recruiter-video");
      const placeholder = document.getElementById("live-video-placeholder");
      if (video && event.streams && event.streams[0]) {
        video.srcObject = event.streams[0];
        video.style.display = "block";
        if (placeholder) placeholder.style.display = "none";
        state.recruiterLiveStatus = "Live Feed Active";
        updateRecruiterLiveStatusUI();
      }
    };

    pc.onicecandidate = (event) => {
      if (event.candidate && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ice_candidate", candidate: event.candidate }));
      }
    };

    ws.onopen = () => {
      state.recruiterLiveStatus = "Requesting live camera stream…";
      updateRecruiterLiveStatusUI();
      ws.send(JSON.stringify({ type: "request_stream" }));
    };

    ws.onmessage = async (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.type === "candidate_status") {
          if (!msg.connected || !msg.camera_connected) {
            state.recruiterLiveStatus = msg.status || "Camera Disconnected";
            updateRecruiterLiveStatusUI(true);
          } else {
            if (state.recruiterLiveStatus !== "Live Feed Active") {
              state.recruiterLiveStatus = "Candidate online · Waiting for video…";
              updateRecruiterLiveStatusUI();
              ws.send(JSON.stringify({ type: "request_stream" }));
            }
          }
        } else if (msg.type === "webrtc_offer") {
          if (msg.sdp) {
            await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
            const answer = await pc.createAnswer();
            await pc.setLocalDescription(answer);
            ws.send(JSON.stringify({ type: "webrtc_answer", sdp: pc.localDescription }));
          }
        } else if (msg.type === "ice_candidate") {
          if (msg.candidate) {
            await pc.addIceCandidate(new RTCIceCandidate(msg.candidate)).catch(() => {});
          }
        }
      } catch (err) {
        console.warn("Recruiter live monitoring message error:", err);
      }
    };

    ws.onclose = (evt) => {
      if (evt.code === 1008) {
        state.recruiterLiveStatus = "Unauthorized: Only the assigned recruiter or admin can monitor this test.";
      } else {
        state.recruiterLiveStatus = "Signaling disconnected.";
      }
      updateRecruiterLiveStatusUI(true);
    };

    ws.onerror = () => {
      state.recruiterLiveStatus = "Signaling connection error.";
      updateRecruiterLiveStatusUI(true);
    };
  } catch (err) {
    state.recruiterLiveStatus = `Connection failed: ${err.message}`;
    updateRecruiterLiveStatusUI(true);
  }
}

function updateRecruiterLiveStatusUI(isWarning = false) {
  const statusEl = document.getElementById("live-video-status-text");
  const placeholder = document.getElementById("live-video-placeholder");
  const placeholderText = document.getElementById("live-video-placeholder-text");
  const video = document.getElementById("live-recruiter-video");

  if (statusEl) {
    statusEl.textContent = state.recruiterLiveStatus || "";
    if (isWarning) {
      statusEl.style.background = "#dc2626";
    } else if (state.recruiterLiveStatus === "Live Feed Active") {
      statusEl.style.background = "#16a34a";
    } else {
      statusEl.style.background = "rgba(15,23,42,0.85)";
    }
  }

  if (isWarning && placeholder && placeholderText) {
    placeholder.style.display = "block";
    placeholderText.textContent = state.recruiterLiveStatus;
    if (video) video.style.display = "none";
  }
}

function stopRecruiterLiveMonitoring() {
  if (state.recruiterLiveWs) {
    try {
      if (state.recruiterLiveWs.readyState === WebSocket.OPEN) {
        state.recruiterLiveWs.send(JSON.stringify({ type: "stop_viewing" }));
      }
      state.recruiterLiveWs.close();
    } catch (e) {}
    state.recruiterLiveWs = null;
  }
  if (state.recruiterLivePc) {
    try { state.recruiterLivePc.close(); } catch (e) {}
    state.recruiterLivePc = null;
  }
  const video = document.getElementById("live-recruiter-video");
  if (video) {
    video.srcObject = null;
    video.style.display = "none";
  }
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

function syncAiQuestionsFromDOM() {
  const cards = document.querySelectorAll(".ai-question-card");
  cards.forEach((card, idx) => {
    if (!state.aiGeneratedQuestions[idx]) return;
    const prompt = card.querySelector(".ai-q-prompt")?.value?.trim();
    if (prompt) state.aiGeneratedQuestions[idx].question = prompt;
    const explanation = card.querySelector(".ai-q-explanation")?.value?.trim();
    state.aiGeneratedQuestions[idx].explanation = explanation || "";
    const options = Array.from(card.querySelectorAll(".ai-q-option")).map(input => input.value.trim());
    if (options.length === 4) state.aiGeneratedQuestions[idx].options = options;
    const checkedRadio = card.querySelector(`input[name="ai_correct_${idx}"]:checked`);
    if (checkedRadio) state.aiGeneratedQuestions[idx].correct_index = Number(checkedRadio.value);
  });
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
  state.aiTab = "chat";
  state.aiSessions = [];
  state.aiActiveSessionId = null;
  state.aiMessages = [];
  state.aiChatBusy = false;
  state.aiResumeLoaded = false;
  state.aiResumeName = "";
  state.mockInterview = null;
  state.mockInterviewsHistory = [];
  state.mockInterviewBusy = false;
  state.mockViewingHistoryItem = null;
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
    renderWorkspace();
    if (result.processing_error) {
      showToast(result.processing_error, true);
    } else {
      showToast("Resume analyzed and profile updated!");
    }
  } catch (error) { showToast(error.message, true); }
}

app.addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target;
  if (form.id === "ai-chat-form") {
    const input = form.querySelector("#ai-chat-input");
    const msg = input ? input.value.trim() : "";
    if (msg) {
      input.value = "";
      await sendAiChatMessage(msg);
    }
    return;
  }
  if (form.id === "start-mock-form") {
    const values = Object.fromEntries(new FormData(form));
    const role = (values.job_role || "").trim() || "Software Engineer";
    await startMockInterview(role, values.interview_type, values.difficulty);
    return;
  }
  if (form.id === "mock-answer-form") {
    const values = Object.fromEntries(new FormData(form));
    const ans = (values.answer || "").trim();
    if (ans) {
      await submitMockAnswer(ans);
    }
    return;
  }
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
  if (form.id === "ai-question-generate-form") {
    if (!state.questionBankJob) return;
    const values = Object.fromEntries(new FormData(form));
    const payload = {
      technology: (values.technology || "").trim(),
      subtopic: (values.subtopic || "").trim(),
      difficulty: values.difficulty || "Mixed",
      count: Number(values.count || 5),
      custom_prompt: (values.custom_prompt || "").trim()
    };
    state.aiGenerating = true;
    renderWorkspace();
    try {
      const result = await api(`/jobs/${encodeURIComponent(state.questionBankJob.id)}/questions/ai-generate`, {
        method: "POST",
        body: JSON.stringify(payload)
      });
      state.aiGeneratedQuestions = result.questions || [];
      showToast(`Generated ${state.aiGeneratedQuestions.length} technical questions.`);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      state.aiGenerating = false;
      renderWorkspace();
    }
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
    const formData = new FormData(form);
    const values = Object.fromEntries(formData);
    values.skills = (values.skills || "").split(",").map(skill => skill.trim()).filter(Boolean);
    values.assessment_question_count = Number(values.assessment_question_count || 20);
    values.allow_detailed_results = formData.get("allow_detailed_results") !== null;
    const desc = (values.description || "").trim();
    if (desc.length < 20) {
      showToast("Job description must contain at least 20 characters before you can post this job.", true);
      return;
    }
    values.description = desc;
    try {
      const createdJob = await api("/jobs", { method: "POST", body: JSON.stringify(values) });
      state.modal = "";
      if (createdJob && createdJob.id) {
        state.jobs = [createdJob, ...state.jobs.filter(j => String(j.id) !== String(createdJob.id))];
        state.jobTotal = (state.jobTotal || 0) + 1;
        if (state.user?.role === "admin" && Array.isArray(state.adminJobs)) {
          state.adminJobs = [createdJob, ...state.adminJobs.filter(j => String(j.id) !== String(createdJob.id))];
        }
        renderWorkspace();
      }
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
      active: form.id === "question-form" || values.active === "on",
      explanation: (values.explanation || "").trim(),
      learning_resource_url: (values.learning_resource_url || "").trim(),
      subtopic: (values.subtopic || "").trim(),
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
  if (event.target.matches("[data-recording-consent]")) {
    if (state.publicAssessment?.proctor) {
      state.publicAssessment.proctor.recordingConsentGiven = event.target.checked;
      renderPublicAssessment();
    }
    return;
  }
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
  if (event.target.id === "recruiter-analytics-range") {
    state.recruiterAnalyticsRange = event.target.value;
    try {
      state.recruiterAnalytics = await api(`/recruiter/analytics?report_range=${encodeURIComponent(state.recruiterAnalyticsRange)}`);
      renderWorkspace();
    } catch (e) { showToast(e.message, true); }
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
  const toggleBtn = event.target.closest("[data-toggle-password]");
  if (toggleBtn) {
    const targetInput = document.getElementById(toggleBtn.dataset.togglePassword);
    if (targetInput) {
      const isPassword = targetInput.type === "password";
      targetInput.type = isPassword ? "text" : "password";
      toggleBtn.textContent = isPassword ? "🙈" : "👁";
      toggleBtn.setAttribute("aria-label", isPassword ? "Hide password" : "Show password");
      toggleBtn.setAttribute("title", isPassword ? "Hide password" : "Show password");
    }
    return;
  }
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
    if (state.page === "Live monitoring") await loadLiveAssessments();
    if ((state.page === "Career Assistant" || state.page === "AI Career Assistant") && state.user.role === "candidate") {
      await loadAiSessions();
      await loadMockInterviewsHistory();
    }
    if ((state.page === "My jobs" || state.page === "Dashboard") && state.user.role === "recruiter") {
      try {
        const result = await api("/jobs?mine=true&page_size=100");
        state.jobs = result.items || [];
        state.jobTotal = result.total ?? state.jobs.length;
      } catch (error) { showToast(error.message, true); }
    }
    if (state.page === "Dashboard" && state.user.role === "candidate") {
      try {
        const result = await api("/jobs?page_size=6");
        state.jobs = result.items || [];
        state.jobTotal = result.total ?? state.jobs.length;
      } catch (error) { showToast(error.message, true); }
    }
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
  if (target.dataset.action === "switch-ai-tab") {
    state.aiTab = target.dataset.tab;
    renderWorkspace();
    if (state.aiTab === "chat") scrollAiChatToBottom();
    if (state.aiTab === "history") await loadMockInterviewsHistory();
    return;
  }
  if (target.dataset.action === "select-ai-session") {
    await loadAiSessionMessages(Number(target.dataset.id));
    return;
  }
  if (target.dataset.action === "new-ai-session") {
    clearAiChat();
    return;
  }
  if (target.dataset.action === "delete-ai-session") {
    event.stopPropagation();
    await deleteAiSession(Number(target.dataset.id));
    return;
  }
  if (target.dataset.action === "clear-ai-chat") {
    clearAiChat();
    return;
  }
  if (target.dataset.action === "use-ai-prompt") {
    const prompt = target.dataset.prompt;
    const input = document.querySelector("#ai-chat-input");
    if (input) input.value = prompt;
    await sendAiChatMessage(prompt);
    return;
  }
  if (target.dataset.action === "reset-mock-interview") {
    state.mockInterview = null;
    state.mockViewingHistoryItem = null;
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "close-mock-history-view") {
    state.mockViewingHistoryItem = null;
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "view-mock-history") {
    await viewMockHistoryDetail(Number(target.dataset.id));
    return;
  }
  if (target.dataset.action === "refresh-mock-history") {
    await loadMockInterviewsHistory();
    return;
  }
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
  if (target.dataset.action === "open-ai-question-generator") {
    state.modal = "ai-questions";
    state.aiGenerating = false;
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "remove-ai-question") {
    const idx = Number(target.dataset.index);
    syncAiQuestionsFromDOM();
    state.aiGeneratedQuestions.splice(idx, 1);
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "clear-ai-questions") {
    state.aiGeneratedQuestions = [];
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "save-ai-questions") {
    if (!state.questionBankJob || !state.aiGeneratedQuestions.length) return;
    syncAiQuestionsFromDOM();
    try {
      const result = await api(`/jobs/${encodeURIComponent(state.questionBankJob.id)}/questions/batch`, {
        method: "POST",
        body: JSON.stringify({ questions: state.aiGeneratedQuestions })
      });
      state.modal = "";
      state.aiGeneratedQuestions = [];
      await loadQuestionBank(state.questionBankJob.id);
      showToast(`Added ${result.count ?? result.created ?? 0} questions to the question bank.`);
    } catch (error) {
      showToast(error.message, true);
    }
    return;
  }
  if (target.dataset.action === "delete-job") {
    const jobId = target.dataset.id;
    const title = target.dataset.title || "this job";
    if (!jobId) return;
    if (!confirm(`Are you sure you want to remove the job posting for "${title}"? This will permanently delete the role and associated applications.`)) {
      return;
    }
    try {
      await api(`/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
      state.jobs = state.jobs.filter(j => String(j.id) !== String(jobId));
      if (state.adminJobs) state.adminJobs = state.adminJobs.filter(j => String(j.id) !== String(jobId));
      if (state.modal?.startsWith(`job:${jobId}`)) state.modal = "";
      showToast("Job posting removed successfully.");
      await loadWorkspace();
      renderWorkspace();
    } catch (error) {
      showToast(error.message, true);
    }
    return;
  }
  if (target.dataset.action === "view-ai-summary") {
    const appId = target.dataset.id;
    state.modal = `ai-summary:${appId}`;
    state.aiSummaryAppId = appId;
    state.aiSummaryLoading = true;
    state.aiSummaryData = null;
    renderWorkspace();
    try {
      state.aiSummaryData = await api(`/applications/${encodeURIComponent(appId)}/ai-summary`);
    } catch (error) {
      showToast(error.message, true);
      state.modal = "";
    } finally {
      state.aiSummaryLoading = false;
      renderWorkspace();
    }
    return;
  }
  if (target.dataset.action === "refresh-ai-summary") {
    const appId = target.dataset.id || state.aiSummaryAppId;
    if (!appId) return;
    state.aiSummaryLoading = true;
    renderWorkspace();
    try {
      state.aiSummaryData = await api(`/applications/${encodeURIComponent(appId)}/ai-summary`, { method: "POST" });
      showToast("AI screening summary refreshed.");
    } catch (error) {
      showToast(error.message, true);
    } finally {
      state.aiSummaryLoading = false;
      renderWorkspace();
    }
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
  if (target.dataset.action === "take-assessment") {
    const url = target.dataset.url;
    if (url) {
      const match = url.match(/[?&]assessment=([^&]+)/);
      if (match) {
        await loadPublicAssessment(decodeURIComponent(match[1]));
        return;
      }
      window.location.href = url;
      return;
    }
    state.page = "Assessments";
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "review-assessment") {
    state.modal = `review:${target.dataset.id}`;
    state.assessmentReview = null;
    state.assessmentProctoringReview = null;
    renderWorkspace();
    try {
      const calls = [api(`/assessment-reviews/${encodeURIComponent(target.dataset.id)}`)];
      if (state.user?.role !== "candidate") {
        calls.push(api(`/assessment-reviews/${encodeURIComponent(target.dataset.id)}/proctoring`));
      }
      const [review, proctoring] = await Promise.all(calls);
      state.assessmentReview = review;
      state.assessmentProctoringReview = proctoring || null;
      renderWorkspace();
    } catch (error) { state.modal = ""; showToast(error.message, true); renderWorkspace(); }
    return;
  }
  if (target.dataset.action === "make-decision") {
    const assessmentId = target.dataset.id;
    const decision = target.dataset.decision;
    const confirmPrompt = confirm(`Are you sure you want to ${decision === 'Pass' ? 'Pass' : 'Fail'} this candidate? This will update the application status to Round 1 ${decision === 'Pass' ? 'Passed' : 'Failed'}.`);
    if (!confirmPrompt) return;
    try {
      await api(`/assessments/${encodeURIComponent(assessmentId)}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision, notes: "" }),
      });
      showToast(`Candidate marked as ${decision === 'Pass' ? 'Passed' : 'Failed'}.`);
      state.modal = "";
      await loadWorkspace();
    } catch (error) {
      showToast(error.message, true);
    }
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
    if (state.modal === "live-video-monitoring") {
      stopRecruiterLiveMonitoring();
      state.liveMonitoringTarget = null;
    }
    state.modal = "";
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "refresh-live-monitoring") {
    await loadLiveAssessments();
    showToast("Live monitoring feeds refreshed.");
    return;
  }
  if (target.dataset.action === "view-live-video") {
    const assessmentId = target.dataset.id;
    let targetAssessment = state.liveMonitoringAssessments?.find(item => String(item.assessment_id) === String(assessmentId));
    if (!targetAssessment) {
      try {
        const liveRes = await api("/recruiter/live-assessments");
        state.liveMonitoringAssessments = liveRes.items || [];
        targetAssessment = state.liveMonitoringAssessments.find(item => String(item.assessment_id) === String(assessmentId));
      } catch (e) {}
    }
    if (!targetAssessment) {
      targetAssessment = {
        assessment_id: assessmentId,
        candidate_name: "Candidate",
        job_title: "Technical Assessment",
        company: "SkillSphere",
        starts_at: new Date().toISOString(),
      };
    }
    state.liveMonitoringTarget = targetAssessment;
    state.modal = "live-video-monitoring";
    renderWorkspace();
    connectRecruiterLiveMonitoring(assessmentId);
    return;
  }
  if (target.dataset.action === "stop-live-monitoring" || target.dataset.action === "close-live-monitoring") {
    stopRecruiterLiveMonitoring();
    state.modal = "";
    state.liveMonitoringTarget = null;
    renderWorkspace();
    return;
  }
  if (target.dataset.action === "view-recording") {
    const recordingId = target.dataset.id;
    state.modal = `admin-recording:${recordingId}`;
    state.adminRecordingLoading = true;
    state.adminRecordingData = null;
    renderWorkspace();
    try {
      state.adminRecordingData = await api(`/admin/recordings/${encodeURIComponent(recordingId)}/playback`);
    } catch (err) {
      showToast(err.message, true);
      state.modal = "";
    } finally {
      state.adminRecordingLoading = false;
      renderWorkspace();
    }
    return;
  }
  if (target.dataset.action === "admin-retention-cleanup") {
    if (!window.confirm("Run recording retention cleanup now? Expired recordings will be safely removed.")) return;
    try {
      const res = await api("/admin/recordings/retention-cleanup", { method: "POST" });
      showToast(`Cleaned up ${res.cleaned_count || 0} expired recording(s). Active retained: ${res.remaining_recordings || 0}.`);
      await loadProctoring();
    } catch (err) {
      showToast(err.message, true);
    }
    return;
  }
  if (target.dataset.action === "seek-video") {
    const seekTime = Number(target.dataset.time || 0);
    const videoElem = document.getElementById("admin-recording-video");
    if (videoElem) {
      videoElem.currentTime = seekTime;
      videoElem.play().catch(() => {});
    }
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
  if (event.target.id === "ai-chat-input" && event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    const form = document.querySelector("#ai-chat-form");
    if (form) form.requestSubmit();
    return;
  }
  if (event.target.id === "job-query" && event.key === "Enter") searchJobs();
  if (event.key === "Escape" && state.modal) {
    if (state.modal === "live-video-monitoring") {
      stopRecruiterLiveMonitoring();
      state.liveMonitoringTarget = null;
    }
    state.modal = "";
    renderWorkspace();
  }
});

if (!window._liveMonitoringPollInterval) {
  window._liveMonitoringPollInterval = setInterval(async () => {
    if (state.user && (state.user.role === "recruiter" || state.user.role === "admin") && !state.modal && (state.page === "Live monitoring" || state.page === "Dashboard")) {
      try {
        const data = await api("/recruiter/live-assessments");
        const prev = JSON.stringify(state.liveMonitoringAssessments || []);
        const next = JSON.stringify(data.items || []);
        if (prev !== next) {
          state.liveMonitoringAssessments = data.items || [];
          renderWorkspace();
        }
      } catch (e) {}
    }
  }, 6000);
}

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

window.addEventListener("pagehide", () => {
  stopCandidateLiveMonitoring();
  stopAssessmentProctoring();
  stopRecruiterLiveMonitoring();
});
