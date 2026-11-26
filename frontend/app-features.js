/**
 * SkillSphere Feature Extensions
 * Modular add-ons seamlessly enhancing the existing SkillSphere application:
 * 1. Account & Security (Forgot/Reset password modal, Email verification status & resend)
 * 2. Candidate Job Discovery (Save/unsave jobs, Saved jobs tab, Recommended jobs section)
 * 3. Candidate Resume & Profile (Resume quality score card, missing info alerts, resume version history)
 * 4. Recruiter Job Management (Edit job modal, live status toggle, company profile)
 * 5. Direct Candidate-Recruiter Communication (Messaging modal & thread, unread count badge)
 * 6. Question Bank Enhancements (Duplicate question button, Question collections modal)
 * 7. Candidate Technical Practice Module (Practice page, skill selection, interactive quiz, analytics)
 * 8. Assessment Improvements (Adjust score modal with audit reason, retry assessment button)
 * 9. Asynchronous Video Assessment Module (Video assessment modal, assignment & evaluation)
 * 10. Interview Hub Improvements (Scorecard tab & submission, candidate dossier modal)
 * 11. Support Tickets & Career Assistant Escalation (Ticket creation, list, escalation from chat)
 * 12. Live Human-Agent Handoff (Admin live availability toggle, WebSocket ticket chat)
 * 13. Administration & Auditability (Admin audit logs view, Job moderation buttons)
 * 14. Multilingual Career Assistant (Language selector dropdown in chat header)
 */

