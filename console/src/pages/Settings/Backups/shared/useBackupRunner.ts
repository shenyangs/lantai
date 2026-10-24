/**
 * Shared hook that drives the SSE backup-creation stream.
 * Owns progress/loading/abort state so both CreateBackupModal (user-initiated)
 * and SilentBackupModal (auto pre-restore) can share identical streaming logic.
 */
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import api from "@/api";
import { useAppMessage } from "@/hooks/useAppMessage";
import type { CreateBackupRequest } from "@/api/types/backup";
import { handleBackupProgressEvent } from "./progress";

interface UseBackupRunnerOptions {
  onSuccess?: () => void;
  onClose?: () => void;
}

export type BackupRunnerPhase =
  | "idle"
  | "running"
  | "stop_requested"
  | "failed";

/**
 * Returns the observable stream state and separates closing the observer from
 * requesting that the server stop at its next safe cancellation point.
 * - start(data): opens the SSE stream, updates progress state on every event.
 * - closeObservation(): hides the progress modal without touching the request.
 * - requestStop(): aborts the stream, but keeps an explicit unverified state.
 * - reset(): clears an inactive or acknowledged state for a fresh run.
 */
export function useBackupRunner({
  onSuccess,
  onClose,
}: UseBackupRunnerOptions) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [phase, setPhase] = useState<BackupRunnerPhase>("idle");
  const [progress, setProgress] = useState(0);
  const [progressMsg, setProgressMsg] = useState("");
  const abortControllerRef = useRef<AbortController | null>(null);
  const phaseRef = useRef<BackupRunnerPhase>("idle");

  const updatePhase = (next: BackupRunnerPhase) => {
    phaseRef.current = next;
    setPhase(next);
  };
  const stopWasRequested = () => phaseRef.current === "stop_requested";

  /** Clears an inactive state. A running request must never be reset implicitly. */
  const reset = () => {
    if (phaseRef.current === "running") return false;
    updatePhase("idle");
    setProgress(0);
    setProgressMsg("");
    return true;
  };

  /** Starts the backup stream. Resolves when the server sends the "done" event. */
  const start = async (data: CreateBackupRequest) => {
    if (phaseRef.current !== "idle" || abortControllerRef.current) return;
    const controller = new AbortController();
    abortControllerRef.current = controller;
    updatePhase("running");
    setProgress(0);
    setProgressMsg(t("backup.progressStarting"));

    try {
      await api.createBackupStream(
        data,
        (event) => {
          const { progress: p, msg } = handleBackupProgressEvent(event, t);
          setProgress(p);
          setProgressMsg(msg);
        },
        controller.signal,
      );
      if (controller.signal.aborted || stopWasRequested()) {
        return;
      }
      updatePhase("idle");
      message.success(t("backup.createSuccess"));
      onSuccess?.();
      onClose?.();
    } catch (err) {
      if (
        typeof err === "object" &&
        err !== null &&
        "name" in err &&
        err.name === "AbortError"
      ) {
        if (phaseRef.current !== "idle") updatePhase("stop_requested");
        return;
      }
      updatePhase("failed");
      message.error(t("backup.createFailed"));
    } finally {
      if (abortControllerRef.current === controller) {
        abortControllerRef.current = null;
      }
    }
  };

  /** Hides this observer only. The owned backup request continues unchanged. */
  const closeObservation = () => {
    onClose?.();
  };

  /**
   * Requests a safe stop. Aborting the response cannot prove whether the
   * backend stopped before or after committing the final archive, so the UI
   * remains in an explicit state that requires list verification.
   */
  const requestStop = () => {
    if (phaseRef.current !== "running" || !abortControllerRef.current) return;
    updatePhase("stop_requested");
    setProgressMsg("停止请求已发送；执行结果待核实");
    abortControllerRef.current.abort();
  };

  const loading = phase === "running";
  const active = phase === "running" || phase === "stop_requested";

  return {
    phase,
    loading,
    active,
    progress,
    progressMsg,
    start,
    closeObservation,
    requestStop,
    reset,
  };
}
