import { escapeHTML as esc } from "./conversation-ui.js";

const words = {
  en: {
    invite: "Invite a tester",
    intro: "Each invitation opens a separate private workspace. Your owner code and recordings stay private.",
    allowance: "One enrollment per tester, up to 30 minutes of teaching and three short agent calls. Invitations and access expire after seven days. Up to ten testers in this test cohort.",
    create: "Create invitation",
    code: "Invitation code",
    once: "This code is shown once. Send it privately with the app link. It can be used by one tester.",
    copy: "Copy invitation",
    copied: "Copied",
    expires: "Expires",
    active: "Waiting for tester",
    redeemed: "Tester joined",
    revoked: "Revoked",
    revoke: "Revoke access",
    empty: "No invitations yet.",
    returnTitle: "Save your access code",
    returnText: "Your invitation has opened your own private workspace. Save this new code somewhere private to return after closing or refreshing this page.",
    returnCode: "Your access code",
    continue: "I saved my code — continue",
    copyCode: "Copy access code",
    disabled: "Tester invitations are not enabled on this release.",
    login: "Enter your access code or tester invitation.",
    preview: "Private test",
    footer: "Private test · No customer calls",
    accessEnded: "Your test access has ended. You can still stop or revoke your saved session.",
    accessRevoked: "The owner revoked this test access. Ask them about the next test.",
    cohortFull: "This ten-person test cohort is full.",
    capacityBusy: "Another test is using the voice connection. Try again when it finishes.",
    sessionLimit: "Your invitation includes one conversation. Open your saved session to continue.",
    storageFull: "Testing is paused because recording storage is low. Contact the owner.",
    signInBusy: "Too many sign-in attempts. Wait one minute, then try again.",
    invalidInvite: "This invitation is expired or already used. Use your saved personal access code to return.",
  },
  ar: {
    invite: "دعوة شخص للتجربة",
    intro: "كل دعوة تفتح مساحة خاصة مستقلة. رمز المالك وتسجيلاتك تبقى خاصة بك.",
    allowance: "جلسة واحدة لكل شخص، حتى ٣٠ دقيقة تعليم وثلاث مكالمات قصيرة لتجربة المساعد. الدعوة والدخول لمدة سبعة أيام، وحتى عشرة أشخاص في هذه التجربة.",
    create: "إنشاء دعوة",
    code: "رمز الدعوة",
    once: "يظهر الرمز مرة واحدة. أرسله بشكل خاص مع رابط التطبيق. يستخدمه شخص واحد فقط.",
    copy: "نسخ الدعوة",
    copied: "تم النسخ",
    expires: "تنتهي",
    active: "بانتظار الشخص",
    redeemed: "تم الانضمام",
    revoked: "أُلغي الدخول",
    revoke: "إلغاء الدخول",
    empty: "لا توجد دعوات بعد.",
    returnTitle: "احفظ رمز الدخول",
    returnText: "فتحت الدعوة مساحتك الخاصة. احفظ هذا الرمز الجديد في مكان خاص لترجع بعد إغلاق الصفحة أو تحديثها.",
    returnCode: "رمز الدخول الخاص بك",
    continue: "حفظت الرمز — متابعة",
    copyCode: "نسخ رمز الدخول",
    disabled: "دعوات التجربة غير مفعّلة في هذا الإصدار.",
    login: "أدخل رمز الدخول أو رمز دعوة التجربة.",
    preview: "تجربة خاصة",
    footer: "تجربة خاصة · بدون مكالمات للعملاء",
    accessEnded: "انتهت مدة دخول التجربة. تقدر توقف أو تلغي جلستك المحفوظة.",
    accessRevoked: "المالك ألغى دخول هذه التجربة. تواصل معه بخصوص التجربة التالية.",
    cohortFull: "اكتمل عدد الأشخاص العشرة في هذه التجربة.",
    capacityBusy: "تجربة أخرى تستخدم الاتصال الصوتي. حاول بعد انتهائها.",
    sessionLimit: "الدعوة تشمل محادثة واحدة. افتح جلستك المحفوظة لتكمل.",
    storageFull: "التجربة متوقفة بسبب قلة مساحة التسجيلات. تواصل مع المالك.",
    signInBusy: "محاولات دخول كثيرة. انتظر دقيقة ثم حاول.",
    invalidInvite: "الدعوة انتهت أو استُخدمت. للرجوع، استخدم رمز الدخول الشخصي اللي حفظته.",
  },
};
export const testerText = (lang, key) => words[lang === "en" ? "en" : "ar"][key];
export function testerFailure(lang, error) {
  const keys = {
    tester_access_expired: "accessEnded", tester_access_paused: "accessEnded",
    tester_access_revoked: "accessRevoked", tester_cohort_limit: "cohortFull",
    tester_capacity_busy: "capacityBusy", tester_session_limit: "sessionLimit",
    tester_storage_full: "storageFull", tester_redemption_busy: "signInBusy",
    tester_invitation_invalid: "invalidInvite",
  };
  const key = keys[error?.detail?.code];
  return key ? testerText(lang, key) : null;
}

