// Presentation only. No provider credentials, evidence inference, or fake scores.
export const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export const icons = {
  conversation:
    '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z"/>',
  history: '<path d="M3 11a9 9 0 1 1 2.8 7M3 4v7h7"/><path d="M12 7v5l3 2"/>',
  sliders:
    '<path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/>',
  spark:
    '<path d="m12 3 2.7 6.3L21 12l-6.3 2.7L12 21l-2.7-6.3L3 12l6.3-2.7L12 3ZM20 2v4m-2-2h4"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  shield:
    '<path d="m12 3 8 3v5c0 5-4 8-8 10-4-2-8-5-8-10V6l8-3Z"/><path d="m9 12 2 2 4-4"/>',
  headphones:
    '<path d="M3 14v-3a9 9 0 0 1 18 0v3"/><rect x="3" y="12" width="4" height="8" rx="2"/><rect x="17" y="12" width="4" height="8" rx="2"/>',
  wave: '<path d="M3 10v4m4-8v12m5-16v20m5-16v12m4-8v4"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
};
export const icon = (name, cls = "") =>
  `<svg class="icon ${cls}" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.spark}</svg>`;
const words = {
  en: {
    studio: "Personal AI studio",
    conversation: "Conversation",
    history: "Your sessions",
    settings: "Workspace",
    tools: "Review tools",
    private: "Private workspace",
    workspace: "Your representative",
    tag: "BUILT AROUND YOU",
    homeTitle: "Your expertise.\nYour voice.\nYour representative.",
    homeText:
      "Teach Raneen through conversation. Help it understand how you think, respond, and work with your clients.",
    start: "Let’s get to know you",
    resume: "Pick up where you left off",
    startText:
      "No scripts. No forms to fill. Just a conversation about the work you know.",
    resumeText:
      "Your saved conversation is here. Keep teaching, or hear how your representative is coming along.",
    talk: "Start a conversation",
    promptLabel: "A good place to start",
    prompt: "“A buyer says the price is too high. What do you say next?”",
    promptNote: "An example of what you can teach Raneen.",
    teaching: "Teaching",
    practice: "Practice",
    voiceReview: "Your voice",
    saved: "Saved audio",
    learning: "What Raneen is learning",
    learningIntro: "Built from what you actually say and confirm.",
    emptyLearning: "It starts with listening.",
    emptyLearningText:
      "Your confirmed examples will build up as you talk. Correct Raneen whenever it misses your meaning.",
    confirmed: "Confirmed examples",
    savedLearning: "Your teaching is taking shape.",
    savedLearningText:
      "Your confirmed examples are saved. Keep teaching with a new situation, or try your representative.",
    tentative: "Still learning",
    confirmedRule: "Confirmed by you",
    locked: "Your explicit rule",
    conflict: "Needs clarification",
    pending: "Waiting for confirmation",
    voice: "Your voice",
    voiceEmpty: "Your natural voice comes first.",
    voiceEmptyText:
      "Keep talking. When a sample is ready, you can listen and decide whether it sounds like you.",
    voiceApproved: "Voice approved by you",
    voiceReady: "Ready for you to listen",
    voiceBuilding: "Preparing your voice",
    voiceWaiting: "Listening comes first",
    voiceNote: "A new voice is only used after you approve it.",
    conversationTitle: "Teach it your way.",
    conversationText:
      "Talk through a real situation. Show Raneen how you would handle it.",
    sayIt: "Say it your way.",
    sayItText: "Your words, your pauses, your way of seeing things.",
    conversationLog: "Conversation",
    you: "You",
    raneen: "Raneen",
    emptyLog: "Your conversation will appear here as you speak.",
    privateNote: "Your microphone is off until you start.",
    tips: "A little guidance",
    tipsText:
      "Use headphones. Speak in your natural dialect. You can interrupt, ask for a repeat, or correct an answer.",
    noHistory: "A fresh start.",
    noHistoryText:
      "Your conversations will be kept here so you can come back to them.",
    newSession: "New conversation",
    close: "Close",
    workspaceTitle: "Your workspace",
    workspaceText: "Voice and conversation services for this private preview.",
    notConnected: "Not connected yet",
    learningUnavailable:
      "Learning details will appear when this session is connected to the learning engine.",
    learningFailed:
      "Couldn’t refresh learning. Your conversation is still saved.",
    version: "Personal model",
    draft: "Draft",
    evidence: "Based on your examples",
    confirmedOnly: "Only confirmed learning is shown as confirmed.",
    rejectedVoice: "Keep teaching instead",
    rejectNote:
      "This voice has not been approved. Keep talking and review it again when you’re ready.",
    showMore: "Connection & microphone",
    connected: "Connected",
    speaking: "Raneen is speaking",
    thinking: "Raneen is thinking",
    listening: "Listening to you",
    practiceHint: "You play the client. Raneen tries your approach.",
    privacy: "Your voice stays yours.",
    privacyText:
      "You choose what to share, approve the voice you hear, and can withdraw consent at any time.",
    loginTitle: "An AI representative,\nwith your way\nof doing things.",
    loginText: "For the conversations only you know how to have.",
    loginNote: "Made for real estate. Taught by you.",
    readinessNote:
      "These are connection settings. They do not verify voice quality.",
    firstConversation: "Your first conversation",
    recent: "RECENT",
    savedSession: "Saved conversation",
    noFakeScore: "Learning from you, one conversation at a time.",
    backTeaching: "Back to teaching",
    listeningHelp: "No need to type anything.",
    voiceStep: "Listen. Decide. Keep talking.",
    preferences: "Language",
    practiceReady: "Try your representative.",
    reviewSummary:
      "A few fresh lines in your voice. Listen to each, then decide.",
    retry: "Try again",
  },
  ar: {
    studio: "استوديو مساعدك الشخصي",
    conversation: "المحادثة",
    history: "جلساتك",
    settings: "مساحة العمل",
    tools: "أدوات المراجعة",
    private: "مساحة خاصة",
    workspace: "مساعدك الشخصي",
    tag: "يتعلّم منك",
    homeTitle: "خبرتك.\nصوتك.\nمساعد يمثّلك.",
    homeText:
      "علّم رنين من خلال المحادثة. ساعده يفهم كيف تفكّر، وكيف تردّ وتتعامل مع عملائك.",
    start: "خلّنا نتعرّف عليك",
    resume: "كمّل من حيث توقّفت",
    startText: "بدون نصوص جاهزة أو كتابة. محادثة عن الشغل اللي تعرفه.",
    resumeText: "محادثتك محفوظة. كمّل تعليمه، أو اسمع كيف صار مساعدك.",
    talk: "ابدأ محادثة",
    promptLabel: "بداية بسيطة",
    prompt: "«عميل يقول إن السعر مرتفع. كيف تردّ عليه؟»",
    promptNote: "مثال على المواقف اللي تقدر تعلّمها لرنين.",
    teaching: "تعليم",
    practice: "تجربة",
    voiceReview: "صوتك",
    saved: "الصوت المحفوظ",
    learning: "ما يتعلّمه رنين منك",
    learningIntro: "من كلامك والأمثلة اللي تؤكّدها بنفسك.",
    emptyLearning: "البداية بالاستماع.",
    emptyLearningText:
      "أمثلتك المؤكّدة تتجمّع وأنت تتكلّم. صحّح لرنين إذا ما فهم قصدك.",
    confirmed: "أمثلة مؤكّدة",
    savedLearning: "تعليمك بدأ يترك أثره.",
    savedLearningText:
      "أمثلتك المؤكّدة محفوظة. علّمه موقف جديد، أو جرّب مساعدك.",
    tentative: "ما زال يتعلّم",
    confirmedRule: "أكّدته بنفسك",
    locked: "قاعدة حدّدتها",
    conflict: "يحتاج توضيح",
    pending: "بانتظار تأكيدك",
    voice: "صوتك",
    voiceEmpty: "صوتك الطبيعي أولاً.",
    voiceEmptyText: "كمّل كلامك. لما تجهز العيّنة، تسمعها وتقرّر إذا تشبهك.",
    voiceApproved: "اعتمدت هذا الصوت",
    voiceReady: "جاهز للاستماع",
    voiceBuilding: "نجهّز صوتك",
    voiceWaiting: "نبدأ بالاستماع",
    voiceNote: "ما نستخدم نسخة صوت جديدة إلا بعد موافقتك.",
    conversationTitle: "علّمه طريقتك.",
    conversationText: "احكِ عن موقف من شغلك. ورّه كيف تتعامل معه.",
    sayIt: "بكلامك وطريقتك.",
    sayItText: "كلماتك، وقفاتك، وطريقتك في فهم الأمور.",
    conversationLog: "المحادثة",
    you: "أنت",
    raneen: "رنين",
    emptyLog: "كلامك يظهر هنا أثناء المحادثة.",
    privateNote: "الميكروفون مغلق لين تبدأ.",
    tips: "قبل ما نبدأ",
    tipsText:
      "استخدم سماعات وتكلّم بلهجتك الطبيعية. تقدر تقاطعه أو تطلب يعيد كلامه أو تصحّح له.",
    noHistory: "بداية جديدة.",
    noHistoryText: "محادثاتك تنحفظ هنا عشان تقدر ترجع لها.",
    newSession: "محادثة جديدة",
    close: "إغلاق",
    workspaceTitle: "مساحة عملك",
    workspaceText: "خدمات الصوت والمحادثة في تجربتك الخاصة.",
    notConnected: "غير متّصل بعد",
    learningUnavailable:
      "تفاصيل التعلّم تظهر لما تتّصل هذه الجلسة بمحرك التعلّم.",
    learningFailed: "تعذّر تحديث التعلّم. محادثتك ما زالت محفوظة.",
    version: "النموذج الشخصي",
    draft: "مسوّدة",
    evidence: "مبني على أمثلتك",
    confirmedOnly: "نميّز التعلّم المؤكّد عن الاستنتاجات.",
    rejectedVoice: "أكمل التعليم الآن",
    rejectNote: "ما اعتمدنا هذا الصوت. كمّل الكلام وراجعه لما تكون جاهز.",
    showMore: "الاتصال والميكروفون",
    connected: "متّصل",
    speaking: "رنين يتكلّم",
    thinking: "رنين يفكّر",
    listening: "نسمعك",
    practiceHint: "أنت العميل، ورنين يجرّب طريقتك.",
    privacy: "صوتك يبقى لك.",
    privacyText:
      "أنت تختار ما تشاركه، وتعتمد الصوت اللي تسمعه، وتقدر تسحب موافقتك بأي وقت.",
    loginTitle: "مساعد ذكي،\nبنفس طريقتك\nفي التعامل.",
    loginText: "للمحادثات اللي أنت تعرف كيف تديرها.",
    loginNote: "للعقار. يتعلّم منك.",
    readinessNote: "هذه إعدادات الاتصال، ولا تقيس جودة الصوت.",
    firstConversation: "محادثتك الأولى",
    recent: "الأخيرة",
    savedSession: "محادثة محفوظة",
    noFakeScore: "يتعلّم منك، محادثة بعد محادثة.",
    backTeaching: "العودة للتعليم",
    listeningHelp: "بدون ما تكتب أي شيء.",
    voiceStep: "اسمع. قرّر. وكمّل.",
    preferences: "اللغة",
    practiceReady: "جرّب مساعدك.",
    reviewSummary: "جمل جديدة بصوتك. اسمع كل عيّنة، وبعدها قرّر.",
    retry: "حاول مرة ثانية",
  },
};
export const uiText = (lang, key) => words[lang]?.[key] || words.en[key] || key;
export const orb = (id = "", small = false) =>
  `<div ${id ? `id="${id}"` : ""} class="orb ${small ? "orb-small" : ""}" aria-hidden="true"><div class="orb-core"></div><div class="orb-ring ring-one"></div><div class="orb-ring ring-two"></div><div class="orb-glint"></div></div>`;
