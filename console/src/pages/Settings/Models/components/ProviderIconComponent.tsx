import React, { useState } from "react";
import { providerIcon } from "./providerIcon";
import {
  getProviderLetterColor,
  getProviderLetter,
} from "./providerLetterIcon";

interface ProviderIconProps {
  providerId: string;
  size?: number;
}

/**
 * Renders a local provider icon when one exists, otherwise a letter avatar.
 */
export const ProviderIcon: React.FC<ProviderIconProps> = ({
  providerId,
  size = 32,
}) => {
  const rawUrl = providerIcon(providerId);
  const imageUrl = rawUrl || undefined;
  const [imageFailed, setImageFailed] = useState(false);

  const borderRadius = size * 0.25;

  if (imageUrl && !imageFailed) {
    return (
      <img
        src={imageUrl}
        alt={providerId}
        width={size}
        height={size}
        style={{ borderRadius, objectFit: "cover", flexShrink: 0 }}
        onError={() => setImageFailed(true)}
      />
    );
  }

  const backgroundColor = getProviderLetterColor(providerId);
  const letter = getProviderLetter(providerId);
  const fontSize = size * 0.45;

  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius,
        backgroundColor,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "#fff",
        fontSize,
        fontWeight: 600,
        fontFamily: "Inter, sans-serif",
        userSelect: "none",
        flexShrink: 0,
      }}
      title={providerId}
    >
      {letter}
    </div>
  );
};
