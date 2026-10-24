import { useEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { requireOfficeWorkbenchResult } from "../pages/OfficeWorkbench/ipcResult";

const OFFICE_WORKBENCH_ROUTE = "/office-workbench";

export default function OfficeWorkbenchNavigationService() {
  const navigate = useNavigate();
  const location = useLocation();
  const currentPathRef = useRef(location.pathname);
  const previousPathRef = useRef(location.pathname);
  currentPathRef.current = location.pathname;

  useEffect(() => {
    const api = window.electronAPI?.officeWorkbench;
    if (!api) return undefined;
    let disposed = false;
    let receivedPresentEvent = false;
    const pathWhenStateRequested = currentPathRef.current;
    const present = (state: OfficeWorkbenchState) => {
      receivedPresentEvent = true;
      if (!disposed && state?.active) navigate(OFFICE_WORKBENCH_ROUTE);
    };
    const unsubscribe = api.onPresent(present);
    void api
      .getState()
      .then((result) =>
        requireOfficeWorkbenchResult(result, "无法读取本地编辑器状态"),
      )
      .then((state) => {
        if (disposed || receivedPresentEvent || !state?.active) return;
        if (currentPathRef.current === OFFICE_WORKBENCH_ROUTE) return;
        // Finder/argv document opens can create the native view before the
        // React shell has subscribed to the presentation event. Recover that
        // durable state on cold start, but do not drag a user back after they
        // deliberately navigated elsewhere while getState() was pending.
        if (currentPathRef.current === pathWhenStateRequested) {
          navigate(OFFICE_WORKBENCH_ROUTE);
          return;
        }
        void api
          .hide()
          .then((result) =>
            requireOfficeWorkbenchResult(result, "无法隐藏本地编辑器"),
          )
          .catch(() => {});
      })
      .catch(() => {});
    return () => {
      disposed = true;
      unsubscribe?.();
    };
  }, [navigate]);

  useEffect(() => {
    const previousPath = previousPathRef.current;
    previousPathRef.current = location.pathname;
    if (
      previousPath === OFFICE_WORKBENCH_ROUTE &&
      location.pathname !== OFFICE_WORKBENCH_ROUTE
    ) {
      void window.electronAPI?.officeWorkbench
        ?.hide()
        .then((result) =>
          requireOfficeWorkbenchResult(result, "无法隐藏本地编辑器"),
        )
        .catch(() => {});
    }
  }, [location.pathname]);

  return null;
}