(function () {
  // Ensure extension state exists on global state
  if (typeof state !== "undefined") {
    state.savedJobIds = state.savedJobIds || new Set();
    state.savedJobsList = state.savedJobsList || [];
    state.recommendedJobsList = state.recommendedJobsList || [];
    state.resumeVersions = state.resumeVersions || [];
    state.resumeQuality = state.resumeQuality || null;
    state.practiceTechs = state.practiceTechs || [];
    state.practiceQuestions = state.practiceQuestions || [];
    state.practiceCurrentIndex = state.practiceCurrentIndex || 0;
    state.practiceSelectedOption = state.practiceSelectedOption ?? null;
    state.practiceSubmitted = state.practiceSubmitted || false;
    state.practiceAnalytics = state.practiceAnalytics || null;
    state.activeMessageThread = state.activeMessageThread || null;
    state.unreadMessageCount = state.unreadMessageCount || 0;
    state.supportTickets = state.supportTickets || [];
    state.adminAuditLogs = state.adminAuditLogs || [];
    state.adminIsAvailable = state.adminIsAvailable || false;
    state.assistantLanguage = state.assistantLanguage || "en";
  }

  window.SkillSphereFeatures = {
    // ----------------------------------------------------
    // 1. Account & Security
    // ----------------------------------------------------
    openForgotPasswordModal() {
      state.modal = "forgot-password";
      renderWorkspaceOrAuth();
    },

    openResetPasswordModal() {
      state.modal = "reset-password";
      renderWorkspaceOrAuth();
    },

    async submitForgotPassword(email) {
      try {
        const res = await api("/auth/forgot-password", {
          method: "POST",
          body: JSON.stringify({ email })
        });
        showToast(res.message || "Reset link generated.");
        if (res.reset_token) {
          // In development/test mode, auto-fill or inform user
          console.log("[SkillSphere Security] Reset token:", res.reset_token);
        }
        state.modal = "reset-password";
        renderWorkspaceOrAuth();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async submitResetPassword(token, new_password) {
      try {
        const res = await api("/auth/reset-password", {
          method: "POST",
          body: JSON.stringify({ token, new_password })
        });
        showToast(res.message || "Password updated. You may now sign in.");
        state.modal = "";
        renderWorkspaceOrAuth();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async resendVerificationEmail() {
      try {
        const res = await api("/auth/resend-verification", { method: "POST" });
        showToast(res.message || "Verification email sent.");
      } catch (err) {
        showToast(err.message, true);
      }
    },

    // ----------------------------------------------------
    // 2. Candidate Job Discovery (Save & Recommend)
    // ----------------------------------------------------
    async toggleSaveJob(jobId) {
      const isSaved = state.savedJobIds.has(String(jobId)) || state.savedJobIds.has(Number(jobId));
      try {
        if (isSaved) {
          await api(`/jobs/${encodeURIComponent(jobId)}/save`, { method: "DELETE" });
          state.savedJobIds.delete(String(jobId));
          state.savedJobIds.delete(Number(jobId));
          showToast("Role removed from saved jobs.");
        } else {
          await api(`/jobs/${encodeURIComponent(jobId)}/save`, { method: "POST" });
          state.savedJobIds.add(String(jobId));
          showToast("Role saved to your collection! 🔖");
        }
        await this.loadSavedJobs();
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async loadSavedJobs() {
      if (!state.user || state.user.role !== "candidate") return;
      try {
        const res = await api("/jobs/saved");
        state.savedJobsList = res.items || [];
        state.savedJobIds = new Set((res.items || []).map(j => String(j.id)));
      } catch (err) {
        // silent fail on initial load
      }
    },

    async loadRecommendedJobs() {
      if (!state.user || state.user.role !== "candidate") return;
      try {
        const res = await api("/jobs/recommended");
        state.recommendedJobsList = res.items || [];
      } catch (err) {
        state.recommendedJobsList = [];
      }
    },

    // ----------------------------------------------------
    // 3. Candidate Resume & Profile Analysis
    // ----------------------------------------------------
    async loadResumeAnalysis() {
      if (!state.user || state.user.role !== "candidate") return;
      try {
        const res = await api("/resume/analysis");
        state.resumeQuality = res;
      } catch (err) {
        state.resumeQuality = null;
      }
    },

    async loadResumeVersions() {
      if (!state.user || state.user.role !== "candidate") return;
      try {
        const res = await api("/resume/versions");
        state.resumeVersions = res.items || [];
      } catch (err) {
        state.resumeVersions = [];
      }
    },

    // ----------------------------------------------------
    // 4. Recruiter Job Management (Edit & Status)
    // ----------------------------------------------------
    openEditJobModal(jobId) {
      const job = (state.jobs || []).find(j => String(j.id) === String(jobId));
      if (!job) return;
      state.editJobData = job;
      state.modal = `edit-job:${jobId}`;
      renderWorkspace();
    },

    async updateJob(jobId, payload) {
      try {
        const updated = await api(`/jobs/${encodeURIComponent(jobId)}`, {
          method: "PATCH",
          body: JSON.stringify(payload)
        });
        showToast("Job listing updated successfully.");
        const idx = (state.jobs || []).findIndex(j => String(j.id) === String(jobId));
        if (idx >= 0) state.jobs[idx] = updated;
        state.modal = "";
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    // ----------------------------------------------------
    // 5. Candidate-Recruiter Direct Messaging
    // ----------------------------------------------------
    async openMessageThread(recipientId, recipientName, applicationId = null, jobId = null) {
      state.activeMessageThread = {
        recipientId,
        recipientName,
        applicationId,
        jobId,
        messages: [],
        loading: true
      };
      state.modal = "message-thread";
      renderWorkspace();
      try {
        const res = await api(`/messages/thread/${encodeURIComponent(recipientId)}`);
        state.activeMessageThread.messages = res.messages || [];
      } catch (err) {
        showToast(err.message, true);
      } finally {
        state.activeMessageThread.loading = false;
        renderWorkspace();
        this.updateUnreadMessagesCount();
      }
    },

    async sendMessageInThread(content) {
      if (!state.activeMessageThread || !content.trim()) return;
      const thread = state.activeMessageThread;
      try {
        const sent = await api("/messages", {
          method: "POST",
          body: JSON.stringify({
            receiver_id: thread.recipientId,
            content: content.trim(),
            application_id: thread.applicationId,
            job_id: thread.jobId
          })
        });
        thread.messages.push(sent);
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async updateUnreadMessagesCount() {
      if (!state.user) return;
      try {
        const res = await api("/messages/unread-count");
        state.unreadMessageCount = res.unread_count || 0;
      } catch (err) {}
    },

    // ----------------------------------------------------
    // 6. Question Bank (Duplicate & Collections)
    // ----------------------------------------------------
    async duplicateQuestion(jobId, questionId) {
      try {
        const res = await api(`/jobs/${encodeURIComponent(jobId)}/questions/${encodeURIComponent(questionId)}/duplicate`, {
          method: "POST"
        });
        showToast("Question duplicated successfully!");
        if (typeof loadQuestionBank === "function") {
          await loadQuestionBank(jobId);
        }
      } catch (err) {
        showToast(err.message, true);
      }
    },

    openQuestionCollectionsModal(jobId) {
      state.modal = `question-collections:${jobId}`;
      renderWorkspace();
    },

    async createQuestionCollection(title, description, questionIds) {
      try {
        await api("/question-collections", {
          method: "POST",
          body: JSON.stringify({ title, description, question_ids: questionIds })
        });
        showToast("Question collection created successfully!");
        state.modal = "";
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    // ----------------------------------------------------
    // 7. Candidate Technical Practice
    // ----------------------------------------------------
    async loadPracticeDashboard() {
      try {
        const [techs, analytics] = await Promise.all([
          api("/practice/technologies"),
          api("/practice/analytics")
        ]);
        state.practiceTechs = techs.technologies || [];
        state.practiceAnalytics = analytics;
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async addPracticeTechnology(tech) {
      try {
        await api("/practice/technologies", {
          method: "POST",
          body: JSON.stringify({ technology: tech })
        });
        showToast(`Added ${tech} to practice technologies.`);
        await this.loadPracticeDashboard();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async removePracticeTechnology(tech) {
      try {
        await api(`/practice/technologies?technology=${encodeURIComponent(tech)}`, {
          method: "DELETE"
        });
        showToast(`Removed ${tech}.`);
        await this.loadPracticeDashboard();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async startPracticeSession(technology, difficulty = "") {
      try {
        let url = `/practice/questions?limit=10`;
        if (technology) url += `&technology=${encodeURIComponent(technology)}`;
        if (difficulty) url += `&difficulty=${encodeURIComponent(difficulty)}`;
        const res = await api(url);
        state.practiceQuestions = res.questions || [];
        state.practiceCurrentIndex = 0;
        state.practiceSelectedOption = null;
        state.practiceSubmitted = false;
        state.practiceLastResult = null;
        state.page = "Practice";
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async submitPracticeAnswer() {
      const q = state.practiceQuestions[state.practiceCurrentIndex];
      if (!q || state.practiceSelectedOption === null) return;
      try {
        const res = await api("/practice/submit", {
          method: "POST",
          body: JSON.stringify({
            question_id: q.id,
            selected_option_index: state.practiceSelectedOption
          })
        });
        state.practiceSubmitted = true;
        state.practiceLastResult = res;
        await this.loadPracticeDashboard();
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    nextPracticeQuestion() {
      state.practiceCurrentIndex++;
      state.practiceSelectedOption = null;
      state.practiceSubmitted = false;
      state.practiceLastResult = null;
      renderWorkspace();
    },

    // ----------------------------------------------------
    // 8. Assessment Improvements (Adjust Score & Retry)
    // ----------------------------------------------------
    openAdjustScoreModal(assessmentId, currentScore) {
      state.adjustScoreAssessmentId = assessmentId;
      state.adjustScoreCurrent = currentScore;
      state.modal = `adjust-score:${assessmentId}`;
      renderWorkspace();
    },

    async submitScoreAdjustment(assessmentId, newScore, reason) {
      try {
        await api(`/assessments/${encodeURIComponent(assessmentId)}/adjust-score`, {
          method: "POST",
          body: JSON.stringify({ new_score: Number(newScore), reason })
        });
        showToast("Assessment score adjusted and logged for audit.");
        state.modal = "";
        if (typeof loadWorkspace === "function") await loadWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async retryAssessment(assessmentId) {
      if (!window.confirm("Grant this candidate a fresh assessment retry? This resets previous attempts and notifies the candidate.")) return;
      try {
        await api(`/assessments/${encodeURIComponent(assessmentId)}/retry`, { method: "POST" });
        showToast("Assessment retry authorized and notified to candidate.");
        if (typeof loadWorkspace === "function") await loadWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    // ----------------------------------------------------
    // 9. Video Assessment Module
    // ----------------------------------------------------
    openVideoAssessmentModal(jobId) {
      state.modal = `video-assessment-job:${jobId}`;
      renderWorkspace();
    },

    async createVideoAssessment(jobId, title, duration, questions) {
      try {
        await api("/video-assessments", {
          method: "POST",
          body: JSON.stringify({
            job_id: Number(jobId),
            title,
            duration_minutes: Number(duration),
            questions
          })
        });
        showToast("Asynchronous video assessment configured!");
        state.modal = "";
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async evaluateVideoResponse(responseId, score, feedback) {
      try {
        await api(`/video-assessments/responses/${encodeURIComponent(responseId)}/evaluate`, {
          method: "POST",
          body: JSON.stringify({ score: Number(score), feedback })
        });
        showToast("Video assessment response evaluated!");
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    // ----------------------------------------------------
    // 10. Interview Hub (Scorecard & Dossier)
    // ----------------------------------------------------
    openScorecardModal(interviewId) {
      state.activeScorecardInterviewId = interviewId;
      state.modal = `scorecard:${interviewId}`;
      renderWorkspace();
    },

    async submitScorecard(interviewId, payload) {
      try {
        await api(`/interviews/${encodeURIComponent(interviewId)}/scorecard`, {
          method: "POST",
          body: JSON.stringify(payload)
        });
        showToast("Structured interview scorecard saved.");
        state.modal = "";
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async openCandidateDossier(interviewId) {
      state.modal = `candidate-dossier:${interviewId}`;
      state.dossierLoading = true;
      state.dossierData = null;
      renderWorkspace();
      try {
        const res = await api(`/interviews/${encodeURIComponent(interviewId)}/dossier`);
        state.dossierData = res;
      } catch (err) {
        showToast(err.message, true);
        state.modal = "";
      } finally {
        state.dossierLoading = false;
        renderWorkspace();
      }
    },

    // ----------------------------------------------------
    // 11. Support Tickets & Escalation
    // ----------------------------------------------------
    async loadTickets() {
      try {
        const endpoint = state.user?.role === "admin" ? "/admin/tickets" : "/tickets";
        const res = await api(endpoint);
        state.supportTickets = res.items || [];
      } catch (err) {
        state.supportTickets = [];
      }
    },

    openNewTicketModal(prefillSubject = "", prefillCategory = "General", prefillMessage = "") {
      state.ticketPrefill = { subject: prefillSubject, category: prefillCategory, message: prefillMessage };
      state.modal = "new-ticket";
      renderWorkspace();
    },

    async createTicket(subject, category, priority, message) {
      try {
        const res = await api("/tickets", {
          method: "POST",
          body: JSON.stringify({ subject, category, priority, message })
        });
        showToast(`Support ticket #${res.id} created.`);
        state.modal = "";
        await this.loadTickets();
        state.page = "Support";
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async openTicketDetail(ticketId) {
      state.activeTicketId = ticketId;
      state.modal = `ticket-detail:${ticketId}`;
      state.ticketDetailLoading = true;
      renderWorkspace();
      try {
        const res = await api(`/tickets/${encodeURIComponent(ticketId)}`);
        state.activeTicketData = res;
      } catch (err) {
        showToast(err.message, true);
        state.modal = "";
      } finally {
        state.ticketDetailLoading = false;
        renderWorkspace();
      }
    },

    async sendTicketReply(ticketId, message, isInternal = false) {
      if (!message.trim()) return;
      try {
        const res = await api(`/tickets/${encodeURIComponent(ticketId)}/messages`, {
          method: "POST",
          body: JSON.stringify({ message: message.trim(), is_internal: isInternal })
        });
        if (state.activeTicketData) {
          state.activeTicketData.messages = state.activeTicketData.messages || [];
          state.activeTicketData.messages.push(res);
        }
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    async updateTicketStatus(ticketId, status, priority = null, assignedAdminId = null) {
      try {
        await api(`/admin/tickets/${encodeURIComponent(ticketId)}`, {
          method: "PATCH",
          body: JSON.stringify({ status, priority, assigned_admin_id: assignedAdminId })
        });
        showToast(`Ticket #${ticketId} status updated to ${status}.`);
        await this.loadTickets();
        if (state.activeTicketData && String(state.activeTicketData.id) === String(ticketId)) {
          state.activeTicketData.status = status;
        }
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    // ----------------------------------------------------
    // 12. Live Human-Agent Handoff & Admin Availability
    // ----------------------------------------------------
    async toggleAdminAvailability() {
      const newStatus = !state.adminIsAvailable;
      try {
        const res = await api("/admin/availability", {
          method: "POST",
          body: JSON.stringify({ is_available: newStatus })
        });
        state.adminIsAvailable = res.is_available;
        showToast(`Support presence: ${res.is_available ? "Online (Available)" : "Offline"}`);
        renderWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    // ----------------------------------------------------
    // 13. Auditability & Job Moderation
    // ----------------------------------------------------
    async loadAdminAuditLogs() {
      if (state.user?.role !== "admin") return;
      try {
        const res = await api("/admin/audit-logs?limit=50");
        state.adminAuditLogs = res.items || [];
      } catch (err) {
        state.adminAuditLogs = [];
      }
    },

    async moderateJob(jobId, action, reason = "") {
      try {
        await api(`/admin/jobs/${encodeURIComponent(jobId)}/moderate`, {
          method: "POST",
          body: JSON.stringify({ action, reason })
        });
        showToast(`Role moderation updated: ${action}`);
        if (typeof loadWorkspace === "function") await loadWorkspace();
      } catch (err) {
        showToast(err.message, true);
      }
    },

    // ----------------------------------------------------
    // 14. Multilingual Career Assistant
    // ----------------------------------------------------
    setAssistantLanguage(lang) {
      state.assistantLanguage = lang;
      showToast(`Assistant language set to ${lang.toUpperCase()}`);
      renderWorkspace();
    }
  };

  function renderWorkspaceOrAuth() {
    if (state.user) {
      if (typeof renderWorkspace === "function") renderWorkspace();
    } else {
      if (typeof renderAuth === "function") renderAuth(state.registerMode || false);
    }
  }
})();
