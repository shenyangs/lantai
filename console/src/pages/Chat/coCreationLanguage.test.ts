import { describe, expect, it } from "vitest";
import {
  CO_CREATION_PHASE_LABELS,
  coCreationObjectLabelForFile,
  coCreationSurfaceForFile,
} from "./coCreationLanguage";

describe("Lantai co-creation language", () => {
  it("maps created objects to one shared surface grammar", () => {
    expect(coCreationSurfaceForFile("report.docx")).toBe("office");
    expect(coCreationSurfaceForFile("deck.pptx")).toBe("office");
    expect(coCreationSurfaceForFile("mini-app/index.html")).toBe("mini-app");
    expect(coCreationSurfaceForFile("dist/Lantai.app")).toBe("executable");
    expect(coCreationSurfaceForFile("src/main.py")).toBe("code");
  });

  it("keeps recovery and review states explicit", () => {
    expect(CO_CREATION_PHASE_LABELS.reviewing).toContain("审阅");
    expect(CO_CREATION_PHASE_LABELS.recovering).toContain("恢复");
  });

  it("names the active co-creation object in user-facing language", () => {
    expect(coCreationObjectLabelForFile("src/main.py")).toBe("代码");
    expect(coCreationObjectLabelForFile("mini-app/index.html")).toBe("小程序");
    expect(coCreationObjectLabelForFile("dist/Lantai.app")).toBe("可执行文件");
    expect(coCreationObjectLabelForFile("report.docx")).toBe("Office 文档");
  });
});
