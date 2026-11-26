/**
 * SkillSphere Additional Feature Views & Modals
 * Renders HTML components for the 14 new feature sets:
 * - Forgot & Reset Password Modals
 * - Practice Page & Question Quiz
 * - Direct Messaging Thread Modal
 * - Saved Jobs Tab & Recommended Roles
 * - Resume Quality Analysis & Versions History
 * - Recruiter Job Edit Modal
 * - Video Assessment Configuration & Evaluation Modal
 * - Structured Interview Scorecard & Candidate Dossier Modal
 * - Support Tickets Queue, Ticket Creation & Live Chat Modal
 * - Admin Audit Logs View
 */

(function () {
  window.SkillSphereViews = {
    // ----------------------------------------------------
    // Modals
    // ----------------------------------------------------
    renderModals() {
      if (!state.modal) return "";

      // 1. Forgot Password Modal
      if (state.modal === "forgot-password") {
        return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <div class="modal-head"><div><span class="eyebrow">Account Recovery</span><h2 id="modal-title">Forgot Password</h2><p>Enter your account email to receive reset instructions.</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
          <form id="forgot-password-form" class="profile-form">
            <div class="field span-2"><label for="forgot-email">Email Address</label><input class="input" id="forgot-email" name="email" type="email" placeholder="you@example.com" required /></div>
            <div class="span-2" style="display:flex;gap:10px;justify-content:flex-end;margin-top:10px;">
              <button class="btn btn-quiet" type="button" data-action="close-modal">Cancel</button>
              <button class="btn btn-primary" type="submit">Send Reset Link →</button>
            </div>
          </form>
        </section></div>`;
      }

      // 1b. Reset Password Modal
      if (state.modal === "reset-password") {
        return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <div class="modal-head"><div><span class="eyebrow">Account Recovery</span><h2 id="modal-title">Reset Password</h2><p>Enter your reset token and your new password.</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
          <form id="reset-password-form" class="profile-form">
            <div class="field span-2"><label for="reset-token">Reset Token</label><input class="input" id="reset-token" name="token" placeholder="Paste reset token here" required /></div>
            <div class="field span-2"><label for="reset-password">New Password</label><input class="input" id="reset-password" name="new_password" type="password" minlength="8" placeholder="At least 8 characters" required /></div>
            <div class="span-2" style="display:flex;gap:10px;justify-content:flex-end;margin-top:10px;">
              <button class="btn btn-quiet" type="button" data-action="close-modal">Cancel</button>
              <button class="btn btn-primary" type="submit">Update Password →</button>
            </div>
          </form>
        </section></div>`;
      }

      // 4. Edit Job Modal
      if (state.modal?.startsWith("edit-job:")) {
        const jobId = state.modal.slice("edit-job:".length);
        const job = (state.jobs || []).find(j => String(j.id) === String(jobId)) || {};
        return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <div class="modal-head"><div><span class="eyebrow">Recruiter Workspace</span><h2 id="modal-title">Edit Role: ${esc(job.title || "")}</h2><p>Update job requirements, status, and details.</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
          <form id="edit-job-form" class="profile-form" data-job-id="${esc(jobId)}">
            <div class="field span-2"><label>Role Title</label><input class="input" name="title" value="${esc(job.title || "")}" required /></div>
            <div class="field"><label>Location</label><input class="input" name="location" value="${esc(job.location || "")}" required /></div>
            <div class="field"><label>Job Type</label><select class="select" name="job_type">${["Full-time", "Part-time", "Contract", "Remote"].map(t => `<option ${t === job.job_type ? "selected" : ""}>${t}</option>`).join("")}</select></div>
            <div class="field"><label>Experience</label><input class="input" name="experience" value="${esc(job.experience || "")}" required /></div>
            <div class="field"><label>Salary Range</label><input class="input" name="salary" value="${esc(job.salary || "")}" /></div>
            <div class="field"><label>Listing Status</label><select class="select" name="status"><option value="Open" ${job.status === "Open" ? "selected" : ""}>Open (Active)</option><option value="Closed" ${job.status === "Closed" ? "selected" : ""}>Closed (Paused)</option></select></div>
            <div class="field span-2"><label>Required Skills (comma-separated)</label><input class="input" name="skills" value="${esc((job.skills || []).join(", "))}" required /></div>
            <div class="field span-2"><label>Description</label><textarea class="textarea" name="description" rows="4" required>${esc(job.description || "")}</textarea></div>
            <div class="span-2" style="display:flex;gap:10px;justify-content:flex-end;margin-top:10px;">
              <button class="btn btn-quiet" type="button" data-action="close-modal">Cancel</button>
              <button class="btn btn-primary" type="submit">Save Changes →</button>
            </div>
          </form>
        </section></div>`;
      }

      // 5. Direct Message Thread Modal
      if (state.modal === "message-thread") {
        const thread = state.activeMessageThread || {};
        const messages = thread.messages || [];
        return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" style="max-width:640px;height:580px;display:flex;flex-direction:column;">
          <div class="modal-head"><div><span class="eyebrow">Direct Communication</span><h2>Conversation with ${esc(thread.recipientName || "Contact")}</h2><p>Direct candidate-recruiter messaging</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
          <div style="flex:1;overflow-y:auto;padding:14px;background:#faf8f6;border-radius:10px;display:flex;flex-direction:column;gap:10px;">
            ${thread.loading ? `<p style="text-align:center;color:#78716c">Loading messages…</p>` : messages.length ? messages.map(m => {
              const isMe = m.sender_id === state.user.id;
              return `<div style="align-self:${isMe ? 'flex-end' : 'flex-start'};max-width:80%;background:${isMe ? 'var(--accent)' : '#ffffff'};color:${isMe ? '#ffffff' : 'var(--ink)'};padding:10px 14px;border-radius:12px;border:${isMe ? 'none' : '1px solid var(--line)'};box-shadow:0 1px 3px rgba(0,0,0,0.05);">
                <div style="font-size:11px;opacity:0.8;margin-bottom:4px">${esc(m.sender_name)} · <time>${new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time></div>
                <div style="font-size:13px;line-height:1.4">${esc(m.content)}</div>
              </div>`;
            }).join("") : `<div class="empty" style="padding:20px"><p>No messages yet. Start the conversation!</p></div>`}
          </div>
          <form id="direct-message-form" style="display:flex;gap:8px;padding-top:14px;">
            <input class="input" id="direct-message-input" placeholder="Type your message…" autocomplete="off" required style="flex:1;" />
            <button class="btn btn-primary" type="submit">Send →</button>
          </form>
        </section></div>`;
      }

      // 8. Adjust Score Modal
      if (state.modal?.startsWith("adjust-score:")) {
        const assessmentId = state.modal.slice("adjust-score:".length);
        return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
          <div class="modal-head"><div><span class="eyebrow">Recruiter Audit Control</span><h2 id="modal-title">Adjust Assessment Score</h2><p>Every score adjustment is permanently logged for auditability.</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
          <form id="adjust-score-form" class="profile-form" data-assessment-id="${esc(assessmentId)}">
            <div class="field"><label>Current Score</label><input class="input" value="${esc(state.adjustScoreCurrent ?? '—')}%" disabled /></div>
            <div class="field"><label>New Score (0–100%)</label><input class="input" type="number" min="0" max="100" name="new_score" required /></div>
            <div class="field span-2"><label>Audit Reason (Required for compliance)</label><textarea class="textarea" name="reason" placeholder="e.g. Granted partial credit for technical explanation in question 4" required minlength="5"></textarea></div>
            <div class="span-2" style="display:flex;gap:10px;justify-content:flex-end;margin-top:10px;">
              <button class="btn btn-quiet" type="button" data-action="close-modal">Cancel</button>
              <button class="btn btn-primary" type="submit">Save Adjustment →</button>
            </div>
          </form>
        </section></div>`;
      }

      // 10. Interview Scorecard Modal
      if (state.modal?.startsWith("scorecard:")) {
        const interviewId = state.modal.slice("scorecard:".length);
        return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" style="max-width:680px">
          <div class="modal-head"><div><span class="eyebrow">Structured Evaluation</span><h2>Interview Scorecard</h2><p>Evaluate candidate across standardized hiring competencies (1 = Poor, 5 = Excellent).</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
          <form id="interview-scorecard-form" class="profile-form" data-interview-id="${esc(interviewId)}">
            <div class="field"><label>Technical Competence (1–5)</label><input class="input" type="number" name="technical_score" min="1" max="5" value="4" required /></div>
            <div class="field"><label>Communication (1–5)</label><input class="input" type="number" name="communication_score" min="1" max="5" value="4" required /></div>
            <div class="field"><label>Problem Solving (1–5)</label><input class="input" type="number" name="problem_solving_score" min="1" max="5" value="4" required /></div>
            <div class="field"><label>Culture & Values (1–5)</label><input class="input" type="number" name="culture_fit_score" min="1" max="5" value="4" required /></div>
            <div class="field span-2"><label>Hiring Recommendation</label><select class="select" name="recommendation"><option value="Strong Yes">Strong Yes</option><option value="Yes" selected>Yes</option><option value="Neutral">Neutral</option><option value="No">No</option><option value="Strong No">Strong No</option></select></div>
            <div class="field span-2"><label>Key Strengths</label><textarea class="textarea" name="strengths" placeholder="Highlights and positive observations" rows="2"></textarea></div>
            <div class="field span-2"><label>Areas for Growth</label><textarea class="textarea" name="weaknesses" placeholder="Development opportunities or gaps" rows="2"></textarea></div>
            <div class="field span-2"><label>Interviewer Notes</label><textarea class="textarea" name="notes" placeholder="Detailed notes from the live conversation" rows="3"></textarea></div>
            <div class="span-2" style="display:flex;gap:10px;justify-content:flex-end;margin-top:10px;">
              <button class="btn btn-quiet" type="button" data-action="close-modal">Cancel</button>
              <button class="btn btn-primary" type="submit">Submit Scorecard →</button>
            </div>
          </form>
        </section></div>`;
      }

      // 10b. Candidate Dossier Modal
      if (state.modal?.startsWith("candidate-dossier:")) {
        const d = state.dossierData || {};
        return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" style="max-width:720px">
          <div class="modal-head"><div><span class="eyebrow">Comprehensive Dossier</span><h2>${esc(d.candidate_name || "Candidate Dossier")}</h2><p>${esc(d.job_title || "")} · ${esc(d.company || "")}</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
          ${state.dossierLoading ? `<p style="text-align:center;padding:20px;color:#78716c">Compiling candidate dossier…</p>` : `
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px;">
              <div class="card" style="padding:12px;"><strong>Resume Match Score</strong><div style="font-size:24px;font-weight:700;color:var(--accent);margin-top:4px;">${d.match_percentage != null ? `${d.match_percentage}%` : "—"}</div><small style="color:var(--muted)">Matched: ${(d.matched_skills || []).join(", ") || "None"}</small></div>
              <div class="card" style="padding:12px;"><strong>Assessment Performance</strong><div style="font-size:24px;font-weight:700;color:#308366;margin-top:4px;">${d.assessment_score != null ? `${d.assessment_score}%` : "Pending"}</div><small style="color:#78716c">Status: ${esc(d.assessment_status || "Not completed")}</small></div>
            </div>
            ${d.proctoring_flagged ? `<div style="background:#fef2f2;border:1px solid #fecaca;padding:10px 14px;border-radius:8px;color:#991b1b;font-size:12px;margin-bottom:14px;">⚠️ Proctoring Notice: Flags recorded during technical assessment (${d.proctoring_flags_count || 0} incidents).</div>` : ""}
            <div class="card" style="padding:14px;margin-bottom:14px;"><strong>Interview Scorecards (${(d.scorecards || []).length})</strong>
              ${(d.scorecards || []).length ? d.scorecards.map(sc => `<div style="border-top:1px solid #f5f5f4;padding-top:8px;margin-top:8px;font-size:12px;">
                <div style="display:flex;justify-content:space-between"><strong>${esc(sc.evaluator_name)}</strong><span class="badge badge-green">${esc(sc.recommendation)}</span></div>
                <div style="margin-top:4px;color:#57534e">Tech: ${sc.technical_score}/5 · Comm: ${sc.communication_score}/5 · Problem Solving: ${sc.problem_solving_score}/5</div>
                ${sc.notes ? `<p style="margin-top:4px;color:#44403c"><em>"${esc(sc.notes)}"</em></p>` : ""}
              </div>`).join("") : `<p style="color:#78716c;font-size:12px;margin-top:4px;">No scorecards submitted yet.</p>`}
            </div>
          `}
          <div style="display:flex;justify-content:flex-end"><button class="btn btn-quiet" data-action="close-modal">Close Dossier</button></div>
        </section></div>`;
      }

      // 11. Support Ticket Modal
      if (state.modal === "new-ticket") {
        const p = state.ticketPrefill || {};
        return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" style="max-width:600px">
          <div class="modal-head"><div><span class="eyebrow">SkillSphere Helpdesk</span><h2>Open Support Ticket</h2><p>Need human assistance or technical help? Our team is here for you.</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
          <form id="new-ticket-form" class="profile-form">
            <div class="field span-2"><label>Subject</label><input class="input" name="subject" value="${esc(p.subject || "")}" placeholder="Brief summary of the issue" required /></div>
            <div class="field"><label>Category</label><select class="select" name="category">${["General", "Technical", "Assessment", "Interview", "Account"].map(c => `<option ${c === p.category ? "selected" : ""}>${c}</option>`).join("")}</select></div>
            <div class="field"><label>Priority</label><select class="select" name="priority"><option value="Low">Low</option><option value="Medium" selected>Medium</option><option value="High">High</option><option value="Urgent">Urgent</option></select></div>
            <div class="field span-2"><label>Description / Details</label><textarea class="textarea" name="message" rows="4" placeholder="Explain what you need assistance with..." required>${esc(p.message || "")}</textarea></div>
            <div class="span-2" style="display:flex;gap:10px;justify-content:flex-end;margin-top:10px;">
              <button class="btn btn-quiet" type="button" data-action="close-modal">Cancel</button>
              <button class="btn btn-primary" type="submit">Submit Ticket →</button>
            </div>
          </form>
        </section></div>`;
      }

      // 11b. Ticket Detail Modal
      if (state.modal?.startsWith("ticket-detail:")) {
        const ticket = state.activeTicketData || {};
        const messages = ticket.messages || [];
        return `<div class="modal-backdrop" data-action="close-modal"><section class="modal" role="dialog" aria-modal="true" style="max-width:680px;height:620px;display:flex;flex-direction:column;">
          <div class="modal-head"><div><span class="eyebrow">Ticket #${esc(ticket.id || "")} · ${esc(ticket.category || "")}</span><h2>${esc(ticket.subject || "Support Ticket")}</h2><p>Status: <span class="badge ${ticket.status === 'Open' ? 'badge-amber' : ticket.status === 'Resolved' ? 'badge-green' : 'badge-blue'}">${esc(ticket.status || "")}</span> · Priority: ${esc(ticket.priority || "")}</p></div><button class="icon-button" aria-label="Close dialog" data-action="close-modal">×</button></div>
          <div style="flex:1;overflow-y:auto;padding:14px;background:#faf8f6;border-radius:10px;display:flex;flex-direction:column;gap:10px;">
            ${state.ticketDetailLoading ? `<p style="text-align:center;color:#78716c">Loading ticket details…</p>` : messages.map(m => {
              const isAdmin = m.sender_role === "admin";
              return `<div style="background:${m.is_internal ? '#fef3c7' : '#ffffff'};border:1px solid ${m.is_internal ? '#fde68a' : '#e7e5e4'};padding:10px 14px;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,0.04);">
                <div style="display:flex;justify-content:space-between;font-size:11px;color:#78716c;margin-bottom:4px;"><strong>${esc(m.sender_name)} (${esc(m.sender_role)})${m.is_internal ? ' [INTERNAL NOTE]' : ''}</strong><time>${new Date(m.created_at).toLocaleString()}</time></div>
                <div style="font-size:13px;line-height:1.45;color:#1c1917">${esc(m.message)}</div>
              </div>`;
            }).join("")}
          </div>
          ${state.user.role === "admin" ? `<div style="padding-top:10px;display:flex;gap:8px;align-items:center;">
            <select class="select" id="admin-ticket-status-select" style="max-width:140px"><option ${ticket.status === 'Open' ? 'selected' : ''}>Open</option><option ${ticket.status === 'In Progress' ? 'selected' : ''}>In Progress</option><option ${ticket.status === 'Resolved' ? 'selected' : ''}>Resolved</option><option ${ticket.status === 'Closed' ? 'selected' : ''}>Closed</option></select>
            <button class="btn btn-quiet btn-small" data-action="update-ticket-status" data-id="${esc(ticket.id)}">Update Status</button>
          </div>` : ""}
          <form id="ticket-reply-form" data-ticket-id="${esc(ticket.id)}" style="display:flex;gap:8px;padding-top:10px;">
            <input class="input" id="ticket-reply-input" placeholder="Type your reply…" autocomplete="off" required style="flex:1;" />
            ${state.user.role === "admin" ? `<label style="display:flex;align-items:center;gap:4px;font-size:11px;cursor:pointer;"><input type="checkbox" id="ticket-reply-internal" /> Internal note</label>` : ""}
            <button class="btn btn-primary" type="submit">Reply →</button>
          </form>
        </section></div>`;
      }

      return "";
    },

    // ----------------------------------------------------
    // Practice Module View (Candidate)
    // ----------------------------------------------------
    practicePage() {
      const techs = state.practiceTechs || [];
      const questions = state.practiceQuestions || [];
      const analytics = state.practiceAnalytics || { total_attempts: 0, accuracy_percentage: 0 };
      const currentQ = questions[state.practiceCurrentIndex];

      return `${heading("Master your technical craft", "Technical Practice", "Sharpen your knowledge with real-world technical interview and assessment questions.")}
        <section class="grid stats-grid" style="margin-bottom:16px;">
          ${stat("Practice questions attempted", analytics.total_attempts, "Total questions submitted", "✓")}
          ${stat("Practice accuracy", `${analytics.accuracy_percentage}%`, "Correct answer rate", "★")}
          ${stat("Focus skills", techs.length, "Active technologies tracked", "▣")}
          ${stat("Skill level", analytics.accuracy_percentage >= 80 ? "Advanced" : analytics.accuracy_percentage >= 50 ? "Intermediate" : "Foundational", "Based on practice results", "↗")}
        </section>

        <section class="columns" style="margin-bottom:16px;">
          <article class="card">
            <div class="card-heading"><div><h2>Tracked Technologies</h2><p>Choose technologies to practice</p></div></div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px;">
              ${techs.map(t => `<span class="skill" style="display:inline-flex;align-items:center;gap:6px;background:#fee2e2;color:#991b1b;padding:6px 12px;border-radius:100px;font-weight:600;">${esc(t)} <button type="button" data-action="remove-practice-tech" data-tech="${esc(t)}" style="border:0;background:transparent;cursor:pointer;color:#991b1b;font-weight:bold;">×</button></span>`).join("")}
            </div>
            <form id="add-practice-tech-form" style="display:flex;gap:8px;">
              <input class="input" id="new-practice-tech" placeholder="e.g. Python, React, SQL, Docker" required />
              <button class="btn btn-quiet" type="submit">Add Skill</button>
            </form>
            <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;">
              ${["Python", "JavaScript", "React", "SQL", "HTML/CSS"].map(t => `<button class="btn btn-quiet btn-small" data-action="quick-practice-tech" data-tech="${esc(t)}">Practice ${t} →</button>`).join("")}
            </div>
          </article>

          <article class="card">
            <div class="card-heading"><div><h2>Quick Practice Session</h2><p>10-question rapid drill</p></div></div>
            <p style="font-size:13px;color:#57534e;line-height:1.6">Practice randomized questions curated from top engineering technical screenings to prepare for live assessments.</p>
            <div style="margin-top:14px;display:flex;gap:10px;">
              <button class="btn btn-primary" data-action="start-quick-practice">Start Practice Drill →</button>
            </div>
          </article>
        </section>

        ${currentQ ? `
          <section class="card" style="border:2px solid #fed7aa;background:white;">
            <div class="card-heading">
              <div>
                <span class="eyebrow">${esc(currentQ.technology)} · ${esc(currentQ.difficulty)}</span>
                <h2>Question ${state.practiceCurrentIndex + 1} of ${questions.length}</h2>
              </div>
            </div>
            <p style="font-size:15px;font-weight:600;color:#1c1917;margin:12px 0 16px;">${esc(currentQ.question)}</p>
            <div style="display:flex;flex-direction:column;gap:10px;margin-bottom:16px;">
              ${(currentQ.options || []).map((opt, idx) => {
                const isSelected = state.practiceSelectedOption === idx;
                let bg = isSelected ? '#fee2e2' : '#f8fafc';
                let border = isSelected ? '1px solid var(--accent)' : '1px solid var(--line)';
                if (state.practiceSubmitted) {
                  if (idx === state.practiceLastResult?.correct_index) {
                    bg = '#dcfce7'; border = '1px solid #16a34a';
                  } else if (isSelected && !state.practiceLastResult?.is_correct) {
                    bg = '#fee2e2'; border = '1px solid #dc2626';
                  }
                }
                return `<button type="button" class="practice-opt-btn" data-action="select-practice-option" data-index="${idx}" ${state.practiceSubmitted ? 'disabled' : ''} style="text-align:left;padding:12px 16px;border-radius:10px;background:${bg};border:${border};cursor:pointer;font-size:13px;display:flex;gap:10px;align-items:center;">
                  <strong style="width:24px;height:24px;border-radius:50%;display:grid;place-items:center;background:white;font-size:11px;border:1px solid #cbd5e1;">${String.fromCharCode(65 + idx)}</strong>
                  <span>${esc(opt)}</span>
                </button>`;
              }).join("")}
            </div>

            ${state.practiceSubmitted ? `
              <div style="padding:14px;border-radius:10px;background:${state.practiceLastResult?.is_correct ? '#f0fdf4' : '#fffbeb'};border:1px solid ${state.practiceLastResult?.is_correct ? '#86efac' : '#fde68a'};margin-bottom:16px;">
                <strong style="color:${state.practiceLastResult?.is_correct ? '#166534' : '#92400e'}">${state.practiceLastResult?.is_correct ? '✓ Correct! Great job.' : '✗ Incorrect.'}</strong>
                ${state.practiceLastResult?.explanation ? `<p style="margin:6px 0 0;font-size:12px;color:#374151">${esc(state.practiceLastResult.explanation)}</p>` : ''}
              </div>
            ` : ''}

            <div style="display:flex;justify-content:flex-end;gap:10px;">
              ${!state.practiceSubmitted ? `
                <button class="btn btn-primary" data-action="submit-practice-answer" ${state.practiceSelectedOption === null ? 'disabled' : ''}>Check Answer →</button>
              ` : state.practiceCurrentIndex + 1 < questions.length ? `
                <button class="btn btn-primary" data-action="next-practice-question">Next Question →</button>
              ` : `
                <button class="btn btn-primary" data-action="finish-practice-drill">Drill Complete! Review Stats →</button>
              `}
            </div>
          </section>
        ` : ''}`;
    },

    // ----------------------------------------------------
    // Support Tickets Page
    // ----------------------------------------------------
    supportPage() {
      const tickets = state.supportTickets || [];
      const isAdmin = state.user?.role === "admin";

      return `${heading("SkillSphere Help & Escalation", "Support & Inquiries", "Get fast assistance with your account, assessments, interviews, or hiring needs.", `<button class="btn btn-primary" data-action="open-new-ticket">＋ Open New Ticket</button>`)}
        <section class="card">
          <div class="card-heading">
            <div>
              <h2>${isAdmin ? "Support Ticket Queue" : "Your Support Tickets"} <span class="badge">${tickets.length}</span></h2>
              <p>${isAdmin ? "Review and respond to candidate and recruiter support inquiries." : "Track status and responses for your tickets."}</p>
            </div>
          </div>
          ${tickets.length ? `
            <div class="table-wrap"><table>
              <thead><tr><th>#</th><th>Subject</th><th>Category</th><th>Priority</th><th>Status</th><th>Updated</th><th>Action</th></tr></thead>
              <tbody>
                ${tickets.map(t => `<tr>
                  <td><strong>#${esc(t.id)}</strong></td>
                  <td><strong>${esc(t.subject)}</strong></td>
                  <td><span class="badge">${esc(t.category)}</span></td>
                  <td><span class="badge ${t.priority === 'High' || t.priority === 'Urgent' ? 'badge-red' : ''}">${esc(t.priority)}</span></td>
                  <td><span class="badge ${t.status === 'Resolved' ? 'badge-green' : t.status === 'Open' ? 'badge-amber' : 'badge-blue'}">${esc(t.status)}</span></td>
                  <td>${dateLabel(t.updated_at || t.created_at)}</td>
                  <td><button class="text-button" data-action="view-ticket-detail" data-id="${esc(t.id)}">View & Reply →</button></td>
                </tr>`).join("")}
              </tbody>
            </table></div>
          ` : empty("No support tickets", "Need help? Open a new ticket anytime.")}
        </section>`;
    },

    // ----------------------------------------------------
    // Admin Audit Logs Page
    // ----------------------------------------------------
    adminAuditLogsPage() {
      const logs = state.adminAuditLogs || [];
      return `${heading("Platform Security & Compliance", "Audit Logs", "Permanent tamper-evident audit history of sensitive actions and decisions.")}
        <section class="card">
          <div class="card-heading"><div><h2>System Audit Trail <span class="badge">${logs.length}</span></h2><p>Latest recorded platform events</p></div><button class="btn btn-quiet btn-small" data-action="refresh-audit-logs">Refresh Logs</button></div>
          ${logs.length ? `
            <div class="table-wrap"><table>
              <thead><tr><th>Timestamp</th><th>Action</th><th>Actor</th><th>Entity</th><th>Details</th><th>IP</th></tr></thead>
              <tbody>
                ${logs.map(l => `<tr>
                  <td><small>${new Date(l.created_at).toLocaleString()}</small></td>
                  <td><strong style="color:var(--accent)">${esc(l.action)}</strong></td>
                  <td>${esc(l.actor_email || `ID:${l.actor_id}`)}</td>
                  <td><span class="badge">${esc(l.target_type)}:${esc(l.target_id || "—")}</span></td>
                  <td><small>${esc(JSON.stringify(l.details || {}))}</small></td>
                  <td><small style="color:#78716c">${esc(l.ip_address || "—")}</small></td>
                </tr>`).join("")}
              </tbody>
            </table></div>
          ` : empty("No audit records found", "Audit logs will populate as platform actions occur.")}
        </section>`;
    }
  };
})();
