import React from "react";
import {
  ChevronDown,
  ChevronUp,
  Copy,
  FileText,
  History,
  Image,
  Languages,
  MessageSquare,
  Mic,
  Monitor,
  Moon,
  PanelRight,
  Paperclip,
  Pencil,
  Pin,
  Plus,
  Search,
  SquarePen,
  Sun,
  Trash2,
  Type,
  Video,
  WandSparkles,
  Wrench,
  type LucideIcon,
} from "lucide-react";

type SparkIconProps = React.ComponentProps<LucideIcon> & {
  iconSize?: number | string;
};

function makeIcon(name: string, Icon: LucideIcon) {
  return function SparkIcon({ iconSize, size, ...props }: SparkIconProps) {
    const resolvedSize = size ?? iconSize ?? 18;
    return React.createElement(Icon, {
      "data-icon": name,
      size: resolvedSize,
      strokeWidth: props.strokeWidth ?? 1.8,
      ...props,
    } as React.ComponentProps<LucideIcon>);
  };
}

export const SparkAttachmentLine = makeIcon("SparkAttachmentLine", Paperclip);
export const SparkAudiouploadLine = makeIcon("SparkAudiouploadLine", Mic);
export const SparkChatTabFill = makeIcon("SparkChatTabFill", MessageSquare);
export const SparkChinese02Line = makeIcon("SparkChinese02Line", Languages);
export const SparkComputerLine = makeIcon("SparkComputerLine", Monitor);
export const SparkCopyLine = makeIcon("SparkCopyLine", Copy);
export const SparkDeleteLine = makeIcon("SparkDeleteLine", Trash2);
export const SparkDownLine = makeIcon("SparkDownLine", ChevronDown);
export const SparkEditLine = makeIcon("SparkEditLine", Pencil);
export const SparkEnglish02Line = makeIcon("SparkEnglish02Line", Languages);
export const SparkFilePdfLine = makeIcon("SparkFilePdfLine", FileText);
export const SparkHistoryLine = makeIcon("SparkHistoryLine", History);
export const SparkImageuploadLine = makeIcon("SparkImageuploadLine", Image);
export const SparkJapanLine = makeIcon("SparkJapanLine", Languages);
export const SparkMagicWandLine = makeIcon("SparkMagicWandLine", WandSparkles);
export const SparkMarkFill = makeIcon("SparkMarkFill", Pin);
export const SparkMarkLine = makeIcon("SparkMarkLine", Pin);
export const SparkMicLine = makeIcon("SparkMicLine", Mic);
export const SparkMoonLine = makeIcon("SparkMoonLine", Moon);
export const SparkNewChatFill = makeIcon("SparkNewChatFill", SquarePen);
export const SparkOperateRightLine = makeIcon(
  "SparkOperateRightLine",
  PanelRight,
);
export const SparkPictureLine = makeIcon("SparkPictureLine", Image);
export const SparkPlusLine = makeIcon("SparkPlusLine", Plus);
export const SparkPtLine = makeIcon("SparkPtLine", Languages);
export const SparkRusLine = makeIcon("SparkRusLine", Languages);
export const SparkSearchLine = makeIcon("SparkSearchLine", Search);
export const SparkSunLine = makeIcon("SparkSunLine", Sun);
export const SparkTextImageLine = makeIcon("SparkTextImageLine", Image);
export const SparkTextLine = makeIcon("SparkTextLine", Type);
export const SparkToolLine = makeIcon("SparkToolLine", Wrench);
export const SparkUpLine = makeIcon("SparkUpLine", ChevronUp);
export const SparkVideouploadLine = makeIcon("SparkVideouploadLine", Video);
