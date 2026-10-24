import { useState, useEffect, useRef } from "react";
import { useAppMessage } from "../../../hooks/useAppMessage";
import api from "../../../api";
import type { CronJobSpecOutput } from "../../../api/types";
import { useAgentStore } from "../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";

type CronJob = CronJobSpecOutput;

function normalizeJobs(data: unknown): CronJob[] {
  return Array.isArray(data) ? (data as CronJob[]) : [];
}

export function useCronJobs() {
  const { selectedAgent } = useAgentStore();
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const { message } = useAppMessage();
  const jobsLoadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());

  const fetchJobs = async () => {
    const ownsState = jobsLoadGateRef.current.begin();
    setLoading(true);
    setError("");
    try {
      const data = await api.listCronJobs();
      if (!ownsState()) return;
      setJobs(normalizeJobs(data));
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load cron jobs", error);
      setError("定时任务加载失败。为避免误判，当前不会显示为空列表。 ");
      message.error("定时任务加载失败");
    } finally {
      if (ownsState()) setLoading(false);
    }
  };

  useEffect(() => {
    setJobs([]);
    void fetchJobs();
    return () => {
      jobsLoadGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
    };
  }, [selectedAgent]);

  const createJob = async (values: CronJob) => {
    const ownsState = mutationScopeRef.current.capture();
    try {
      const created = await api.createCronJob(values);
      if (!ownsState()) return true;
      setJobs((prev) => [created as CronJob, ...prev]);
      message.success("Created successfully");
      return true;
    } catch (error) {
      if (!ownsState()) return false;
      console.error("Failed to create cron job", error);
      message.error("Failed to save");
      return false;
    }
  };

  const updateJob = async (jobId: string, values: CronJob) => {
    const ownsState = mutationScopeRef.current.capture();
    const original = jobs.find((j) => j.id === jobId);
    const optimisticUpdate = { ...original, ...values };
    setJobs((prev) => prev.map((j) => (j.id === jobId ? optimisticUpdate : j)));

    try {
      const updated = await api.replaceCronJob(jobId, values);
      if (!ownsState()) return true;
      setJobs((prev) =>
        prev.map((j) => (j.id === jobId ? (updated as CronJob) : j)),
      );
      message.success("Updated successfully");
      return true;
    } catch (error) {
      if (!ownsState()) return false;
      console.error("Failed to update cron job", error);
      if (original) {
        setJobs((prev) => prev.map((j) => (j.id === jobId ? original : j)));
      }
      message.error("Failed to save");
      return false;
    }
  };

  const deleteJob = async (jobId: string) => {
    const ownsState = mutationScopeRef.current.capture();
    const original = jobs.find((j) => j.id === jobId);
    setJobs((prev) => prev.filter((j) => j.id !== jobId));

    try {
      await api.deleteCronJob(jobId);
      if (!ownsState()) return true;
      message.success("Deleted successfully");
      return true;
    } catch (error) {
      if (!ownsState()) return false;
      console.error("Failed to delete cron job", error);
      if (original) {
        setJobs((prev) => [...prev, original]);
      }
      message.error("Failed to delete");
      return false;
    }
  };

  const toggleEnabled = async (job: CronJob) => {
    const ownsState = mutationScopeRef.current.capture();
    const updated = { ...job, enabled: !job.enabled };
    setJobs((prev) => prev.map((j) => (j.id === job.id ? updated : j)));

    try {
      const returned = await api.replaceCronJob(job.id, updated);
      if (!ownsState()) return true;
      setJobs((prev) =>
        prev.map((j) => (j.id === job.id ? (returned as CronJob) : j)),
      );
      message.success(`${updated.enabled ? "Enabled" : "Disabled"}`);
      return true;
    } catch (error) {
      if (!ownsState()) return false;
      console.error("Failed to toggle cron job", error);
      setJobs((prev) => prev.map((j) => (j.id === job.id ? job : j)));
      message.error("Operation failed");
      return false;
    }
  };

  const executeNow = async (jobId: string) => {
    const ownsState = mutationScopeRef.current.capture();
    try {
      await api.triggerCronJob(jobId);
      if (!ownsState()) return true;
      message.success("Task triggered successfully");
      return true;
    } catch (error) {
      if (!ownsState()) return false;
      console.error("Failed to execute cron job", error);
      message.error("Failed to execute");
      return false;
    }
  };

  return {
    jobs,
    loading,
    error,
    fetchJobs,
    createJob,
    updateJob,
    deleteJob,
    toggleEnabled,
    executeNow,
  };
}