export async function signInWithCode({ code, request, post, setToken }) {
  setToken(code);
  try {
    const user = await request("/api/me");
    if (!["admin", "tester"].includes(user.role))
      throw Object.assign(Error("access_denied"), { status: 403 });
    return { user };
  } catch (e) {
    setToken("");
    if (e.status !== 401) throw e;
  }
  const result = await post("/api/testing/redeem", { code });
  setToken(result.token);
  return { user: result.user, newToken: result.token };
}

function codeField(label, value) {
  return `<label>${esc(label)}<input class="access-code" type="text" readonly autocomplete="off" spellcheck="false" dir="ltr" value="${esc(value)}"></label>`;
}
async function copyValue(button, value, label) {
  try {
    await navigator.clipboard.writeText(value);
    button.textContent = label;
  } catch {
    // The adjacent read-only field can always be selected and copied manually.
    button.parentElement.querySelector("input")?.select();
  }
}

export function saveTesterCode({ dialog, lang, token }) {
  const t = (key) => testerText(lang, key);
  return new Promise((resolve) => {
    const d = dialog(t("returnTitle"), `<p>${t("returnText")}</p>${codeField(t("returnCode"), token)}<button id="copyTesterCode" class="secondary">${t("copyCode")}</button><button id="testerContinue" class="primary">${t("continue")}</button>`);
    d.querySelector("#copyTesterCode").onclick = (e) => copyValue(e.currentTarget, token, t("copied"));
    d.querySelector("#testerContinue").onclick = () => d.close();
    d.addEventListener("close", () => {
      // Remove rendered credentials on dismissal; only the page's memory retains access.
      d.replaceChildren();
      resolve();
    }, { once: true });
  });
}

export async function showTesterInvitations({ dialog, lang, request, post, report }) {
  const t = (key) => testerText(lang, key);
  const d = dialog(t("invite"), `<p>${t("intro")}</p><p class="small muted">${t("allowance")}</p><button id="createTesterInvite" class="primary">${t("create")}</button><div id="newTesterInvite"></div><div id="testerInviteList" role="status"></div>`);
  const list = d.querySelector("#testerInviteList");
  const refresh = async () => {
    const result = await request("/api/testing/invitations");
    if (!d.isConnected) return;
    list.innerHTML = result.items.length ? `<ul class="session-list">${result.items.map((item, i) => `<li><div><strong>${t("invite")} ${i + 1}</strong><p>${item.revoked ? t("revoked") : item.redeemed ? t("redeemed") : t("active")} · ${t("expires")} ${esc(new Date(item.expires_at).toLocaleDateString(lang === "en" ? "en-GB" : "ar-AE"))}</p></div>${item.revoked ? "" : `<button class="secondary" data-revoke-invite="${esc(item.id)}">${t("revoke")}</button>`}</li>`).join("")}</ul>` : `<p class="small">${t("empty")}</p>`;
    list.querySelectorAll("[data-revoke-invite]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try { await post(`/api/testing/invitations/${encodeURIComponent(b.dataset.revokeInvite)}/revoke`); await refresh(); }
        catch (e) { b.disabled = false; report(e); }
      };
    });
  };
  d.querySelector("#createTesterInvite").onclick = async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    try {
      const result = await post("/api/testing/invitations");
      if (!d.isConnected) return;
      const area = d.querySelector("#newTesterInvite");
      area.innerHTML = `<section class="card"><p>${t("once")}</p>${codeField(t("code"), result.code)}<button id="copyTesterInvite" class="secondary">${t("copy")}</button></section>`;
      area.querySelector("#copyTesterInvite").onclick = (e) => copyValue(e.currentTarget, `${location.origin}/enroll\n${t("code")}: ${result.code}`, t("copied"));
      await refresh();
    } catch (e) { report(e); }
    finally { b.disabled = false; }
  };
  d.addEventListener("close", () => d.replaceChildren(), { once: true });
  await refresh();
}