export function learningPanel({
  lang,
  journey = {},
  workflow = {},
  learning = null,
  learningError = false,
}) {
  const u = (k) => uiText(lang, k),
    e = escapeHTML;
  const confirmed = journey.confirmed_patterns || 0,
    pending = journey.pending_patterns || 0;
  const observations = (learning?.hypotheses || [])
    .filter((x) => x.state !== "rejected")
    .slice(0, 4);
  const voiceLabel = workflow.voice_approved
    ? "voiceApproved"
    : workflow.voice_state === "ready"
      ? "voiceReady"
      : ["creating", "building"].includes(workflow.voice_state)
        ? "voiceBuilding"
        : "voiceWaiting";
  return `<aside class="learning-panel" aria-label="${u("learning")}"><div class="panel-heading">${icon("spark")}<h2>${u("learning")}</h2></div><p class="panel-intro">${u("learningIntro")}</p><div class="learning-body">${observations.length ? observations.map((x) => `<article class="learned-item"><span class="learning-kind">${e(u(x.conflict ? "conflict" : x.state === "confirmed" ? "confirmedRule" : x.state === "locked" ? "locked" : "tentative"))}</span><p dir="auto">${e(ruleText(x))}</p>${x.evidence_count ? `<span class="small muted">${e(x.evidence_count)} · ${u("evidence")}</span>` : ""}</article>`).join("") : `<div class="learning-empty"><div class="empty-glyph">${icon("conversation")}</div><h3>${u(confirmed ? "savedLearning" : "emptyLearning")}</h3><p>${u(confirmed ? "savedLearningText" : "emptyLearningText")}</p></div>`}<div class="fact-row"><span>${u("confirmed")}</span><strong id="learningConfirmed">${confirmed}</strong></div>${pending ? `<div class="fact-row"><span>${u("pending")}</span><strong>${pending}</strong></div>` : ""}${learning?.profile_version ? `<div class="fact-row"><span>${u("version")}</span><strong>v${e(learning.profile_version.version_number)} · ${e(learning.profile_version.status)}</strong></div>` : ""}${learningError ? `<p class="small muted">${u("learningFailed")}</p>` : ""}</div><section class="voice-panel"><div class="panel-heading">${icon("wave")}<h2>${u("voice")}</h2></div><div class="voice-mini-wave" aria-hidden="true">${Array.from({ length: 21 }, (_, i) => `<i class="bar-${i % 7}"></i>`).join("")}</div><span class="voice-state">${workflow.voice_approved ? icon("check") : ""}${u(voiceLabel)}</span><p>${u("voiceNote")}</p></section><div class="panel-foot">${icon("shield")}<span>${u("noFakeScore")}</span></div></aside>`;
}

function ruleText(item) {
  if (typeof item.value === "string") return item.value;
  const value = item.value || {};
  const text =
    value.summary ||
    value.pattern ||
    value.instruction ||
    value.action ||
    value.description ||
    item.summary;
  return typeof text === "string"
    ? text.replace(/_/g, " ")
    : String(item.key || "").replace(/[_.]/g, " ");
}
