import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "./locales/en.json";
import ru from "./locales/ru.json";
import zh from "./locales/zh.json";
import ja from "./locales/ja.json";
import ptBR from "./locales/pt-BR.json";
const resources = {
  en: {
    translation: en,
  },
  ru: {
    translation: ru,
  },
  zh: {
    translation: zh,
  },
  ja: {
    translation: ja,
  },
  "pt-BR": {
    translation: ptBR,
  },
};

const storedLanguage = localStorage.getItem("language");
const initialLanguage =
  storedLanguage && storedLanguage !== "en" ? storedLanguage : "zh";

if (!storedLanguage || storedLanguage === "en") {
  localStorage.setItem("language", initialLanguage);
}

i18n.use(initReactI18next).init({
  resources,
  lng: initialLanguage,
  fallbackLng: "zh",
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
