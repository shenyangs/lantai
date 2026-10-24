import { useState, useEffect, useRef } from "react";
import { useAppMessage } from "../../../../hooks/useAppMessage";
import { useTranslation } from "react-i18next";
import type {
  WikiIndexResponse,
  WikiInsightsResponse,
  WikiLintResponse,
  WikiPageSummary,
  WikiPurposeResponse,
  WikiReviewResponse,
  WikiSchemaResponse,
  WikiSearchResponse,
} from "../../../../api/types";
import { workspaceApi } from "../../../../api/modules/workspace";
import { useAgentStore } from "../../../../stores/agentStore";
import { isInternalWorkspaceFile } from "./utils";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../../utils/latestRequest";
import {
  requestDiscardChanges,
  useBeforeUnload,
} from "../../../../hooks/useUnsavedChanges";

const getParentDir = (filePath: string): string => {
  const match = filePath.match(/^(.*)[/\\]/);
  return match ? match[1] : filePath;
};

const toWikiFiles = (fileList: WikiPageSummary[]) =>
  fileList
    .filter((file) => !isInternalWorkspaceFile(file.filename))
    .sort((a, b) => a.title.localeCompare(b.title));

export const useAgentsData = () => {
  const { t } = useTranslation();
  const { selectedAgent } = useAgentStore();
  const [wikiIndex, setWikiIndex] = useState<WikiIndexResponse | null>(null);
  const [lint, setLint] = useState<WikiLintResponse | null>(null);
  const [insights, setInsights] = useState<WikiInsightsResponse | null>(null);
  const [schema, setSchema] = useState<WikiSchemaResponse | null>(null);
  const [review, setReview] = useState<WikiReviewResponse | null>(null);
  const [purpose, setPurpose] = useState<WikiPurposeResponse | null>(null);
  const [searchResults, setSearchResults] = useState<WikiSearchResponse | null>(
    null,
  );
  const [searchLoading, setSearchLoading] = useState(false);
  const [files, setFiles] = useState<WikiPageSummary[]>([]);
  const [selectedFile, setSelectedFile] = useState<WikiPageSummary | null>(
    null,
  );
  const [fileContent, setFileContent] = useState("");
  const [originalContent, setOriginalContent] = useState("");
  const [purposeContent, setPurposeContent] = useState("");
  const [originalPurposeContent, setOriginalPurposeContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [workspacePath, setWorkspacePath] = useState<string | null>(null);
  const { message, modal } = useAppMessage();
  const workspaceLoadGateRef = useRef(createLatestRequestGate());
  const fileLoadGateRef = useRef(createLatestRequestGate());
  const searchGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());
  const selectedFileRef = useRef<WikiPageSummary | null>(selectedFile);
  const previousAgentRef = useRef(selectedAgent);
  const hasChanges = fileContent !== originalContent;
  const purposeHasChanges = purposeContent !== originalPurposeContent;
  const draftSnapshotRef = useRef({
    selectedFile,
    fileContent,
    originalContent,
    purposeContent,
    originalPurposeContent,
    hasChanges,
    purposeHasChanges,
  });
  draftSnapshotRef.current = {
    selectedFile,
    fileContent,
    originalContent,
    purposeContent,
    originalPurposeContent,
    hasChanges,
    purposeHasChanges,
  };
  useBeforeUnload(hasChanges || purposeHasChanges);
  selectedFileRef.current = selectedFile;

  const persistDraft = (agentId: string) => {
    const draft = draftSnapshotRef.current;
    if (!draft.hasChanges && !draft.purposeHasChanges) return;
    try {
      window.sessionStorage.setItem(
        `lantai:wiki-draft:${agentId}`,
        JSON.stringify({
          filename: draft.selectedFile?.filename || null,
          fileContent: draft.fileContent,
          originalContent: draft.originalContent,
          purposeContent: draft.purposeContent,
          originalPurposeContent: draft.originalPurposeContent,
        }),
      );
    } catch {
      message.warning("未保存的 Wiki 草稿无法暂存，请先复制重要内容。 ");
    }
  };

  const handleFileClick = async (file: WikiPageSummary) => {
    if (isInternalWorkspaceFile(file.filename)) return;
    if (
      !(await requestDiscardChanges(modal, hasChanges, {
        title: "切换文件并舍弃当前修改？",
        content: `“${selectedFile?.title || selectedFile?.filename || "当前文件"}”仍有未保存的修改。`,
      }))
    )
      return;

    const ownsState = fileLoadGateRef.current.begin();
    setSelectedFile(file);
    selectedFileRef.current = file;
    setFileContent("");
    setOriginalContent("");
    setLoading(true);
    try {
      const data = await workspaceApi.loadWikiPage(file.filename);
      if (!ownsState()) return;
      setFileContent(data.content);
      setOriginalContent(data.content);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load wiki file", error);
      message.error(t("workspace.loadFailed"));
    } finally {
      if (ownsState()) setLoading(false);
    }
  };

  const fetchFiles = async () => {
    const ownsState = workspaceLoadGateRef.current.begin();
    try {
      const home = await workspaceApi.getWikiHome();
      if (!ownsState()) return;
      const wiki = home.wiki;
      const wikiLint = home.lint;
      const wikiSchema = home.structure;
      const wikiPurpose = home.purpose;
      const wikiInsights = home.insights;
      const wikiReview = home.review;
      const wikiFiles = toWikiFiles(wiki.pages);
      setWikiIndex(wiki);
      setLint(wikiLint);
      setSchema(wikiSchema);
      setPurpose(wikiPurpose);
      setPurposeContent(wikiPurpose.content);
      setOriginalPurposeContent(wikiPurpose.content);
      setInsights(wikiInsights);
      setReview(wikiReview);
      setFiles(wikiFiles);

      try {
        const draftKey = `lantai:wiki-draft:${selectedAgent}`;
        const rawDraft = window.sessionStorage.getItem(draftKey);
        if (rawDraft) {
          const draft = JSON.parse(rawDraft) as {
            filename?: string | null;
            fileContent?: string;
            originalContent?: string;
            purposeContent?: string;
            originalPurposeContent?: string;
          };
          const draftFile = wikiFiles.find(
            (file) => file.filename === draft.filename,
          );
          if (draftFile) {
            selectedFileRef.current = draftFile;
            setSelectedFile(draftFile);
            setFileContent(draft.fileContent || "");
            setOriginalContent(draft.originalContent || "");
          }
          setPurposeContent(draft.purposeContent ?? wikiPurpose.content);
          setOriginalPurposeContent(
            draft.originalPurposeContent ?? wikiPurpose.content,
          );
          window.sessionStorage.removeItem(draftKey);
          message.info("已恢复此前未保存的 Wiki 草稿。 ");
        }
      } catch {
        // Ignore malformed or unavailable session storage.
      }

      if (wikiFiles.length > 0) {
        setWorkspacePath(getParentDir(wikiFiles[0].path));
      } else if (wiki.sources.length > 0) {
        setWorkspacePath(getParentDir(wiki.sources[0].path));
      } else {
        setWorkspacePath("");
      }

      const activeFile = selectedFileRef.current;
      if (
        activeFile &&
        !wikiFiles.some((file) => file.filename === activeFile.filename)
      ) {
        selectedFileRef.current = null;
        setSelectedFile(null);
        setFileContent("");
        setOriginalContent("");
      }
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to fetch wiki files", error);
      message.error(t("workspace.loadFailed"));
    }
  };

  const refreshFiles = async () => {
    if (
      !(await requestDiscardChanges(modal, hasChanges || purposeHasChanges, {
        title: "刷新 Wiki 并舍弃未保存的修改？",
        content:
          "刷新会重新读取工作区内容，当前文件或 Wiki 说明中尚未保存的修改将被覆盖。",
        okText: "舍弃并刷新",
      }))
    )
      return;
    await fetchFiles();
  };

  useEffect(() => {
    const previousAgent = previousAgentRef.current;
    if (previousAgent !== selectedAgent) persistDraft(previousAgent);
    previousAgentRef.current = selectedAgent;
    workspaceLoadGateRef.current.invalidate();
    fileLoadGateRef.current.invalidate();
    searchGateRef.current.invalidate();
    selectedFileRef.current = null;
    setWikiIndex(null);
    setLint(null);
    setInsights(null);
    setSchema(null);
    setReview(null);
    setPurpose(null);
    setFiles([]);
    setWorkspacePath(null);
    setSelectedFile(null);
    setFileContent("");
    setOriginalContent("");
    setPurposeContent("");
    setOriginalPurposeContent("");
    setSearchResults(null);
    setLoading(false);
    setDeleting(false);
    void fetchFiles();
    return () => {
      workspaceLoadGateRef.current.invalidate();
      fileLoadGateRef.current.invalidate();
      searchGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAgent]);

  useEffect(() => {
    const handleWorkspaceChanged = () => {
      persistDraft(selectedAgent);
      workspaceLoadGateRef.current.invalidate();
      fileLoadGateRef.current.invalidate();
      searchGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
      selectedFileRef.current = null;
      setWikiIndex(null);
      setLint(null);
      setInsights(null);
      setSchema(null);
      setReview(null);
      setPurpose(null);
      setFiles([]);
      setWorkspacePath(null);
      setSelectedFile(null);
      setFileContent("");
      setOriginalContent("");
      setPurposeContent("");
      setOriginalPurposeContent("");
      setSearchResults(null);
      setLoading(false);
      setDeleting(false);
      void fetchFiles();
    };
    window.addEventListener("lantai:workspace-changed", handleWorkspaceChanged);
    return () => {
      window.removeEventListener(
        "lantai:workspace-changed",
        handleWorkspaceChanged,
      );
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleSave = async () => {
    if (!selectedFile || isInternalWorkspaceFile(selectedFile.filename)) return;

    const ownsState = mutationScopeRef.current.capture();
    setLoading(true);
    try {
      await workspaceApi.saveWikiPage(selectedFile.filename, fileContent);
      if (!ownsState()) return;
      setOriginalContent(fileContent);
      message.success(t("workspace.saveSuccess"));
      void fetchFiles();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to save wiki file", error);
      message.error(t("workspace.saveFailed"));
    } finally {
      if (ownsState()) setLoading(false);
    }
  };

  const handleReset = () => {
    setFileContent(originalContent);
  };

  const handleDelete = async () => {
    if (!selectedFile || isInternalWorkspaceFile(selectedFile.filename))
      return false;

    const ownsState = mutationScopeRef.current.capture();
    setDeleting(true);
    try {
      await workspaceApi.deleteWikiPage(selectedFile.filename);
      if (!ownsState()) return;
      message.success(t("workspace.deletePageSuccess"));
      setSelectedFile(null);
      setFileContent("");
      setOriginalContent("");
      await fetchFiles();
      return true;
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to delete wiki file", error);
      message.error(t("workspace.deletePageFailed"));
      return false;
    } finally {
      if (ownsState()) setDeleting(false);
    }
  };

  const handlePurposeSave = async () => {
    const ownsState = mutationScopeRef.current.capture();
    setLoading(true);
    try {
      await workspaceApi.saveWikiPurpose(purposeContent);
      if (!ownsState()) return;
      setOriginalPurposeContent(purposeContent);
      message.success(t("workspace.saveSuccess"));
      void fetchFiles();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to save wiki purpose", error);
      message.error(t("workspace.saveFailed"));
    } finally {
      if (ownsState()) setLoading(false);
    }
  };

  const handlePurposeReset = () => {
    setPurposeContent(originalPurposeContent);
  };

  const handleSearch = async (query: string) => {
    const normalizedQuery = query.trim();
    if (!normalizedQuery) {
      searchGateRef.current.invalidate();
      setSearchResults(null);
      setSearchLoading(false);
      return;
    }

    const ownsState = searchGateRef.current.begin();
    setSearchLoading(true);
    try {
      const result = await workspaceApi.searchWiki(normalizedQuery);
      if (ownsState()) setSearchResults(result);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to search wiki", error);
      message.error(t("workspace.searchFailed"));
    } finally {
      if (ownsState()) setSearchLoading(false);
    }
  };

  const handleRunIngest = async (
    mode: "fast" | "deep" = "fast",
    sourcePath = "",
  ) => {
    const ownsState = mutationScopeRef.current.capture();
    setLoading(true);
    try {
      await workspaceApi.runWikiIngest(mode, sourcePath);
      if (!ownsState()) return;
      message.success(
        mode === "deep"
          ? t("workspace.deepIngestQueued")
          : t("workspace.ingestRefreshed"),
      );
      await fetchFiles();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to run wiki ingest", error);
      message.error(t("workspace.ingestFailed"));
    } finally {
      if (ownsState()) setLoading(false);
    }
  };

  const handleResolveReview = async (
    itemId: string,
    status: "resolved" | "skipped",
  ) => {
    const ownsState = mutationScopeRef.current.capture();
    setLoading(true);
    try {
      await workspaceApi.resolveWikiReview(itemId, status);
      if (!ownsState()) return;
      message.success(t("workspace.reviewResolved"));
      await fetchFiles();
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to resolve wiki review", error);
      message.error(t("workspace.reviewResolveFailed"));
    } finally {
      if (ownsState()) setLoading(false);
    }
  };

  return {
    wikiIndex,
    lint,
    insights,
    schema,
    review,
    purpose,
    searchResults,
    searchLoading,
    files,
    sources: wikiIndex?.sources ?? [],
    collections: wikiIndex?.collections ?? [],
    graph: wikiIndex?.graph ?? { nodes: [], edges: [] },
    stats: wikiIndex?.stats ?? {},
    overview: wikiIndex?.overview ?? null,
    queueSummary: wikiIndex?.queue_summary ?? {
      pending: 0,
      running: 0,
      finished: 0,
      failed: 0,
      cancelled: 0,
      total: 0,
      latest: [],
    },
    selectedFile,
    fileContent,
    loading,
    deleting,
    workspacePath,
    hasChanges,
    purposeContent,
    purposeHasChanges,
    setFileContent,
    setPurposeContent,
    fetchFiles: refreshFiles,
    handleSearch,
    handleRunIngest,
    handleResolveReview,
    setSearchResults,
    handleFileClick,
    handleSave,
    handleReset,
    handleDelete,
    handlePurposeSave,
    handlePurposeReset,
  };
};
