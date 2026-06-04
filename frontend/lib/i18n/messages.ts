/**
 * Phase 40 — message bundles for uz / ru / en.
 *
 * Pattern is intentionally tiny: a `Locale` union, a `Messages`
 * type that is the structural superset of every bundle, and three
 * literal objects. No build step, no external library. Adding a
 * new key means adding it to all three bundles — TypeScript will
 * surface any drift.
 *
 * The agent itself mirrors the user's language in answers; this
 * file covers UI chrome only (nav, buttons, form labels, status
 * badges, common error toasts).
 */
export type Locale = "uz" | "ru" | "en";

export const LOCALES: Locale[] = ["uz", "ru", "en"];

export const LOCALE_LABEL: Record<Locale, string> = {
  uz: "O‘zbekcha",
  ru: "Русский",
  en: "English",
};

/** Flat structure on purpose — easier to keep three bundles in
 *  sync than a deeply nested one. Group by area via prefixes. */
export type Messages = {
  // Header / nav
  nav_workspaces: string;
  nav_chat: string;
  nav_settings: string;
  nav_admin: string;
  nav_sign_in: string;
  nav_sign_out: string;
  // Common buttons
  btn_save: string;
  btn_cancel: string;
  btn_delete: string;
  btn_create: string;
  btn_refresh: string;
  btn_close: string;
  btn_back: string;
  btn_loading: string;
  // Workspaces page
  ws_title: string;
  ws_subtitle: string;
  ws_new: string;
  ws_new_name_label: string;
  ws_new_name_placeholder: string;
  ws_empty: string;
  ws_connections_count: (n: number) => string;
  // Connection statuses
  conn_status_pending: string;
  conn_status_profiling: string;
  conn_status_ready: string;
  conn_status_error: string;
  conn_status_auth_error: string;
  // Health dot
  health_ok: string;
  health_fail: string;
  health_unknown: string;
  health_recheck: string;
  // Chat
  chat_input_placeholder: string;
  chat_send: string;
  chat_thinking: string;
  chat_running_node: (node: string) => string;
  chat_similar_label: string;
  chat_dismiss: string;
  chat_no_response: string;
  chat_workspace_pending: string;
  // Login / register
  auth_login_title: string;
  auth_register_title: string;
  auth_username_label: string;
  auth_email_label: string;
  auth_password_label: string;
  auth_login_button: string;
  auth_register_button: string;
  auth_switch_to_register: string;
  auth_switch_to_login: string;
  // Errors
  err_generic: string;
  err_network: string;
  err_unauthorized: string;
  err_not_found: string;
  err_validation: string;
  err_too_large: string;
  err_rate_limited: string;
};


