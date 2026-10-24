"use strict";

// PptxGenJS 4.0.1 currently resolves image-size 1.x. The ICNS/JXL/HEIF
// parsers covered by the published infinite-loop advisories are not valid
// Presentation v3 inputs, so disable them before the PptxGenJS module loads.
const DISABLED_IMAGE_TYPES = ["icns", "jxl", "jxl-stream", "heif"];

function installImageParserMitigation(pptxgenjsModulePath) {
  if (!pptxgenjsModulePath) {
    throw new Error("PptxGenJS module path is required for image parser hardening");
  }
  const imageSizePath = require.resolve("image-size", {
    paths: [pptxgenjsModulePath],
  });
  const imageSize = require(imageSizePath);
  imageSize.disableTypes(DISABLED_IMAGE_TYPES);
  return imageSizePath;
}

module.exports = {
  DISABLED_IMAGE_TYPES,
  installImageParserMitigation,
};

if (process.env.LANTAI_PPTXGENJS_MODULE_PATH) {
  installImageParserMitigation(process.env.LANTAI_PPTXGENJS_MODULE_PATH);
}
