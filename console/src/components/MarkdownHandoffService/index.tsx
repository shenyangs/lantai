import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  MARKDOWN_HANDOFF_EVENT,
  isMarkdownHandoffEnvelope,
  storeMarkdownHandoff,
} from "./markdownHandoff";

export default function MarkdownHandoffService() {
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    const subscribe = window.electronAPI?.onMarkdownChatHandoff;
    if (!subscribe) return;
    return subscribe((envelope) => {
      if (!isMarkdownHandoffEnvelope(envelope)) return;
      storeMarkdownHandoff(envelope);
      if (location.pathname === "/" || location.pathname.startsWith("/chat")) {
        window.dispatchEvent(new CustomEvent(MARKDOWN_HANDOFF_EVENT));
        return;
      }
      navigate("/chat");
    });
  }, [location.pathname, navigate]);

  return null;
}