export const MESSAGES: Record<Locale, Messages> = {
  uz: {
    nav_workspaces: "Ishchi maydonlar",
    nav_chat: "Chat",
    nav_settings: "Sozlamalar",
    nav_admin: "Administrator",
    nav_sign_in: "Kirish",
    nav_sign_out: "Chiqish",

    btn_save: "Saqlash",
    btn_cancel: "Bekor qilish",
    btn_delete: "O‘chirish",
    btn_create: "Yaratish",
    btn_refresh: "Yangilash",
    btn_close: "Yopish",
    btn_back: "Orqaga",
    btn_loading: "Yuklanmoqda…",

    ws_title: "Ishchi maydonlar",
    ws_subtitle: "Har bir ishchi maydon — ulangan ma’lumotlar bazalari va hujjatlar to‘plami.",
    ws_new: "+ Yangi maydon",
    ws_new_name_label: "Nomi",
    ws_new_name_placeholder: "Masalan, Marketing analitikasi",
    ws_empty: "Hali ishchi maydon yo‘q. Yangi maydon yarating va birinchi ulanishni qo‘shing.",
    ws_connections_count: (n) =>
      n === 1 ? "1 ta ulanish" : `${n} ta ulanish`,

    conn_status_pending: "Kutilmoqda",
    conn_status_profiling: "Aniqlanmoqda",
    conn_status_ready: "Tayyor",
    conn_status_error: "Xato",
    conn_status_auth_error: "Autentifikatsiya xatosi",

    health_ok: "Sog‘lom",
    health_fail: "Ishlamayapti",
    health_unknown: "Hech qachon tekshirilmagan",
    health_recheck: "Qayta tekshirish",

    chat_input_placeholder: "Ma’lumotlar bazasi haqida savol bering…",
    chat_send: "Yuborish",
    chat_thinking: "o‘ylanmoqda…",
    chat_running_node: (node) => `${node} bajarilmoqda…`,
    chat_similar_label: "Avval so‘ralgan",
    chat_dismiss: "Yopish",
    chat_no_response: "Javob yo‘q.",
    chat_workspace_pending:
      "Ulanish hali tayyor emas — profil yaratish tugashini kuting.",

    auth_login_title: "Kirish",
    auth_register_title: "Ro‘yxatdan o‘tish",
    auth_username_label: "Foydalanuvchi nomi",
    auth_email_label: "Elektron pochta",
    auth_password_label: "Parol",
    auth_login_button: "Kirish",
    auth_register_button: "Ro‘yxatdan o‘tish",
    auth_switch_to_register: "Hisobingiz yo‘qmi? Ro‘yxatdan o‘ting",
    auth_switch_to_login: "Hisobingiz bormi? Kiring",

    err_generic: "Nimadir noto‘g‘ri ketdi",
    err_network: "Tarmoq xatosi — qaytadan urinib ko‘ring",
    err_unauthorized: "Sessiyangiz tugadi — qayta kiring",
    err_not_found: "Topilmadi",
    err_validation: "Forma to‘g‘ri to‘ldirilmadi",
    err_too_large: "Hajm cheklovidan oshib ketdi",
    err_rate_limited: "Juda ko‘p so‘rov — biroz kutib qayta urinib ko‘ring",
  },

  ru: {
    nav_workspaces: "Рабочие пространства",
    nav_chat: "Чат",
    nav_settings: "Настройки",
    nav_admin: "Администратор",
    nav_sign_in: "Войти",
    nav_sign_out: "Выйти",

    btn_save: "Сохранить",
    btn_cancel: "Отмена",
    btn_delete: "Удалить",
    btn_create: "Создать",
    btn_refresh: "Обновить",
    btn_close: "Закрыть",
    btn_back: "Назад",
    btn_loading: "Загрузка…",

    ws_title: "Рабочие пространства",
    ws_subtitle:
      "Каждое рабочее пространство — это набор подключений к базам данных и документов.",
    ws_new: "+ Новое пространство",
    ws_new_name_label: "Название",
    ws_new_name_placeholder: "Например, Маркетинг-аналитика",
    ws_empty:
      "Нет рабочих пространств. Создайте первое и добавьте подключение.",
    ws_connections_count: (n) =>
      n === 1 ? "1 подключение" : `${n} подключений`,

    conn_status_pending: "В ожидании",
    conn_status_profiling: "Профилирование",
    conn_status_ready: "Готово",
    conn_status_error: "Ошибка",
    conn_status_auth_error: "Ошибка аутентификации",

    health_ok: "В порядке",
    health_fail: "Не отвечает",
    health_unknown: "Не проверялось",
    health_recheck: "Перепроверить",

    chat_input_placeholder: "Задайте вопрос о ваших данных…",
    chat_send: "Отправить",
    chat_thinking: "думаю…",
    chat_running_node: (node) => `Выполняется ${node}…`,
    chat_similar_label: "Уже спрашивали",
    chat_dismiss: "Закрыть",
    chat_no_response: "Нет ответа.",
    chat_workspace_pending:
      "Подключение ещё не готово — дождитесь окончания профилирования.",

    auth_login_title: "Вход",
    auth_register_title: "Регистрация",
    auth_username_label: "Имя пользователя",
    auth_email_label: "Электронная почта",
    auth_password_label: "Пароль",
    auth_login_button: "Войти",
    auth_register_button: "Зарегистрироваться",
    auth_switch_to_register: "Нет аккаунта? Зарегистрируйтесь",
    auth_switch_to_login: "Есть аккаунт? Войдите",

    err_generic: "Что-то пошло не так",
    err_network: "Сетевая ошибка — попробуйте ещё раз",
    err_unauthorized: "Сессия истекла — войдите заново",
    err_not_found: "Не найдено",
    err_validation: "Проверьте форму",
    err_too_large: "Превышен лимит размера",
    err_rate_limited:
      "Слишком много запросов — подождите и попробуйте снова",
  },

  en: {
    nav_workspaces: "Workspaces",
    nav_chat: "Chat",
    nav_settings: "Settings",
    nav_admin: "Admin",
    nav_sign_in: "Sign in",
    nav_sign_out: "Sign out",

    btn_save: "Save",
    btn_cancel: "Cancel",
    btn_delete: "Delete",
    btn_create: "Create",
    btn_refresh: "Refresh",
    btn_close: "Close",
    btn_back: "Back",
    btn_loading: "Loading…",

    ws_title: "Workspaces",
    ws_subtitle:
      "Each workspace groups database connections and document sources.",
    ws_new: "+ New workspace",
    ws_new_name_label: "Name",
    ws_new_name_placeholder: "e.g. Marketing analytics",
    ws_empty:
      "No workspaces yet. Create the first one and add a connection.",
    ws_connections_count: (n) =>
      n === 1 ? "1 connection" : `${n} connections`,

    conn_status_pending: "Pending",
    conn_status_profiling: "Profiling",
    conn_status_ready: "Ready",
    conn_status_error: "Error",
    conn_status_auth_error: "Auth error",

    health_ok: "Healthy",
    health_fail: "Unhealthy",
    health_unknown: "Never checked",
    health_recheck: "Recheck",

    chat_input_placeholder: "Ask anything about your data…",
    chat_send: "Send",
    chat_thinking: "thinking…",
    chat_running_node: (node) => `running ${node}…`,
    chat_similar_label: "Asked before",
    chat_dismiss: "Dismiss",
    chat_no_response: "No response.",
    chat_workspace_pending:
      "Connection isn’t ready — wait for profiling to finish.",

    auth_login_title: "Sign in",
    auth_register_title: "Create an account",
    auth_username_label: "Username",
    auth_email_label: "Email",
    auth_password_label: "Password",
    auth_login_button: "Sign in",
    auth_register_button: "Create account",
    auth_switch_to_register: "No account? Register",
    auth_switch_to_login: "Have an account? Sign in",

    err_generic: "Something went wrong",
    err_network: "Network error — please retry",
    err_unauthorized: "Session expired — please sign in again",
    err_not_found: "Not found",
    err_validation: "Please check the form",
    err_too_large: "Payload exceeds the size limit",
    err_rate_limited: "Too many requests — wait and try again",
  },
};

/** Browser detection: try the stored preference, fall back to
 *  `navigator.language`, finally `en`. Server side (Next SSR pass)
 *  always returns `en` so hydration is deterministic. */
export function detectLocale(): Locale {
  if (typeof window === "undefined") return "en";
  const stored = window.localStorage.getItem("qm.locale");
  if (stored === "uz" || stored === "ru" || stored === "en") {
    return stored;
  }
  const nav = (window.navigator.language || "en").toLowerCase();
  if (nav.startsWith("uz")) return "uz";
  if (nav.startsWith("ru")) return "ru";
  return "en";
}

export function persistLocale(locale: Locale): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem("qm.locale", locale);
  } catch {
    // localStorage unavailable (private mode / quota); silently
    // skip — the user will still see translations for the rest of
    // this session.
  }
}
