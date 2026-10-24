import { Checkbox, Modal } from "@/vendor/lantaiDesign";
import type { TFunction } from "i18next";

const FREE_MODEL_WARNING_DISABLED_KEY =
  "lantai_free_model_switch_warning_disabled";

interface FreeModelWarningProvider {
  id: string;
  base_url?: string;
}

interface FreeModelWarningModel {
  is_free?: boolean;
}

interface ConfirmFreeModelSwitchOptions {
  provider: FreeModelWarningProvider;
  model: FreeModelWarningModel;
  t: TFunction;
}

function isWarningDisabled(): boolean {
  return localStorage.getItem(FREE_MODEL_WARNING_DISABLED_KEY) === "1";
}

function disableWarning(): void {
  localStorage.setItem(FREE_MODEL_WARNING_DISABLED_KEY, "1");
}

export async function confirmFreeModelSwitch({
  model,
  t,
}: ConfirmFreeModelSwitchOptions): Promise<boolean> {
  if (!model.is_free || isWarningDisabled()) {
    return true;
  }

  let dontShowAgain = false;

  return new Promise<boolean>((resolve) => {
    let settled = false;

    const settle = (value: boolean) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };

    Modal.confirm({
      title: t("models.freeModelWarningTitle"),
      content: (
        <div>
          <div>{t("models.freeModelWarningMessage")}</div>
          <div className="lantai-free-model-warning-checkbox-row">
            <Checkbox
              onChange={(event) => {
                dontShowAgain = Boolean(event?.target?.checked);
              }}
            >
              {t("models.freeModelWarningDontShowAgain")}
            </Checkbox>
          </div>
        </div>
      ),
      okText: t("common.confirm"),
      cancelText: t("common.cancel"),
      onOk: () => {
        if (dontShowAgain) {
          disableWarning();
        }
        settle(true);
      },
      onCancel: () => settle(false),
      afterClose: () => settle(false),
    });
  });
}
