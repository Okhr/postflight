/**
 * Live pipeline state, pushed rather than polled.
 *
 * The API already streams the queued and running jobs over SSE; this puts that
 * one connection at the root and makes it drive the rest of the UI. When a job
 * appears, changes state or finishes, whatever is on screen describing the
 * pipeline is stale, so we invalidate it and it catches up at once, on any tab.
 *
 * Progress arrives every second too, but progress alone does not make a list
 * stale: we compare the identities and states only, or every bar tick would
 * trigger a round of refetches.
 */
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";

import type { Job } from "@/lib/api";

const LiveJobsContext = createContext<Job[]>([]);

/** Queued and running jobs, straight from the worker's table. */
export function useLiveJobs(): Job[] {
  return useContext(LiveJobsContext);
}

// Everything that describes pipeline state.
//
// `["sequence"]` covers one rush's detail, and it has to be in here: the derush page
// reads that key and nothing else, so a finished proxy left it saying "the proxy is
// not ready yet" until the page was reloaded. It is safe because this list is only
// invalidated when a job's identity or state changes, never on a progress tick, and
// because the marks being edited live in local state that a refetch does not touch.
//
// `["grade", id]` stays out: its params *are* the slider positions, so refetching
// under the cursor would move them while they are being dragged.
const STALE_ON_JOB_CHANGE = [
  ["sequences"],
  ["sequence"],
  ["renders"],
  ["grades"],
  ["status"],
];

export function LiveJobsProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [jobs, setJobs] = useState<Job[]>([]);

  useEffect(() => {
    let signature = "";
    const source = new EventSource("/api/jobs/stream");

    source.onmessage = (event) => {
      let next: Job[];
      try {
        next = JSON.parse(event.data) as Job[];
      } catch {
        return; // partial frame
      }
      setJobs(next);

      const identities = next.map((job) => `${job.id}:${job.state}`).join(",");
      if (identities === signature) return;
      signature = identities;
      for (const queryKey of STALE_ON_JOB_CHANGE) {
        queryClient.invalidateQueries({ queryKey });
      }
    };

    // No close() on error: EventSource reconnects on its own, whereas closing
    // here would freeze the UI after a single hiccup, and an API restart is enough.
    return () => source.close();
  }, [queryClient]);

  return <LiveJobsContext.Provider value={jobs}>{children}</LiveJobsContext.Provider>;
}
