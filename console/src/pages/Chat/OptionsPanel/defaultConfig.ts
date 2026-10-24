import type { TFunction } from "i18next";

const defaultConfig = {
  theme: {
    colorPrimary: "#FF7F16",
    darkMode: false,
    prefix: "lantai",
    leftHeader: {
      logo: "",
      title: "Work with lantai",
    },
  },
  sender: {
    attachments: true,
    maxLength: 10000,
    disclaimer: "",
  },
  welcome: {
    greeting: "Hello, how can I help you today?",
    description:
      "I'm Shen Xingzhi, ready to help you think through the next step.",
    avatar: "/online.svg",
    prompts: [],
  },
  api: {
    baseURL: "",
    token: "",
  },
} as const;

class ChatConfigProvider {
  getGreeting(t: TFunction): string {
    return t("chat.greeting");
  }

  getDescription(t: TFunction): string {
    return t("chat.description");
  }

  getPrompts(_t: TFunction): Array<{ value: string }> {
    return [];
  }

  getConfig(t: TFunction) {
    return {
      ...defaultConfig,
      sender: {
        ...defaultConfig.sender,
        disclaimer: t("chat.disclaimer"),
      },
      welcome: {
        ...defaultConfig.welcome,
        greeting: this.getGreeting(t),
        description: this.getDescription(t),
        prompts: this.getPrompts(t),
      },
    };
  }
}

const configProvider = new ChatConfigProvider();

export function getDefaultConfig(t: TFunction) {
  return configProvider.getConfig(t);
}

export default defaultConfig;

export type DefaultConfig = typeof defaultConfig;

// Export provider for extension
export { configProvider };
