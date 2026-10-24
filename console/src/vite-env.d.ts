/// <reference types="vite/client" />

declare module "dayjs" {
  interface Dayjs {
    fromNow(withoutSuffix?: boolean): string;
  }
}

declare module "*.less" {
  const classes: { [key: string]: string };
  export default classes;
}

interface PyWebViewAPI {
  open_external_link: (url: string) => void;
  save_file: (url: string, filename: string) => Promise<boolean>;
}

declare global {
  interface OfficeWorkbenchState {
    active: boolean;
    workbench_id?: string;
    kind?: "docs" | "sheets" | "slides" | "pdf";
    display_name?: string;
    source_chat_id?: string;
    route: "/office-workbench";
    visible: boolean;
    bounds?: { x: number; y: number; width: number; height: number } | null;
  }

  interface OfficeWorkbenchIpcFailure {
    success: false;
    error?: string;
    error_code?: string;
  }

  interface BrowserWorkbenchState {
    ok: boolean;
    active_tab_id?: string | null;
    profile_id?: string;
    tabs: Array<{
      id: string;
      profile_id: string;
      title: string;
      url: string;
      loading: boolean;
      can_go_back?: boolean;
      can_go_forward?: boolean;
      background?: boolean;
      task_group_id?: string;
    }>;
    current_action?: string;
    agent_status?: string;
    message?: string;
    error?: string;
    error_code?: string;
    surface?: {
      mode: "hidden" | "window" | "sidecar_preview" | "sidecar_interactive";
      visible: boolean;
      control_owner: "agent" | "user";
      run_id: string;
      bounds: { x: number; y: number; width: number; height: number } | null;
    };
  }

  interface BrowserSidecarPreview {
    ok: boolean;
    runtime?: "electron_bridge";
    page_id?: string;
    url?: string;
    title?: string;
    data_url?: string;
    captured_at?: string;
    error?: string;
    error_code?: string;
  }

  interface BrowserWorkbenchDesktopAPI {
    open?: (payload?: {
      profile_id?: string;
      url?: string;
    }) => Promise<BrowserWorkbenchState>;
    action?: (
      payload: Record<string, unknown>,
    ) => Promise<BrowserWorkbenchState>;
    getState?: () => Promise<BrowserWorkbenchState>;
    mountSidecar?: (payload: {
      run_id?: string;
      profile_id?: string;
      url?: string;
      bounds: { x: number; y: number; width: number; height: number };
    }) => Promise<BrowserWorkbenchState>;
    layoutSidecar?: (bounds: {
      x: number;
      y: number;
      width: number;
      height: number;
    }) => Promise<BrowserWorkbenchState>;
    setSidecarControl?: (
      owner: "agent" | "user",
    ) => Promise<BrowserWorkbenchState>;
    captureSidecarPreview?: () => Promise<BrowserSidecarPreview>;
    unmountSidecar?: () => Promise<BrowserWorkbenchState>;
    popoutSidecar?: () => Promise<BrowserWorkbenchState>;
    focusExternalBrowser?: (browserName?: string) => Promise<{
      ok: boolean;
      browser_name?: string;
      error?: string;
      error_code?: string;
    }>;
    onState?: (callback: (state: BrowserWorkbenchState) => void) => () => void;
  }

  interface DesktopFileGrantSelection {
    ok?: boolean;
    path: string;
    security_scoped_bookmark?: boolean;
    security_scope_ref?: string;
    security_scope_bookmark_sha256?: string;
    security_scope_bookmark_persisted?: boolean;
    path_sha256?: string;
    error?: string;
    error_code?: string;
  }

  interface DesktopWorkspaceGrantSelection {
    ok: boolean;
    selectionReceipt?: string;
    projectRoot?: string;
    displayName?: string;
    accessMode?: "read_only" | "read_write";
    trustState?: "trusted" | "restricted";
    error?: string;
    error_code?: string;
  }

  interface DesktopClipboardAttachment {
    name: string;
    type?: string;
    size?: number;
    lastModified?: number;
    dataBase64: string;
  }

  interface DesktopChatAttachmentUploadResult {
    ok: boolean;
    url?: string;
    file_name?: string;
    stored_name?: string;
    size?: number;
    digest?: string;
    version?: number;
    upload_receipt?: string;
    status?: number;
    error?: string;
    error_code?: string;
  }

  interface DesktopSystemBehaviorSettings {
    launch_at_login: boolean;
    keep_awake_while_running: boolean;
    turn_completion_notification: "never" | "when_unfocused" | "always";
    permission_notification: boolean;
  }

  interface MacUseDesktopResult {
    ok: boolean;
    action?: string;
    run_id?: string;
    dispatch_status?: "not_sent" | "sent" | "transport_failed" | "unknown";
    effect_status?:
      | "not_applicable"
      | "verified"
      | "no_effect_verified"
      | "ambiguous"
      | "failed";
    message?: string;
    error?: string;
    error_code?: string;
    data?: Record<string, unknown>;
  }

  interface DesktopLaunchServicesCopy {
    path: string;
    version: string;
    in_trash?: boolean;
  }

  interface DesktopLaunchServicesState {
    ok: boolean;
    supported: boolean;
    bundle_id?: string;
    current_app?: { path: string; version: string };
    registrations?: DesktopLaunchServicesCopy[];
    stale_registrations?: DesktopLaunchServicesCopy[];
    disk_copies?: DesktopLaunchServicesCopy[];
    stale_count?: number;
    disk_copy_count?: number;
    removed_copy_count?: number;
    error?: string;
  }

  interface DesktopAppIconOption {
    id: string;
    name: string;
    group: "default" | "brush" | "signature" | "experimental" | "custom";
    previewUrl: string;
  }

  interface DesktopAppIconState {
    ok: boolean;
    supported: boolean;
    selectedId: string;
    defaultIcon: DesktopAppIconOption;
    builtinIcons: DesktopAppIconOption[];
    customIcon: DesktopAppIconOption & { available: boolean };
    designSpec: string;
    canceled?: boolean;
    error?: string;
  }

  type DesktopComputerPermissionKey =
    | "accessibility"
    | "full_disk_access"
    | "screen_recording"
    | "automation"
    | "calendar";

  type DesktopComputerPermissionStatusValue =
    | "granted"
    | "denied"
    | "unknown"
    | "unsupported";

  interface DesktopComputerPermissionStatus {
    ok: boolean;
    platform: string;
    app_path?: string | null;
    permissions: Array<{
      key: DesktopComputerPermissionKey;
      status: DesktopComputerPermissionStatusValue;
      can_check: boolean;
    }>;
    error?: string;
  }

  type WindowMaterialMode = "solid" | "native" | "vibrancy" | "fallback";
  interface LantaiElectronAPI {
    setWindowMaterial?: (options: {
      enabled: boolean;
      dark: boolean;
    }) => Promise<WindowMaterialMode>;
    onWindowMaterialChanged?: (
      callback: (mode: WindowMaterialMode) => void,
    ) => () => void;
    platform: string;
    apiBaseUrl: string;
    isDesktop: boolean;
    versions?: {
      electron?: string;
      chrome?: string;
      node?: string;
    };
    selectDirectory?: () => Promise<string | null>;
    selectWorkspaceGrant?: (payload: {
      agentId: string;
      accessMode?: "read_only" | "read_write";
      trustState?: "trusted" | "restricted";
    }) => Promise<DesktopWorkspaceGrantSelection | null>;
    restoreWorkspace?: (payload: {
      agentId: string;
      expectedPath: string;
    }) => Promise<{
      ok: boolean;
      stagedPath?: string;
      sourcePath?: string;
      error?: string;
      error_code?: string;
    } | null>;
    selectFile?: () => Promise<string | null>;
    selectFileGrant?: () => Promise<DesktopFileGrantSelection | null>;
    readClipboardAttachments?: (payload?: {
      preferredName?: string;
      mimeTypes?: string[];
    }) => Promise<{
      attachments: DesktopClipboardAttachment[];
      error?: string;
      error_code?: string;
    }>;
    uploadChatAttachment?: (payload: {
      fileName: string;
      mimeType: string;
      bytes: ArrayBuffer;
      agentId?: string;
      workspaceGrantId?: string;
    }) => Promise<DesktopChatAttachmentUploadResult>;
    copyImageFromUrl?: (payload: {
      url: string;
      localPath?: string;
      localPaths?: string[];
    }) => Promise<{ success: boolean; error?: string; error_code?: string }>;
    saveImageFromUrl?: (payload: {
      url: string;
      localPath?: string;
      localPaths?: string[];
    }) => Promise<{
      success: boolean;
      canceled?: boolean;
      path?: string;
      error?: string;
      error_code?: string;
    }>;
    saveFileAs?: (payload: {
      sourcePath: string;
      filename?: string;
    }) => Promise<{
      success: boolean;
      canceled?: boolean;
      path?: string;
      size_bytes?: number;
      error?: string;
      error_code?: string;
    }>;
    openExternal?: (
      url: string,
    ) => Promise<{ success: boolean; error?: string }>;
    openPath?: (
      targetPath: string,
    ) => Promise<{ success: boolean; error?: string }>;
    openHtmlWork?: (
      targetPath: string,
      title?: string,
    ) => Promise<{ success: boolean; error?: string }>;
    createWorkThumbnail?: (targetPath: string) => Promise<{
      success: boolean;
      data_url?: string;
      error?: string;
    }>;
    openDocxEditor?: (
      targetPath: string,
      options?: { source_chat_id?: string; agent_id?: string },
    ) => Promise<{ success: boolean; error?: string; error_code?: string }>;
    openOfficeEditor?: (
      targetPath: string,
      options?: { source_chat_id?: string; agent_id?: string },
    ) => Promise<{
      success: boolean;
      workbench_id?: string;
      route?: "/office-workbench";
      reused?: boolean;
      error?: string;
      error_code?: string;
    }>;
    officeWorkbench?: {
      getState: () => Promise<
        OfficeWorkbenchState | OfficeWorkbenchIpcFailure | null
      >;
      layout: (payload: {
        workbench_id: string;
        bounds: { x: number; y: number; width: number; height: number };
      }) => Promise<{ success: true } | OfficeWorkbenchIpcFailure | undefined>;
      hide: () => Promise<
        { success: true } | OfficeWorkbenchIpcFailure | undefined
      >;
      onPresent: (
        callback: (state: OfficeWorkbenchState) => void,
      ) => () => void;
    };
    getPathForFile?: (file: File) => string;
    onDocxRewriteRequest?: (
      callback: (envelope: {
        request_id: string;
        payload: {
          success: true;
          instruction: string;
          selected_text: string;
          document_before: string;
          document_after: string;
          current_draft: string;
          document_name: string;
          source_chat_id: string;
        };
      }) => void,
    ) => () => void;
    respondDocxRewriteRequest?: (envelope: {
      request_id: string;
      result:
        | {
            success: true;
            replacement_text: string;
            summary?: string;
            model?: string;
          }
        | {
            success: false;
            error: string;
            error_code?: string;
          };
    }) => void;
    onMarkdownChatHandoff?: (
      callback: (envelope: {
        protocol_version: "2.0";
        mode: "ask" | "suggest" | "execute";
        path: string;
        document_name: string;
        text: string;
        selection_only: boolean;
      }) => void,
    ) => () => void;
    revealPath?: (
      targetPath: string,
    ) => Promise<{ success: boolean; error?: string }>;
    openWith?: (targetPath: string) => Promise<{
      success: boolean;
      canceled?: boolean;
      revealed?: boolean;
      error?: string;
      error_code?: string;
    }>;
    getAppVersion?: () => Promise<{
      ok: boolean;
      version?: string;
      error?: string;
    }>;
    getComputerPermissionStatus?: () => Promise<DesktopComputerPermissionStatus>;
    requestComputerPermission?: (key: DesktopComputerPermissionKey) => Promise<{
      ok: boolean;
      key: DesktopComputerPermissionKey;
      status: DesktopComputerPermissionStatusValue;
      error?: string;
    }>;
    getMacUseStatus?: () => Promise<MacUseDesktopResult>;
    stopMacUse?: (runId: string) => Promise<MacUseDesktopResult>;
    selectMacUseTarget?: (target: {
      app_bundle_id: string;
      window_id: string;
      process_id: number;
      process_started_at: number;
      window_title?: string;
    }) => Promise<MacUseDesktopResult>;
    requestMacUsePermission?: (
      key: "accessibility" | "screen_recording",
    ) => Promise<MacUseDesktopResult>;
    getSystemBehavior?: () => Promise<{
      ok: boolean;
      preferences?: DesktopSystemBehaviorSettings & {
        task_running?: boolean;
        keep_awake_active?: boolean;
      };
      error?: string;
    }>;
    getLaunchServicesState?: () => Promise<DesktopLaunchServicesState>;
    cleanLaunchServices?: (payload?: {
      remove_copies?: boolean;
    }) => Promise<DesktopLaunchServicesState>;
    getAppIcon?: () => Promise<DesktopAppIconState>;
    selectAppIcon?: (id: string) => Promise<DesktopAppIconState>;
    importCustomAppIcon?: () => Promise<DesktopAppIconState>;
    updateSystemBehavior?: (
      payload: Partial<DesktopSystemBehaviorSettings>,
    ) => Promise<{
      ok: boolean;
      preferences?: DesktopSystemBehaviorSettings;
      error?: string;
    }>;
    setTaskActivity?: (payload: {
      sourceId: string;
      running: boolean;
    }) => Promise<{ ok: boolean; error?: string }>;
    notifySystemBehavior?: (payload: {
      id: string;
      type: "turn_completed" | "permission_required" | "daily_brief" | "reminder";
      receiptId?: string;
      run_id?: string;
      attempt_id?: string;
      agent_id?: string;
      reminder_id?: string;
      caller_id?: string;
      receipt_token?: string;
      title?: string;
      body?: string;
    }) => Promise<{
      ok: boolean;
      id?: string;
      status?: "shown" | "failed" | "unknown";
      run_id?: string;
      attempt_id?: string;
      agent_id?: string;
      reminder_id?: string;
      caller_id?: string;
      receipt_token?: string;
      shown?: boolean;
      reason?: string;
      error?: string;
    }>;
    onDailyBriefOpen?: (
      callback: (payload: { receiptId: string }) => void,
    ) => () => void;
    onReminderOpen?: (
      callback: (payload: { reminderId: string; agentId: string }) => void,
    ) => () => void;
    browserWorkbench?: BrowserWorkbenchDesktopAPI;
  }

  interface Window {
    pywebview?: {
      api: PyWebViewAPI;
    };
    electronAPI?: LantaiElectronAPI;
  }
}

export {};
