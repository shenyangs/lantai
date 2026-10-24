const SHEN_XINGZHI_AVATAR_URL = new URL('../../../../../public/lantai.png', import.meta.url).href

export interface OfficeAgentAvatarProps {
  readonly size?: number | undefined
}

/** The same Shen Xingzhi identity portrait used by Lantai's primary chat. */
export function OfficeAgentAvatar({ size = 22 }: OfficeAgentAvatarProps): React.JSX.Element {
  return (
    <img
      className="lt-office-agent-avatar"
      src={SHEN_XINGZHI_AVATAR_URL}
      alt=""
      aria-hidden="true"
      draggable={false}
      style={{ width: size, height: size }}
    />
  )
}
