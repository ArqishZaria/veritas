"""Updates app/templates/index.html (your A1 file) to the /api/v1 API. Every replacement is
asserted, so it fails loudly instead of half-patching. Safe to re-run.
    python scripts/patch_frontend.py"""
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app/templates/index.html")
src = open(PATH, encoding="utf-8").read()
if "currentSessionId" in src:
    raise SystemExit("index.html already patched")

R = []
R.append(("let currentUser = null;\n", "let currentUser = null;\nlet currentSessionId = null;\n"))
R.append(('"/api/login" : "/api/register"', '"/api/v1/auth/login" : "/api/v1/auth/register"'))
R.append(('showToast(data.detail || "Authentication failed.")',
          'showToast((data.error && data.error.message) || "Authentication failed.")'))
R.append(('fetch("/api/quizzes" + (search', 'fetch("/api/v1/quizzes" + (search'))
R.append(('fetch("/api/history/" + currentUser.user_id)',
          'fetch("/api/v1/users/" + currentUser.user_id + "/attempts")'))
R.append(("fetch(`/api/attempts/${attemptId}/review?user_id=${currentUser.user_id}`)",
          "fetch(`/api/v1/attempts/${attemptId}?user_id=${currentUser.user_id}`)"))
R.append(('fetch("/api/quizzes/" + currentQuiz.id)', 'fetch("/api/v1/quizzes/" + currentQuiz.id)'))
R.append(('''  await fetch("/api/exam/start", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({quiz_id: quiz.id, user_id: currentUser.user_id})
  });
''', '''  const startRes = await fetch(`/api/v1/quizzes/${quiz.id}/sessions`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({user_id: currentUser.user_id})
  });
  if (!startRes.ok) {
    const err = await startRes.json();
    showToast((err.error && err.error.message) || "Couldn't start the exam.");
    return;
  }
  currentSessionId = (await startRes.json()).session_id;
'''))
R.append(('''    await fetch("/api/exam/tab_switch", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({user_id: currentUser.user_id, quiz_id: currentQuiz.id})
    });''', '''    await fetch(`/api/v1/sessions/${currentSessionId}/tab-switches`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({user_id: currentUser.user_id})
    });'''))
R.append(('''  const res = await fetch("/api/exam/submit", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      quiz_id: currentQuiz.id, user_id: currentUser.user_id,
      answers, tab_switch_count: tabSwitchCount
    })
  });
  const result = await res.json();
''', '''  // PUT is idempotent: the session id is the key, so a retried submit can never double-record.
  const res = await fetch(`/api/v1/sessions/${currentSessionId}/attempt`, {
    method: "PUT", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({user_id: currentUser.user_id, answers, tab_switch_count: tabSwitchCount})
  });
  const result = await res.json();
  if (!res.ok) {
    document.getElementById("examModal").classList.add("hidden");
    showToast((result.error && result.error.message) || "Submission failed.");
    return;
  }
'''))
R.append(('"/api/admin/', '"/api/v1/admin/'))
R.append(("`/api/admin/", "`/api/v1/admin/"))
R.append(('showToast(err.detail || "Couldn\'t create the quiz.")',
          'showToast((err.error && err.error.message) || "Couldn\'t create the quiz.")'))
for old, new in R:
    assert old in src, f"pattern not found (is this the A1 file?): {old[:70]!r}"
    src = src.replace(old, new)
for leftover in ("/api/exam/", "/api/history", "/api/login", "/api/register", "/api/admin/", "/api/quizzes"):
    assert leftover not in src.replace("/api/v1/", ""), f"unpatched reference left: {leftover}"
open(PATH, "w", encoding="utf-8").write(src)
print(f"patched {len(R)} patterns in {PATH}")
